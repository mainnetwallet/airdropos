"""Agent Inbox: intervention requests, task-scoped conversations, and automatic resumption.

These lock down the behaviour the feature exists for:

* a blocking condition creates a PERSISTENT request and a message in the RIGHT conversation;
* a reply is bound to the conversation it was sent from, so it can never move another task;
* the blocking condition is genuinely checked before anything is resumed;
* a satisfied reply resumes the same task without a manual Task Manager retry;
* duplicate replies, concurrent resumes and cancelled tasks cannot produce two workers;
* an intentionally stopped task is not restarted by an informational reply.

External services are replaced with deterministic doubles. Nothing here contacts Telegram, a
chain, a browser or an LLM. The reply interpreter is replaced by a scripted double by default
(see `interpreter` below): these tests are about verification, resumption and isolation, so the
model's reading of a reply is held fixed. Reply interpretation itself is covered in
`test_inbox_reply_interpretation.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.database.session import get_db
from backend.app.inbox import replies as replies_module
from backend.app.inbox import verification as verification_module
from backend.app.inbox.interpretation import (
    InterpretationResult,
    ReplyDecision,
    ReplyInterpretation,
)
from backend.app.inbox.replies import ReplyRouter
from backend.app.inbox.service import (
    OPERATOR_PAUSE,
    REQUIRES_EXPLICIT_AUTHORIZATION,
    TELEGRAM_SESSION_UNAVAILABLE,
    UNKNOWN_REASON,
    ConversationService,
    InterventionService,
    classify_reason,
)
from backend.app.inbox.verification import VerificationResult
from backend.app.main import app
from backend.app.models import (
    AgentConversation,
    Base,
    ConversationMessage,
    Task,
    InterventionRequest,
    TaskCheckpoint,
    User,
    UserSession,
)
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

OPERATOR_A = "operator-a@test.local"
OPERATOR_B = "operator-b@test.local"


# ======================================================================
# Harness
# ======================================================================
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

    async def override_get_db():
        async with made() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    yield made
    app.dependency_overrides.clear()
    await engine.dispose()


async def _session_for(email: str, scopes: List[Scope], factory) -> str:
    token = generate_token()
    async with factory() as db:
        user = User(
            email=email,
            hashed_password=hash_password("correct-horse-battery"),
            is_active=True,
            is_superuser=True,
        )
        db.add(user)
        await db.flush()
        db.add(
            UserSession(
                user_id=user.id,
                token=hash_token(token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                scopes=sorted(s.value for s in scopes),
            )
        )
        await db.commit()
    return token


@pytest_asyncio.fixture
async def operator_a(factory):
    return await _session_for(OPERATOR_A, list(Scope), factory)


@pytest_asyncio.fixture
async def operator_b(factory):
    return await _session_for(OPERATOR_B, list(Scope), factory)


def _client(token: Optional[str]) -> AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _make_task(
    factory,
    *,
    owner: str = OPERATOR_A,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    progress: float = 0.5,
    retry_count: int = 0,
    max_retries: int = 3,
) -> str:
    async with factory() as db:
        task = await TaskManager(db).create_task(
            title=f"Task {uuid.uuid4().hex[:6]}",
            description="A task under test.",
            status=status,
            owner_email=owner,
        )
        task.progress = progress
        task.retry_count = retry_count
        task.max_retries = max_retries
        await db.commit()
        return task.task_id


async def _park(factory, task_id: str, *, owner: str = OPERATOR_A, **overrides):
    """Put a task into NEEDS_USER_ACTION through the real intervention path."""
    from backend.app.inbox.raise_intervention import park_for_intervention

    async with factory() as db:
        task = await TaskManager(db).get_by_task_id(task_id)
        kwargs = dict(
            error_text="Telegram session is unavailable for this task.",
            error_category="AUTH_FAILURE",
            blocking_step="Step 2 (telegram_join_chat): join the channel",
            integration_ref="telegram:agent",
        )
        kwargs.update(overrides)
        result = await park_for_intervention(db, task, owner_email=owner, **kwargs)
        await db.commit()
        return result


class ScriptedInterpreter:
    """Stands in for the LLM reply interpreter. Records every call it receives."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.result: Any = InterpretationResult.success(
            ReplyInterpretation(
                decision=ReplyDecision.BLOCKER_REQUIRES_VERIFICATION,
                explanation="Scripted default: the operator reports a step they performed.",
            )
        )

    async def __call__(self, db, **kwargs):
        self.calls.append(kwargs)
        outcome = self.result(kwargs) if callable(self.result) else self.result
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def interpreter(monkeypatch):
    """Hold the model's reading of every reply fixed unless a test scripts it."""
    scripted = ScriptedInterpreter()
    monkeypatch.setattr(replies_module, "interpret_reply", scripted)
    return scripted


@pytest.fixture
def always_ok(monkeypatch):
    """A verifier that positively confirms the condition."""
    calls: List[str] = []

    async def _verify(db, task, request):
        calls.append(request.request_id)
        return VerificationResult.satisfied(
            method="test_double", outcome="The test double confirms the condition is resolved."
        )

    monkeypatch.setitem(verification_module._REGISTRY, TELEGRAM_SESSION_UNAVAILABLE, _verify)
    return calls


@pytest.fixture
def never_ok(monkeypatch):
    async def _verify(db, task, request):
        return VerificationResult.failed(
            method="test_double",
            outcome="The test double reports the session is still signed out.",
            question="Please sign the session in, then tell me to re-check.",
        )

    monkeypatch.setitem(verification_module._REGISTRY, TELEGRAM_SESSION_UNAVAILABLE, _verify)
    return _verify


@pytest.fixture
def uncheckable(monkeypatch):
    async def _verify(db, task, request):
        return VerificationResult.unchecked(
            method="test_double",
            outcome="The gateway is not reachable.",
            question="Please tell me what you did and what you saw.",
        )

    monkeypatch.setitem(verification_module._REGISTRY, TELEGRAM_SESSION_UNAVAILABLE, _verify)
    return _verify


