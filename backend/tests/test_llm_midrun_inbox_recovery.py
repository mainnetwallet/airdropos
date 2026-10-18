"""A user-fixable model error AFTER planning started must reach the Agent Inbox.

Before: the error raised inside an execution window ended the run as FAILED and left the task
IN_PROGRESS for the worker, which counted it as a failed attempt (RETRYING, then FAILED once the
budget was spent). The operator was never told to fix their key, credit or model settings, and
the retry budget was burned on an error no retry can fix.

Real paths exercised: `AgentController.execute_task` -> `_run_window` -> checkpoints, lease,
`park_for_intervention`, and the worker's own `task_execution_loop`. Only the model-backed
planner and the per-iteration step (which would call the model and the tools) are stubbed.
"""

import asyncio
import logging

import pytest
from sqlalchemy import func, select

from backend.app.agent import controller as controller_module
from backend.app.agent.controller import AgentController, LoopOutcome, StepResult
from backend.app.core.config import settings
from backend.app.database.session import Base
from backend.app.llm.client import LLMError
from backend.app.models import AgentConversation, ConversationMessage, InterventionRequest, Task
from backend.app.models.tasks import TaskCheckpoint
from backend.app.planning.planner import ExecutionPlanSpec, PlanStep, StepKind
from backend.app.scheduler import worker
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

OWNER = "alice@example.test"
# Deliberately NOT key-shaped: pattern redaction cannot catch it, only the exact value can.
FAKE_KEY = "Zq9-plain-secret-value-4417"

FIXABLE = {
    "AUTH_FAILURE": "LLM_AUTH_FAILURE",
    "BILLING_REFUSAL": "LLM_BILLING_REFUSAL",
    "NOT_FOUND": "LLM_NOT_FOUND",
    "MODEL_MISMATCH": "LLM_MODEL_MISMATCH",
}


def _spec() -> ExecutionPlanSpec:
    return ExecutionPlanSpec(
        objective="o",
        steps=[
            PlanStep(index=i, description=f"do {i}", kind=StepKind.INTERACT, tool="probe",
                     depends_on=[i - 1] if i else [])
            for i in range(3)
        ],
    )


def _stub_run(monkeypatch, failure, *, fail_on_iteration=2):
    """Plan once; iteration 1 succeeds; the model fails on `fail_on_iteration`."""
    state = {"iterations": []}

    async def ensure_plan(self, task, report):
        return _spec()

    async def run_iteration(self, task, iteration, report, plan_spec):
        state["iterations"].append(iteration)
        if iteration == fail_on_iteration:
            raise failure
        return StepResult(iteration, f"did {iteration}", "probe", {"ok": True}, True, "ok", 0), False, None

    monkeypatch.setattr(AgentController, "_ensure_plan", ensure_plan)
    monkeypatch.setattr(AgentController, "_run_iteration", run_iteration)
    return state


async def _make_task(factory, task_id="AIR-MR-1", **kw) -> int:
    async with factory() as db:
        task = Task(task_id=task_id, title=f"title {task_id}", status=TaskStatus.IN_PROGRESS.value,
                    owner_email=OWNER, **{"max_retries": 3, **kw})
        db.add(task)
        await db.commit()
        return task.id


async def _execute(factory, pk):
    async with factory() as db:
        report = await AgentController(db).execute_task(await db.get(Task, pk))
        await db.commit()  # the worker commits after a run
    return report


async def _inbox(factory, pk):
    async with factory() as db:
        requests = (await db.scalars(select(InterventionRequest).where(InterventionRequest.task_id == pk))).all()
        conversations = (await db.scalars(select(AgentConversation).where(AgentConversation.task_id == pk))).all()
        return requests, conversations


async def _checkpoint_names(factory, pk):
    async with factory() as db:
        rows = (await db.scalars(
            select(TaskCheckpoint).where(TaskCheckpoint.task_id == pk).order_by(TaskCheckpoint.created_at)
        )).all()
        return [c.checkpoint_name for c in rows]


