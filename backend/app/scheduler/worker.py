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
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import select

from backend.app.agent.controller import AgentController
from backend.app.core.config import settings
from backend.app.core.logging_setup import configure_logging
from backend.app.inbox.raise_intervention import park_for_intervention
from backend.app.inbox.service import OPERATOR_FALLBACK_EMAIL, UNKNOWN_REASON
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
from backend.app.terminal.events import publish_success
from backend.app.tools.bootstrap import bootstrap_tools
from backend.app.tools.registry import registry

configure_logging()
logger = logging.getLogger("airdropos.worker")

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


def retry_delay_seconds(retry_count: int) -> int:
    """Exponential backoff for the next attempt, bounded by RETRY_BACKOFF_MAX_SECONDS."""
    exponent = max(0, min(int(retry_count), 20))
    return min(RETRY_BACKOFF_BASE_SECONDS * (2 ** exponent), RETRY_BACKOFF_MAX_SECONDS)


async def _release_task_leases(db: AsyncSession, task_pk: Any, reason: str) -> None:
    """Release leases of a run that has ended, so the next attempt is not refused as a conflict.

    Leases are now committed while a task runs. If the run ends in an error the lease would
    otherwise stay live for its full TTL and every retry would be rejected with "another worker
    holds this task" while the task sat IN_PROGRESS.
    """
    from sqlalchemy import update

    from backend.app.models import ExecutionLease

    await db.execute(
        update(ExecutionLease)
        .where(ExecutionLease.task_id == task_pk, ExecutionLease.released_at.is_(None))
        .values(released_at=datetime.now(timezone.utc), broken_reason=reason)
    )


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
        await _release_task_leases(db, task_pk, f"execution ended: {reason}")
        if task.retry_count < task.max_retries:
            delay = retry_delay_seconds(task.retry_count)
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


_TELEGRAM_SWEEP_INTERVAL_SECONDS = 10.0
_last_telegram_sweep = 0.0


async def release_idle_telegram_sessions(*, force: bool = False) -> int:
    """Close the Telegram connections that no unfinished task needs any more.

    A task keeps its Telegram account open and locked until it is finished; when the last
    task bound to an account ends, this closes that account. The next task that names it
    reconnects automatically. Only connections a task opened are touched, and never one that
    is in the middle of an action. Returns how many connections were closed.
    """
    global _last_telegram_sweep
    now = time.monotonic()
    if not force and now - _last_telegram_sweep < _TELEGRAM_SWEEP_INTERVAL_SECONDS:
        return 0
    _last_telegram_sweep = now
    try:
        from backend.app.integrations import telegram_leases
        from backend.app.tools.telegram_user_tools import all_sessions

        candidates = [
            s for s in all_sessions()
            if s.bound_by_task and s.is_connected() and not s._lock.locked()
        ]
        if not candidates:
            return 0
        async with AsyncSessionLocal() as db:
            in_use = await telegram_leases.labels_in_use(db)
        closed = 0
        for sess in candidates:
            if sess.label.lower() in in_use:
                continue
            if await sess.disconnect_quietly():
                closed += 1
                sess.bound_by_task = False
                sess._emit("session.released", "No task needs this Telegram account any more; closed", "ok")
                logger.info("Telegram account '%s' closed: its tasks have finished", sess.label)
        return closed
    except Exception:
        logger.warning("Telegram idle-session sweep failed", exc_info=True)
        return 0


