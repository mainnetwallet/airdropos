"""Regression tests for SQLite contention and worker task execution (commit a1633e7 and follow-up).

Isolated temporary SQLite files only; the real database is never touched.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agent.controller import AgentController, LoopOutcome, LoopReport, StepResult
from backend.app.database import session as db_session
from backend.app.database.session import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    Base,
    configure_sqlite_connection,
    engine_kwargs_for,
)
from backend.app.models import (
    ExecutionLease,
    IntegrationHealth,
    Task,
    TaskCheckpoint,
    TaskEvent,
    TaskExecution,
    User,
    UserSession,
)
from backend.app.scheduler import worker, worker_status
from backend.app.security.auth import (
    LAST_USED_WRITE_INTERVAL,
    AuthError,
    Scope,
    generate_token,
    hash_token,
    resolve_session,
)
from backend.app.security.emergency_stop import emergency_stop
from backend.app.tasks.concurrency import LeaseManager
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
async def factory(tmp_path):
    """File-backed SQLite engine configured exactly like the app's SQLite engine."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        connect_args={"timeout": SQLITE_BUSY_TIMEOUT_SECONDS},
    )
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


@pytest.fixture
def worker_env(factory, monkeypatch):
    """Point the worker and heartbeat at the temp database; make its sleeps instant."""
    monkeypatch.setattr(worker, "AsyncSessionLocal", factory)
    monkeypatch.setattr(worker_status, "AsyncSessionLocal", factory)
    worker._shutdown_event.clear()
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay=0, *a, **k):
        worker._shutdown_event.set()  # one loop iteration per test
        await real_sleep(0)

    monkeypatch.setattr(worker.asyncio, "sleep", fast_sleep)
    yield factory
    worker._shutdown_event.clear()


async def _ready_task(factory, **kwargs) -> uuid.UUID:
    async with factory() as db:
        task = await TaskManager(db).create_task(
            "Open Browser Session", status=TaskStatus.READY, **kwargs
        )
        await db.commit()
        return task.id


async def _load(factory, pk) -> Task:
    async with factory() as db:
        return await db.get(Task, pk)


def _fake_controller(behaviour):
    class FakeController:
        def __init__(self, db, registry=None):
            self.db = db

        async def execute_task(self, task):
            return await behaviour(self, task)

    return FakeController


# ----------------------------------------------------------------------
# A. SQLite configuration
# ----------------------------------------------------------------------
async def test_sqlite_pragmas_are_applied(factory):
    async with factory() as db:
        mode = (await db.execute(text("PRAGMA journal_mode"))).scalar()
        timeout = (await db.execute(text("PRAGMA busy_timeout"))).scalar()
    assert str(mode).lower() == "wal"
    assert int(timeout) == SQLITE_BUSY_TIMEOUT_SECONDS * 1000


def test_sqlite_settings_do_not_reach_postgres():
    pg = engine_kwargs_for("postgresql+asyncpg://u:p@localhost/db")
    assert "connect_args" not in pg
    assert {"pool_size", "max_overflow"} <= set(pg)
    lite = engine_kwargs_for("sqlite+aiosqlite:///./x.db")
    assert lite == {"connect_args": {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS}}


def test_busy_timeout_is_below_worker_staleness_window():
    assert SQLITE_BUSY_TIMEOUT_SECONDS < worker_status.STALE_AFTER_SECONDS


async def test_failed_transaction_rolls_back_and_session_stays_usable(factory):
    async with factory() as db:
        db.add(IntegrationHealth(subsystem="a", status="HEALTHY", detail={}, success_count=0, failure_count=0))
        await db.flush()
        await db.rollback()
        assert await db.scalar(select(IntegrationHealth).where(IntegrationHealth.subsystem == "a")) is None
        assert (await db.execute(text("SELECT 1"))).scalar() == 1


# ----------------------------------------------------------------------
# B. Authentication: throttled last_used_at, checks still enforced
# ----------------------------------------------------------------------
async def _session(factory, *, expires=None, revoked=False, last_used=None):
    token = generate_token()
    async with factory() as db:
        user = User(email=f"{uuid.uuid4().hex[:8]}@test.local", hashed_password="x", is_active=True)
        db.add(user)
        await db.flush()
        db.add(
            UserSession(
                user_id=user.id,
                token=hash_token(token),
                expires_at=expires or datetime.now(timezone.utc) + timedelta(hours=1),
                scopes=[Scope.READ.value],
                revoked_at=datetime.now(timezone.utc) if revoked else None,
                last_used_at=last_used,
            )
        )
        await db.commit()
    return token