# ======================================================================
# 1. An intervention creates a persistent request
# ======================================================================
class TestPersistentIntervention:
    @pytest.mark.asyncio
    async def test_intervention_persists_a_structured_request(self, factory):
        task_id = await _make_task(factory)
        result = await _park(factory, task_id)

        assert result is not None and result["new_request"] is True
        async with factory() as db:
            request = await db.scalar(
                select(InterventionRequest).where(
                    InterventionRequest.request_id == result["request_id"]
                )
            )
            assert request is not None
            assert request.status == "PENDING"
            assert request.reason_code == TELEGRAM_SESSION_UNAVAILABLE
            assert request.owner_email == OPERATOR_A
            assert request.blocking_step
            assert request.integration_ref == "telegram:agent"
            assert request.created_at_task_status == TaskStatus.IN_PROGRESS.value

    @pytest.mark.asyncio
    async def test_intervention_parks_the_task_and_saves_a_checkpoint(self, factory):
        task_id = await _make_task(factory)
        result = await _park(factory, task_id)
        assert result["checkpoint_saved"] is True

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            assert task.status == TaskStatus.NEEDS_USER_ACTION.value
            # Progress must survive parking; the default for the status must not overwrite it.
            assert task.progress == 0.5
            checkpoint = await _latest_checkpoint(db, task)
            assert checkpoint is not None
            assert checkpoint.checkpoint_name.startswith("awaiting_operator:")

    @pytest.mark.asyncio
    async def test_a_repeat_park_does_not_duplicate_request_or_message(self, factory):
        task_id = await _make_task(factory)
        first = await _park(factory, task_id)
        second = await _park(factory, task_id)

        assert second["request_id"] == first["request_id"]
        assert second["new_request"] is False

        async with factory() as db:
            requests = (
                await db.execute(
                    select(InterventionRequest).where(
                        InterventionRequest.task_id == await _task_pk(db, task_id)
                    )
                )
            ).scalars().all()
            assert len(requests) == 1
            messages = (
                await db.execute(
                    select(ConversationMessage).where(
                        ConversationMessage.conversation_id == requests[0].conversation_id,
                        ConversationMessage.kind == "INTERVENTION_REQUEST",
                    )
                )
            ).scalars().all()
            assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_the_message_states_what_to_do_and_how_it_is_verified(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            messages = await ConversationService(db).list_messages(conversation.id)
            body = messages[0].content
            assert task_id in body
            assert "What happened:" in body
            assert "What you need to do:" in body
            assert "How I will verify this:" in body
            assert "I have saved the current task state" in body

    @pytest.mark.asyncio
    async def test_an_unknown_blocker_asks_a_question_instead_of_inventing_a_fix(self, factory):
        task_id = await _make_task(factory)
        await _park(
            factory,
            task_id,
            error_text="Something went sideways in a way nobody has classified",
            error_category=None,
            integration_ref=None,
        )
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            body = (await ConversationService(db).list_messages(conversation.id))[0].content
            assert "could not determine a specific fix" in body
            # No Telegram advice for a non-Telegram failure.
            assert "Telegram" not in body
            assert "What exactly is blocking this step?" in body

    @pytest.mark.asyncio
    async def test_reason_classification_covers_the_real_categories(self):
        assert classify_reason("captcha required", None) == "HUMAN_VERIFICATION"
        assert classify_reason(
            "Telegram session is no longer valid (AuthKeyUnregisteredError)", "AUTH_FAILURE"
        ) == "TELEGRAM_SESSION_UNAVAILABLE"
        # Auth failure with no Telegram signal stays a generic auth problem rather than being
        # mislabelled as a Telegram issue.
        assert classify_reason("session revoked", "AUTH_FAILURE") == "MISSING_AUTHENTICATION"
        assert classify_reason("browser is not running", None) == "BROWSER_SESSION_UNAVAILABLE"
        assert classify_reason("no wallet configured", None) == "MISSING_AUTHENTICATION"
        assert classify_reason("emergency stop engaged", None) == "OPERATOR_PAUSE"
        assert classify_reason("gibberish", None) == UNKNOWN_REASON


# ======================================================================
# 2 & 5. The right conversation receives the message
# ======================================================================
class TestConversationIsolation:
    @pytest.mark.asyncio
    async def test_each_task_gets_its_own_conversation(self, factory):
        a = await _make_task(factory)
        b = await _make_task(factory)
        await _park(factory, a)
        await _park(factory, b)

        async with factory() as db:
            conv_a = await _first_conversation(db, a)
            conv_b = await _first_conversation(db, b)
            assert conv_a.id != conv_b.id
            assert conv_a.task_id != conv_b.task_id

            messages_a = await ConversationService(db).list_messages(conv_a.id)
            for message in messages_a:
                assert a in message.content
                assert b not in message.content

    @pytest.mark.asyncio
    async def test_message_history_survives_a_new_session(self, factory):
        """Persistence, not in-memory state: a fresh session reads the same history."""
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            first = await ConversationService(db).get_or_create_for_task(
                await TaskManager(db).get_by_task_id(task_id), OPERATOR_A
            )
            await ConversationService(db).append_message(first, role="user", content="hello")
            await db.commit()

        async with factory() as db:  # a genuinely new session/engine-level connection
            conversation = await _first_conversation(db, task_id)
            messages = await ConversationService(db).list_messages(conversation.id)
            assert [m.content for m in messages][-1] == "hello"

    @pytest.mark.asyncio
    async def test_messages_are_persisted_before_a_resume_is_scheduled(self, factory, always_ok):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation,
                task=task,
                owner_email=OPERATOR_A,
                text="Telegram session is ready.",
            )
            # Before the commit the user message already exists alongside the status change.
            messages = await ConversationService(db).list_messages(conversation.id)
            assert any(m.role == "user" and "ready" in m.content for m in messages)
            assert outcome.status == "RESUMED"

    @pytest.mark.asyncio
    async def test_completed_and_cancelled_tasks_keep_their_history(self, factory, always_ok):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="stop this task",
            )
            await db.commit()

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            messages = await ConversationService(db).list_messages(conversation.id)
            assert len(messages) >= 2
            task = await TaskManager(db).get_by_task_id(task_id)
            assert task.status == TaskStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_conversation_ordering_uses_persisted_sequence(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            service = ConversationService(db)
            for i in range(5):
                await service.append_message(conversation, role="user", content=f"m{i}")
            await db.commit()

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            messages = await ConversationService(db).list_messages(conversation.id)
            seqs = [m.seq for m in messages]
            assert seqs == sorted(seqs)
            assert [m.content for m in messages[-5:]] == [f"m{i}" for i in range(5)]


# ======================================================================
# 3, 6, 7, 10. Reply routing and verification
# ======================================================================
class TestReplyRouting:
    @pytest.mark.asyncio
    async def test_a_reply_is_bound_to_its_own_conversation_and_task(
        self, factory, operator_a, always_ok
    ):
        first = await _make_task(factory)
        second = await _make_task(factory)
        await _park(factory, first)
        await _park(factory, second)

        async with factory() as db:
            conversation = await _first_conversation(db, first)
            task = await TaskManager(db).get_by_task_id(first)
            await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.",
            )
            await db.commit()

        async with factory() as db:
            assert (await TaskManager(db).get_by_task_id(first)).status == TaskStatus.READY.value
            # The untouched task must still be parked.
            assert (
                await TaskManager(db).get_by_task_id(second)
            ).status == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_an_unresolved_condition_prevents_resumption(self, factory, never_ok):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="I think I fixed it",
            )
            await db.commit()
            assert outcome.status == "NEEDS_MORE_INFO"
            assert outcome.resumed is False
            assert outcome.verification["ok"] is False

        async with factory() as db:
            assert (
                await TaskManager(db).get_by_task_id(task_id)
            ).status == TaskStatus.NEEDS_USER_ACTION.value
            request = await InterventionService(db).active_for_task(
                (await TaskManager(db).get_by_task_id(task_id)).id
            )
            assert request is not None, "the request stays open until the condition passes"

    @pytest.mark.asyncio
    async def test_an_uncheckable_condition_asks_rather_than_assuming(self, factory, uncheckable):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="done",
            )
            await db.commit()
            assert outcome.status == "NEEDS_MORE_INFO"
            assert outcome.verification["checkable"] is False

        async with factory() as db:
            assert (
                await TaskManager(db).get_by_task_id(task_id)
            ).status == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_a_clarification_alone_does_not_clear_a_blocker_that_remains(
        self, factory, never_ok
    ):
        """A directive is not a claim that the blocker was fixed."""
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Use BNB Smart Chain instead.",
            )
            await db.commit()
            assert outcome.status == "NEEDS_MORE_INFO"
            assert outcome.resumed is False

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            instructions = task.operator_instructions or []
            assert any("bnb" in i["instruction"].lower() for i in instructions), (
                "the instruction is still recorded even though the task did not resume"
            )
            assert task.status == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_a_clarification_is_kept_when_the_blocker_also_clears(self, factory, always_ok):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Session restored, and use BNB Smart Chain instead.",
            )
            await db.commit()
            assert outcome.status == "RESUMED"

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            assert any("bnb" in i["instruction"].lower() for i in (task.operator_instructions or []))

    @pytest.mark.asyncio
    async def test_a_clarification_with_no_open_blocker_is_recorded(self, factory):
        task_id = await _make_task(factory)
        async with factory() as db:
            conversation = await ConversationService(db).get_or_create_for_task(
                await TaskManager(db).get_by_task_id(task_id), OPERATOR_A
            )
            await db.commit()

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Use BNB Smart Chain instead.",
            )
            await db.commit()
            assert outcome.status == "CLARIFIED"
            assert outcome.resumed is False

    @pytest.mark.asyncio
    async def test_an_informational_reply_does_not_authorise_a_paused_task(self, factory):
        task_id = await _make_task(factory)
        await _park(
            factory, task_id,
            error_text="Emergency stop engaged; execution halted.",
            force_reason_code=OPERATOR_PAUSE,
            integration_ref=None,
        )

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="looks like everything is fine now",
            )
            await db.commit()
            assert outcome.status == "NEEDS_MORE_INFO"

        async with factory() as db:
            assert (
                await TaskManager(db).get_by_task_id(task_id)
            ).status == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_an_explicit_instruction_does_resume_a_paused_task(self, factory):
        task_id = await _make_task(factory)
        await _park(
            factory, task_id,
            error_text="Emergency stop engaged; execution halted.",
            force_reason_code=OPERATOR_PAUSE,
            integration_ref=None,
        )
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Yes, resume this task.",
            )
            await db.commit()
            assert outcome.status == "RESUMED"

        async with factory() as db:
            assert (await TaskManager(db).get_by_task_id(task_id)).status == TaskStatus.READY.value

    @pytest.mark.asyncio
    async def test_operator_pause_needs_explicit_authorisation(self):
        assert OPERATOR_PAUSE in REQUIRES_EXPLICIT_AUTHORIZATION

    @pytest.mark.asyncio
    async def test_cancellation_applies_to_that_task_only(self, factory, operator_a):
        first = await _make_task(factory)
        second = await _make_task(factory)
        await _park(factory, first)
        await _park(factory, second)

        async with factory() as db:
            conversation = await _first_conversation(db, first)
            task = await TaskManager(db).get_by_task_id(first)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Stop this task.",
            )
            await db.commit()
            assert outcome.status == "CANCELLED"

        async with factory() as db:
            assert (await TaskManager(db).get_by_task_id(first)).status == TaskStatus.CANCELLED.value
            assert (
                await TaskManager(db).get_by_task_id(second)
            ).status == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_a_reply_to_a_cancelled_task_never_resumes_it(self, factory, always_ok):
        task_id = await _make_task(factory, status=TaskStatus.CANCELLED)
        async with factory() as db:
            conversation = await ConversationService(db).get_or_create_for_task(
                await TaskManager(db).get_by_task_id(task_id), OPERATOR_A
            )
            await db.commit()
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.",
            )
            await db.commit()
            assert outcome.resumed is False
            assert task.status == TaskStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_a_task_that_used_its_retry_budget_is_not_requeued(self, factory, always_ok):
        task_id = await _make_task(factory, retry_count=5, max_retries=3)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.",
            )
            await db.commit()
            assert outcome.resumed is False
            assert "retries" in (outcome.detail or "") or outcome.status == "NEEDS_MORE_INFO"

        async with factory() as db:
            assert (
                await TaskManager(db).get_by_task_id(task_id)
            ).status == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_uncertain_side_effects_block_an_automatic_resume(self, factory, always_ok, monkeypatch):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async def _uncertain(self, task):
            return "step 3 (transact) was running"

        monkeypatch.setattr(
            "backend.app.tasks.concurrency.LeaseManager._uncertain_side_effects", _uncertain
        )

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.",
            )
            await db.commit()
            assert outcome.resumed is False
            assert "may already have taken effect" in (outcome.detail or "")


