"""Server-side human approval for on-chain broadcasts.

`TransactionEngine.submit_raw` will not broadcast on the strength of a caller-supplied
flag: it asks an independent `ApprovalVerifier` whether a named human approved *this
exact transaction*, and that verifier must consume the approval atomically so one
approval authorises exactly one broadcast.

This module is that verifier, plus the service the API and the agent use to request
an approval in the first place. Two rules shape it:

* An approval is bound to one transaction (network, chain id, wallet, destination,
  value, calldata, gas limit). Approving "send some ETH" is not a thing; the operator
  approves a specific, quoted transfer. The engine decodes the signed bytes and states
  those fields from what it actually decoded, never from this dictionary, so the
  comparison below is between what was authorized and what is about to be sent.
* Approving one transfer never authorises another, and an approval can never be
  replayed. `verify_and_consume` is a single conditional UPDATE, so two concurrent
  broadcasts of the same approval cannot both win.

Nothing here signs. Signing happens in `wallets/vault.KeyVault`; broadcasting happens
in `transactions/engine.py`. Both keep their own guards.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import TransactionApproval
from backend.app.security.autonomous_policy import (
    VALUE_CATEGORIES,
    is_policy_approver,
    rule_id_from_approver,
)
from backend.app.transactions.engine import NON_HUMAN_APPROVERS

logger = logging.getLogger(__name__)

STATUS_PENDING = "PENDING"
STATUS_CONSUMED = "CONSUMED"
STATUS_REJECTED = "REJECTED"
STATUS_EXPIRED = "EXPIRED"

# How long a quoted transfer stays approvable. Short on purpose: the quote contains a
# gas estimate and a balance check that both go stale.
DEFAULT_APPROVAL_TTL_MINUTES = 15


class ApprovalError(Exception):
    """An approval could not be created or consumed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def canonical_binding(binding: Mapping[str, Any]) -> Dict[str, str]:
    """Normalise a binding for exact comparison.

    Addresses are case-folded because `to` may arrive checksummed from the decoder and
    lowercase from configuration; calldata is case-folded for the same reason - hex digit
    case is not semantic, and `0xA9059CBB` and `0xa9059cbb` are the same bytes, so
    comparing them verbatim would refuse a correct transfer whenever an encoder and the
    decoder disagreed on case. Everything else is stringified verbatim so `1` and `"1"`
    cannot compare differently.
    """
    from backend.app.transactions.engine import canonical_calldata

    out: Dict[str, str] = {}
    for key, value in binding.items():
        name = str(key)
        text = "" if value is None else str(value)
        if name in ("wallet", "to"):
            out[name] = text.strip().lower()
        elif name == "data":
            out[name] = canonical_calldata(text)
        else:
            out[name] = text
    return out


def intent_fingerprint(binding: Mapping[str, Any]) -> str:
    """A stable digest of exactly what a transfer does.

    Used as the idempotency key so the same instruction asked twice maps to one
    approval and therefore at most one broadcast. The network and chain id are
    included deliberately: the same address and amount on a different chain is a
    different transfer, not a repeat.
    """
    import hashlib

    normalized = canonical_binding(binding)
    material = "|".join(
        f"{key}={normalized.get(key, '')}"
        for key in sorted(("network", "chain_id", "wallet", "to", "value_wei", "data"))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:64]


