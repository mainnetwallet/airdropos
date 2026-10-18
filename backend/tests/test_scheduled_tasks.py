"""User-defined scheduled tasks: cron maths, validation, the HTTP API and the scheduler loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.scheduler import cron, user_tasks
from backend.app.scheduler.scheduler import Scheduler

NOW = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)


# ======================================================================
# Cron
# ======================================================================
def _next(expr: str, tz: str = "UTC", after: datetime = NOW, n: int = 3):
    return cron.upcoming(expr, after, cron.get_timezone(tz), n)


def test_every_fifteen_minutes():
    got = _next("*/15 * * * *")
    assert [g.minute for g in got] == [15, 30, 45]


def test_weekday_morning_in_local_timezone_is_returned_in_utc():
    # 09:00 in Dhaka (UTC+6) on weekdays. 2026-10-18 is a Sunday, so the next is Monday.
    got = _next("0 9 * * MON-FRI", "Asia/Dhaka")
    assert got[0] == datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc)
    assert [g.weekday() for g in got] == [0, 1, 2]


def test_sunday_can_be_zero_or_seven():
    assert _next("0 0 * * 0", n=1) == _next("0 0 * * 7", n=1) == _next("0 0 * * SUN", n=1)


def test_day_of_month_and_weekday_are_ored_when_both_restricted():
    # Friday OR the 13th: Fri 23 Oct comes before Fri-or-13th Nov.
    got = _next("0 0 13 * FRI", n=2)
    assert got[0] == datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
    assert got[1] == datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)


def test_dst_gap_is_skipped_not_invented():
    # 02:30 does not exist in New York on 8 March 2026.
    got = _next("30 2 * * *", "America/New_York", datetime(2026, 3, 7, 12, tzinfo=timezone.utc), 2)
    assert got[0] == datetime(2026, 3, 9, 6, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "expr",
    ["* * * *", "61 * * * *", "0 25 * * *", "a b c d e", "5-1 * * * *", "*/0 * * * *", "0 0 31 2 *"],
)
def test_invalid_or_impossible_expressions_are_rejected(expr):
    with pytest.raises(cron.CronError):
        _next(expr, n=1)


def test_unknown_timezone_is_rejected():
    with pytest.raises(cron.CronError):
        cron.get_timezone("Mars/Olympus")


# ======================================================================
# Trigger validation
# ======================================================================
def _validate(trigger, **kw):
    base = dict(interval_minutes=None, cron_expression=None, timezone_name="UTC", run_at=None, now=NOW)
    base.update(kw)
    return user_tasks.validate_trigger(trigger, **base)


def test_interval_has_a_floor():
    with pytest.raises(user_tasks.ScheduleError):
        _validate("INTERVAL", interval_minutes=1)
    assert _validate("INTERVAL", interval_minutes=30) == NOW + timedelta(minutes=30)


def test_cron_that_would_flood_the_queue_is_rejected():
    for expr in ("* * * * *", "0,2 * * * *", "*/1 * * * *"):
        with pytest.raises(user_tasks.ScheduleError, match="less than"):
            _validate("CRON", cron_expression=expr)
    assert _validate("CRON", cron_expression="*/5 * * * *") == NOW + timedelta(minutes=5)


def test_once_must_be_in_the_future_and_reads_naive_time_in_the_schedule_zone():
    with pytest.raises(user_tasks.ScheduleError):
        _validate("ONCE", run_at="2026-10-18T13:00:00+00:00")
    got = _validate("ONCE", run_at="2026-10-19T09:00", timezone_name="Asia/Dhaka")
    assert got == datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc)


def test_preview_returns_consecutive_runs():
    runs = user_tasks.preview(
        "CRON", interval_minutes=None, cron_expression="0 * * * *", timezone_name="UTC", run_at=None, count=3, now=NOW
    )
    assert runs == [NOW + timedelta(hours=h) for h in (1, 2, 3)]


# ======================================================================
# HTTP API
# ======================================================================
@pytest.fixture
def client(factory) -> Iterator[TestClient]:
    from backend.app.main import app

    original = settings.AUTH_ENABLED
    settings.AUTH_ENABLED = False
    with TestClient(app) as test_client:
        yield test_client
    settings.AUTH_ENABLED = original


def _body(**extra: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "name": "Morning check",
        "title": "Check new airdrops",
        "description": "Look for new campaigns",
        "priority": 3,
        "trigger_type": "CRON",
        "cron_expression": "0 9 * * *",
        "timezone": "Asia/Dhaka",
    }
    body.update(extra)
    return body


def test_create_list_get_roundtrip(client):
    created = client.post("/api/v1/scheduled-tasks", json=_body())
    assert created.status_code == 201, created.text
    schedule = created.json()
    assert schedule["name"] == "Morning check"
    assert schedule["trigger_type"] == "CRON"
    assert schedule["is_active"] is True
    assert schedule["next_run_at"] is not None

    listing = client.get("/api/v1/scheduled-tasks").json()
    assert listing["count"] == 1
    assert listing["limits"]["min_interval_minutes"] == user_tasks.MIN_INTERVAL_MINUTES
    assert client.get(f"/api/v1/scheduled-tasks/{schedule['id']}").json()["title"] == "Check new airdrops"


def test_invalid_triggers_are_422(client):
    assert client.post("/api/v1/scheduled-tasks", json=_body(cron_expression="nope")).status_code == 422
    assert client.post("/api/v1/scheduled-tasks", json=_body(timezone="Mars/X")).status_code == 422
    assert client.post(
        "/api/v1/scheduled-tasks", json=_body(trigger_type="INTERVAL", interval_minutes=2)
    ).status_code == 422
    assert client.post(
        "/api/v1/scheduled-tasks", json=_body(trigger_type="ONCE", run_at="2001-01-01T00:00")
    ).status_code == 422


def test_pause_resume_and_update(client):
    sid = client.post("/api/v1/scheduled-tasks", json=_body()).json()["id"]

    paused = client.post(f"/api/v1/scheduled-tasks/{sid}/pause").json()
    assert paused["is_active"] is False and paused["next_run_at"] is None

    resumed = client.post(f"/api/v1/scheduled-tasks/{sid}/resume").json()
    assert resumed["is_active"] is True and resumed["next_run_at"] is not None

    updated = client.patch(
        f"/api/v1/scheduled-tasks/{sid}",
        json={"trigger_type": "INTERVAL", "interval_minutes": 60, "title": "Renamed"},
    ).json()
    assert updated["trigger_type"] == "INTERVAL"
    assert updated["interval_minutes"] == 60
    assert updated["cron_expression"] is None
    assert updated["title"] == "Renamed"

    bad = client.patch(f"/api/v1/scheduled-tasks/{sid}", json={"interval_minutes": 1})
    assert bad.status_code == 422


def test_run_now_creates_a_queued_task_and_records_a_run(client):
    sid = client.post("/api/v1/scheduled-tasks", json=_body()).json()["id"]
    before = client.get(f"/api/v1/scheduled-tasks/{sid}").json()

    result = client.post(f"/api/v1/scheduled-tasks/{sid}/run-now")
    assert result.status_code == 200, result.text
    task_id = result.json()["task_id"]
    assert task_id

    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert task["status"] == "READY"  # claimable by the worker
    assert task["title"] == "Check new airdrops"

    after = client.get(f"/api/v1/scheduled-tasks/{sid}").json()
    assert after["total_runs"] == 1
    assert after["next_run_at"] == before["next_run_at"]  # run-now does not move the schedule
    runs = client.get(f"/api/v1/scheduled-tasks/{sid}/runs").json()["runs"]
    assert runs[0]["task_id"] == task_id and runs[0]["manual"] is True


def test_delete_removes_schedule_and_runs_but_keeps_tasks(client):
    sid = client.post("/api/v1/scheduled-tasks", json=_body()).json()["id"]
    task_id = client.post(f"/api/v1/scheduled-tasks/{sid}/run-now").json()["task_id"]
    assert client.delete(f"/api/v1/scheduled-tasks/{sid}").status_code == 200
    assert client.get(f"/api/v1/scheduled-tasks/{sid}").status_code == 404
    assert client.get(f"/api/v1/tasks/{task_id}").status_code == 200


def test_preview_endpoint(client):
    ok = client.post(
        "/api/v1/scheduled-tasks/preview",
        json={"trigger_type": "CRON", "cron_expression": "0 */6 * * *", "timezone": "UTC", "count": 3},
    )
    assert ok.status_code == 200 and len(ok.json()["next_runs"]) == 3
    bad = client.post(
        "/api/v1/scheduled-tasks/preview", json={"trigger_type": "CRON", "cron_expression": "* * * * *"}
    )
    assert bad.status_code == 422


def test_unknown_and_malformed_ids_are_404(client):
    assert client.get("/api/v1/scheduled-tasks/not-a-uuid").status_code == 404
    assert client.post("/api/v1/scheduled-tasks/00000000-0000-0000-0000-000000000000/pause").status_code == 404


async def test_system_jobs_are_not_exposed_or_editable(client, factory):
    from backend.app.models import AutomationSchedule

    async with factory() as db:
        row = AutomationSchedule(job_name="watchdog", job_type="WATCHDOG", interval_minutes=15, payload={})
        db.add(row)
        await db.commit()
        system_id = str(row.id)

    assert client.get("/api/v1/scheduled-tasks").json()["count"] == 0
    assert client.post(f"/api/v1/scheduled-tasks/{system_id}/pause").status_code == 404
    assert client.delete(f"/api/v1/scheduled-tasks/{system_id}").status_code == 404


# ======================================================================
# Scheduler loop
# ======================================================================
async def _make_schedule(factory, **fields) -> str:
    from backend.app.models import AutomationSchedule

    defaults = dict(
        job_name=f"user_task:{datetime.now().timestamp()}",
        job_type=user_tasks.JOB_TYPE,
        trigger_type="INTERVAL",
        interval_minutes=30,
        is_active=True,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        payload={"title": "Scheduled work", "priority": 4, "skip_if_running": True},
    )
    defaults.update(fields)
    async with factory() as db:
        row = AutomationSchedule(**defaults)
        db.add(row)
        await db.commit()
        return str(row.id)


def _scheduler() -> Scheduler:
    s = Scheduler()
    s.register_job_type(user_tasks.JOB_TYPE, user_tasks.run_schedule)
    return s


async def test_due_schedule_creates_a_queued_task_and_advances(factory):
    from backend.app.models import AutomationSchedule, ScheduledRun, Task

    sid = await _make_schedule(factory)
    async with factory() as db:
        await _scheduler()._tick(db)

    async with factory() as db:
        tasks = (await db.execute(select(Task))).scalars().all()
        assert [t.title for t in tasks] == ["Scheduled work"]
        assert tasks[0].status == "READY"
        row = await db.get(AutomationSchedule, __import__("uuid").UUID(sid))
        assert row.total_runs == 1
        assert row.next_run_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=25)
        run = (await db.execute(select(ScheduledRun))).scalars().one()
        assert run.status == "SUCCESS" and run.result_summary["task_id"] == tasks[0].task_id


async def test_overlapping_run_is_skipped_while_previous_task_is_unfinished(factory):
    from backend.app.models import AutomationSchedule, ScheduledRun, Task

    sid = await _make_schedule(factory)
    scheduler = _scheduler()
    async with factory() as db:
        await scheduler._tick(db)

    # Make it due again; the first task is still READY, so this fire must be skipped.
    async with factory() as db:
        row = await db.get(AutomationSchedule, __import__("uuid").UUID(sid))
        row.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
    async with factory() as db:
        await scheduler._tick(db)

    async with factory() as db:
        assert len((await db.execute(select(Task))).scalars().all()) == 1
        statuses = sorted(r.status for r in (await db.execute(select(ScheduledRun))).scalars().all())
        assert statuses == ["SKIPPED", "SUCCESS"]


async def test_once_schedule_fires_a_single_time_then_deactivates(factory):
    from backend.app.models import AutomationSchedule, Task

    sid = await _make_schedule(
        factory,
        trigger_type="ONCE",
        interval_minutes=None,
        payload={"title": "One shot", "run_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
    )
    scheduler = _scheduler()
    async with factory() as db:
        await scheduler._tick(db)
    async with factory() as db:
        await scheduler._tick(db)

    async with factory() as db:
        assert len((await db.execute(select(Task))).scalars().all()) == 1
        row = await db.get(AutomationSchedule, __import__("uuid").UUID(sid))
        assert row.is_active is False and row.next_run_at is None


async def test_cron_schedule_is_rearmed_at_its_next_slot(factory):
    from backend.app.models import AutomationSchedule

    sid = await _make_schedule(
        factory,
        trigger_type="CRON",
        interval_minutes=None,
        cron_expression="0 * * * *",
        payload={"title": "Hourly", "timezone": "UTC"},
    )
    async with factory() as db:
        await _scheduler()._tick(db)
    async with factory() as db:
        row = await db.get(AutomationSchedule, __import__("uuid").UUID(sid))
        nxt = row.next_run_at.replace(tzinfo=timezone.utc)
        assert nxt.minute == 0 and nxt > datetime.now(timezone.utc)


async def test_paused_schedule_never_fires(factory):
    from backend.app.models import Task

    await _make_schedule(factory, is_active=False)
    async with factory() as db:
        await _scheduler()._tick(db)
    async with factory() as db:
        assert (await db.execute(select(Task))).scalars().all() == []


async def test_existing_named_system_jobs_still_run_with_their_old_handler_signature(factory):
    from backend.app.models import AutomationSchedule

    calls = []

    async def handler(db, payload):
        calls.append(payload)
        return {"ok": True}

    scheduler = Scheduler()
    scheduler.register_job("watchdog", "WATCHDOG", 15, handler)
    async with factory() as db:
        db.add(
            AutomationSchedule(
                job_name="watchdog",
                job_type="WATCHDOG",
                trigger_type="INTERVAL",
                interval_minutes=15,
                is_active=True,
                next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                payload={"k": "v"},
            )
        )
        await db.commit()
    async with factory() as db:
        await scheduler._tick(db)
    assert calls == [{"k": "v"}]
