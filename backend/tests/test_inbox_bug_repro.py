"""Reproduction tests for the confirmed Agent Inbox / task lifecycle bugs.

These were written against commit 9badc41 BEFORE any fix, to prove each defect is real.
Run against the unfixed tree they fail; after the fix they pass and stay as regressions.

Bugs covered:
  B1  POST /api/v1/tasks creates a task in CREATED, which claim_next_task() never selects.
  B2  _verify_live_session() reads the gateway's {"session": ...} wrapper instead of the
      session inside it, so it reports every session as verified.
  B3  _verify_telegram_session() opens a second Telethon client on the live session file.
  B4  A reply that carries both a clarification and an explicit authorisation stores the
      clarification twice.
  B6  A negated cancellation ("don't stop this task") still cancels the task.
  B7  With no open request, an explicit resume is answered as a dead end.
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
from backend.app.inbox import verification as verification_module
from backend.app.inbox.replies import ReplyRouter
from backend.app.inbox.service import (
    OPERATOR_PAUSE,
    TELEGRAM_SESSION_UNAVAILABLE,
    ConversationService,
)
from backend.app.main import app
from backend.app.models import AgentConversation, Base, Task, User, UserSession
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

OWNER = "owner@test.local"


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
        db.add(
            User(
                email=email,
                hashed_password=hash_password("correct-horse-battery"),
                is_active=True,
                is_superuser=True,
            )
        )
        await db.flush()
        db.add(
            UserSession(
                user_id=(await db.scalar(select(User.id).where(User.email == email))),
                token=hash_token(token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                scopes=sorted(s.value for s in scopes),
            )
        )
        await db.commit()
    return token


@pytest_asyncio.fixture
async def operator(factory):
    return await _session_for(OWNER, list(Scope), factory)


def _client(token: Optional[str]) -> AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _park(factory, task_id: str, **overrides) -> Dict[str, Any]:
    from backend.app.inbox.raise_intervention import park_for_intervention

    async with factory() as db:
        task = await TaskManager(db).get_by_task_id(task_id)
        kwargs = dict(
            error_text="Telegram session is not logged in.",
            error_category="AUTH_FAILURE",
            blocking_step="Step 1 (telegram_join_chat)",
            integration_ref="telegram:agent",
        )
        kwargs.update(overrides)
        result = await park_for_intervention(db, task, owner_email=OWNER, **kwargs)
        await db.commit()
        return result


async def _running_task(factory, *, owner: str = OWNER) -> str:
    async with factory() as db:
        task = await TaskManager(db).create_task(
            title=f"t{uuid.uuid4().hex[:6]}", description="d", owner_email=owner
        )
        await TaskManager(db).transition(task, TaskStatus.READY, reason="queued")
        await TaskManager(db).transition(task, TaskStatus.IN_PROGRESS, reason="claimed")
        await db.commit()
        return task.task_id


# ======================================================================
# B1 - API-created tasks are stuck in CREATED
# ======================================================================
class TestApiCreatedTasksAreClaimable:
    @pytest.mark.asyncio
    async def test_api_created_task_is_actually_claimable(self, factory, operator):
        """The task the API accepts must reach the worker queue.

        Fails on 9badc41: POST /api/v1/tasks persists CREATED and never transitions, while
        claim_next_task() only selects READY/RETRYING, so the task sits forever.
        """
        async with _client(operator) as client:
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
    async def test_two_workers_cannot_claim_the_same_api_task(self, factory, operator):
        async with _client(operator) as client:
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
                title="raw manager creation", description="d", owner_email=OWNER
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
    async def test_invalid_task_is_never_created_or_runnable(self, factory, operator):
        """An unvalidated task must not reach the queue at all."""
        async with _client(operator) as client:
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
    async def test_ownership_is_recorded_so_the_inbox_can_scope_it(self, factory, operator):
        async with _client(operator) as client:
            created = await client.post("/api/v1/tasks", json={"title": "owned"})
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(created.json()["task_id"])
            assert task.owner_email == OWNER

    @pytest.mark.asyncio
    async def test_a_failed_creation_persists_nothing(self, factory, operator):
        """A rejected creation must leave no runnable task behind."""
        async with factory() as db:
            before = await db.scalar(select(Task))
            assert before is None
        async with _client(operator) as client:
            await client.post("/api/v1/tasks", json={"title": ""})
        async with factory() as db:
            assert await db.scalar(select(Task)) is None
            assert await TaskManager(db).claim_next_task("w") is None


# ======================================================================
# B2 - _verify_live_session reports everything as verified
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
# B3 - the Telegram verifier opens a second client on the live session file
# ======================================================================
class TestTelegramVerificationUsesTheOwningClient:
    @pytest.mark.asyncio
    async def test_verifier_never_constructs_a_second_telethon_client(self, monkeypatch):
        """It must ask the worker that owns the client, not open its own on the same file."""
        from backend.app.core import config as config_mod

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
# B4 - clarification stored twice
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
                conversation=conversation, task=task, owner_email=OWNER,
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
# B6 - negated cancellation still cancels
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
                conversation=conversation, task=task, owner_email=OWNER,
                text="Do not stop this task. I fixed it.",
            )
            await db.commit()

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            assert task.status != TaskStatus.CANCELLED.value, (
                "a negated instruction cancelled the task"
            )


# ======================================================================
# B7 - the agent promises a resume it cannot perform
# ======================================================================
class TestHonestPromisesWithoutAnOpenBlocker:
    @pytest.mark.asyncio
    async def test_explicit_resume_without_a_blocker_is_handled_honestly(self, factory):
        task_id = await _running_task(factory)
        async with factory() as db:
            await ConversationService(db).get_or_create_for_task(
                await TaskManager(db).get_by_task_id(task_id), OWNER
            )
            await db.commit()

        async with factory() as db:
            conversation = await db.scalar(
                select(AgentConversation).where(AgentConversation.task_id.is_not(None)).limit(1)
            )
            task = await TaskManager(db).get_by_task_id(task_id)
            outcome = await ReplyRouter(db).handle(
                conversation=conversation, task=task, owner_email=OWNER,
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
# B5 - replies that are not English
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
                conversation=conversation, task=task, owner_email=OWNER,
                text="হ্যাঁ, এখন চালাও।",
            )
            await db.commit()
            assert outcome.resumed is True, "a clear Bengali resume instruction was ignored"

    @pytest.mark.asyncio
    async def test_bengali_clarification_is_recorded(self, factory):
        task_id = await _running_task(factory)
        async with factory() as db:
            await ConversationService(db).get_or_create_for_task(
                await TaskManager(db).get_by_task_id(task_id), OWNER
            )
            await db.commit()
        async with factory() as db:
            conversation = await db.scalar(
                select(AgentConversation).where(AgentConversation.task_id.is_not(None)).limit(1)
            )
            task = await TaskManager(db).get_by_task_id(task_id)
            await ReplyRouter(db).handle(
                conversation=conversation, task=task, owner_email=OWNER,
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
                conversation=conversation, task=task, owner_email=OWNER,
                text="সব ঠিক আছে।",
            )
            await db.commit()
            assert outcome.resumed is False