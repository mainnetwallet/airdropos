"""Chat history: no character cap, and the context pipeline decides what fits.

`POST /agent/chat` used to trim remembered turns at `CHAT_HISTORY_MAX_CHARS`
(16,000 characters per request). That cap was applied at the API boundary, before
any token accounting could run, so a long conversation lost whole older messages
silently - and because they never reached the request, the token-aware compaction
pipeline could never compact them instead of deleting them.

These tests pin the removal:

* the constant and its enforcement are gone from the endpoint module;
* a history whose combined length exceeds the former cap keeps every message;
* ordering and roles are preserved and empty turns are still dropped;
* the remaining bound is a turn COUNT, not a character count;
* an oversized history actually reaches the model through the context pipeline.
"""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.api.v1.endpoints import system as system_module
from backend.app.api.v1.endpoints.system import (
    CHAT_HISTORY_MAX_TURNS,
    ChatRequest,
    ChatTurn,
    _trim_history,
)
from backend.app.database.session import get_db
from backend.app.main import app
from backend.app.models import Base, User, UserSession
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token

# The limit that was removed. Kept as a literal here so the tests describe the
# behaviour change rather than importing the thing they prove is gone.
FORMER_CHAR_LIMIT = 16_000


def _turns(spec):
    return [ChatTurn(role=role, content=content) for role, content in spec]


# ======================================================================
# The cap is gone
# ======================================================================
def test_the_character_cap_constant_is_gone_from_the_chat_module():
    # No constant, and no assignment of one under any name.
    assert not hasattr(system_module, "CHAT_HISTORY_MAX_CHARS")

    source = inspect.getsource(system_module)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "MAX_CHARS" in line:
            raise AssertionError(f"a character cap remains: {stripped}")

    # And the trim no longer accumulates a character total at all.
    trim = inspect.getsource(system_module._trim_history)
    assert "total" not in trim
    assert "MAX_CHARS" not in trim


def test_history_beyond_the_former_limit_keeps_every_message():
    history = _turns(
        [
            ("user", "a" * 7_000),
            ("assistant", "b" * 7_000),
            ("user", "c" * 7_000),
        ]
    )
    kept = _trim_history(history)

    assert len(kept) == 3, "a character cap dropped valid messages"
    assert sum(len(t.content) for t in kept) > FORMER_CHAR_LIMIT


def test_ordering_and_roles_are_preserved():
    history = _turns(
        [
            ("user", "first " + "x" * 6_000),
            ("assistant", "second " + "y" * 6_000),
            ("user", "third " + "z" * 6_000),
        ]
    )
    kept = _trim_history(history)

    assert [t.role for t in kept] == ["user", "assistant", "user"]
    assert [t.content.split()[0] for t in kept] == ["first", "second", "third"]


def test_tool_call_and_tool_result_relationships_are_not_broken():
    """The trim only ever keeps *whole* turns, in order, so nothing is orphaned.

    A tool result can only be meaningful next to the call it answers, which is why
    trimming works on whole turns from the oldest end rather than on any per-message
    budget.
    """
    history = _turns(
        [
            ("user", "ask"),
            ("assistant", "TOOL CALL: read_balance " + "a" * 400),
            ("user", "TOOL RESULT: 0.5 ETH " + "b" * 400),
            ("assistant", "answer"),
        ]
    )
    kept = _trim_history(history)
    assert [t.content.split(":")[0] for t in kept] == [
        "ask",
        "TOOL CALL",
        "TOOL RESULT",
        "answer",
    ]


def test_empty_and_whitespace_turns_are_still_dropped():
    history = _turns(
        [
            ("user", "real question"),
            ("assistant", "   "),
            ("user", ""),
            ("assistant", "real answer"),
        ]
    )
    kept = _trim_history(history)
    assert [t.content for t in kept] == ["real question", "real answer"]


def test_the_remaining_bound_is_a_turn_count_not_a_length():
    history = _turns([("user", f"turn {i} " + "q" * 100) for i in range(CHAT_HISTORY_MAX_TURNS + 20)])
    kept = _trim_history(history)

    assert len(kept) == CHAT_HISTORY_MAX_TURNS
    # The most recent turns are the ones kept, still in order.
    assert kept[-1].content.startswith(f"turn {CHAT_HISTORY_MAX_TURNS + 19} ")


def test_a_long_transcript_is_accepted_rather_than_rejected():
    """A client that sends its whole conversation must not get a 422.

    The request model's bound is a payload-size guard; the session keeps the most
    recent turns itself. A tight bound here turned a long conversation into an
    opaque validation failure the moment it grew past the limit.
    """
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * 500}
        for i in range(150)
    ]
    request = ChatRequest(message="and now?", history=history)
    assert len(request.history) == 150


# ======================================================================
# The oversized history reaches the model through the context pipeline
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


@pytest_asyncio.fixture
async def operator(factory):
    email = f"op-{uuid.uuid4().hex[:8]}@test.local"
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
                scopes=sorted(s.value for s in Scope),
            )
        )
        await db.commit()
    return token


class _StubResponse:
    def __init__(self, payload):
        self.content = json.dumps(payload)
        self.model = "fixed-model"
        self.usage = {"total_tokens": 1}


class _StubLLM:
    """Stands in for the single fixed model so no call leaves the process."""

    def __init__(self):
        self.calls = []

    async def complete(self, messages, json_mode=False, **kwargs):
        self.calls.append(messages)
        return _StubResponse({"reply": "ok", "tool_calls": []})

    @staticmethod
    def parse_json(content):
        return json.loads(content)


@pytest.mark.asyncio
async def test_an_oversized_history_is_sent_to_the_model_not_thinned_first(monkeypatch, operator):
    from backend.app.tools import bootstrap as bootstrap_module

    bootstrap_module.bootstrap_tools()
    stub = _StubLLM()
    monkeypatch.setattr("backend.app.agent.chat_loop.llm", stub)

    history = [
        {"role": "user", "content": "first " + "a" * 7_000},
        {"role": "assistant", "content": "second " + "b" * 7_000},
        {"role": "user", "content": "third " + "c" * 7_000},
    ]
    assert sum(len(t["content"]) for t in history) > FORMER_CHAR_LIMIT

    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {operator}"},
    )
    async with client:
        response = await client.post(
            "/api/v1/agent/chat", json={"message": "continue", "history": history}
        )

    assert response.status_code == 200, response.text
    assert stub.calls, "the turn never reached the model"
    sent = stub.calls[0]
    contents = [m.content for m in sent]
    for turn in history:
        assert turn["content"] in contents, "an older turn was discarded before the model saw it"
    # Ordering survives: the three turns appear in the order they were sent.
    positions = [contents.index(turn["content"]) for turn in history]
    assert positions == sorted(positions)
    assert sent[-1].content == "continue"
    assert sent[-1].role == "user"
