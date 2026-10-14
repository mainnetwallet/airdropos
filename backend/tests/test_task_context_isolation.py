"""Task Context: isolation, persistence, recovery, and the memory hierarchy.

The specification (§6.1, §6.6) requires that every task have its own independently
persisted execution context, that one task can never read another's, and that the three
levels of context stay separate. These tests pin the properties that make that true, and
they are the regression net for the specific ways it fails in practice:

* `load_or_create` handing back a context that belongs to a different task;
* observations written for one task showing up in another's resolved context;
* a task-scoped memory surfacing as general knowledge;
* a repeated action being executed again because its history was not consulted;
* a resumed run replaying work that was already proven.

Nothing here contacts Telegram, a chain, a browser or an LLM. External results are
constructed directly, so the tests describe the context layer's behaviour rather than
any integration's.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.agent.context import (
    ALLOWED_RESOURCE_REF_KEYS,
    TaskContextService,
    action_fingerprint,
)
from backend.app.memory.manager import MemoryManager, SecretLeakError
from backend.app.models import (
    Base,
    MemoryRecord,
    Task,
    TaskContext,
    TaskObservation,
    TaskRequirement,
)
from backend.app.models.context import (
    OUTCOME_CONFIRMED,
    OUTCOME_DUPLICATE_SUPPRESSED,
    OUTCOME_UNVERIFIED,
    REQUIREMENT_DISCOVERED,
    REQUIREMENT_SATISFIED,
)
from backend.app.models.memory import SCOPE_CAMPAIGN, SCOPE_GLOBAL, SCOPE_TASK


# ======================================================================
# Harness
# ======================================================================
@pytest_asyncio.fixture
async def factory():
    """Its own in-memory database, so no test can reach the operator's real one."""
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


async def _new_task(factory, title: str, description: str = "") -> str:
    async with factory() as db:
        task = Task(
            task_id=f"AIR-2026-{abs(hash(title)) % 900000 + 100000:06d}",
            title=title,
            description=description or title,
            status="IN_PROGRESS",
            owner_email="operator@test.local",
        )
        db.add(task)
        await db.commit()
        return task.id


async def _task_row(factory, task_id):
    async with factory() as db:
        return await db.get(Task, task_id)


def _evidence(kind: str, authoritative: bool = True) -> dict:
    return {"authoritative": authoritative, "kind": kind}