async def _everything_persisted(factory) -> str:
    """Every column of every row in the database, as text."""
    chunks = []
    async with factory() as db:
        for table in Base.metadata.sorted_tables:
            for row in (await db.execute(select(table))).mappings().all():
                chunks.append(" ".join(str(v) for v in row.values() if v is not None))
    return "\n".join(chunks)


def _llm_error(category, retryable=False, text=None):
    return LLMError(text or f"provider refused ({category})", category=category, retryable=retryable)


# ----------------------------------------------------------------------------------------
# 1. A user-fixable error mid-run parks the SAME task in the Agent Inbox.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("category,reason", sorted(FIXABLE.items()))
async def test_midrun_fixable_error_parks_the_task_in_the_inbox(factory, monkeypatch, category, reason):
    state = _stub_run(monkeypatch, _llm_error(category))
    pk = await _make_task(factory)

    report = await _execute(factory, pk)

    assert state["iterations"] == [1, 2]  # it really was mid-run: one step done, then refused
    assert report.outcome is LoopOutcome.NEEDS_USER_ACTION
    async with factory() as db:
        task = await db.get(Task, pk)
        assert task.status == TaskStatus.NEEDS_USER_ACTION.value  # not left IN_PROGRESS / FAILED
        assert task.task_id == "AIR-MR-1"
        assert task.next_execution_at is None
        assert task.next_planned_action
        assert (task.error_details or {}).get("llm_category") == category
        assert task.last_blocker and category in task.last_blocker
    requests, conversations = await _inbox(factory, pk)
    assert [(r.reason_code, r.status, r.error_category) for r in requests] == [(reason, "PENDING", category)]
    assert len(conversations) == 1 and conversations[0].requires_reply is True
    async with factory() as db:
        # Waiting for a person is never a reason for the worker to try again.
        assert await TaskManager(db).claim_next_task("w") is None


async def test_midrun_park_preserves_checkpoints_and_does_not_spend_retries(factory, monkeypatch):
    _stub_run(monkeypatch, _llm_error("AUTH_FAILURE"))
    pk = await _make_task(factory, retry_count=2)

    await _execute(factory, pk)

    names = await _checkpoint_names(factory, pk)
    assert "iteration_1" in names, "progress made before the refusal must stay checkpointed"
    assert "llm_error" in names
    assert "awaiting_operator:llm_auth_failure" in names
    async with factory() as db:
        task = await db.get(Task, pk)
        assert task.retry_count == 2  # unchanged: not a retry
        assert task.max_retries == 3


async def test_midrun_park_is_recorded_once_and_a_repeat_does_not_stack_requests(factory, monkeypatch):
    _stub_run(monkeypatch, _llm_error("BILLING_REFUSAL"))
    pk = await _make_task(factory)
    await _execute(factory, pk)
    # An operator resumes without fixing anything; the same refusal comes back mid-run again.
    async with factory() as db:
        task = await db.get(Task, pk)
        assert await TaskManager(db).transition_if_current(
            task, (TaskStatus.NEEDS_USER_ACTION,), TaskStatus.READY, reason="resumed"
        )
        await db.commit()
    async with factory() as db:
        claimed = await TaskManager(db).claim_next_task("w")
        await db.commit()
        assert claimed is not None and claimed.id == pk
    await _execute(factory, pk)

    requests, conversations = await _inbox(factory, pk)
    assert len(conversations) == 1
    assert len([r for r in requests if r.status == "PENDING"]) == 1
    async with factory() as db:
        assert (await db.get(Task, pk)).retry_count == 0
        assert (await db.get(Task, pk)).status == TaskStatus.NEEDS_USER_ACTION.value


