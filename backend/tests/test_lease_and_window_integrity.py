"""Regression tests: lease ownership, competing workers, recovery races, window accounting.

Each test pins one defect that was present before the fix:

* `renew` accepted a released or expired lease as long as the token matched, so a stale worker
  could keep writing over whoever had taken the task.
* A live lease could be "re-acquired" by any caller using the holder's own id, and worker ids
  (`worker-{pid}`, `controller-{id(self)}`) were not unique.
* Start-up recovery released every IN_PROGRESS task's lease, live ones included.
* `execution_windows` was incremented in an uncommitted transaction, so cancellation or an
  exception before the first commit lost the window.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from backend.app.models import ExecutionLease, Task
from backend.app.tasks.concurrency import (
    LeaseConflict,
    LeaseManager,
    PROCESS_INSTANCE_ID,
    new_run_worker_id,
)
from backend.app.tasks.state_machine import TaskStatus

_n = 0


async def make_task(db, status=TaskStatus.IN_PROGRESS, **kw) -> Task:
    global _n
    _n += 1
    task = Task(task_id=f"AIR-TEST-{_n:05d}", title="t", status=status.value, **kw)
    db.add(task)
    await db.commit()
    return task


async def expire(factory, task_pk, *, seconds_ago=60):
    async with factory() as db:
        await db.execute(
            update(ExecutionLease)
            .where(ExecutionLease.task_id == task_pk)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago))
        )
        await db.commit()


async def lease_row(factory, task_pk) -> ExecutionLease:
    async with factory() as db:
        return await db.scalar(select(ExecutionLease).where(ExecutionLease.task_id == task_pk))


# ---------------------------------------------------------------- renew
async def test_renew_extends_a_live_lease(factory):
    async with factory() as db:
        task = await make_task(db)
        lease = await LeaseManager(db).acquire(task, "w-a", ttl_minutes=1)
        before = (await lease_row_in(db, task)).expires_at
        assert await LeaseManager(db).renew(task, lease.lease_token, ttl_minutes=30) is True
        await db.commit()
    assert (await lease_row(factory, task.id)).expires_at > before


async def lease_row_in(db, task):
    return await db.scalar(select(ExecutionLease).where(ExecutionLease.task_id == task.id))


async def test_renew_rejects_a_released_lease_even_with_the_right_token(factory):
    async with factory() as db:
        task = await make_task(db)
        mgr = LeaseManager(db)
        lease = await mgr.acquire(task, "w-a")
        await mgr.release(task, lease.lease_token)
        await db.commit()
        assert await mgr.renew(task, lease.lease_token) is False
        await db.commit()
    row = await lease_row(factory, task.id)
    assert row.released_at is not None, "a rejected renewal must not revive the lease"


async def test_renew_rejects_an_expired_lease_even_with_the_right_token(factory):
    async with factory() as db:
        task = await make_task(db)
        lease = await LeaseManager(db).acquire(task, "w-a")
        await db.commit()
        task_pk, token = task.id, lease.lease_token
    await expire(factory, task_pk)
    async with factory() as db:
        task = await db.get(Task, task_pk)
        assert await LeaseManager(db).renew(task, token) is False
        await db.commit()
    row = await lease_row(factory, task_pk)
    assert row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)


async def test_a_superseded_worker_cannot_renew_over_its_successor(factory):
    async with factory() as db:
        task = await make_task(db)
        old = await LeaseManager(db).acquire(task, "w-old")
        await db.commit()
        task_pk, old_token = task.id, old.lease_token
    await expire(factory, task_pk)

    async with factory() as db:
        task = await db.get(Task, task_pk)
        new = await LeaseManager(db).acquire(task, "w-new")  # takes the stale lease over
        await db.commit()
    successor = await lease_row(factory, task_pk)

    async with factory() as db:
        task = await db.get(Task, task_pk)
        assert await LeaseManager(db).renew(task, old_token) is False
        await db.commit()

    after = await lease_row(factory, task_pk)
    assert after.worker_id == "w-new" and after.lease_token == new.lease_token
    assert after.expires_at == successor.expires_at, "the stale renewal must not touch the new lease"
    assert after.released_at is None


# ---------------------------------------------------------------- competing workers
async def test_a_live_lease_conflicts_even_for_the_holders_own_id(factory):
    async with factory() as db:
        task = await make_task(db)
        task_pk = task.id
        mgr = LeaseManager(db)
        first = await mgr.acquire(task, "same-id")
        await db.commit()
        with pytest.raises(LeaseConflict):
            await mgr.acquire(task, "same-id")  # a duplicate with a colliding id
        await db.rollback()
    row = await lease_row(factory, task_pk)
    assert row.lease_token == first.lease_token, "the original run keeps its token"


async def test_a_different_worker_is_refused_while_the_lease_is_live(factory):
    async with factory() as db:
        task = await make_task(db)
        await LeaseManager(db).acquire(task, "w-a")
        await db.commit()
    async with factory() as db:
        task = await db.get(Task, task.id)
        with pytest.raises(LeaseConflict):
            await LeaseManager(db).acquire(task, "w-b")


async def test_a_released_lease_can_be_taken_by_the_next_worker(factory):
    async with factory() as db:
        task = await make_task(db)
        mgr = LeaseManager(db)
        first = await mgr.acquire(task, "w-a")
        await mgr.release(task, first.lease_token)
        second = await mgr.acquire(task, "w-b")
        await db.commit()
    assert second.lease_token != first.lease_token
    assert (await lease_row(factory, task.id)).worker_id == "w-b"


def test_worker_and_run_ids_are_unique_per_process_and_per_run():
    import os

    assert str(os.getpid()) in PROCESS_INSTANCE_ID
    ids = {new_run_worker_id() for _ in range(200)}
    assert len(ids) == 200, "two runs must never share a holder id"
    assert all(i.startswith("controller-") and len(i) <= 128 for i in ids)


# ---------------------------------------------------------------- stale-lease break race
async def test_breaking_a_stale_lease_cannot_hit_a_lease_taken_over_meanwhile(factory):
    async with factory() as db:
        task = await make_task(db)
        await LeaseManager(db).acquire(task, "w-dead")
        await db.commit()
        task_pk = task.id
    await expire(factory, task_pk)

    async with factory() as scanner:
        stale = await LeaseManager(scanner).find_stale_leases()
        assert len(stale) == 1
        seen = stale[0]

        # Between listing and breaking, a live worker takes the task over.
        async with factory() as other:
            t = await other.get(Task, task_pk)
            await LeaseManager(other).acquire(t, "w-live")
            await other.commit()

        broke = await LeaseManager(scanner)._break_if_stale(seen, "stale")
        await scanner.commit()

    assert broke is False
    row = await lease_row(factory, task_pk)
    assert row.worker_id == "w-live" and row.released_at is None


async def test_break_stale_leases_parks_a_task_with_an_uncertain_side_effect(factory):
    from backend.app.models import ExecutionPlan

    async with factory() as db:
        task = await make_task(db)
        db.add(
            ExecutionPlan(
                task_id=task.id, revision=1, is_current=True, objective="o",
                steps=[{"index": 0, "kind": "transact", "status": "running", "description": "send"}],
            )
        )
        await LeaseManager(db).acquire(task, "w-dead")
        await db.commit()
        task_pk = task.id
    await expire(factory, task_pk)

    async with factory() as db:
        result = await LeaseManager(db).break_stale_leases()
        await db.commit()
    assert result["parked_for_review"] == 1 and result["recovered"] == 0
    async with factory() as db:
        assert (await db.get(Task, task_pk)).status != TaskStatus.RETRYING.value


# ---------------------------------------------------------------- recovery
async def _age(factory, task_pk, seconds=600):
    async with factory() as db:
        await db.execute(
            update(Task)
            .where(Task.id == task_pk)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=seconds))
        )
        await db.commit()


async def test_recovery_does_not_steal_a_task_a_live_worker_holds(factory):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with factory() as db:
        task = await make_task(db)
        live = await LeaseManager(db).acquire(task, "w-live")
        await db.commit()
        task_pk = task.id

    assert await recover_interrupted_tasks() == 0

    async with factory() as db:
        assert (await db.get(Task, task_pk)).status == TaskStatus.IN_PROGRESS.value
    row = await lease_row(factory, task_pk)
    assert row.released_at is None and row.lease_token == live.lease_token
    async with factory() as db:  # and the live owner can still renew
        task = await db.get(Task, task_pk)
        assert await LeaseManager(db).renew(task, live.lease_token) is True


async def test_recovery_requeues_a_task_whose_lease_expired(factory):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with factory() as db:
        task = await make_task(db)
        dead = await LeaseManager(db).acquire(task, "w-dead")
        await db.commit()
        task_pk = task.id
    await expire(factory, task_pk)

    assert await recover_interrupted_tasks() == 1
    async with factory() as db:
        assert (await db.get(Task, task_pk)).status == TaskStatus.RETRYING.value
        # the dead owner's token is now useless
        assert await LeaseManager(db).renew(await db.get(Task, task_pk), dead.lease_token) is False


async def test_recovery_leaves_a_freshly_claimed_task_that_has_no_lease_yet(factory):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with factory() as db:
        task = await make_task(db)  # claimed (IN_PROGRESS), lease not taken yet
        task_pk = task.id

    assert await recover_interrupted_tasks() == 0
    async with factory() as db:
        assert (await db.get(Task, task_pk)).status == TaskStatus.IN_PROGRESS.value

    await _age(factory, task_pk)  # genuinely abandoned
    assert await recover_interrupted_tasks() == 1


async def test_recovery_leaves_a_rerun_task_with_an_old_released_lease_alone(factory):
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with factory() as db:
        task = await make_task(db)
        mgr = LeaseManager(db)
        first = await mgr.acquire(task, "w-a")
        await mgr.release(task, first.lease_token)  # the previous attempt ended
        await db.commit()
        task_pk = task.id
    # ... and the task was just re-claimed: IN_PROGRESS again, new lease not taken yet.
    async with factory() as db:
        await db.execute(update(Task).where(Task.id == task_pk).values(priority=5))
        await db.commit()

    assert await recover_interrupted_tasks() == 0


async def test_recovery_never_replays_a_possibly_executed_transaction(factory):
    from backend.app.models import ExecutionPlan
    from backend.app.scheduler.worker import recover_interrupted_tasks

    async with factory() as db:
        task = await make_task(db)
        db.add(
            ExecutionPlan(
                task_id=task.id, revision=1, is_current=True, objective="o",
                steps=[{"index": 0, "kind": "transact", "status": "running", "description": "send"}],
            )
        )
        await LeaseManager(db).acquire(task, "w-dead")
        await db.commit()
        task_pk = task.id
    await expire(factory, task_pk)

    assert await recover_interrupted_tasks() == 0, "nothing may be queued for blind replay"
    async with factory() as db:
        status = (await db.get(Task, task_pk)).status
    assert status not in (TaskStatus.RETRYING.value, TaskStatus.READY.value, TaskStatus.IN_PROGRESS.value)


# ---------------------------------------------------------------- window accounting
def _controller(db):
    from backend.app.agent.controller import AgentController

    c = object.__new__(AgentController)
    c.db = db
    c.leases = LeaseManager(db)
    c._window_counted = False
    c._window_start_revision = None
    return c


async def windows(factory, task_pk) -> int:
    async with factory() as db:
        return (await db.get(Task, task_pk)).execution_windows


async def test_a_window_is_counted_once_and_durably_at_open(factory):
    async with factory() as db:
        task = await make_task(db)
        task_pk = task.id
        c = _controller(db)
        await c._count_window(task)
        await c._count_window(task)  # reached twice: still one window
        await db.rollback()  # nothing else was committed, and the count must survive anyway
    assert await windows(factory, task_pk) == 1


async def test_a_recovered_task_opens_a_new_window_without_losing_the_old_one(factory):
    async with factory() as db:
        task = await make_task(db)
        task_pk = task.id
        await _controller(db)._count_window(task)
    async with factory() as db:  # the recovered re-run: a new controller, a new window
        task = await db.get(Task, task_pk)
        await _controller(db)._count_window(task)
    assert await windows(factory, task_pk) == 2


@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_cancellation_or_an_exception_keeps_the_window_and_frees_the_lease(factory, failure):
    from backend.app.agent.controller import AgentController

    async with factory() as db:
        task = await make_task(db)
        task_pk = task.id
        c = _controller(db)

        async def plan(*a, **k):
            return type("Spec", (), {"steps": []})()

        async def noop(*a, **k):
            return None

        async def boom(*a, **k):
            raise failure("interrupted mid-window")

        c._ensure_plan, c._plan_revision, c._resume_iteration = plan, noop, noop
        c._rearm_policy_refused_steps, c._run_window = noop, boom
        c._mark_window_start = lambda t, p, i: AgentController._mark_window_start(c, t, p, i)
        c._record_blocker = noop

        with pytest.raises(failure):
            await AgentController.execute_task(c, task)

    assert await windows(factory, task_pk) == 1, "the window was opened, so it is counted"
    row = await lease_row(factory, task_pk)
    assert row.released_at is not None, "a dead run's lease must not stay live for its TTL"


async def test_a_run_that_fails_before_planning_does_not_count_a_window(factory):
    from backend.app.agent.controller import AgentController

    async with factory() as db:
        task = await make_task(db)
        task_pk = task.id
        c = _controller(db)

        async def boom(*a, **k):
            raise RuntimeError("planning blew up")

        c._ensure_plan = boom
        with pytest.raises(RuntimeError):
            await AgentController.execute_task(c, task)
    assert await windows(factory, task_pk) == 0
