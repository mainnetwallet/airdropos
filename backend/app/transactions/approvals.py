"""Server-side human approval for on-chain broadcasts.

`TransactionEngine.submit_raw` will not broadcast on the strength of a caller-supplied
flag: it asks an independent `ApprovalVerifier` whether a named human approved *this
exact transaction*, and that verifier must consume the approval atomically so one
approval authorises exactly one broadcast.

This module is that verifier, plus the service the API and the agent use to request
an approval in the first place. Two rules shape it:

* An approval is bound to one transaction (network, chain id, wallet, destination,
  value, signed-payload hash). Approving "send some ETH" is not a thing; the operator
  approves a specific, quoted transfer.
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
    lowercase from configuration; everything else is stringified verbatim so `1` and
    `"1"` cannot compare differently.
    """
    out: Dict[str, str] = {}
    for key, value in binding.items():
        text = "" if value is None else str(value)
        out[str(key)] = text.strip().lower() if str(key) in ("wallet", "to") else text
    return out


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
    ) -> TransactionApproval:
        """Record that a named human approved one specific transaction."""
        who = str(approved_by or "").strip()
        if not who:
            raise ApprovalError("An approval must name the human who gave it.")
        if who.lower() in NON_HUMAN_APPROVERS:
            raise ApprovalError(
                f"'{who}' is not a person. The agent cannot approve its own transaction."
            )

        normalized = canonical_binding(binding)
        for required in ("network", "wallet", "to"):
            if not normalized.get(required):
                raise ApprovalError(f"Approval binding is missing '{required}'.")

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
            task_id=task_id,
            summary=(summary or "")[:2000],
            status=STATUS_PENDING,
            expires_at=_now() + timedelta(minutes=max(1, min(ttl_minutes, 120))),
        )
        self.db.add(row)
        await self.db.flush()
        logger.info(
            "Approval %s recorded for %s -> %s on %s by %s",
            row.approval_id, row.wallet_address, row.to_address, row.network, who,
        )
        return row

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get(self, approval_id: str) -> Optional[TransactionApproval]:
        return await self.db.scalar(
            select(TransactionApproval).where(TransactionApproval.approval_id == approval_id)
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
        if str(row.approved_by).strip().lower() in NON_HUMAN_APPROVERS:
            return False, "The approver is not a person."

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