# ----------------------------------------------------------------------------------------
# 2. Only the model client's category decides it; every other error keeps its behaviour.
# ----------------------------------------------------------------------------------------
async def test_midrun_retryable_error_keeps_the_existing_backoff_path(factory, monkeypatch):
    # Even a fixable-looking category is only parked when the client says it is not retryable.
    _stub_run(monkeypatch, _llm_error("AUTH_FAILURE", retryable=True))
    pk = await _make_task(factory)

    report = await _execute(factory, pk)

    assert report.outcome is LoopOutcome.FAILED
    requests, _ = await _inbox(factory, pk)
    assert requests == []
    async with factory() as db:
        assert (await db.get(Task, pk)).status == TaskStatus.IN_PROGRESS.value  # the worker settles it
    await worker._settle_failed_execution(pk, "execution failed", {})
    async with factory() as db:
        task = await db.get(Task, pk)
        assert task.status == TaskStatus.RETRYING.value
        assert task.retry_count == 1 and task.next_execution_at is not None


@pytest.mark.parametrize("category", ["PROVIDER_ERROR", "TIMEOUT", "RATE_LIMITING", "CONNECTION_ERROR"])
async def test_midrun_outage_is_not_parked(factory, monkeypatch, category):
    _stub_run(monkeypatch, _llm_error(category, retryable=True))
    pk = await _make_task(factory)
    report = await _execute(factory, pk)
    assert report.outcome is LoopOutcome.FAILED
    assert (await _inbox(factory, pk))[0] == []


@pytest.mark.parametrize("category", ["CONTEXT_OVERFLOW", "MALFORMED_RESPONSE", "EMPTY_CONTENT", "BAD_REQUEST"])
async def test_midrun_non_fixable_non_retryable_error_is_not_parked(factory, monkeypatch, category):
    _stub_run(monkeypatch, _llm_error(category))
    pk = await _make_task(factory)
    report = await _execute(factory, pk)
    assert report.outcome is LoopOutcome.FAILED
    assert (await _inbox(factory, pk))[0] == []
    async with factory() as db:
        assert (await db.get(Task, pk)).status == TaskStatus.IN_PROGRESS.value


async def test_a_tool_failure_with_a_model_like_category_is_not_a_model_refusal(factory, monkeypatch):
    """A tool's own AUTH_FAILURE / NOT_FOUND never becomes an LLM_* reason."""
    from backend.app.tools.base import ToolResult

    pk = await _make_task(factory)
    async with factory() as db:
        controller = AgentController(db)
        task = await db.get(Task, pk)
        result = await controller._request_operator_input(
            task, iteration=1,
            error_text="Step 2 failed: the site returned 401 unauthorized",
            error_category="AUTH_FAILURE",
        )
        assert result is not None
        assert result["reason_code"] == "MISSING_AUTHENTICATION"
    assert ToolResult  # imported only to show the tool-side type exists


# ----------------------------------------------------------------------------------------
# 3. Secrets never reach persisted text, events or logs.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("category", sorted(FIXABLE))
async def test_midrun_secret_echoed_by_the_provider_is_scrubbed_everywhere(
    factory, monkeypatch, caplog, category
):
    monkeypatch.setattr(settings, "LLM_API_KEY", FAKE_KEY)
    published = []

    async def capture(event, **fields):
        published.append((event, fields))

    from backend.app.inbox import events

    monkeypatch.setattr(events, "publish", capture)
    echoed = f"provider refused ({category}): invalid key {FAKE_KEY} for this account"
    _stub_run(monkeypatch, _llm_error(category, text=echoed))
    pk = await _make_task(factory)

    with caplog.at_level(logging.DEBUG):
        report = await _execute(factory, pk)

    assert FAKE_KEY not in await _everything_persisted(factory)  # messages, events, context, checkpoints
    assert FAKE_KEY not in caplog.text  # including the traceback of the logged exception
    assert FAKE_KEY not in repr(published)
    assert FAKE_KEY not in " ".join(report.blockers + report.failures)


