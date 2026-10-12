"""Regression tests: emergency-stop parking, crash-recovery safety, plan refresh after replan.

1. Emergency stop mid-run left the task IN_PROGRESS (not claimable, shown as running, only fixed
   by a worker restart). It is now parked as NEEDS_USER_ACTION for an operator to resume.
2. ``recover_interrupted_tasks`` reset every IN_PROGRESS task to RETRYING without counting a
   retry (a crash-looping task was retried forever) and without looking at whether a
   side-effecting step (on-chain transaction, web interaction) was in flight. Such tasks are now
   parked for operator verification instead of being re-executed automatically.
3. After a replan the controller kept iterating over the superseded in-memory plan.

Temporary SQLite files and a fake registry only; nothing real is touched.
"""

from __future__ import annotations

from typing import Any, List, Optional

import pytest
from sqlalchemy import select

from backend.app.agent import controller as controller_module
from backend.app.agent.controller import AgentController, LoopOutcome
from backend.app.models import ExecutionLease, Task, TaskEvent
from backend.app.planning.planner import ExecutionPlanSpec, Planner, PlanStep, StepKind
from backend.app.scheduler import worker
from backend.app.security.emergency_stop import emergency_stop
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.concurrency import LeaseManager
from backend.app.tasks.state_machine import TaskStatus
from backend.app.tools.base import ToolResult
from backend.tests.test_controller_status_and_budget import (  # noqa: F401  (factory is a fixture)
    FakeRegistry,
    _claimed_task_with_plan,
    _load,
    _run,
    _spec,
    factory,
)


# ----------------------------------------------------------------------
# 1. Emergency stop mid-execution
# ----------------------------------------------------------------------
class StoppingRegistry(FakeRegistry):
    """Engages the emergency stop right after the first tool call, like an operator would."""

    async def invoke(self, name, params, policy=None, task_id=None) -> ToolResult:
        result = await super().invoke(name, params, policy=policy, task_id=task_id)
        if len(self.calls) == 1:
            emergency_stop.activate("test stop", activated_by="operator")
        return result


async def test_emergency_stop_mid_run_parks_task_instead_of_leaving_it_in_progress(factory):
    pk = await _claimed_task_with_plan(factory, 5)
    registry = StoppingRegistry()

    report = await _run(factory, pk, registry)

    assert report.outcome is LoopOutcome.STOPPED
    assert registry.steps_run == [0]  # nothing ran after the stop
    task = await _load(factory, pk)
    assert task.status == TaskStatus.NEEDS_USER_ACTION.value  # was left IN_PROGRESS
    assert task.completed_at is None
    assert task.retry_count == 0
    assert [a["id"] for a in task.completed_actions] == ["step-0"]  # finished work is kept
    assert task.progress == pytest.approx(0.2)
    async with factory() as db:
        leases = (await db.execute(select(ExecutionLease).where(ExecutionLease.task_id == pk))).scalars().all()
        assert leases and all(lease.released_at is not None for lease in leases)
        emergency_stop._stopped = False  # lift the stop; the parked task must still not auto-run
        assert await TaskManager(db).claim_next_task("w") is None


async def test_emergency_stop_before_first_step_also_parks_the_claimed_task(factory):
    pk = await _claimed_task_with_plan(factory, 2)
    emergency_stop.activate("test stop", activated_by="operator")
    registry = FakeRegistry()

    report = await _run(factory, pk, registry)

    assert report.outcome is LoopOutcome.STOPPED
    assert registry.calls == []
    assert (await _load(factory, pk)).status == TaskStatus.NEEDS_USER_ACTION.value


async def test_parked_task_can_be_resumed_by_operator_and_continues(factory):
    pk = await _claimed_task_with_plan(factory, 3)
    registry = StoppingRegistry()
    await _run(factory, pk, registry)
    emergency_stop._stopped = False

    async with factory() as db:
        task = await db.get(Task, pk)
        await TaskManager(db).transition(task, TaskStatus.READY, reason="operator resume")
        await db.commit()
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is not None
        await db.commit()

    report = await _run(factory, pk, registry)
    assert report.outcome is LoopOutcome.COMPLETED
    assert registry.steps_run == [0, 1, 2]  # step 0 was not repeated


