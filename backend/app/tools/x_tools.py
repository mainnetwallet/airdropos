"""The unified X.com capability exposed to the agent.

One tool per operation, not one per method. Each tool takes a ``method``
argument (``auto`` | ``api`` | ``browser``) and hands everything else to
:func:`backend.app.integrations.x.service.execute`, which decides, de-duplicates,
executes, verifies and records. Routing therefore cannot drift between the chat
console, the task worker and the X.com page: they all call the same function.

Deliberately NOT provided: any shell, SQL, filesystem or raw-HTTP tool for X.
Every X action goes through one of these registered tools, so the registry's
permission checks, the emergency stop, the audit trail and the operator's
confirmation policy all still apply.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.app.database.session import session_scope
from backend.app.integrations.x import routing, service
from backend.app.integrations.x.store import StoreError
from backend.app.tools.base import BaseTool, SideEffect, ToolResult, ToolResultStatus
from backend.app.tools.context import get_current_task_id
from backend.app.tools.registry import registry

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 25_000
MAX_HANDLE_CHARS = 15

METHOD_ENUM = ["auto", "api", "browser"]

_METHOD_HINT = (
    "method: auto (default) picks the right one; 'api' forces the official X API; "
    "'browser' forces the signed-in x.com website. A forced method is still refused when "
    "that method cannot do the operation or is not authorized."
)

# Every X tool can be routed to the signed-in browser at runtime, and EVERY one of
# them has to change the URL to do its job: the X UI has no in-page API, so
# reading a profile means navigating to it. `browser.navigate` is therefore
# required by all of them, reads included - otherwise revoking that permission
# (which `bootstrap.py` deliberately separates from `browser.read`) would not
# actually stop X work from navigating.
_BROWSER_READ_PERMS = ["x.read", "browser.read", "browser.navigate"]
_BROWSER_WRITE_PERMS = ["x.write", "browser.interact", "browser.navigate"]


class _XToolBase(BaseTool):
    """Shared plumbing: routing arguments, result mapping, failure honesty."""

    async def _run(
        self,
        operation: str,
        params: Dict[str, Any],
        *,
        task_text: str = "",
        post_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> ToolResult:
        # Normalize BEFORE anything else: an argument the model got wrong must
        # come back as a precise validation failure, not as a 500 from deep in a
        # client, and it must never reach the network half-formed.
        try:
            if post_id is not None:
                params = {**params, "post_id": service.parse_post_id(post_id)}
            if username is not None:
                params = {**params, "username": service.parse_handle(username)}
        except service.XError as exc:
            return ToolResult.failure(str(exc), category="INVALID_INPUT", retryable=False)

        async with session_scope() as db:
            try:
                outcome = await service.execute(
                    db,
                    operation,
                    # No `preference` argument: the service reads the operator's
                    # stored preference, which is what keeps the API process and
                    # the worker process routing identically.
                    method=str(params.get("method") or routing.AUTO),
                    config_id=_optional(params.get("config_id")),
                    browser_profile_id=_optional(params.get("browser_profile_id")),
                    task_text=task_text,
                    task_id=get_current_task_id(),
                    params=params,
                    allow_duplicate=bool(params.get("allow_duplicate", False)),
                    browser_permitted=policy_allows("browser.interact"),
                )
            except StoreError as exc:
                return ToolResult.failure(str(exc), category="X_CONFIGURATION_ERROR")
            except service.XError as exc:
                return ToolResult.failure(str(exc), category=exc.category)

        return _to_tool_result(self.name, outcome)


def _optional(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def policy_allows(permission: str) -> bool:
    """Whether the operator's live policy grants one permission right now.

    Read from the running registry rather than a cached copy, so revoking a
    permission takes effect on the very next tool call.
    """
    return permission in set(getattr(registry.policy, "enabled_permissions", set()) or set())


def _to_tool_result(tool_name: str, outcome: service.XOutcome) -> ToolResult:
    """Map one X outcome onto the registry's result envelope.

    The status mapping is the important part. An UNCERTAIN write is a FAILURE
    that says why it must not be retried; NEEDS_USER_ACTION parks the task; an
    X-side refusal is a failure, never a fallback invitation.
    """
    data = outcome.as_dict()

    if outcome.needs_user:
        return ToolResult.needs_user(outcome.error or "Human interaction required on x.com.", data=data)
    if outcome.ok:
        return ToolResult.success(
            data=data,
            evidence={
                "authoritative": True,
                "kind": "x_api_response" if outcome.method == routing.API else "page_observation",
                "operation": outcome.operation,
                "method": outcome.method,
                "status": outcome.status,
            },
        )
    if outcome.status == service.UNCERTAIN or outcome.uncertain:
        # `retryable=False` on purpose: the deterministic retry policy keys off
        # this flag, and an ambiguous write must be verified, not resent.
        #
        # The uncertainty is also restated in the error itself, whatever the
        # underlying message was. The model reads the message, and a bare "no
        # answer" reads like a transient failure to try again.
        detail = (
            f"{outcome.error} " if outcome.error else ""
        ) + (
            "The outcome of this X operation is UNKNOWN: it may still have been applied. "
            "Do NOT repeat the action. Run x_verify_action to check the current state first."
        )
        return ToolResult(
            status=ToolResultStatus.FAILURE,
            data=data,
            error=detail,
            error_category=outcome.error_category or "X_OUTCOME_UNCERTAIN",
            retryable=False,
        )
    if outcome.status == service.BLOCKED:
        return ToolResult(
            status=ToolResultStatus.FAILURE,
            data=data,
            error=outcome.blocked_reason or outcome.error or "X work is blocked right now.",
            error_category=outcome.error_category or "X_OPERATION_BLOCKED",
            retryable=False,
        )
    return ToolResult.failure(
        outcome.error or f"{outcome.operation} failed.",
        category=outcome.error_category or "X_OPERATION_FAILED",
        retryable=outcome.error_category in ("X_RATE_LIMITED", "X_SERVER_ERROR", "X_NETWORK_ERROR"),
        data=data,
    )


_TARGET_PROPS: Dict[str, Any] = {
    "method": {"type": "string", "enum": METHOD_ENUM, "description": f"Which method to use. {_METHOD_HINT}"},
    "config_id": {
        "type": "string", "maxLength": 64,
        "description": "Named API configuration to use. Omit to let AirdropOS pick an authorized one.",
    },
    "browser_profile_id": {
        "type": "string", "maxLength": 64,
        "description": "Browser profile to use for browser mode. Omit to use the operator's default agent profile.",
    },
}


class XStatusTool(_XToolBase):
    name = "x_status"
    version = "1.0.0"
    description = (
        "What the X.com integration can do right now: which browser profiles are available, "
        "which API configurations exist and whether each is authorized, the operator's "
        "execution preference, and which operations each method supports. Read-only; use it "
        "before guessing whether an X action is possible."
    )
    required_permissions = _BROWSER_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 45
    input_schema = {"type": "object", "properties": {}, "required": []}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        async with session_scope() as db:
            status = await service.configuration_status(db)
        if status.get("browser_profile_id"):
            from backend.app.integrations.x import browser_adapter

            try:
                status["browser"] = await browser_adapter.session_status(status["browser_profile_id"])
            except Exception as exc:
                status["browser"] = {"ok": False, "detail": f"{type(exc).__name__}"}
        return ToolResult.success(data=status, evidence={"authoritative": True, "kind": "x_status"})


class XReadProfileTool(_XToolBase):
    name = "x_read_profile"
    version = "1.0.0"
    description = (
        "Look up an X (Twitter) account by @handle and return its real profile: id, username, "
        "display name, bio, follower counts. Uses the API when one is authorized and the "
        "website when the task is about the page."
    )
    required_permissions = _BROWSER_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 90
    input_schema = {
        "type": "object",
        "properties": {
            "username": {"type": "string", "maxLength": MAX_HANDLE_CHARS, "description": "Handle without @."},
            **_TARGET_PROPS,
        },
        "required": ["username"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("read_profile", params, username=params.get("username"))


class XReadPostTool(_XToolBase):
    name = "x_read_post"
    version = "1.0.0"
    description = (
        "Read a single X (Twitter) post by id or by status URL and return its text, author and "
        "metrics. Never guesses content it did not read."
    )
    required_permissions = _BROWSER_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 90
    input_schema = {
        "type": "object",
        "properties": {
            "post_id": {"type": "string", "maxLength": 512, "description": "Post id or an x.com status URL."},
            **_TARGET_PROPS,
        },
        "required": ["post_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("read_post", params, post_id=params.get("post_id"))


class XPostTool(_XToolBase):
    name = "x_post"
    version = "1.0.0"
    description = (
        "Publish a post on X (Twitter). Returns the new post's id when the API created it. "
        "Refuses to send the same text twice for the same account: if an identical post already "
        "succeeded it reports that instead of duplicating it, and if an earlier attempt's "
        "outcome was unknown it refuses and asks you to verify first. Use x_verify_action for "
        "that check. Publishing publicly cannot be undone."
    )
    required_permissions = _BROWSER_WRITE_PERMS
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 180
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "maxLength": MAX_TEXT_CHARS, "description": "The post text."},
            **_TARGET_PROPS,
        },
        "required": ["text"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("create_post", params, task_text=str(params.get("text") or ""))


class XReplyTool(_XToolBase):
    name = "x_reply"
    version = "1.0.0"
    description = (
        "Publish a reply to an existing X (Twitter) post. Same duplicate protection as x_post."
    )
    required_permissions = _BROWSER_WRITE_PERMS
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 180
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "maxLength": MAX_TEXT_CHARS, "description": "The reply text."},
            "post_id": {"type": "string", "maxLength": 512, "description": "Post being replied to (id or URL)."},
            **_TARGET_PROPS,
        },
        "required": ["text", "post_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        try:
            reply_to = service.parse_post_id(params.get("post_id"))
        except service.XError as exc:
            return ToolResult.failure(str(exc), category="INVALID_INPUT", retryable=False)
        return await self._run(
            "create_reply", {**params, "reply_to": reply_to}, task_text=str(params.get("text") or "")
        )


class XLikeTool(_XToolBase):
    name = "x_like"
    version = "1.0.0"
    description = (
        "Like an X (Twitter) post. Idempotent: if this account already liked that exact post, "
        "the tool reports that and clicks nothing."
    )
    required_permissions = _BROWSER_WRITE_PERMS
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 150
    input_schema = {
        "type": "object",
        "properties": {
            "post_id": {"type": "string", "maxLength": 512, "description": "Post id or an x.com status URL."},
            **_TARGET_PROPS,
        },
        "required": ["post_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("like", params, post_id=params.get("post_id"))


class XUnlikeTool(_XToolBase):
    name = "x_unlike"
    version = "1.0.0"
    description = "Remove this account's like from an X (Twitter) post. Idempotent."
    required_permissions = _BROWSER_WRITE_PERMS
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 150
    input_schema = {
        "type": "object",
        "properties": {
            "post_id": {"type": "string", "maxLength": 512, "description": "Post id or an x.com status URL."},
            **_TARGET_PROPS,
        },
        "required": ["post_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("unlike", params, post_id=params.get("post_id"))


class XFollowTool(_XToolBase):
    name = "x_follow"
    version = "1.0.0"
    description = (
        "Follow an X (Twitter) account by @handle. Idempotent: already-followed is reported, "
        "not clicked again."
    )
    required_permissions = _BROWSER_WRITE_PERMS
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 150
    input_schema = {
        "type": "object",
        "properties": {
            "username": {"type": "string", "maxLength": MAX_HANDLE_CHARS, "description": "Handle without @."},
            **_TARGET_PROPS,
        },
        "required": ["username"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("follow", params, username=params.get("username"))


class XUnfollowTool(_XToolBase):
    name = "x_unfollow"
    version = "1.0.0"
    description = "Unfollow an X (Twitter) account by @handle. Idempotent."
    required_permissions = _BROWSER_WRITE_PERMS
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 150
    input_schema = {
        "type": "object",
        "properties": {
            "username": {"type": "string", "maxLength": MAX_HANDLE_CHARS, "description": "Handle without @."},
            **_TARGET_PROPS,
        },
        "required": ["username"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("unfollow", params, username=params.get("username"))


class XVerifyActionTool(_XToolBase):
    name = "x_verify_action"
    version = "1.0.0"
    description = (
        "Check whether an X (Twitter) action actually happened, instead of retrying it. Use this "
        "whenever a previous x_post / x_like / x_follow reported an unknown outcome or timed out: "
        "it reads the real current state and closes the uncertainty. Never send a write twice "
        "without checking here first."
    )
    required_permissions = _BROWSER_READ_PERMS
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 120
    input_schema = {
        "type": "object",
        "properties": {
            "check": {"type": "string", "enum": ["post", "like", "follow"], "description": "What to check."},
            "post_id": {"type": "string", "maxLength": 512, "description": "Post id (for check=post or like)."},
            "username": {"type": "string", "maxLength": MAX_HANDLE_CHARS, "description": "Handle (for check=follow)."},
            **_TARGET_PROPS,
        },
        "required": ["check"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return await self._run("verify_action", params, task_text=str(params.get("check") or ""))


X_TOOLS: List[BaseTool] = [
    XStatusTool(),
    XReadProfileTool(),
    XReadPostTool(),
    XPostTool(),
    XReplyTool(),
    XLikeTool(),
    XUnlikeTool(),
    XFollowTool(),
    XUnfollowTool(),
    XVerifyActionTool(),
]


def policy_allows(permission: str) -> bool:
    """Whether the operator's live policy grants one permission right now.

    Read from the running registry rather than a cached copy, so revoking a
    permission takes effect on the very next tool call.
    """
    return permission in set(getattr(registry.policy, "enabled_permissions", set()) or set())


def register_x_tools(target_registry) -> None:
    """Register the X family with the shared registry."""
    target_registry.register_or_replace_many(X_TOOLS)