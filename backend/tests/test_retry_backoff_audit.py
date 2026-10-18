"""Audit tests: retry budget, backoff scheduling and eligibility.

`test_planning_outage_*` pins a confirmed defect: a model outage while planning moved the task to
RETRYING without any `next_execution_at`, so an idle worker re-claimed it within seconds and a
short outage burned the whole retry budget.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.agent.controller import AgentController
from backend.app.llm.client import LLMError
from backend.app.models import Task
from backend.app.scheduler import worker
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus


async def _task(factory, status, **kw) -> int:
    async with factory() as db:
        task = Task(task_id=f"AIR-RB-{status.value}-{id(kw)}", title="t", status=status.value, **kw)
        db.add(task)
        await db.commit()
        return task.id


async def _load(factory, pk) -> Task:
    async with factory() as db:
        return await db.get(Task, pk)


@pytest.mark.parametrize(
    "count,expected",
    [(-3, 30), (0, 30), (1, 60), (2, 120), (4, 480), (5, 600), (20, 600), (10_000, 600)],
)
def test_backoff_is_exponential_and_capped(count, expected):
    assert worker.retry_delay_seconds(count) == expected


async def test_failed_attempt_is_scheduled_with_backoff_and_counted_once(factory):
    pk = await _task(factory, TaskStatus.IN_PROGRESS, max_retries=3)
    before = datetime.now(timezone.utc)
    await worker._settle_failed_execution(pk, "execution failed", {})
    task = await _load(factory, pk)
    assert task.status == TaskStatus.RETRYING.value
    assert task.retry_count == 1
    assert task.next_execution_at.replace(tzinfo=timezone.utc) >= before + timedelta(seconds=29)
    # A second settle on a task that is no longer IN_PROGRESS must not count another retry.
    await worker._settle_failed_execution(pk, "execution failed", {})
    assert (await _load(factory, pk)).retry_count == 1


async def test_exhausted_budget_fails_the_task(factory):
    pk = await _task(factory, TaskStatus.IN_PROGRESS, max_retries=2, retry_count=2)
    await worker._settle_failed_execution(pk, "execution failed", {})
    task = await _load(factory, pk)
    assert task.status == TaskStatus.FAILED.value
    assert task.retry_count == 2


async def test_retrying_task_is_not_claimed_before_it_is_eligible(factory):
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    await _task(factory, TaskStatus.RETRYING, retry_count=1, next_execution_at=future)
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is None


async def test_retrying_task_is_claimed_once_eligible(factory):
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    pk = await _task(factory, TaskStatus.RETRYING, retry_count=1, next_execution_at=past)
    async with factory() as db:
        claimed = await TaskManager(db).claim_next_task("w")
        assert claimed is not None and claimed.id == pk


async def test_planning_outage_is_retried_with_backoff_not_immediately(factory, monkeypatch):
    pk = await _task(factory, TaskStatus.IN_PROGRESS, max_retries=3)

    async def down(self, task, report):
        raise LLMError("model unreachable", category="PROVIDER_ERROR")

    monkeypatch.setattr(AgentController, "_ensure_plan", down)
    before = datetime.now(timezone.utc)
    async with factory() as db:
        task = await db.get(Task, pk)
        await AgentController(db).execute_task(task)

    task = await _load(factory, pk)
    assert task.status == TaskStatus.RETRYING.value
    assert task.retry_count == 1
    assert task.next_execution_at is not None, "no backoff: the task is claimable immediately"
    assert task.next_execution_at.replace(tzinfo=timezone.utc) > before + timedelta(seconds=20)
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is None