class ApprovalService:
    """Create, inspect, reject and consume transaction approvals."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    async def request(
        self,
        *,
        approved_by: str,
        binding: Mapping[str, Any],
        summary: str,
        proposal_id: Optional[str] = None,
        task_id: Optional[Any] = None,
        ttl_minutes: int = DEFAULT_APPROVAL_TTL_MINUTES,
        rule_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> TransactionApproval:
        """Record approval of one specific transaction.

        See `find_or_create` for the variant that also reports whether the row was
        newly created.
        """
        row, _created = await self.find_or_create(
            approved_by=approved_by,
            binding=binding,
            summary=summary,
            proposal_id=proposal_id,
            task_id=task_id,
            ttl_minutes=ttl_minutes,
            rule_id=rule_id,
            idempotency_key=idempotency_key,
        )
        return row

    async def find_or_create(
        self,
        *,
        approved_by: str,
        binding: Mapping[str, Any],
        summary: str,
        proposal_id: Optional[str] = None,
        task_id: Optional[Any] = None,
        ttl_minutes: int = DEFAULT_APPROVAL_TTL_MINUTES,
        rule_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> tuple[TransactionApproval, bool]:
        """Record an approval, or return the existing one for this exact intent.

        Returns `(row, created)`. `created` is False when an approval already existed
        for this idempotency key, which is how a repeated request is told apart from a
        new one without either being able to queue a second broadcast.

        `approved_by` is either a named human or the autonomous-policy identity. The
        policy identity is NOT a claim the caller can simply make: `verify_and_consume`
        re-evaluates the live policy against the actual transaction before spending it,
        so writing that string into a tool call authorises nothing on its own.
        """
        who = str(approved_by or "").strip()
        if not who:
            raise ApprovalError("An approval must name the human who gave it.")
        if who.lower() in NON_HUMAN_APPROVERS:
            # `auto` is in this set. A policy identity must use the explicit
            # `policy:autonomous:<rule>` form, which is checked below, so it can never
            # collide with a bare "the system did it".
            raise ApprovalError(
                f"'{who}' is not a person. The agent cannot approve its own transaction."
            )
        if is_policy_approver(who) and not rule_id_from_approver(who):
            raise ApprovalError(
                "An autonomous approval must name the rule that authorized it."
            )

        normalized = canonical_binding(binding)
        for required in ("network", "wallet", "to"):
            if not normalized.get(required):
                raise ApprovalError(f"Approval binding is missing '{required}'.")

        if idempotency_key:
            existing = await self.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                # One intent, one approval. A repeated request must not queue a second
                # broadcast of a transfer that is already authorized or already sent.
                return existing, False

        row = TransactionApproval(
            approval_id=secrets.token_urlsafe(18)[:32],
            approved_by=who,
            network=normalized["network"],
            chain_id=_as_int(normalized.get("chain_id")),
            wallet_address=normalized["wallet"],
            to_address=normalized.get("to") or None,
            value_wei=normalized.get("value_wei") or None,
            tx_hash=normalized.get("tx_hash") or None,
            estimated_cost_usd=_as_float(normalized.get("estimated_cost_usd")),
            binding=normalized,
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            policy_rule_id=rule_id_from_approver(who),
            task_id=task_id,
            summary=(summary or "")[:2000],
            status=STATUS_PENDING,
            expires_at=_now() + timedelta(minutes=max(1, min(ttl_minutes, 120))),
        )
        self.db.add(row)
        await self.db.flush()
        logger.info(
            "Approval %s recorded for %s -> %s on %s by %s (rule=%s)",
            row.approval_id, row.wallet_address, row.to_address, row.network, who,
            rule_id_from_approver(who),
        )
        return row, True

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get(self, approval_id: str) -> Optional[TransactionApproval]:
        return await self.db.scalar(
            select(TransactionApproval).where(TransactionApproval.approval_id == approval_id)
        )

    async def find_by_idempotency_key(self, key: str) -> Optional[TransactionApproval]:
        """The approval already issued for this exact intent, if any.

        This is what makes a repeated request safe. "Transfer 0.01 ETH to X" asked
        twice must not produce two approvals and therefore two broadcasts.
        """
        if not key:
            return None
        return await self.db.scalar(
            select(TransactionApproval)
            .where(TransactionApproval.idempotency_key == key)
            .order_by(TransactionApproval.created_at.desc())
            .limit(1)
        )

    async def list_recent(self, limit: int = 25) -> List[TransactionApproval]:
        rows = (
            await self.db.execute(
                select(TransactionApproval)
                .order_by(TransactionApproval.created_at.desc())
                .limit(max(1, min(limit, 200)))
            )
        ).scalars().all()
        return list(rows)

    async def is_usable(self, approval_id: str, binding: Optional[Mapping[str, Any]] = None) -> tuple[bool, str]:
        """Whether an approval may still be used, and why not when it may not."""
        row = await self.get(approval_id)
        if row is None:
            return False, "No such approval."
        return self.evaluate(row, binding)

    @staticmethod
    def evaluate(
        row: TransactionApproval, binding: Optional[Mapping[str, Any]] = None
    ) -> tuple[bool, str]:
        """Pure, synchronous usability check. Exposed for tests and the API."""
        if row.status == STATUS_CONSUMED:
            return False, "This approval has already been used for a broadcast."
        if row.status == STATUS_REJECTED:
            return False, f"This approval was rejected: {row.rejection_reason or 'no reason given'}."
        # A policy approver is neither a person nor a spoofable name: it is only
        # honoured if `verify_and_consume` can still prove, from the live policy and
        # the actual transaction, that the named rule authorizes it. `evaluate` alone
        # never grants it.
        if str(row.approved_by).strip().lower() in NON_HUMAN_APPROVERS:
            return False, "The approver is not a person."
        if is_policy_approver(row.approved_by) and not row.policy_rule_id:
            return False, "This autonomous approval does not name the rule that authorized it."

        expires = _as_utc(row.expires_at)
        if expires is None:
            return False, "This approval has no expiry and cannot be used."
        if expires <= _now():
            return False, f"This approval expired at {expires.isoformat()}."

        if binding is not None:
            approved = canonical_binding(row.binding or {})
            offered = canonical_binding(binding)
            # Compare every field the approval actually covers. Comparing the union
            # would demand that the caller assert fields the operator never saw (the
            # engine's computed tx hash, for instance, which cannot exist before the
            # transaction is signed). The approval is a statement about what the
            # operator was shown: network, chain, wallet, destination, amount and
            # calldata. Anything the caller adds is extra proof, not a mismatch.
            if not approved:
                return False, "This approval covers no fields and cannot authorise anything."
            missing = [key for key in approved if key not in offered]
            if missing:
                return False, (
                    "The transaction presented for broadcast does not state the approved "
                    f"field(s): {', '.join(sorted(missing))}."
                )
            mismatched = [key for key in approved if approved[key] != offered.get(key, "")]
            if mismatched:
                return False, (
                    "This approval was issued for a different transaction; it does not "
                    f"authorise this one (differs on: {', '.join(sorted(mismatched))})."
                )
        return True, ""

    # ------------------------------------------------------------------
    # Rejection
    # ------------------------------------------------------------------
    async def reject(self, approval_id: str, reason: str) -> Optional[TransactionApproval]:
        """Operator says no. A rejected approval can never be resurrected."""
        row = await self.get(approval_id)
        if row is None:
            return None
        if row.status == STATUS_CONSUMED:
            raise ApprovalError(
                "This approval was already used for a broadcast and cannot be rejected."
            )
        row.status = STATUS_REJECTED
        row.rejection_reason = (reason or "rejected by operator")[:1000]
        await self.db.flush()
        return row

    # ------------------------------------------------------------------
    # Consumption
    # ------------------------------------------------------------------
    async def verify_and_consume(
        self,
        *,
        approval_id: str,
        approved_by: str,
        binding: Mapping[str, str],
    ) -> bool:
        """Atomically consume an approval, but only for exactly this transaction.

        The check and the consume are one conditional UPDATE. A plain SELECT followed
        by a write would let two concurrent broadcasts of the same approval both read
        it as PENDING and both succeed, which is exactly the double-spend this whole
        mechanism exists to prevent.
        """
        row = await self.get(approval_id)
        if row is None:
            logger.warning("Broadcast refused: approval %s does not exist", approval_id)
            return False

        ok, reason = self.evaluate(row, binding)
        if not ok:
            logger.warning("Broadcast refused: %s", reason)
            return False

        if str(row.approved_by).strip().lower() != str(approved_by).strip().lower():
            logger.warning(
                "Broadcast refused: approval %s was issued to %s, not %s",
                approval_id, row.approved_by, approved_by,
            )
            return False

        # An autonomous approval is only as good as the policy behind it, evaluated
        # NOW against the transaction actually presented. An operator who narrows or
        # disables a rule between approval and broadcast stops the broadcast, and an
        # approval naming a rule that no longer exists is dead.
        if is_policy_approver(row.approved_by) and not _policy_still_authorizes(row, binding):
            return False

        result = await self.db.execute(
            update(TransactionApproval)
            .where(
                TransactionApproval.id == row.id,
                TransactionApproval.status == STATUS_PENDING,
                TransactionApproval.consumed_at.is_(None),
            )
            .values(status=STATUS_CONSUMED, consumed_at=_now())
        )
        if result.rowcount != 1:
            # Someone else consumed it between the read and the write.
            logger.warning(
                "Broadcast refused: approval %s was consumed concurrently", approval_id
            )
            await self.db.rollback()
            return False

        await self.db.commit()
        logger.info("Approval %s consumed for a real broadcast", approval_id)
        return True

    async def record_broadcast(
        self, approval_id: str, tx_hash: str, status: str = "SUBMITTED"
    ) -> None:
        """Attach the real broadcast hash to the approval that authorized it.

        Recorded immediately after the node accepts the transaction, before any receipt
        wait. That ordering matters: if the process dies between the two, the hash is
        already on disk and a retry can report what happened instead of sending a
        second time.
        """
        row = await self.get(approval_id)
        if row is None:
            return
        row.tx_hash = tx_hash
        row.broadcast_status = status
        await self.db.flush()

    async def already_broadcast(self, binding: Mapping[str, Any]) -> Optional[str]:
        """The hash of an earlier broadcast of this exact intent, if any."""
        row = await self.find_by_idempotency_key(intent_fingerprint(binding))
        if row is None:
            return None
        return row.tx_hash

    async def expire_stale(self) -> int:
        """Mark timed-out approvals EXPIRED so the UI stops offering them."""
        result = await self.db.execute(
            update(TransactionApproval)
            .where(
                TransactionApproval.status == STATUS_PENDING,
                TransactionApproval.expires_at <= _now(),
            )
            .values(status=STATUS_EXPIRED)
        )
        await self.db.commit()
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------
    @staticmethod
    def to_dict(row: TransactionApproval) -> Dict[str, Any]:
        expires = _as_utc(row.expires_at)
        usable, reason = ApprovalService.evaluate(row)
        return {
            "approval_id": row.approval_id,
            "approved_by": row.approved_by,
            "network": row.network,
            "chain_id": row.chain_id,
            "wallet": row.wallet_address,
            "to": row.to_address,
            "value_wei": row.value_wei,
            "tx_hash": row.tx_hash,
            "estimated_cost_usd": row.estimated_cost_usd,
            "proposal_id": row.proposal_id,
            "task_id": str(row.task_id) if row.task_id else None,
            "status": row.status,
            "summary": row.summary,
            "usable": usable,
            "unusable_reason": None if usable else reason,
            "expires_at": expires.isoformat() if expires else None,
            "consumed_at": _as_utc(row.consumed_at).isoformat() if _as_utc(row.consumed_at) else None,
            "rejection_reason": row.rejection_reason,
        }


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Policy re-authorization
# ----------------------------------------------------------------------
def _policy_still_authorizes(
    row: TransactionApproval, binding: Mapping[str, str]
) -> bool:
    """Re-evaluate the autonomous policy against the transaction being broadcast.

    This is the load-bearing check for unattended execution. The approval row records
    a rule id; this asks the *live* policy whether that rule still authorizes this
    exact transaction. Nothing here is derived from the caller.

    It also maps the transfer back onto its category, so a rule for
    `tx.native_transfer` can never authorize an ERC-20 transfer (different `to`:
    the token contract) and vice versa.
    """
    from backend.app.security.autonomous_policy import Category, get_policy

    policy = get_policy()
    if not policy.enabled:
        logger.warning(
            "Broadcast refused: autonomous execution is disabled; approval %s cannot be used.",
            row.approval_id,
        )
        return False

    rule_id = row.policy_rule_id or rule_id_from_approver(row.approved_by)
    rule = next((r for r in policy.rules if r.rule_id == rule_id), None)
    if rule is None:
        logger.warning(
            "Broadcast refused: rule %r no longer exists in the policy; approval %s is dead.",
            rule_id, row.approval_id,
        )
        return False

    if rule.category not in VALUE_CATEGORIES:
        logger.warning(
            "Broadcast refused: rule %r is for %r, which is not a value-moving category.",
            rule_id, rule.category,
        )
        return False

    approved = canonical_binding(row.binding or {})
    destination = approved.get("to", "")
    # An ERC-20 transfer's `to` is the token contract, so the category is decided by
    # the calldata, which only an ERC-20 call carries.
    category = (
        Category.TX_TOKEN_TRANSFER
        if str(approved.get("data") or "0x") not in ("", "0x")
        else Category.TX_NATIVE_TRANSFER
    )

    decision = policy.evaluate(
        category,
        network=approved.get("network"),
        wallet=approved.get("wallet"),
        destination=destination,
        value_native=approved.get("value_wei"),
    )
    if not decision.allowed:
        logger.warning(
            "Broadcast refused by autonomous policy: %s (approval %s)", decision.reason, row.approval_id
        )
        return False
    return True


# ----------------------------------------------------------------------
# Engine wiring
# ----------------------------------------------------------------------
def bind_verifier() -> None:
    """Give the process-wide engine a working, database-backed approval verifier.

    Called once per process at startup. Without it `submit_raw` refuses every
    broadcast, which is the correct fail-closed behaviour but leaves value-moving
    operations permanently impossible; this is the operator-approved way to enable
    them. It changes nothing about *whether* approval is required.
    """
    from backend.app.database.session import AsyncSessionLocal
    from backend.app.transactions.engine import engine as tx_engine

    tx_engine.approval_verifier = _SessionScopedVerifier(AsyncSessionLocal)


class _SessionScopedVerifier:
    """Adapter that satisfies the engine's `ApprovalVerifier` protocol.

    The engine holds a single long-lived verifier, but approvals are per-transaction
    rows in a database, so each check needs its own short-lived session rather than
    one held open for the process lifetime.
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def verify_and_consume(
        self, *, approval_id: str, approved_by: str, binding: Mapping[str, str]
    ) -> bool:
        from backend.app.database.session import session_scope

        async with session_scope() as db:
            return await ApprovalService(db).verify_and_consume(
                approval_id=approval_id, approved_by=approved_by, binding=binding
            )


def verifier_status() -> Dict[str, Any]:
    """Whether broadcasting is possible at all, and why not when it is not."""
    from backend.app.transactions.engine import engine as tx_engine

    return {
        "approval_verifier_configured": tx_engine.approval_verifier is not None,
        "note": (
            "Approvals are stored server-side and consumed once per broadcast."
            if tx_engine.approval_verifier is not None
            else "Broadcasting is disabled: no approval verifier is bound."
        ),
    }