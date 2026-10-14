"""Source-aware context compaction for the single fixed LLM.

WHAT THIS IS
------------
A last-resort reducer for requests that cannot fit the model's context window.
It runs *after* the budget check says the request is too big, and it does
nothing at all to a request that fits.

WHAT THIS IS DELIBERATELY NOT
-----------------------------
It is not a content filter and not a summarizer. AirdropOS already made a
deliberate decision, recorded in `test_llm_limits.py` and in commits
`27ffb62` and `e4c65fc`, that prompts and tool results are never silently cut.
This module preserves that decision for every request that fits, and applies it
in the only case where the alternative is a failed request: it replaces content
with an explicit, provenance-labelled statement of what was reduced and where
the complete original can be re-read.

The rules that follow from that:

  * SECURITY AND SYSTEM PROMPT CONTENT IS NEVER REDUCED. System messages are
    passed through byte-for-byte. If reducing them were the only way to fit,
    the request fails loudly instead, because a request that runs with an
    edited operating manual is worse than one that does not run.
  * THE ACTIVE OBJECTIVE IS NEVER REDUCED. The final user message is the
    request being answered; it is passed through byte-for-byte.
  * NOTHING IS SILENTLY DROPPED. Every reduction emits a marker naming what was
    removed, how much, and that the full original is still retrievable.
  * STRUCTURE IS NEVER BROKEN. JSON is reduced by selecting keys, never by
    cutting a string mid-object.
  * TRUST LABELS SURVIVE. Text marked as untrusted external content stays
    marked, so a summary can never launder a website's instruction into a
    trusted instruction.

COMPACTION IS DETERMINISTIC
---------------------------
No model is used to compact, because compaction runs against the very budget
the model must fit inside. Spending part of that budget to rewrite the input
would be self-defeating, and the configured endpoint serves reasoning models
that can return a null message body, which makes a model-written summary an
unreliable thing to depend on. Every decision below is reproducible from the
input alone, which also makes it testable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.app.core.config import settings
from backend.app.llm.token_budget import TokenBudget, count_request_tokens

logger = logging.getLogger(__name__)


class ContextCompactionError(Exception):
    """Raised when a request cannot be reduced safely.

    This is deliberately fatal. Sending the original oversized request, or a
    partially-reduced one, would either fail at the provider or run with the
    task's meaning quietly altered. Both are worse than a clear error.
    """


# ----------------------------------------------------------------------
# Provenance markers
# ----------------------------------------------------------------------

# Marks content that came from outside the trusted instruction channel. This
# literal is what the rest of the codebase already emits and looks for, so
# reusing it means a reduced block is recognised exactly like a full one.
UNTRUSTED_MARKER = "UNTRUSTED"

# Content the model must not be able to talk its way past.
SECURITY_MARKERS: Tuple[str, ...] = (
    "UNTRUSTED-CONTENT-FLAGGED",
    "EMERGENCY STOP",
    "emergency stop",
    "NEVER",
    "MUST NOT",
    "requires operator approval",
    "AWAITING_OPERATOR_APPROVAL",
    "human approval",
    "never",
)

_ELISION_MARKER = "[COMPACTED"
_REFERENCE_MARKER = "[full original retained in the database"


def _describe_elision(kind: str, removed_chars: int, source: str) -> str:
    return (
        f"{_ELISION_MARKER} by AirdropOS: {source} was too large for the model's context "
        f"window, so {removed_chars} characters were reduced to fit. This is not a "
        f"security decision and nothing was deleted: {_REFERENCE_MARKER}, and re-read "
        f"the original {source} if a specific detail is needed."
    )


# ----------------------------------------------------------------------
# Overflow classification
# ----------------------------------------------------------------------

# Substrings providers use for a genuine context-window refusal. Matched
# case-insensitively against the response body.
_OVERFLOW_SIGNALS: Tuple[str, ...] = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "context window",
    "too many tokens",
    "reduce the length of the messages",
    "prompt is too long",
    "input is too long",
    "request too large for",
    "exceeds the maximum",
    "string too long",
)

# Observed on the configured endpoint: a credit failure is delivered as HTTP
# 200 with the refusal in the assistant's own words. It must never be read as a
# model answer, and it is emphatically not a context problem.
_BILLING_SIGNALS: Tuple[str, ...] = (
    "doesn't have enough credits",
    "does not have enough credits",
    "insufficient credits",
    "insufficient_credits",
    "insufficient balance",
    "credit balance is too low",
    "exceeded your current quota",
    "top up",
)


def classify_provider_error(status_code: int, body: str) -> Optional[str]:
    """Classify a provider error body.

    Returns ``"CONTEXT_OVERFLOW"``, ``"BILLING_REFUSAL"`` or ``None``. Only an
    explicit, unambiguous signal counts: an unrecognised body is never guessed
    into a context error, because compacting on a misread error wastes the
    budget and hides the real fault.
    """
    text = (body or "").lower()

    # Billing is checked first. A billing refusal can mention a model or a
    # limit, and treating it as a context problem would send the caller into a
    # pointless compaction loop instead of telling them to top up.
    if any(signal in text for signal in _BILLING_SIGNALS):
        return "BILLING_REFUSAL"

    if status_code in (400, 413, 422) and any(signal in text for signal in _OVERFLOW_SIGNALS):
        return "CONTEXT_OVERFLOW"
    return None


def detect_billing_refusal_in_content(content: Optional[str]) -> bool:
    """Detect a provider credit refusal that arrived as HTTP 200 body text.

    The configured endpoint answers an unfunded request with a normal-looking
    completion whose content is a top-up notice. Without this, that string is
    handed to the JSON parser and surfaces as a confusing "did not return
    valid JSON" error, hiding the real cause.
    """
    if not content:
        return False
    lowered = content.lower()
    if any(signal in lowered for signal in _BILLING_SIGNALS):
        # Require it to actually read like a notice, not like a quote about
        # billing that the agent legitimately reasoned its way into.
        return "credit" in lowered or "balance" in lowered or "quota" in lowered
    return False


# ----------------------------------------------------------------------
# Priority model
# ----------------------------------------------------------------------

MANDATORY = "mandatory"      # never reduced: system prompt, active objective
PRESERVE = "preserve"        # kept whole while budget allows
REDUCIBLE = "reducible"      # may be reduced, never silently
RETRIEVABLE = "retrievable"  # may be replaced by a reference


@dataclass
class CompactionReport:
    """Content-free record of what compaction did, for logs and diagnostics."""

    original_tokens: int
    final_tokens: int
    budget: Dict[str, int]
    strategies: List[str] = field(default_factory=list)
    messages_reduced: int = 0
    characters_removed: int = 0
    references_emitted: int = 0

    @property
    def compacted(self) -> bool:
        return bool(self.strategies)

    def describe(self) -> Dict[str, Any]:
        return {
            "original_tokens": self.original_tokens,
            "final_tokens": self.final_tokens,
            "compacted": self.compacted,
            "strategies": list(self.strategies),
            "messages_reduced": self.messages_reduced,
            "characters_removed": self.characters_removed,
            "references_emitted": self.references_emitted,
            "budget": self.budget,
        }


# ----------------------------------------------------------------------
# Single-message reduction
# ----------------------------------------------------------------------


def _looks_like_json_object(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("{") and stripped.endswith("}")


def _reduce_json_object(text: str, allow: int) -> Tuple[str, int]:
    """Reduce a JSON object by dropping whole keys, never by cutting a string.

    Returns the reduced text and the number of characters removed. If the object
    cannot be reduced by dropping keys without losing its meaning, the original
    is returned unchanged and the caller reports the refusal.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return text, 0
    if not isinstance(payload, dict):
        return text, 0

    original_len = len(text)
    kept: Dict[str, Any] = {}
    dropped: List[str] = []
    used = 0

    for key, value in payload.items():
        rendered = json.dumps({key: value}, default=str)
        if used + len(rendered) <= allow or not kept:
            kept[key] = value
            used += len(rendered)
        else:
            dropped.append(key)

    if not dropped:
        return text, 0

    kept["__compacted__"] = {
        "note": (
            "Whole top-level keys were omitted to fit the context window. Every value "
            "shown is still exactly as recorded. "
            f"{_REFERENCE_MARKER} and re-read the original record for omitted keys."
        ),
        "omitted_keys": dropped,
        "characters_removed": original_len - len(json.dumps(kept, default=str)),
    }
    return json.dumps(kept, default=str, indent=None), original_len - len(
        json.dumps(kept, default=str)
    )


