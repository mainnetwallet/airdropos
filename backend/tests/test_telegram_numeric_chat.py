"""A resolved numeric chat id ("@8804778898") must reach Telethon as an int, not a username.

Observed failure: the planner sent "@8804778898" to telegram_send_message. It matched the
username pattern, Telethon received the digit string and raised ValueError, so every
retry failed with PROVIDER_ERROR. The continuation tool also lacked its _resolve_entity import.
"""

import asyncio

from backend.app.tools import telegram_continuation_tools
from backend.app.tools.telegram_user_tools import (
    TelegramSendMessageTool,
    _numeric_chat_id,
    _resolve_entity,
    _session,
)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _Client:
    def __init__(self):
        self.asked = []

    async def get_entity(self, ref):
        self.asked.append(ref)
        if isinstance(ref, str) and ref.isdigit():
            raise ValueError(f"Cannot find any entity corresponding to {ref!r}")
        return type("E", (), {"id": ref})()


def test_numeric_chat_id_forms():
    assert _numeric_chat_id("@8804778898") == 8804778898
    assert _numeric_chat_id("8804778898") == 8804778898
    assert _numeric_chat_id("-1001234567890") == -1001234567890
    assert _numeric_chat_id("@arkybet_bot") is None
    assert _numeric_chat_id("https://t.me/arkybet_bot") is None


def test_resolve_entity_passes_an_int_for_at_prefixed_id():
    client = _Client()
    entity = _run(_resolve_entity(client, "@8804778898"))
    assert client.asked == [8804778898]
    assert entity.id == 8804778898


def test_resolve_ref_accepts_at_prefixed_numeric_id():
    ref = {"chat_id": 8804778898, "username": None, "title": "bot"}
    _session.resolved_chats[8804778898] = ref
    try:
        assert _session.resolve_ref("@8804778898") == ref
    finally:
        _session.resolved_chats.pop(8804778898, None)


def test_continuation_module_has_resolve_entity():
    assert telegram_continuation_tools._resolve_entity is _resolve_entity


async def test_send_tool_accepts_numeric_target_without_invalid_input(client):
    """A numeric chat id must be treated as an id, not parsed as a username.

    This used to run against no client at all and assert only that the category was
    not INVALID_INPUT -- which passed for the wrong reason: the tool failed later,
    at connection time. With a fake client installed the numeric target now resolves
    and the send actually succeeds, which is the real assertion.
    """
    client.entities[8804778898] = client.group
    tool = TelegramSendMessageTool()
    result = await tool.execute({"chat": "@8804778898", "text": "https://x.com/user"})
    assert getattr(result, "error_category", None) != "INVALID_INPUT"
    assert result.status.value == "success"
    assert client.group.id in client.messages


async def test_send_tool_still_rejects_a_genuinely_malformed_target(client):
    """The complementary case: a target that is neither a username nor an id is refused."""
    tool = TelegramSendMessageTool()
    result = await tool.execute({"chat": "not a valid target!!", "text": "hello"})
    assert result.error_category == "INVALID_INPUT"