# ----------------------------------------------------------------------
# 2. Crash recovery
# ----------------------------------------------------------------------
@pytest.fixture
def recovery_env(factory, monkeypatch):
    monkeypatch.setattr(worker, "AsyncSessionLocal", factory)
    return factory


async def _interrupted_task(factory, steps: List[PlanStep], *, retry_count: int = 0, max_retries: int = 3):
    """A task as a crashed worker leaves it: IN_PROGRESS with a live lease and a partly run plan."""
    async with factory() as db:
        task = await TaskManager(db).create_task("Interrupted", status=TaskStatus.READY, max_retries=max_retries)
        await Planner(db, None).persist_plan(
            task, ExecutionPlanSpec(objective="o", steps=steps, rationale="r")
        )
        task.status = TaskStatus.IN_PROGRESS.value
        task.retry_count = retry_count
        task.progress = 0.4
        await LeaseManager(db).acquire(task, "controller-killed")
        await db.commit()
        return task.id


def _step(index: int, kind: StepKind, status: str, tool: Optional[str] = "fake_tool") -> PlanStep:
    return PlanStep(index=index, description=f"step {index}", kind=kind, tool=tool, status=status,
                    parameters={"step": index})


async def _step_statuses(factory, pk) -> List[str]:
    async with factory() as db:
        task = await db.get(Task, pk)
        spec = await Planner(db, None).load_plan_spec(task)
        return [s.status for s in spec.steps]


async def test_recovery_counts_the_retry_and_keeps_progress(recovery_env):
    pk = await _interrupted_task(recovery_env, [_step(0, StepKind.RESEARCH, "completed"),
                                                _step(1, StepKind.RESEARCH, "pending")])
    assert await worker.recover_interrupted_tasks() == 1
    task = await _load(recovery_env, pk)
    assert task.status == TaskStatus.RETRYING.value
    assert task.retry_count == 1  # was left at 0
    assert task.progress == pytest.approx(0.4)  # not reset to the RETRYING default


async def test_crash_looping_task_is_bounded_by_max_retries(recovery_env):
    """claim -> crash -> startup recovery, repeated: the task must end FAILED, not loop forever."""
    pk = await _interrupted_task(recovery_env, [_step(0, StepKind.RESEARCH, "pending")], max_retries=2)
    attempts = 0
    for _ in range(10):
        await worker.recover_interrupted_tasks()  # worker restarted after a crash
        task = await _load(recovery_env, pk)
        if task.status != TaskStatus.RETRYING.value:
            break
        async with recovery_env() as db:
            claimed = await TaskManager(db).claim_next_task("w")  # picked up again, crashes again
            await db.commit()
            assert claimed is not None
            attempts += 1
    assert (await _load(recovery_env, pk)).status == TaskStatus.FAILED.value
    assert attempts == 2  # exactly the configured retries


async def test_recovery_failing_exhausted_task_releases_its_lease(recovery_env):
    pk = await _interrupted_task(recovery_env, [_step(0, StepKind.RESEARCH, "pending")],
                                 retry_count=3, max_retries=3)
    await worker.recover_interrupted_tasks()
    assert (await _load(recovery_env, pk)).status == TaskStatus.FAILED.value
    async with recovery_env() as db:
        task = await db.get(Task, pk)
        await LeaseManager(db).acquire(task, "controller-next")  # raises if the lease is still live


