"""Regression tests: redundant IN_PROGRESS transition and loop-budget exhaustion.

Bug 1: ``AgentController`` called ``tasks.transition(task, IN_PROGRESS, ...)`` after every
processed step (and after every replan) although ``claim_next_task`` has already put the task in
IN_PROGRESS. The state machine forbids IN_PROGRESS -> IN_PROGRESS, so the second plan step of any
tool-backed plan raised ``InvalidTransition``.

Bug 2: when the iteration budget ran out, ``_apply_final_status`` only rewrote
``next_planned_action``. The task stayed IN_PROGRESS forever (not claimable, not terminal), and
because the budget was an absolute cap that also counted iterations of earlier runs, resuming it
would be exhausted again immediately.

Everything here uses a throw-away SQLite file and a fake tool registry. No wallet, network, chain
or browser is ever touched.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agent import controller as controller_module
from backend.app.agent.controller import AgentController, LoopOutcome
from backend.app.database.session import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    Base,
    configure_sqlite_connection,
)
from backend.app.models import (
    ExecutionLease,
    Task,
    TaskCheckpoint,
    TaskEvent,
    TaskExecution,
)
from backend.app.planning.planner import ExecutionPlanSpec, Planner, PlanStep, StepKind
from backend.app.scheduler import worker, worker_status
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import (
    InvalidTransition,
    TaskStatus,
    can_transition,
)
from backend.app.tools.base import ToolResult, ToolResultStatus


# ----------------------------------------------------------------------
# Fixtures and helpers
# ----------------------------------------------------------------------
@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ctl.db'}",
        connect_args={"timeout": SQLITE_BUSY_TIMEOUT_SECONDS},
    )
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


class FakeRegistry:
    """Stands in for the tool registry. Never performs a real action."""

    def __init__(self, *, evidence: bool = True, denied: Optional[set] = None) -> None:
        self.evidence = evidence
        self.denied = denied or set()
        self.calls: List[str] = []
        self.steps_run: List[int] = []  # which plan step each call belonged to, in order
        self.disabled: List[str] = []

    async def invoke(self, name, params, policy=None, task_id=None) -> ToolResult:
        self.calls.append(name)
        self.steps_run.append((params or {}).get("step"))
        if name in self.denied:
            return ToolResult(
                status=ToolResultStatus.NOT_AUTHORIZED,
                error="not authorized by test policy",
                error_category="NOT_AUTHORIZED",
            )
        return ToolResult.success(
            {"ok": True, "tool": name},
            evidence={"authoritative": True} if self.evidence else None,
        )

    def disable(self, name: str, reason: str) -> None:
        self.disabled.append(name)


def _spec(n_steps: int, tool: str = "fake_read") -> ExecutionPlanSpec:
    steps = [
        PlanStep(
            index=i,
            description=f"step {i}",
            kind=StepKind.RESEARCH,
            tool=tool,
            parameters={"step": i},
            verification_method="tool_evidence",
        )
        for i in range(n_steps)
    ]
    return ExecutionPlanSpec(objective="test objective", steps=steps, rationale="test")


async def _claimed_task_with_plan(factory, n_steps: int, *, max_retries: int = 3, tool: str = "fake_read"):
    """A task exactly as the worker hands it to the controller: claimed, so IN_PROGRESS."""
    async with factory() as db:
        task = await TaskManager(db).create_task(
            "Budget test", status=TaskStatus.READY, max_retries=max_retries
        )
        await Planner(db, None).persist_plan(task, _spec(n_steps, tool))
        await db.commit()
        pk = task.id
    async with factory() as db:
        claimed = await TaskManager(db).claim_next_task("test-worker")
        await db.commit()
        assert claimed is not None and claimed.id == pk
        assert claimed.status == TaskStatus.IN_PROGRESS.value
    return pk


async def _run(factory, pk, registry: FakeRegistry, max_iterations: Optional[int] = None):
    """One controller run in its own session, committed the way the worker commits."""
    async with factory() as db:
        task = await db.get(Task, pk)
        controller = AgentController(db, registry=registry)
        report = await controller.execute_task(task, max_iterations=max_iterations)
        await db.commit()
        return report


async def _load(factory, pk) -> Task:
    async with factory() as db:
        return await db.get(Task, pk)


async def _transition_events(factory, pk) -> List[Dict[str, Any]]:
    async with factory() as db:
        rows = (
            await db.execute(
                select(TaskEvent).where(
                    TaskEvent.task_id == pk, TaskEvent.event_type == "STATUS_TRANSITION"
                )
            )
        ).scalars().all()
        return [r.payload for r in rows]


# ----------------------------------------------------------------------
# Bug 1: redundant IN_PROGRESS -> IN_PROGRESS transition
# ----------------------------------------------------------------------
async def test_state_machine_still_rejects_in_progress_self_transition(factory):
    """The fix must not weaken validation: the self-transition stays forbidden."""
    assert not can_transition(TaskStatus.IN_PROGRESS, TaskStatus.IN_PROGRESS)
    pk = await _claimed_task_with_plan(factory, 1)
    async with factory() as db:
        task = await db.get(Task, pk)
        with pytest.raises(InvalidTransition):
            await TaskManager(db).transition(task, TaskStatus.IN_PROGRESS, reason="redundant")


async def test_processing_multiple_plan_steps_does_not_raise(factory):
    pk = await _claimed_task_with_plan(factory, 3)
    registry = FakeRegistry()

    report = await _run(factory, pk, registry)  # raised InvalidTransition on step 2 before the fix

    assert registry.calls == ["fake_read"] * 3
    assert report.outcome is LoopOutcome.COMPLETED
    task = await _load(factory, pk)
    assert task.status == TaskStatus.COMPLETED.value
    assert [a["id"] for a in task.completed_actions] == ["step-0", "step-1", "step-2"]
    # No transition event may claim IN_PROGRESS -> IN_PROGRESS.
    assert not any(
        p["from"] == p["to"] == TaskStatus.IN_PROGRESS.value
        for p in await _transition_events(factory, pk)
    )


async def test_step_processing_updates_progress_and_next_action_without_transition(factory):
    pk = await _claimed_task_with_plan(factory, 4)
    registry = FakeRegistry()
    async with factory() as db:
        task = await db.get(Task, pk)
        controller = AgentController(db, registry=registry)
        plan = await controller.planner.load_plan_spec(task)
        report = controller_module.LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED)

        await controller._run_iteration(task, 1, report, plan)
        assert task.status == TaskStatus.IN_PROGRESS.value
        assert task.progress == pytest.approx(0.25)
        assert task.next_planned_action == "step 1"

        await controller._run_iteration(task, 2, report, plan)
        assert task.status == TaskStatus.IN_PROGRESS.value
        assert task.progress == pytest.approx(0.5)
        assert task.next_planned_action == "step 2"
        await db.commit()

    # Progress updates are not status transitions, so no extra events are written for them.
    assert await _transition_events(factory, pk) == []


async def test_replan_does_not_raise_redundant_transition(factory, monkeypatch):
    pk = await _claimed_task_with_plan(factory, 2)
    registry = FakeRegistry(denied={"fake_read"})
    async with factory() as db:
        task = await db.get(Task, pk)
        controller = AgentController(db, registry=registry)

        async def fake_replan(task, spec, trigger, reason, constraints=None):
            return spec  # no LLM; the persisted plan already carries the skipped step

        monkeypatch.setattr(controller.planner, "replan", fake_replan)
        plan = await controller.planner.load_plan_spec(task)
        report = controller_module.LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED)

        _step, is_terminal, _outcome = await controller._run_iteration(task, 1, report, plan)

        assert is_terminal is False
        assert task.status == TaskStatus.IN_PROGRESS.value
        assert task.next_planned_action  # replan still records what comes next


# ----------------------------------------------------------------------
# Bug 2: budget exhaustion
# ----------------------------------------------------------------------
async def test_budget_exhausted_incomplete_plan_is_not_left_in_progress(factory):
    pk = await _claimed_task_with_plan(factory, 5)
    registry = FakeRegistry()

    report = await _run(factory, pk, registry, max_iterations=2)

    assert report.outcome is LoopOutcome.BUDGET_EXHAUSTED
    task = await _load(factory, pk)
    assert task.status == TaskStatus.PARTIALLY_COMPLETED.value  # was left IN_PROGRESS
    assert task.completed_at is None  # incomplete work is never COMPLETED
    assert task.progress == pytest.approx(0.4)
    assert task.next_planned_action.startswith("step 2")  # resume point is recorded
    assert task.retry_count == 0  # running out of iterations is not a failure
    assert [a["id"] for a in task.completed_actions] == ["step-0", "step-1"]


async def test_budget_exhausted_preserves_checkpoint_executions_and_releases_lease(factory):
    pk = await _claimed_task_with_plan(factory, 5)
    await _run(factory, pk, FakeRegistry(), max_iterations=2)

    async with factory() as db:
        checkpoints = (
            await db.execute(select(TaskCheckpoint).where(TaskCheckpoint.task_id == pk))
        ).scalars().all()
        executions = (
            await db.execute(select(TaskExecution).where(TaskExecution.task_id == pk))
        ).scalars().all()
        leases = (
            await db.execute(select(ExecutionLease).where(ExecutionLease.task_id == pk))
        ).scalars().all()

    assert sorted(e.iteration for e in executions) == [1, 2]
    final = max(checkpoints, key=lambda c: (c.iteration, c.checkpoint_name == "budget_exhausted"))
    assert final.checkpoint_name == "budget_exhausted" and final.iteration == 2
    statuses = [s["status"] for s in final.state["observations"]["plan_steps"]]
    assert statuses == ["completed", "completed", "pending", "pending", "pending"]
    assert leases and all(lease.released_at is not None for lease in leases)


async def test_budget_exhausted_task_is_not_auto_claimed_and_can_be_resumed_by_operator(factory):
    pk = await _claimed_task_with_plan(factory, 5)
    await _run(factory, pk, FakeRegistry(), max_iterations=2)

    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is None  # parked, not hot-looping
        task = await db.get(Task, pk)
        await TaskManager(db).transition(task, TaskStatus.READY, reason="operator resume")
        await db.commit()
    async with factory() as db:
        claimed = await TaskManager(db).claim_next_task("w")
        assert claimed is not None and claimed.id == pk


async def test_budget_exhausted_after_last_step_finalizes_a_verified_plan(factory):
    """N steps need N iterations plus one to finalize; budget == N must not strand a done plan."""
    pk = await _claimed_task_with_plan(factory, 2)

    report = await _run(factory, pk, FakeRegistry(), max_iterations=2)

    assert report.outcome is LoopOutcome.COMPLETED
    task = await _load(factory, pk)
    assert task.status == TaskStatus.COMPLETED.value
    assert task.progress == 1.0


async def test_budget_exhausted_after_last_step_without_evidence_is_never_completed(factory):
    pk = await _claimed_task_with_plan(factory, 2)

    await _run(factory, pk, FakeRegistry(evidence=False), max_iterations=2)

    task = await _load(factory, pk)
    assert task.status == TaskStatus.PARTIALLY_COMPLETED.value
    assert task.completed_at is None


async def test_resume_after_budget_exhaustion_makes_progress(factory):
    """The budget is per run. Previously a resumed run was exhausted before executing a step."""
    pk = await _claimed_task_with_plan(factory, 5)
    registry = FakeRegistry()
    await _run(factory, pk, registry, max_iterations=2)
    assert len(registry.calls) == 2

    async with factory() as db:
        task = await db.get(Task, pk)
        await TaskManager(db).transition(task, TaskStatus.READY, reason="operator resume")
        await db.commit()
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is not None
        await db.commit()

    await _run(factory, pk, registry, max_iterations=2)
    assert registry.steps_run == [0, 1, 2, 3]  # steps 2 and 3 ran; 0 and 1 were NOT repeated
    assert (await _load(factory, pk)).status == TaskStatus.PARTIALLY_COMPLETED.value

    async with factory() as db:
        task = await db.get(Task, pk)
        await TaskManager(db).transition(task, TaskStatus.READY, reason="operator resume")
        await db.commit()
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is not None
        await db.commit()

    report = await _run(factory, pk, registry, max_iterations=3)
    assert report.outcome is LoopOutcome.COMPLETED
    assert registry.steps_run == [0, 1, 2, 3, 4]  # every step exactly once across three runs
    task = await _load(factory, pk)
    assert task.status == TaskStatus.COMPLETED.value
    async with factory() as db:
        iterations = [
            e.iteration
            for e in (
                await db.execute(select(TaskExecution).where(TaskExecution.task_id == pk))
            ).scalars()
        ]
    assert len(iterations) == len(set(iterations))  # iteration numbers keep increasing


async def test_plan_step_status_is_persisted_across_sessions(factory):
    """update_step_status edited stored dicts in place, so no UPDATE was ever emitted."""
    async with factory() as db:
        task = await TaskManager(db).create_task("Persist steps", status=TaskStatus.READY)
        planner = Planner(db, None)
        await planner.persist_plan(task, _spec(3))
        await db.commit()
        pk = task.id

    async with factory() as db:
        task = await db.get(Task, pk)
        planner = Planner(db, None)
        await planner.update_step_status(task, 0, "completed", result={"ok": True})
        await planner.update_step_status(task, 1, "failed", failure_reason="boom")
        await db.commit()

    async with factory() as db:
        task = await db.get(Task, pk)
        spec = await Planner(db, None).load_plan_spec(task)
    assert [s.status for s in spec.steps] == ["completed", "failed", "pending"]
    assert spec.steps[1].failure_reason == "boom"


# ----------------------------------------------------------------------
# Worker: budget exhaustion end to end (real controller, fake tools, temp DB)
# ----------------------------------------------------------------------
async def test_worker_leaves_budget_exhausted_task_resumable(factory, monkeypatch):
    monkeypatch.setattr(worker, "AsyncSessionLocal", factory)
    monkeypatch.setattr(worker_status, "AsyncSessionLocal", factory)
    monkeypatch.setattr(controller_module.settings, "MAX_EXECUTION_LOOP_ITERATIONS", 2)
    worker._shutdown_event.clear()
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay=0, *a, **k):
        worker._shutdown_event.set()
        await real_sleep(0)

    monkeypatch.setattr(worker.asyncio, "sleep", fast_sleep)

    registry_under_test = registry = FakeRegistry()
    monkeypatch.setattr(
        worker, "AgentController", lambda db, registry=None: AgentController(db, registry=registry_under_test)
    )

    async with factory() as db:
        task = await TaskManager(db).create_task("Worker budget", status=TaskStatus.READY)
        await Planner(db, None).persist_plan(task, _spec(5))
        await db.commit()
        pk = task.id

    try:
        await worker.task_execution_loop()
    finally:
        worker._shutdown_event.clear()

    task = await _load(factory, pk)
    assert task.status == TaskStatus.PARTIALLY_COMPLETED.value
    assert task.retry_count == 0
    assert len(registry.calls) == 2
    async with factory() as db:
        leases = (await db.execute(select(ExecutionLease).where(ExecutionLease.task_id == pk))).scalars().all()
    assert leases and all(lease.released_at is not None for lease in leases)


# ----------------------------------------------------------------------
# Claiming, cooldowns and leases (worker / task-lease regressions)
# ----------------------------------------------------------------------
async def _task_in(factory, status: TaskStatus, *, retry_count: int = 0, max_retries: int = 3,
                   next_execution_at=None, title: str = "t") -> Any:
    async with factory() as db:
        task = await TaskManager(db).create_task(title, status=status, max_retries=max_retries)
        task.retry_count = retry_count
        task.next_execution_at = next_execution_at
        await db.commit()
        return task.id


async def test_retrying_tasks_waiting_for_backoff_do_not_starve_the_queue(factory, monkeypatch):
    """RETRYING tasks are parked, not running. Counting them against MAX_CONCURRENT_TASKS meant
    that N waiting retries (N == the limit) blocked every claim, including their own retry."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(controller_module.settings, "MAX_CONCURRENT_TASKS", 2)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    await _task_in(factory, TaskStatus.RETRYING, retry_count=1, next_execution_at=future, title="waiting-1")
    await _task_in(factory, TaskStatus.RETRYING, retry_count=1, next_execution_at=future, title="waiting-2")
    due = await _task_in(factory, TaskStatus.RETRYING, retry_count=1, next_execution_at=past, title="due")

    async with factory() as db:
        claimed = await TaskManager(db).claim_next_task("w")
        await db.commit()
    assert claimed is not None and claimed.id == due


