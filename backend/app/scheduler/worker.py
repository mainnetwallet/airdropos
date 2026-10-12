"""24/7 Background Worker Daemon.

Runs the scheduler, claims tasks, executes them through the agent controller,
recovers interrupted work after restart, and shuts down gracefully.

Run with: python -m backend.app.scheduler.worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import select

from backend.app.agent.controller import AgentController
from backend.app.core.config import settings
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database.session import AsyncSessionLocal, engine
from backend.app.models import IntegrationHealth, Task
from backend.app.notifications.dispatcher import Severity, dispatcher
from backend.app.scheduler.scheduler import DEFAULT_JOBS, scheduler
from backend.app.browser.controller import controller as browser_controller
from backend.app.live import gateway as live_gateway
from backend.app.scheduler.worker_status import heartbeat_loop, redact, state as worker_state
from backend.app.security.emergency_stop import emergency_stop
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus
from backend.app.tools.bootstrap import bootstrap_tools
from backend.app.tools.registry import registry

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("airdropos.worker")
# httpx logs every request at INFO; the live-event poll runs every second and floods the terminal.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

WORKER_ID = f"worker-{os.getpid()}"
_shutdown_event = asyncio.Event()


# ----------------------------------------------------------------------
# Job handlers
# ----------------------------------------------------------------------
async def handle_airdrop_discovery(db: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run airdrop discovery using the discovery workflow."""
    from backend.app.airdrops.discovery import run_discovery_cycle

    # Run discovery with default sources (in production, these would come from config)
    result = await run_discovery_cycle()

    # Record health for discovery subsystem
    await _record_health(
        db,
        "discovery",
        "HEALTHY" if result.get("status") == "completed" else "DEGRADED",
        {
            "campaigns_discovered": result.get("campaigns_discovered", 0),
            "duration_seconds": result.get("duration_seconds", 0),
            "timestamp": result.get("timestamp")
        }
    )

    return {
        "status": "ok",
        "note": f"Discovery executed: {result.get('campaigns_discovered', 0)} campaigns found",
        "campaigns_discovered": result.get("campaigns_discovered", 0),
        "at": datetime.now(timezone.utc).isoformat()
    }


