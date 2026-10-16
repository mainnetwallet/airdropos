"""Execution Leases: stale-lease detection and duplicate-claim prevention.

Complements the row-level lock in `TaskManager.claim_next_task()`. That lock
prevents two *live* workers from claiming the same row. A lease additionally
handles the *dead worker* case: a lease whose `expires_at` has passed is stale
and may be broken, so a crashed worker's task does not sit in IN_PROGRESS forever.

Irreversible work is protected explicitly: a task holding an irreversible step
is never auto-resumed by lease recovery without operator acknowledgement.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.inbox.raise_intervention import park_for_intervention
from backend.app.inbox.service import OPERATOR_FALLBACK_EMAIL, UNKNOWN_REASON
from backend.app.models import ExecutionLease, ExecutionPlan, Task
from backend.app.tasks.state_machine import TaskStatus

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_MINUTES = 30


class LeaseConflict(Exception):
    """Raised when another worker holds a live lease on a task."""


@dataclass
class LeaseInfo:
    task_id: str
    worker_id: str
    lease_token: str
    expires_at: datetime
    stale: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "expires_at": self.expires_at.isoformat(),
            "stale": self.stale,
        }


class LeaseManager:
    """Acquire, renew, release, and break execution leases."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
        """Read a stored timestamp back as UTC-aware.

        SQLite has no timezone type, so a timestamp written as aware comes back naive.
        Comparing that to an aware `now()` raised TypeError rather than reporting a stale
        lease, which meant the second `acquire` on an existing task crashed instead of
        correctly raising LeaseConflict or taking over an expired lease.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    # ------------------------------------------------------------------
    async def acquire(
        self,
        task: Task,
        worker_id: str,
        ttl_minutes: int = DEFAULT_LEASE_TTL_MINUTES,
    ) -> LeaseInfo:
        """Take the lease for a task, or raise LeaseConflict if one is live."""
        now = self._now()
        expires = now + timedelta(minutes=ttl_minutes)

        existing = await self.db.scalar(
            select(ExecutionLease).where(ExecutionLease.task_id == task.id)
        )

        if existing is not None:
            live = existing.released_at is None and self._as_utc(existing.expires_at) > now
            if live and existing.worker_id != worker_id:
                raise LeaseConflict(
                    f"Task {task.task_id} is leased by worker '{existing.worker_id}' "
                    f"until {existing.expires_at.isoformat()}."
                )
            # Same worker re-acquiring, or a stale lease: take it over.
            existing.worker_id = worker_id
            existing.lease_token = secrets.token_hex(32)
            existing.acquired_at = now
            existing.renewed_at = now
            existing.expires_at = expires
            existing.released_at = None
            existing.broken_reason = None
            await self.db.flush()
            logger.info("Lease (re)acquired on %s by %s", task.task_id, worker_id)
            return LeaseInfo(task.task_id, worker_id, existing.lease_token, expires, False)

        lease = ExecutionLease(
            task_id=task.id,
            worker_id=worker_id,
            lease_token=secrets.token_hex(32),
            acquired_at=now,
            renewed_at=now,
            expires_at=expires,
        )
        self.db.add(lease)
        await self.db.flush()
        logger.info("Lease acquired on %s by %s", task.task_id, worker_id)
        return LeaseInfo(task.task_id, worker_id, lease.lease_token, expires, False)

    async def renew(self, task: Task, lease_token: str, ttl_minutes: int = DEFAULT_LEASE_TTL_MINUTES) -> bool:
        """Extend a lease. Returns False if the token no longer matches,
        which means another worker has taken over."""
        lease = await self.db.scalar(
            select(ExecutionLease).where(ExecutionLease.task_id == task.id)
        )
        if lease is None or lease.lease_token != lease_token:
            logger.warning("Lease renewal rejected for %s: token mismatch", task.task_id)
            return False
        lease.expires_at = self._now() + timedelta(minutes=ttl_minutes)
        lease.renewed_at = self._now()
        await self.db.flush()
        return True

    async def release(self, task: Task, lease_token: str) -> None:
        lease = await self.db.scalar(
            select(ExecutionLease).where(ExecutionLease.task_id == task.id)
        )
        if lease is not None and lease.lease_token == lease_token:
            lease.released_at = self._now()
            await self.db.flush()

    # ------------------------------------------------------------------
    async def find_stale_leases(self, limit: int = 100) -> List[ExecutionLease]:
        now = self._now()
        return list(
            (
                await self.db.execute(
                    select(ExecutionLease)
                    .where(ExecutionLease.released_at.is_(None))
                    .where(ExecutionLease.expires_at <= now)
                    .limit(limit)
                )
            ).scalars().all()
        )

    async def break_stale_leases(self, limit: int = 100) -> Dict[str, int]:
        """Recover tasks abandoned by dead workers.

        A task whose side effect may have happened is NOT auto-resumed: a transact step that was
        running or completed, or an interact step that was running, has an unknown real-world
        outcome, and blind resubmission is exactly what this system must never do. Such tasks are
        parked as NEEDS_USER_ACTION for operator review. Everything else is bounded by
        ``max_retries``. Status changes go through ``TaskManager.transition`` so they are
        validated and leave an audit event.

        Not currently scheduled: worker start-up recovery (``recover_interrupted_tasks``) covers
        crashes. If this is ever wired to a timer, the lease TTL must comfortably exceed the longest
        single step, or a live run could be broken.
        """
        from backend.app.tasks.manager import TaskManager

        manager = TaskManager(self.db)
        stale = await self.find_stale_leases(limit)
        recovered, parked, failed = 0, 0, 0

        for lease in stale:
            task = await self.db.get(Task, lease.task_id)
            if task is None:
                lease.released_at = self._now()
                lease.broken_reason = "Task record no longer exists."
                await self.db.flush()
                continue

            uncertain = await self._uncertain_side_effects(task)

            lease.released_at = self._now()
            lease.broken_reason = (
                f"Lease expired at {lease.expires_at.isoformat()} held by '{lease.worker_id}'."
            )
            await self.db.flush()

            if task.status != TaskStatus.IN_PROGRESS.value:
                # Only a task that was executing is abandoned. RETRYING/READY/parked/terminal
                # tasks just had a leftover lease released; do not consume another retry.
                logger.debug("Stale lease on %s in state %s; released only", task.task_id, task.status)
                continue

            if uncertain:
                # Copy: the JSONB column has no mutation tracking, so editing the stored dict in
                # place would not be persisted.
                details = dict(task.error_details or {})
                details["lease_recovery"] = (
                    "Stale lease broken while a side-effecting step may have been in flight. "
                    f"Parked for operator review to prevent duplicate execution. {uncertain}"
                )[:900]
                task.error_details = details
                # Raises the intervention request and posts it to the task's Agent Inbox
                # conversation, then parks it. Skipped silently if it cannot: the caller still
                # breaks the lease, so a dead worker never keeps running.
                try:
                    await park_for_intervention(
                        self.db,
                        task,
                        owner_email=getattr(task, "owner_email", None) or OPERATOR_FALLBACK_EMAIL,
                        error_text=(
                            "A previous worker's lease expired while a side-effecting step may "
                            f"still have been in flight: {uncertain}. I stopped this task rather "
                            "than repeating a step whose real-world outcome I cannot confirm."
                        ),
                        error_category="HUMAN_INTERVENTION_REQUIRED",
                        blocking_step=uncertain,
                        force_reason_code=UNKNOWN_REASON,
                    )
                except Exception as exc:
                    logger.error(
                        "Could not raise an intervention for %s under a stale lease: %s",
                        task.task_id, exc,
                    )
                parked += 1
                logger.warning("Task %s parked for operator review under a stale lease.", task.task_id)
            elif task.retry_count < task.max_retries:
                await manager.transition(
                    task, TaskStatus.RETRYING,
                    reason=f"stale lease held by '{lease.worker_id}' broken",
                    progress=task.progress,
                )
                recovered += 1
                logger.info(
                    "Recovered task %s from stale lease held by '%s' (attempt %s)",
                    task.task_id, lease.worker_id, task.retry_count,
                )
            else:
                await manager.transition(
                    task, TaskStatus.FAILED,
                    reason="stale lease broken (retries exhausted)",
                    progress=task.progress,
                )
                failed += 1

            await self.db.flush()

        if recovered or parked or failed:
            logger.info(
                "Lease recovery complete: %s recovered, %s parked, %s failed", recovered, parked, failed
            )
        return {
            "recovered": recovered,
            "parked_for_review": parked,
            "failed": failed,
            "stale_found": len(stale),
        }

    async def _uncertain_side_effects(self, task: Task) -> Optional[str]:
        """Describe steps whose real-world outcome is unknown, or None when it is safe to retry.

        transact: running or completed. interact: running. Read-only steps never block recovery.
        """
        plan = await self.db.scalar(
            select(ExecutionPlan).where(
                ExecutionPlan.task_id == task.id,
                ExecutionPlan.is_current.is_(True),
            )
        )
        if plan is None:
            return None
        found: List[str] = []
        for raw in plan.steps or []:
            kind, status = raw.get("kind"), raw.get("status")
            if (kind == "transact" and status in ("running", "completed")) or (
                kind == "interact" and status == "running"
            ):
                found.append(f"step {raw.get('index')} ({kind}) was {status}")
        return "; ".join(found) if found else None

    # ------------------------------------------------------------------
    async def reconcile_transactions(self) -> Dict[str, Any]:
        """Reconcile submitted-but-unconfirmed transactions against chain state.

        Called on worker startup. A transaction whose status is PENDING is
        re-checked; a REVERTED one is marked failed. Nothing is resubmitted.
        """
        from backend.app.models import BlockchainTransaction

        pending = list(
            (
                await self.db.execute(
                    select(BlockchainTransaction).where(
                        BlockchainTransaction.status.in_(["PENDING", "SUBMITTED"])
                    )
                )
            ).scalars().all()
        )

        checked, still_pending, reverted = 0, 0, 0
        for tx in pending:
            if not tx.tx_hash:
                continue
            from backend.app.transactions.engine import TransactionRejected, engine

            try:
                receipt = await engine.get_receipt(tx.network, tx.tx_hash)
            except TransactionRejected as exc:
                tx.error_message = f"Reconciliation RPC failure: {exc}"
                tx.error_category = "RPC_FAILURE"
                await self.db.flush()
                continue

            checked += 1
            if receipt is None:
                still_pending += 1
                continue

            # The model column is `confirmation_count`; assigning `confirmations`
            # would create a stray instance attribute that never persists, so the
            # progress bar silently froze at whatever it was when it was created.
            tx.status = receipt["status"]
            tx.confirmation_count = await engine.get_confirmations(tx.network, tx.tx_hash)
            tx.confirmed_at = self._now() if receipt["status"] == "CONFIRMED" else None
            if receipt["status"] == "REVERTED":
                tx.error_message = "Transaction reverted on-chain."
                tx.error_category = "TX_REVERTED"
                reverted += 1
            await self.db.flush()

        logger.info(
            "Transaction reconciliation: %s checked, %s pending, %s reverted",
            checked, still_pending, reverted,
        )
        return {
            "checked": checked,
            "still_pending": still_pending,
            "reverted": reverted,
            "note": "Reconciliation only observes chain state. Nothing was resubmitted.",
        }
