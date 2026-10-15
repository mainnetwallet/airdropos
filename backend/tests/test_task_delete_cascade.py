"""Deleting a task removes its dependent rows on SQLite, where foreign keys are not enforced."""

import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.database.session import Base
from backend.app.models import ExecutionPlan, PlanRevision, Task, TaskEvent
from backend.app.tasks.deletion import delete_task_cascade
from backend.app.tasks.manager import TaskManager


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


async def _count(db, model) -> int:
    return await db.scalar(select(func.count(model.id)))


async def _task_with_plan(db, title):
    task = await TaskManager(db).create_task(title=title)
    plan = ExecutionPlan(task_id=task.id, objective="o", steps=[])
    db.add(plan)
    await db.flush()
    db.add(PlanRevision(plan_id=plan.id, task_id=task.id, to_revision=1, reason="r"))
    await db.commit()
    return task


async def test_sqlite_does_not_enforce_foreign_keys_here(factory):
    async with factory() as db:
        assert (await db.execute(text("PRAGMA foreign_keys"))).scalar() == 0


async def test_delete_removes_children_and_grandchildren(factory):
    async with factory() as db:
        task = await _task_with_plan(db, "a")
        assert await _count(db, TaskEvent) >= 1

        await delete_task_cascade(db, task.id)
        await db.commit()

    async with factory() as db:
        assert await _count(db, Task) == 0
        assert await _count(db, TaskEvent) == 0
        assert await _count(db, ExecutionPlan) == 0
        assert await _count(db, PlanRevision) == 0


async def test_delete_leaves_other_tasks_untouched(factory):
    async with factory() as db:
        keep = await _task_with_plan(db, "keep")
        gone = await _task_with_plan(db, "gone")
        keep_events = await db.scalar(
            select(func.count(TaskEvent.id)).where(TaskEvent.task_id == keep.id)
        )

        await delete_task_cascade(db, gone.id)
        await db.commit()

    async with factory() as db:
        assert await _count(db, Task) == 1
        assert await db.scalar(
            select(func.count(TaskEvent.id)).where(TaskEvent.task_id == keep.id)
        ) == keep_events
        assert await _count(db, ExecutionPlan) == 1
        assert await _count(db, PlanRevision) == 1


async def test_child_task_survives_with_parent_cleared(factory):
    async with factory() as db:
        parent = await TaskManager(db).create_task(title="parent")
        child = await TaskManager(db).create_task(title="child", parent_task_id=parent.id)
        await db.commit()

        await delete_task_cascade(db, parent.id)
        await db.commit()

    async with factory() as db:
        survivor = await db.get(Task, child.id)
        assert survivor is not None and survivor.parent_task_id is None