# ======================================================================
# 8, 11, 12, 13. Idempotency, serialisation, cancellation
# ======================================================================
class TestResumptionSafety:
    @pytest.mark.asyncio
    async def test_a_valid_reply_resumes_without_a_manual_retry(self, factory, always_ok):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.",
            )
            await db.commit()

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            # READY is what claim_next_task() selects. No Retry click, no retry_count bump.
            assert task.status == TaskStatus.READY.value
            assert task.retry_count == 0
            assert task.next_execution_at is None, "READY must not wait out a backoff"
        assert outcome.resumed is True
        assert always_ok, "the condition must actually have been checked"

    @pytest.mark.asyncio
    async def test_the_resumed_task_is_claimable_by_the_existing_worker(self, factory, always_ok):
        """The end of the loop: the same claim query the worker already uses picks it up."""
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.",
            )
            await db.commit()

        async with factory() as db:
            claimed = await TaskManager(db).claim_next_task("test-worker")
            assert claimed is not None
            assert claimed.task_id == task_id
            assert claimed.status == TaskStatus.IN_PROGRESS.value

    @pytest.mark.asyncio
    async def test_a_duplicate_reply_does_not_launch_a_second_worker(self, factory, always_ok):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            first = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.", client_token="tok-1",
            )
            await db.commit()
            assert first.status == "RESUMED"

        # A second identical submit (double click, network retry, reopened tab).
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            second = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.", client_token="tok-1",
            )
            await db.commit()
            assert second.status == "RECORDED"
            assert second.resumed is False
            user_messages = (
                await db.execute(
                    select(ConversationMessage).where(
                        ConversationMessage.conversation_id == conversation.id,
                        ConversationMessage.role == "user",
                    )
                )
            ).scalars().all()
            assert len(user_messages) == 1

    @pytest.mark.asyncio
    async def test_concurrent_resume_attempts_are_serialised(self, factory, always_ok):
        """Two replies racing on one conversation: one resumes, the other is recorded."""
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async def _send(factory_ref, token):
            async with factory_ref() as db:
                conversation = await _first_conversation(db, task_id)
                task = await TaskManager(db).get_by_task_id(task_id)
                outcome = await ReplyRouter(db).handle(
                    conversation=conversation, task=task,
                    owner_email=OPERATOR_A, text="Telegram session is ready.",
                    client_token=token,
                )
                await db.commit()
                return outcome.status

        first, second = await asyncio.gather(_send(factory, "a"), _send(factory, "b"))
        # Exactly one resume. The loser is told the task is already queued rather than
        # queueing it a second time.
        assert sorted([first, second]) == ["RECORDED", "RESUMED"]

        async with factory() as db:
            assert (await TaskManager(db).get_by_task_id(task_id)).status == TaskStatus.READY.value
            requests = (
                await db.execute(
                    select(InterventionRequest).where(InterventionRequest.status == "PENDING")
                )
            ).scalars().all()
            assert len(requests) == 0, "the request is closed exactly once"

    @pytest.mark.asyncio
    async def test_a_stale_reply_cannot_resume_a_cancelled_task(self, factory, always_ok):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            await TaskManager(db).transition(
                task, TaskStatus.CANCELLED, reason="cancelled elsewhere"
            )
            await db.commit()

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.",
            )
            await db.commit()
            assert outcome.resumed is False
            assert task.status == TaskStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_cancelling_a_task_closes_its_open_request(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="stop this task",
            )
            await db.commit()
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            assert conversation.requires_reply is False
            assert await InterventionService(db).active_for_conversation(conversation.id) is None

    @pytest.mark.asyncio
    async def test_the_emergency_stop_blocks_resumption(self, factory, always_ok):
        from backend.app.security import emergency_stop as stop_module

        task_id = await _make_task(factory)
        await _park(factory, task_id)
        stop_module.emergency_stop._stopped = True
        stop_module.emergency_stop._reason = "test"
        try:
            async with factory() as db:
                conversation = await _first_conversation(db, task_id)
                task = await TaskManager(db).get_by_task_id(task_id)
                outcome = await ReplyRouter(db).handle(
                    conversation=conversation, task=task,
                    owner_email=OPERATOR_A, text="Telegram session is ready.",
                )
                await db.commit()
                assert outcome.resumed is False
        finally:
            stop_module.emergency_stop._stopped = False
            stop_module.emergency_stop._reason = None


