"""User-defined scheduled tasks.

A schedule is one ``AutomationSchedule`` row with ``job_type == "USER_TASK"``. Its
``payload`` is a template of the task to create each time the schedule fires. No new table
is needed: the persistent scheduler already stores, re-arms and records runs of these rows.

Three trigger types:

* ``INTERVAL`` - every N minutes (``interval_minutes``).
* ``CRON``     - a 5-field expression, evaluated in the schedule's timezone.
* ``ONCE``     - a single run at ``payload["run_at"]``; the schedule then deactivates itself.

Every fire creates a normal task and queues it (``READY``), exactly like ``POST /tasks``, so
the worker executes it through the same pipeline and it shows up in the Task Manager.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.integrations import telegram_leases
from backend.app.models import AutomationSchedule, Task
from backend.app.scheduler import cron
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TERMINAL_STATES, TaskStatus

logger = logging.getLogger(__name__)

JOB_TYPE = "USER_TASK"
TRIGGERS = ("INTERVAL", "CRON", "ONCE")

# A schedule that fires more often than this would flood the task queue.
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 60 * 24 * 365
MAX_USER_SCHEDULES = 100

_TERMINAL = {s.value for s in TERMINAL_STATES}


class ScheduleError(ValueError):
    """The schedule definition is not valid. The message is safe to show the operator."""


def _aware(value: datetime, tz) -> datetime:
    """A naive datetime is read as wall-clock time in ``tz``; an aware one is kept as is."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(timezone.utc)


def parse_run_at(raw: Any, tz) -> datetime:
    if isinstance(raw, datetime):
        return _aware(raw, tz)
    if not isinstance(raw, str) or not raw.strip():
        raise ScheduleError("run_at is required for a one-time schedule")
    try:
        return _aware(datetime.fromisoformat(raw.strip().replace("Z", "+00:00")), tz)
    except ValueError as exc:
        raise ScheduleError("run_at must be an ISO date-time, e.g. 2026-11-01T09:00") from exc


def compute_next_run(
    trigger_type: str,
    *,
    after: datetime,
    interval_minutes: Optional[int] = None,
    cron_expression: Optional[str] = None,
    timezone_name: Optional[str] = None,
    run_at: Any = None,
) -> Optional[datetime]:
    """The next fire time (UTC) after ``after``, or None when there is none (a past ONCE)."""
    trigger_type = (trigger_type or "").upper()
    try:
        tz = cron.get_timezone(timezone_name)
        if trigger_type == "INTERVAL":
            if not interval_minutes:
                raise ScheduleError("interval_minutes is required for an interval schedule")
            return after + timedelta(minutes=int(interval_minutes))
        if trigger_type == "CRON":
            return cron.next_after(cron.parse(cron_expression or ""), after, tz)
        if trigger_type == "ONCE":
            when = parse_run_at(run_at, tz)
            return when if when > after else None
    except cron.CronError as exc:
        raise ScheduleError(str(exc)) from exc
    raise ScheduleError(f"Unknown trigger type '{trigger_type}'")


def validate_trigger(
    trigger_type: str,
    *,
    interval_minutes: Optional[int],
    cron_expression: Optional[str],
    timezone_name: Optional[str],
    run_at: Any,
    now: Optional[datetime] = None,
) -> datetime:
    """Validates a trigger and returns its first fire time. Raises ScheduleError."""
    now = now or datetime.now(timezone.utc)
    trigger_type = (trigger_type or "").upper()
    if trigger_type not in TRIGGERS:
        raise ScheduleError(f"trigger_type must be one of {', '.join(TRIGGERS)}")

    if trigger_type == "INTERVAL":
        if interval_minutes is None or interval_minutes < MIN_INTERVAL_MINUTES:
            raise ScheduleError(f"The interval must be at least {MIN_INTERVAL_MINUTES} minutes")
        if interval_minutes > MAX_INTERVAL_MINUTES:
            raise ScheduleError("The interval is too long (one year at most)")
    elif trigger_type == "CRON":
        try:
            spec = cron.parse(cron_expression or "")
            tz = cron.get_timezone(timezone_name)
            # The smallest gap among the next runs must respect the minimum interval,
            # so "* * * * *" and "0,1 * * * *" are rejected rather than flooding the queue.
            runs = cron.upcoming(spec.expression, now, tz, count=12)
        except cron.CronError as exc:
            raise ScheduleError(str(exc)) from exc
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(runs, runs[1:])]
        if gaps and min(gaps) < MIN_INTERVAL_MINUTES:
            raise ScheduleError(
                f"That expression can fire less than {MIN_INTERVAL_MINUTES} minutes apart; "
                "choose a less frequent one"
            )
    else:
        cron.get_timezone(timezone_name)  # raises CronError for a bad zone
        if parse_run_at(run_at, cron.get_timezone(timezone_name)) <= now:
            raise ScheduleError("run_at must be in the future")

    first = compute_next_run(
        trigger_type,
        after=now,
        interval_minutes=interval_minutes,
        cron_expression=cron_expression,
        timezone_name=timezone_name,
        run_at=run_at,
    )
    if first is None:
        raise ScheduleError("This schedule has no upcoming run")
    return first