@pytest.mark.parametrize("retryable", [True, False])
async def test_midrun_secret_is_scrubbed_even_when_the_error_is_not_parked(
    factory, monkeypatch, caplog, retryable
):
    monkeypatch.setattr(settings, "LLM_API_KEY", FAKE_KEY)
    _stub_run(monkeypatch, _llm_error("BAD_REQUEST", retryable=retryable, text=f"bad request near {FAKE_KEY}"))
    pk = await _make_task(factory)
    with caplog.at_level(logging.DEBUG):
        report = await _execute(factory, pk)
    await worker._settle_failed_execution(pk, "execution failed", {"failures": [str(f) for f in report.failures]})
    assert FAKE_KEY not in await _everything_persisted(factory)
    assert FAKE_KEY not in caplog.text


# ----------------------------------------------------------------------------------------
# 4. The real worker loop.
# ----------------------------------------------------------------------------------------
async def _run_worker_once(factory, monkeypatch, pk):
    """Drive `task_execution_loop` for exactly one claimed task, with the real controller."""
    notified = []

    async def notify(title, body, severity, **kw):
        notified.append((title, body))

    async def no_sweep():
        return 0

    async def no_release(*a, **k):
        return 0

    claims = {"n": 0}
    real_claim = TaskManager.claim_next_task

    async def claim_once(self, worker_id):
        claims["n"] += 1
        if claims["n"] > 1:
            worker._shutdown_event.set()
            return None
        task = await real_claim(self, worker_id)
        worker._shutdown_event.set()  # the loop exits after this iteration finishes
        return task

    monkeypatch.setattr(worker.dispatcher, "notify", notify)
    monkeypatch.setattr(worker, "sweep_orphaned_tasks", no_sweep)
    monkeypatch.setattr(worker, "release_idle_telegram_sessions", no_release)
    monkeypatch.setattr(TaskManager, "claim_next_task", claim_once)
    worker._shutdown_event.clear()
    try:
        await asyncio.wait_for(worker.task_execution_loop(), 30)
    finally:
        worker._shutdown_event.clear()
    return notified


async def _ready_task(factory, **kw) -> int:
    async with factory() as db:
        task = Task(task_id="AIR-MR-W", title="t", status=TaskStatus.READY.value, owner_email=OWNER,
                    max_retries=3, **kw)
        db.add(task)
        await db.commit()
        return task.id


@pytest.mark.parametrize("category,reason", sorted(FIXABLE.items()))
async def test_worker_leaves_a_midrun_model_refusal_parked_without_a_retry(
    factory, monkeypatch, category, reason
):
    _stub_run(monkeypatch, _llm_error(category))
    pk = await _ready_task(factory)

    notified = await _run_worker_once(factory, monkeypatch, pk)

    async with factory() as db:
        task = await db.get(Task, pk)
        assert task.status == TaskStatus.NEEDS_USER_ACTION.value
        assert task.retry_count == 0 and task.next_execution_at is None
    requests, _ = await _inbox(factory, pk)
    assert [r.reason_code for r in requests] == [reason]
    assert notified and "AIR-MR-W" in notified[0][0]  # the operator is told, as for any parked task


async def test_worker_still_retries_a_midrun_outage_with_backoff(factory, monkeypatch):
    _stub_run(monkeypatch, _llm_error("PROVIDER_ERROR", retryable=True))
    pk = await _ready_task(factory)

    await _run_worker_once(factory, monkeypatch, pk)

    async with factory() as db:
        task = await db.get(Task, pk)
        assert task.status == TaskStatus.RETRYING.value
        assert task.retry_count == 1 and task.next_execution_at is not None
    assert (await _inbox(factory, pk))[0] == []


async def test_nothing_else_was_created(factory, monkeypatch):
    _stub_run(monkeypatch, _llm_error("AUTH_FAILURE"))
    pk = await _make_task(factory)
    async with factory() as db:
        before = await db.scalar(select(func.count()).select_from(Task))
    await _execute(factory, pk)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Task)) == before
        assert await db.scalar(select(func.count()).select_from(ConversationMessage)) >= 1
    assert controller_module.USER_FIXABLE_LLM_ERRORS == frozenset(FIXABLE)
