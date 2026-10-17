"""Regression tests: lease heartbeat, ownership loss, and atomic recovery claims.

Pins two defects that were present before the fix:

* A lease was renewed only after an iteration finished, so one long LLM or tool call could outlive
  the TTL and let another worker (or recovery) take a task that was still running. Nothing stopped
  the old run from starting its next side effect.
* Two recovery workers that both found a task orphaned (no lease at all, or an already released
  one: nothing left to break) both recovered it, because `FOR UPDATE SKIP LOCKED` is a no-op on
  SQLite and nothing else claimed the task first.

These tests use a FILE-backed SQLite database with a separate connection per session. The shared
in-memory database of the `factory` fixture runs every session on one connection, which cannot
express "two workers at once".
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from backend.app.models import ExecutionLease, ExecutionPlan, Task, TaskEvent
from backend.app.tasks.concurrency import (
    LeaseConflict,
    LeaseHeartbeat,
    LeaseManager,
)
from backend.app.tasks.state_machine import TaskStatus

_n = 0


@pytest.fixture
async def file_factory(monkeypatch, tmp_path):
    """Session factory over a file database; every session gets its own connection."""
    import backend.app.database.session as session_module
    import backend.app.models  # noqa: F401  (register every table on Base.metadata)
    import backend.app.scheduler.worker as worker_module
    from backend.app.database.session import Base

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'airdropos-test.db'}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    def make(*args, **kwargs):
        return AsyncSession(engine, expire_on_commit=False, autoflush=False)

    monkeypatch.setattr(session_module, "AsyncSessionLocal", make)
    monkeypatch.setattr(worker_module, "AsyncSessionLocal", make)
    yield make
    await engine.dispose()


async def make_task(db, status=TaskStatus.IN_PROGRESS, **kw) -> Task:
    global _n
    _n += 1
    task = Task(task_id=f"AIR-HB-{_n:05d}", title="t", status=status.value, **kw)
    db.add(task)
    await db.commit()
    return task


async def lease_row(factory, task_pk) -> ExecutionLease:
    async with factory() as db:
        return await db.scalar(select(ExecutionLease).where(ExecutionLease.task_id == task_pk))


async def expire(factory, task_pk, *, seconds_ago=60):
    async with factory() as db:
        await db.execute(
            update(ExecutionLease)
            .where(ExecutionLease.task_id == task_pk)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago))
        )
        await db.commit()


async def take_over(factory, task_pk, worker="w-other"):
    """Another worker (or recovery) takes the task after the lease has run out."""
    await expire(factory, task_pk, seconds_ago=5)
    async with factory() as db:
        task = await db.get(Task, task_pk)
        info = await LeaseManager(db).acquire(task, worker)
        await db.commit()
    return info


async def age(factory, task_pk, seconds=600):
    async with factory() as db:
        await db.execute(
            update(Task)
            .where(Task.id == task_pk)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=seconds))
        )
        await db.commit()


async def acquire_lease(factory, task_pk, ttl_minutes, worker="w-run"):
    async with factory() as db:
        task = await db.get(Task, task_pk)
        info = await LeaseManager(db).acquire(task, worker, ttl_minutes=ttl_minutes)
        await db.commit()
    return info


async def wait_for(predicate, timeout=5.0, step=0.02):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


def live_heartbeat_tasks():
    return [
        t for t in asyncio.all_tasks()
        if (t.get_name() or "").startswith("lease-heartbeat") and not t.done()
    ]


# ====================================================================== heartbeat (unit)
async def test_heartbeat_renews_the_lease_past_its_original_expiry(file_factory):
    async with file_factory() as db:
        task = await make_task(db)
        task_pk, label = task.id, task.task_id
    info = await acquire_lease(file_factory, task_pk, ttl_minutes=0.025)  # 1.5 s
    original_expiry = info.expires_at

    async with LeaseHeartbeat(
        task_pk, label, info.lease_token, ttl_minutes=0.025, interval_seconds=0.2,
        expires_at=info.expires_at,
    ) as hb:
        await asyncio.sleep(2.2)  # well past the lease's original expiry, with no iteration boundary
        row = await lease_row(file_factory, task_pk)
        assert hb.owned and hb.renewals >= 3
        assert row.released_at is None
        assert LeaseManager._as_utc(row.expires_at) > datetime.now(timezone.utc)
        assert LeaseManager._as_utc(row.expires_at) > LeaseManager._as_utc(original_expiry)
        assert row.lease_token == info.lease_token, "renewal must not rotate the token"


async def test_heartbeat_never_extends_a_lease_owned_by_another_worker(file_factory):
    async with file_factory() as db:
        task = await make_task(db)
        task_pk, label = task.id, task.task_id
    mine = await acquire_lease(file_factory, task_pk, ttl_minutes=30, worker="w-old")
    theirs = await take_over(file_factory, task_pk, worker="w-new")
    before = await lease_row(file_factory, task_pk)

    async with LeaseHeartbeat(
        task_pk, label, mine.lease_token, interval_seconds=0.05
    ) as hb:
        assert await wait_for(lambda: not hb.owned), "a rejected renewal must be noticed"
        await asyncio.sleep(0.2)  # give a buggy loop the chance to keep writing

    after = await lease_row(file_factory, task_pk)
    assert hb.lost_reason and "refused" in hb.lost_reason
    assert hb.renewals == 0
    assert after.worker_id == "w-new" and after.lease_token == theirs.lease_token
    assert after.expires_at == before.expires_at and after.renewed_at == before.renewed_at, (
        "the new owner's lease must be untouched by the old run's heartbeat"
    )


async def test_heartbeat_cannot_revive_a_lease_that_already_ran_out(file_factory):
    async with file_factory() as db:
        task = await make_task(db)
        task_pk, label = task.id, task.task_id
    info = await acquire_lease(file_factory, task_pk, ttl_minutes=30)
    await expire(file_factory, task_pk)  # nobody took it, but it has lapsed

    hb = LeaseHeartbeat(task_pk, label, info.lease_token, interval_seconds=10)
    assert await hb.verify() is False
    assert hb.owned is False
    row = await lease_row(file_factory, task_pk)
    assert LeaseManager._as_utc(row.expires_at) < datetime.now(timezone.utc), "still lapsed: not revived"


async def test_verify_checks_the_database_not_a_stale_flag(file_factory):
    async with file_factory() as db:
        task = await make_task(db)
        task_pk, label = task.id, task.task_id
    mine = await acquire_lease(file_factory, task_pk, ttl_minutes=30, worker="w-old")

    hb = LeaseHeartbeat(task_pk, label, mine.lease_token, interval_seconds=3600)
    hb.start()  # never ticks within this test
    try:
        await take_over(file_factory, task_pk, worker="w-new")
        assert hb.owned is True, "the flag alone cannot know yet"
        assert await hb.verify() is False, "the check made before a side effect must"
        assert hb.owned is False
    finally:
        await hb.stop()


async def test_renewal_errors_fail_closed_once_the_confirmed_window_passes(file_factory, monkeypatch):
    async with file_factory() as db:
        task = await make_task(db)
        task_pk, label = task.id, task.task_id
    info = await acquire_lease(file_factory, task_pk, ttl_minutes=0.02)  # 1.2 s

    calls = {"n": 0}

    async def broken(self):
        calls["n"] += 1
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(LeaseHeartbeat, "_renew_once", broken)
    hb = LeaseHeartbeat(
        task_pk, label, info.lease_token, ttl_minutes=0.02, interval_seconds=0.1,
        expires_at=info.expires_at,
    )
    hb.start()
    try:
        assert hb.owned, "an error says nothing about ownership: still owned inside the window"
        assert await hb.verify() is False, "but ownership cannot be CONFIRMED, so nothing new may start"
        assert await wait_for(lambda: not hb.owned, timeout=4.0)
        assert hb.lost_reason
        assert await wait_for(lambda: hb._task.done(), timeout=2.0), "the loop ends once ownership is gone"
        assert calls["n"] >= 2, "failed renewals are retried, not abandoned at the first error"
    finally:
        await hb.stop()


async def test_one_failed_renewal_followed_by_success_keeps_ownership(file_factory, monkeypatch):
    async with file_factory() as db:
        task = await make_task(db)
        task_pk, label = task.id, task.task_id
    info = await acquire_lease(file_factory, task_pk, ttl_minutes=30)

    real = LeaseHeartbeat._renew_once
    state = {"n": 0}

    async def flaky(self):
        state["n"] += 1
        if state["n"] == 1:
            raise TimeoutError("busy")
        return await real(self)

    monkeypatch.setattr(LeaseHeartbeat, "_renew_once", flaky)
    async with LeaseHeartbeat(task_pk, label, info.lease_token, interval_seconds=0.05) as hb:
        assert await wait_for(lambda: hb.renewals >= 2)
        assert hb.owned and hb.failed_attempts == 1


async def test_heartbeat_stop_is_clean_and_idempotent(file_factory):
    async with file_factory() as db:
        task = await make_task(db)
        task_pk, label = task.id, task.task_id
    info = await acquire_lease(file_factory, task_pk, ttl_minutes=30)

    hb = LeaseHeartbeat(task_pk, label, info.lease_token, interval_seconds=0.05).start()
    assert await wait_for(lambda: hb.renewals >= 1)
    assert live_heartbeat_tasks()
    await hb.stop()
    await hb.stop()  # twice is fine
    await LeaseHeartbeat(task_pk, label, info.lease_token).stop()  # never started is fine
    assert not live_heartbeat_tasks()
    renewed_at = (await lease_row(file_factory, task_pk)).renewed_at
    await asyncio.sleep(0.2)
    assert (await lease_row(file_factory, task_pk)).renewed_at == renewed_at, "no writes after stop"


async def test_heartbeat_stops_when_its_scope_exits_with_an_exception(file_factory):
    async with file_factory() as db:
        task = await make_task(db)
        task_pk, label = task.id, task.task_id
    info = await acquire_lease(file_factory, task_pk, ttl_minutes=30)
    with pytest.raises(RuntimeError):
        async with LeaseHeartbeat(task_pk, label, info.lease_token, interval_seconds=0.05):
            raise RuntimeError("run blew up")
    assert not live_heartbeat_tasks()


async def test_cancelling_the_caller_during_stop_is_not_swallowed(file_factory):
    """`stop()` absorbs the heartbeat's own cancellation, never a cancellation aimed at its caller."""
    async with file_factory() as db:
        task = await make_task(db)
        task_pk, label = task.id, task.task_id
    info = await acquire_lease(file_factory, task_pk, ttl_minutes=30)
    hb = LeaseHeartbeat(task_pk, label, info.lease_token, interval_seconds=0.05).start()

    slow = asyncio.Event()

    async def slow_to_wind_down():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await slow.wait()  # still winding down when the caller is cancelled
            raise

    real_task = hb._task
    real_task.cancel()
    await asyncio.gather(real_task, return_exceptions=True)
    hb._task = asyncio.get_running_loop().create_task(slow_to_wind_down(), name="lease-heartbeat-slow")
    await asyncio.sleep(0)  # let it start sleeping

    stopper = asyncio.get_running_loop().create_task(hb.stop())
    await asyncio.sleep(0.1)
    stopper.cancel()
    slow.set()
    with pytest.raises(asyncio.CancelledError):
        await stopper


