"""The proof test for the P1 finding: intent is not evidence.

The identifier fix for requirement completion unions several sources of text and asks
"do the requirement's identifiers appear in it?". That is wrong in one specific and
important way: the **action description** is what the planner *intended*, while the tool
**parameters** are what was actually *submitted*. Unioning them means a correctly-worded
step aimed at the right target can satisfy a requirement by describing the right thing
while doing the wrong thing.

The concrete case:

    requirement      Join Telegram channel @examplechannel
    step description Join Telegram channel @examplechannel      <- intent, says @examplechannel
    parameters       {"chat": "@otherchannel"}                  <- what was actually submitted

The union contains `@examplechannel` (from the description), so the requirement closes and
the agent believes the work is done against a channel it never joined.

This file is kept even once fixed: a test that only exercises the negative case (where
the description names the *wrong* channel) cannot see this, which is exactly why the
original regression test missed it.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.agent.context import TaskContextService
from backend.app.models import Base, Task


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    made = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield made
    await engine.dispose()


async def _task(factory, title: str) -> str:
    async with factory() as db:
        task = Task(
            task_id=f"AIR-2026-{abs(hash(title)) % 900000 + 100000:06d}",
            title=title, description=title, status="IN_PROGRESS",
            owner_email="operator@test.local",
        )
        db.add(task)
        await db.commit()
        return task.task_id


async def _row(db, public_id: str) -> Task:
    return await db.scalar(sa_select(Task).where(Task.task_id == public_id))


class TestIntentIsNotProof:
    @pytest.mark.asyncio
    async def test_a_correct_description_with_wrong_parameters_must_not_satisfy(self, factory):
        """The exact case the previous regression test could not see.

        The description names the right channel and the parameters name the wrong one.
        Only the parameters describe what the agent actually did, so the requirement must
        stay outstanding.
        """
        task_id = await _task(factory, "Right words, wrong channel")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_requirement(task, "Join Telegram channel @examplechannel")
            await db.commit()

            await service.record_observation(
                task,
                # The planner wrote the RIGHT target into the step description...
                action="Join Telegram channel @examplechannel",
                outcome="confirmed",
                tool="telegram_join_chat",
                # ...but the call was actually made against a different channel.
                parameters={"chat": "@otherchannel"},
                evidence={"authoritative": True, "kind": "membership_verified"},
            )
            await db.commit()

            outstanding = await service.requirements(task, only_outstanding=True)

        assert [r.text for r in outstanding] == ["Join Telegram channel @examplechannel"], (
            "a correctly-worded description is an INTENT, not evidence of what was done; "
            "the requirement must stay outstanding when the submitted target is different"
        )

    @pytest.mark.asyncio
    async def test_a_description_only_match_is_not_evidence(self, factory):
        """Even with no parameters at all, the description alone proves nothing."""
        task_id = await _task(factory, "Description only")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_requirement(task, "Join Telegram channel @examplechannel")
            await db.commit()

            await service.record_observation(
                task,
                action="Join Telegram channel @examplechannel",
                outcome="confirmed",
                tool="telegram_join_chat",
                parameters={},
                evidence={"authoritative": True, "kind": "membership_verified"},
            )
            await db.commit()

            outstanding = await service.requirements(task, only_outstanding=True)

        assert [r.text for r in outstanding] == ["Join Telegram channel @examplechannel"]

    @pytest.mark.asyncio
    async def test_the_matching_parameters_still_satisfy_it(self, factory):
        """The fix must not make the feature useless: a real match must still close it."""
        task_id = await _task(factory, "Genuine match")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_requirement(task, "Join Telegram channel @examplechannel")
            await db.commit()

            await service.record_observation(
                task,
                action="Join Telegram channel @examplechannel",
                outcome="confirmed",
                tool="telegram_join_chat",
                parameters={"chat": "@examplechannel"},
                evidence={"authoritative": True, "kind": "membership_verified"},
                observed_state={"chat": "@examplechannel", "is_member": True},
            )
            await db.commit()

            outstanding = await service.requirements(task, only_outstanding=True)

        assert outstanding == [], "an action actually taken against the named target closes it"

    @pytest.mark.asyncio
    async def test_an_observed_result_alone_can_also_prove_it(self, factory):
        """A tool that echoes the target in its result is proof even with no parameters."""
        task_id = await _task(factory, "Observed proof")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_requirement(task, "Join Telegram channel @examplechannel")
            await db.commit()

            await service.record_observation(
                task,
                action="Check membership",
                outcome="confirmed",
                tool="telegram_check_membership",
                parameters={"chat": "@examplechannel"},
                evidence={"authoritative": True, "kind": "membership_verified"},
                observed_state={"chat": "@examplechannel", "is_member": True},
            )
            await db.commit()

            outstanding = await service.requirements(task, only_outstanding=True)

        assert outstanding == []