async def handle_rpc_health(db: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Probe every catalogued RPC and persist subsystem health separately.

    Delegates to the chain adapter so the network list and the probe logic live in
    one place, and sweeps the chains concurrently rather than serially.
    """
    from backend.app.blockchain.chains import registry as chain_registry

    results = await chain_registry.health_sweep()

    for network, detail in results.items():
        await _record_health(db, f"rpc:{network}", detail.get("status", "UNKNOWN"), detail)
    return results


async def handle_watchdog(db: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Probe every subsystem and persist the outcome."""
    from backend.app.diagnostics.watchdog import watchdog

    report = await watchdog.sweep(db)
    await _record_health(
        db,
        "watchdog",
        report.overall,
        {"failing": [r.subsystem for r in report.failing()]},
    )
    return report.to_dict()


async def handle_memory_maintenance(db: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Consolidate memories and extend TTL on entries that still hold."""
    from backend.app.memory.manager import MemoryManager

    manager = MemoryManager(db)
    consolidated = await manager.consolidate()
    stats = await manager.stats()
    return {"status": "ok", "consolidated": consolidated, "stats": stats}


async def handle_learning_analysis(db: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run the learning engine over recorded outcomes."""
    from backend.app.learning.engine import LearningEngine

    result = await LearningEngine(db).run_full_analysis()
    return {"status": "ok", **result}


async def handle_campaign_monitoring(db: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Re-check tracked campaigns and flag stale or expiring ones."""
    from backend.app.models import AirdropCampaign

    now = datetime.now(timezone.utc)
    tracked = list(
        (
            await db.execute(
                select(AirdropCampaign).where(
                    AirdropCampaign.lifecycle_status.notin_(["EXPIRED", "CANCELLED"])
                )
            )
        )
        .scalars()
        .all()
    )

    expiring_soon, stale = [], []
    for campaign in tracked:
        deadline = campaign.claim_deadline
        if deadline is not None:
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            remaining = (deadline - now).total_seconds()
            if remaining < 0:
                expiring_soon.append({"id": str(campaign.id), "seconds_remaining": int(remaining)})
            elif remaining < 86400:
                expiring_soon.append({"id": str(campaign.id), "seconds_remaining": int(remaining)})

        last = campaign.last_checked_at
        if last is None:
            stale.append(str(campaign.id))
        else:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() > 86400:
                stale.append(str(campaign.id))

    if expiring_soon:
        await dispatcher.notify(
            f"{len(expiring_soon)} campaign claim deadline(s) approaching or passed",
            ", ".join(str(c["seconds_remaining"]) + "s" for c in expiring_soon[:5]),
            Severity.WARNING,
            dedupe_key=f"deadlines-{now.strftime('%Y%m%d%H')}",
        )

    return {
        "status": "ok",
        "campaigns_tracked": len(tracked),
        "expiring_within_24h": len(expiring_soon),
        "stale_not_checked_24h": len(stale),
    }


async def handle_claim_monitoring(db: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Advance pending claims by reading their on-chain receipts."""
    from backend.app.airdrops.claims import ClaimLifecycleService
    from backend.app.models import ClaimRecord

    service = ClaimLifecycleService(db)
    pending = list(
        (
            await db.execute(
                select(ClaimRecord).where(
                    ClaimRecord.claim_status.in_(["PENDING", "SUBMITTED"])
                )
            )
        )
        .scalars()
        .all()
    )

    outcomes: Dict[str, str] = {}
    for claim in pending:
        try:
            outcomes[str(claim.id)] = await service.refresh(claim)
        except Exception as exc:
            outcomes[str(claim.id)] = f"ERROR: {type(exc).__name__}"
            logger.warning("Claim %s refresh failed: %s", claim.id, exc)

    return {"status": "ok", "claims_checked": len(pending), "outcomes": outcomes}


async def handle_receipt_monitoring(db: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Advance submitted transactions toward CONFIRMED as receipts arrive."""
    from backend.app.tasks.concurrency import LeaseManager

    # Reconciliation only observes chain state; it never resubmits.
    result = await LeaseManager(db).reconcile_transactions()
    await db.commit()
    return {"status": "ok", **result}


async def handle_deadline_alerts(db: AsyncSession, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Notify about snapshot and claim deadlines inside the alert window."""
    from backend.app.models import AirdropCampaign

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=48)

    def _aware(value):
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

    upcoming = list(
        (
            await db.execute(
                select(AirdropCampaign).where(
                    AirdropCampaign.claim_deadline.isnot(None),
                    AirdropCampaign.claim_deadline >= now,
                    AirdropCampaign.claim_deadline <= horizon,
                )
            )
        )
        .scalars()
        .all()
    )

    for campaign in upcoming:
        deadline = _aware(campaign.claim_deadline)
        hours = (deadline - now).total_seconds() / 3600
        await dispatcher.notify(
            f"Claim deadline: {campaign.title}",
            f"{hours:.1f}h remaining. Claim contract: {campaign.requirements or 'n/a'}",
            Severity.INFO,
            dedupe_key=f"deadline-{campaign.id}-{deadline.isoformat()}",
        )

    return {"status": "ok", "deadlines_in_48h": len(upcoming)}


# Job type -> handler. Previously this was a ternary that routed every
# non-RPC job to discovery, so memory maintenance, learning analysis, and every
# monitor silently re-ran discovery instead of their own work.
JOB_HANDLERS: Dict[str, Any] = {
    "DISCOVERY": handle_airdrop_discovery,
    "RPC_HEALTH": handle_rpc_health,
    "WATCHDOG": handle_watchdog,
    "MEMORY_MAINTENANCE": handle_memory_maintenance,
    "LEARNING_ANALYSIS": handle_learning_analysis,
    "CAMPAIGN_MONITOR": handle_campaign_monitoring,
    "CLAIM_MONITOR": handle_claim_monitoring,
    "TX_RECEIPT_CHECK": handle_receipt_monitoring,
    "DEADLINE_ALERT": handle_deadline_alerts,
}


async def _record_health(db: AsyncSession, subsystem: str, status: str, detail: Dict[str, Any]) -> None:
    row = await db.scalar(select(IntegrationHealth).where(IntegrationHealth.subsystem == subsystem))
    if row is None:
        db.add(
            IntegrationHealth(
                subsystem=subsystem,
                status=status,
                detail=detail,
                last_checked_at=datetime.now(timezone.utc),
                success_count=1 if status == "HEALTHY" else 0,
                failure_count=0 if status == "HEALTHY" else 1,
            )
        )
    else:
        row.status = status
        row.detail = detail
        row.last_checked_at = datetime.now(timezone.utc)
        if status == "HEALTHY":
            row.success_count += 1
        else:
            row.failure_count += 1
    await db.commit()


# ----------------------------------------------------------------------
# Task execution loop
# ----------------------------------------------------------------------
RETRY_BACKOFF_BASE_SECONDS = 30
RETRY_BACKOFF_MAX_SECONDS = 600


async def _settle_failed_execution(task_pk: Any, reason: str, detail: Dict[str, Any]) -> None:
    """Record a failed execution attempt on the task, in a fresh session.

    Without this, a failed run left the task IN_PROGRESS (or, when the exception rolled the
    session back, silently put it back to READY with no retry counted, no event and no error), so
    the worker re-claimed it every few seconds forever and the dashboard showed a READY task that
    never progressed. The retry budget (``max_retries``) is honoured; once spent the task FAILS.
    Only a task still IN_PROGRESS is touched, so a status set by the run itself is preserved.
    """
    async with AsyncSessionLocal() as db:
        task = await db.get(Task, task_pk)
        if task is None or task.status != TaskStatus.IN_PROGRESS.value:
            return
        manager = TaskManager(db)
        if task.retry_count < task.max_retries:
            delay = min(RETRY_BACKOFF_BASE_SECONDS * (2 ** task.retry_count), RETRY_BACKOFF_MAX_SECONDS)
            task.next_execution_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            await manager.transition(
                task, TaskStatus.RETRYING, reason=reason, error_details=detail
            )
        else:
            await manager.transition(
                task, TaskStatus.FAILED, reason=f"{reason} (retries exhausted)", error_details=detail
            )
        await db.commit()
        logger.warning("Task %s -> %s after failed execution: %s", task.task_id, task.status, reason)


async def task_execution_loop() -> None:
    """Continuously claim and execute tasks."""
    logger.info("Task execution loop started for %s", WORKER_ID)
    while not _shutdown_event.is_set():
        current_task_id = None
        current_task_pk = None
        try:
            if emergency_stop.is_stopped and not emergency_stop.can_execute_tasks():
                worker_state.polled("paused: emergency stop is active")
                await asyncio.sleep(5)
                continue

            async with AsyncSessionLocal() as db:
                manager = TaskManager(db)
                task = await manager.claim_next_task(WORKER_ID)

                if task is None:
                    worker_state.polled("idle: no runnable task")
                    await asyncio.sleep(3)
                    continue

                # Persist the claim NOW. Until this commit the claim only exists in this open
                # transaction: on SQLite it holds the database write lock for the whole run
                # (blocking the heartbeat and every API write), and any later exception rolls the
                # claim back so the task silently reverts to READY.
                await db.commit()

                current_task_id = task.task_id
                current_task_pk = task.id
                worker_state.polled(f"running {task.task_id}")
                worker_state.task_claimed(task.task_id, task.title)

                controller = AgentController(db, registry=registry)
                report = await controller.execute_task(task)
                await db.commit()

                logger.info("Task %s finished: %s", task.task_id, report.outcome.value)
                worker_state.task_finished(task.task_id, report.outcome.value)

                if report.outcome.value == "failed" and task.status == TaskStatus.IN_PROGRESS.value:
                    await _settle_failed_execution(
                        current_task_pk,
                        "execution failed",
                        {"failures": [redact(str(f))[:300] for f in report.failures[:5]]},
                    )

                if report.outcome.value == "needs_user_action" and report.blockers:
                    await dispatcher.notify(
                        f"Task {task.task_id} needs your action",
                        "\n".join(report.blockers[:3]),
                        Severity.WARNING,
                        task_id=task.task_id,
                    )

        except asyncio.CancelledError:
            logger.info("Task loop cancelled for %s", WORKER_ID)
            raise
        except Exception as exc:
            logger.exception("Task execution error: %s", exc)
            worker_state.failed(exc, current_task_id)
            if current_task_pk is not None:
                # The session above was rolled back; record the failure in a new one so the
                # attempt is counted instead of vanishing. Never let this mask the original error.
                try:
                    await _settle_failed_execution(
                        current_task_pk,
                        f"execution raised {type(exc).__name__}",
                        {"type": type(exc).__name__, "message": redact(str(exc))[:400]},
                    )
                except Exception:
                    logger.exception("Could not record failure for task %s", current_task_id)
            await asyncio.sleep(5)


# ----------------------------------------------------------------------
# Recovery
# ----------------------------------------------------------------------
async def recover_interrupted_tasks() -> int:
    """Reset tasks stuck IN_PROGRESS after a crash so they can be resumed."""
    from sqlalchemy import update

    from backend.app.models import Task

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Task)
            .where(Task.status == TaskStatus.IN_PROGRESS.value)
            .values(status=TaskStatus.RETRYING.value)
        )
        count = result.rowcount or 0
        await db.commit()
        if count:
            logger.info("Recovered %s interrupted task(s) for retry", count)
        return count


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------
async def main() -> None:
    # The worker is its own process with its own (initially empty) tool registry.
    # Without this, every planned step ran with no tools.
    worker_state.attach_logging()
    bootstrap_tools()
    worker_state.begin(WORKER_ID, list(registry._tools))
    logger.info("=" * 70)
    logger.info("AirdropOS Worker %s starting", WORKER_ID)
    logger.info("Fixed model: %s at %s", settings.LLM_MODEL_ID, settings.LLM_BASE_URL)
    logger.info("Registered tools: %s", len(registry._tools))
    if emergency_stop.is_stopped:
        logger.warning("EMERGENCY STOP IS ACTIVE. No new execution or submissions.")
    logger.info("=" * 70)

    await recover_interrupted_tasks()

    for job_name, job_type, interval in DEFAULT_JOBS:
        handler = JOB_HANDLERS.get(job_type)
        if handler is None:
            logger.error("No handler for job type '%s' (%s); it will not run.", job_type, job_name)
            continue
        scheduler.register_job(job_name, job_type, interval, handler)

    async with AsyncSessionLocal() as db:
        for job_name, job_type, interval in DEFAULT_JOBS:
            await scheduler.ensure_schedule(db, job_name, job_type, interval)
        await db.commit()
        await scheduler.start(db)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown_event.set)
        except (NotImplementedError, AttributeError):
            pass  # Windows does not support add_signal_handler.

    # Heartbeat first, so the dashboard shows the worker even while a task runs.
    heartbeat = asyncio.create_task(heartbeat_loop(_shutdown_event))

    # Live Broadcast gateway: the only way the API can show or control the browsers
    # and Telegram session that live in THIS process.
    gateway_task = asyncio.create_task(live_gateway.serve(_shutdown_event))

    await asyncio.create_task(task_execution_loop())

    await _shutdown_event.wait()
    logger.info("Shutdown signal received. Preserving state.")

    heartbeat.cancel()
    try:
        await asyncio.wait_for(gateway_task, timeout=6)
    except Exception:
        gateway_task.cancel()
    try:  # keep browser cookies of open sessions, then close the browsers cleanly
        for key in list(browser_controller.open_keys()):
            await browser_controller.save_state(key)
        await browser_controller.stop()
    except Exception:
        logger.exception("Browser shutdown failed")
    await scheduler.stop()
    try:
        await engine.dispose()
    except Exception:
        pass
    logger.info("Worker %s stopped cleanly", WORKER_ID)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
