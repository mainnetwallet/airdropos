"""Telegram continuation tools: come back to the bot, and wait for async replies.

These exist because of a specific, observed failure: the agent joined a Telegram
group and then stopped. Two things were missing from the toolset:

1. No way to *return* to the originating bot. The only Telegram tools were
   open/join/read/send/click, and "go back to the bot" was expected to happen
   implicitly. It did not, so the workflow ended at the join.
2. No way to *wait* for a delayed confirmation. Campaigns frequently verify a join
   asynchronously, so reading immediately returns nothing new.

Both live here rather than in ``telegram_user_tools`` so the existing session,
pacing, rate-limit and redaction machinery is reused unchanged: every tool runs
through ``_session.run(...)`` exactly like the original five.
"""

from __future__ import annotations

from typing import Any, Dict, List

from backend.app.tools.base import BaseTool, SideEffect, ToolResult
from backend.app.tools.telegram_user_tools import (
    _UNTRUSTED_NOTE,
    _describe_entity,
    _describe_message,
    _evidence,
    _recent_messages,
    _session,
    _wait_for_replies,
    parse_target,
)


class TelegramReturnToBotTool(BaseTool):
    """Re-open the bot the workflow started from and read its newest replies.

    This is the step the reported failure never reached. After joining a group the
    agent must come BACK to the bot; making that an explicit, addressable tool means
    it no longer depends on the model remembering which bot it opened.
    """

    name = "telegram_return_to_bot"
    version = "1.0.0"
    description = (
        "Return to the Telegram bot this task started from (or a named bot) and read its latest "
        "messages and buttons. Use it AFTER an external action such as joining a group, so the "
        "workflow continues from the bot instead of stopping."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 45
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "bot": {
                "type": "string",
                "maxLength": 300,
                "description": "@BotName. Omit to return to the bot this task already opened.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        limit = int(params.get("limit") or 10)
        requested = str(params.get("bot") or "").strip()
        target_name = requested or (_session.current_bot or "")
        if not target_name:
            return ToolResult.failure(
                "No bot is known for this task and none was named. Open it first with "
                "telegram_open_bot, or pass its @username.",
                category="INVALID_INPUT",
                retryable=False,
            )
        try:
            target = parse_target(target_name)
        except ValueError as exc:
            return ToolResult.failure(str(exc), category="INVALID_INPUT")
        if target["kind"] != "username":
            return ToolResult.failure(
                "A bot must be given as @username or a t.me link.", category="INVALID_INPUT"
            )

        async def action(client: Any) -> ToolResult:
            entity = await client.get_entity(target["value"])
            # Read history only. Sending /start again would restart the bot's flow and
            # could discard progress already made in this task.
            messages = await _recent_messages(client, entity, limit)
            ref = _describe_entity(entity)
            _session.remember_chat(ref)
            _session.current_bot = target["value"]
            buttons: List[Dict[str, Any]] = [
                b for m in messages for b in (m.get("buttons") or [])
            ]
            return ToolResult.success(
                data={
                    "bot": target["value"],
                    "returned_to_bot": True,
                    "messages": messages,
                    "count": len(messages),
                    "available_buttons": [b.get("text") for b in buttons if b.get("text")][:20],
                    "note": _UNTRUSTED_NOTE,
                },
                evidence=_evidence(
                    "message_observed", chat_id=ref.get("chat_id"), count=len(messages)
                ),
            )

        return await _session.run(
            action, interactive=False, label=f"Returned to bot @{target['value']}"
        )


class TelegramWaitForReplyTool(BaseTool):
    """Wait, with a bounded timeout, for a bot to answer.

    Campaigns often verify a join asynchronously. Without an explicit wait the agent
    reads too early, sees nothing new, and wrongly concludes the step failed.
    """

    name = "telegram_wait_for_reply"
    version = "1.0.0"
    description = (
        "Wait for a Telegram bot or chat to send a NEW message (for example a delayed confirmation "
        "after joining a group) and return it. Bounded: it gives up after the timeout rather than "
        "waiting forever."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 75
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {
            "chat": {"type": "string", "maxLength": 300},
            "after_message_id": {
                "type": "integer",
                "minimum": 0,
                "description": "Only consider messages newer than this id",
            },
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
        },
        "required": ["chat"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        chat = str(params["chat"]).strip()
        known = _session.resolve_ref(chat)
        if known is not None and known.get("chat_id"):
            chat = str(known["chat_id"])
        after_id = int(params.get("after_message_id") or 0)
        timeout = float(params.get("timeout_seconds") or 20)

        async def action(client: Any) -> ToolResult:
            try:
                entity = await _resolve_entity(client, chat)
            except ValueError as exc:
                return ToolResult.failure(str(exc), category="INVALID_INPUT")
            fresh = await _wait_for_replies(client, entity, after_id, timeout=timeout)
            described = [_describe_message(m) for m in fresh]
            ref = _describe_entity(entity)
            _session.remember_chat(ref)
            if not described:
                # Timing out is a reported fact, not a failure: the caller decides next.
                return ToolResult.success(
                    data={
                        "chat": chat,
                        "received": False,
                        "replies": [],
                        "timed_out": True,
                        "waited_seconds": timeout,
                        "note": "No new message arrived within the timeout.",
                    },
                    evidence=_evidence(
                        "message_observed", chat_id=ref.get("chat_id"), count=0
                    ),
                )
            return ToolResult.success(
                data={
                    "chat": chat,
                    "received": True,
                    "replies": described,
                    "reply_count": len(described),
                    "timed_out": False,
                    "note": _UNTRUSTED_NOTE,
                },
                evidence=_evidence(
                    "message_observed", chat_id=ref.get("chat_id"), count=len(described)
                ),
            )

        return await _session.run(
            action, interactive=False, label=f"Waited for a reply from {chat[:60]}"
        )


CONTINUATION_TOOLS = (TelegramReturnToBotTool, TelegramWaitForReplyTool)