# ======================================================================
# Isolation
# ======================================================================
class TestTaskContextIsolation:
    async def test_each_task_gets_its_own_context(self, factory):
        """A new task must create a new context, never inherit another task's."""
        a_id = await _new_task(factory, "Complete airdrop A")
        b_id = await _new_task(factory, "Complete airdrop B")

        async with factory() as db:
            a = await db.get(Task, a_id)
            b = await db.get(Task, b_id)
            service = TaskContextService(db)
            ctx_a = await service.load_or_create(a)
            ctx_b = await service.load_or_create(b)
            assert ctx_a.id != ctx_b.id
            assert ctx_a.task_id == a.id
            assert ctx_b.task_id == b.id

    async def test_loading_twice_returns_the_same_row(self, factory):
        """Re-entering a task must not fork its history into two contexts."""
        task_id = await _new_task(factory, "Repeat load")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            first = await service.load_or_create(task)
            second = await service.load_or_create(task)
            assert first.id == second.id

    async def test_resolving_task_a_never_returns_task_bs_history(self, factory):
        """The isolation property the hierarchy exists to provide."""
        a_id = await _new_task(factory, "Research campaign X")
        b_id = await _new_task(factory, "Research campaign Y")

        async with factory() as db:
            a = await db.get(Task, a_id)
            b = await db.get(Task, b_id)
            service = TaskContextService(db)

            await service.record_observation(
                a, action="read the official announcement", outcome=OUTCOME_CONFIRMED,
                tool="web_fetch", parameters={"url": "https://example.org"},
                evidence=_evidence("page_observation"),
            )
            await service.record_requirement(a, "join the Telegram channel @xchannel")
            await db.commit()

            resolved_a = await service.resolve(a)
            resolved_b = await service.resolve(b)

            assert [o.action for o in resolved_a.observations] == ["read the official announcement"]
            assert resolved_b.observations == []
            assert resolved_b.outstanding_requirements() == []

    async def test_writing_to_one_context_does_not_touch_the_other(self, factory):
        a_id = await _new_task(factory, "Task A")
        b_id = await _new_task(factory, "Task B")

        async with factory() as db:
            a = await db.get(Task, a_id)
            b = await db.get(Task, b_id)
            service = TaskContextService(db)
            await service.record_observation(
                a, action="join group", outcome=OUTCOME_CONFIRMED, tool="telegram_join_chat",
                parameters={"chat": "@one"}, evidence=_evidence("membership_verified"),
            )
            await service.set_next_step(a, rationale="because of task A's own history")
            await db.commit()

            ctx_b = await service.get(b)
            assert ctx_b is None or not (ctx_b.next_step_rationale or "").strip()

    async def test_concurrent_tasks_do_not_corrupt_each_other(self, factory):
        """Two runs writing at once must not lose or cross a write.

        Each task's context is addressed by its own task id, so interleaving the two
        coroutines cannot merge their records.
        """
        a_id = await _new_task(factory, "Concurrent A")
        b_id = await _new_task(factory, "Concurrent B")

        async def write(task_id: str, label: str, count: int) -> None:
            async with factory() as db:
                task = await db.get(Task, task_id)
                service = TaskContextService(db)
                for index in range(count):
                    await service.record_observation(
                        task,
                        action=f"{label} step {index}",
                        outcome=OUTCOME_CONFIRMED,
                        tool="browser_navigate",
                        parameters={"url": f"https://example.org/{label}/{index}"},
                        evidence=_evidence("page_observation"),
                    )
                await db.commit()

        await asyncio.gather(write(a_id, "A", 12), write(b_id, "B", 12))

        async with factory() as db:
            service = TaskContextService(db)
            a = await db.get(Task, a_id)
            b = await db.get(Task, b_id)
            obs_a = await service.observations(a)
            obs_b = await service.observations(b)

        assert len(obs_a) == 12, "a concurrent run must not lose another task's writes"
        assert len(obs_b) == 12
        assert all(o.action.startswith("A ") for o in obs_a)
        assert all(o.action.startswith("B ") for o in obs_b)

    async def test_a_deleted_task_takes_its_context_with_it(self, factory):
        """Private execution history must not linger as context another task could reach."""
        task_id = await _new_task(factory, "Disposable")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.record_observation(
                task, action="read a page", outcome=OUTCOME_CONFIRMED, tool="web_fetch",
                parameters={}, evidence=_evidence("page_observation"),
            )
            await db.commit()
            await db.delete(task)
            await db.commit()

        async with factory() as db:
            rows = list((await db.execute(select(TaskObservation))).scalars().all())
            contexts = list((await db.execute(select(TaskContext))).scalars().all())
        assert rows == [] and contexts == []


