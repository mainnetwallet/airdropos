"""Execution Leases: stale-lease detection and duplicate-claim prevention.

Complements the row-level lock in `TaskManager.claim_next_task()`. That lock
prevents two *live* workers from claiming the same row. A lease additionally
handles the *dead worker* case: a lease whose `expires_at` has passed is stale
and may be broken, so a crashed worker's task does not sit in IN_PROGRESS forever.

Irreversible work is protected explicitly: a task holding an irreversible step
is never auto-resumed by lease recovery without operator acknowledgement.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.inbox.raise_intervention import park_for_intervention
from backend.app.inbox.service import OPERATOR_FALLBACK_EMAIL, UNKNOWN_REASON
from backend.app.models import ExecutionLease, ExecutionPlan, Task
from backend.app.tasks.state_machine import TaskStatus

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_MINUTES = 30

# A task is IN_PROGRESS from the moment it is claimed, but its lease is only taken a moment later
# (and a task re-run after an earlier attempt still has that attempt's released lease row). Recovery
# must not mistake that gap for an orphan, so a task with no live lease is only treated as
# orphaned once it has been quiet for this long.
RECOVERY_GRACE_SECONDS = 120

# Identifies THIS process. `worker-{pid}` was not unique: containers routinely run as pid 1, and a
# restarted worker reused its predecessor's id, so a lease held by a dead process looked like the
# new process's own. Host + pid + a random suffix is unique per process incarnation.
PROCESS_INSTANCE_ID = f"{socket.gethostname()[:40]}-{os.getpid()}-{secrets.token_hex(4)}"


def new_run_worker_id(prefix: str = "controller") -> str:
    """A holder id unique to one execution run (not merely one process).

    ``controller-{id(self):x}`` was a memory address: unique only inside one interpreter, and
    reusable once the object was freed. Two runs of the same task must never share a holder id,
    because a shared id is exactly what let a duplicate "take over" a live lease.
    """
    return f"{prefix}-{PROCESS_INSTANCE_ID}-{secrets.token_hex(3)}"


# How long a recovery worker's claim on one task lives. Recovery of a single task is a handful of
# short writes, so this only matters when the claimant dies mid-recovery: the claim then lapses
# and a later sweep recovers the task. It is deliberately much shorter than an execution lease.
RECOVERY_CLAIM_TTL_MINUTES = 5

# Heartbeat: renew well inside the TTL so several consecutive failures are survivable, but not so
# often that a healthy run spends its time writing to the database.
HEARTBEAT_MAX_INTERVAL_SECONDS = 60.0
HEARTBEAT_RETRY_INTERVAL_SECONDS = 5.0
HEARTBEAT_RENEW_TIMEOUT_SECONDS = 20.0


class LeaseConflict(Exception):
    """Raised when another worker holds a live lease on a task."""


class LeaseLostError(Exception):
    """This run no longer (provably) owns its task, so it must not start anything new.

    ``in_flight`` is set when the ownership check failed AFTER a side-effecting operation had
    already been started: that operation's outcome is UNKNOWN. It may have completed, failed or
    be half done, and the run must say so rather than treat it as not having happened.
    """

    def __init__(self, reason: str, *, in_flight: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.in_flight = in_flight


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
        ttl_minutes: float = DEFAULT_LEASE_TTL_MINUTES,
    ) -> LeaseInfo:
        """Take the lease for a task, or raise LeaseConflict if one is live.

        A live lease conflicts whoever asks, the holder's own id included: re-acquiring it would
        rotate the token under the original run, leaving two runs that both believe they own the
        task. Only a released or expired lease can be taken, and the takeover is a compare-and-set
        on the token that was read, so two workers racing for the same stale lease cannot both win.
        """
        now = self._now()
        expires = now + timedelta(minutes=ttl_minutes)

        existing = await self.db.scalar(
            select(ExecutionLease).where(ExecutionLease.task_id == task.id)
        )

        if existing is not None:
            live = existing.released_at is None and self._as_utc(existing.expires_at) > now
            if live:
                raise LeaseConflict(
                    f"Task {task.task_id} is leased by worker '{existing.worker_id}' "
                    f"until {self._as_utc(existing.expires_at).isoformat()}."
                )
            token = secrets.token_hex(32)
            taken = await self.db.execute(
                update(ExecutionLease)
                .where(
                    ExecutionLease.id == existing.id,
                    ExecutionLease.lease_token == existing.lease_token,
                    or_(ExecutionLease.released_at.is_not(None), ExecutionLease.expires_at <= now),
                )
                .values(
                    worker_id=worker_id,
                    lease_token=token,
                    acquired_at=now,
                    renewed_at=now,
                    expires_at=expires,
                    released_at=None,
                    broken_reason=None,
                )
                .execution_options(synchronize_session="fetch")
            )
            if (taken.rowcount or 0) != 1:
                raise LeaseConflict(f"Task {task.task_id} was leased by another worker first.")
            logger.info("Lease (re)acquired on %s by %s", task.task_id, worker_id)
            return LeaseInfo(task.task_id, worker_id, token, expires, False)

        lease = ExecutionLease(
            task_id=task.id,
            worker_id=worker_id,
            lease_token=secrets.token_hex(32),
            acquired_at=now,
            renewed_at=now,
            expires_at=expires,
        )
        try:
            # SAVEPOINT: a lost insert race must not abort the caller's whole transaction.
            async with self.db.begin_nested():
                self.db.add(lease)
                await self.db.flush()
        except IntegrityError as exc:
            raise LeaseConflict(f"Task {task.task_id} was leased by another worker first.") from exc
        logger.info("Lease acquired on %s by %s", task.task_id, worker_id)
        return LeaseInfo(task.task_id, worker_id, lease.lease_token, expires, False)

    async def renew(self, task: Task, lease_token: str, ttl_minutes: float = DEFAULT_LEASE_TTL_MINUTES) -> bool:
        """Extend a lease. Returns False unless the caller still owns a LIVE lease.

        The token must match AND the lease must be neither released nor expired. Matching the
        token alone let a worker whose lease had been released (by recovery, by a stale-lease
        break, or by its own run ending) or had run out revive it and keep writing progress over
        whichever worker had meanwhile taken the task. The check and the extension are one
        guarded UPDATE, so a takeover that lands between them cannot be overwritten either.
        """
        now = self._now()
        renewed = await self.db.execute(
            update(ExecutionLease)
            .where(
                ExecutionLease.task_id == task.id,
                ExecutionLease.lease_token == lease_token,
                ExecutionLease.released_at.is_(None),
                ExecutionLease.expires_at > now,
            )
            .values(expires_at=now + timedelta(minutes=ttl_minutes), renewed_at=now)
            .execution_options(synchronize_session="fetch")
        )
        if (renewed.rowcount or 0) == 1:
            return True

        lease = await self.db.scalar(
            select(ExecutionLease).where(ExecutionLease.task_id == task.id)
        )
        if lease is None:
            why = "no lease exists"
        elif lease.lease_token != lease_token:
            why = "superseded by another worker"
        elif lease.released_at is not None:
            why = "already released"
        else:
            why = "expired"
        logger.warning("Lease renewal rejected for %s: %s", task.task_id, why)
        return False

    async def release(self, task: Task, lease_token: str) -> None:
        """Release the lease if this token still holds it. Never touches another holder's."""
        await self.db.execute(
            update(ExecutionLease)
            .where(
                ExecutionLease.task_id == task.id,
                ExecutionLease.lease_token == lease_token,
                ExecutionLease.released_at.is_(None),
            )
            .values(released_at=self._now())
            .execution_options(synchronize_session="fetch")
        )

    @staticmethod
    async def release_detached(task_pk: Any, lease_token: str, reason: str) -> None:
        """Release a lease in its OWN committed transaction.

        For a run that is dying (cancelled, or raised): its session is about to be rolled back,
        and a release that only lived in that session would roll back with it, leaving a dead
        run's lease live for its whole TTL. Guarded by the token, so it can only release the
        caller's own lease.
        """
        from backend.app.database.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ExecutionLease)
                .where(
                    ExecutionLease.task_id == task_pk,
                    ExecutionLease.lease_token == lease_token,
                    ExecutionLease.released_at.is_(None),
                )
                .values(released_at=datetime.now(timezone.utc), broken_reason=reason[:500])
            )
            await db.commit()

    @staticmethod
    async def renew_detached(
        task_pk: Any, lease_token: str, ttl_minutes: float = DEFAULT_LEASE_TTL_MINUTES
    ) -> bool:
        """Renew a lease in its OWN committed transaction. True only if the caller still owns it.

        The heartbeat cannot use the run's session: that session is mid-transaction while a tool
        or LLM call is awaited, and an `AsyncSession` must never be used by two coroutines at
        once. The guard is the same one `renew` applies (token matches, not released, not
        expired) and is a single UPDATE, so a heartbeat can never extend a lease that another
        worker has taken, nor revive one that has already run out.
        """
        from backend.app.database.session import AsyncSessionLocal

        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(ExecutionLease)
                .where(
                    ExecutionLease.task_id == task_pk,
                    ExecutionLease.lease_token == lease_token,
                    ExecutionLease.released_at.is_(None),
                    ExecutionLease.expires_at > now,
                )
                .values(expires_at=now + timedelta(minutes=ttl_minutes), renewed_at=now)
            )
            await db.commit()
            return (result.rowcount or 0) == 1

    async def try_claim_for_recovery(self, task: Task, claimant_id: str) -> Optional[LeaseInfo]:
        """Atomically claim the exclusive right to recover `task`, or return None if another got it.

        The claim IS a lease acquisition, so it reuses the one compare-and-set the system already
        trusts: taking over a released/expired lease is a CAS on the token that was read, and
        creating the first lease row is protected by the unique constraint on ``task_id``. Both
        work identically on SQLite and PostgreSQL, unlike ``SELECT ... FOR UPDATE SKIP LOCKED``,
        which SQLite ignores. Exactly one of any number of concurrent claimants succeeds.

        The caller MUST commit before acting, so the claim is visible to the other recovery
        workers, and must release it (in the same transaction as the outcome) when done. While
        it is held, a recovering sweep sees a live lease and leaves the task alone, and no run can
        acquire the task. If the claimant dies, the claim simply expires.
        """
        try:
            return await self.acquire(task, claimant_id, ttl_minutes=RECOVERY_CLAIM_TTL_MINUTES)
        except LeaseConflict:
            return None

    async def _break_if_stale(self, lease: ExecutionLease, reason: str) -> bool:
        """Release `lease` only if it is STILL the same, unreleased, expired lease.

        A plain "set released_at" could land on a lease another worker had just taken over,
        silently ending a live run's ownership. Guarding on the token and expiry makes the break
        a compare-and-set: it succeeds for exactly one caller and only for a genuinely stale lease.
        """
        now = self._now()
        broken = await self.db.execute(
            update(ExecutionLease)
            .where(
                ExecutionLease.id == lease.id,
                ExecutionLease.lease_token == lease.lease_token,
                ExecutionLease.released_at.is_(None),
                ExecutionLease.expires_at <= now,
            )
            .values(released_at=now, broken_reason=reason[:500])
            .execution_options(synchronize_session="fetch")
        )
        return (broken.rowcount or 0) == 1

    async def break_if_orphaned(
        self, task: Task, grace_seconds: int = RECOVERY_GRACE_SECONDS
    ) -> Optional[str]:
        """Decide whether an IN_PROGRESS task has no live owner, breaking a stale lease if so.

        Returns a reason when the task is orphaned and may be recovered, or None when it must be
        left alone: a live worker holds it, or it is too fresh to tell (claimed but not yet
        leased). Recovery that skipped this check released every IN_PROGRESS task's lease at
        start-up, including ones another live worker was running.
        """
        now = self._now()
        lease = await self.db.scalar(
            select(ExecutionLease).where(ExecutionLease.task_id == task.id)
        )
        if lease is not None and lease.released_at is None:
            if self._as_utc(lease.expires_at) > now:
                return None  # a live worker owns it
            broken = await self._break_if_stale(
                lease,
                f"Lease expired at {self._as_utc(lease.expires_at).isoformat()} "
                f"held by '{lease.worker_id}'.",
            )
            return f"stale lease held by '{lease.worker_id}' broken" if broken else None

        # No lease, or a released one: the previous run is over, but a worker may have claimed
        # the task a moment ago and not leased it yet. Only a quiet task is an orphan.
        newest = max(
            (t for t in (self._as_utc(getattr(task, "updated_at", None)),
                         self._as_utc(lease.released_at) if lease is not None else None)
             if t is not None),
            default=None,
        )
        if newest is not None and (now - newest).total_seconds() < grace_seconds:
            return None
        return "no live lease" if lease is None else "lease already released"

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

            if not await self._break_if_stale(
                lease,
                f"Lease expired at {self._as_utc(lease.expires_at).isoformat()} "
                f"held by '{lease.worker_id}'.",
            ):
                # Taken over, renewed or already broken since it was listed: not ours to recover.
                logger.info("Stale lease on %s changed hands; left alone", task.task_id)
                continue

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