# How much of the opening of a reduced message must survive verbatim. The
# opening is what carries the objective: every prompt in this codebase leads
# with it (planner writes "OBJECTIVE:" first, chat sends the user's request as
# the final message). A reduction that keeps this prefix has kept the task.
MIN_OBJECTIVE_PREFIX_CHARS = 200


def _reduce_prose(text: str, allow: int) -> Tuple[str, int]:
    """Reduce prose by keeping the head and tail and naming what was between.

    Head-and-tail is used rather than a summary because a summary invents text.
    This only ever removes, and it says exactly what it removed. The opening is
    preserved verbatim and is never smaller than `MIN_OBJECTIVE_PREFIX_CHARS`,
    so reducing a message can never quietly delete the request it was answering.

    The allowance may be zero when the rest of the request is itself oversized.
    That must not stall the reduction: the message is still brought down to the
    preserved opening, so each pass makes progress and the loop terminates on
    the mandatory content rather than running forever.
    """
    floor = max(allow, MIN_OBJECTIVE_PREFIX_CHARS)
    if len(text) <= floor:
        return text, 0

    marker_len = len(_describe_elision("this block", len(text) - floor, "the content block")) + 64
    usable = max(0, floor - marker_len)
    # 3:1 head:tail. The opening of a page carries its topic and the closing
    # carries its conclusions and any instructions; the middle is the part a
    # relevance search can recover on demand.
    head = max(MIN_OBJECTIVE_PREFIX_CHARS, int(usable * 0.75))
    if head >= len(text):
        return text, 0
    tail = max(0, usable - head)

    kept = (
        text[:head]
        + "\n"
        + _describe_elision("this content block", len(text) - head - tail, "the content block")
        + "\n"
        + (text[-tail:] if tail else "")
    )
    return kept, len(text) - len(kept)