# ======================================================================
# Attempted is not confirmed
# ======================================================================
class TestAttemptedIsNotConfirmed:
    async def test_an_unverified_attempt_does_not_count_as_proven(self, factory):
        task_id = await _new_task(factory, "Uncertain outcome")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            record = await service.record_observation(
                task, action="submit the form", outcome=OUTCOME_UNVERIFIED,
                tool="browser_submit", parameters={"selector": "#submit"},
                evidence=_evidence("post_action_observation", authoritative=False),
            )
            await db.commit()

        assert record.proved_completion is False
        assert record.outcome == "unverified"

    async def test_only_an_authoritative_confirmation_suppresses_a_repeat(self, factory):
        """The repeat guard must not turn an unknown outcome into a silent skip."""
        task_id = await _new_task(factory, "Form submission")
        params = {"selector": "#claim"}
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)

            await service.record_observation(
                task, action="submit", outcome=OUTCOME_UNVERIFIED,
                tool="browser_submit", parameters=params,
                evidence=_evidence("post_action_observation", authoritative=False),
            )
            await db.commit()
            assert await service.confirmed_fingerprint(task, "browser_submit", params) is None

            await service.record_observation(
                task, action="submit", outcome=OUTCOME_CONFIRMED,
                tool="browser_submit", parameters=params,
                evidence=_evidence("post_action_observation"),
            )
            await db.commit()
            confirmed = await service.confirmed_fingerprint(task, "browser_submit", params)
            assert confirmed is not None, "a proven action must be recognised, not repeated"

    async def test_a_different_action_is_not_mistaken_for_the_same_one(self, factory):
        task_id = await _new_task(factory, "Two joins")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.record_observation(
                task, action="join", outcome=OUTCOME_CONFIRMED, tool="telegram_join_chat",
                parameters={"chat": "@one"}, evidence=_evidence("membership_verified"),
            )
            await db.commit()
            assert await service.confirmed_fingerprint(
                task, "telegram_join_chat", {"chat": "@two"}
            ) is None, "joining a different chat is a different action"

    async def test_argument_order_does_not_create_a_false_difference(self, factory):
        """Canonical fingerprinting: the same call in another key order is the same action."""
        assert action_fingerprint("t", {"a": 1, "b": 2}) == action_fingerprint("t", {"b": 2, "a": 1})
        assert action_fingerprint("t", {"a": 1}) != action_fingerprint("u", {"a": 1})

    async def test_the_latest_attempt_distinguishes_never_tried_from_unknown(self, factory):
        task_id = await _new_task(factory, "Investigate before retrying")
        params = {"url": "https://example.org"}
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            assert await service.latest_attempt(task, "web_fetch", params) is None

            await service.record_observation(
                task, action="fetch", outcome=OUTCOME_UNVERIFIED, tool="web_fetch",
                parameters=params, evidence=_evidence("page_observation", authoritative=False),
            )
            await db.commit()
            attempt = await service.latest_attempt(task, "web_fetch", params)
            assert attempt is not None and attempt.outcome == OUTCOME_UNVERIFIED

    async def test_a_suppressed_duplicate_is_recorded_as_history(self, factory):
        """Skipping a repeat is still an event the operator can see."""
        task_id = await _new_task(factory, "Duplicate suppressed")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.record_observation(
                task, action="already done", outcome=OUTCOME_DUPLICATE_SUPPRESSED,
                tool="browser_submit", parameters={},
            )
            await db.commit()
            outcomes = [o.outcome for o in await service.observations(task)]
            assert outcomes == [OUTCOME_DUPLICATE_SUPPRESSED]


# ======================================================================
# Requirements
# ======================================================================
class TestDiscoveredRequirements:
    async def test_a_requirement_is_recorded_once_however_often_it_is_re_seen(self, factory):
        task_id = await _new_task(factory, "Join the channel")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.record_requirement(task, "Join the channel @examplechannel")
            await service.record_requirement(task, "  join the CHANNEL   @examplechannel ")
            await db.commit()
            rows = await service.requirements(task)

        assert len(rows) == 1, "the same requirement must not be duplicated"

    async def test_the_outstanding_view_tracks_satisfaction(self, factory):
        task_id = await _new_task(factory, "Requirements progress")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            requirement = await service.record_requirement(task, "hold 100 tokens")
            await db.commit()

            assert [r.text for r in await service.requirements(task, only_outstanding=True)] == [
                "hold 100 tokens"
            ]

            observation = await service.record_observation(
                task, action="hold 100 tokens", outcome=OUTCOME_CONFIRMED,
                tool="get_token_balance", parameters={"symbol": "TKN"},
                evidence=_evidence("record_read"),
            )
            await db.commit()

            assert await service.requirements(task, only_outstanding=True) == []
            refreshed = await db.get(TaskRequirement, requirement.id)
            assert refreshed.status == REQUIREMENT_SATISFIED
            assert refreshed.satisfied_by_observation_id == observation.id

    async def test_an_unproven_action_closes_no_requirement(self, factory):
        task_id = await _new_task(factory, "Nothing proven")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.record_requirement(task, "complete the quiz")
            await service.record_observation(
                task, action="complete the quiz", outcome=OUTCOME_UNVERIFIED,
                tool="telegram_click_button", parameters={},
                evidence=_evidence("button_clicked", authoritative=False),
            )
            await db.commit()
            outstanding = await service.requirements(task, only_outstanding=True)
        assert [r.text for r in outstanding] == ["complete the quiz"]

    async def test_unaddressed_requirements_are_separated_from_covered_ones(self, factory):
        """The signal that decides whether a plan must be revised."""
        task_id = await _new_task(factory, "Partially covered")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.record_requirement(task, "join the Telegram channel @examplechannel")
            await service.record_requirement(task, "sign in with the connected wallet")
            await db.commit()

            plan = ["Open the bot and read its instructions", "join the Telegram channel"]
            unaddressed = await service.unaddressed_requirements(task, plan)

        assert [r.text for r in unaddressed] == ["sign in with the connected wallet"]

    async def test_an_account_specific_requirement_is_marked_and_kept_in_its_task(self, factory):
        """Membership of one account must not be reusable as another's evidence."""
        task_id = await _new_task(factory, "Wallet eligibility")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            requirement = await service.record_requirement(
                task,
                "wallet 0xabc123 is eligible for this campaign",
                is_account_specific=True,
                account_ref="0xabc123",
            )
            await db.commit()

            assert requirement.is_account_specific is True
            assert requirement.account_ref == "0xabc123"
            assert requirement.is_campaign_scoped is False

            # And it is reachable only through this task.
            other = await _new_task(factory, "Another wallet")
            other_row = await db.get(Task, other)
            assert await service.requirements(other_row) == []


