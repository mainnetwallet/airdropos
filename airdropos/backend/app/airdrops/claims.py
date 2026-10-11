"""Claim Lifecycle (Airdrop Claim State Machine + Service).

Mirrors the house pattern in `app/tasks/state_machine.py`: an explicit
transition table enforced in the backend, so a claim can never skip verification.

Lifecycle: PENDING -> SUBMITTED -> CONFIRMED -> DISTRIBUTED, with FAILED and
EXPIRED as terminal failure states.

Two rules carry the weight:

  1. **A claim reaches CONFIRMED only with a verified receipt.** The model
     stores `is_verified`; `assert_verified` refuses to confirm without it. A
     claim whose tx is on-chain but whose receipt has not been read stays
     SUBMITTED rather than being optimistically marked CONFIRMED.
  2. **EXPIRED beats everything.** A claim whose `claim_deadline` has passed can
     only move to EXPIRED, never forward, even if a late receipt arrives.

This module never signs or broadcasts. It records what has already happened on
chain. Submission is a reviewed tool path; this is the bookkeeping.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import AirdropCampaign, ClaimRecord

logger = logging.getLogger(__name__)


class ClaimStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    DISTRIBUTED = "DISTRIBUTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


TERMINAL_CLAIM_STATES: FrozenSet[ClaimStatus] = frozenset(
    {ClaimStatus.DISTRIBUTED, ClaimStatus.FAILED, ClaimStatus.EXPIRED}
)

# States a monitor job should keep polling.
PENDING_CLAIM_STATES: FrozenSet[ClaimStatus] = frozenset(
    {ClaimStatus.PENDING, ClaimStatus.SUBMITTED}
)


VALID_CLAIM_TRANSITIONS: Dict[ClaimStatus, Set[ClaimStatus]] = {
    ClaimStatus.PENDING: {ClaimStatus.SUBMITTED, ClaimStatus.FAILED, ClaimStatus.EXPIRED},
    ClaimStatus.SUBMITTED: {
        ClaimStatus.CONFIRMED,
        ClaimStatus.FAILED,
        ClaimStatus.EXPIRED,
    },
    ClaimStatus.CONFIRMED: {ClaimStatus.DISTRIBUTED, ClaimStatus.FAILED, ClaimStatus.EXPIRED},
    ClaimStatus.DISTRIBUTED: set(),
    ClaimStatus.FAILED: {ClaimStatus.EXPIRED},
    ClaimStatus.EXPIRED: set(),
}


class InvalidClaimTransition(Exception):
    """Raised when a claim is asked to make a transition the table forbids."""

    def __init__(self, current: ClaimStatus, requested: ClaimStatus) -> None:
        self.current = current
        self.requested = requested
        super().__init__(
            f"Invalid claim transition {current.value} -> {requested.value}. "
            f"Allowed: {sorted(s.value for s in VALID_CLAIM_TRANSITIONS.get(current, set())) or 'none (terminal)'}"
        )


def can_transition(current: ClaimStatus, requested: ClaimStatus) -> bool:
    return requested in VALID_CLAIM_TRANSITIONS.get(current, set())


def assert_transition(current: ClaimStatus, requested: ClaimStatus) -> None:
    if not can_transition(current, requested):
        raise InvalidClaimTransition(current, requested)


def assert_verified(status: ClaimStatus, *, is_verified: bool, has_receipt: bool) -> None:
    """A claim may only be CONFIRMED with a verified receipt.

    This is the claim-side analogue of `assert_completion_verified` for tasks.
    """
    if status is ClaimStatus.CONFIRMED and not (is_verified and has_receipt):
        raise ValueError(
            "A claim may only be CONFIRMED when is_verified is true and a receipt "
            "has been read. Leave it SUBMITTED if verification has not completed."
        )


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Treat naive timestamps stored by SQLite/Postgres as UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ClaimLifecycleService:
    """Advances claims by observing chain state. Never signs or broadcasts."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------
    async def transition(
        self,
        claim: ClaimRecord,
        new_status: ClaimStatus,
        *,
        receipt_status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> ClaimRecord:
        """Apply a validated claim transition and persist it."""
        current = ClaimStatus(claim.claim_status)
        assert_transition(current, new_status)

        now = datetime.now(timezone.utc)
        claim.claim_status = new_status.value

        if new_status is ClaimStatus.SUBMITTED:
            claim.claimed_at = now
        elif new_status is ClaimStatus.CONFIRMED:
            assert_verified(
                new_status,
                is_verified=bool(claim.is_verified),
                has_receipt=bool(receipt_status or claim.receipt_status),
            )
            claim.verified_at = now
            claim.is_verified = True
        elif new_status is ClaimStatus.DISTRIBUTED:
            claim.distribution_status = "CONFIRMED"
        elif new_status is ClaimStatus.FAILED:
            claim.distribution_status = "FAILED"
        elif new_status is ClaimStatus.EXPIRED:
            claim.distribution_status = "EXPIRED"

        if receipt_status is not None:
            claim.receipt_status = receipt_status
        if error is not None:
            claim.evidence_reference = error[:512]

        await self.db.flush()
        logger.info(
            "Claim %s: %s -> %s", claim.id, current.value, new_status.value
        )
        return claim

    # ------------------------------------------------------------------
    # Chain observation
    # ------------------------------------------------------------------
    async def refresh(self, claim: ClaimRecord) -> str:
        """Advance one claim by reading its receipt. Returns the new status.

        Failure here is never fatal: a claim whose RPC is unreachable simply
        stays in its current state and is retried on the next sweep.
        """
        from backend.app.transactions.engine import engine

        # Expiry is decided before chain state: a passed deadline is terminal
        # even if a receipt shows up late.
        deadline = await self._claim_deadline(claim)
        if deadline is not None and deadline <= datetime.now(timezone.utc):
            if can_transition(ClaimStatus(claim.claim_status), ClaimStatus.EXPIRED):
                await self.transition(claim, ClaimStatus.EXPIRED)
                await self.db.commit()
            return ClaimStatus.EXPIRED.value

        if not claim.tx_hash:
            return ClaimStatus(claim.claim_status).value

        try:
            receipt = await engine.get_receipt(claim.network, claim.tx_hash)
        except Exception as exc:
            logger.warning("Claim %s receipt lookup failed: %s", claim.id, exc)
            return ClaimStatus(claim.claim_status).value

        if receipt is None:
            # Still in the mempool. SUBMITTED is accurate; CONFIRMED would be a lie.
            if claim.claim_status == ClaimStatus.PENDING.value:
                await self.transition(claim, ClaimStatus.SUBMITTED)
                await self.db.commit()
            return ClaimStatus(claim.claim_status).value

        if receipt["status"] == "REVERTED":
            if can_transition(ClaimStatus(claim.claim_status), ClaimStatus.FAILED):
                await self.transition(
                    claim,
                    ClaimStatus.FAILED,
                    receipt_status="REVERTED",
                    error="Claim transaction reverted on-chain.",
                )
                await self.db.commit()
            return ClaimStatus.FAILED.value

        # Receipt present and successful.
        claim.receipt_status = "SUCCESS"
        await self._persist_transaction_evidence(claim, receipt)

        current = ClaimStatus(claim.claim_status)
        if current is ClaimStatus.PENDING:
            await self.transition(claim, ClaimStatus.SUBMITTED, receipt_status="SUCCESS")
            current = ClaimStatus.SUBMITTED

        if can_transition(current, ClaimStatus.CONFIRMED):
            claim.is_verified = True  # set before assert_verified
            try:
                await self.transition(claim, ClaimStatus.CONFIRMED, receipt_status="SUCCESS")
            except ValueError:
                logger.warning(
                    "Claim %s has a receipt but failed verification; leaving it %s",
                    claim.id, current.value,
                )
            else:
                await self.db.commit()
                return ClaimStatus.CONFIRMED.value

        await self.db.commit()
        return ClaimStatus(claim.claim_status).value

    async def _persist_transaction_evidence(
        self, claim: ClaimRecord, receipt: Dict[str, Any]
    ) -> None:
        """Record the receipt so the claim has an auditable evidence trail."""
        from backend.app.models import BlockchainTransaction, TransactionReceipt

        # The receipt must hang off a real transaction row; an orphan receipt
        # would satisfy the foreign key only by not existing.
        tx = await self.db.scalar(
            select(BlockchainTransaction).where(
                BlockchainTransaction.tx_hash == claim.tx_hash
            )
        )
        if tx is None:
            return

        already = await self.db.scalar(
            select(TransactionReceipt).where(TransactionReceipt.transaction_id == tx.id)
        )
        if already is not None:
            return

        self.db.add(
            TransactionReceipt(
                transaction_id=tx.id,
                block_number=receipt.get("block_number"),
                gas_used=str(receipt["gas_used"]) if receipt.get("gas_used") is not None else None,
                effective_gas_price=(
                    str(receipt["effective_gas_price"])
                    if receipt.get("effective_gas_price") is not None
                    else None
                ),
                status="SUCCESS",
                logs=receipt.get("logs"),
                contract_address=receipt.get("contract_address"),
                timestamp=datetime.now(timezone.utc),
            )
        )
        claim.evidence_reference = f"receipt:{claim.tx_hash}"

    async def _claim_deadline(self, claim: ClaimRecord) -> Optional[datetime]:
        """The claim's deadline: its campaign's, falling back to nothing."""
        campaign = await self.db.get(AirdropCampaign, claim.campaign_id)
        return _aware(campaign.claim_deadline) if campaign else None

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    async def summary(self, campaign_id: Optional[Any] = None) -> Dict[str, int]:
        """Count claims by status, optionally for one campaign."""
        query = select(ClaimRecord.claim_status)
        if campaign_id is not None:
            query = query.where(ClaimRecord.campaign_id == campaign_id)

        counts: Dict[str, int] = {status.value: 0 for status in ClaimStatus}
        rows = (await self.db.execute(query)).scalars().all()
        for value in rows:
            counts[value] = counts.get(value, 0) + 1
        counts["TOTAL"] = len(rows)
        return counts

    async def list_for_campaign(self, campaign_id: Any) -> list[ClaimRecord]:
        result = await self.db.execute(
            select(ClaimRecord)
            .where(ClaimRecord.campaign_id == campaign_id)
            .order_by(ClaimRecord.created_at.desc())
        )
        return list(result.scalars().all())