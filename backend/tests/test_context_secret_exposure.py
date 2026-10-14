"""Proof-of-concept: does the secret screening actually hold?

This file exists to answer one question with evidence rather than by reading code:
**can untrusted external content reach the persisted context in unscreened form?**

Every test here asserts the *desired* behaviour. Run against the current code they are
expected to FAIL, and the failure output is the proof that the reviewer was right. They
are kept in the suite (not deleted once fixed) so the leak cannot come back.

Untrusted input reaches the context from several directions, and each one is a separate
route:

* a tool result's `observed_state`, which becomes both the observation record AND
  `task_contexts.external_state`;
* a requirement extracted from a bot message or a web page;
* an error string echoing what a remote service said;
* a human-intervention reason quoting the page that blocked the agent;
* values nested inside a dict or list, which are not strings at the top level.

Nothing here contacts Telegram, a chain, a browser or an LLM.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.agent.context import TaskContextService
from backend.app.models import Base, Task, TaskRequirement

# Values that match MemoryManager.SECRET_PATTERNS.
PRIVATE_KEY = "0x" + "a" * 64
SEED_PHRASE = "seed phrase: correct horse battery staple"
API_KEY = "api_key: sk-live-0123456789abcdefghij"


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
    """Fetch by the PUBLIC id. `db.get` uses the primary key, which is the UUID."""
    from sqlalchemy import select as sa_select

    return await db.scalar(sa_select(Task).where(Task.task_id == public_id))


def _assert_clean(blob: str, label: str) -> None:
    """The persisted text must not contain any of the secret fixtures."""
    for secret in (PRIVATE_KEY, SEED_PHRASE, API_KEY, "a" * 64, "sk-live-0123456789abcdefghij"):
        assert secret not in blob, (
            f"{label} persisted a secret verbatim: {secret[:24]}..."
        )


class TestObservedStateIsScreenedEverywhere:
    @pytest.mark.asyncio
    async def test_external_state_must_not_be_the_unscreened_copy(self, factory):
        """`external_state` is written from the RAW payload, not the screened one.

        The observation record is screened at construction, then `external_state` is
        assigned the original dict a few lines later. Anything the screening caught in the
        observation is therefore still present in full on the context row.
        """
        task_id = await _task(factory, "Observed state screening")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_observation(
                task, action="read a page", outcome="confirmed",
                tool="web_fetch", parameters={},
                evidence={"authoritative": True, "kind": "page_observation"},
                observed_state={"page_body": f"the page printed {PRIVATE_KEY}"},
            )
            await db.commit()
            context = await service.get(task)
            observations = await service.observations(task)

        assert "REDACTED" in str(observations[0].observed_state), (
            "the observation record should show the redaction marker"
        )
        _assert_clean(str(context.external_state), "task_contexts.external_state")

    @pytest.mark.asyncio
    async def test_nested_values_must_be_screened_not_json_blindly_stored(self, factory):
        """A secret one level down is not a top-level string, so it is never checked.

        `_screened_dict` screens only `isinstance(value, str)` entries; everything else
        goes through `_clip`, which JSON-encodes without inspecting the contents.
        """
        task_id = await _task(factory, "Nested secret screening")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_observation(
                task, action="inspect the bot dialog", outcome="confirmed",
                tool="telegram_read_messages", parameters={},
                evidence={"authoritative": True, "kind": "message_observed"},
                observed_state={
                    "messages": [{"text": "here is the key", "detail": PRIVATE_KEY}],
                    "meta": {"nested": {"secret": API_KEY}},
                },
            )
            await db.commit()
            context = await service.get(task)
            observations = await service.observations(task)

        _assert_clean(str(context.external_state), "external_state (nested)")
        _assert_clean(str(observations[0].observed_state), "observation (nested)")


class TestRequirementAndErrorTextIsScreened:
    @pytest.mark.asyncio
    async def test_a_requirement_containing_a_secret_is_redacted(self, factory):
        """Requirements come from bot messages and pages, which are untrusted."""
        task_id = await _task(factory, "Requirement screening")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_requirement(
                task, f"enter your {PRIVATE_KEY} to verify", origin="tool_result"
            )
            await db.commit()
            rows = await service.requirements(task)

        _assert_clean(str([r.text for r in rows]), "task_requirements.text")

    @pytest.mark.asyncio
    async def test_an_error_echoing_a_secret_is_redacted(self, factory):
        """A remote service that quotes the submitted value back must not persist it."""
        task_id = await _task(factory, "Error screening")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_observation(
                task, action="submit", outcome="failed",
                tool="browser_submit", parameters={},
                error=f"the server rejected the value {API_KEY}",
            )
            await service.record_failure(
                task, error=f"the server rejected the value {API_KEY}", category="INVALID_INPUT"
            )
            await db.commit()
            context = await service.get(task)

        _assert_clean(str(context.last_error), "task_contexts.last_error")
        _assert_clean(str(context.retry_summary), "task_contexts.retry_summary")

    @pytest.mark.asyncio
    async def test_a_human_intervention_reason_quoting_a_secret_is_redacted(self, factory):
        task_id = await _task(factory, "Intervention screening")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_human_intervention(
                task, required=True,
                reason=f"the page showed {PRIVATE_KEY} and refused to continue",
                instruction=f"solve the challenge; it said {SEED_PHRASE}",
            )
            await db.commit()
            context = await service.get(task)

        _assert_clean(str(context.human_intervention), "task_contexts.human_intervention")


class TestScreeningIsBoundedAtEveryLevel:
    """The recursive walker bounded nested containers; the root level did not."""

    @pytest.mark.asyncio
    async def test_a_huge_flat_dict_is_truncated_at_the_root(self, factory):
        from backend.app.agent.context import MAX_SCREEN_ITEMS

        task_id = await _task(factory, 'Root bound')
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            huge = {f'field_{i}': f'value {i}' for i in range(MAX_SCREEN_ITEMS * 3)}
            await service.record_observation(
                task, action='read a huge payload', outcome='confirmed',
                tool='web_fetch', parameters={},
                evidence={'authoritative': True, 'kind': 'page_observation'},
                observed_state=huge,
            )
            await db.commit()
            observations = await service.observations(task)

        stored = observations[0].observed_state
        assert len(stored) <= MAX_SCREEN_ITEMS + 1, (
            'the root dict must be bounded like every nested container'
        )
        assert any('TRUNCATED' in str(v) for v in stored.values())

    @pytest.mark.asyncio
    async def test_a_secret_in_a_mapping_key_is_redacted(self, factory):
        """Keys are attacker-influenced text too, and the patterns match labels."""
        task_id = await _task(factory, 'Key screening')
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_observation(
                task, action='inspect a payload', outcome='confirmed',
                tool='web_fetch', parameters={},
                evidence={'authoritative': True, 'kind': 'page_observation'},
                observed_state={f'api_key: {API_KEY}': 'harmless', 'normal_field': 'fine'},
            )
            await db.commit()
            observations = await service.observations(task)
            context = await service.get(task)

        blob = str(observations[0].observed_state) + str(context.external_state)
        assert 'sk-live-0123456789abcdefghij' not in blob
        assert 'REDACTED' in blob
        assert 'normal_field' in blob, 'an ordinary key must be left alone'

    @pytest.mark.asyncio
    async def test_integration_ref_is_screened(self, factory):
        """It only carries a browser profile today; screen it regardless."""
        task_id = await _task(factory, 'Integration ref')
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_observation(
                task, action='open a browser', outcome='confirmed',
                tool='browser_navigate', parameters={},
                evidence={'authoritative': True, 'kind': 'page_observation'},
                integration_ref=f'browser:{API_KEY}',
            )
            await db.commit()
            observations = await service.observations(task)

        assert 'sk-live-0123456789abcdefghij' not in str(observations[0].integration_ref)

class TestRequirementSatisfactionNeedsMatchingTargets:
    """The weak word-overlap matching can mark the WRONG requirement as satisfied.

    Two requirements that differ only in a handle share most of their words, so joining
    the wrong channel satisfies the right one. That is a false completion state, which is
    worse than an unfinished one.
    """

    @pytest.mark.asyncio
    async def test_joining_the_wrong_channel_must_not_satisfy_the_requirement(self, factory):
        task_id = await _task(factory, "Channel matching")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_requirement(
                task, "Join Telegram channel @examplechannel"
            )
            await db.commit()

            await service.record_observation(
                task,
                action="Join Telegram channel @otherchannel",
                outcome="confirmed",
                tool="telegram_join_chat",
                parameters={"chat": "@otherchannel"},
                evidence={"authoritative": True, "kind": "membership_verified"},
            )
            await db.commit()

            outstanding = await service.requirements(task, only_outstanding=True)

        assert [r.text for r in outstanding] == ["Join Telegram channel @examplechannel"], (
            "a requirement must not be closed by an action against a DIFFERENT target; "
            "word overlap on 'join', 'telegram' and 'channel' is not proof"
        )

    @pytest.mark.asyncio
    async def test_the_matching_channel_does_satisfy_it(self, factory):
        """The positive case still has to work, or nothing would ever be satisfied."""
        task_id = await _task(factory, "Channel matching positive")
        async with factory() as db:
            task = await _row(db, task_id)
            service = TaskContextService(db)
            await service.record_requirement(
                task, "Join Telegram channel @examplechannel"
            )
            await db.commit()
            await service.record_observation(
                task,
                action="Join Telegram channel @examplechannel",
                outcome="confirmed",
                tool="telegram_join_chat",
                parameters={"chat": "@examplechannel"},
                evidence={"authoritative": True, "kind": "membership_verified"},
            )
            await db.commit()
            outstanding = await service.requirements(task, only_outstanding=True)

        assert outstanding == [], "the matching action must close the requirement"