# ======================================================================
# Resource references and secrets
# ======================================================================
class TestResourceReferencesAndSecrets:
    async def test_a_legitimate_reference_survives(self, factory):
        task_id = await _new_task(factory, "Keep the chat handle")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.merge_resource_refs(
                task, {"chat_id": 12345, "chat_title": "Example Channel", "url": "https://t.me/x"}
            )
            await db.commit()
            context = await service.get(task)

        assert context.resource_refs["chat_id"] == 12345
        assert context.resource_refs["chat_title"] == "Example Channel"

    async def test_a_key_outside_the_allowlist_is_refused(self, factory):
        """The context is not a place credentials accumulate."""
        task_id = await _new_task(factory, "No secrets")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.merge_resource_refs(
                task, {"session_cookie": "abc123", "chat_id": 7}
            )
            await db.commit()
            context = await service.get(task)

        assert "session_cookie" not in context.resource_refs
        assert context.resource_refs.get("chat_id") == 7
        assert "session_cookie" not in ALLOWED_RESOURCE_REF_KEYS

    async def test_a_value_matching_a_secret_pattern_is_not_stored(self, factory):
        task_id = await _new_task(factory, "Token must not persist")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.merge_resource_refs(
                task, {"url": "https://x/?k=0x" + "a" * 64}
            )
            await service.record_observation(
                task, action="read", outcome=OUTCOME_CONFIRMED, tool="web_fetch",
                parameters={}, evidence=_evidence("page_observation"),
                observed_state={"note": "the page said token: " + "b" * 32},
            )
            await db.commit()
            context = await service.get(task)
            observations = list(
                (
                    await db.execute(
                        select(TaskObservation).where(TaskObservation.task_id == task.id)
                    )
                ).scalars().all()
            )

        blob = str(context.resource_refs) + str(observations[0].observed_state)
        assert "a" * 64 not in blob
        assert "b" * 32 not in blob
        assert "REDACTED" in observations[0].observed_state["note"]

    async def test_screenshotted_state_is_bounded(self, factory):
        """A whole page dump must not be able to bloat one row."""
        task_id = await _new_task(factory, "Large observation")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.record_observation(
                task, action="read a very long page", outcome=OUTCOME_CONFIRMED,
                tool="web_fetch", parameters={}, evidence=_evidence("page_observation"),
                observed_state={"body": "x" * 100_000},
            )
            await db.commit()
            observations = await service.observations(task)

        assert len(observations[0].observed_state["body"]) <= 2100