# ======================================================================
# 4 & 5. Authorisation at the HTTP boundary
# ======================================================================
class TestInboxApiSecurity:
    @pytest.mark.asyncio
    async def test_another_operator_cannot_read_the_conversation(
        self, factory, operator_a, operator_b
    ):
        task_id = await _make_task(factory, owner=OPERATOR_A)
        await _park(factory, task_id)
        async with factory() as db:
            conversation_id = str((await _first_conversation(db, task_id)).id)

        async with _client(operator_b) as client:
            response = await client.get(f"/api/v1/inbox/conversations/{conversation_id}")
            assert response.status_code == 404, "must be indistinguishable from 'not found'"

    @pytest.mark.asyncio
    async def test_another_operator_cannot_reply_to_the_conversation(
        self, factory, operator_a, operator_b, always_ok
    ):
        task_id = await _make_task(factory, owner=OPERATOR_A)
        await _park(factory, task_id)
        async with factory() as db:
            conversation_id = str((await _first_conversation(db, task_id)).id)

        async with _client(operator_b) as client:
            response = await client.post(
                f"/api/v1/inbox/conversations/{conversation_id}/messages",
                json={"message": "Telegram session is ready."},
            )
            assert response.status_code == 404

        async with factory() as db:
            assert (
                await TaskManager(db).get_by_task_id(task_id)
            ).status == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_a_client_supplied_task_id_cannot_pick_the_target_task(
        self, factory, operator_a, always_ok
    ):
        """The body carries no task id at all, so there is nothing to forge."""
        mine = await _make_task(factory, owner=OPERATOR_A)
        theirs = await _make_task(factory, owner=OPERATOR_B)
        await _park(factory, mine)
        await _park(factory, theirs, owner=OPERATOR_B)
        async with factory() as db:
            conversation_id = str((await _first_conversation(db, mine)).id)

        async with _client(operator_a) as client:
            response = await client.post(
                f"/api/v1/inbox/conversations/{conversation_id}/messages",
                json={"message": "Telegram session is ready.", "task_id": theirs},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["task_id"] == mine

        async with factory() as db:
            assert (await TaskManager(db).get_by_task_id(mine)).status == TaskStatus.READY.value
            assert (
                await TaskManager(db).get_by_task_id(theirs)
            ).status == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_the_inbox_requires_authentication(self, factory):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            conversation_id = str((await _first_conversation(db, task_id)).id)

        async with _client(None) as client:
            assert (await client.get("/api/v1/inbox/conversations")).status_code == 401
            assert (await client.get("/api/v1/inbox/summary")).status_code == 401
            assert (
                await client.get(f"/api/v1/inbox/conversations/{conversation_id}")
            ).status_code == 401
            assert (
                await client.post(
                    f"/api/v1/inbox/conversations/{conversation_id}/messages",
                    json={"message": "hello"},
                )
            ).status_code == 401

    @pytest.mark.asyncio
    async def test_read_only_scope_cannot_send_a_reply(self, factory):
        # A session with `read` but without `agent_chat`/`task_write` must not be able to
        # requeue a task by replying to its conversation.
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            conversation_id = str((await _first_conversation(db, task_id)).id)

        read_only = await _session_for("readonly@test.local", [Scope.READ, Scope.WALLET_READ], factory)
        async with _client(read_only) as client:
            response = await client.post(
                f"/api/v1/inbox/conversations/{conversation_id}/messages",
                json={"message": "Telegram session is ready."},
            )
            assert response.status_code == 403


# ======================================================================
# Listing, ordering and the API surface
# ======================================================================
class TestInboxListing:
    @pytest.mark.asyncio
    async def test_needs_reply_conversations_sort_first(self, factory, operator_a):
        waiting = await _make_task(factory)
        quiet = await _make_task(factory)
        await _park(factory, waiting)

        # A conversation with no open blocker is ordinary history, not a reply prompt.
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(quiet)
            conversation = await ConversationService(db).get_or_create_for_task(task, OPERATOR_A)
            await ConversationService(db).append_message(
                conversation, role="assistant", content="all good"
            )
            await db.commit()

        async with _client(operator_a) as client:
            body = (await client.get("/api/v1/inbox/conversations")).json()

        assert body["needs_reply"] == 1
        assert body["conversations"][0]["requires_reply"] is True
        assert body["conversations"][0]["task_id"] == waiting

    @pytest.mark.asyncio
    async def test_the_list_reports_a_preview_and_the_open_request(
        self, factory, operator_a, always_ok
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with _client(operator_a) as client:
            listed = (await client.get("/api/v1/inbox/conversations")).json()
            entry = next(c for c in listed["conversations"] if c["task_id"] == task_id)
            assert entry["last_message_preview"]
            assert entry["requires_reply"] is True

            detail = (
                await client.get(f"/api/v1/inbox/conversations/{entry['conversation_id']}")
            ).json()
            assert detail["open_request"]["reason_code"] == TELEGRAM_SESSION_UNAVAILABLE
            assert detail["task"]["task_id"] == task_id
            assert detail["task"]["detail_url"] == f"/tasks/{task_id}"
            assert detail["messages"]

    @pytest.mark.asyncio
    async def test_the_summary_counts_only_the_callers_conversations(
        self, factory, operator_a, operator_b
    ):
        mine = await _make_task(factory, owner=OPERATOR_A)
        await _park(factory, mine)
        await _park(factory, await _make_task(factory, owner=OPERATOR_B), owner=OPERATOR_B)

        async with _client(operator_a) as client:
            body = (await client.get("/api/v1/inbox/summary")).json()
        assert body["needs_reply"] == 1
        assert body["conversations"] == 1

    @pytest.mark.asyncio
    async def test_search_filters_by_task_id_and_title(self, factory, operator_a):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with _client(operator_a) as client:
            body = (await client.get(f"/api/v1/inbox/conversations?search={task_id}")).json()
        assert body["total"] == 1
        assert body["conversations"][0]["task_id"] == task_id

    @pytest.mark.asyncio
    async def test_a_conversation_for_a_deleted_task_is_reported_not_faked(
        self, factory, operator_a
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            conversation_id = str(conversation.id)
            task = await TaskManager(db).get_by_task_id(task_id)
            await db.delete(task)
            await db.commit()

        async with _client(operator_a) as client:
            body = (
                await client.get(f"/api/v1/inbox/conversations/{conversation_id}")
            ).json()
            assert body["task"] is None, "no invented task data"
            assert body["messages"], "the history is still readable"


# ======================================================================
# 15, 16, 17, 18. Checkpoints and recovery
# ======================================================================
class TestCheckpointReuse:
    @pytest.mark.asyncio
    async def test_the_checkpoint_is_reused_when_the_task_resumes(self, factory, always_ok):
        from backend.app.tasks.checkpoints import CheckpointManager

        task_id = await _make_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.",
            )
            await db.commit()

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            checkpoint = await CheckpointManager(db).load_latest(task)
            assert checkpoint is not None
            assert CheckpointManager.should_resume(task.status), (
                "a requeued task must be resumable from its checkpoint"
            )

    @pytest.mark.asyncio
    async def test_a_missing_checkpoint_is_reported_explicitly(self, factory, always_ok):
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            await db.execute(delete_checkpoints(task.id))
            await db.commit()

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task,
                owner_email=OPERATOR_A, text="Telegram session is ready.",
            )
            await db.commit()
            assert outcome.status == "RESUMED"

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            messages = await ConversationService(db).list_messages(conversation.id)
            assert any("No checkpoint was saved" in m.content for m in messages)

    @pytest.mark.asyncio
    async def test_the_emergency_stop_park_is_an_operator_pause(self, factory):
        from backend.app.agent.controller import AgentController

        task_id = await _make_task(factory, status=TaskStatus.IN_PROGRESS)
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            await AgentController(db)._park_for_emergency_stop(task)
            await db.commit()

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            assert task.status == TaskStatus.NEEDS_USER_ACTION.value
            request = await InterventionService(db).active_for_task(task.id)
            assert request is not None
            assert request.reason_code == OPERATOR_PAUSE


# ======================================================================
# Shared helpers for the sections below
# ======================================================================
async def _running_task(factory, *, owner: str = OPERATOR_A) -> str:
    """A task already claimed by a worker, i.e. one that can hit a blocking condition."""
    async with factory() as db:
        task = await TaskManager(db).create_task(
            title=f't{uuid.uuid4().hex[:6]}', description='d', owner_email=owner
        )
        await TaskManager(db).transition(task, TaskStatus.READY, reason='queued')
        await TaskManager(db).transition(task, TaskStatus.IN_PROGRESS, reason='claimed')
        await db.commit()
        return task.task_id


# ======================================================================
# Task creation actually reaches the worker queue
# ======================================================================
class TestApiCreatedTasksAreClaimable:
    @pytest.mark.asyncio
    async def test_api_created_task_is_actually_claimable(self, factory, operator_a):
        """The task the API accepts must reach the worker queue.

        Fails on 9badc41: POST /api/v1/tasks persists CREATED and never transitions, while
        claim_next_task() only selects READY/RETRYING, so the task sits forever.
        """
        async with _client(operator_a) as client:
            created = await client.post(
                "/api/v1/tasks", json={"title": "API created", "description": "x"}
            )
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            assert task.status != TaskStatus.CREATED.value, (
                f"an accepted task is left in {task.status} and no worker will ever claim it"
            )

        async with factory() as db:
            claimed = await TaskManager(db).claim_next_task("worker-a")
            assert claimed is not None, "no worker could claim an API-created task"
            assert claimed.task_id == task_id
            assert claimed.status == TaskStatus.IN_PROGRESS.value

    @pytest.mark.asyncio
    async def test_two_workers_cannot_claim_the_same_api_task(self, factory, operator_a):
        async with _client(operator_a) as client:
            created = await client.post("/api/v1/tasks", json={"title": "Only once"})
        task_id = created.json()["task_id"]

        async with factory() as db:
            first = await TaskManager(db).claim_next_task("worker-a")
            second = await TaskManager(db).claim_next_task("worker-b")
            await db.commit()
        assert first is not None and first.task_id == task_id
        assert second is None, "the same task was claimed twice"

    @pytest.mark.asyncio
    async def test_manager_still_creates_unqueued_so_other_paths_can_validate(self, factory):
        """Regression guard on the contract the agent chat and create_task tool rely on.

        Those two paths call TaskManager.create_task and then queue the task themselves,
        after their own validation. The fix must NOT change create_task's default status,
        or they would be creating an already-queued task and their transition would fail.
        """
        async with factory() as db:
            task = await TaskManager(db).create_task(
                title="raw manager creation", description="d", owner_email=OPERATOR_A
            )
            assert task.status == TaskStatus.CREATED.value, (
                "create_task now pre-queues tasks, which breaks the chat and tool paths"
            )
            # The caller queues it, which is what they already do.
            await TaskManager(db).transition(task, TaskStatus.READY, reason="queued for the worker")
            await db.commit()
        async with factory() as db:
            claimed = await TaskManager(db).claim_next_task("w")
            assert claimed is not None and claimed.status == TaskStatus.IN_PROGRESS.value

    @pytest.mark.asyncio
    async def test_invalid_task_is_never_created_or_runnable(self, factory, operator_a):
        """An unvalidated task must not reach the queue at all."""
        async with _client(operator_a) as client:
            bad_priority = await client.post(
                "/api/v1/tasks", json={"title": "bad", "priority": 99}
            )
            empty_title = await client.post("/api/v1/tasks", json={"title": ""})
        assert bad_priority.status_code == 422
        assert empty_title.status_code == 422

        async with factory() as db:
            assert await TaskManager(db).claim_next_task("w") is None

    @pytest.mark.asyncio
    async def test_unauthenticated_creation_is_refused(self, factory):
        async with _client(None) as client:
            assert (await client.post("/api/v1/tasks", json={"title": "nope"})).status_code == 401

    @pytest.mark.asyncio
    async def test_ownership_is_recorded_so_the_inbox_can_scope_it(self, factory, operator_a):
        async with _client(operator_a) as client:
            created = await client.post("/api/v1/tasks", json={"title": "owned"})
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(created.json()["task_id"])
            assert task.owner_email == OPERATOR_A

    @pytest.mark.asyncio
    async def test_a_failed_creation_persists_nothing(self, factory, operator_a):
        """A rejected creation must leave no runnable task behind."""
        async with factory() as db:
            before = await db.scalar(select(Task))
            assert before is None
        async with _client(operator_a) as client:
            await client.post("/api/v1/tasks", json={"title": ""})
        async with factory() as db:
            assert await db.scalar(select(Task)) is None
            assert await TaskManager(db).claim_next_task("w") is None


# ======================================================================
# Live session verification never claims more than it checked
# ======================================================================
def _gateway_session(status: str, *, waiting: Any = None, key: str = "p1", integration: str = "browser") -> Dict[str, Any]:
    """The exact shape the worker gateway returns: a {"session": ...} wrapper."""
    return {
        "session": {
            "id": f"{integration}:{key}",
            "integration": integration,
            "key": key,
            "status": status,
            "status_detail": None,
            "waiting": waiting,
            "task": None,
            "meta": {"connected": True},
        }
    }


class TestLiveSessionVerification:
    """Every case below is a distinct way the old verifier could claim success wrongly."""

    @staticmethod
    def _install(monkeypatch, payload):
        calls: List[str] = []

        async def _get(path, params=None):
            calls.append(path)
            if isinstance(payload, Exception):
                raise payload
            return payload

        monkeypatch.setattr("backend.app.live.client.get_json", _get)
        return calls

    @staticmethod
    def _request():
        class _R:
            reason_code = "HUMAN_VERIFICATION"
            integration_ref = "browser:p1"
            request_id = "IR-x"
            problem_summary = "captcha"
        return _R()

    @staticmethod
    def _task():
        class _T:
            task_id = "AIR-2026-000001"
        return _T()

    @pytest.mark.asyncio
    async def test_waiting_session_is_not_verified(self, monkeypatch):
        """THE BUG: a session still waiting on the human was reported as verified."""
        self._install(
            monkeypatch,
            _gateway_session("waiting_for_user", waiting={"reason": "solve the captcha"}),
        )
        result = await verification_module._verify_live_session(
            None, self._task(), self._request()
        )
        assert result.ok is False, "a session still waiting for the user was treated as resolved"
        assert result.checkable is True

    @pytest.mark.asyncio
    async def test_stopped_session_is_not_verified(self, monkeypatch):
        self._install(monkeypatch, _gateway_session("stopped"))
        result = await verification_module._verify_live_session(None, self._task(), self._request())
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_failed_session_is_not_verified(self, monkeypatch):
        self._install(monkeypatch, _gateway_session("failed"))
        result = await verification_module._verify_live_session(None, self._task(), self._request())
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_paused_session_is_not_verified(self, monkeypatch):
        self._install(monkeypatch, _gateway_session("paused"))
        result = await verification_module._verify_live_session(None, self._task(), self._request())
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_running_session_is_verified(self, monkeypatch):
        self._install(monkeypatch, _gateway_session("running"))
        result = await verification_module._verify_live_session(None, self._task(), self._request())
        assert result.ok is True
        assert result.checkable is True

    @pytest.mark.asyncio
    async def test_missing_status_is_inconclusive_not_success(self, monkeypatch):
        """A response without a status must never be read as 'active'."""
        payload = {"session": {"integration": "browser", "key": "p1", "meta": {"connected": True}}}
        self._install(monkeypatch, payload)
        result = await verification_module._verify_live_session(None, self._task(), self._request())
        assert result.ok is False
        assert result.checkable is False

    @pytest.mark.asyncio
    async def test_malformed_response_is_inconclusive(self, monkeypatch):
        self._install(monkeypatch, {"session": "not-a-dict"})
        result = await verification_module._verify_live_session(None, self._task(), self._request())
        assert result.ok is False
        assert result.checkable is False

    @pytest.mark.asyncio
    async def test_unexpected_status_is_inconclusive(self, monkeypatch):
        self._install(monkeypatch, _gateway_session("totally-unknown"))
        result = await verification_module._verify_live_session(None, self._task(), self._request())
        assert result.ok is False
        assert result.checkable is False

    @pytest.mark.asyncio
    async def test_wrong_session_identity_is_inconclusive(self, monkeypatch):
        """A stale reference must not be satisfied by some other session's state."""
        self._install(monkeypatch, _gateway_session("running", key="some-other-profile"))
        result = await verification_module._verify_live_session(None, self._task(), self._request())
        assert result.ok is False
        assert result.checkable is False

    @pytest.mark.asyncio
    async def test_missing_session_reference_is_inconclusive(self, monkeypatch):
        self._install(monkeypatch, _gateway_session("running"))

        class _R:
            reason_code = "HUMAN_VERIFICATION"
            integration_ref = None
            request_id = "IR-x"
            problem_summary = "captcha"

        result = await verification_module._verify_live_session(None, self._task(), _R())
        assert result.ok is False
        assert result.checkable is False

    @pytest.mark.asyncio
    async def test_gateway_unavailable_is_inconclusive(self, monkeypatch):
        from backend.app.live import client as gateway_mod

        self._install(monkeypatch, gateway_mod.GatewayUnavailable("down"))
        result = await verification_module._verify_live_session(None, self._task(), self._request())
        assert result.ok is False
        assert result.checkable is False

    @pytest.mark.asyncio
    async def test_session_not_found_is_reported_not_assumed_ok(self, monkeypatch):
        from backend.app.live import client as gateway_mod

        self._install(monkeypatch, gateway_mod.GatewayError(404, "not_found", "Session not found."))
        result = await verification_module._verify_live_session(None, self._task(), self._request())
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_gateway_timeout_is_inconclusive(self, monkeypatch):
        """A hanging gateway must not be read as a verified condition."""

        async def _hang(path, params=None):
            await asyncio.sleep(30)

        monkeypatch.setattr("backend.app.live.client.get_json", _hang)
        monkeypatch.setattr(verification_module, "VERIFY_TIMEOUT_SECONDS", 0.05)
        result = await verification_module.verify(None, self._task(), self._request())
        assert result.ok is False
        assert result.checkable is False


# ======================================================================
# Telegram verification uses the client the worker owns
# ======================================================================
class TestTelegramVerificationUsesTheOwningClient:
    @pytest.mark.asyncio
    async def test_verifier_never_constructs_a_second_telethon_client(self, monkeypatch, tmp_path):
        """It must ask the worker that owns the client, not open its own on the same file."""
        from backend.app.core import config as config_mod

        # The readiness pre-check looks for a session file on disk. Point it at a
        # throwaway one so the test does not depend on the machine it runs on.
        session_base = tmp_path / "agent"
        (tmp_path / "agent.session").write_bytes(b"")
        monkeypatch.setattr(config_mod.settings, "TELEGRAM_SESSION_PATH", str(session_base), raising=False)
        monkeypatch.setattr(config_mod.settings, "TELEGRAM_USER_ENABLED", True, raising=False)
        monkeypatch.setattr(config_mod.settings, "TELEGRAM_API_ID", 12345, raising=False)
        monkeypatch.setattr(config_mod.settings, "TELEGRAM_API_HASH", "hash", raising=False)

        built: List[str] = []

        class _ExplodingClient:
            def __init__(self, *a, **k):
                built.append("constructed")
                raise AssertionError(
                    "the API process must not open a second Telethon client on the live session file"
                )

        monkeypatch.setitem(
            __import__("sys").modules, "telethon", type("m", (), {"TelegramClient": _ExplodingClient})
        )

        async def _get(path, params=None):
            return _gateway_session("running", key="agent", integration="telegram")

        monkeypatch.setattr("backend.app.live.client.get_json", _get)

        class _R:
            reason_code = TELEGRAM_SESSION_UNAVAILABLE
            integration_ref = "telegram:agent"
            request_id = "IR-x"
            problem_summary = "telegram"

        class _T:
            task_id = "AIR-2026-000001"

        result = await verification_module._verify_telegram_session(None, _T(), _R())
        assert built == [], "a second Telegram client was constructed"
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_worker_reports_disconnected_so_it_is_not_verified(self, monkeypatch):
        from backend.app.core import config as config_mod

        monkeypatch.setattr(config_mod.settings, "TELEGRAM_USER_ENABLED", True, raising=False)
        monkeypatch.setattr(config_mod.settings, "TELEGRAM_API_ID", 12345, raising=False)
        monkeypatch.setattr(config_mod.settings, "TELEGRAM_API_HASH", "hash", raising=False)

        payload = _gateway_session("running", key="agent", integration="telegram")
        payload["session"]["meta"]["connected"] = False

        async def _get(path, params=None):
            return payload

        monkeypatch.setattr("backend.app.live.client.get_json", _get)

        class _R:
            reason_code = TELEGRAM_SESSION_UNAVAILABLE
            integration_ref = "telegram:agent"
            request_id = "IR-x"
            problem_summary = "telegram"

        class _T:
            task_id = "AIR-2026-000001"

        result = await verification_module._verify_telegram_session(None, _T(), _R())
        assert result.ok is False, "a disconnected client was reported as authorised"


# ======================================================================
# A clarification is recorded exactly once
# ======================================================================
class TestClarificationStoredOnce:
    @pytest.mark.asyncio
    async def test_clarification_is_stored_exactly_once(self, factory):
        """A pause + 'use BSC instead, resume this task' must record one instruction."""
        task_id = await _running_task(factory)
        await _park(
            factory, task_id, error_text="Emergency stop engaged.",
            force_reason_code=OPERATOR_PAUSE, integration_ref=None,
        )

        async with factory() as db:
            conversation = await db.scalar(
                select(AgentConversation).where(AgentConversation.task_id.is_not(None)).limit(1)
            )
            task = await TaskManager(db).get_by_task_id(task_id)
            await ReplyRouter(db).handle(
                conversation=conversation, task=task, owner_email=OPERATOR_A,
                text="Use BNB Smart Chain instead, and resume this task.",
            )
            await db.commit()

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            instructions = task.operator_instructions or []
            assert len(instructions) == 1, (
                f"the same instruction was recorded {len(instructions)} times"
            )


# ======================================================================
# A negated instruction is not a cancellation
# ======================================================================
class TestNegatedCancellation:
    @pytest.mark.asyncio
    async def test_do_not_stop_is_not_a_cancellation(self, factory):
        task_id = await _running_task(factory)
        await _park(factory, task_id)

        async with factory() as db:
            conversation = await db.scalar(
                select(AgentConversation).where(AgentConversation.task_id.is_not(None)).limit(1)
            )
            task = await TaskManager(db).get_by_task_id(task_id)
            await ReplyRouter(db).handle(
                conversation=conversation, task=task, owner_email=OPERATOR_A,
                text="Do not stop this task. I fixed it.",
            )
            await db.commit()

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            assert task.status != TaskStatus.CANCELLED.value, (
                "a negated instruction cancelled the task"
            )


# ======================================================================
# The agent does not promise a resume it cannot perform
# ======================================================================
class TestHonestPromisesWithoutAnOpenBlocker:
    @pytest.mark.asyncio
    async def test_explicit_resume_without_a_blocker_is_handled_honestly(self, factory):
        task_id = await _running_task(factory)
        async with factory() as db:
            await ConversationService(db).get_or_create_for_task(
                await TaskManager(db).get_by_task_id(task_id), OPERATOR_A
            )
            await db.commit()

        async with factory() as db:
            conversation = await db.scalar(
                select(AgentConversation).where(AgentConversation.task_id.is_not(None)).limit(1)
            )
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task, owner_email=OPERATOR_A,
                text="resume this task",
            )
            await db.commit()
            assert outcome.resumed is False

        async with factory() as db:
            conversation = await db.scalar(
                select(AgentConversation).where(AgentConversation.task_id.is_not(None)).limit(1)
            )
            messages = await ConversationService(db).list_messages(conversation.id)
            body = messages[-1].content
            assert "no open request" in body.lower(), (
                f"the agent did not explain that there is nothing to resume: {body!r}"
            )