def preview(
    trigger_type: str,
    *,
    interval_minutes: Optional[int],
    cron_expression: Optional[str],
    timezone_name: Optional[str],
    run_at: Any,
    count: int = 5,
    now: Optional[datetime] = None,
) -> List[datetime]:
    """The next ``count`` fire times, validating the trigger first."""
    now = now or datetime.now(timezone.utc)
    first = validate_trigger(
        trigger_type,
        interval_minutes=interval_minutes,
        cron_expression=cron_expression,
        timezone_name=timezone_name,
        run_at=run_at,
        now=now,
    )
    out = [first]
    if trigger_type.upper() == "ONCE":
        return out
    while len(out) < count:
        nxt = compute_next_run(
            trigger_type,
            after=out[-1],
            interval_minutes=interval_minutes,
            cron_expression=cron_expression,
            timezone_name=timezone_name,
            run_at=run_at,
        )
        if nxt is None:
            break
        out.append(nxt)
    return out


# ----------------------------------------------------------------------
# Running one schedule
# ----------------------------------------------------------------------
async def _previous_run_still_active(db: AsyncSession, payload: Dict[str, Any]) -> Optional[str]:
    last = payload.get("last_task_id")
    if not last:
        return None
    status = await db.scalar(select(Task.status).where(Task.task_id == last))
    if status is not None and status not in _TERMINAL:
        return str(last)
    return None


async def create_task_from_schedule(
    db: AsyncSession, schedule: AutomationSchedule, *, manual: bool = False
) -> Dict[str, Any]:
    """Creates and queues one task from the schedule's template.

    Returns a summary stored on the ``ScheduledRun``. Does NOT commit: the caller owns the
    transaction, so the new task and the run record land together.
    """
    payload = dict(schedule.payload or {})

    if payload.get("skip_if_running", True) and not manual:
        active = await _previous_run_still_active(db, payload)
        if active:
            return {
                "skipped": True,
                "reason": f"The previous run ({active}) has not finished yet",
                "previous_task_id": active,
            }

    manager = TaskManager(db)
    # A savepoint, so a failure half-way through never leaves an un-queued task behind.
    async with db.begin_nested():
        task = await manager.create_task(
            title=payload.get("title") or schedule.job_name,
            description=payload.get("description") or "",
            category=payload.get("category"),
            network=payload.get("network"),
            priority=int(payload.get("priority") or 5),
            owner_email=payload.get("owner_email"),
            telegram_account=payload.get("telegram_account"),
        )
        await manager.transition(
            task,
            TaskStatus.READY,
            reason=f"Created by schedule '{schedule.job_name}'" + (" (run now)" if manual else ""),
        )

    payload["last_task_id"] = task.task_id
    schedule.payload = payload  # reassign so the JSONB change is detected
    return {"task_id": task.task_id, "status": task.status, "manual": manual}


async def run_schedule(db: AsyncSession, schedule: AutomationSchedule) -> Dict[str, Any]:
    """Scheduler entry point for ``USER_TASK`` rows."""
    return await create_task_from_schedule(db, schedule)


def normalize_telegram_account(label: Optional[str]) -> Optional[str]:
    try:
        return telegram_leases.normalize_label(label)
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc
