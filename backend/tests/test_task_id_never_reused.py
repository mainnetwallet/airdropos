"""A deleted task's AIR-YYYY-NNNNNN ID must never be issued again."""

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.database.session import Base
from backend.app.models import Task
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


async def _new(factory) -> str:
    async with factory() as db:
        task = await TaskManager(db).create_task(title="t")
        await db.commit()
        return task.task_id


async def _delete_all(factory) -> None:
    async with factory() as db:
        await db.execute(delete(Task))
        await db.commit()


async def test_ids_increase_without_deletes(factory):
    ids = [await _new(factory) for _ in range(3)]
    assert [i.rsplit("-", 1)[1] for i in ids] == ["000001", "000002", "000003"]


async def test_deleted_id_is_not_reused(factory):
    first = await _new(factory)
    await _delete_all(factory)
    second = await _new(factory)
    assert second != first
    assert second.endswith("000002")


async def test_deleting_the_newest_does_not_roll_the_counter_back(factory):
    await _new(factory)
    await _new(factory)
    await _delete_all(factory)
    assert (await _new(factory)).endswith("000003")


async def test_existing_database_without_a_counter_continues_after_highest(factory):
    # A database from before the counter: tasks exist, the counter row does not.
    await _new(factory)
    await _new(factory)
    from sqlalchemy import text

    async with factory() as db:
        await db.execute(text("DELETE FROM task_id_counters"))
        await db.commit()
    assert (await _new(factory)).endswith("000003")
