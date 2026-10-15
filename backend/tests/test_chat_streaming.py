"""Live streaming of the agent chat turn.

Covers the three layers that make streaming work, and the guarantees that must survive it:

* `JsonStringFieldStreamer` recovers the `reply` text from a JSON body that arrives in
  arbitrary pieces, and never confuses a nested `reply` key for the real one;
* `ChatToolLoop` with a listener streams the model, announces each tool, and still returns
  exactly the result the blocking path returns; without a listener it never streams;
* a streaming failure before anything was shown falls back to one blocking call, a client
  that went away stops the turn before the next tool, and no task is created for it;
* `POST /agent/chat/stream` delivers the events as Server-Sent Events and ends in `done`.

No real model, network or chain is contacted.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.agent import chat_loop
from backend.app.agent.chat_loop import ChatToolLoop
from backend.app.database.session import get_db
from backend.app.llm.client import LLMError, LLMResponse
from backend.app.llm.json_stream import JsonStringFieldStreamer
from backend.app.main import app
from backend.app.models import Base, User, UserSession
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token


# ======================================================================
# JsonStringFieldStreamer
# ======================================================================
def _stream_text(text: str, field: str = "reply", seed: int = 0) -> str:
    rng = random.Random(seed)
    streamer = JsonStringFieldStreamer(field)
    out: List[str] = []
    i = 0
    while i < len(text):
        step = rng.randint(1, 7)
        out.append(streamer.feed(text[i : i + step]))
        i += step
    return "".join(out)


@pytest.mark.parametrize("ascii_only", [True, False])
@pytest.mark.parametrize("seed", range(20))
def test_reply_is_recovered_from_any_chunking(ascii_only: bool, seed: int) -> None:
    body = {
        "reply": 'He said "hi"\nback\\slash / é 😀 中',
        "tool_calls": [],
        "create_task": None,
    }
    text = "```json\n" + json.dumps(body, ensure_ascii=ascii_only) + "\n```"
    assert _stream_text(text, seed=seed) == body["reply"]


def test_nested_reply_keys_are_not_the_reply() -> None:
    body = {
        "tool_calls": [{"tool": "x", "arguments": {"reply": "NOT THIS", "n": [1, {"reply": "nor"}]}}],
        "reply": "after the tools",
    }
    assert _stream_text(json.dumps(body)) == "after the tools"


def test_reply_that_comes_after_other_scalar_values() -> None:
    body = {"n": 1, "ok": True, "z": None, "reply": "x" * 40, "o": {"reply": "inner"}}
    assert _stream_text(json.dumps(body, indent=2)) == "x" * 40


def test_prose_without_json_yields_nothing() -> None:
    streamer = JsonStringFieldStreamer("reply")
    assert streamer.feed("I will just talk instead of answering in JSON.") == ""
    assert not streamer.done


def test_partial_reply_is_available_before_the_object_closes() -> None:
    streamer = JsonStringFieldStreamer("reply")
    assert streamer.feed('{"reply": "partial te') == "partial te"
    assert not streamer.done
    assert streamer.feed('xt", "tool_calls": []}') == "xt"
    assert streamer.done


# ======================================================================
# ChatToolLoop with a fake model and a fake registry
# ======================================================================
def _json_body(reply: str, tool_calls: Optional[List[Dict[str, Any]]] = None, **extra: Any) -> str:
    return json.dumps({"reply": reply, "tool_calls": tool_calls or [], **extra})


def _response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model="fake-model",
        usage={"total_tokens": 7},
        finish_reason="stop",
        raw={},
        requested_model="fake-model",
    )


class FakeModel:
    """Stands in for `llm`. Bodies are consumed in order by whichever method is called."""

    def __init__(self, bodies: List[str], *, stream_error: Optional[BaseException] = None) -> None:
        self.bodies = list(bodies)
        self.stream_error = stream_error
        self.stream_calls = 0
        self.complete_calls = 0

    @staticmethod
    def parse_json(content: str) -> Dict[str, Any]:
        return json.loads(content)

    async def complete(self, messages, **kwargs) -> LLMResponse:
        self.complete_calls += 1
        return _response(self.bodies.pop(0))

    async def stream_complete(self, messages, *, on_token=None, **kwargs) -> LLMResponse:
        self.stream_calls += 1
        if self.stream_error is not None:
            raise self.stream_error
        body = self.bodies.pop(0)
        for i in range(0, len(body), 5):  # arrives in small pieces
            if on_token:
                on_token(body[i : i + 5])
        return _response(body)


class FakeRegistry:
    def __init__(self) -> None:
        self.policy = SimpleNamespace(permits=lambda tool: (True, ""))
        self._disabled: Dict[str, str] = {}
        self.invoked: List[str] = []

    def catalog_for_llm(self, policy) -> List[Dict[str, Any]]:
        return [{"name": "wallet_address", "description": "show the address"}]

    def get(self, name: str):
        return SimpleNamespace(name=name) if name == "wallet_address" else None

    async def invoke(self, name: str, arguments: Dict[str, Any], policy=None):
        self.invoked.append(name)
        return SimpleNamespace(
            status=SimpleNamespace(value="success"),
            data={"address": "0xabc"},
            error=None,
            error_category=None,
            retryable=False,
            duration_ms=3,
            evidence=None,
        )


def _loop() -> tuple[ChatToolLoop, FakeRegistry]:
    registry = FakeRegistry()
    return ChatToolLoop(registry=registry, policy=registry.policy), registry


async def _run(loop: ChatToolLoop, **kwargs):
    return await loop.run(db=None, message="hi", history=[], **kwargs)


async def test_reply_streams_and_matches_the_blocking_result(monkeypatch) -> None:
    body = _json_body("Hello there, operator.")
    streaming = FakeModel([body])
    monkeypatch.setattr(chat_loop, "llm", streaming)
    events: List[Dict[str, Any]] = []
    loop, _ = _loop()
    streamed = await _run(loop, on_event=events.append)

    blocking = FakeModel([body])
    monkeypatch.setattr(chat_loop, "llm", blocking)
    loop2, _ = _loop()
    plain = await _run(loop2)

    deltas = [e["text"] for e in events if e["type"] == "reply_delta"]
    assert len(deltas) > 1, "the reply should arrive in several pieces, not one block"
    assert "".join(deltas) == "Hello there, operator."
    assert streamed.to_dict() == plain.to_dict()
    assert blocking.stream_calls == 0, "without a listener the model must not be streamed"
    assert streaming.complete_calls == 0


async def test_tool_round_is_announced_in_order(monkeypatch) -> None:
    model = FakeModel(
        [
            _json_body("Checking.", [{"tool": "wallet_address", "arguments": {}}]),
            _json_body("Your address is 0xabc."),
        ]
    )
    monkeypatch.setattr(chat_loop, "llm", model)
    events: List[Dict[str, Any]] = []
    loop, registry = _loop()
    result = await _run(loop, on_event=events.append)

    kinds = [e["type"] for e in events if e["type"] != "reply_delta"]
    assert kinds == ["round_start", "tool_start", "tool_result", "round_start"]
    assert [e["round"] for e in events if e["type"] == "round_start"] == [1, 2]
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["tool"] == "wallet_address" and tool_result["status"] == "success"
    assert registry.invoked == ["wallet_address"]
    assert result.reply == "Your address is 0xabc."
    assert len(result.tool_calls) == 1


async def test_streaming_failure_before_any_text_falls_back_once(monkeypatch) -> None:
    model = FakeModel(
        [_json_body("Recovered without streaming.")],
        stream_error=LLMError("no stream support", category="HTTP_ERROR", retryable=False),
    )
    monkeypatch.setattr(chat_loop, "llm", model)
    events: List[Dict[str, Any]] = []
    loop, _ = _loop()
    result = await _run(loop, on_event=events.append)

    assert model.stream_calls == 1 and model.complete_calls == 1
    assert result.reply == "Recovered without streaming."
    assert not result.blockers


async def test_unrecoverable_stream_error_is_reported_not_retried(monkeypatch) -> None:
    model = FakeModel(
        [_json_body("never used")],
        stream_error=LLMError("too big", category="CONTEXT_OVERFLOW", retryable=False),
    )
    monkeypatch.setattr(chat_loop, "llm", model)
    loop, _ = _loop()
    result = await _run(loop, on_event=lambda e: None)

    assert model.complete_calls == 0
    assert any("CONTEXT_OVERFLOW" in b for b in result.blockers)


async def test_a_broken_listener_does_not_break_the_turn(monkeypatch) -> None:
    monkeypatch.setattr(chat_loop, "llm", FakeModel([_json_body("Still answered.")]))

    def explode(event: Dict[str, Any]) -> None:
        raise RuntimeError("listener bug")

    loop, _ = _loop()
    result = await _run(loop, on_event=explode)
    assert result.reply == "Still answered."


async def test_disconnect_stops_before_the_next_tool_and_creates_no_task(monkeypatch) -> None:
    model = FakeModel(
        [
            _json_body(
                "Working.",
                [{"tool": "wallet_address", "arguments": {}}],
                create_task={"title": "Should never exist"},
            )
        ]
    )
    monkeypatch.setattr(chat_loop, "llm", model)
    created: List[Dict[str, Any]] = []

    async def create_task(spec):
        created.append(spec)
        return {"task_id": "T"}, None

    loop, registry = _loop()
    gone = {"value": False}

    def on_event(event: Dict[str, Any]) -> None:
        if event["type"] == "round_start":
            return
        gone["value"] = True  # the browser leaves as soon as text/tool events begin

    result = await _run(
        loop, on_event=on_event, should_stop=lambda: gone["value"], create_task=create_task
    )

    assert registry.invoked == [], "no tool may start after the client has gone"
    assert result.truncated is True
    assert created == [], "a turn the operator never saw must not create a task"


# ======================================================================
# POST /agent/chat/stream
# ======================================================================
@pytest_asyncio.fixture
async def operator_token():
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
            email="stream@test.local",
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
    yield token
    app.dependency_overrides.clear()
    await engine.dispose()


def _parse_sse(raw: str) -> List[Dict[str, Any]]:
    frames = []
    for block in raw.split("\n\n"):
        data = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
        if data:
            frames.append(json.loads("\n".join(data)))
    return frames


async def test_endpoint_streams_events_and_ends_with_done(monkeypatch, operator_token) -> None:
    monkeypatch.setattr(chat_loop, "llm", FakeModel([_json_body("Streamed over HTTP.")]))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {operator_token}"},
    ) as client:
        async with client.stream(
            "POST", "/api/v1/agent/chat/stream", json={"message": "hello", "history": []}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            raw = "".join([chunk async for chunk in response.aiter_text()])

    frames = _parse_sse(raw)
    types = [f["type"] for f in frames]
    assert types[0] == "round_start"
    assert types[-1] == "done" and types.count("done") == 1
    assert "".join(f["text"] for f in frames if f["type"] == "reply_delta") == "Streamed over HTTP."
    done = frames[-1]
    assert done["reply"] == "Streamed over HTTP."
    assert done["model_policy"] == "single_fixed_model"


async def test_endpoint_requires_authentication(operator_token) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/agent/chat/stream", json={"message": "hello", "history": []}
        )
    assert response.status_code in (401, 403)
