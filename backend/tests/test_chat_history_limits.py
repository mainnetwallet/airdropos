"""Chat history: no application-level limit, and the context pipeline decides.

`POST /agent/chat` used to trim remembered turns at `CHAT_HISTORY_MAX_CHARS`
(16,000 characters per request). That cap was applied at the API boundary, before
any token accounting could run, so a long conversation lost whole older messages
silently - and because they never reached the request, the token-aware compaction
pipeline could never compact them instead of deleting them.

The character cap is gone, and so is everything that replaced it:
`CHAT_HISTORY_MAX_TURNS` (24), the `max_length=200` on the history list, the
`max_length=8000` on each historical turn and the `max_length=8000` on the
operator's own message. There is deliberately NO application-level limit on chat
history - not a character total, not a turn count and no replacement cap of either
kind. These tests pin the removal:

* the constants and their enforcement are gone from the endpoint module;
* a history whose combined length exceeds the former cap keeps every message;
* more than 200 turns are accepted by the request model and reach the model;
* a single historical turn longer than 8000 characters is accepted and kept whole;
* an operator message longer than 8000 characters is accepted;
* ordering and roles are preserved and empty turns are still dropped;
* an oversized history actually reaches the model through the context pipeline, and
  a request that is over the token budget is handed to the existing fixed-model
  compaction pipeline instead of being thinned at the boundary.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.api.v1.endpoints import system as system_module
from backend.app.api.v1.endpoints.system import (
    ChatRequest,
    ChatTurn,
    _trim_history,
)
from backend.app.core.config import settings
from backend.app.database.session import get_db
from backend.app.main import app
from backend.app.models import Base, User, UserSession
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token

# The limits that were removed. Kept as literals here so the tests describe the
# behaviour change rather than importing the things they prove are gone.
FORMER_CHAR_LIMIT = 16_000
FORMER_TURN_LIMIT = 24
FORMER_HISTORY_LIST_LIMIT = 200
FORMER_MESSAGE_LIMIT = 8_000


def _turns(spec):
    return [ChatTurn(role=role, content=content) for role, content in spec]


# ======================================================================
# The caps are gone
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


def test_the_turn_count_cap_is_gone_from_the_chat_module():
    """The count-based truncation is gone, not merely raised.

    A 24-turn ceiling silently deleted the oldest part of a conversation before
    compaction could see it, which is the same fault the character cap had. There
    is no replacement bound, so there is no constant to find.
    """
    assert not hasattr(system_module, "CHAT_HISTORY_MAX_TURNS")

    source = inspect.getsource(system_module)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "MAX_TURNS" not in line, f"a turn-count cap remains: {stripped}"

    # No "keep only the last N" slice survives in the trim: that is the shape a
    # count-based history cut takes.
    trim = inspect.getsource(system_module._trim_history)
    assert re.search(r"\[-\s*[A-Za-z0-9_]+\s*:\s*\]", trim) is None, (
        "the trim still cuts the history with a slice"
    )


def test_the_request_model_imposes_no_length_limits():
    """No bound on the history list, its turns, or the operator's own message.

    These were the pydantic-level enforcement of the caps: 200 turns, 8000
    characters per historical turn, 8000 characters per message. A user who typed
    a longer message got an opaque 422 rather than an answer.
    """
    request_schema = ChatRequest.model_json_schema()
    message_field = request_schema["properties"]["message"]
    history_field = request_schema["properties"]["history"]
    assert "maxLength" not in message_field, message_field
    assert "maxItems" not in history_field, history_field
    # The empty-message guard is kept: it is a validity check, not a size limit.
    assert message_field.get("minLength") == 1

    turn_schema = ChatTurn.model_json_schema()
    content_field = turn_schema["properties"]["content"]
    assert "maxLength" not in content_field, content_field


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


def test_no_turn_count_bound_remains_on_the_history_itself():
    """A long transcript is kept whole and in order - the oldest turn survives.

    The old `CHAT_HISTORY_MAX_TURNS` cut kept only the most recent 24 turns, so the
    first message of the conversation never reached the model at all.
    """
    total = FORMER_TURN_LIMIT + 40
    history = _turns([("user", f"turn {i} " + "q" * 100) for i in range(total)])
    kept = _trim_history(history)

    assert len(kept) == total, "the history is still being cut to a turn count"
    assert kept[0].content.startswith("turn 0 ")
    assert kept[-1].content.startswith(f"turn {total - 1} ")
    assert [t.content.split()[1] for t in kept] == [str(i) for i in range(total)]


def test_more_than_200_turns_are_accepted_by_the_request_model():
    """A client that sends its whole conversation must not get a 422.

    The request model's bound was a payload-size guard that turned a long
    conversation into an opaque validation failure the moment it grew past 200.
    """
    history = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"m{i} " + "x" * 500,
        }
        for i in range(FORMER_HISTORY_LIST_LIMIT + 80)
    ]
    request = ChatRequest(message="and now?", history=history)
    assert len(request.history) == len(history)


def test_historical_turns_longer_than_8000_characters_are_accepted():
    """One long turn is content, not a rule violation."""
    long_content = "L" * (FORMER_MESSAGE_LIMIT + 2_000)
    request = ChatRequest(
        message="what did I just paste?",
        history=[{"role": "user", "content": long_content}],
    )
    assert len(request.history) == 1
    assert request.history[0].content == long_content


def test_an_operator_message_longer_than_8000_characters_is_accepted():
    """The current message must never be refused for being long."""
    message = "please analyse this\n" + "y" * (FORMER_MESSAGE_LIMIT + 5_000)
    request = ChatRequest(message=message)
    assert request.message == message


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


def _client(token):
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


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


def _stub_loop_llm(monkeypatch) -> _StubLLM:
    from backend.app.tools import bootstrap as bootstrap_module

    bootstrap_module.bootstrap_tools()
    stub = _StubLLM()
    monkeypatch.setattr("backend.app.agent.chat_loop.llm", stub)
    return stub


@pytest.mark.asyncio
async def test_an_oversized_history_is_sent_to_the_model_not_thinned_first(monkeypatch, operator):
    stub = _stub_loop_llm(monkeypatch)

    history = [
        {"role": "user", "content": "first " + "a" * 7_000},
        {"role": "assistant", "content": "second " + "b" * 7_000},
        {"role": "user", "content": "third " + "c" * 7_000},
    ]
    assert sum(len(t["content"]) for t in history) > FORMER_CHAR_LIMIT

    async with _client(operator) as client:
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


@pytest.mark.asyncio
async def test_more_than_200_turns_are_not_discarded_at_the_api_boundary(monkeypatch, operator):
    """Every turn survives the endpoint, oldest first, and reaches the model.

    The old ceiling kept only the most recent 24 turns, so the start of a long
    conversation was gone before any token-aware decision was made.
    """
    stub = _stub_loop_llm(monkeypatch)

    history = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i:04d} " + "h" * 200,
        }
        for i in range(FORMER_HISTORY_LIST_LIMIT + 100)
    ]

    async with _client(operator) as client:
        response = await client.post(
            "/api/v1/agent/chat", json={"message": "continue", "history": history}
        )

    assert response.status_code == 200, response.text
    assert stub.calls, "the turn never reached the model"
    contents = [m.content for m in stub.calls[0]]
    for turn in history:
        assert turn["content"] in contents, f"{turn['content'].split()[1]} was discarded"
    positions = [contents.index(turn["content"]) for turn in history]
    assert positions == sorted(positions), "ordering was not preserved"
    assert [m.role for m in stub.calls[0][1:-1]] == [t["role"] for t in history]
    assert contents[-1] == "continue"


@pytest.mark.asyncio
async def test_historical_turns_longer_than_8000_characters_survive_the_boundary(
    monkeypatch, operator
):
    """A single turn longer than the old per-message cap is kept whole.

    It is not rejected with a 422, not truncated, and not replaced by a placeholder.
    """
    stub = _stub_loop_llm(monkeypatch)
    long_body = "L" * (FORMER_MESSAGE_LIMIT + 12_000)

    history = [
        {"role": "user", "content": "first " + long_body},
        {"role": "assistant", "content": "second " + long_body},
        {"role": "user", "content": "third " + long_body},
    ]
    assert all(len(t["content"]) > FORMER_MESSAGE_LIMIT for t in history)

    async with _client(operator) as client:
        response = await client.post(
            "/api/v1/agent/chat", json={"message": "continue", "history": history}
        )

    assert response.status_code == 200, response.text
    contents = [m.content for m in stub.calls[0]]
    for turn in history:
        assert turn["content"] in contents, "a long historical turn was dropped or shortened"


@pytest.mark.asyncio
async def test_an_operator_message_longer_than_8000_characters_is_accepted(
    monkeypatch, operator
):
    stub = _stub_loop_llm(monkeypatch)
    message = "Summarise everything below.\n" + "z" * (FORMER_MESSAGE_LIMIT + 3_000)

    async with _client(operator) as client:
        response = await client.post(
            "/api/v1/agent/chat", json={"message": message, "history": []}
        )

    assert response.status_code == 200, response.text
    sent = stub.calls[0]
    assert sent[-1].content == message
    assert sent[-1].role == "user"


@pytest.mark.asyncio
async def test_an_over_budget_history_is_handed_to_the_compaction_pipeline(
    monkeypatch, factory, operator
):
    """An oversized history is compacted, never dropped, and never 422'd.

    This is the path that makes removing the boundary limits safe: the real
    `FixedLLMClient` runs, the accepted history exceeds the token budget, and the
    existing fixed-model compaction pipeline is what reduces it. The model call is
    the only thing stubbed, exactly as everywhere else in the suite.
    """
    from backend.app.agent.prompts import build_chat_system_prompt
    from backend.app.llm.client import FixedLLMClient, Message
    from backend.app.llm.token_budget import TokenBudget, count_request_tokens
    from backend.app.tools import bootstrap as bootstrap_module
    from backend.app.tools.registry import registry

    bootstrap_module.bootstrap_tools()

    # Configuration, not a chat-history limit: a smaller window puts a transcript the
    # endpoint now accepts over the compaction trigger without a huge fixture.
    monkeypatch.setattr(settings, "LLM_CONTEXT_WINDOW_TOKENS", 60_000)
    monkeypatch.setattr(settings, "LLM_CONTEXT_RESERVED_OUTPUT_TOKENS", 2_000)
    monkeypatch.setattr(settings, "LLM_CONTEXT_SAFETY_MARGIN_TOKENS", 1_000)
    budget = TokenBudget.from_settings()

    # Size the transcript from the real system prompt so the fixture does not depend
    # on a hard-coded prompt length: the whole request must land over the trigger.
    message = "continue from where we left off"
    prompt = build_chat_system_prompt(registry.catalog_for_llm(registry.policy))
    head_tokens = count_request_tokens([Message("system", prompt), Message("user", message)])
    turn_chars = 3_000
    turn_tokens = count_request_tokens([Message("user", "x" * turn_chars)])
    turns = max(4, math.ceil((budget.trigger + 2_000 - head_tokens) / turn_tokens))
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"t{i:04d} " + "x" * turn_chars}
        for i in range(turns)
    ]

    posted: list = []
    summarised: list = []

    async def fake_post(self, path, payload):
        posted.append(payload)
        return {
            "choices": [
                {"message": {"content": json.dumps({"reply": "ok", "tool_calls": []})}}
            ],
            "model": "fixed-model",
            "usage": {"total_tokens": 1},
        }

    async def fake_summarize(self, messages):
        # A usable summary: the two core keys the workflow depends on.
        summarised.append([m.content for m in messages])
        return json.dumps(
            {"objective": message, "execution_state": "earlier turns summarised by compaction"}
        )

    monkeypatch.setattr(FixedLLMClient, "_post", fake_post)
    monkeypatch.setattr(FixedLLMClient, "_compaction_summarize", fake_summarize)

    async with _client(operator) as client:
        response = await client.post(
            "/api/v1/agent/chat", json={"message": message, "history": history}
        )

    assert response.status_code == 200, response.text
    assert response.json()["reply"] == "ok", response.text

    # 1. The oversized history was NOT dropped or refused at the boundary: the whole
    #    transcript was handed to the compaction pipeline as it was sent, including
    #    its OLDEST turn - which is exactly what a turn-count cut deleted before the
    #    pipeline ever ran.
    assert summarised, "the oversized history never reached the compaction pipeline"
    summarised_text = "\n".join(content for call in summarised for content in call)
    assert history[0]["content"] in summarised_text, "the oldest turn never reached compaction"
    assert history[-1]["content"] in summarised_text, "the newest turn never reached compaction"

    # 2. Compaction reduced it to the model's own summary, so the request the model
    #    finally saw was smaller - and it was summarised, not truncated by us.
    assert posted, "nothing was submitted"
    final_contents = [m["content"] for m in posted[-1]["messages"]]
    assert any("COMPACTED CONTEXT SUMMARY" in c for c in final_contents)
    assert all(turn["content"] not in final_contents for turn in history)
    assert final_contents[-1] == message, "the active objective must survive compaction"
    assert len(" ".join(final_contents)) < sum(len(t["content"]) for t in history)