# ======================================================================
# The memory hierarchy
# ======================================================================
class TestMemoryHierarchy:
    async def test_a_task_scoped_memory_never_becomes_global_knowledge(self, factory):
        """The single most important boundary: one task's history is not another's context."""
        a_id = await _new_task(factory, "Task A")
        b_id = await _new_task(factory, "Task B")

        async with factory() as db:
            memory = MemoryManager(db)
            await memory.remember_scoped(
                "EXECUTION_OUTCOME",
                "Task A joined @privatechannel with account one",
                "agent",
                scope=SCOPE_TASK,
                task_id=a_id,
            )
            await memory.remember_scoped(
                "TOOL_RELIABILITY",
                "telegram_read_messages reliably returns the last 10 messages",
                "agent",
                scope=SCOPE_GLOBAL,
            )
            await db.commit()

            for_task_a = await memory.recall_for_context(task_id=a_id)
            for_task_b = await memory.recall_for_context(task_id=b_id)
            assert any("privatechannel" in r.content for r in for_task_a)
            assert all("privatechannel" not in r.content for r in for_task_b)
            assert any("telegram_read_messages" in r.content for r in for_task_b)

    async def test_scoped_memory_requires_a_matching_owner(self, factory):
        """A scoped record with no owner is private context nobody could read back."""
        task_id = await _new_task(factory, "Owner required")
        async with factory() as db:
            memory = MemoryManager(db)
            with pytest.raises(ValueError):
                await memory.remember_scoped("X", "orphan", "agent", scope=SCOPE_TASK)
            with pytest.raises(ValueError):
                await memory.remember_scoped("X", "orphan", "agent", scope=SCOPE_CAMPAIGN)
            with pytest.raises(ValueError):
                # A "global" record that belongs to a task is private data published as
                # general knowledge, which is exactly what must be impossible.
                await memory.remember_scoped(
                    "X", "private", "agent", scope=SCOPE_GLOBAL, task_id=task_id
                )

    async def test_secrets_are_still_refused_at_every_scope(self, factory):
        task_id = await _new_task(factory, "No secret memory")
        async with factory() as db:
            memory = MemoryManager(db)
            for scope, kwargs in (
                (SCOPE_GLOBAL, {}),
                (SCOPE_TASK, {"task_id": task_id}),
            ):
                with pytest.raises(SecretLeakError):
                    await memory.remember_scoped(
                        "VERIFIED_FACT", "private key: 0x" + "c" * 64, "agent",
                        scope=scope, **kwargs,
                    )

    async def test_resolved_context_keeps_the_three_levels_apart(self, factory):
        """The levels are unioned for reading, never merged into one indistinguishable blob."""
        a_id = await _new_task(factory, "Level separation")
        async with factory() as db:
            task = await db.get(Task, a_id)
            service = TaskContextService(db)
            memory = MemoryManager(db)
            await memory.remember_scoped(
                "TOOL_RELIABILITY", "browser_find confirms an element exists before a click",
                "agent", scope=SCOPE_GLOBAL,
            )
            await service.record_observation(
                task, action="join the channel", outcome=OUTCOME_CONFIRMED,
                tool="telegram_join_chat", parameters={"chat": "@x"},
                evidence=_evidence("membership_verified"),
            )
            await service.record_requirement(task, "sign in with the wallet")
            await db.commit()

            resolved = await service.resolve(task)
            digest = resolved.digest()

        assert len(resolved.global_facts) == 1
        assert resolved.campaign_facts == []  # this task is linked to no campaign
        assert "GENERAL KNOWLEDGE" in digest
        assert "ALREADY PROVEN" in digest
        assert "OUTSTANDING REQUIREMENTS" in digest
        assert "join the channel" in digest

    async def test_a_digest_is_bounded(self, factory):
        task_id = await _new_task(factory, "Huge history")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            for index in range(40):
                await service.record_observation(
                    task, action=f"action {index} " + "detail " * 40,
                    outcome=OUTCOME_CONFIRMED, tool="web_fetch",
                    parameters={"i": index}, evidence=_evidence("page_observation"),
                )
            await db.commit()
            resolved = await service.resolve(task)

        assert len(resolved.digest(max_chars=2000)) <= 2000