# ======================================================================
# Replies that are not in English
# ======================================================================
class TestBengaliReplies:
    @pytest.mark.asyncio
    async def test_bengali_resume_of_an_operator_pause(self, factory):
        task_id = await _running_task(factory)
        await _park(
            factory, task_id, error_text="Emergency stop engaged.",
            force_reason_code=OPERATOR_PAUSE, integration_ref=None,
        )
        async with factory() as db:
            conversation = await db.scalar(
                select(AgentConversation).where(AgentConversation.task_id.is_not(None)).limit(1)
            )
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task, owner_email=OPERATOR_A,
                text="হ্যাঁ, এখন চালাও।",
            )
            await db.commit()
            assert outcome.resumed is True, "a clear Bengali resume instruction was ignored"

    @pytest.mark.asyncio
    async def test_bengali_clarification_is_recorded(self, factory):
        task_id = await _running_task(factory)
        async with factory() as db:
            await ConversationService(db).get_or_create_for_task(
                await TaskManager(db).get_by_task_id(task_id), OPERATOR_A
            )
            await db.commit()
        async with factory() as db:
            conversation = await db.scalar(
                select(AgentConversation).where(AgentConversation.task_id.is_not(None)).limit(1)
            )
            task = await TaskManager(db).get_by_task_id(task_id)
            await ReplyRouter(db).handle(
                conversation=conversation, task=task, owner_email=OPERATOR_A,
                text="এখন BSC ব্যবহার করো।",
            )
            await db.commit()
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            instructions = task.operator_instructions or []
            assert instructions, "a Bengali network instruction was dropped"

    @pytest.mark.asyncio
    async def test_bengali_informational_reply_does_not_clear_an_operator_pause(self, factory):
        """Bengali support must not weaken the pause rule."""
        task_id = await _running_task(factory)
        await _park(
            factory, task_id, error_text="Emergency stop engaged.",
            force_reason_code=OPERATOR_PAUSE, integration_ref=None,
        )
        async with factory() as db:
            conversation = await db.scalar(
                select(AgentConversation).where(AgentConversation.task_id.is_not(None)).limit(1)
            )
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task, owner_email=OPERATOR_A,
                text="সব ঠিক আছে।",
            )
            await db.commit()
            assert outcome.resumed is False