@pytest.mark.parametrize(
    "kind, status",
    [
        (StepKind.TRANSACT, "running"),    # tx may or may not have been broadcast
        (StepKind.TRANSACT, "completed"),  # value already moved; never auto-resume
        (StepKind.INTERACT, "running"),    # a web action may or may not have been submitted
    ],
)
async def test_recovery_parks_tasks_with_side_effecting_step_in_flight(recovery_env, kind, status):
    pk = await _interrupted_task(recovery_env, [_step(0, StepKind.RESEARCH, "completed"),
                                                _step(1, kind, status)])
    assert await worker.recover_interrupted_tasks() == 0  # nothing was queued for automatic retry
    task = await _load(recovery_env, pk)
    assert task.status == TaskStatus.NEEDS_USER_ACTION.value
    assert task.retry_count == 0
    assert "recovery" in (task.error_details or {})
    assert (await _step_statuses(recovery_env, pk))[1] == status  # evidence of the in-flight step kept
    async with recovery_env() as db:
        assert await TaskManager(db).claim_next_task("w") is None  # never auto-claimed
        leased = await db.get(Task, pk)
        await LeaseManager(db).acquire(leased, "controller-operator")  # lease was released


async def test_recovery_resets_interrupted_read_only_step_so_it_can_run_again(recovery_env):
    pk = await _interrupted_task(recovery_env, [_step(0, StepKind.RESEARCH, "completed"),
                                                _step(1, StepKind.RESEARCH, "running"),
                                                _step(2, StepKind.RESEARCH, "pending")])
    assert await worker.recover_interrupted_tasks() == 1
    assert (await _load(recovery_env, pk)).status == TaskStatus.RETRYING.value
    assert await _step_statuses(recovery_env, pk) == ["completed", "pending", "pending"]


async def test_recovery_records_an_audit_event_per_task(recovery_env):
    pk = await _interrupted_task(recovery_env, [_step(0, StepKind.TRANSACT, "running")])
    await worker.recover_interrupted_tasks()
    async with recovery_env() as db:
        rows = (await db.execute(select(TaskEvent).where(
            TaskEvent.task_id == pk, TaskEvent.event_type == "STATUS_TRANSITION"))).scalars().all()
    assert [r.payload["to"] for r in rows] == [TaskStatus.NEEDS_USER_ACTION.value]


# ----------------------------------------------------------------------
# 3. Replan: the controller must continue with the newly persisted plan
# ----------------------------------------------------------------------
async def test_controller_follows_the_new_plan_after_a_replan(factory, monkeypatch):
    pk = await _claimed_task_with_plan(factory, 3)
    registry = FakeRegistry(denied={"denied_tool"})

    async with factory() as db:  # step 0 will be refused, which triggers a replan
        task = await db.get(Task, pk)
        planner = Planner(db, None)
        old = await planner.load_plan_spec(task)
        old.steps[0].tool = "denied_tool"
        await planner.persist_plan(task, old, trigger_reason="test setup")
        await db.commit()

    async with factory() as db:
        task = await db.get(Task, pk)
        controller = AgentController(db, registry=registry)

        async def fake_replan(task, spec, trigger, reason, constraints=None):
            revised = ExecutionPlanSpec(
                objective=spec.objective,
                rationale="revised",
                steps=[
                    PlanStep(index=0, description="step 0", kind=StepKind.RESEARCH, tool="denied_tool",
                             status="skipped"),
                    PlanStep(index=1, description="replacement", kind=StepKind.RESEARCH, tool="fake_read",
                             parameters={"step": 99}),
                ],
            )
            await controller.planner.persist_plan(task, revised, trigger=trigger, trigger_reason=reason)
            return revised

        monkeypatch.setattr(controller.planner, "replan", fake_replan)
        report = await controller.execute_task(task)
        await db.commit()

    # Old steps 1 and 2 belong to the superseded plan and must never run.
    assert registry.steps_run == [0, 99]
    assert report.outcome is LoopOutcome.PARTIALLY_COMPLETED  # a step was skipped: never COMPLETED
    task = await _load(factory, pk)
    assert task.status == TaskStatus.PARTIALLY_COMPLETED.value
