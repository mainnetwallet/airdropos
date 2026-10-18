"""Scheduled Tasks API (v1).

Lets the operator schedule a task to be created and queued automatically: every N minutes,
on a cron expression, or once at a chosen time. Schedules are ``AutomationSchedule`` rows
with ``job_type == "USER_TASK"`` and are fired by the worker's persistent scheduler, so this
API only edits rows. System jobs (discovery, watchdog, ...) are never listed or changed here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database.session import get_db
from backend.app.models import AutomationSchedule, ScheduledRun
from backend.app.scheduler import user_tasks
from backend.app.scheduler.user_tasks import JOB_TYPE, ScheduleError
from backend.app.security.auth import (
    Principal,
    enforce_rate_limit,
    require_read,
    require_task_write,
    write_audit,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scheduled-tasks", tags=["scheduler"])

TriggerType = Literal["INTERVAL", "CRON", "ONCE"]


# ======================================================================
# Schemas
# ======================================================================
class _TriggerFields(BaseModel):
    trigger_type: TriggerType = "CRON"
    interval_minutes: Optional[int] = Field(default=None, ge=1)
    cron_expression: Optional[str] = Field(default=None, max_length=100)
    # ISO date-time. Without an offset it is read in `timezone`.
    run_at: Optional[str] = Field(default=None, max_length=64)
    timezone: str = Field(default="UTC", max_length=64)


class CreateScheduleRequest(_TriggerFields):
    name: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=6000)
    category: Optional[str] = Field(default=None, max_length=100)
    network: Optional[str] = Field(default=None, max_length=100)
    priority: int = Field(default=5, ge=1, le=10)
    telegram_account: Optional[str] = Field(default=None, max_length=40)
    skip_if_running: bool = True
    is_active: bool = True


class UpdateScheduleRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=6000)
    category: Optional[str] = Field(default=None, max_length=100)
    network: Optional[str] = Field(default=None, max_length=100)
    priority: Optional[int] = Field(default=None, ge=1, le=10)
    telegram_account: Optional[str] = Field(default=None, max_length=40)
    skip_if_running: Optional[bool] = None
    trigger_type: Optional[TriggerType] = None
    interval_minutes: Optional[int] = Field(default=None, ge=1)
    cron_expression: Optional[str] = Field(default=None, max_length=100)
    run_at: Optional[str] = Field(default=None, max_length=64)
    timezone: Optional[str] = Field(default=None, max_length=64)


class PreviewRequest(_TriggerFields):
    count: int = Field(default=5, ge=1, le=10)


# ======================================================================
# Helpers
# ======================================================================
def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"error": message})


async def _load(db: AsyncSession, schedule_id: str) -> AutomationSchedule:
    """A user schedule by id. System jobs are reported as not found on purpose."""
    try:
        key = uuid.UUID(schedule_id)
    except ValueError:
        raise HTTPException(status_code=404, detail={"error": "Schedule not found"})
    row = await db.scalar(
        select(AutomationSchedule).where(
            AutomationSchedule.id == key, AutomationSchedule.job_type == JOB_TYPE
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "Schedule not found"})
    return row


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite returns naive values; everything stored is UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _view(row: AutomationSchedule, last_run: Optional[ScheduledRun] = None) -> Dict[str, Any]:
    payload = row.payload or {}
    return {
        "id": str(row.id),
        "name": payload.get("name") or row.job_name,
        "title": payload.get("title") or "",
        "description": payload.get("description") or "",
        "category": payload.get("category"),
        "network": payload.get("network"),
        "priority": payload.get("priority") or 5,
        "telegram_account": payload.get("telegram_account"),
        "skip_if_running": payload.get("skip_if_running", True),
        "trigger_type": row.trigger_type,
        "interval_minutes": row.interval_minutes,
        "cron_expression": row.cron_expression,
        "run_at": payload.get("run_at"),
        "timezone": payload.get("timezone") or "UTC",
        "is_active": row.is_active,
        "next_run_at": _iso(row.next_run_at),
        "last_run_at": _iso(row.last_run_at),
        "total_runs": row.total_runs,
        "failure_count": row.failure_count,
        "last_task_id": payload.get("last_task_id"),
        "last_run_status": last_run.status if last_run else None,
        "last_run_error": last_run.error_message if last_run else None,
        "created_at": _iso(row.created_at),
    }


def _run_view(run: ScheduledRun) -> Dict[str, Any]:
    summary = run.result_summary or {}
    return {
        "id": str(run.id),
        "status": run.status,
        "task_id": summary.get("task_id"),
        "skipped_reason": summary.get("reason") if summary.get("skipped") else None,
        "manual": bool(summary.get("manual")),
        "error": run.error_message,
        "duration_ms": run.duration_ms,
        "started_at": _iso(run.created_at),
    }


async def _latest_runs(db: AsyncSession, ids: List[uuid.UUID]) -> Dict[uuid.UUID, ScheduledRun]:
    if not ids:
        return {}
    newest = (
        select(ScheduledRun.schedule_id, func.max(ScheduledRun.created_at).label("at"))
        .where(ScheduledRun.schedule_id.in_(ids))
        .group_by(ScheduledRun.schedule_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(ScheduledRun).join(
                newest,
                (ScheduledRun.schedule_id == newest.c.schedule_id)
                & (ScheduledRun.created_at == newest.c.at),
            )
        )
    ).scalars().all()
    return {r.schedule_id: r for r in rows}


def _template_from(body: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merges the editable task template into a payload, keeping internal bookkeeping."""
    payload = dict(existing or {})
    for key in ("name", "title", "description", "category", "network", "priority",
                "telegram_account", "skip_if_running", "timezone", "run_at"):
        if key in body:
            payload[key] = body[key]
    return payload


