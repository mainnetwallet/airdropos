"""Persistent, isolated execution context for one task.

This is the infrastructure the master specification calls for in §5 and §6: the
knowledge that must survive a tool call, a switch between integrations, a replan, a
worker restart and an application restart.

What it stores, per task and in structured rows rather than in a prompt:

* the original goal and the objective it was normalised to,
* every action attempted, the arguments used, what came back, and what state was
  observed afterwards,
* requirements discovered while executing, and which of them are still outstanding,
* references to the external resources involved (page, chat, session, wallet),
* the reason the current next action is the next action,
* human-intervention and authorization state,
* errors, retries and the recovery decision taken.

Three deliberate design points, each of which closes a specific way context is lost or
mixed:

**Attempted is not confirmed.** An observation records what was tried *and* what the
external system then looked like, in separate columns, and the outcome vocabulary keeps
`attempted`, `executed`, `unverified` and `confirmed` distinct. `confirmed_fingerprint()`
answers "has this exact action already been proven?" from that record, so a resumed run
does not repeat a completed non-idempotent action, and an action whose result is unknown
is investigated rather than blindly retried.

**Everything is addressed by `task_id`.** Loading a context requires the task; there is
no "load the current context" call that could resolve to another task's history. Every
write is filtered by task id, so two concurrent tasks cannot overwrite each other.

**Secrets never enter context.** Reference values pass the same screening memory uses, so
a page URL or a chat id can be recorded while a token, a seed phrase or a private key
cannot. This is enforced at the write, not by convention at the call site.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from backend.app.memory.manager import MemoryManager, SecretLeakError
from backend.app.models import (
    MemoryRecord,
    Task,
    TaskContext,
    TaskObservation,
    TaskRequirement,
)
from backend.app.models.context import (
    OBSERVATION_OUTCOMES,
    OUTCOME_ATTEMPTED,
    OUTCOME_BLOCKED,
    OUTCOME_CONFIRMED,
    OUTCOME_DUPLICATE_SUPPRESSED,
    OUTCOME_EXECUTED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_UNVERIFIED,
    REQUIREMENT_DISCOVERED,
    REQUIREMENT_SATISFIED,
    REQUIREMENT_UNVERIFIABLE,
    REQUIREMENT_WAIVED,
)
from backend.app.models.memory import SCOPE_CAMPAIGN, SCOPE_GLOBAL, SCOPE_TASK

logger = logging.getLogger(__name__)

# Reference-bearing keys that may be recorded in `resource_refs`. Anything outside this
# set is refused, so a session token cannot be parked in the context "for later".
ALLOWED_RESOURCE_REF_KEYS = frozenset(
    {
        "url",
        "chat_id",
        "chat_title",
        "bot_username",
        "channel",
        "session_key",
        "session_id",
        "profile",
        "wallet_address",
        "wallet_id",
        "network",
        "contract_address",
        "tx_hash",
        "campaign_id",
        "project_id",
        "discord_guild_id",
        "discord_channel_id",
        "discord_role_id",
        "x_handle",
        "screenshot_path",
        "evidence_uri",
        "conversation_id",
    }
)

# Keys whose values are external resource references rather than observations of state.
# Anything else in a tool result is a *result*, and is recorded as such.
_RESOURCE_KEYS = ALLOWED_RESOURCE_REF_KEYS

_WHITESPACE = re.compile(r"\s+")

# Screening bounds. Deep enough for the shape a bot reply or a page dump actually has
# (a list of messages, each with a nested button list), shallow enough that a hostile
# payload cannot make the walk unbounded.
MAX_SCREEN_DEPTH = 8
MAX_SCREEN_ITEMS = 200

# Marker written in place of a value that matched a secret pattern. The value is never
# stored, but the record still shows that something was there.
REDACTION_MARKER = "[REDACTED: matched a secret pattern]"
TRUNCATION_MARKER = "[TRUNCATED: nested too deep to store]"


def canonical_digest(payload: Any) -> str:
    """A stable digest of arbitrary JSON-ish arguments.

    Used so an action can be recognised again without storing its arguments verbatim:
    typed text and secrets may be in them, and the digest is what the repeat check
    compares.
    """
    try:
        blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def action_fingerprint(tool: Optional[str], parameters: Dict[str, Any]) -> str:
    """Identity of an action: the tool plus its arguments, independent of the step."""
    return canonical_digest({"tool": tool or "", "parameters": parameters or {}})


def normalize_requirement(text: str) -> str:
    """Collapse a requirement to a comparable key so the same one is not stored twice."""
    return _WHITESPACE.sub(" ", str(text or "").strip().lower())[:400]


# The concrete things a requirement names. These are what distinguish "join
# @examplechannel" from "join @otherchannel" - the words around them are identical, and
# matching on those words is what let the wrong action close the requirement.
#
# Deliberately specific. A pattern loose enough to match "channel" or "join" cannot tell
# two channels apart, which is the whole problem.
_IDENTIFIER_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"@[A-Za-z0-9_]{3,}"),                       # Telegram / Discord handles
    re.compile(r"\b0x[a-fA-F0-9]{8,}\b"),                   # addresses and transaction hashes
    re.compile(r"https?://[^\s,;)\]\"']+"),                 # URLs
    re.compile(r"\bt\.me/[A-Za-z0-9_/+-]+", re.IGNORECASE),  # Telegram invite links
    re.compile(r"\b[\w-]+\.[\w-]+\.[\w-]{2,}\b"),           # sub.domain.tld
)

# Quantities are handled separately from identities, because "hold 100 tokens" is
# satisfied by holding 150. An exact string match would never close it; matching on the
# unit alone would close it at 50. The number has to be compared, not string-matched.
_QUANTITY_RE = re.compile(
    r"\b(\d[\d,._]*)\s*"
    r"(usd|usdt|usdc|eth|btc|token|tokens|%|percent|days?|hours?|min|minutes|weeks?)\b",
    re.IGNORECASE,
)


def _as_decimal(value: Any) -> Decimal:
    """Coerce a number to `Decimal` without importing a float's binary noise.

    `Decimal(0.1)` is `0.1000000000000000055...`, which is then *greater* than the
    exactly-parsed `Decimal("0.1")` and would fail a boundary comparison that should
    pass. Going through `str()` first gives the number as it is written.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _extract_quantities(text: str) -> List[Tuple[Decimal, str]]:
    """Every "amount unit" in `text`, as a comparable (number, unit) pair.

    Separating the number from the unit is what lets a threshold be checked honestly:
    "hold at least 100 tokens" becomes `(Decimal("100"), "token")`, which a proof reading
    "150 token" satisfies and a proof reading "50 token" does not.

    The number is a `Decimal`, never a `float`. Crypto amounts are fractional and compared
    against thresholds; a binary float cannot represent `0.1` or `1.250` exactly, so a
    balance sitting exactly on a threshold could round to the wrong side of it. A
    `Decimal` keeps every digit as written (`0.500`, `0.0001`, `1000.500`).
    """
    found: List[Tuple[Decimal, str]] = []
    for raw_number, unit in _QUANTITY_RE.findall(str(text or "").lower()):
        # Read the number exactly as written. A previous version treated a trailing group
        # of three digits after a point as a thousands separator, which read `0.500
        # tokens` as 500 and `1.250 tokens` as 1250 - inflating a wallet's balance a
        # thousandfold and letting 0.5 tokens satisfy "hold 100 tokens".
        #
        # The heuristic is removed rather than refined. Crypto amounts are fractional by
        # nature and `0.500` is overwhelmingly a value, not a malformed thousands
        # separator; there is no rule that gets both readings right, so there is no rule.
        cleaned = raw_number.replace(",", "").replace("_", "")
        try:
            value = Decimal(cleaned)
        except InvalidOperation:
            # "1.2.3" and similar are not a number. Skip them rather than guess.
            continue
        if not value.is_finite():
            continue
        found.append((value, unit[:-1] if unit.endswith("s") else unit))
    return found


# A tool result reports a resolved target by FIELD NAME, and the value is often a bare
# name: telegram_join_chat returns `{"username": "examplechannel", "chat_id": 4242}`.
# A bare name matches none of the syntactic patterns above, so without this a genuinely
# successful join could never close the requirement that asked for it.
#
# Scoped to an explicit list of identifier-bearing field names rather than "any bare
# word", which would match every word in the payload and make the check meaningless.
_NAMED_TARGET_RE = re.compile(
    r'"(?:username|user_name|handle|bot_username|chat|chat_username|channel|'
    r'contract_address|token_address)"\s*:\s*"([^"]{2,120})"',
    re.IGNORECASE,
)


def _quantities_met(
    required: Sequence[Tuple[Any, str]], proved: Sequence[Tuple[Any, str]]
) -> bool:
    """Is every required amount met by something the proof actually shows?

    Same unit, and an amount at least as large. A requirement naming a unit the proof
    never mentions is not met, however large the proof's other numbers are.

    Amounts are compared as `Decimal`. A `float` on either side is converted through its
    printed form first (see `_as_decimal`), so `0.1` meets `0.1` exactly.
    """
    for amount, unit in required:
        needed = _as_decimal(amount)
        if not any(
            proved_unit == unit and _as_decimal(proved_amount) >= needed
            for proved_amount, proved_unit in proved
        ):
            return False
    return True


# ----------------------------------------------------------------------
# Which kind of evidence proves which kind of quantity
# ----------------------------------------------------------------------
# "hold 100 tokens" and "send 100 usdt" both contain "100 <unit>", but they are proved by
# different things. A holding is proved by what the wallet *has* - a balance reading. A
# transfer is proved by an amount that actually *settled*. Treating every number in the
# result as interchangeable proof let this close a holding requirement:
#
#     requirement   hold 100 tokens
#     observed      {"balance": "50 tokens"}                      <- what the wallet has
#     result        {"amount": "100 tokens", "status": "pending"} <- a transfer not yet done
#
# 100 appears in the proof, so "hold 100 tokens" closed on a wallet holding 50.
_HOLDING_REQUIREMENT_RE = re.compile(
    r"\b(?:hold|holds|holding|held|balance|own|owns|owning|possess|possesses)\b"
    r"|\bin\s+(?:your|the|a)\s+wallet\b",
    re.IGNORECASE,
)

# A key names a balance reading when one of its words is a balance word...
_BALANCE_KEY_RE = re.compile(r"(?:^|_)(?:balance|balances|holding|holdings|held)(?:_|$)")

# ...unless another word says the number is not a settled holding: a pending, expected or
# requested figure, or a threshold. `pending_balance` is a transaction in flight, not what
# the wallet holds.
_NOT_A_HOLDING_WORDS = frozenset(
    {
        "pending", "unconfirmed", "incoming", "outgoing", "expected", "projected",
        "requested", "estimated", "target", "required", "requirement", "minimum", "min",
        "max", "maximum", "threshold", "needed", "reserved",
    }
)

# A result that says it is still in flight, or that it did not happen, proves no amount.
_UNSETTLED_STATUSES = frozenset(
    {
        "pending", "processing", "submitted", "queued", "in_progress", "inprogress",
        "unconfirmed", "awaiting", "awaiting_confirmation", "waiting", "initiated",
        "broadcast", "failed", "error", "rejected", "reverted", "cancelled", "canceled",
        "declined", "expired", "dropped", "unknown",
    }
)
_STATUS_KEYS = ("status", "state", "tx_status", "transaction_status")


def _key_words(key: Any) -> List[str]:
    return [w for w in re.split(r"[^a-z0-9]+", str(key).lower()) if w]


def _is_balance_path(path: Tuple[str, ...]) -> bool:
    """Does this key path name a settled balance reading?"""
    words: List[str] = []
    for key in path:
        words.extend(_key_words(key))
    if any(word in _NOT_A_HOLDING_WORDS for word in words):
        return False
    return any(_BALANCE_KEY_RE.search("_".join(_key_words(key))) for key in path)


# Flags that say the same thing without a status word: `"success": false`, `"pending": true`.
_FALSE_MEANS_UNSETTLED = frozenset(
    {"success", "succeeded", "confirmed", "settled", "completed", "ok"}
)
_TRUE_MEANS_UNSETTLED = frozenset(
    {"pending", "failed", "rejected", "reverted", "unconfirmed"}
)


def _status_value_is_unsettled(value: Any) -> bool:
    if isinstance(value, str):
        return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_") in _UNSETTLED_STATUSES
    if isinstance(value, Mapping):
        # `{"status": {"state": "pending"}}`
        return any(_status_value_is_unsettled(value.get(key)) for key in _STATUS_KEYS)
    return False


def _node_is_unsettled(node: Mapping[str, Any]) -> bool:
    """Does this one object say that the thing it describes is not done?

    Reads only the object's own direct keys. Whether that verdict reaches a given number
    is decided by `_walk_text_leaves`, which applies it to the object's descendants and to
    nothing beside it.
    """
    for key in _STATUS_KEYS:
        if _status_value_is_unsettled(node.get(key)):
            return True
    for raw_key, value in node.items():
        name = "_".join(_key_words(raw_key))
        if value is False and name in _FALSE_MEANS_UNSETTLED:
            return True
        if value is True and name in _TRUE_MEANS_UNSETTLED:
            return True
        if name == "error" and isinstance(value, str) and value.strip():
            return True
    return False


def _walk_text_leaves(
    node: Any,
    path: Tuple[str, ...] = (),
    depth: int = 0,
    unsettled: bool = False,
) -> Iterator[Tuple[Tuple[str, ...], str, bool]]:
    """Every string value in a bounded walk: its key path, and whether it is unsettled.

    A value is unsettled when **any object enclosing it** reports a pending, failed or
    otherwise unfinished status. The verdict flows *down* the tree and never sideways:

        {"transaction": {"status": "pending", "amount": "100 tokens"},
         "balance": "150 tokens"}

    The amount sits inside the pending transaction, so it is unsettled. The balance is
    the transaction's sibling - the only object enclosing it is the root, which reports
    no status - so an independent wallet reading is not invalidated by a transfer
    that happens to be in flight beside it. `{"status": "failed", "balance": ...}` is the
    opposite case: the root itself failed, and everything under it goes with it.
    """
    if depth > MAX_SCREEN_DEPTH:
        return
    if isinstance(node, Mapping):
        unsettled = unsettled or _node_is_unsettled(node)
        for count, (key, value) in enumerate(node.items()):
            if count >= MAX_SCREEN_ITEMS:
                break
            yield from _walk_text_leaves(value, path + (str(key),), depth + 1, unsettled)
    elif isinstance(node, (list, tuple)):
        for count, item in enumerate(node):
            if count >= MAX_SCREEN_ITEMS:
                break
            yield from _walk_text_leaves(item, path, depth + 1, unsettled)
    elif isinstance(node, str):
        yield path, node, unsettled


@dataclass
class _QuantityEvidence:
    """The quantities one observation can actually prove, sorted by what they prove."""

    # unit -> every distinct *settled* balance reading seen for it, across all sources.
    balances: Dict[str, List[Decimal]] = field(default_factory=dict)
    # Quantities that are not balance readings and are not inside anything unsettled.
    settled: List[Tuple[Decimal, str]] = field(default_factory=list)


