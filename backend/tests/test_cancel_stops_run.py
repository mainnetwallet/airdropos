"""Cancelling or deleting a task stops a run that is in flight."""

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.agent.controller import AgentController
from backend.app.database.session import Base
from backend.app.models import Task
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _running_task(factory):
    async with factory() as db:
        task = await TaskManager(db).create_task(title="t", status=TaskStatus.IN_PROGRESS)
        await db.commit()
        return task.id


async def test_running_task_is_not_stopped(factory):
    pk = await _running_task(factory)
    async with factory() as db:
        task = await db.get(Task, pk)
        assert await AgentController(db)._external_stop_reason(task) is None


async def test_cancelled_by_operator_stops_the_run(factory):
    pk = await _running_task(factory)
    async with factory() as worker_db:
        task = await worker_db.get(Task, pk)  # the worker holds this (IN_PROGRESS) copy

        async with factory() as api_db:  # the dashboard cancels it in another session
            api_task = await api_db.get(Task, pk)
            await TaskManager(api_db).transition(api_task, TaskStatus.CANCELLED, reason="op")
            await api_db.commit()

        assert task.status == TaskStatus.IN_PROGRESS.value  # stale in memory
        reason = await AgentController(worker_db)._external_stop_reason(task)
        assert reason and "cancelled" in reason


async def test_deleted_task_stops_the_run(factory):
    pk = await _running_task(factory)
    async with factory() as worker_db:
        task = await worker_db.get(Task, pk)
        async with factory() as api_db:
            await api_db.execute(delete(Task).where(Task.id == pk))
            await api_db.commit()
        reason = await AgentController(worker_db)._external_stop_reason(task)
        assert reason and "deleted" in reason