async def task_execution_loop(slot: int = 0) -> None:
    """Continuously claim and execute tasks. Several of these run side by side."""
    logger.info("Task execution loop %s started for %s", slot, WORKER_ID)
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
                    if not worker_state.running:
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
                run_started = time.perf_counter()
                logger.info(
                    "WORKER claimed task=%s worker=%s status=%s retry=%s/%s title=%s",
                    task.task_id, WORKER_ID, task.status, task.retry_count, task.max_retries,
                    (task.title or "")[:80],
                )
                worker_state.polled(f"running {task.task_id}")
                worker_state.task_claimed(task.task_id, task.title)
                # Tell this process's Telegram tools which account the task is bound to.
                from backend.app.integrations import telegram_leases

                telegram_leases.begin_run(task.task_id, task.telegram_account)

                # Bind the operator's authorization policy explicitly. The registry's
                # own policy is used as a fallback inside `invoke`, but passing it here
                # makes the controller's permission decisions explicit and identical to
                # the ones the catalog and the planner were built from.
                controller = AgentController(
                    db, registry=registry, policy=registry.policy
                )
                report = await controller.execute_task(task)
                await db.commit()

                logger.info(
                    "WORKER finished task=%s outcome=%s status=%s ms=%s steps=%s failures=%s blockers=%s",
                    task.task_id, report.outcome.value, task.status,
                    int((time.perf_counter() - run_started) * 1000), len(report.steps),
                    len(report.failures), len(report.blockers),
                )
                for failure in report.failures[:3]:
                    logger.warning("WORKER failure task=%s detail=%s", task.task_id, redact(str(failure))[:300])
                for blocker in report.blockers[:3]:
                    logger.warning("WORKER blocker task=%s detail=%s", task.task_id, redact(str(blocker))[:300])
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
            # The task row can vanish while this run is in flight (deleted from the dashboard
            # during a long LLM call). The next write then matches 0 rows, which is not a
            # failure of the task: there is nothing left to retry or settle.
            if current_task_pk is not None:
                try:
                    async with AsyncSessionLocal() as check_db:
                        still_exists = await check_db.get(Task, current_task_pk) is not None
                except Exception:
                    still_exists = True  # cannot tell: fall through to the normal failure path
                if not still_exists:
                    logger.warning(
                        "Task %s was deleted while it was running; run discarded.", current_task_id
                    )
                    worker_state.polled(f"discarded {current_task_id} (deleted while running)")
                    continue
            logger.exception(
                "WORKER crashed task=%s type=%s error=%s",
                current_task_id, type(exc).__name__, redact(str(exc))[:300],
            )
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
        finally:
            # The run is over (finished, failed, or discarded) and its status is settled:
            # stop routing Telegram calls for it, then close any account nobody needs now.
            if current_task_id:
                try:
                    from backend.app.integrations import telegram_leases

                    telegram_leases.end_run(current_task_id)
                except Exception:
                    pass
            await release_idle_telegram_sessions(force=bool(current_task_id))


# ----------------------------------------------------------------------
# Recovery
# ----------------------------------------------------------------------
# Step kinds whose side effects cannot be undone or safely repeated. A crash while one of these
# was running leaves the real-world outcome unknown (was the transaction broadcast? was the form
# submitted?), so recovery must never re-run it on its own.
_SIDE_EFFECT_KINDS = frozenset({"transact", "interact"})


async def _inspect_plan_for_recovery(db: AsyncSession, task_pk: Any):
    """Return (blocking_reason, read_only_running_step_indexes) for an interrupted task."""
    from backend.app.models import ExecutionPlan

    plan = await db.scalar(
        select(ExecutionPlan).where(
            ExecutionPlan.task_id == task_pk, ExecutionPlan.is_current.is_(True)
        )
    )
    blocking: list[str] = []
    resettable: list[int] = []
    for raw in (plan.steps if plan is not None else None) or []:
        kind, status = raw.get("kind"), raw.get("status")
        label = f"step {raw.get('index')} ({kind}): {raw.get('description', '')}"[:200]
        if kind == "transact" and status in ("running", "completed"):
            blocking.append(f"{label} was {status} when the worker stopped; verify on-chain state")
        elif kind in _SIDE_EFFECT_KINDS and status == "running":
            blocking.append(f"{label} was running when the worker stopped; verify whether it took effect")
        elif status == "running":
            resettable.append(int(raw.get("index", -1)))  # read-only: safe to run again
    reason = "; ".join(blocking) if blocking else None
    return reason, resettable