# ======================================================================
# Persistence and recovery
# ======================================================================
class TestContextPersistenceAndRecovery:
    async def test_context_survives_a_fresh_session(self, factory):
        """A worker restart must find everything the previous run learned."""
        task_id = await _new_task(factory, "Survives a restart")

        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.record_observation(
                task, action="read the bot", outcome=OUTCOME_CONFIRMED,
                tool="telegram_read_messages", parameters={"chat": "@bot"},
                evidence=_evidence("message_observed"),
                observed_state={"chat_id": 42, "summary": "join @channel first"},
            )
            await service.record_requirement(task, "join the channel @channel")
            await service.merge_resource_refs(task, {"chat_id": 42})
            await service.set_next_step(task, rationale="the bot asked for a join", step_index=1)
            await db.commit()

        # A brand new session, exactly as a restarted worker would have.
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            resolved = await service.resolve(task)
            context = resolved.task_context

            assert context.next_step_rationale == "the bot asked for a join"
            assert context.current_step_index == 1
            assert context.resource_refs["chat_id"] == 42
            assert [o.tool for o in resolved.observations] == ["telegram_read_messages"]
            assert [r.text for r in resolved.outstanding_requirements()] == [
                "join the channel @channel"
            ]

    async def test_resume_is_recorded_distinctly_from_a_first_run(self, factory):
        task_id = await _new_task(factory, "Resumed")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.mark_resumed(task)
            await db.commit()
            context = await service.get(task)

        assert context.resumed_at is not None

    async def test_a_human_blocker_is_a_fact_about_now_not_a_permanent_label(self, factory):
        task_id = await _new_task(factory, "Captcha then continue")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.record_human_intervention(
                task, required=True, reason="CAPTCHA", instruction="solve it in the browser",
                step_index=3,
            )
            await db.commit()
            context = await service.get(task)
            assert context.human_intervention["pending"] is True
            assert context.human_intervention["step_index"] == 3

            await service.record_human_intervention(
                task, required=False, reason="cleared", resolved=True
            )
            await db.commit()
            context = await service.get(task)
            assert context.human_intervention["pending"] is False
            assert context.human_intervention["resolved_at"]

    async def test_an_unknown_outcome_is_never_stored_as_success(self, factory):
        """A future caller inventing an outcome string must not create a false claim."""
        task_id = await _new_task(factory, "Unknown outcome string")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            record = await service.record_observation(
                task, action="do a thing", outcome="definitely_fine", tool="x", parameters={}
            )
            await db.commit()

        assert record.outcome == OUTCOME_UNVERIFIED

    async def test_resolving_the_intervention_clears_the_context_blocker(self, factory):
        """"Blocked on a human" must stop being reported once it is actually resolved.

        The clearing happens in the inbox's `mark_resolved`, which is where the blocker
        genuinely goes away. Without it the task keeps reporting a blocker nobody is
        looking at.
        """
        from backend.app.inbox.service import ConversationService, InterventionService

        task_id = await _new_task(factory, "Blocked then cleared")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            await service.record_human_intervention(
                task, required=True, reason="CAPTCHA in the browser", step_index=2
            )
            await db.commit()

            conversation = await ConversationService(db).get_or_create_for_task(
                task, task.owner_email or ""
            )
            request, _created = await InterventionService(db).create(
                task=task,
                conversation=conversation,
                reason_code="HUMAN_VERIFICATION_REQUIRED",
                problem_summary="CAPTCHA in the browser",
                required_action="Solve the CAPTCHA in the browser window.",
            )
            await db.commit()

            await InterventionService(db).mark_resolved(
                request, resolved_by="operator@test.local", outcome="CAPTCHA solved"
            )
            await db.commit()

            context = await service.get(task)
            assert context.human_intervention["pending"] is False
            assert context.human_intervention["resolved_at"]

    async def test_the_context_revision_advances_on_every_write(self, factory):
        """A stale context is detectable rather than silently trusted."""
        task_id = await _new_task(factory, "Revision counter")
        async with factory() as db:
            task = await db.get(Task, task_id)
            service = TaskContextService(db)
            first = await service.load_or_create(task)
            start = first.revision
            await service.set_next_step(task, rationale="one")
            second = await service.get(task)
            after_second = second.revision
            await service.set_next_step(task, rationale="two")
            third = await service.get(task)
            after_third = third.revision
            await db.commit()

        # Read as integers: an async session identity-maps the row, so `second` and
        # `third` are the same object and comparing them would compare it to itself.
        assert after_second > start
        assert after_third > after_second