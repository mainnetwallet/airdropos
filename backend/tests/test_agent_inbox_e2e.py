"""End-to-end: create a task, block it, answer it, and watch only that task resume.

This is the whole promise of the feature in one test. A task is created, a recoverable
blocking condition stops it mid-execution, the agent raises an intervention and explains
itself in that task's own conversation, the operator replies, a test double confirms the
external condition, and exactly one task -- the one that was blocked -- is picked back up by
the real worker claim query and resumes from its persisted checkpoint.

An unrelated task runs alongside it throughout and must be unaffected. The worker is replaced
with a deterministic double, and no Telegram, browser, chain or LLM is contacted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.database.session import get_db
from backend.app.inbox import verification as verification_module
from backend.app.inbox.service import TELEGRAM_SESSION_UNAVAILABLE
from backend.app.inbox.verification import VerificationResult
from backend.app.main import app
from backend.app.models import (
    AgentConversation,
    Base,
    TaskCheckpoint,
    User,
    UserSession,
)
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

OPERATOR = "integration@test.local"


@pytest_asyncio.fixture
async def env():
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

    token = generate_token()
    async with made() as db:
        user = User(
            email=OPERATOR,
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
                scopes=sorted(s.value for s in Scope),
            )
        )
        await db.commit()

    yield made, token
    app.dependency_overrides.clear()
    await engine.dispose()


def _client(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


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


@pytest.mark.asyncio
async def test_a_blocked_task_resumes_itself_after_one_good_reply(env, telegram):
    made, token = env

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
            owner_email=OPERATOR,
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