def _collect_quantity_evidence(*sources: Optional[Mapping[str, Any]]) -> _QuantityEvidence:
    """Read the observed state and the tool result into balance and non-balance evidence.

    The classification is by the *key* a number sits under, not by the text around it,
    because the text is what an attacker or a sloppy tool controls and the key is what
    the tool's own schema says the number is.

    **Anything unsettled proves nothing, balance included.** A reading inside an object
    that reports `pending`, `failed`, `rejected`, `reverted` (or `"success": false`) is
    dropped before it is classified, so it can neither satisfy a holding nor take part in
    a conflict. Which numbers an unsettled status covers is decided by the object
    structure (see `_walk_text_leaves`), so a pending transfer beside a wallet reading
    leaves that reading intact.
    """
    evidence = _QuantityEvidence()
    for source in sources:
        if not source:
            continue
        for path, text, unsettled in _walk_text_leaves(source):
            if unsettled:
                continue
            quantities = _extract_quantities(text)
            if not quantities:
                continue
            if _is_balance_path(path):
                for amount, unit in quantities:
                    evidence.balances.setdefault(unit, []).append(amount)
            else:
                evidence.settled.extend(quantities)
    return evidence


def _holding_met(
    required: Sequence[Tuple[Decimal, str]], evidence: _QuantityEvidence
) -> bool:
    """A holding is met only by an unambiguous balance reading that is large enough.

    No balance reading for the unit means nothing was observed, so it is not met. Two
    readings that disagree mean the evidence conflicts, so it is not met either: the
    requirement stays outstanding until a later observation settles which is true.
    """
    for amount, unit in required:
        readings = evidence.balances.get(unit)
        if not readings:
            return False
        if len(set(readings)) > 1:
            return False
        if readings[0] < amount:
            return False
    return True


def _extract_identifiers(text: str) -> List[str]:
    """Every concrete target named in `text`, normalised for comparison.

    Trailing punctuation is
    stripped and the result lowercased, so `(@examplechannel)` and `@examplechannel` are
    the same target while `@otherchannel` is not. Quantities are excluded here and
    compared numerically by `_quantities_met`.
    """
    found: List[str] = []
    lowered = str(text or "").lower()

    for pattern in _IDENTIFIER_PATTERNS:
        for match in pattern.findall(lowered):
            if isinstance(match, tuple):
                match = "".join(match)
            cleaned = match.strip().strip(".,;:!?)]}'\"").rstrip("/")
            # Drop a handle's leading `@`. A requirement says "@examplechannel" while a
            # Telegram tool reports `username: examplechannel`; without this a genuine,
            # successful join would never close its own requirement. The `@` is a sigil on
            # the name, not part of it.
            if cleaned.startswith("@"):
                cleaned = cleaned[1:]
            if cleaned:
                found.append(cleaned)

    for match in _NAMED_TARGET_RE.findall(lowered):
        cleaned = match.strip().strip(".,;:!?)]}'\"").rstrip("/")
        if cleaned.startswith("@"):
            cleaned = cleaned[1:]
        if cleaned and not cleaned.isdigit():
            found.append(cleaned)

    return found


