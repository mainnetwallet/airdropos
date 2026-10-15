"""Task IDs are AIR-YYYY-MM-DD-NNNNNN (today + 6 random digits) and are never reused."""

import re
from datetime import date

import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.database.session import Base
from backend.app.models import IssuedTaskId, Task
from backend.app.tasks import id_generator
from backend.app.tasks.id_generator import format_task_id, validate_task_id
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


async def _delete_all_tasks(factory) -> None:
    async with factory() as db:
        await db.execute(delete(Task))
        await db.commit()


async def test_format_is_today_plus_six_random_digits(factory):
    task_id = await _new(factory)
    assert re.fullmatch(r"AIR-\d{4}-\d{2}-\d{2}-\d{6}", task_id)
    assert task_id.startswith(f"AIR-{id_generator.today().isoformat()}-")
    assert validate_task_id(task_id)


async def test_ids_are_random_not_sequential(factory):
    ids = [await _new(factory) for _ in range(5)]
    assert len(set(ids)) == 5
    suffixes = [int(i[-6:]) for i in ids]
    assert suffixes != sorted(suffixes) or suffixes != list(range(suffixes[0], suffixes[0] + 5))


async def test_deleted_id_is_never_issued_again(factory, monkeypatch):
    first = await _new(factory)
    await _delete_all_tasks(factory)

    # Force the random source to offer the deleted ID first, then a different one.
    offers = iter([first[-6:], "123456"])
    monkeypatch.setattr(id_generator, "random_suffix", lambda: next(offers))

    second = await _new(factory)
    assert second != first
    assert second.endswith("123456")


async def test_every_issued_id_stays_in_the_ledger_after_delete(factory):
    first = await _new(factory)
    await _delete_all_tasks(factory)
    async with factory() as db:
        ledger = (await db.scalars(select(IssuedTaskId.task_id))).all()
    assert first in ledger


def test_validate_accepts_dated_and_legacy_ids_only():
    assert validate_task_id("AIR-2026-10-15-483920")
    assert validate_task_id("AIR-2026-000001")  # legacy, still readable
    assert not validate_task_id("AIR-2026-13-40-483920")  # not a real date
    assert not validate_task_id("AIR-2026-10-15-48392")  # 5 digits
    assert not validate_task_id("")


def test_format_uses_the_given_day():
    assert format_task_id(date(2026, 8, 15), "000042") == "AIR-2026-08-15-000042"
