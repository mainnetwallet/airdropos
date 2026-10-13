"""Persistent Background Scheduler.

Job definitions live in PostgreSQL, so schedules survive restarts. Duplicate
registration is prevented by a unique constraint on job_name (Part 21).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.database.session import AsyncSessionLocal
from backend.app.models import AutomationSchedule, ScheduledRun
from backend.app.security.emergency_stop import emergency_stop
from backend.app.terminal.events import publish_success

logger = logging.getLogger(__name__)

JobHandler = Callable[[AsyncSession, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class Scheduler:
    """Interval-based scheduler with persistent job definitions."""

    def __init__(self) -> None:
        self._jobs: Dict[str, JobHandler] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._running = False

    # ------------------------------------------------------------------
    def register_job(self, job_name: str, job_type: str, interval_minutes: int, handler: JobHandler) -> None:
        if job_name in self._jobs:
            logger.warning("Job '%s' already registered; ignoring duplicate.", job_name)
            return
        self._jobs[job_name] = handler
        # DEBUG, not INFO: the worker registers every default job on every start, so
        # this was one line per job before the summary existed. `start()` reports the
        # count, which is the number an operator actually wants; the per-job detail
        # (name, interval) stays one `LOG_LEVEL=DEBUG` away.
        logger.debug("Registered scheduler job '%s' every %smin", job_name, interval_minutes)

    # ------------------------------------------------------------------
    async def ensure_schedule(self, db: AsyncSession, job_name: str, job_type: str, interval_minutes: int) -> AutomationSchedule:
        """Insert the job definition if absent. Idempotent across restarts."""
        existing = await db.scalar(
            select(AutomationSchedule).where(AutomationSchedule.job_name == job_name)
        )
        if existing is not None:
            return existing

        schedule = AutomationSchedule(
            job_name=job_name,
            job_type=job_type,
            trigger_type="INTERVAL",
            interval_minutes=interval_minutes,
            is_active=True,
            next_run_at=datetime.now(timezone.utc) + timedelta(minutes=interval_minutes),
        )
        db.add(schedule)
        await db.flush()
        # DEBUG for the same reason as register_job: on a first boot this is one line
        # per job, and nothing changed for the operator beyond "the scheduler is up".
        logger.debug("Persisted schedule '%s'", job_name)
        return schedule

    async def start(self, db: AsyncSession) -> None:
        """Start the scheduler loop and re-arm all active schedules."""
        if self._running:
            return
        self._running = True
        logger.info("Scheduler started: %d jobs registered", len(self._jobs))
        # The milestone the operator cares about, published by the component
        # that reached it, so the dashboard shows a completed start rather than
        # inferring one from a line of log text.
        publish_success(__name__, f"Scheduler started — {len(self._jobs)} jobs registered")
        self._tasks["loop"] = asyncio.create_task(self._loop(db))

    async def stop(self) -> None:
        """Graceful shutdown. In-flight work is allowed to checkpoint."""
        self._running = False
        for name, task in list(self._tasks.items()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        logger.info("Scheduler stopped")

    # ------------------------------------------------------------------
    async def _loop(self, db: Optional[AsyncSession] = None) -> None:
        # The loop outlives the session passed to start(), which the caller closes
        # right away. Each tick therefore opens its own short-lived session.
        while self._running:
            try:
                if not emergency_stop.can_run_jobs():
                    logger.debug("Scheduler idle: emergency stop active.")
                else:
                    async with AsyncSessionLocal() as session:
                        await self._tick(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Scheduler tick failed: %s", exc)
            await asyncio.sleep(settings.SCHEDULER_HEARTBEAT_SECONDS)

    async def _tick(self, db: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        due = (
            await db.execute(
                select(AutomationSchedule).where(
                    AutomationSchedule.is_active.is_(True),
                    AutomationSchedule.next_run_at.is_not(None),
                    AutomationSchedule.next_run_at <= now,
                )
            )
        ).scalars().all()

        for schedule in due:
            handler = self._jobs.get(schedule.job_name)
            if handler is None:
                logger.warning("No handler registered for schedule '%s'", schedule.job_name)
            else:
                await self._run_job(db, schedule, handler)

            # Reschedule regardless of outcome so one failure does not kill the job.
            interval = schedule.interval_minutes or 60
            schedule.next_run_at = now + timedelta(minutes=interval)
            schedule.last_run_at = now
            schedule.total_runs += 1
            await db.commit()

    async def _run_job(self, db: AsyncSession, schedule: AutomationSchedule, handler: JobHandler) -> None:
        run = ScheduledRun(schedule_id=schedule.id, status="RUNNING")
        db.add(run)
        await db.commit()
        started = datetime.now(timezone.utc)

        try:
            result = await handler(db, schedule.payload or {})
            run.status = "SUCCESS"
            run.result_summary = result
        except asyncio.CancelledError:
            run.status = "CANCELLED"
            await db.commit()
            raise
        except Exception as exc:
            run.status = "FAILED"
            run.error_message = f"{type(exc).__name__}: {exc}"
            schedule.failure_count += 1
            logger.exception("Scheduled job '%s' failed", schedule.job_name)
        finally:
            run.duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            await db.commit()


# Default recurring jobs. Handlers are bound at startup in the worker.
DEFAULT_JOBS = [
    ("airdrop_discovery", "DISCOVERY", settings.DISCOVERY_INTERVAL_MINUTES),
    ("campaign_monitoring", "CAMPAIGN_MONITOR", settings.MONITORING_INTERVAL_MINUTES),
    ("claim_monitoring", "CLAIM_MONITOR", settings.CLAIM_CHECK_INTERVAL_MINUTES),
    ("receipt_monitoring", "TX_RECEIPT_CHECK", 10),
    ("deadline_alerts", "DEADLINE_ALERT", 30),
    ("rpc_health_check", "RPC_HEALTH", 15),
    ("watchdog", "WATCHDOG", 15),
    ("memory_maintenance", "MEMORY_MAINTENANCE", 240),
    ("learning_analysis", "LEARNING_ANALYSIS", 720),
]


scheduler = Scheduler()