async def _last_used(factory, token):
    async with factory() as db:
        row = await db.scalar(select(UserSession).where(UserSession.token == hash_token(token)))
        return row.last_used_at


async def test_last_used_written_once_then_throttled(factory):
    token = await _session(factory)
    async with factory() as db:
        await resolve_session(db, token)
        await db.commit()
    first = await _last_used(factory, token)
    assert first is not None
    async with factory() as db:
        await resolve_session(db, token)
        await db.commit()
    assert await _last_used(factory, token) == first  # no second write inside the interval


async def test_last_used_refreshed_after_interval(factory):
    old = datetime.now(timezone.utc) - LAST_USED_WRITE_INTERVAL - timedelta(seconds=5)
    token = await _session(factory, last_used=old)
    async with factory() as db:
        await resolve_session(db, token)
        await db.commit()
    refreshed = await _last_used(factory, token)
    refreshed = refreshed.replace(tzinfo=timezone.utc) if refreshed.tzinfo is None else refreshed
    assert refreshed > old


async def test_throttling_does_not_weaken_session_checks(factory):
    recent = datetime.now(timezone.utc)
    revoked = await _session(factory, revoked=True, last_used=recent)
    expired = await _session(factory, expires=datetime.now(timezone.utc) - timedelta(minutes=1), last_used=recent)
    async with factory() as db:
        for token in (revoked, expired, "not-a-real-token"):
            with pytest.raises(AuthError):
                await resolve_session(db, token)


# ----------------------------------------------------------------------
# C. Worker
# ----------------------------------------------------------------------
def test_retry_backoff_is_exponential_and_bounded():
    assert worker.retry_delay_seconds(0) == worker.RETRY_BACKOFF_BASE_SECONDS
    assert worker.retry_delay_seconds(1) == worker.RETRY_BACKOFF_BASE_SECONDS * 2
    assert worker.retry_delay_seconds(500) == worker.RETRY_BACKOFF_MAX_SECONDS
    assert worker.retry_delay_seconds(-3) == worker.RETRY_BACKOFF_BASE_SECONDS


async def test_claim_is_committed_and_survives_execution_failure(worker_env):
    pk = await _ready_task(worker_env)

    async def boom(self, task):
        raise RuntimeError("browser exploded")

    worker.AgentController = _fake_controller(boom)
    await worker.task_execution_loop()

    task = await _load(worker_env, pk)
    assert task.status == TaskStatus.RETRYING.value  # not silently back to READY
    assert task.retry_count == 1
    assert task.next_execution_at is not None
    assert task.error_details["type"] == "RuntimeError"
    async with worker_env() as db:
        kinds = [e.event_type for e in (await db.execute(select(TaskEvent).where(TaskEvent.task_id == pk))).scalars()]
    assert "TASK_CLAIMED" in kinds and "STATUS_TRANSITION" in kinds


async def test_exhausted_retries_end_in_failed(worker_env):
    pk = await _ready_task(worker_env, max_retries=0)

    async def boom(self, task):
        raise RuntimeError("still broken")

    worker.AgentController = _fake_controller(boom)
    await worker.task_execution_loop()
    assert (await _load(worker_env, pk)).status == TaskStatus.FAILED.value


async def test_failed_outcome_does_not_leave_task_in_progress(worker_env):
    pk = await _ready_task(worker_env)

    async def failed(self, task):
        return LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED, failures=["LLM error"])

    worker.AgentController = _fake_controller(failed)
    await worker.task_execution_loop()
    task = await _load(worker_env, pk)
    assert task.status == TaskStatus.RETRYING.value and task.retry_count == 1


async def test_failure_details_are_redacted(worker_env):
    pk = await _ready_task(worker_env)

    async def boom(self, task):
        raise RuntimeError("auth failed token=supersecretvalue123")

    worker.AgentController = _fake_controller(boom)
    await worker.task_execution_loop()
    assert "supersecretvalue123" not in str((await _load(worker_env, pk)).error_details)


