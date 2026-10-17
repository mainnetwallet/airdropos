"""Regression tests for `ChatToolLoop.run`: what the model is actually sent each round.

`FixedLLMClient._post` is replaced by a capturing fake, so assertions are about the serialised
request that would have gone on the wire.

Shape note (verified against the code, not assumed): this loop does NOT use `tool` role messages
or tool-call IDs. After a tool round it appends the model's own reply as an `assistant` message and
the rendered tool results as one `user` message. The tests pin that shape, because the ordering of
those messages is what keeps results attached to the round that produced them.
"""

import ast
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.agent.chat_loop import ChatToolLoop
from backend.app.llm.client import FixedLLMClient
from backend.app.tasks.telemetry import get_llm_traces, reset_llm_traces
from backend.app.tools.base import BaseTool, SideEffect, ToolResult
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry


@pytest.fixture(autouse=True)
def _clean_traces():
    reset_llm_traces()
    yield
    reset_llm_traces()


def body(content):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "model": "served-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def decision(reply="", tool_calls=None):
    return json.dumps({"reply": reply, "tool_calls": tool_calls or [], "create_task": None})


class Transport:
    def __init__(self, *replies):
        self.payloads = []
        self.replies = list(replies)

    async def __call__(self, path, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        index = min(len(self.payloads) - 1, len(self.replies) - 1)
        return body(self.replies[index])


@pytest.fixture
def make_transport(monkeypatch):
    def build(*replies):
        fake = Transport(*replies)
        monkeypatch.setattr(FixedLLMClient, "_post", lambda self, path, payload: fake(path, payload))
        return fake

    return build


class EchoTool(BaseTool):
    name = "echo_probe"
    description = "Returns its value unchanged."
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    side_effect = SideEffect.NONE

    async def execute(self, params):
        return ToolResult.success({"echoed": params["value"]})


def make_loop(**kwargs):
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.policy = AuthorizationPolicy()
    return ChatToolLoop(registry=registry, policy=registry.policy, **kwargs)


def turns(*pairs):
    return [SimpleNamespace(role=r, content=c) for r, c in pairs]


def roles(payload):
    return [m["role"] for m in payload["messages"]]


def text_of(message):
    content = message["content"]
    if isinstance(content, str):
        return content
    return json.dumps(content)


# ---------------------------------------------------------------------------------------------
# Tool rounds
# ---------------------------------------------------------------------------------------------


async def test_tool_round_results_follow_the_round_that_produced_them(make_transport):
    first = decision(
        "checking",
        [
            {"tool": "echo_probe", "arguments": {"value": "alpha"}},
            {"tool": "echo_probe", "arguments": {"value": "bravo"}},
        ],
    )
    transport = make_transport(first, decision("all done"))

    result = await make_loop().run(db=None, message="run the probe twice", history=[])

    assert result.reply == "all done"
    assert result.tool_invocation_count == 2
    assert len(transport.payloads) == 2

    # Round 1 is exactly system + the operator message.
    assert roles(transport.payloads[0]) == ["system", "user"]

    # Round 2 keeps round 1 intact and appends: the model's own reply, then the tool results.
    second = transport.payloads[1]
    assert roles(second) == ["system", "user", "assistant", "user"]
    assert second["messages"][:2] == transport.payloads[0]["messages"]
    assert text_of(second["messages"][2]) == first

    feedback = text_of(second["messages"][3])
    assert "ROUND 1" in feedback
    assert feedback.index("alpha") < feedback.index("bravo")  # executed order is preserved
    assert feedback.count("TOOL echo_probe -> SUCCESS") == 2


async def test_second_tool_round_accumulates_without_reordering(make_transport):
    r1 = decision("one", [{"tool": "echo_probe", "arguments": {"value": "first-call"}}])
    r2 = decision("two", [{"tool": "echo_probe", "arguments": {"value": "second-call"}}])
    transport = make_transport(r1, r2, decision("finished"))

    result = await make_loop().run(db=None, message="go", history=[])

    assert result.reply == "finished"
    assert len(transport.payloads) == 3
    third = transport.payloads[2]
    assert roles(third) == ["system", "user", "assistant", "user", "assistant", "user"]
    # Earlier rounds are a strict prefix of the later request.
    assert third["messages"][:4] == transport.payloads[1]["messages"]
    assert text_of(third["messages"][4]) == r2
    last_feedback = text_of(third["messages"][5])
    assert "ROUND 1" in last_feedback and "ROUND 2" in last_feedback
    assert last_feedback.index("first-call") < last_feedback.index("second-call")


async def test_secret_arguments_are_not_rendered_into_the_tool_feedback(make_transport):
    secret = "hunter2-DO-NOT-LEAK"
    r1 = decision("x", [{"tool": "echo_probe", "arguments": {"value": "v", "password": secret}}])
    transport = make_transport(r1, decision("done"))

    result = await make_loop().run(db=None, message="go", history=[])

    # The tool rejects the unexpected key, so the call fails.
    assert result.tool_calls and result.tool_calls[0].status != "success"
    assert secret not in json.dumps([c.arguments for c in result.tool_calls])

    second = transport.payloads[1]["messages"]
    # The loop's own contribution (tool feedback) never carries the value. The assistant
    # message is the model's reply echoed back verbatim, so it is the one place the model's
    # own text appears; that is by design and is not asserted against here.
    for message in second:
        if message["role"] != "assistant":
            assert secret not in text_of(message)
    assert second[2]["role"] == "assistant"


# ---------------------------------------------------------------------------------------------
# History and the latest user message
# ---------------------------------------------------------------------------------------------


async def test_history_is_sent_in_order_and_current_message_is_last(make_transport):
    history = turns(
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "repeat this"),
        ("assistant", "second answer"),
    )
    transport = make_transport(decision("ok"))

    await make_loop().run(db=None, message="repeat this", history=history)

    sent = transport.payloads[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert [text_of(m) for m in sent[1:5]] == [
        "first question", "first answer", "repeat this", "second answer",
    ]
    # Identical wording earlier in the history must not displace or duplicate the new message.
    assert text_of(sent[-1]) == "repeat this"
    assert sum(1 for m in sent if m["role"] == "user" and text_of(m) == "repeat this") == 2


async def test_stored_conversation_history_excludes_the_message_being_sent(factory, make_transport):
    """The endpoint reads history BEFORE recording the new message, so it is sent exactly once."""
    from backend.app.api.v1.endpoints.system import (
        ChatRequest,
        _open_chat_conversation,
        _record_chat_turn,
    )
    from backend.app.security.auth import Principal

    principal = Principal(user_id=None, email="operator@example.com", scopes=frozenset())
    conversation_id = str(uuid.uuid4())
    transport = make_transport(decision("ok"))

    async with factory() as db:
        first = ChatRequest(message="hello there", conversation_id=conversation_id)
        conversation, history = await _open_chat_conversation(db, principal, first)
        await _record_chat_turn(db, conversation, "user", first.message)
        await make_loop().run(
            db=db, message=first.message, history=history, conversation_id=str(conversation.id)
        )
        await _record_chat_turn(db, conversation, "assistant", "hi, how can I help?")

        second = ChatRequest(message="latest reply wins", conversation_id=conversation_id)
        conversation, history = await _open_chat_conversation(db, principal, second)
        await _record_chat_turn(db, conversation, "user", second.message)
        await make_loop().run(
            db=db, message=second.message, history=history, conversation_id=str(conversation.id)
        )

    sent = transport.payloads[1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert [text_of(m) for m in sent[1:]] == [
        "hello there", "hi, how can I help?", "latest reply wins",
    ]


# ---------------------------------------------------------------------------------------------
# Trace attribution
# ---------------------------------------------------------------------------------------------


async def test_conversation_id_and_purpose_land_in_the_trace(make_transport):
    r1 = decision("x", [{"tool": "echo_probe", "arguments": {"value": "v"}}])
    make_transport(r1, decision("done"))

    await make_loop().run(db=None, message="go", history=[], conversation_id="conv-abc")

    traces = get_llm_traces(conversation_id="conv-abc")
    assert [t["purpose"] for t in traces] == ["chat_decision", "chat_tool_round"]
    assert all(t["conversation_id"] == "conv-abc" for t in traces)
    assert [t["message_count"] for t in traces] == [2, 4]
    assert [t["outcome"] for t in traces] == ["ok", "ok"]
    # Nothing about this turn is filed under another conversation or as unattributed.
    assert get_llm_traces(conversation_id="conv-other") == []
    assert get_llm_traces() == []


async def test_a_turn_without_a_conversation_is_not_attributed_to_the_previous_one(make_transport):
    make_transport(decision("a"), decision("b"))
    loop = make_loop()

    await loop.run(db=None, message="first", history=[], conversation_id="conv-one")
    await loop.run(db=None, message="second", history=[])

    assert len(get_llm_traces(conversation_id="conv-one")) == 1
    assert len(get_llm_traces()) == 1  # the second turn is unattributed, not conv-one


# ---------------------------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------------------------


def test_every_chat_loop_run_call_site_in_the_endpoints_passes_conversation_id():
    source = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "endpoints" / "system.py"
    ).read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "loop"
    ]
    assert len(calls) == 2, "expected the blocking and the streaming chat endpoints"
    for call in calls:
        assert "conversation_id" in {kw.arg for kw in call.keywords}