class LeaseHeartbeat:
    """Keeps one run's lease alive while it works, and tells the run when it no longer owns the task.

    A lease used to be renewed only after an iteration finished, so a single long LLM call or
    tool call could outlast the TTL and let recovery (or another worker) take a task that was
    still running. This renews it periodically from a background task with its own database
    session, and exposes the answer to "may I start something new?" so the run can stop.

    Fails CLOSED:

    * A renewal the database REJECTS (token superseded, released or expired) is permanent. The
      lease cannot be revived, so ownership is gone for good.
    * A renewal that merely ERRORS (database busy, timeout) is retried, but the run is only
      treated as owning the task for as long as the last CONFIRMED renewal guarantees. Timing is
      measured on the monotonic clock from when that renewal was requested, so it errs short.
    * ``verify()`` re-checks ownership in the database right now. The run calls it before it
      starts anything with a side effect.

    The heartbeat never cancels an in-flight operation. Stopping a tool half way would not make it
    not have happened; it would only hide that it might have.
    """

    def __init__(
        self,
        task_pk: Any,
        task_label: str,
        lease_token: str,
        *,
        ttl_minutes: float = DEFAULT_LEASE_TTL_MINUTES,
        interval_seconds: Optional[float] = None,
        expires_at: Optional[datetime] = None,
    ) -> None:
        self.task_pk = task_pk
        self.task_label = task_label
        self._token = lease_token
        self._ttl_minutes = ttl_minutes
        ttl_seconds = ttl_minutes * 60.0
        self._ttl_seconds = ttl_seconds
        # A safety margin so local bookkeeping expires before the database's does.
        self._margin = min(30.0, ttl_seconds * 0.1)
        self._interval = (
            interval_seconds
            if interval_seconds is not None
            else min(HEARTBEAT_MAX_INTERVAL_SECONDS, max(1.0, ttl_seconds / 6.0))
        )
        remaining = ttl_seconds
        if expires_at is not None:
            remaining = (LeaseManager._as_utc(expires_at) - datetime.now(timezone.utc)).total_seconds()
        self._confirmed_until = time.monotonic() + max(0.0, remaining) - self._margin
        self._lost_reason: Optional[str] = None
        self._task: Optional["asyncio.Task[None]"] = None
        self._renew_lock = asyncio.Lock()
        self.renewals = 0
        self.failed_attempts = 0

    # -- state ---------------------------------------------------------
    @property
    def lost_reason(self) -> Optional[str]:
        """Why the run may not start new side effects, or None while ownership is intact."""
        if self._lost_reason is not None:
            return self._lost_reason
        if time.monotonic() >= self._confirmed_until:
            return (
                "the lease could not be renewed in time; it may have expired and the task may "
                "already belong to another worker"
            )
        return None

    @property
    def owned(self) -> bool:
        return self.lost_reason is None

    def _mark_lost(self, reason: str) -> None:
        if self._lost_reason is None:
            self._lost_reason = reason
            logger.warning("LEASE_LOST task=%s: %s", self.task_label, reason)

    # -- renewal -------------------------------------------------------
    async def _renew_once(self) -> bool:
        return await LeaseManager.renew_detached(self.task_pk, self._token, self._ttl_minutes)

    async def _renew_and_record(self, *, timeout: Optional[float]) -> bool:
        """One guarded renewal. True: owned and extended. False: the database refused it.

        Raises on infrastructure errors, which say nothing about ownership.
        """
        async with self._renew_lock:
            if self._lost_reason is not None:
                return False
            requested_at = time.monotonic()
            coro = self._renew_once()
            ok = await (asyncio.wait_for(coro, timeout) if timeout else coro)
            if ok:
                self._confirmed_until = requested_at + self._ttl_seconds - self._margin
                self.renewals += 1
                return True
            self._mark_lost(
                "the database refused the renewal: the lease was taken over, released or expired"
            )
            return False

    async def verify(self) -> bool:
        """Confirm ownership in the database right now (and extend it). Call before a side effect.

        False on a rejected renewal and also when ownership could not be confirmed because the
        check itself failed: starting something irreversible on an unconfirmed lease is exactly
        what this exists to prevent.
        """
        if self._lost_reason is not None:
            return False
        try:
            return await self._renew_and_record(timeout=HEARTBEAT_RENEW_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failed_attempts += 1
            logger.warning(
                "Could not confirm lease ownership of %s: %s: %s",
                self.task_label, type(exc).__name__, exc,
            )
            return False

    async def _run(self) -> None:
        delay = self._interval
        while True:
            await asyncio.sleep(delay)
            try:
                ok = await self._renew_and_record(timeout=HEARTBEAT_RENEW_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failed_attempts += 1
                logger.warning(
                    "Lease heartbeat for %s failed (%s: %s); retrying",
                    self.task_label, type(exc).__name__, exc,
                )
                if time.monotonic() >= self._confirmed_until:
                    # Nothing renewed it in time, so the lease has run out in the database too
                    # (and a lapsed lease cannot be renewed). Ownership is gone for good.
                    self._mark_lost("the lease expired while the heartbeat could not reach the database")
                    return
                delay = min(self._interval, HEARTBEAT_RETRY_INTERVAL_SECONDS)
                continue
            if not ok:
                return  # already marked lost
            delay = self._interval

    async def _guarded_run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # a dead heartbeat must read as lost, never as healthy
            self._mark_lost(f"the lease heartbeat crashed: {type(exc).__name__}: {exc}")

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "LeaseHeartbeat":
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._guarded_run(), name=f"lease-heartbeat-{self.task_label}"
            )
        return self

    async def stop(self) -> None:
        """Stop the background task and wait for it. Safe to call twice, or when never started."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        # gather(return_exceptions=True) absorbs the heartbeat task's own cancellation but still
        # lets a cancellation aimed at THE CALLER propagate, which `except CancelledError` would
        # have swallowed.
        await asyncio.gather(task, return_exceptions=True)

    async def __aenter__(self) -> "LeaseHeartbeat":
        return self.start()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()