# ======================================================================
# Full round trip: create -> block -> answer -> resume, end to end
# ======================================================================
class FakeTelegram:
    """Deterministic stand-in for the Telegram session. Not a mock of our own code."""

    def __init__(self) -> None:
        self.signed_in = False
        self.sign_in_calls: List[str] = []

    def sign_in(self) -> None:
        self.signed_in = True


@pytest.fixture
def telegram(monkeypatch) -> FakeTelegram:
    fake = FakeTelegram()

    async def _verify(db, task, request):
        # Reports the real state of the double rather than a hard-coded "ok".
        if fake.signed_in:
            return VerificationResult.satisfied(
                method="telegram_is_user_authorized",
                outcome="The Telegram session reports it is connected and authorised.",
            )
        return VerificationResult.failed(
            method="telegram_is_user_authorized",
            outcome="The Telegram session reports the account is not authorised.",
            question="Please sign the Telegram session in, then tell me to re-check.",
        )

    monkeypatch.setitem(verification_module._REGISTRY, TELEGRAM_SESSION_UNAVAILABLE, _verify)
    return fake


class TestAgentInboxEndToEnd:
    """The whole promise in one test, over HTTP, with the real claim query.

    Uses this module's factory and operator_a fixtures, so it runs against the
    same database as the rest of the suite rather than a second one.
    """

    @pytest.mark.asyncio
    async def test_a_blocked_task_resumes_itself_after_one_good_reply(
        self, factory, operator_a, telegram
    ):
        made, token = factory, operator_a

        # ---------------------------------------------------------------- 1. create
        async with _client(token) as client:
            blocked_response = await client.post(
                "/api/v1/tasks",
                json={
                    "title": "Join the Telegram announcement channel",
                    "description": "Join the channel and report back.",
                    "priority": 3,
                },
            )
        assert blocked_response.status_code == 201, blocked_response.text
        blocked_id = blocked_response.json()["task_id"]

        async with _client(token) as client:
            other_response = await client.post(
                "/api/v1/tasks",
                json={"title": "Check an unrelated deadline", "description": "Read only.", "priority": 5},
            )
        other_id = other_response.json()["task_id"]

        # The unrelated task has a plan and is ready to run.
        async with made() as db:
            blocked = await TaskManager(db).get_by_task_id(blocked_id)
            other = await TaskManager(db).get_by_task_id(other_id)
            # Both arrive READY from the API now; the worker only has to claim them.
            assert blocked.status == TaskStatus.READY.value, (
                "an API-created task must arrive queued for the worker"
            )
            assert other.status == TaskStatus.READY.value
            await TaskManager(db).transition(blocked, TaskStatus.IN_PROGRESS, reason="claimed by worker")
            await TaskManager(db).transition(other, TaskStatus.IN_PROGRESS, reason="claimed by worker")
            await db.commit()

        # ------------------------------------------- 2. a blocking condition is hit
        async with made() as db:
            from backend.app.inbox.raise_intervention import park_for_intervention

            blocked = await TaskManager(db).get_by_task_id(blocked_id)
            result = await park_for_intervention(
                db,
                blocked,
                owner_email=OPERATOR_A,
                error_text="Telegram session is not logged in. Run the login script once.",
                error_category="AUTH_FAILURE",
                blocking_step="Step 1 (telegram_join_chat): join the announcement channel",
                integration_ref="telegram:agent",
            )
            await db.commit()

        assert result is not None and result["new_request"] is True

        # -------------------------------- 3. the agent messaged the Agent Inbox
        async with _client(token) as client:
            listing = (await client.get("/api/v1/inbox/conversations")).json()
            entry = next(c for c in listing["conversations"] if c["task_id"] == blocked_id)
            assert entry["requires_reply"] is True
            assert listing["needs_reply"] == 1

            detail = (await client.get(f"/api/v1/inbox/conversations/{entry['conversation_id']}")).json()

        body = detail["messages"][0]["content"]
        assert blocked_id in body
        assert "Telegram session" in body
        assert "What you need to do:" in body
        assert "I have saved the current task state" in body
        assert detail["open_request"]["status"] == "PENDING"
        assert detail["open_request"]["reason_code"] == TELEGRAM_SESSION_UNAVAILABLE

        # The unrelated task is still in its own conversation-free state and untouched.
        async with made() as db:
            assert (await TaskManager(db).get_by_task_id(other_id)).status == TaskStatus.IN_PROGRESS.value

        # ----------------------------------- 4. a reply that does not fix anything
        async with _client(token) as client:
            bad = await client.post(
                f"/api/v1/inbox/conversations/{entry['conversation_id']}/messages",
                json={"message": "I think it is fine now", "client_token": "reply-1"},
            )
        assert bad.status_code == 200, bad.text
        assert bad.json()["resumed"] is False
        assert bad.json()["verification"]["ok"] is False

        async with made() as db:
            assert (
                await TaskManager(db).get_by_task_id(blocked_id)
            ).status == TaskStatus.NEEDS_USER_ACTION.value

        # ------------------------------ 5. the external condition is fixed for real
        telegram.sign_in()

        async with _client(token) as client:
            good = await client.post(
                f"/api/v1/inbox/conversations/{entry['conversation_id']}/messages",
                json={"message": "Telegram session is ready.", "client_token": "reply-2"},
            )
        assert good.status_code == 200, good.text
        body = good.json()
        assert body["status"] == "RESUMED"
        assert body["resumed"] is True
        assert body["verification"]["ok"] is True

        # ------------- 6. the task resumes from its persisted checkpoint, no Retry click
        async with made() as db:
            blocked = await TaskManager(db).get_by_task_id(blocked_id)
            assert blocked.status == TaskStatus.READY.value
            assert blocked.retry_count == 0, "a verified answer is not a retry"
            assert blocked.next_execution_at is None
            checkpoint = await db.scalar(
                select(TaskCheckpoint)
                .where(TaskCheckpoint.task_id == blocked.id)
                .order_by(TaskCheckpoint.iteration.desc())
            )
            assert checkpoint is not None

        # A duplicate of the accepted reply must not queue a second run.
        async with _client(token) as client:
            duplicate = await client.post(
                f"/api/v1/inbox/conversations/{entry['conversation_id']}/messages",
                json={"message": "Telegram session is ready.", "client_token": "reply-2"},
            )
        assert duplicate.json()["resumed"] is False

        # The real worker claim query picks it up, exactly once.
        async with made() as db:
            claimed = await TaskManager(db).claim_next_task("worker-1")
            assert claimed is not None and claimed.task_id == blocked_id
            assert claimed.status == TaskStatus.IN_PROGRESS.value
            await db.commit()

        async with made() as db:
            assert await TaskManager(db).claim_next_task("worker-2") is None, (
                "a duplicate reply must not produce a second runnable task"
            )

        # -------- 7. the unrelated task is completely unaffected by any of the above
        async with made() as db:
            other = await TaskManager(db).get_by_task_id(other_id)
            assert other.status == TaskStatus.IN_PROGRESS.value
            assert other.retry_count == 0
            conversations = (
                await db.execute(
                    select(AgentConversation).where(AgentConversation.task_id == other.id)
                )
            ).scalars().all()
            assert conversations == [], "a task with no intervention gets no conversation"

        # --------------------------------------- 8. history is complete and durable
        async with _client(token) as client:
            final = (await client.get(f"/api/v1/inbox/conversations/{entry['conversation_id']}")).json()

        roles = [m["role"] for m in final["messages"]]
        assert roles.count("user") == 2, "one accepted reply; the duplicate was stored once"
        assert roles[0] == "assistant"
        assert final["open_request"] is None, "the request is closed once resolved"
        assert any("queued to resume" in m["content"] for m in final["messages"])


# ======================================================================
# Helpers
# ======================================================================
async def _task_pk(db, task_id: str):
    task = await TaskManager(db).get_by_task_id(task_id)
    assert task is not None
    return task.id


async def _first_conversation(db, task_id: str) -> AgentConversation:
    conversation = await db.scalar(
        select(AgentConversation)
        .where(AgentConversation.task_id == await _task_pk(db, task_id))
        .limit(1)
    )
    assert conversation is not None, "expected a conversation for this task"
    return conversation


async def _latest_checkpoint(db, task):
    from backend.app.tasks.checkpoints import CheckpointManager

    return await CheckpointManager(db).load_latest(task)


def delete_checkpoints(task_pk):
    from sqlalchemy import delete

    return delete(TaskCheckpoint).where(TaskCheckpoint.task_id == task_pk)