async def recover_interrupted_tasks() -> int:
    """Settle tasks left IN_PROGRESS by a crash. Returns how many were queued for retry.

    * A side-effecting step (transaction, web interaction) in flight, or an already completed
      transaction: park as NEEDS_USER_ACTION. Never auto-resumed, so nothing irreversible or
      uncertain is executed twice; an operator verifies the outcome first.
    * Otherwise the interruption counts as a retry (RETRYING) while the budget lasts, and FAILED
      once ``max_retries`` is spent, so a task that keeps crashing the worker cannot loop forever.
      Interrupted read-only steps are reset to pending so they can run again.

    Leases of the dead run are always released, completed progress is kept, and every change is
    recorded as a status-transition event.
    """
    from backend.app.planning.planner import Planner
    from backend.app.tools.registry import registry

    async with AsyncSessionLocal() as db:
        interrupted = list(
            (
                await db.execute(select(Task).where(Task.status == TaskStatus.IN_PROGRESS.value))
            ).scalars().all()
        )
        manager = TaskManager(db)
        # Bound to the real registry. A Planner built without one sees an empty tool
        # catalog, so any replan it produced would have every tool reference dropped as
        # "unregistered" and the recovered task would lose its tools.
        planner = Planner(db, registry)
        queued = parked = failed = 0
        for task in interrupted:
            # The dead run's committed lease would otherwise block the retry for its full TTL.
            await _release_task_leases(db, task.id, "worker restarted; run interrupted")
            blocking, resettable = await _inspect_plan_for_recovery(db, task.id)

            if blocking:
                details = dict(task.error_details or {})
                details["recovery"] = (
                    "Worker stopped mid-run with a side-effecting step in flight; parked so it is "
                    f"not executed again automatically. {blocking}"
                )[:900]
                task.error_details = details
                # Tell the operator in the task's own conversation instead of leaving them to
                # find this in the Task Manager and work out what happened. This also performs
                # the NEEDS_USER_ACTION transition and saves a checkpoint.
                await park_for_intervention(
                    db,
                    task,
                    owner_email=task.owner_email or OPERATOR_FALLBACK_EMAIL,
                    error_text=(
                        f"The worker restarted while a side-effecting step was in flight: {blocking}. "
                        "I stopped this task rather than repeating a step whose real-world outcome "
                        "I cannot confirm."
                    ),
                    error_category="HUMAN_INTERVENTION_REQUIRED",
                    blocking_step=blocking,
                    force_reason_code=UNKNOWN_REASON,
                )
                parked += 1
                continue

            for index in resettable:
                await planner.update_step_status(task, index, "pending")
            if task.retry_count < task.max_retries:
                await manager.transition(
                    task, TaskStatus.RETRYING, reason="worker restarted; run interrupted",
                    progress=task.progress,
                )
                queued += 1
            else:
                await manager.transition(
                    task, TaskStatus.FAILED,
                    reason="worker restarted; run interrupted (retries exhausted)",
                    progress=task.progress,
                )
                failed += 1
        await db.commit()
        if interrupted:
            logger.info(
                "Recovered interrupted tasks: %s queued for retry, %s parked for operator review, "
                "%s failed (retries exhausted)", queued, parked, failed,
            )
        return queued


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------
async def main() -> None:
    worker_state.attach_logging()
    logger.info("AirdropOS Worker %s starting", WORKER_ID)
    logger.info("Fixed model: %s at %s", settings.LLM_MODEL_ID, settings.LLM_BASE_URL)
    # The worker is its own process with its own (initially empty) tool registry, so
    # it must bootstrap them itself: without this, every planned step ran with no
    # tools. Logs one summary line with the count. The API process logs the same
    # thing for its own registry; neither prints the tool names.
    bootstrap_tools()
    worker_state.begin(WORKER_ID, list(registry._tools))
    # Published by the process that finished initialising, so the dashboard can
    # say "worker initialized" as a fact rather than guessing from a log line.
    publish_success("airdropos.worker", f"Worker initialized ({WORKER_ID})")
    if emergency_stop.is_stopped:
        logger.warning("EMERGENCY STOP IS ACTIVE. No new execution or submissions.")

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

    # One loop per execution slot, so tasks run at the same time (each on its own Telegram
    # account, or sharing one). claim_next_task enforces MAX_CONCURRENT_TASKS across them.
    slots = max(1, int(settings.MAX_CONCURRENT_TASKS))
    logger.info("Running up to %s tasks at the same time", slots)
    await asyncio.gather(*(task_execution_loop(i) for i in range(slots)))

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
