"""Regression tests for ``LeaseManager.break_stale_leases``.

It was unreferenced and unsafe if ever wired up: it set statuses directly (no state-machine
validation, no audit event), only guarded a *completed* transact step (a running transact or
interact step could be re-executed), had no retry bound, double-counted retries for tasks that were
not executing, and edited ``error_details`` in place, which the un-tracked JSONB column silently
drops. Temporary SQLite only; nothing real is touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.models import ExecutionLease, Task, TaskEvent
from backend.app.planning.planner import StepKind
from backend.app.tasks.concurrency import LeaseManager
from backend.app.tasks.state_machine import TaskStatus
from backend.tests.test_controller_status_and_budget import _load, factory  # noqa: F401
from backend.tests.test_recovery_estop_replan import _interrupted_task, _step


async def _expire_lease(factory, pk) -> None:
    async with factory() as db:
        lease = await db.scalar(select(ExecutionLease).where(ExecutionLease.task_id == pk))
        lease.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await db.commit()


async def _break(factory) -> dict:
    async with factory() as db:
        result = await LeaseManager(db).break_stale_leases()
        await db.commit()
        return result


async def _events(factory, pk):
    async with factory() as db:
        rows = (await db.execute(select(TaskEvent).where(TaskEvent.task_id == pk))).scalars().all()
        return [r for r in rows if r.event_type == "STATUS_TRANSITION"]


@pytest.mark.parametrize(
    "kind, status",
    [
        (StepKind.TRANSACT, "running"),
        (StepKind.TRANSACT, "completed"),
        (StepKind.INTERACT, "running"),
    ],
)
async def test_stale_lease_parks_uncertain_side_effects_and_persists_the_note(factory, kind, status):
    pk = await _interrupted_task(factory, [_step(0, kind, status)])
    async with factory() as db:  # pre-existing details: the in-place edit used to be lost for these
        task = await db.get(Task, pk)
        task.error_details = {"earlier": "kept"}
        await db.commit()
    await _expire_lease(factory, pk)

    result = await _break(factory)

    task = await _load(factory, pk)
    assert result["parked_for_review"] == 1 and result["recovered"] == 0
    assert task.status == TaskStatus.NEEDS_USER_ACTION.value
    assert task.retry_count == 0
    assert task.error_details.get("earlier") == "kept"
    assert "lease_recovery" in task.error_details  # persisted, not dropped
    assert any(e.payload["to"] == "NEEDS_USER_ACTION" for e in await _events(factory, pk))
    async with factory() as db:
        from backend.app.tasks.manager import TaskManager

        assert await TaskManager(db).claim_next_task("w") is None  # never auto-claimed


async def test_stale_lease_retries_read_only_work_once_and_audits_it(factory):
    pk = await _interrupted_task(factory, [_step(0, StepKind.RESEARCH, "running")])
    await _expire_lease(factory, pk)

    result = await _break(factory)

    task = await _load(factory, pk)
    assert result["recovered"] == 1
    assert task.status == TaskStatus.RETRYING.value
    assert task.retry_count == 1  # exactly one, via the state machine
    assert any(e.payload["to"] == "RETRYING" for e in await _events(factory, pk))


async def test_stale_lease_fails_a_task_whose_retries_are_spent(factory):
    pk = await _interrupted_task(factory, [_step(0, StepKind.RESEARCH, "pending")],
                                 retry_count=3, max_retries=3)
    await _expire_lease(factory, pk)

    result = await _break(factory)

    assert result["failed"] == 1
    assert (await _load(factory, pk)).status == TaskStatus.FAILED.value


async def test_stale_lease_on_a_task_that_is_not_executing_is_only_released(factory):
    pk = await _interrupted_task(factory, [_step(0, StepKind.RESEARCH, "pending")])
    async with factory() as db:
        task = await db.get(Task, pk)
        task.status = TaskStatus.RETRYING.value
        task.retry_count = 1
        await db.commit()
    await _expire_lease(factory, pk)

    await _break(factory)

    task = await _load(factory, pk)
    assert task.status == TaskStatus.RETRYING.value
    assert task.retry_count == 1  # not incremented a second time
    async with factory() as db:
        lease = await db.scalar(select(ExecutionLease).where(ExecutionLease.task_id == pk))
        assert lease.released_at is not None


async def test_live_lease_is_left_alone(factory):
    pk = await _interrupted_task(factory, [_step(0, StepKind.RESEARCH, "running")])  # lease not expired

    result = await _break(factory)

    assert result["stale_found"] == 0
    assert (await _load(factory, pk)).status == TaskStatus.IN_PROGRESS.value