# ======================================================================
# Endpoints
# ======================================================================
@router.get("")
async def list_schedules(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "read")
    rows = list(
        (
            await db.execute(
                select(AutomationSchedule)
                .where(AutomationSchedule.job_type == JOB_TYPE)
                .order_by(AutomationSchedule.created_at.desc())
            )
        ).scalars().all()
    )
    latest = await _latest_runs(db, [r.id for r in rows])
    return {
        "count": len(rows),
        "limits": {
            "min_interval_minutes": user_tasks.MIN_INTERVAL_MINUTES,
            "max_schedules": user_tasks.MAX_USER_SCHEDULES,
        },
        "schedules": [_view(r, latest.get(r.id)) for r in rows],
    }


@router.post("/preview")
async def preview_schedule(
    body: PreviewRequest,
    request: Request,
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """The next run times for a trigger, so the form can show them before anything is saved."""
    enforce_rate_limit(request, "read")
    try:
        runs = user_tasks.preview(
            body.trigger_type,
            interval_minutes=body.interval_minutes,
            cron_expression=body.cron_expression,
            timezone_name=body.timezone,
            run_at=body.run_at,
            count=body.count,
        )
    except ScheduleError as exc:
        raise _unprocessable(str(exc))
    return {"next_runs": [_iso(r) for r in runs]}


@router.post("", status_code=201)
async def create_schedule(
    body: CreateScheduleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    total = await db.scalar(
        select(func.count(AutomationSchedule.id)).where(AutomationSchedule.job_type == JOB_TYPE)
    )
    if (total or 0) >= user_tasks.MAX_USER_SCHEDULES:
        raise _unprocessable(f"At most {user_tasks.MAX_USER_SCHEDULES} schedules are allowed")

    try:
        first = user_tasks.validate_trigger(
            body.trigger_type,
            interval_minutes=body.interval_minutes,
            cron_expression=body.cron_expression,
            timezone_name=body.timezone,
            run_at=body.run_at,
        )
        account = user_tasks.normalize_telegram_account(body.telegram_account)
    except ScheduleError as exc:
        raise _unprocessable(str(exc))

    payload = _template_from(
        {
            "name": body.name.strip(),
            "title": body.title.strip(),
            "description": body.description,
            "category": body.category,
            "network": body.network,
            "priority": body.priority,
            "telegram_account": account,
            "skip_if_running": body.skip_if_running,
            "timezone": body.timezone,
            "run_at": body.run_at if body.trigger_type == "ONCE" else None,
        }
    )
    # Which operator the created tasks belong to; it scopes their Agent Inbox conversation.
    payload["owner_email"] = principal.email

    row = AutomationSchedule(
        # job_name is globally unique and also names system jobs, so user rows get their own
        # namespace and can never collide with (or hijack) a built-in job.
        job_name=f"user_task:{uuid.uuid4().hex[:16]}",
        job_type=JOB_TYPE,
        trigger_type=body.trigger_type,
        interval_minutes=body.interval_minutes if body.trigger_type == "INTERVAL" else None,
        cron_expression=body.cron_expression.strip() if body.trigger_type == "CRON" and body.cron_expression else None,
        payload=payload,
        is_active=body.is_active,
        next_run_at=first if body.is_active else None,
    )
    db.add(row)
    await db.flush()
    await write_audit(
        db, principal, "SCHEDULE_CREATE", "schedule", str(row.id),
        after_state={"name": body.name, "trigger_type": body.trigger_type},
    )
    await db.commit()
    return _view(row)


@router.get("/{schedule_id}")
async def get_schedule(
    schedule_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "read")
    row = await _load(db, schedule_id)
    latest = await _latest_runs(db, [row.id])
    return _view(row, latest.get(row.id))


@router.patch("/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    body: UpdateScheduleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    row = await _load(db, schedule_id)
    changes = body.model_dump(exclude_unset=True)
    payload = dict(row.payload or {})

    trigger = changes.get("trigger_type", row.trigger_type)
    interval = changes["interval_minutes"] if "interval_minutes" in changes else row.interval_minutes
    expression = changes["cron_expression"] if "cron_expression" in changes else row.cron_expression
    tz_name = changes.get("timezone") or payload.get("timezone") or "UTC"
    run_at = changes["run_at"] if "run_at" in changes else payload.get("run_at")

    try:
        first = user_tasks.validate_trigger(
            trigger,
            interval_minutes=interval,
            cron_expression=expression,
            timezone_name=tz_name,
            run_at=run_at,
        )
        if "telegram_account" in changes:
            changes["telegram_account"] = user_tasks.normalize_telegram_account(changes["telegram_account"])
    except ScheduleError as exc:
        raise _unprocessable(str(exc))

    if "title" in changes:
        changes["title"] = changes["title"].strip()
    if "name" in changes:
        changes["name"] = changes["name"].strip()

    before = _view(row)
    row.payload = _template_from({**changes, "timezone": tz_name,
                                  "run_at": run_at if trigger == "ONCE" else None}, payload)
    row.trigger_type = trigger
    row.interval_minutes = interval if trigger == "INTERVAL" else None
    row.cron_expression = expression.strip() if trigger == "CRON" and expression else None
    # A changed trigger re-arms the schedule from now; an untouched one keeps its place.
    trigger_changed = any(k in changes for k in
                          ("trigger_type", "interval_minutes", "cron_expression", "run_at", "timezone"))
    if row.is_active and trigger_changed:
        row.next_run_at = first

    await write_audit(db, principal, "SCHEDULE_UPDATE", "schedule", str(row.id),
                      before_state={"trigger_type": before["trigger_type"]},
                      after_state={"trigger_type": row.trigger_type})
    await db.commit()
    return _view(row)


@router.post("/{schedule_id}/pause")
async def pause_schedule(
    schedule_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    row = await _load(db, schedule_id)
    row.is_active = False
    row.next_run_at = None
    await write_audit(db, principal, "SCHEDULE_PAUSE", "schedule", str(row.id))
    await db.commit()
    return _view(row)


@router.post("/{schedule_id}/resume")
async def resume_schedule(
    schedule_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    row = await _load(db, schedule_id)
    payload = row.payload or {}
    try:
        # Re-validated from now: a one-time schedule whose moment has passed cannot resume,
        # and a cron/interval schedule resumes at its NEXT slot, never with a burst of
        # runs missed while it was paused.
        row.next_run_at = user_tasks.validate_trigger(
            row.trigger_type,
            interval_minutes=row.interval_minutes,
            cron_expression=row.cron_expression,
            timezone_name=payload.get("timezone"),
            run_at=payload.get("run_at"),
        )
    except ScheduleError as exc:
        raise _unprocessable(str(exc))
    row.is_active = True
    await write_audit(db, principal, "SCHEDULE_RESUME", "schedule", str(row.id))
    await db.commit()
    return _view(row)


@router.post("/{schedule_id}/run-now")
async def run_schedule_now(
    schedule_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Creates and queues one task from the template immediately, without moving the schedule."""
    enforce_rate_limit(request, "write")
    row = await _load(db, schedule_id)
    started = datetime.now(timezone.utc)
    run = ScheduledRun(schedule_id=row.id, status="RUNNING")
    try:
        summary = await user_tasks.create_task_from_schedule(db, row, manual=True)
    except Exception as exc:
        logger.exception("Run-now of schedule %s failed", row.id)
        raise HTTPException(
            status_code=500,
            detail={"error": f"Could not create the task: {type(exc).__name__}: {exc}"},
        )
    run.status = "SUCCESS"
    run.result_summary = summary
    run.duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    db.add(run)
    row.last_run_at = started
    row.total_runs += 1
    await write_audit(db, principal, "SCHEDULE_RUN_NOW", "schedule", str(row.id),
                      after_state={"task_id": summary.get("task_id")})
    await db.commit()
    return {"task_id": summary.get("task_id"), "status": summary.get("status"), "schedule": _view(row, run)}


@router.get("/{schedule_id}/runs")
async def list_runs(
    schedule_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "read")
    row = await _load(db, schedule_id)
    runs = (
        await db.execute(
            select(ScheduledRun)
            .where(ScheduledRun.schedule_id == row.id)
            .order_by(ScheduledRun.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {"schedule_id": str(row.id), "runs": [_run_view(r) for r in runs]}


@router.delete("/{schedule_id}", status_code=status.HTTP_200_OK)
async def delete_schedule(
    schedule_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Deletes the schedule and its run history. Tasks it already created are kept."""
    enforce_rate_limit(request, "write")
    row = await _load(db, schedule_id)
    await write_audit(db, principal, "SCHEDULE_DELETE", "schedule", str(row.id),
                      before_state={"name": (row.payload or {}).get("name")})
    # Explicit, because the ON DELETE CASCADE on scheduled_runs is not enforced on SQLite.
    await db.execute(delete(ScheduledRun).where(ScheduledRun.schedule_id == row.id))
    await db.delete(row)
    await db.commit()
    return {"deleted": schedule_id}