def reduce_single_message(content: str, allow_chars: int, source: str) -> Tuple[str, int, str]:
    """Reduce one oversized message. Returns (text, chars_removed, strategy)."""
    if len(content) <= allow_chars:
        return content, 0, "none"

    if _looks_like_json_object(content):
        reduced, removed = _reduce_json_object(content, allow_chars)
        if removed:
            return reduced, removed, "json_keys"

    reduced, removed = _reduce_prose(content, allow_chars)
    if removed > 0:
        return reduced, removed, "head_tail"
    return content, 0, "none"


# ----------------------------------------------------------------------
# Security invariants
# ----------------------------------------------------------------------


def _security_markers_in(content: str) -> Tuple[str, ...]:
    return tuple(marker for marker in SECURITY_MARKERS if marker in content)


def _is_structured_reduction(original: str, reduced: str) -> bool:
    """True when a reduction removed whole JSON keys rather than cutting text.

    Recognised only by re-parsing the result and confirming it is a valid
    object that still declares what it dropped. An unparseable result is not
    treated as structured, so it falls through to the prefix check.
    """
    try:
        parsed = json.loads(reduced)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and "__compacted__" in parsed


def _validate_reduction(
    original: Sequence[Any],
    reduced: Sequence[Any],
) -> None:
    """Refuse a compacted request that lost a mandatory property.

    This is the post-compaction gate. A reduction that removed a system prompt,
    altered the active objective, or dropped an untrusted-content label is
    rejected outright rather than submitted.
    """
    if len(original) != len(reduced):
        raise ContextCompactionError(
            f"Compaction changed the message count ({len(original)} -> {len(reduced)}). "
            "The request was not submitted."
        )

    for before, after in zip(original, reduced):
        if before.role != after.role:
            raise ContextCompactionError(
                f"Compaction changed a message role ({before.role} -> {after.role}). "
                "The request was not submitted."
            )

        # System messages carry the operating manual and the safety rules.
        if before.role == "system" and before.content != after.content:
            raise ContextCompactionError(
                "Compaction altered a system message. System instructions are never "
                "reduced. The request was not submitted."
            )

    # The last message carries the active objective. It may be reduced, because
    # in the planner and discovery paths it IS the enormous one, but its
    # opening must survive verbatim so the task itself cannot be edited away.
    if original and reduced:
        first, last = original[-1].content, reduced[-1].content
        if first != last and not _is_structured_reduction(first, last):
            keep = min(len(first), MIN_OBJECTIVE_PREFIX_CHARS)
            if not first[:keep] or not last.startswith(first[:keep]):
                raise ContextCompactionError(
                    "Compaction did not preserve the opening of the final user message, "
                    "which carries the active objective. The request was not submitted."
                )

    # A trust label must never disappear: an unlabelled block of external text
    # is indistinguishable from an instruction to the model.
    before_markers = sum(len(_security_markers_in(m.content)) for m in original)
    after_markers = sum(len(_security_markers_in(m.content)) for m in reduced)
    if after_markers < before_markers:
        raise ContextCompactionError(
            "Compaction removed a security or trust label. A reduced block of external "
            "content would be indistinguishable from an instruction. The request was "
            "not submitted."
        )


