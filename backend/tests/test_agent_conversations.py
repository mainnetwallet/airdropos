"""Console chat conversations: unique ids, stored history, strict isolation.

Every chat has its own UUID and its own stored messages; the model is only ever shown the
messages of the conversation a request names. No real model or network is contacted.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agent import chat_loop
from backend.app.api.v1.endpoints import system as system_endpoints
from backend.app.database.session import get_db
from backend.app.main import app
from backend.app.models import (
    Base,
    ConversationMessage,
    Task,
    User,
    UserSession,
)
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token
from backend.tests.test_chat_streaming import FakeModel, FakeRegistry, _json_body


class RecordingModel(FakeModel):
    """Replies 'ok <n>' and remembers exactly which messages the model was shown."""

    def __init__(self) -> None:
        super().__init__([])
        self.seen: List[List[Dict[str, Any]]] = []

    async def complete(self, messages, **kwargs):
        self.seen.append([{"role": m.role, "content": m.content} for m in messages])
        self.complete_calls += 1
        from backend.tests.test_chat_streaming import _response

        return _response(_json_body(f"ok {self.complete_calls}"))

    def texts_shown(self, call: int = -1) -> str:
        return "\n".join(m["content"] for m in self.seen[call] if m["role"] != "system")


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path):
    # A file database with a real connection pool: concurrent requests get their own
    # connections and transactions, like PostgreSQL, instead of sharing a single one.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chats.db'}")
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

    tokens: Dict[str, str] = {}
    async with made() as db:
        for name in ("alice", "bob"):
            token = generate_token()
            user = User(
                email=f"{name}@test.local",
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
            tokens[name] = token
        await db.commit()

    from backend.app.security.auth import rate_limiter

    rate_limiter.reset()
    model = RecordingModel()
    monkeypatch.setattr(chat_loop, "llm", model)
    monkeypatch.setattr(system_endpoints, "registry", FakeRegistry())

    def client(who: str) -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {tokens[who]}"},
        )

    yield SimpleEnv(client, model, made)
    rate_limiter.reset()
    app.dependency_overrides.clear()
    await engine.dispose()


class SimpleEnv:
    def __init__(self, client, model, made) -> None:
        self.client = client
        self.model = model
        self.made = made


async def _new(c: AsyncClient) -> str:
    response = await c.post("/api/v1/agent/conversations")
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _say(c: AsyncClient, conversation_id: str, text: str, **extra: Any):
    return await c.post(
        "/api/v1/agent/chat",
        json={"message": text, "conversation_id": conversation_id, **extra},
    )


async def test_every_new_conversation_has_a_distinct_uuid(env) -> None:
    async with env.client("alice") as c:
        ids = {await _new(c) for _ in range(5)}
    assert len(ids) == 5
    assert all(uuid.UUID(i) for i in ids)


async def test_messages_are_stored_under_their_own_conversation(env) -> None:
    async with env.client("alice") as c:
        a, b = await _new(c), await _new(c)
        await _say(c, a, "about Arky.bet")
        await _say(c, b, "teach me Python")
        got_a = (await c.get(f"/api/v1/agent/conversations/{a}")).json()
        got_b = (await c.get(f"/api/v1/agent/conversations/{b}")).json()
    assert [m["content"] for m in got_a["messages"]] == ["about Arky.bet", "ok 1"]
    assert [m["content"] for m in got_b["messages"]] == ["teach me Python", "ok 2"]
    assert [m["seq"] for m in got_a["messages"]] == [1, 2]


async def test_conversation_a_never_reaches_the_model_in_conversation_b(env) -> None:
    async with env.client("alice") as c:
        a, b = await _new(c), await _new(c)
        await _say(c, a, "SECRET-ARKY-123")
        await _say(c, b, "teach me Python")
        assert "SECRET-ARKY-123" not in env.model.texts_shown()
        # ...and a client-supplied history cannot smuggle it in either: the server's own wins.
        await _say(
            c, b, "again", history=[{"role": "user", "content": "SECRET-ARKY-123"}]
        )
        assert "SECRET-ARKY-123" not in env.model.texts_shown()
        # Going back to A still has A's history.
        await _say(c, a, "continue")
        assert "SECRET-ARKY-123" in env.model.texts_shown()
        assert "teach me Python" not in env.model.texts_shown()


async def test_another_users_conversation_is_not_found(env) -> None:
    async with env.client("alice") as alice:
        a = await _new(alice)
        await _say(alice, a, "alice private")
    calls_before = env.model.complete_calls
    async with env.client("bob") as bob:
        assert (await bob.get(f"/api/v1/agent/conversations/{a}")).status_code == 404
        assert (await bob.delete(f"/api/v1/agent/conversations/{a}")).status_code == 404
        assert (await _say(bob, a, "hijack")).status_code == 404
        assert (await bob.get("/api/v1/agent/conversations")).json()["count"] == 0
    # Bob's hijack attempt never reached the model at all, and stored nothing.
    assert env.model.complete_calls == calls_before
    async with env.client("alice") as alice:
        assert len((await alice.get(f"/api/v1/agent/conversations/{a}")).json()["messages"]) == 2


async def test_malformed_id_is_rejected(env) -> None:
    async with env.client("alice") as c:
        assert (await _say(c, "not-a-uuid", "hi")).status_code == 422
        assert (await c.get("/api/v1/agent/conversations/not-a-uuid")).status_code == 404


async def test_listing_shows_only_used_chats_newest_first(env) -> None:
    async with env.client("alice") as c:
        empty = await _new(c)
        a, b = await _new(c), await _new(c)
        await _say(c, a, "first chat")
        await _say(c, b, "second chat")
        listed = (await c.get("/api/v1/agent/conversations")).json()["conversations"]
    assert [x["id"] for x in listed] == [b, a]
    assert empty not in [x["id"] for x in listed]
    assert listed[0]["title"] == "second chat"


async def test_unknown_id_is_created_and_seeded_once_from_browser_history(env) -> None:
    fresh = str(uuid.uuid4())
    async with env.client("alice") as c:
        await _say(
            c,
            fresh,
            "next",
            history=[
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
        )
        await _say(c, fresh, "again", history=[{"role": "user", "content": "IGNORED"}])
        got = (await c.get(f"/api/v1/agent/conversations/{fresh}")).json()
    contents = [m["content"] for m in got["messages"]]
    assert contents[:2] == ["old question", "old answer"] and "IGNORED" not in contents
    assert len(contents) == 6


async def test_keep_messages_supports_edit_and_resend(env) -> None:
    async with env.client("alice") as c:
        a = await _new(c)
        await _say(c, a, "one")
        await _say(c, a, "two")
        await _say(c, a, "two edited", keep_messages=2)
        got = (await c.get(f"/api/v1/agent/conversations/{a}")).json()
    assert [m["content"] for m in got["messages"]][:3] == ["one", "ok 1", "two edited"]
    assert "two" not in env.model.texts_shown().replace("two edited", "")


async def test_concurrent_conversations_stay_isolated(env) -> None:
    async with env.client("alice") as c:
        ids = [await _new(c) for _ in range(4)]
        await asyncio.gather(*[_say(c, cid, f"MARK-{i}") for i, cid in enumerate(ids)])
        for i, cid in enumerate(ids):
            got = (await c.get(f"/api/v1/agent/conversations/{cid}")).json()
            users = [m["content"] for m in got["messages"] if m["role"] == "user"]
            assert users == [f"MARK-{i}"]


async def test_history_survives_a_new_session(env) -> None:
    async with env.client("alice") as c:
        a = await _new(c)
        await _say(c, a, "remember me")
    # A brand-new client / DB session (what a restart looks like) reads the same rows.
    async with env.client("alice") as c2:
        got = (await c2.get(f"/api/v1/agent/conversations/{a}")).json()
    assert got["messages"][0]["content"] == "remember me"


async def test_deleting_a_chat_never_deletes_a_task(env) -> None:
    async with env.made() as db:
        task = Task(
            task_id="AIR-2026-10-16-000001",
            title="keep me",
            description="d",
            status="READY",
            owner_email="alice@test.local",
        )
        db.add(task)
        await db.commit()
    async with env.client("alice") as c:
        a = await _new(c)
        await _say(c, a, "make a task")
        result = (await c.delete(f"/api/v1/agent/conversations/{a}")).json()
        assert result["deleted"] is True and result["tasks_affected"] == 0
        assert (await c.get(f"/api/v1/agent/conversations/{a}")).status_code == 404
    async with env.made() as db:
        assert await db.scalar(select(func.count()).select_from(Task)) == 1
        left = await db.scalar(
            select(func.count()).select_from(ConversationMessage).where(
                ConversationMessage.conversation_id == uuid.UUID(a)
            )
        )
        assert left == 0


async def test_console_chats_are_not_inbox_conversations(env) -> None:
    async with env.client("alice") as c:
        a = await _new(c)
        await _say(c, a, "hello")
        inbox = await c.get("/api/v1/inbox/conversations")
        assert a not in str(inbox.json())
        assert (await c.get(f"/api/v1/inbox/conversations/{a}")).status_code == 404


async def test_stateless_calls_without_an_id_still_work(env) -> None:
    async with env.client("alice") as c:
        r = await c.post(
            "/api/v1/agent/chat",
            json={"message": "hi", "history": [{"role": "user", "content": "earlier"}]},
        )
    assert r.status_code == 200 and r.json()["conversation_id"] is None
    assert "earlier" in env.model.texts_shown()
