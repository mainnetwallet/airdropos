"""The Discord capability exposed to the agent.

One tool per operation. Each takes a ``browser_profile_id`` and hands everything else
to :func:`backend.app.integrations.discord.service.execute`, which resolves the
profile, enforces the read/write boundary, suppresses duplicates, executes through
the existing browser controller and records the result. Routing therefore cannot
drift between the chat console, the task worker and the Discord page: they all call
the same function.

Deliberately NOT provided
-------------------------
* Any shell, SQL, filesystem or raw-HTTP tool for Discord.
* Any way to read cookies, tokens or stored credentials. A Discord operation never
  touches browser session storage, and nothing it returns contains any.
* A "send to many channels" tool. Unsolicited bulk messaging and fake engagement are
  out of scope, and a tool that could do it in one call is a tool that will.
* A login tool. Credentials are never typed by AirdropOS; the operator signs in by
  hand and confirms through the Inbox.

Every tool here goes through the registry, so its permission checks, the emergency
stop, the audit trail and the operator's confirmation policy all still apply.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.app.database.session import session_scope
from backend.app.integrations.discord import routing, service
from backend.app.integrations.discord.errors import CAT_LOGIN_REQUIRED, DiscordLoginRequired
from backend.app.integrations.discord.models import MAX_MESSAGE_CHARS, UNCERTAIN
from backend.app.tools.base import BaseTool, SideEffect, ToolResult, ToolResultStatus
from backend.app.tools.context import get_current_task_id
from backend.app.tools.registry import registry

logger = logging.getLogger(__name__)

# Discord reads all navigate - there is no in-page API to read a channel through - so
# every tool, reads included, declares `browser.navigate`. Without that, revoking
# navigation would leave the read tools able to navigate anyway.
_DISCORD_READ_PERMS = ["discord.read", "browser.read", "browser.navigate"]
_DISCORD_WRITE_PERMS = ["discord.write", "browser.interact", "browser.navigate"]

MAX_SEARCH_QUERY = 200
MAX_TEXT_CHARS = MAX_MESSAGE_CHARS


class _DiscordToolBase(BaseTool):
    """Shared plumbing: profile argument, task attribution, honest result mapping."""

    async def _run(
        self,
        operation: str,
        params: Dict[str, Any],
        *,
        intent: Optional[str] = None,
    ) -> ToolResult:
        # A profile id that is missing or wrong is a validation failure the model can
        # see and act on, not a 500 from deep in the browser layer.
        profile_id = str(params.get("browser_profile_id") or "").strip()
        if not profile_id:
            return ToolResult.failure(
                "You must name the browser_profile_id that holds the Discord account this task "
                "should act as. Do not guess it and do not use a different account: the profile "
                "is the account, and picking the wrong one acts as the wrong person.",
                category="DISCORD_PROFILE_REQUIRED",
                retryable=False,
            )

        async with session_scope() as db:
            task_id = get_current_task_id()
            # AUTHORIZATION IS NEVER TAKEN FROM A TOOL ARGUMENT.
            #
            # The read/write boundary is decided from the task the operator actually
            # created: the intent they chose on the Discord page (stored in the
            # checkpoint), else the task's own title and description. It used to be
            # possible to pass a `task_text` here, and the send tool passed the
            # MESSAGE BODY - so a message body containing the word "send" decided
            # whether a read-only task was allowed to send. Anything read in a
            # channel can contain that word. Content is a payload, never a grant.
            declared = intent or await _operator_intent(db, task_id)
            try:
                outcome = await service.execute(
                    db,
                    operation,
                    browser_profile_id=profile_id,
                    task_text="",
                    intent=declared,
                    task_id=task_id,
                    params=params,
                    allow_duplicate=bool(params.get("allow_duplicate", False)),
                )
            except DiscordLoginRequired as exc:
                return _needs_user(exc)
            except Exception as exc:
                logger.exception("Discord tool %s failed", operation)
                return ToolResult.failure(
                    f"{type(exc).__name__}: the Discord operation could not be completed.",
                    category="DISCORD_UNEXPECTED",
                )
        return _to_tool_result(outcome)


def _needs_user(exc: DiscordLoginRequired) -> ToolResult:
    """Park the task with the CATEGORY the Inbox needs to route it correctly.

    Constructed directly rather than through ``ToolResult.needs_user``, because that
    helper hardcodes ``HUMAN_INTERVENTION_REQUIRED``. The difference is not cosmetic:
    ``inbox.service.classify_reason`` maps ``DISCORD_LOGIN_REQUIRED`` to a Discord
    login request with the "sign in yourself, I will re-check" wording, and maps the
    generic category by text-matching instead. Losing the category would park the
    operator's Discord login under the wrong guidance.
    """
    return ToolResult(
        status=ToolResultStatus.NEEDS_USER_ACTION,
        error=str(exc),
        error_category=CAT_LOGIN_REQUIRED,
        retryable=False,
        data={"error_category": CAT_LOGIN_REQUIRED, "indicator": exc.indicator},
    )


async def _operator_intent(db, task_id: Optional[str]) -> str:
    """What the operator's own task says this work may do.

    Resolution order, most authoritative first:

    1. **The intent declared on the Discord page**, read back from the task
       checkpoint. This is an explicit choice the operator made with a radio button,
       so it outranks anything inferred from prose - and it survives a restart, so a
       resumed task cannot drift into write authority.
    2. **The task's own title and description**, classified by
       ``routing.read_intent``.

    Returns ``""`` when neither can be read. ``enforce_intent`` treats an empty level
    as ``unknown`` and refuses writes, so an unreadable task fails CLOSED: a
    database hiccup must never widen what a task is allowed to do.

    Nothing here reads a tool argument. That is the whole point.
    """
    if not task_id:
        return ""
    try:
        from backend.app.integrations.discord import state as discord_state
        from backend.app.models import Task
        from backend.app.tasks.checkpoints import CheckpointManager

        result = await db.execute(select(Task).where(Task.task_id == task_id))
        task = result.scalars().first()
        if task is None:
            return ""

        checkpoint = await CheckpointManager(db).load_latest(task)
        declared = discord_state.load(
            checkpoint.state if checkpoint is not None else None
        ).get("intent", "")
        if declared in routing.INTENTS and declared != routing.INTENT_UNKNOWN:
            return declared

        wording = " ".join(
            part for part in (task.title, task.description or "") if part
        )[:4000]
        return routing.read_intent(wording).level
    except Exception:
        # Fail closed, loudly in the log but silently to the caller: the refusal
        # message explains that the wording could not be read.
        logger.warning(
            "Could not resolve the operator's intent for %s; writes will be refused", task_id
        )
        return ""


def _to_tool_result(outcome) -> ToolResult:
    """Map one Discord outcome onto the registry's envelope.

    The status mapping is the important part. UNCERTAIN is a failure that says why it
    must not be retried; NEEDS_USER_ACTION parks the task; a refusal is a failure,
    never an invitation to try something else.
    """
    data = outcome.as_dict()

    if outcome.needs_user:
        # Same reason as `_needs_user`: the category has to survive, or the Inbox
        # will not recognise this as a Discord login request.
        return ToolResult(
            status=ToolResultStatus.NEEDS_USER_ACTION,
            error=outcome.error or "A human must log in to Discord.",
            error_category=CAT_LOGIN_REQUIRED,
            retryable=False,
            data=data,
        )
    if outcome.ok:
        return ToolResult.success(
            data=data,
            evidence={
                "authoritative": True,
                "kind": "page_observation",
                "operation": outcome.operation,
                "status": outcome.status,
            },
        )
    if outcome.uncertain or outcome.status == UNCERTAIN:
        # `retryable=False` on purpose: the deterministic retry policy keys off this
        # flag, and an ambiguous send must be verified, not resent.
        detail = (
            f"{outcome.error} " if outcome.error else ""
        ) + (
            "The outcome of this Discord send is UNKNOWN: it may already have been sent. "
            "Do NOT send it again. Use discord_verify_message to check the channel first."
        )
        return ToolResult(
            status=ToolResultStatus.FAILURE,
            data=data,
            error=detail,
            error_category=outcome.error_category or "DISCORD_OUTCOME_UNKNOWN",
            retryable=False,
        )
    if outcome.status == "NEEDS_USER_ACTION":
        return ToolResult(
            status=ToolResultStatus.FAILURE,
            data=data,
            error=outcome.error or "Discord work is blocked right now.",
            error_category=outcome.error_category or "DISCORD_BLOCKED",
            retryable=False,
        )
    return ToolResult.failure(
        outcome.error or f"{outcome.operation} failed.",
        category=outcome.error_category or "DISCORD_OPERATION_FAILED",
        retryable=outcome.error_category in ("DISCORD_NETWORK", "DISCORD_RATE_LIMITED", "DISCORD_PAGE_ERROR"),
        data=data,
    )


_TARGET_PROPS: Dict[str, Any] = {
    "browser_profile_id": {
        "type": "string",
        "maxLength": 64,
        "description": (
            "The existing browser profile that is signed in to the Discord account this task acts as. "
            "MANDATORY: it is the account. Never guess it, and never switch profiles mid-task."
        ),
    },
}

_CHANNEL_PROPS: Dict[str, Any] = {
    "server_id": {"type": "string", "maxLength": 32, "description": "Discord server (guild) id, if known."},
    "channel_id": {"type": "string", "maxLength": 32, "description": "Discord channel id, if known."},
    "channel_name": {
        "type": "string", "maxLength": 80,
        "description": "Channel name including or without the leading #, used when the ids are not known.",
    },
}


class DiscordStatusTool(_DiscordToolBase):
    name = "discord_status"
    version = "1.0.0"
    description = (
        "What the Discord integration can do right now: which existing browser profiles are "
        "available to use as Discord accounts, what each operation does, and which ones send a "
        "message. Read-only. Call this first if you are unsure whether a Discord action is "
        "possible, rather than attempting it."
    )
    required_permissions = ["discord.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 45
    input_schema = {"type": "object", "properties": {}, "required": []}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        async with session_scope() as db:
            status = await service.integration_status(db, params.get("browser_profile_id"))
        return ToolResult.success(
            data=status,
            evidence={"authoritative": True, "kind": "discord_status"},
        )


class DiscordOpenTool(_DiscordToolBase):
    name = "discord_open"
    version = "1.0.0"
    description = (
        "Open the Discord web app in the selected browser profile and report whether that profile "
        "is actually signed in. Use this when a task needs to start on Discord, or when you need "
        "to check the session before acting. If it reports that a login is required, STOP and tell "
        "the operator - do not try to sign in yourself."
    )
    required_permissions = _DISCORD_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 120
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string", "maxLength": 300,
                "description": "Optional discord.com path or URL. Defaults to the app home.",
            },
            **_TARGET_PROPS,
        },
        "required": ["browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("open_app", params)


class DiscordListServersTool(_DiscordToolBase):
    name = "discord_list_servers"
    version = "1.0.0"
    description = (
        "List the Discord servers the selected account can see, with their ids. Use this to turn a "
        "server name the operator gave you into the id a channel needs."
    )
    required_permissions = _DISCORD_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 120
    input_schema = {
        "type": "object",
        "properties": {**_TARGET_PROPS},
        "required": ["browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("list_servers", params)


class DiscordOpenChannelTool(_DiscordToolBase):
    name = "discord_open_channel"
    version = "1.0.0"
    description = (
        "Open one Discord channel, by its server/channel ids, or by name when you only have a name. "
        "Every other Discord read needs to be on a channel first. If the account cannot access it, "
        "this fails rather than reading something else."
    )
    required_permissions = _DISCORD_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 120
    input_schema = {
        "type": "object",
        "properties": {**_CHANNEL_PROPS, **_TARGET_PROPS},
        "required": ["browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("open_channel", params)


class DiscordReadChannelTool(_DiscordToolBase):
    name = "discord_read_channel"
    version = "1.0.0"
    description = (
        "Read the messages in the channel the profile is currently on, with their author, timestamp "
        "and text. Set older_pages to scroll back through earlier messages the ordinary way. "
        "READ-ONLY: this cannot send anything, whatever the channel contains."
    )
    required_permissions = _DISCORD_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 180
    input_schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            "older_pages": {
                "type": "integer", "minimum": 0, "maximum": 20, "default": 0,
                "description": "How many times to scroll back for older messages.",
            },
            **_TARGET_PROPS,
        },
        "required": ["browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("read_channel", params)


class DiscordSearchTool(_DiscordToolBase):
    name = "discord_search"
    version = "1.0.0"
    description = (
        "Search Discord messages through Discord's own search box. Only what the signed-in account "
        "can already read is searched or returned. READ-ONLY."
    )
    required_permissions = _DISCORD_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 180
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": MAX_SEARCH_QUERY},
            "limit": {"type": "integer", "minimum": 1, "maximum": 60, "default": 25},
            **_TARGET_PROPS,
        },
        "required": ["query", "browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("search_messages", params)


class DiscordFindQuestionsTool(_DiscordToolBase):
    name = "discord_find_unanswered_questions"
    version = "1.0.0"
    description = (
        "Find messages in the current channel that look like questions and have no obvious answer "
        "later in what was read. These are CANDIDATES, not facts: Discord's web page does not "
        "expose reliable reply threading, so confirm any of them with the operator before acting. "
        "READ-ONLY."
    )
    required_permissions = _DISCORD_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 180
    input_schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            "older_pages": {"type": "integer", "minimum": 0, "maximum": 10, "default": 1},
            **_TARGET_PROPS,
        },
        "required": ["browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("find_unanswered_questions", params)


class DiscordSendTool(_DiscordToolBase):
    name = "discord_send_message"
    version = "1.0.0"
    description = (
        "Send a message to the channel the selected profile is currently on. This publishes "
        "something under the operator's own Discord account and CANNOT BE UNDONE.\n"
        "Only use it when the operator asked for a message to actually be sent. If they asked for a "
        "draft, return the text instead - do not call this.\n"
        "Never send to a channel the operator did not name, and never send the same message to "
        "several channels. Discord's rate limits and each server's rules apply, and unsolicited "
        "bulk messaging is out of scope.\n"
        "The result is verified against the page. If it reports the outcome is UNKNOWN, do NOT send "
        "again - call discord_verify_message first."
    )
    required_permissions = _DISCORD_WRITE_PERMS
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 180
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string", "minLength": 1, "maxLength": MAX_TEXT_CHARS,
                "description": "The message text. Discord's limit is 2000 characters.",
            },
            **_TARGET_PROPS,
        },
        "required": ["text", "browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("send_message", params)


class DiscordReplyTool(_DiscordToolBase):
    name = "discord_reply_to_message"
    version = "1.0.0"
    description = (
        "Reply to one specific Discord message. Same rules as discord_send_message: it publishes "
        "something the operator cannot unsend, so only send when they asked for the reply to be "
        "sent rather than drafted. The message must be visible in the channel you are on. "
        "An UNKNOWN result means do not repeat it."
    )
    required_permissions = _DISCORD_WRITE_PERMS
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 180
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_CHARS},
            "reply_to_message_id": {
                "type": "string", "maxLength": 32,
                "description": "The id of the message being replied to, as read from the channel.",
            },
            **_TARGET_PROPS,
        },
        "required": ["text", "reply_to_message_id", "browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("reply_to_message", params)


class DiscordVerifyMessageTool(_DiscordToolBase):
    name = "discord_verify_message"
    version = "1.0.0"
    description = (
        "Check whether a given message text is currently in the channel the profile is on. "
        "READ-ONLY. Use this to close an UNCERTAIN send BEFORE deciding whether to send again - "
        "this is the only safe way to decide whether a previous message landed."
    )
    required_permissions = _DISCORD_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 150
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_CHARS},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            **_TARGET_PROPS,
        },
        "required": ["text", "browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("verify_message", params)


class DiscordJoinServerTool(_DiscordToolBase):
    name = "discord_join_server"
    version = "1.0.0"
    description = (
        "Join a Discord server from an invite the operator supplied. Only ever with an invite they "
        "gave you for this task. Never accept unsolicited invitations, never join in bulk, and "
        "never use this to raise engagement on a server."
    )
    required_permissions = _DISCORD_WRITE_PERMS
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 180
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "invite": {
                "type": "string", "minLength": 2, "maxLength": 200,
                "description": "A Discord invite code or URL the operator supplied.",
            },
            **_TARGET_PROPS,
        },
        "required": ["invite", "browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("join_server", params)


class DiscordLeaveServerTool(_DiscordToolBase):
    name = "discord_leave_server"
    version = "1.0.0"
    description = (
        "Leave a Discord server. IRREVERSIBLE and only with the operator's explicit instruction - "
        "you may not lose access they cannot restore. Pass confirmed=true only when they have said "
        "so directly for this server, not when they mentioned it in passing."
    )
    required_permissions = _DISCORD_WRITE_PERMS
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 180
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "server_name": {"type": "string", "minLength": 1, "maxLength": 120},
            "confirmed": {
                "type": "boolean", "default": False,
                "description": "Must be true. There is no path to leaving a server without it.",
            },
            **_TARGET_PROPS,
        },
        "required": ["server_name", "confirmed", "browser_profile_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("leave_server", params)


DISCORD_TOOLS: List[BaseTool] = [
    DiscordStatusTool(),
    DiscordOpenTool(),
    DiscordListServersTool(),
    DiscordOpenChannelTool(),
    DiscordReadChannelTool(),
    DiscordSearchTool(),
    DiscordFindQuestionsTool(),
    DiscordSendTool(),
    DiscordReplyTool(),
    DiscordVerifyMessageTool(),
    DiscordJoinServerTool(),
    DiscordLeaveServerTool(),
]


def policy_allows(permission: str) -> bool:
    """Whether the operator's live policy grants one permission right now.

    Read from the running registry rather than a cached copy, so revoking a
    permission takes effect on the very next tool call.
    """
    return permission in set(getattr(registry.policy, "enabled_permissions", set()) or set())


def register_discord_tools(target_registry) -> None:
    """Register the Discord family with the shared registry."""
    target_registry.register_or_replace_many(DISCORD_TOOLS)