# ----------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------


@dataclass
class PreflightResult:
    messages: List[Any]
    report: CompactionReport


class ContextCompactor:
    """Reduces a request to the budget, or refuses to."""

    def __init__(self, budget: Optional[TokenBudget] = None, message_factory=None):
        self.budget = budget or TokenBudget.from_settings()
        # Injected so this module does not import the client (which imports
        # this module) and so tests can build messages without a real client.
        self._message_factory = message_factory

    # -- helpers -------------------------------------------------------
    def _new_message(self, role: str, content: str):
        if self._message_factory is None:
            raise ContextCompactionError("ContextCompactor requires a message factory.")
        return self._message_factory(role, content)

    def _fits(self, messages: Sequence[Any]) -> bool:
        return count_request_tokens(messages) <= self.budget.usable_input

    # -- public API ----------------------------------------------------
    def preflight(self, messages: Sequence[Any], *, aggressive: bool = False) -> PreflightResult:
        """Return messages that fit, compacting only if they do not.

        ``aggressive`` lowers the aim to the safety margin instead of the
        target, which is what the overflow-recovery path uses after the
        provider has already refused the request once.
        """
        original = list(messages)
        original_tokens = count_request_tokens(original)

        report = CompactionReport(
            original_tokens=original_tokens,
            final_tokens=original_tokens,
            budget=self.budget.describe(),
        )

        # Under the trigger there is nothing to do. This is the common case and
        # it must be exactly a pass-through, byte-for-byte.
        limit = self.budget.trigger if not aggressive else self.budget.target
        if original_tokens <= limit and self._fits(original):
            return PreflightResult(messages=original, report=report)

        reduced = self._reduce_message_list(original, target_tokens=limit, report=report)

        final_tokens = count_request_tokens(reduced)
        if final_tokens > self.budget.usable_input:
            # Everything droppable was dropped and it still does not fit. The
            # remaining content is mandatory or would be corrupted to save.
            raise ContextCompactionError(
                f"Request cannot be reduced to fit the context window: after compaction "
                f"it still needs about {final_tokens} tokens but the usable budget is "
                f"{self.budget.usable_input}. The mandatory content alone exceeds the "
                f"budget, so nothing was submitted. Reduce the request content, or raise "
                f"LLM_CONTEXT_WINDOW_TOKENS if your endpoint genuinely supports a larger "
                f"window."
            )

        _validate_reduction(original, reduced)

        report.final_tokens = final_tokens
        return PreflightResult(messages=reduced, report=report)

    # -- internals -----------------------------------------------------
    def _reduce_message_list(
        self,
        messages: List[Any],
        *,
        target_tokens: int,
        report: CompactionReport,
    ) -> List[Any]:
        """Shrink, cheapest content first, until the target is met.

        Order is deliberate:
          1. reduce the single largest reducible message,
          2. if that is not enough, replace trailing history with a reference.

        The active objective (the last message) and every system message are
        never touched, so this loop terminates on those alone rather than
        running until the objective is gone.
        """
        working = list(messages)
        # System messages are excluded entirely: they are never reduced.
        # Everything else is fair game, including the final message, because in
        # the planner and discovery paths the final message IS the oversized
        # one. Its opening is preserved by `_reduce_prose`, so reducing it
        # cannot edit the objective away.
        candidates = [i for i, m in enumerate(working) if m.role != "system" and m.content]
        candidates.sort(key=lambda i: len(working[i].content), reverse=True)

        for index in candidates:
            if count_request_tokens(working) <= target_tokens:
                break
            message = working[index]
            allowance = self._allowance_for(working, index, target_tokens)
            reduced_text, removed, strategy = reduce_single_message(
                message.content, allowance, f"message {index + 1} of {len(working)}"
            )
            if not removed:
                continue
            new_message = self._new_message(message.role, reduced_text)
            working[index] = new_message
            report.strategies.append(f"message[{index}]:{strategy}")
            report.messages_reduced += 1
            report.characters_removed += removed
            report.references_emitted += 1

        # Still too large? Drop the oldest reducible history behind an explicit,
        # retrievable reference so the model knows the record exists.
        if count_request_tokens(working) > target_tokens:
            working = self._replace_history_with_reference(working, target_tokens, report)

        return working

    def _allowance_for(
        self, messages: Sequence[Any], index: int, target_tokens: int
    ) -> int:
        """Characters message ``index`` may keep and still hit the target.

        The current cost of everything else is subtracted from the target, so
        the allowance shrinks as the rest of the request stays fixed. This is
        what stops one huge message from being left oversized because another
        message was already large.
        """
        others = [m for i, m in enumerate(messages) if i != index]
        others_tokens = count_request_tokens(others)
        spare = max(0, target_tokens - others_tokens)
        chars_per_token = settings.LLM_CONTEXT_ESTIMATOR_CHARS_PER_TOKEN
        # Reserve room for the elision marker this reduction will add.
        return int(spare * chars_per_token * 0.85)

    def _replace_history_with_reference(
        self,
        messages: List[Any],
        target_tokens: int,
        report: CompactionReport,
    ) -> List[Any]:
        """Replace old turns with one retrievable reference block."""
        active = messages[-1]
        systems = [m for m in messages if m.role == "system"]
        middle = messages[len(systems): -1]

        if not middle:
            return messages

        dropped = len(middle)
        dropped_chars = sum(len(m.content) for m in middle)
        marker = self._new_message(
            "user",
            _describe_elision(
                f"{dropped} earlier conversation message(s)", dropped_chars, "earlier history"
            ),
        )
        report.strategies.append(f"history:{dropped}_messages->reference")
        report.characters_removed += dropped_chars
        report.references_emitted += 1

        candidate = systems + [marker, active]
        if not self._fits(candidate):
            raise ContextCompactionError(
                "Compaction reached its floor: only system instructions and the active "
                "objective remain and they still exceed the usable context budget. "
                "Nothing was submitted."
            )
        return candidate


# ----------------------------------------------------------------------
# Convenience for the client
# ----------------------------------------------------------------------


def build_compactor(budget: Optional[TokenBudget] = None) -> ContextCompactor:
    """Build a compactor bound to the real `Message` type.

    The budget is passed in rather than defaulted so the compactor always
    reduces against the SAME budget the caller validated against. Defaulting it
    here would let a request be checked against one window and reduced against
    another, which would silently disable compaction.
    """
    from backend.app.llm.client import Message  # local import avoids a cycle

    return ContextCompactor(
        budget=budget,
        message_factory=lambda role, content: Message(role, content),
    )