# ====================================================================== controller integration
class FakeTool:
    is_idempotent = True


class FakeRegistry:
    """Counts invocations and lets the test decide when a tool call returns."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def get(self, name):
        return FakeTool()

    async def invoke(self, name, params=None, **kwargs):
        self.calls.append(name)
        return await self.behaviour(len(self.calls), name)


async def build_run(file_factory, steps, behaviour, *, ttl_minutes=30, interval=None, kinds=None):
    """A real AgentController running a pre-built plan against a fake tool registry."""
    from backend.app.agent import controller as controller_module
    from backend.app.agent.controller import AgentController, LoopOutcome, StepResult
    from backend.app.planning.planner import ExecutionPlanSpec, PlanStep, StepKind

    async with file_factory() as db:
        task = await make_task(db)
        task_pk = task.id
        db.add(
            ExecutionPlan(
                task_id=task_pk, revision=1, is_current=True, objective="o",
                steps=[
                    {"index": i, "kind": "interact", "status": "pending", "description": f"do {i}",
                     "tool": "probe"}
                    for i in range(steps)
                ],
            )
        )
        await db.commit()

    registry = FakeRegistry(behaviour)
    db = file_factory()
    controller = AgentController(db, registry=registry)
    controller.LEASE_TTL_MINUTES = ttl_minutes
    controller.LEASE_HEARTBEAT_INTERVAL_SECONDS = interval

    spec = ExecutionPlanSpec(
        objective="o",
        revision=1,
        steps=[
            PlanStep(index=i, description=f"do {i}", kind=StepKind.INTERACT, tool="probe",
                     depends_on=[i - 1] if i else [])
            for i in range(steps)
        ],
    )

    async def ensure_plan(task, report):
        return spec

    async def none(*a, **k):
        return None

    async def rationale(*a, **k):
        return "next"

    async def verify_step(task, step, result):
        return True, 1

    async def finalize(task, iteration, plan, report):
        return StepResult(iteration, "final", None, None, True, "done", 0), True, LoopOutcome.PARTIALLY_COMPLETED

    controller._ensure_plan = ensure_plan
    controller._rearm_policy_refused_steps = none
    controller._maybe_replan_for_discovery = none
    controller._next_step_rationale = rationale
    controller._verify_step = verify_step
    controller._absorb_result = none
    controller._finalize_plan = finalize
    controller._apply_final_status = none
    controller._settle_budget_exhausted = none
    return controller, db, task_pk, registry, SimpleNamespace(module=controller_module)


async def run_task(controller, db, task_pk):
    """Run like the worker does: execute, then commit what the run left pending."""
    task = await db.get(Task, task_pk)
    report = await controller.execute_task(task)
    await db.commit()
    return report


async def plan_statuses(factory, task_pk):
    async with factory() as db:
        plan = await db.scalar(
            select(ExecutionPlan).where(ExecutionPlan.task_id == task_pk, ExecutionPlan.is_current.is_(True))
        )
        return [s["status"] for s in plan.steps]


async def test_a_long_tool_call_keeps_the_lease_alive_for_the_whole_call(file_factory):
    from backend.app.tools.base import ToolResult

    seen = {}

    async def long_tool(n, name):
        await asyncio.sleep(3.0)  # longer than the 2.4 s lease
        seen["hb"] = controller._heartbeat
        seen["row"] = await lease_row(file_factory, task_pk)
        return ToolResult.success({"ok": True})

    controller, db, task_pk, registry, _ = await build_run(
        file_factory, 1, long_tool, ttl_minutes=0.04, interval=0.3
    )
    try:
        report = await run_task(controller, db, task_pk)
    finally:
        await db.close()

    row = seen["row"]
    assert row.released_at is None, "still held at the end of the long call"
    assert LeaseManager._as_utc(row.expires_at) > datetime.now(timezone.utc), (
        "the lease must not have lapsed during a call that outlasted its TTL"
    )
    assert seen["hb"].renewals >= 4 and seen["hb"].owned
    assert registry.calls == ["probe"]
    assert report.outcome.value != "stopped", report.blockers
    assert await plan_statuses(file_factory, task_pk) == ["completed"]
    assert (await lease_row(file_factory, task_pk)).released_at is not None, "released when the run ends"
    assert not live_heartbeat_tasks() and controller._heartbeat is None


async def test_ownership_lost_during_a_tool_call_is_reported_unknown_not_not_run(file_factory):
    from backend.app.tools.base import ToolResult

    started = asyncio.Event()
    finish = asyncio.Event()
    takeover = {}

    async def slow_tool(n, name):
        started.set()
        await finish.wait()
        return ToolResult.success({"ok": True})

    controller, db, task_pk, registry, _ = await build_run(
        file_factory, 2, slow_tool, ttl_minutes=30, interval=0.1
    )
    run = asyncio.get_running_loop().create_task(run_task(controller, db, task_pk))
    try:
        await asyncio.wait_for(started.wait(), 10)
        takeover["info"] = await take_over(file_factory, task_pk)
        assert await wait_for(lambda: controller._heartbeat and not controller._heartbeat.owned)
        finish.set()  # the in-flight tool now returns SUCCESS
        report = await asyncio.wait_for(run, 10)
    finally:
        finish.set()
        if not run.done():
            run.cancel()
        await db.close()

    assert report.outcome.value == "stopped"
    text = " ".join(report.blockers)
    assert "UNKNOWN" in text and "probe" in text, text
    assert "not assumed to have not happened" in text or "was not assumed" in text or "not happened" in text
    assert registry.calls == ["probe"], "the second step must never start"
    # The in-flight step stays 'running' (so recovery parks a side-effecting one for review); it is
    # not recorded as completed, and the second step was never touched.
    assert await plan_statuses(file_factory, task_pk) == ["running", "pending"]
    async with file_factory() as check:
        assert (await check.get(Task, task_pk)).status == TaskStatus.IN_PROGRESS.value
    row = await lease_row(file_factory, task_pk)
    assert row.worker_id == "w-other" and row.lease_token == takeover["info"].lease_token
    assert row.released_at is None, "the old run's cleanup must not release the new owner's lease"
    assert not live_heartbeat_tasks()


async def test_no_side_effect_starts_once_ownership_is_lost_between_steps(file_factory):
    from backend.app.tools.base import ToolResult

    async def quick_tool(n, name):
        return ToolResult.success({"n": n})

    controller, db, task_pk, registry, _ = await build_run(
        file_factory, 3, quick_tool, ttl_minutes=30, interval=3600  # heartbeat never ticks here
    )
    original_checkpoint = controller._checkpoint
    state = {"taken": False}

    async def checkpoint_then_lose(task, iteration, name):
        await original_checkpoint(task, iteration, name)
        if not state["taken"]:
            state["taken"] = True
            await db.commit()  # release this session's write lock, as the loop does next
            await take_over(file_factory, task_pk)  # lost right after step 0 finished

    controller._checkpoint = checkpoint_then_lose
    try:
        report = await run_task(controller, db, task_pk)
    finally:
        await db.close()

    assert report.outcome.value == "stopped"
    assert registry.calls == ["probe"], "only the step that ran before the loss may have run"
    assert "No new operation was started" in " ".join(report.blockers)
    statuses = await plan_statuses(file_factory, task_pk)
    assert statuses == ["completed", "pending", "pending"], (
        "the next step must not even be marked running by a run that lost the task"
    )
    row = await lease_row(file_factory, task_pk)
    assert row.worker_id == "w-other" and row.released_at is None


async def test_renewal_failure_stops_new_side_effects_and_marks_the_call_in_flight(
    file_factory, monkeypatch
):
    from backend.app.tools.base import ToolResult

    started = asyncio.Event()
    real = LeaseHeartbeat._renew_once

    async def breaks_once_the_tool_is_running(self):
        if started.is_set():
            raise ConnectionError("database unreachable")
        return await real(self)  # healthy until the call is in flight

    async def slow_tool(n, name):
        started.set()
        await asyncio.sleep(1.8)  # outlasts the 1.2 s lease while no renewal can succeed
        return ToolResult.success({"ok": True})

    controller, db, task_pk, registry, _ = await build_run(
        file_factory, 2, slow_tool, ttl_minutes=0.02, interval=0.1
    )
    monkeypatch.setattr(LeaseHeartbeat, "_renew_once", breaks_once_the_tool_is_running)
    try:
        report = await run_task(controller, db, task_pk)
    finally:
        await db.close()

    assert report.outcome.value == "stopped"
    assert registry.calls == ["probe"]
    assert "UNKNOWN" in " ".join(report.blockers)
    assert await plan_statuses(file_factory, task_pk) == ["running", "pending"]
    assert not live_heartbeat_tasks()


async def test_cancelling_a_run_stops_the_heartbeat_and_releases_the_lease(file_factory):
    from backend.app.tools.base import ToolResult

    started = asyncio.Event()

    async def hangs(n, name):
        started.set()
        await asyncio.sleep(3600)
        return ToolResult.success({})

    controller, db, task_pk, registry, _ = await build_run(
        file_factory, 1, hangs, ttl_minutes=30, interval=0.05
    )
    run = asyncio.get_running_loop().create_task(run_task(controller, db, task_pk))
    await asyncio.wait_for(started.wait(), 10)
    assert live_heartbeat_tasks()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    await db.close()

    assert not live_heartbeat_tasks(), "no heartbeat may outlive its run"
    assert controller._heartbeat is None
    assert (await lease_row(file_factory, task_pk)).released_at is not None


async def test_a_run_without_a_heartbeat_is_unaffected(file_factory):
    """Direct iteration drivers (no `execute_task`) have no lease to protect."""
    controller, db, task_pk, registry, _ = await build_run(file_factory, 1, None)
    try:
        assert controller._heartbeat is None
        await controller._confirm_lease_for_side_effect()
        await controller._confirm_lease_after_side_effect("anything")
    finally:
        await db.close()


# ====================================================================== atomic recovery claim
@pytest.mark.parametrize("existing_lease", ["none", "released"])
async def test_exactly_one_of_many_concurrent_claimants_wins(file_factory, existing_lease):
    async with file_factory() as db:
        task = await make_task(db)
        task_pk = task.id
        if existing_lease == "released":
            mgr = LeaseManager(db)
            first = await mgr.acquire(task, "w-prev")
            await mgr.release(task, first.lease_token)
            await db.commit()

    async def claim(name):
        async with file_factory() as db:
            task = await db.get(Task, task_pk)
            info = await LeaseManager(db).try_claim_for_recovery(task, name)
            await db.commit()
            return info

    results = await asyncio.gather(*[claim(f"recovery-{i}") for i in range(6)])
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"{len(winners)} claimants won the same task"
    row = await lease_row(file_factory, task_pk)
    assert row.worker_id == winners[0].worker_id and row.released_at is None


async def test_a_held_claim_blocks_runs_and_other_recovery_workers(file_factory):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with file_factory() as db:
        task = await make_task(db)
        task_pk = task.id
    await age(file_factory, task_pk)
    async with file_factory() as db:
        task = await db.get(Task, task_pk)
        claim = await LeaseManager(db).try_claim_for_recovery(task, "recovery-a")
        await db.commit()
    assert claim is not None

    async with file_factory() as db:  # a run cannot acquire the task meanwhile
        with pytest.raises(LeaseConflict):
            await LeaseManager(db).acquire(await db.get(Task, task_pk), "run-b")
    assert await recover_interrupted_tasks() == 0  # nor can another recovery pass settle it

    async with file_factory() as db:
        t = await db.get(Task, task_pk)
        assert t.status == TaskStatus.IN_PROGRESS.value and t.retry_count == 0
    row = await lease_row(file_factory, task_pk)
    assert row.lease_token == claim.lease_token and row.released_at is None


async def _gated_orphan_check(monkeypatch, parties):
    """Make every worker finish its orphan check before ANY of them claims: the worst interleaving."""
    barrier = asyncio.Barrier(parties)
    original = LeaseManager.break_if_orphaned

    async def gated(self, task, *args, **kwargs):
        verdict = await original(self, task, *args, **kwargs)
        if verdict is not None:
            await asyncio.wait_for(barrier.wait(), 10)
        return verdict

    monkeypatch.setattr(LeaseManager, "break_if_orphaned", gated)


async def count_events(factory, task_pk, to_status):
    async with factory() as db:
        events = (await db.execute(select(TaskEvent).where(TaskEvent.task_id == task_pk))).scalars().all()
    return sum(
        1 for e in events
        if e.event_type == "STATUS_TRANSITION" and (e.payload or {}).get("to") == to_status
    )


@pytest.mark.parametrize("workers", [2, 4])
async def test_concurrent_recovery_workers_retry_the_same_task_exactly_once(
    file_factory, monkeypatch, workers
):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with file_factory() as db:
        task = await make_task(db)  # IN_PROGRESS, no lease at all: nothing for a worker to break
        task_pk = task.id
    await age(file_factory, task_pk)
    await _gated_orphan_check(monkeypatch, workers)

    results = await asyncio.gather(*[recover_interrupted_tasks() for _ in range(workers)])

    assert sum(results) == 1, f"queued {results}: the task was retried more than once"
    async with file_factory() as db:
        t = await db.get(Task, task_pk)
    assert t.status == TaskStatus.RETRYING.value
    assert t.retry_count == 1, "the interruption must be counted once"
    assert await count_events(file_factory, task_pk, TaskStatus.RETRYING.value) == 1
    row = await lease_row(file_factory, task_pk)
    assert row.released_at is not None, "the winner's claim is released with its outcome"


async def test_concurrent_recovery_workers_race_for_a_released_lease_too(file_factory, monkeypatch):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with file_factory() as db:
        task = await make_task(db)
        mgr = LeaseManager(db)
        old = await mgr.acquire(task, "w-dead")
        await mgr.release(task, old.lease_token)
        await db.commit()
        task_pk = task.id
    await age(file_factory, task_pk)
    await expire(file_factory, task_pk)  # old release long ago
    async with file_factory() as db:
        await db.execute(
            update(ExecutionLease).where(ExecutionLease.task_id == task_pk)
            .values(released_at=datetime.now(timezone.utc) - timedelta(seconds=600))
        )
        await db.commit()
    await _gated_orphan_check(monkeypatch, 3)

    results = await asyncio.gather(*[recover_interrupted_tasks() for _ in range(3)])
    assert sum(results) == 1
    async with file_factory() as db:
        assert (await db.get(Task, task_pk)).retry_count == 1
    assert await count_events(file_factory, task_pk, TaskStatus.RETRYING.value) == 1


async def test_concurrent_recovery_parks_a_side_effecting_task_once(file_factory, monkeypatch):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with file_factory() as db:
        task = await make_task(db)
        db.add(
            ExecutionPlan(
                task_id=task.id, revision=1, is_current=True, objective="o",
                steps=[{"index": 0, "kind": "transact", "status": "running", "description": "send"}],
            )
        )
        await db.commit()
        task_pk = task.id
    await age(file_factory, task_pk)
    await _gated_orphan_check(monkeypatch, 2)

    results = await asyncio.gather(recover_interrupted_tasks(), recover_interrupted_tasks())

    assert sum(results) == 0, "a possibly executed transaction is never queued for replay"
    async with file_factory() as db:
        t = await db.get(Task, task_pk)
    assert t.status == TaskStatus.NEEDS_USER_ACTION.value
    assert t.retry_count == 0
    assert await count_events(file_factory, task_pk, TaskStatus.NEEDS_USER_ACTION.value) == 1, (
        "two interventions would be raised for one task"
    )


async def test_a_second_recovery_pass_does_not_retry_an_already_recovered_task(file_factory):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with file_factory() as db:
        task = await make_task(db)
        task_pk = task.id
    await age(file_factory, task_pk)
    assert await recover_interrupted_tasks() == 1
    assert await recover_interrupted_tasks() == 0
    async with file_factory() as db:
        t = await db.get(Task, task_pk)
    assert t.status == TaskStatus.RETRYING.value and t.retry_count == 1


async def test_retries_exhausted_still_fails_the_task_exactly_once(file_factory, monkeypatch):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with file_factory() as db:
        task = await make_task(db, retry_count=3, max_retries=3)
        task_pk = task.id
    await age(file_factory, task_pk)
    await _gated_orphan_check(monkeypatch, 2)

    await asyncio.gather(recover_interrupted_tasks(), recover_interrupted_tasks())
    async with file_factory() as db:
        t = await db.get(Task, task_pk)
    assert t.status == TaskStatus.FAILED.value and t.retry_count == 3
    assert await count_events(file_factory, task_pk, TaskStatus.FAILED.value) == 1


async def test_a_task_that_fails_to_recover_is_released_and_does_not_strand_the_rest(
    file_factory, monkeypatch
):
    from backend.app.scheduler import worker as worker_module

    async with file_factory() as db:
        bad = await make_task(db)
        db.add(
            ExecutionPlan(
                task_id=bad.id, revision=1, is_current=True, objective="o",
                steps=[{"index": 0, "kind": "transact", "status": "running", "description": "send"}],
            )
        )
        good = await make_task(db)
        await db.commit()
        bad_pk, good_pk = bad.id, good.id
    await age(file_factory, bad_pk)
    await age(file_factory, good_pk)

    async def explode(*args, **kwargs):
        raise RuntimeError("inbox unavailable")

    monkeypatch.setattr(worker_module, "park_for_intervention", explode)
    assert await worker_module.recover_interrupted_tasks() == 1  # the good one still recovered

    async with file_factory() as db:
        assert (await db.get(Task, bad_pk)).status == TaskStatus.IN_PROGRESS.value
        assert (await db.get(Task, good_pk)).status == TaskStatus.RETRYING.value
    assert (await lease_row(file_factory, bad_pk)).released_at is not None, (
        "a failed recovery must not leave the task claimed until the claim times out"
    )

    # (the real parking path is covered by test_concurrent_recovery_parks_a_side_effecting_task_once)


async def test_recovery_still_leaves_a_live_worker_alone_under_the_new_claim(file_factory):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with file_factory() as db:
        task = await make_task(db)
        task_pk = task.id
    live = await acquire_lease(file_factory, task_pk, ttl_minutes=30, worker="w-live")
    assert await recover_interrupted_tasks() == 0
    row = await lease_row(file_factory, task_pk)
    assert row.lease_token == live.lease_token and row.worker_id == "w-live"
    async with file_factory() as db:
        t = await db.get(Task, task_pk)
    assert t.status == TaskStatus.IN_PROGRESS.value and t.retry_count == 0


async def test_cancelling_recovery_releases_its_claim_and_is_not_swallowed(file_factory, monkeypatch):
    from backend.app.scheduler import worker as worker_module

    async with file_factory() as db:
        task = await make_task(db)
        db.add(
            ExecutionPlan(
                task_id=task.id, revision=1, is_current=True, objective="o",
                steps=[{"index": 0, "kind": "transact", "status": "running", "description": "send"}],
            )
        )
        await db.commit()
        task_pk = task.id
    await age(file_factory, task_pk)

    in_park = asyncio.Event()

    async def hangs(*args, **kwargs):
        in_park.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(worker_module, "park_for_intervention", hangs)
    run = asyncio.get_running_loop().create_task(worker_module.recover_interrupted_tasks())
    await asyncio.wait_for(in_park.wait(), 10)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    async with file_factory() as db:
        t = await db.get(Task, task_pk)
    assert t.status == TaskStatus.IN_PROGRESS.value and t.retry_count == 0
    assert (await lease_row(file_factory, task_pk)).released_at is not None, (
        "a cancelled recovery must not leave the task claimed until the claim times out"
    )
