"""Chat must create a task only when asked or when the work truly needs one."""

from types import SimpleNamespace

import pytest

from backend.app.agent.chat_loop import (
    ChatToolLoop,
    ChatTurnResult,
    ToolCallRecord,
    operator_task_signal,
)


def turn(role, content):
    return SimpleNamespace(role=role, content=content)


@pytest.mark.parametrize(
    "message",
    [
        "create a task for this airdrop",
        "ekta task create koro",
        "task banao",
        "এই কাজের জন্য একটা টাস্ক বানাও",
        "queue this as a new task",
    ],
)
def test_explicit_request_is_yes(message):
    assert operator_task_signal(message, []) == "yes"


@pytest.mark.parametrize(
    "message",
    [
        "hi",
        "show my wallet address",
        "what is this airdrop? https://example.com/quest",
        "eta ki bujhaiya bolo",
        "amar tasks dekhao",
    ],
)
def test_ordinary_chat_is_none(message):
    assert operator_task_signal(message, []) == "none"


@pytest.mark.parametrize(
    "message",
    [
        "don't create a task, just explain",
        "task create korba na",
        "task lagbe na, shudhu bolo",
        "টাস্ক বানাও না",
    ],
)
def test_negation_wins(message):
    assert operator_task_signal(message, []) == "no"


def test_bare_yes_counts_only_after_a_task_offer():
    offer = [turn("user", "https://x.io/quest"), turn("assistant", "Want me to create a task for this?")]
    assert operator_task_signal("koro", offer) == "yes"
    assert operator_task_signal("yes", offer) == "yes"
    other = [turn("user", "hi"), turn("assistant", "Hello! How can I help?")]
    assert operator_task_signal("yes", other) == "none"
    assert operator_task_signal("yes", []) == "none"


def _result(*statuses):
    r = ChatTurnResult(reply="x")
    r.tool_calls = [ToolCallRecord(tool="t", status=s) for s in statuses]
    return r


def test_guard_enforces_operator_word_both_ways():
    assert ChatToolLoop._task_allowed("no", _result())[0] is False
    assert ChatToolLoop._task_allowed("yes", _result("success"))[0] is True


def test_guard_blocks_task_wrapping_a_finished_tool_answer():
    assert ChatToolLoop._task_allowed("none", _result("success"))[0] is False
    assert ChatToolLoop._task_allowed("none", _result("failure"))[0] is True
    assert ChatToolLoop._task_allowed("none", _result())[0] is True