async def test_lease_of_failed_run_does_not_block_retry(worker_env):
    """Leases are committed during a run; a failed run must release its lease."""
    pk = await _ready_task(worker_env)

    async def leased_then_boom(self, task):
        await LeaseManager(self.db).acquire(task, "controller-dead")
        await self.db.commit()
        raise RuntimeError("crash after lease")

    worker.AgentController = _fake_controller(leased_then_boom)
    await worker.task_execution_loop()

    async with worker_env() as db:
        task = await db.get(Task, pk)
        await LeaseManager(db).acquire(task, "controller-next")  # raises LeaseConflict if still live


async def test_startup_recovery_releases_leases_of_interrupted_runs(worker_env):
    pk = await _ready_task(worker_env)
    async with worker_env() as db:
        task = await db.get(Task, pk)
        task.status = TaskStatus.IN_PROGRESS.value
        await LeaseManager(db).acquire(task, "controller-killed")
        await db.commit()
    assert await worker.recover_interrupted_tasks() == 1
    assert (await _load(worker_env, pk)).status == TaskStatus.RETRYING.value
    async with worker_env() as db:
        task = await db.get(Task, pk)
        await LeaseManager(db).acquire(task, "controller-new")


async def test_heartbeat_is_not_blocked_while_task_executes(worker_env):
    pk = await _ready_task(worker_env)
    seen = {}

    async def run_with_heartbeat(self, task):
        await worker_status.publish_once()  # separate connection, mid-execution
        async with worker_env() as db:
            seen["row"] = await db.scalar(select(IntegrationHealth).where(IntegrationHealth.subsystem == "worker"))
            seen["status"] = (await db.get(Task, pk)).status
        return LoopReport(task_id=task.task_id, outcome=LoopOutcome.NEEDS_USER_ACTION)

    worker.AgentController = _fake_controller(run_with_heartbeat)
    await worker.task_execution_loop()
    assert seen["row"] is not None
    assert seen["status"] == TaskStatus.IN_PROGRESS.value  # the claim was already visible to others


async def test_task_is_claimed_exactly_once_under_concurrent_polling(factory):
    pk = await _ready_task(factory)

    async def poll(name):
        async with factory() as db:
            try:
                task = await TaskManager(db).claim_next_task(name)
                await db.commit()
                return task
            except OperationalError:
                await db.rollback()
                return None  # lost the SQLite write race: no duplicate claim

    results = await asyncio.gather(*(poll(f"worker-{i}") for i in range(4)))
    assert sum(1 for r in results if r is not None) == 1
    assert (await _load(factory, pk)).status == TaskStatus.IN_PROGRESS.value
    async with factory() as db:
        claims = (await db.execute(select(TaskEvent).where(TaskEvent.event_type == "TASK_CLAIMED"))).scalars().all()
    assert len(claims) == 1


async def test_emergency_stop_prevents_claiming(worker_env):
    pk = await _ready_task(worker_env)
    emergency_stop.activate("test stop", activated_by="operator")

    async def must_not_run(self, task):
        raise AssertionError("executed while emergency stop was active")

    worker.AgentController = _fake_controller(must_not_run)
    await worker.task_execution_loop()
    assert (await _load(worker_env, pk)).status == TaskStatus.READY.value


# ----------------------------------------------------------------------
# D. Controller: per-iteration commits survive a later failure
# ----------------------------------------------------------------------
async def test_progress_and_checkpoints_survive_later_failure(factory, monkeypatch):
    pk = await _ready_task(factory)
    async with factory() as db:
        task = await db.get(Task, pk)
        controller = AgentController(db, registry=None)
        calls = {"n": 0}

        async def plan(task, report):
            return object()

        async def resume(task):
            return 0

        async def iteration(task, n, report, plan_spec):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("second iteration crashed")
            return StepResult(n, "step", None, None, True, "ok", 0), False, None

        monkeypatch.setattr(controller, "_ensure_plan", plan)
        monkeypatch.setattr(controller, "_resume_iteration", resume)
        monkeypatch.setattr(controller, "_run_iteration", iteration)
        with pytest.raises(RuntimeError):
            await controller.execute_task(task)
        await db.rollback()  # what the worker loop's session close does
        assert (await db.execute(text("SELECT 1"))).scalar() == 1  # session still usable

    async with factory() as db:
        executions = (await db.execute(select(TaskExecution).where(TaskExecution.task_id == pk))).scalars().all()
        checkpoints = (await db.execute(select(TaskCheckpoint).where(TaskCheckpoint.task_id == pk))).scalars().all()
    assert [e.iteration for e in executions] == [1]
    assert len(checkpoints) >= 1