def _screen_value(value: Any, *, limit: int, depth: int = 0) -> Any:
    """Recursively bound and secret-screen any value on its way into the context.

    Every string at every level is checked, not only the ones at the top. The earlier
    version screened `isinstance(value, str)` entries and JSON-encoded everything else,
    so a secret one level down - inside a `messages` list, or a nested `meta` dict, which
    is exactly the shape a bot reply or a page dump has - was written verbatim.

    Depth and breadth are bounded so a pathological payload cannot make this unbounded;
    anything past the limit is replaced by a truncation marker rather than dropped, so
    the record still says "there was more here".
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        try:
            MemoryManager.assert_no_secrets(value)
        except SecretLeakError:
            return REDACTION_MARKER
        return value[:limit]

    if depth >= MAX_SCREEN_DEPTH:
        return TRUNCATION_MARKER

    if isinstance(value, dict):
        screened: Dict[str, Any] = {}
        for key, item in list(value.items())[:MAX_SCREEN_ITEMS]:
            # Keys are screened at every level, not only at the root. Applying it only in
            # `_screened_dict` left `{"metadata": {"api_key: sk-live-...": ...}}` written
            # verbatim one level down, which is the shape most real payloads have.
            screened[_screen_mapping_key(key)] = _screen_value(
                item, limit=limit, depth=depth + 1
            )
        return screened

    if isinstance(value, (list, tuple, set)):
        items = list(value)[:MAX_SCREEN_ITEMS]
        return [_screen_value(item, limit=limit, depth=depth + 1) for item in items]

    # Anything else is rendered to text, and that text is screened like any other.
    rendered = str(value)
    try:
        MemoryManager.assert_no_secrets(rendered)
    except SecretLeakError:
        return REDACTION_MARKER
    return rendered[:limit]


def _screen_mapping_key(key: Any) -> str:
    """Bound and secret-screen a mapping key as well as its value.

    Keys were previously coerced straight to `str(...)` and truncated. That is harmless
    for ordinary field names, but a key is attacker-influenced text from an untrusted
    payload, and the secret patterns are not shy about labels: a key reading
    `api_key: sk-live-...` would have been persisted verbatim.
    """
    text = _screen_value(str(key), limit=80, depth=MAX_SCREEN_DEPTH + 1)
    return text if isinstance(text, str) else str(key)[:80]


def _clip(value: Any, limit: int) -> Any:
    """Bound a value before it is written, so a huge page dump cannot bloat a row."""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    try:
        return json.dumps(value, default=str)[:limit]
    except (TypeError, ValueError):
        return str(value)[:limit]


@dataclass
class ObservationRecord:
    """What one meaningful action did, as persisted."""

    id: Any
    fingerprint: str
    outcome: str
    tool: Optional[str]
    action: str
    evidence_kind: Optional[str] = None
    evidence_authoritative: bool = False
    observed_state: Dict[str, Any] = field(default_factory=dict)
    integration_ref: Optional[str] = None
    # Which loop iteration produced this record, and what the tool said if it failed.
    # Both are carried into the planner payload so a plan can be read against the run
    # that generated it rather than as a flat, undated list of claims.
    iteration: int = 0
    error: Optional[str] = None

    @property
    def proved_completion(self) -> bool:
        """True only when the outcome is an authoritative confirmation.

        `executed` and `unverified` deliberately do not qualify: an action that ran, or
        ran without matching evidence, is not a completed requirement.
        """
        return self.outcome == OUTCOME_CONFIRMED and self.evidence_authoritative


@dataclass
class ResolvedContext:
    """The three context levels, assembled but never merged into one blob.

    The levels stay separate on purpose. A caller can tell which fact came from this
    task, which from its campaign, and which from general knowledge, and can enforce the
    boundaries between them - most importantly that only `global` facts may be offered
    to a task that has no relationship to the others.
    """

    task_context: Optional[TaskContext] = None
    campaign_id: Optional[Any] = None
    campaign_facts: List[Dict[str, Any]] = field(default_factory=list)
    global_facts: List[Dict[str, Any]] = field(default_factory=list)
    observations: List[ObservationRecord] = field(default_factory=list)
    requirements: List[TaskRequirement] = field(default_factory=list)

    def outstanding_requirements(self) -> List[TaskRequirement]:
        return [r for r in self.requirements if r.status == REQUIREMENT_DISCOVERED]

    def digest(self, *, max_chars: Optional[int] = None) -> str:
        """A compact, source-labelled summary for a prompt (cut only if `max_chars` is given).

        Only what a decision actually needs: what is outstanding, what is proven, what
        the last observation found, and the relevant external references. Never the whole
        history, and never at the cost of the distinctions the levels exist to preserve.
        """
        parts: List[str] = []

        if self.task_context is not None:
            ctx = self.task_context
            if ctx.goal:
                parts.append(f"GOAL (this task): {ctx.goal}")
            if ctx.next_step_rationale:
                parts.append(f"WHY THE NEXT ACTION: {ctx.next_step_rationale}")

        hi = dict(self.task_context.human_intervention or {}) if self.task_context else {}
        if hi.get("pending"):
            parts.append(f"WAITING ON THE OPERATOR: {hi.get('reason') or 'an operator action'}")
        elif hi.get("summary"):
            parts.append(f"OPERATOR ALREADY RESPONDED: {hi['summary']}")
        if hi.get("standing_rules"):
            parts.append(
                "RULES THE OPERATOR GAVE FOR THIS TASK: " + "; ".join(hi["standing_rules"])
            )

        confirmed = [o for o in self.observations if o.proved_completion]
        if confirmed:
            parts.append(
                "ALREADY PROVEN (do not repeat): "
                + "; ".join(f"{o.action}" for o in confirmed[-8:])
            )
        uncertain = [
            o for o in self.observations if o.outcome in (OUTCOME_UNVERIFIED, OUTCOME_ATTEMPTED)
        ]
        if uncertain:
            parts.append(
                "ATTEMPTED BUT NOT PROVEN (investigate before retrying): "
                + "; ".join(f"{o.action}" for o in uncertain[-5:])
            )

        outstanding = self.outstanding_requirements()
        if outstanding:
            parts.append(
                "OUTSTANDING REQUIREMENTS (discovered while executing): "
                + "; ".join(r.text for r in outstanding[:10])
            )

        if self.observations:
            last = self.observations[-1]
            if last.observed_state:
                parts.append(
                    f"LAST OBSERVED EXTERNAL STATE: {_clip(last.observed_state, 800)}"
                )

        if self.campaign_facts:
            parts.append(
                "CAMPAIGN FACTS (shared by every task on this campaign; not "
                "account-specific): "
                + "; ".join(str(f.get("content", "")) for f in self.campaign_facts[:8])
            )
        if self.global_facts:
            parts.append(
                "GENERAL KNOWLEDGE (reusable, contains no task's private history): "
                + "; ".join(str(f.get("content", "")) for f in self.global_facts[:8])
            )

        text = "\n".join(parts)
        return text if max_chars is None else text[:max_chars]

    def to_prompt_dict(self) -> Dict[str, Any]:
        """The three context levels as structured, source-labelled planning input.

        Deliberately not a single fused blob. Every entry keeps the level it came from,
        how it was established, and where it was observed, because a planner that cannot
        tell a verified fact from an unverified observation plans against a fiction. The
        levels stay separated so a task with no campaign link cannot be handed campaign
        facts, and a campaign-scoped fact is never presented as account state.
        """
        ctx = self.task_context
        payload: Dict[str, Any] = {
            "level": "task",
            "isolated_to_task": True,
            "goal": ctx.goal if ctx else None,
            "normalized_objective": ctx.normalized_objective if ctx else None,
            "success_criteria": list(ctx.success_criteria or []) if ctx else [],
            "next_step_rationale": ctx.next_step_rationale if ctx else None,
            "current_step_index": ctx.current_step_index if ctx else None,
            "last_error": ctx.last_error if ctx else None,
            "campaign_id": str(self.campaign_id) if self.campaign_id is not None else None,
            # Timestamps where they exist. Context updates are stamped; individual
            # observations are not (the table keeps no created_at column), so they are
            # attributed by iteration instead of by an invented time.
            "context_updated_at": (
                ctx.updated_at.isoformat()
                if ctx is not None and getattr(ctx, "updated_at", None)
                else None
            ),
            "resumed_at": (
                ctx.resumed_at.isoformat()
                if ctx is not None and getattr(ctx, "resumed_at", None)
                else None
            ),
            "resource_refs": dict(ctx.resource_refs or {}) if ctx else {},
            "external_state": dict(ctx.external_state or {}) if ctx else {},
            "unresolved_questions": list(ctx.unresolved_questions or []) if ctx else [],
            "human_intervention": dict(ctx.human_intervention or {}) if ctx else {},
            "authorization_state": dict(ctx.authorization_state or {}) if ctx else {},
            "retry_summary": dict(ctx.retry_summary or {}) if ctx else {},
        }

        def _observation(record: ObservationRecord) -> Dict[str, Any]:
            return {
                "action": record.action,
                "tool": record.tool,
                "outcome": record.outcome,
                # Provenance: which integration this was observed through, and the
                # evidence class the verifier assigned it.
                "source": record.integration_ref,
                "evidence_kind": record.evidence_kind,
                "evidence_authoritative": bool(record.evidence_authoritative),
                "iteration": record.iteration,
                "error": record.error,
            }

        payload["verified_facts"] = [
            _observation(o) for o in self.observations if o.proved_completion
        ]
        payload["unverified_observations"] = [
            _observation(o)
            for o in self.observations
            if o.outcome in (OUTCOME_UNVERIFIED, OUTCOME_ATTEMPTED, OUTCOME_EXECUTED)
        ]
        payload["failed_attempts"] = [
            _observation(o)
            for o in self.observations
            if o.outcome in (OUTCOME_FAILED, OUTCOME_BLOCKED)
        ]

        def _requirement(req: Any) -> Dict[str, Any]:
            return {
                "text": req.text,
                "status": req.status,
                "origin": req.origin,
                "source_ref": req.source_ref,
                "account_specific": bool(req.is_account_specific),
                "campaign_scoped": bool(req.is_campaign_scoped),
            }

        payload["outstanding_requirements"] = [
            _requirement(r) for r in self.requirements if r.status == REQUIREMENT_DISCOVERED
        ]
        payload["satisfied_requirements"] = [
            _requirement(r) for r in self.requirements if r.status == REQUIREMENT_SATISFIED
        ]

        # Campaign facts are campaign-wide by construction (`_campaign_facts` reads only
        # campaign-scoped memory). They are labelled as such and never merged into the
        # task's own state, so no account- or wallet-specific claim can hide in them.
        payload["campaign_facts"] = [
            {
                "content": str(fact.get("content", "")),
                "category": fact.get("category"),
                "confidence": fact.get("confidence"),
                "scope": "campaign",
                "shared_with_other_tasks_on_this_campaign": True,
            }
            for fact in self.campaign_facts
        ]
        payload["global_facts"] = [
            {
                "content": str(fact.get("content", "")),
                "category": fact.get("category"),
                "confidence": fact.get("confidence"),
                "scope": "global",
            }
            for fact in self.global_facts
        ]
        return payload


class TaskContextService:
    """Reads and writes one task's execution context.

    Every method that touches persistence is scoped by `task_id`. There is deliberately
    no API here that reads "the" context without a task, because that is precisely the
    call that would let one task read another's history.
    """

    def __init__(self, db: AsyncSession, task: Optional[Task] = None) -> None:
        self.db = db
        self._task = task
        self._memory = MemoryManager(db)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def load_or_create(self, task: Task) -> TaskContext:
        """Return this task's context, creating it on first use.

        The lookup is by `task_id`, so an existing context belonging to another task can
        never be returned. A task created before this existed gets one here on its first
        run rather than being silently skipped.
        """
        existing = await self.db.scalar(
            select(TaskContext).where(TaskContext.task_id == task.id)
        )
        if existing is not None:
            return existing

        context = TaskContext(
            task_id=task.id,
            goal=(task.title or "")[:2000] or None,
            normalized_objective=(task.description or task.title or "")[:4000] or None,
            success_criteria=[],
            outstanding_requirements=[],
            unresolved_questions=[],
            resource_refs={},
            external_state={},
            human_intervention={},
            authorization_state={},
            retry_summary={},
            revision=1,
        )
        self.db.add(context)
        await self.db.flush()
        logger.info("Created execution context for task %s", task.task_id)
        return context

    async def get(self, task: Task) -> Optional[TaskContext]:
        return await self.db.scalar(select(TaskContext).where(TaskContext.task_id == task.id))

    # ------------------------------------------------------------------
    # Writing the context after a meaningful action
    # ------------------------------------------------------------------
    async def set_goal(
        self,
        task: Task,
        *,
        goal: Optional[str] = None,
        normalized_objective: Optional[str] = None,
        success_criteria: Optional[Sequence[str]] = None,
        campaign_id: Optional[Any] = None,
    ) -> TaskContext:
        context = await self.load_or_create(task)
        if goal:
            context.goal = goal[:2000]
        if normalized_objective:
            context.normalized_objective = normalized_objective[:4000]
        if success_criteria is not None:
            context.success_criteria = [str(c)[:300] for c in success_criteria][:20]
        if campaign_id is not None:
            context.campaign_id = campaign_id
        self._touch(context)
        await self.db.flush()
        return context

    async def set_next_step(
        self, task: Task, *, rationale: str, step_index: Optional[int] = None
    ) -> None:
        """Record *why* the next action is the next action.

        Without this the rationale exists only in the loop variable that produced it, so
        a restart or a second worker cannot tell a considered choice from an arbitrary one.
        """
        context = await self.load_or_create(task)
        context.next_step_rationale = rationale[:2000]
        if step_index is not None:
            context.current_step_index = int(step_index)
        self._touch(context)
        await self.db.flush()

    async def record_observation(
        self,
        task: Task,
        *,
        action: str,
        outcome: str,
        tool: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        step_index: Optional[int] = None,
        iteration: int = 0,
        evidence: Optional[Dict[str, Any]] = None,
        observed_state: Optional[Dict[str, Any]] = None,
        result_digest: Optional[Dict[str, Any]] = None,
        integration_ref: Optional[str] = None,
        is_idempotent: bool = True,
        error: Optional[str] = None,
    ) -> ObservationRecord:
        """Persist what one action did and what the external state then looked like.

        `observed_state` is deliberately separate from `result_digest`: the first is what
        the external system looks like (what the bot said, what the page shows), the second
        is what the tool returned. Recording only the latter is what makes an attempt look
        like a fact.

        `integration_ref` is screened too. Today it only ever carries a browser profile
        reference such as `browser:default`, which is not a credential - but it is free
        text taken from tool output, and whether a value is sensitive is a property of the
        tool that produced it today, not a guarantee about tomorrow.
        """
        if outcome not in OBSERVATION_OUTCOMES:
            # An unknown outcome must not silently become "fine". Unknown -> unverified.
            logger.warning(
                "Task %s: unknown observation outcome %r stored as unverified", task.task_id, outcome
            )
            outcome = OUTCOME_UNVERIFIED

        context = await self.load_or_create(task)
        evidence = evidence or {}
        fingerprint = action_fingerprint(tool, parameters or {})
        # Screen ONCE, then write the same screened copy everywhere. Assigning the raw
        # payload to `external_state` below would have left whatever the screening just
        # removed from the observation sitting in full on the context row, and that row
        # is what `GET /tasks/{id}/context` returns.
        safe_state = self._screened_dict(observed_state or {})
        safe_result = self._screened_dict(result_digest or {})
        safe_error = self._screened_text(error, limit=2000)

        observation = TaskObservation(
            task_id=task.id,
            context_id=context.id,
            iteration=int(iteration or 0),
            step_index=step_index,
            action=str(action)[:500],
            tool=(tool or None) and str(tool)[:128],
            arguments_digest=canonical_digest(parameters or {}),
            fingerprint=fingerprint,
            outcome=outcome,
            evidence_kind=(str(evidence.get("kind"))[:64] if evidence.get("kind") else None),
            evidence_authoritative=bool(evidence.get("authoritative")),
            observed_state=safe_state,
            result_digest=safe_result,
            integration_ref=(integration_ref or None) and self._screened_text(str(integration_ref), limit=255),
            is_idempotent=bool(is_idempotent),
            error=safe_error,
        )
        self.db.add(observation)
        await self.db.flush()

        # The newest observation is the current external state, and any references it
        # carries are the ones a later step in another integration will need.
        if safe_state:
            context.external_state = dict(safe_state)
            flag_modified(context, "external_state")
        if outcome in (OUTCOME_FAILED, OUTCOME_BLOCKED) and safe_error:
            context.last_error = safe_error
        await self.merge_resource_refs(task, self._refs_from(safe_state))

        self._touch(context)
        await self.db.flush()

        if outcome == OUTCOME_CONFIRMED and evidence.get("authoritative"):
            # Proof is what the external system REPORTED. The action description and the submitted
            # parameters are both excluded.
            #
            # The description is the planner's intent. The parameters are an attempt -
            # and an attempt is not an outcome. Including them let a requirement be
            # satisfied by the intent that failed:
            #
            #     requirement  hold 100 tokens
            #     submitted    {"amount": "100 tokens"}   <- what was tried
            #     observed     {"balance": "50 tokens"}    <- what is true
            #
            # The concatenation contained 100 tokens, so the requirement closed and the
            # agent reported a holding requirement met on a wallet holding 50. Only the
            # observed state and the returned result can prove anything happened.
            await self._satisfy_matching_requirements(
                task,
                observation,
                " ".join(
                    [
                        json.dumps(safe_state, default=str),
                        json.dumps(safe_result, default=str),
                    ]
                ),
                # Amounts are read from these, by key, not from the flat text above: a
                # pending transfer's amount must not be able to stand in for a balance.
                observed_state=safe_state,
                result=safe_result,
            )

        await self._notify_context_changed(task, action)

        return ObservationRecord(
            id=observation.id,
            fingerprint=fingerprint,
            outcome=outcome,
            tool=tool,
            action=str(action)[:500],
            evidence_kind=observation.evidence_kind,
            evidence_authoritative=observation.evidence_authoritative,
            observed_state=observed_state or {},
            integration_ref=integration_ref,
        )

    async def record_requirement(
        self,
        task: Task,
        text: str,
        *,
        origin: str = "tool_result",
        source_ref: Optional[str] = None,
        is_campaign_scoped: bool = False,
        is_account_specific: bool = False,
        account_ref: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Optional[TaskRequirement]:
        """Record a requirement discovered while executing.

        An account-specific requirement is one that belongs to *this* account or wallet.
        It is stored here and is never offered to another task, even on the same campaign:
        one wallet's eligibility is not evidence about another wallet.
        """
        cleaned = str(text or "").strip()
        if not cleaned:
            return None
        # Requirement text comes from a bot message or a page. It is untrusted input that
        # happens to be shaped as an instruction, so it is screened like any other
        # external string before it is stored or ever shown to an operator.
        cleaned = self._screened_text(cleaned, limit=1000) or ""
        if not cleaned or cleaned == REDACTION_MARKER:
            return None
        context = await self.load_or_create(task)
        key = normalize_requirement(cleaned)

        # Queried explicitly rather than through `context.requirements`: a lazy
        # relationship load raises MissingGreenlet in an async session, which is exactly
        # the failure that made this unreadable from the loop before.
        existing = next(
            (
                row
                for row in await self.requirements(task)
                if normalize_requirement(row.text) == key
            ),
            None,
        )
        if existing is not None:
            return existing  # already known; do not duplicate the outstanding list

        requirement = TaskRequirement(
            task_id=task.id,
            context_id=context.id,
            text=cleaned[:1000],
            origin=origin[:50],
            source_ref=(source_ref or None) and str(source_ref)[:512],
            status=REQUIREMENT_DISCOVERED,
            is_campaign_scoped=bool(is_campaign_scoped),
            is_account_specific=bool(is_account_specific),
            account_ref=(account_ref or None) and str(account_ref)[:255],
            evidence=self._screened_dict(evidence or {}),
        )
        self.db.add(requirement)
        await self.db.flush()
        await self._refresh_outstanding(task)
        await self._notify_context_changed(task, f"Requirement discovered: {cleaned[:150]}")
        return requirement

    async def satisfy_requirement(
        self, task: Task, requirement_id: Any, observation_id: Any, *, evidence: Optional[Dict[str, Any]] = None
    ) -> None:
        requirement = await self.db.get(TaskRequirement, requirement_id)
        if requirement is None or requirement.task_id != task.id:
            return
        requirement.status = REQUIREMENT_SATISFIED
        requirement.satisfied_by_observation_id = observation_id
        if evidence:
            requirement.evidence = self._screened_dict(evidence)
        await self.db.flush()
        await self._refresh_outstanding(task)

    async def mark_requirement(
        self, task: Task, requirement_id: Any, status: str, *, evidence: Optional[Dict[str, Any]] = None
    ) -> None:
        """Move a requirement to waived/unverifiable, recording why.

        Only reachable states are accepted; anything else is a programming error and is
        refused rather than written.
        """
        if status not in (REQUIREMENT_SATISFIED, REQUIREMENT_WAIVED, REQUIREMENT_UNVERIFIABLE):
            raise ValueError(f"Not a terminal requirement status: {status!r}")
        requirement = await self.db.get(TaskRequirement, requirement_id)
        if requirement is None or requirement.task_id != task.id:
            return
        requirement.status = status
        if evidence:
            requirement.evidence = self._screened_dict(evidence)
        await self.db.flush()
        await self._refresh_outstanding(task)

    async def add_question(self, task: Task, question: str) -> None:
        context = await self.load_or_create(task)
        questions = list(context.unresolved_questions or [])
        if question and question not in questions:
            questions.append(str(question)[:500])
        context.unresolved_questions = questions[:20]
        flag_modified(context, "unresolved_questions")
        self._touch(context)
        await self.db.flush()

    async def record_human_intervention(
        self,
        task: Task,
        *,
        required: bool,
        reason: str,
        step_index: Optional[int] = None,
        instruction: Optional[str] = None,
        resolved: bool = False,
        resolution: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record what the task is waiting on a person for.

        Cleared when the operator clears it, so "waiting for a human" is a fact about
        now, not a permanent label. But the STORY is kept: once resolved, the context still
        says why the task needed the operator, what the operator answered and how that was
        accepted, so every later LLM call (replans, the completion judgment) understands why
        the task stopped and that the manual step was done, instead of seeing only a bare
        "pending: false".
        """
        context = await self.load_or_create(task)
        state = dict(context.human_intervention or {})
        if resolved:
            now = datetime.now(timezone.utc).isoformat()
            res = {k: v for k, v in dict(resolution or {}).items() if v not in (None, "")}
            for key in ("operator_reply", "outcome", "interpretation"):
                if key in res:
                    res[key] = self._screened_text(str(res[key]), limit=600)
            info = [
                self._screened_text(str(i), limit=300)
                for i in (res.get("extracted_information") or [])
                if str(i).strip()
            ]
            if info:
                res["extracted_information"] = info
            else:
                res.pop("extracted_information", None)
            blocker = state.get("reason") or "an operator action"
            parts = [f"The task was parked because: {blocker}."]
            if res.get("operator_reply"):
                parts.append(f'The operator replied: "{res["operator_reply"]}".')
            if res.get("interpretation"):
                parts.append(f"Reading of the reply: {res['interpretation']}.")
            if res.get("extracted_information"):
                parts.append("Information the operator supplied: " + "; ".join(res["extracted_information"]) + ".")
            if res.get("outcome"):
                parts.append(f"Accepted as: {res['outcome']}")
            parts.append(
                "This blocker is RESOLVED. Treat the manual step as done or the information as "
                "supplied; do not ask the operator for it again unless a new problem appears."
            )
            summary = self._screened_text(" ".join(parts), limit=1500)
            entry = {
                "reason": state.get("reason"),
                "step_index": state.get("step_index"),
                "raised_at": state.get("raised_at"),
                "resolved_at": now,
                "resolution": res,
                "summary": summary,
            }
            history = (list(state.get("history") or []) + [entry])[-5:]
            state = {
                **state,
                "pending": False,
                "resolved_at": now,
                "resolution": res,
                "summary": summary,
                "history": history,
            }
        else:
            state = {
                **state,
                # A new blocker: the previous resolution is history, not the current state.
                "resolution": None,
                "summary": None,
                "pending": bool(required),
                # The reason quotes whatever the page or bot actually said, so it is
                # screened for the same reason requirement text is.
                "reason": self._screened_text(str(reason), limit=1000),
                "instruction": self._screened_text(str(instruction or reason), limit=1000),
                "step_index": step_index,
                "raised_at": datetime.now(timezone.utc).isoformat(),
            }
        context.human_intervention = state
        flag_modified(context, "human_intervention")
        self._touch(context)
        await self.db.flush()

    async def add_standing_rules(self, task: Task, rules: Sequence[str]) -> None:
        """Keep rules the operator gave for the rest of this task (e.g. "ordinary math
        questions from the bot may be answered by the agent"), so later LLM calls see them."""
        cleaned = [self._screened_text(str(r), limit=200) for r in rules if str(r).strip()]
        if not cleaned:
            return
        context = await self.load_or_create(task)
        state = dict(context.human_intervention or {})
        existing = list(state.get("standing_rules") or [])
        for rule in cleaned:
            if rule and rule not in existing:
                existing.append(rule)
        state["standing_rules"] = existing[-8:]
        context.human_intervention = state
        flag_modified(context, "human_intervention")
        self._touch(context)
        await self.db.flush()

    async def record_authorization(
        self, task: Task, *, permission: str, granted: bool, detail: str = ""
    ) -> None:
        """Record an authorization decision so the reason a call was refused survives."""
        context = await self.load_or_create(task)
        state = dict(context.authorization_state or {})
        state[str(permission)[:128]] = {
            "granted": bool(granted),
            # A refusal reason can quote the policy text or a remote response verbatim.
            "detail": self._screened_text(str(detail), limit=500),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        context.authorization_state = state
        flag_modified(context, "authorization_state")
        self._touch(context)
        await self.db.flush()

    async def record_failure(
        self, task: Task, *, error: str, category: str = "", recovery: str = ""
    ) -> None:
        """Record a failure and the recovery decision taken.

        An error string is whatever the failing call said, which for a remote service is
        whatever that service chose to echo back - including a submitted value. It is
        screened before storage for the same reason requirement text is.
        """
        context = await self.load_or_create(task)
        safe_error = self._screened_text(str(error), limit=2000)
        summary = dict(context.retry_summary or {})
        attempts = int(summary.get("attempts", 0)) + 1
        summary.update(
            {
                "attempts": attempts,
                "last_error": self._screened_text(str(error), limit=1000),
                "last_category": self._screened_text(str(category), limit=100),
                "last_recovery": self._screened_text(str(recovery), limit=500),
                "last_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        context.retry_summary = summary
        context.last_error = safe_error
        flag_modified(context, "retry_summary")
        self._touch(context)
        await self.db.flush()

    async def mark_resumed(self, task: Task) -> None:
        """Record that this task is being picked up again after an interruption."""
        context = await self.load_or_create(task)
        context.resumed_at = datetime.now(timezone.utc)
        self._touch(context)
        await self.db.flush()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    async def confirmed_fingerprint(
        self, task: Task, tool: Optional[str], parameters: Dict[str, Any]
    ) -> Optional[TaskObservation]:
        """The already-confirmed observation of this exact action, if there is one.

        This is the repeat guard. It only reports an *authoritative confirmation*, never
        an attempt or an unverified result, so an action whose outcome is genuinely
        unknown is investigated rather than skipped and never silently repeated.
        """
        fingerprint = action_fingerprint(tool, parameters or {})
        return await self.db.scalar(
            select(TaskObservation)
            .where(
                TaskObservation.task_id == task.id,
                TaskObservation.fingerprint == fingerprint,
                TaskObservation.outcome == OUTCOME_CONFIRMED,
                TaskObservation.evidence_authoritative.is_(True),
            )
            .order_by(TaskObservation.id.desc())
            .limit(1)
        )

    async def latest_attempt(
        self, task: Task, tool: Optional[str], parameters: Dict[str, Any]
    ) -> Optional[TaskObservation]:
        """The most recent outcome of this exact action, confirmed or not.

        Used to tell "never tried" from "tried, outcome unknown", which are different
        situations and deserve different handling.
        """
        fingerprint = action_fingerprint(tool, parameters or {})
        return await self.db.scalar(
            select(TaskObservation)
            .where(
                TaskObservation.task_id == task.id,
                TaskObservation.fingerprint == fingerprint,
            )
            .order_by(TaskObservation.id.desc())
            .limit(1)
        )

    async def observations(
        self, task: Task, *, limit: Optional[int] = 200
    ) -> List[ObservationRecord]:
        """Most recent observations, oldest first. `limit=None` returns the whole history."""
        stmt = (
            select(TaskObservation)
            .where(TaskObservation.task_id == task.id)
            .order_by(TaskObservation.id.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = list((await self.db.execute(stmt)).scalars().all())
        rows.reverse()
        return [
            ObservationRecord(
                id=r.id,
                fingerprint=r.fingerprint,
                outcome=r.outcome,
                tool=r.tool,
                action=r.action,
                evidence_kind=r.evidence_kind,
                evidence_authoritative=r.evidence_authoritative,
                observed_state=r.observed_state or {},
                integration_ref=r.integration_ref,
                iteration=int(r.iteration or 0),
                error=r.error,
            )
            for r in rows
        ]

    async def requirements(self, task: Task, *, only_outstanding: bool = False) -> List[TaskRequirement]:
        stmt = select(TaskRequirement).where(TaskRequirement.task_id == task.id)
        if only_outstanding:
            stmt = stmt.where(TaskRequirement.status == REQUIREMENT_DISCOVERED)
        return list(
            (await self.db.execute(stmt.order_by(TaskRequirement.id))).scalars().all()
        )

    async def unaddressed_requirements(self, task: Task, plan_descriptions: Sequence[str]) -> List[TaskRequirement]:
        """Outstanding requirements that no current plan step actually targets.

        This decides whether the plan needs revising, so it errs towards reporting a
        requirement as unaddressed: a needless replan costs one bounded LLM call, while
        a missed one leaves real work with no step aimed at it.

        A requirement that names a target - a handle, an address, a URL, a quantity - is
        only treated as addressed when that target appears in the plan. Sharing the word
        "channel" with a step aimed at a different channel does not count.
        """
        outstanding = await self.requirements(task, only_outstanding=True)
        if not outstanding:
            return []

        haystack = " ".join(str(d or "") for d in plan_descriptions).lower()
        plan_ids = set(_extract_identifiers(haystack))
        plan_words = set(re.findall(r"[a-z0-9]{4,}", haystack))

        unaddressed: List[TaskRequirement] = []
        for requirement in outstanding:
            text = requirement.text.lower()
            identifiers = set(_extract_identifiers(text))

            # A named target the plan never mentions means the plan is aimed elsewhere.
            if identifiers and not identifiers.issubset(plan_ids):
                unaddressed.append(requirement)
                continue

            terms = set(re.findall(r"[a-z0-9]{4,}", text))
            shared = terms & plan_words
            if not shared:
                unaddressed.append(requirement)
                continue
            if len(shared) * 2 < len(terms) and len(shared) < 3:
                unaddressed.append(requirement)

        return unaddressed

    async def merge_resource_refs(self, task: Task, refs: Dict[str, Any]) -> None:
        """Store external references so a later step in another integration can resume.

        Only an allowlisted set of reference keys is accepted, and every value is secret-
        screened. A session cookie or a bot token has no key here and is refused, so the
        context cannot become a place credentials accumulate.
        """
        accepted = {
            key: value
            for key, value in (refs or {}).items()
            if key in ALLOWED_RESOURCE_REF_KEYS and value not in (None, "")
        }
        if not accepted:
            return
        context = await self.load_or_create(task)
        merged = dict(context.resource_refs or {})
        for key, value in accepted.items():
            try:
                MemoryManager.assert_no_secrets(str(value))
            except SecretLeakError:
                logger.warning(
                    "Refusing to record %r as a task resource reference: it matches a secret pattern",
                    key,
                )
                continue
            merged[key] = _clip(value, 500)
        if merged != (context.resource_refs or {}):
            context.resource_refs = merged
            flag_modified(context, "resource_refs")
            self._touch(context)
            await self.db.flush()

    # ------------------------------------------------------------------
    # Resolution across the three levels
    # ------------------------------------------------------------------
    async def resolve(
        self, task: Task, *, global_limit: int = 8, observation_limit: int = 200
    ) -> ResolvedContext:
        """Assemble task, campaign and global context without merging their boundaries.

        Task context is always this task's. Campaign context is read only through an
        explicit campaign link. Global context is restricted to records written at global
        scope, so a task- or campaign-scoped memory can never surface as general knowledge.
        """
        context = await self.get(task)
        resolved = ResolvedContext(task_context=context)

        if context is not None:
            resolved.campaign_id = context.campaign_id
            # Explicit query, not `context.requirements`: lazy loading raises
            # MissingGreenlet in an async session.
            resolved.requirements = await self.requirements(task)

        resolved.observations = await self.observations(task, limit=observation_limit)

        if context is not None and context.campaign_id is not None:
            resolved.campaign_facts = await self._campaign_facts(context.campaign_id)

        resolved.global_facts = await self._global_facts(
            task, limit=global_limit
        )
        return resolved

    async def _campaign_facts(self, campaign_id: Any) -> List[Dict[str, Any]]:
        """Campaign-scoped memories: reusable facts, never account state.

        Only records explicitly written at campaign scope are returned. A fact that
        happened to be learned by one task lives in that task's context, not here.
        """
        rows = list(
            (
                await self.db.execute(
                    select(MemoryRecord)
                    .where(
                        MemoryRecord.scope == SCOPE_CAMPAIGN,
                        MemoryRecord.campaign_id == campaign_id,
                    )
                    .order_by(MemoryRecord.confidence.desc())
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        return [{"content": r.content, "category": r.category, "confidence": r.confidence} for r in rows]

    async def _global_facts(self, task: Task, *, limit: int) -> List[Dict[str, Any]]:
        """General knowledge only: rows written at global scope and related to nothing else."""
        rows = list(
            (
                await self.db.execute(
                    select(MemoryRecord)
                    .where(
                        MemoryRecord.scope == SCOPE_GLOBAL,
                        MemoryRecord.task_id.is_(None),
                        MemoryRecord.campaign_id.is_(None),
                    )
                    .order_by(MemoryRecord.confidence.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [{"content": r.content, "category": r.category, "confidence": r.confidence} for r in rows]

    @staticmethod
    async def _notify_context_changed(task: Task, summary: str) -> None:
        """Tell the dashboard this task's context moved on.

        A hint only: everything a client needs is already committed to the database
        before this fires, so a dropped notification costs a refresh and never state. It
        is wrapped because a websocket bus problem must never interrupt execution.
        """
        try:
            from backend.app.inbox import events

            await events.publish(
                events.EV_TASK_CONTEXT_UPDATED,
                task_id=task.task_id,
                summary=summary[:200],
            )
        except Exception as exc:  # pragma: no cover - notification is best-effort
            logger.debug("Context change notification failed for %s: %s", task.task_id, exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _touch(context: TaskContext) -> None:
        context.revision = int(context.revision or 0) + 1
        context.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _refs_from(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Pick the reference-bearing keys out of a flat observation payload."""
        return {
            key: payload[key]
            for key in _RESOURCE_KEYS
            if key in payload and payload[key] not in (None, "")
        }

    @staticmethod
    def _screened_dict(payload: Dict[str, Any], limit: int = 2000) -> Dict[str, Any]:
        """Bound and secret-screen a dict before it is written to the context.

        Delegates to the recursive walker, so a secret nested inside a list or a dict is
        redacted exactly as a top-level one is. A value matching a secret pattern becomes
        a redaction marker rather than being dropped, so the record still says "there was
        something here" without storing it.

        The root level is bounded by `MAX_SCREEN_ITEMS` too. It was not: only nested
        containers were capped, so a flat dict with ten thousand keys was screened - and
        stored - in full.
        """
        if not isinstance(payload, dict):
            return {"value": _screen_value(payload, limit=limit)}
        screened: Dict[str, Any] = {}
        for key, value in list(payload.items())[:MAX_SCREEN_ITEMS]:
            screened[_screen_mapping_key(key)] = _screen_value(value, limit=limit)
        if len(payload) > MAX_SCREEN_ITEMS:
            screened["__truncated__"] = (
                f"[TRUNCATED: {len(payload) - MAX_SCREEN_ITEMS} further key(s) not stored]"
            )
        return screened

    @staticmethod
    def _screened_text(value: Optional[str], limit: int = 2000) -> Optional[str]:
        """Screen a single string on its way into the context.

        Requirement text, error strings and human-intervention reasons are all
        single strings, and all of them are built from untrusted external content: a bot
        message, a page, or a remote service quoting back what was submitted to it.
        """
        if value is None:
            return None
        return _screen_value(str(value), limit=limit)

    async def _refresh_outstanding(self, task: Task) -> None:
        """Keep the denormalised outstanding list equal to the authoritative rows."""
        context = await self.get(task)
        if context is None:
            return
        outstanding = await self.requirements(task, only_outstanding=True)
        context.outstanding_requirements = [
            {
                "id": str(r.id),
                "text": r.text,
                "origin": r.origin,
                "account_specific": r.is_account_specific,
            }
            for r in outstanding[:50]
        ]
        flag_modified(context, "outstanding_requirements")
        self._touch(context)
        await self.db.flush()

    async def _satisfy_matching_requirements(
        self,
        task: Task,
        observation: TaskObservation,
        proof_text: str,
        *,
        observed_state: Optional[Mapping[str, Any]] = None,
        result: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Close only the requirements this confirmed observation actually satisfies.

        Two rules have applied here, and both were wrong.

        **Any shared 4-character word.** That closed *Join Telegram channel
        @examplechannel* when the agent joined *@otherchannel*, because `join`,
        `telegram` and `channel` all matched.

        **Identifiers present in a union of every text source.** The action description is
        what the planner *intended*; the tool parameters are what was actually
        *submitted*. Unioning them let a correctly-worded step satisfy a requirement by
        describing the right target while acting on the wrong one:

            requirement       Join Telegram channel @examplechannel
            description       Join Telegram channel @examplechannel   <- intent
            parameters        {"chat": "@otherchannel"}               <- what happened

        So `proof_text` carries **only** what can prove something happened: the submitted
        parameters and what the external system returned. The description is never part
        of it.

        Given that, a requirement closes when:

        * it names identifiers and **all** of them appear in the proof - the identifiers
          are what distinguish one channel from another, so they decide it; or
        * it names none, and enough of its ordinary words overlap to be the same thing.

        Anything else stays outstanding. Ambiguity resolves to "not proven".

        **Amounts are proved by the right kind of evidence.** `proof_text` is a flat
        string and cannot say whether "100 tokens" in it is a balance or a pending
        transfer, so quantities are not read from it. They come from the structured
        `observed_state` and `result`, classified by the key each number sits under:

        * a holding requirement ("hold 100 tokens") is met only by a *balance* reading,
          never by a transfer amount, and never when two balance readings disagree;
        * any other quantity requirement is met by a non-balance amount from a source
          that is not marked pending or failed.

        If neither structured source is supplied, no amount can be proved and a
        requirement that names one stays outstanding.
        """
        outstanding = await self.requirements(task, only_outstanding=True)
        if not outstanding:
            return

        haystack = proof_text.lower()
        haystack_ids = set(_extract_identifiers(haystack))
        proof_words = set(re.findall(r"[a-z0-9]{4,}", haystack))
        quantity_evidence = _collect_quantity_evidence(observed_state, result)

        for requirement in outstanding:
            text = requirement.text.lower()
            terms = set(re.findall(r"[a-z0-9]{4,}", text))
            identifiers = set(_extract_identifiers(text))
            quantities = _extract_quantities(text)

            if identifiers or quantities:
                # The target is the discriminator. Every named handle/address/URL must be
                # in the proof, and every named amount must actually be met by it. The
                # surrounding wording is decoration around a concrete requirement.
                if not identifiers.issubset(haystack_ids):
                    continue
                if quantities:
                    if _HOLDING_REQUIREMENT_RE.search(text):
                        if not _holding_met(quantities, quantity_evidence):
                            continue
                    elif not _quantities_met(quantities, quantity_evidence.settled):
                        continue
            else:
                # No target to match on, so fall back to wording - and demand real
                # overlap, because a single shared word proves nothing.
                if not terms:
                    continue
                shared = terms & proof_words
                if not shared or (len(shared) * 2 < len(terms) and len(shared) < 3):
                    continue

            requirement.status = REQUIREMENT_SATISFIED
            requirement.satisfied_by_observation_id = observation.id

        await self.db.flush()
        await self._refresh_outstanding(task)

    # ------------------------------------------------------------------
    async def count(self, task: Task) -> Tuple[int, int]:
        """(observations, requirements) for this task only. Used by the status report."""
        observations = await self.db.scalar(
            select(func.count(TaskObservation.id)).where(TaskObservation.task_id == task.id)
        )
        requirements = await self.db.scalar(
            select(func.count(TaskRequirement.id)).where(TaskRequirement.task_id == task.id)
        )
        return int(observations or 0), int(requirements or 0)


__all__ = [
    "ALLOWED_RESOURCE_REF_KEYS",
    "ObservationRecord",
    "ResolvedContext",
    "TaskContextService",
    "action_fingerprint",
    "canonical_digest",
    "normalize_requirement",
]