async def test_concurrency_limit_still_counts_tasks_that_are_actually_running(factory, monkeypatch):
    monkeypatch.setattr(controller_module.settings, "MAX_CONCURRENT_TASKS", 2)
    await _task_in(factory, TaskStatus.IN_PROGRESS, title="running-1")
    await _task_in(factory, TaskStatus.IN_PROGRESS, title="running-2")
    await _task_in(factory, TaskStatus.READY, title="queued")
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is None


async def test_retry_cooldown_is_respected(factory):
    from datetime import datetime, timedelta, timezone

    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    pk = await _task_in(factory, TaskStatus.RETRYING, retry_count=1, next_execution_at=future)
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is None  # still cooling down
    async with factory() as db:
        task = await db.get(Task, pk)
        task.next_execution_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    async with factory() as db:
        claimed = await TaskManager(db).claim_next_task("w")
        assert claimed is not None and claimed.id == pk


async def test_task_with_spent_retry_budget_is_not_claimed(factory):
    await _task_in(factory, TaskStatus.RETRYING, retry_count=4, max_retries=3)
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is None


async def test_concurrent_workers_claim_distinct_tasks_exactly_once(factory):
    from sqlalchemy.exc import OperationalError

    ids = [await _task_in(factory, TaskStatus.READY, title=f"job-{i}") for i in range(3)]

    async def poll(name):
        async with factory() as db:
            try:
                task = await TaskManager(db).claim_next_task(name)
                await db.commit()
                return task.id if task is not None else None
            except OperationalError:
                await db.rollback()
                return None  # lost the SQLite write race; never a duplicate claim

    results = [r for r in await asyncio.gather(*(poll(f"w{i}") for i in range(6))) if r is not None]
    assert len(results) == len(set(results))  # nobody got the same task twice
    assert set(results) <= set(ids)
    async with factory() as db:
        claims = (
            await db.execute(select(TaskEvent).where(TaskEvent.event_type == "TASK_CLAIMED"))
        ).scalars().all()
    assert len(claims) == len(results)
