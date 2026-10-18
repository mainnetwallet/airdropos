"""Non-retryable model errors during planning.

User-fixable ones (credentials, billing, endpoint/model configuration) park the SAME task in
NEEDS_USER_ACTION without spending retry budget; other non-retryable ones settle as FAILED with
the reason recorded. Neither may leave the task IN_PROGRESS or schedule a retry.
"""

import pytest
from sqlalchemy import select

from backend.app.agent.controller import AgentController
from backend.app.llm.client import LLMError
from backend.app.models import Task
from backend.app.models.tasks import TaskCheckpoint
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

USER_FIXABLE = ["AUTH_FAILURE", "BILLING_REFUSAL", "NOT_FOUND", "MODEL_MISMATCH"]
UNFIXABLE = ["CONTEXT_OVERFLOW", "MALFORMED_RESPONSE", "EMPTY_CONTENT"]


async def _run_failing_plan(factory, monkeypatch, category, retryable=False, **task_kw):
    async with factory() as db:
        task = Task(task_id="AIR-NR-1", title="t", status=TaskStatus.IN_PROGRESS.value, **task_kw)
        db.add(task)
        await db.commit()
        pk = task.id

    async def broken(self, task, report):
        raise LLMError(f"provider said no ({category})", category=category, retryable=retryable)

    monkeypatch.setattr(AgentController, "_ensure_plan", broken)
    async with factory() as db:
        task = await db.get(Task, pk)
        report = await AgentController(db).execute_task(task)
    return pk, report


async def _load(factory, pk):
    async with factory() as db:
        return await db.get(Task, pk)


@pytest.mark.parametrize("category", USER_FIXABLE)
async def test_user_fixable_error_parks_the_task_without_spending_a_retry(factory, monkeypatch, category):
    pk, report = await _run_failing_plan(factory, monkeypatch, category, max_retries=3)
    task = await _load(factory, pk)
    assert task.status == TaskStatus.NEEDS_USER_ACTION.value  # not stuck IN_PROGRESS
    assert task.retry_count == 0
    assert task.next_execution_at is None
    assert task.task_id == "AIR-NR-1"
    assert category in (task.error_details or {}).get("llm_category", "")
    assert task.next_planned_action  # an actionable instruction is persisted
    assert report.outcome.value == "needs_user_action"
    async with factory() as db:
        checkpoints = (await db.scalars(select(TaskCheckpoint).where(TaskCheckpoint.task_id == pk))).all()
        assert checkpoints, "the task's state must be checkpointed"
        # Waiting for the person is not a reason for the worker to try again.
        assert await TaskManager(db).claim_next_task("w") is None


@pytest.mark.parametrize("category", UNFIXABLE)
async def test_unfixable_non_retryable_error_fails_with_the_reason(factory, monkeypatch, category):
    pk, report = await _run_failing_plan(factory, monkeypatch, category, max_retries=3)
    task = await _load(factory, pk)
    assert task.status == TaskStatus.FAILED.value
    assert task.retry_count == 0
    assert task.next_execution_at is None
    assert category in (task.error_details or {}).get("llm_category", "")
    assert report.outcome.value == "failed"
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is None


async def test_retryable_error_keeps_the_backoff_behaviour(factory, monkeypatch):
    pk, _ = await _run_failing_plan(factory, monkeypatch, "PROVIDER_ERROR", retryable=True, max_retries=3)
    task = await _load(factory, pk)
    assert task.status == TaskStatus.RETRYING.value
    assert task.retry_count == 1
    assert task.next_execution_at is not None


async def test_corrected_user_response_resumes_the_same_task(factory, monkeypatch):
    pk, _ = await _run_failing_plan(factory, monkeypatch, "AUTH_FAILURE", max_retries=3)
    async with factory() as db:
        task = await db.get(Task, pk)
        # What the existing resume path does once the person has fixed the problem.
        resumed = await TaskManager(db).transition_if_current(
            task, (TaskStatus.NEEDS_USER_ACTION,), TaskStatus.READY, reason="credentials corrected"
        )
        await db.commit()
        assert resumed is True
    async with factory() as db:
        claimed = await TaskManager(db).claim_next_task("w")
        assert claimed is not None and claimed.id == pk and claimed.task_id == "AIR-NR-1"
        assert claimed.retry_count == 0
