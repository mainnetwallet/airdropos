"""Telegram session/account tools: is the account usable, and why not?

An airdrop workflow that cannot reach Telegram cannot do anything, and the single
most expensive failure mode in an automation loop is a task that keeps running
against a session that has been paused, logged out or flood-limited. The agent
used to discover this only by failing: every tool returned the same opaque
"not logged in" and the model had to guess whether to retry, reconnect or stop.

These tools make the session itself observable. They answer three different
questions that were previously conflated:

* **Is Telegram usable right now?** -- ``telegram_check_connection``,
  ``telegram_check_login_status``, ``telegram_get_session_status``. These read
  LOCAL process state and never touch the network, so a diagnostic cannot mask
  the very problem it is diagnosing (a "check connection" that silently connects
  and reports "connected" is worse than useless).
* **What happened / what is blocked?** -- ``telegram_get_pending_user_action``,
  ``telegram_get_rate_limit_status``. These explain a pause honestly instead of
  leaving the model to invent a retry loop.
* **Can I change it?** -- ``telegram_reconnect``, ``telegram_disconnect``,
  ``telegram_resume_session``. These are the only mutating tools, and each one is
  explicit about what it does NOT do.

Why a diagnostic must not reconnect
-----------------------------------
``_TelegramUserSession._connect()`` lazily creates the client and connects it on
first use. If a "is it connected?" tool went through that path it would answer
its own question: the answer would be "yes, connected" even when the session had
been unusable a second earlier. So the reads here inspect the session object
directly (``is_connected()``, ``paused``, ``stopped``, ``waiting_reason``,
``last_error``, ``actions_today()``, ``gate_reason()``) and report what is true
right now.

Secrets
-------
Nothing here returns or logs a phone number, API hash, session string, login
code, 2FA password or ``access_hash``. Login state is reported as a tri-state
(True/False/None with a reason) because "could not be determined" is a real and
common answer: before the first call there is no client to ask.

The session state itself contains no credential -- ``telegram_accounts`` stores
only a label, a session path and profile display fields -- so the account list is
safe to return in full, minus the phone number that Telethon's ``User`` object
happens to carry and that is therefore never copied out.

This module contains NO new permission strings: every tool here requires only
``telegram.read``, because none of them sends anything to another party -- the
three mutating ones change this process's own connection state, which is what
``STATE_MUTATION`` already means.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from backend.app.core.config import settings
from backend.app.integrations import telegram_accounts
from backend.app.tools.base import BaseTool, SideEffect, ToolResult
from backend.app.tools.context import get_current_task_id
from backend.app.tools.telegram_user_tools import (
    ACCOUNT_PROPERTY,
    TELETHON_AVAILABLE,
    UnknownAccount,
    _TelegramUserSession,
    _evidence,
    all_sessions,
    session_for,
    telegram_capability_report,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------


def _account_rows() -> List[Dict[str, Any]]:
    """The registered accounts, without anything credential-shaped.

    ``telegram_accounts.list_accounts()`` is already the safe projection (label,
    session path, profile display name, whether a session file exists); nothing
    from it is a secret, and the session *path* is deliberately not returned --
    only whether a file is present -- so a tool result never helps an attacker
    locate a credential on disk.
    """
    rows: List[Dict[str, Any]] = []
    for account in telegram_accounts.list_accounts():
        rows.append(
            {
                "label": account.get("label"),
                "profile_name": account.get("profile_name"),
                "username": account.get("username"),
                "user_id": account.get("user_id"),
                "is_default": bool(account.get("is_default")),
                "session_file_present": bool(account.get("session_ready")),
            }
        )
    return rows


def _session_row(session: _TelegramUserSession) -> Dict[str, Any]:
    """One session's live, process-local state. Read-only, secret-free."""
    return {
        "account_label": session.label,
        "session_id": session.SESSION_ID,
        "connected": session.is_connected(),
        "paused": session.paused,
        "stopped": session.stopped,
        "gate_reason": session.gate_reason(),
        "waiting_reason": session.waiting_reason,
        "last_error": session.last_error,
        "ever_used": session.ever_used,
        "started_at": session.started_at,
        "last_action_at": session.last_action_at,
        "actions_today": session.actions_today(),
        "known_chats": len(session.known_chats()),
        "current_bot": session.current_bot,
    }


def _resolve(params: Dict[str, Any]) -> Any:
    """``(session, None)`` or ``(None, ToolResult)`` for an account label."""
    try:
        return session_for(params.get("account")), None
    except UnknownAccount as exc:
        return None, ToolResult.failure(str(exc), category="INVALID_INPUT", retryable=False)


async def _probe_authorized(client: Any) -> Optional[bool]:
    """``True``/``False``, or ``None`` when the client cannot answer.

    ``is_user_authorized`` needs a live connection. A client that is present but
    not connected answers ``False`` on some Telethon versions and raises on
    others, and that ``False`` would be a lie (it means "not connected", not
    "not authorized"). So the connection is checked first and an unknown answer
    is reported as ``None`` rather than guessed.
    """
    try:
        if not client.is_connected():
            return None
    except Exception:
        return None
    checker = getattr(client, "is_user_authorized", None)
    if checker is None:
        return None
    try:
        return bool(await checker())
    except Exception as exc:  # pragma: no cover - defensive
        logger.info("Telegram login probe failed (%s)", type(exc).__name__)
        return None


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------


class TelegramCheckConnectionTool(BaseTool):
    """Is the Telegram client connected, and what blocks it if it is not."""

    name = "telegram_check_connection"
    version = "1.0.0"
    description = (
        "Report whether this Telegram account is currently connected to Telegram, whether an "
        "operator paused or stopped it, and what exactly is blocking use. Use this FIRST when a "
        "Telegram step fails or before starting Telegram work. It only inspects local state and "
        "never reconnects, so the answer is the true current state. `available=false` means the "
        "tools are not usable at all (feature flag, missing API id/hash, or telethon not "
        "installed); `connected=false` with `available=true` means configured but not connected."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        session, failure = _resolve(params)
        if failure is not None:
            return failure
        capability = telegram_capability_report()
        connected = session.is_connected()
        gate = session.gate_reason()
        data: Dict[str, Any] = {
            "available": bool(capability.get("available")),
            "connected": connected,
            "usable": bool(capability.get("available")) and connected and gate is None,
            "state": capability.get("state"),
            "reason": capability.get("reason"),
            "gate_reason": gate,
            "paused": session.paused,
            "stopped": session.stopped,
            "waiting_reason": session.waiting_reason,
            "last_error": session.last_error,
            "account": session.label,
            "sessions": [_session_row(s) for s in all_sessions()],
            "accounts": _account_rows(),
            "capability": capability,
            "next_steps": _connection_hints(bool(capability.get("available")), connected, gate),
        }
        return ToolResult.success(data=data)


def _connection_hints(available: bool, connected: bool, gate: Optional[str]) -> List[str]:
    """What to actually do about the state just reported."""
    if not available:
        return [
            "Telegram user tools are unavailable. Enable TELEGRAM_USER_ENABLED, set "
            "TELEGRAM_API_ID/TELEGRAM_API_HASH and install telethon.",
        ]
    if gate:
        return [f"The session is gated: {gate} An operator must lift it in the UI.", "telegram_resume_session"]
    if not connected:
        return ["The session is configured but not connected.", "telegram_reconnect", "telegram_resume_session"]
    return ["The session is connected and ready."]


class TelegramCheckLoginStatusTool(BaseTool):
    """Is the Telegram session authorized, as a tri-state with a reason."""

    name = "telegram_check_login_status"
    version = "1.0.0"
    description = (
        "Report whether the Telegram session is logged in (authorized). Returns logged_in=true, "
        "false, or null with a reason: null means it could not be determined, which is normal "
        "before the first Telegram call because no connection exists yet. Never returns or asks "
        "for the phone number, login code or 2FA password -- login is done once by the operator in "
        "a terminal with `python scripts/telegram_login.py`, never by a tool."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        session, failure = _resolve(params)
        if failure is not None:
            return failure

        client = getattr(session, "_client", None)
        connected = session.is_connected()
        if client is None:
            logged_in: Optional[bool] = None
            reason = (
                "No Telegram client has been created in this process yet, so the login state is "
                "unknown. It will be checked on the first real Telegram call."
            )
        else:
            logged_in = await _probe_authorized(client)
            reason = (
                ""
                if logged_in is not None
                else "The Telegram client could not report whether it is authorized, usually "
                "because it is not connected. Reconnect, then check again."
            )

        account = next(
            (a for a in _account_rows() if a["label"] == session.label), None
        ) or {}
        return ToolResult.success(
            data={
                "logged_in": logged_in,
                "determined": logged_in is not None,
                "connected": connected,
                "reason": reason,
                "account": session.label,
                "session_file_present": account.get("session_file_present"),
                "blocked": session.gate_reason(),
                "login_instructions": (
                    "Run `python scripts/telegram_login.py` in a terminal, then retry. No tool "
                    "ever handles the phone number, the login code or the 2FA password."
                ),
            },
            evidence=_evidence("account_read", account=session.label, logged_in=logged_in),
        )


class TelegramGetAccountProfileTool(BaseTool):
    """The registered profile of one account (label, @username, ids)."""

    name = "telegram_get_account_profile"
    version = "1.0.0"
    description = (
        "Return the profile of one or all registered Telegram accounts: label, @username, display "
        "name, numeric user id, which one is the default, and whether its session file exists. "
        "Use it to find the right account label before passing `account` to another tool. Never "
        "returns the phone number. For the LIVE Telegram profile of the account in use, call "
        "telegram_get_my_account instead."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        session, failure = _resolve(params)
        if failure is not None:
            return failure
        rows = _account_rows()
        focused = next((a for a in rows if a["label"] == session.label), None)
        return ToolResult.success(
            data={
                "account": focused or {"label": session.label, "registered": False},
                "registered": focused is not None,
                "accounts": rows,
                "count": len(rows),
                "profile_source": "account_registry",
                "note": (
                    "Profiles come from the account registry written at login time. Values are "
                    "absent for accounts registered before a profile was stored. The phone number "
                    "is never part of that registry and is never returned here."
                ),
            },
            evidence=_evidence("account_read", account=session.label),
        )


class TelegramGetSessionStatusTool(BaseTool):
    """Operational status of the session: gate, counters, last error."""

    name = "telegram_get_session_status"
    version = "1.0.0"
    description = (
        "Return the full operational status of the Telegram session: connected, paused, stopped, "
        "waiting reason, last error, actions used today, which chats it has resolved, and which bot "
        "the current task is anchored to. Use it to decide whether to reconnect, resume, or wait "
        "for the operator. Purely local state; it never reconnects and never contacts Telegram."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        session, failure = _resolve(params)
        if failure is not None:
            return failure
        other = [_session_row(s) for s in all_sessions() if s is not session]
        needs_user = bool(session.gate_reason()) or bool(session.waiting_reason)
        return ToolResult.success(
            data={
                "session": _session_row(session),
                "other_sessions": other,
                "accounts": _account_rows(),
                "needs_user_action": needs_user,
                "capability": telegram_capability_report(),
                "task_id": get_current_task_id(),
            },
            evidence=_evidence("account_read", account=session.label),
        )


class TelegramReconnectTool(BaseTool):
    """Re-establish the connection for one account."""

    name = "telegram_reconnect"
    version = "1.0.0"
    description = (
        "Reconnect the Telegram account (run telegram_check_connection first to see whether this "
        "is needed). Connects the existing session and reports the resulting state, including "
        "whether Telegram considers it logged in. Safe to retry: if the connection is already up "
        "this is a no-op. Does NOT log in, does not touch credentials and does not disconnect the "
        "other accounts."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.STATE_MUTATION
    timeout_seconds = 60
    is_idempotent = True
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        session, failure = _resolve(params)
        if failure is not None:
            return failure
        was_connected = session.is_connected()

        # session.run() -> _connect() creates the client if needed and connects it if
        # it is down; if the session is not authorized that raises _NotLoggedIn,
        # which run() turns into NEEDS_USER_ACTION with the login instructions.
        async def action(client: Any) -> ToolResult:
            connected = bool(client.is_connected())
            authorized = await _probe_authorized(client)
            return ToolResult.success(
                data={
                    "account": session.label,
                    "was_connected": was_connected,
                    "connected": connected,
                    "reconnected": connected and not was_connected,
                    "logged_in": authorized,
                    "session_state": _session_row(session),
                    "note": (
                        "Reconnect only restores the network connection. If the session is not "
                        "authorized, an operator must run `python scripts/telegram_login.py`."
                    ),
                },
                evidence=_evidence(
                    "account_read", account=session.label, connected=connected, logged_in=authorized
                ),
            )

        return await session.run(
            action, interactive=False, label=f"Reconnected Telegram account {session.label}"
        )


class TelegramDisconnectTool(BaseTool):
    """Drop the connection for ONE named/default account."""

    name = "telegram_disconnect"
    version = "1.0.0"
    description = (
        "Disconnect ONE Telegram account (the one named by `account`, else the default). Use it to "
        "release a stalled connection or before restarting the service. It does NOT log out of "
        "Telegram, does NOT revoke the session, does NOT delete the session file, and never "
        "touches the other accounts -- the session reconnects automatically on the next call. "
        "Refused while an operator has paused or stopped that session."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.STATE_MUTATION
    timeout_seconds = 45
    is_idempotent = True
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        session, failure = _resolve(params)
        if failure is not None:
            return failure
        was_connected = session.is_connected()
        gate = session.gate_reason()
        if gate:
            # The gate is the operator's, and dropping the connection of a session
            # they deliberately froze is a late call acting against their decision --
            # the same rule _run_locked() enforces for everything else.
            return ToolResult.needs_user(
                f"{gate} An operator must clear it before this session is disconnected.",
                data={
                    "account": session.label,
                    "disconnected": False,
                    "was_connected": was_connected,
                    "gate_reason": gate,
                },
            )
        # disconnect_quietly() never raises and returns whether the client is really
        # down, which is the fact worth reporting rather than "the call returned".
        down = await session.disconnect_quietly()
        return ToolResult.success(
            data={
                "account": session.label,
                "disconnected": down,
                "was_connected": was_connected,
                "connected": session.is_connected(),
                "logged_out": False,
                "session_revoked": False,
                "session_file_deleted": False,
                "other_accounts_untouched": True,
                "note": (
                    "Only the named account was disconnected. The Telegram login stays valid and "
                    "the session file is kept; the next Telegram call reconnects automatically."
                ),
            },
            evidence=_evidence("account_read", account=session.label, disconnected=down),
        )


class TelegramGetRateLimitStatusTool(BaseTool):
    """How much Telegram action budget is left, and how long to wait."""

    name = "telegram_get_rate_limit_status"
    version = "1.0.0"
    description = (
        "Report how many Telegram actions this account has used today against the configured daily "
        "limit, the minimum delay enforced between actions, how long until the next action is "
        "allowed, and whether the session is paused. Use it before a burst of sends/clicks and to "
        "explain a RATE_LIMITED result. Purely local accounting; it contacts nobody and cannot "
        "raise a limit."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        session, failure = _resolve(params)
        if failure is not None:
            return failure

        used = session.actions_today()
        limit = int(settings.TELEGRAM_USER_DAILY_ACTION_LIMIT)
        min_delay = float(settings.TELEGRAM_USER_MIN_ACTION_DELAY_SECONDS)

        # ``_pace`` sleeps for the remainder of the minimum delay after the previous
        # action, so this is exactly how long the next interactive call waits.
        wait = None
        if session.last_action_at is not None:
            wait = round(max(0.0, min_delay - (time.time() - session.last_action_at)), 2)

        # The session does not record FloodWait values today, so this is reported as
        # None with the reason rather than guessed. If the session ever starts
        # tracking it, the field starts carrying the real value with no change here.
        flood_wait = getattr(session, "flood_wait_last_seconds", None)
        remaining = max(0, limit - used)

        data: Dict[str, Any] = {
            "account": session.label,
            "actions_used_today": used,
            "daily_action_limit": limit,
            "actions_remaining": remaining,
            "daily_limit_reached": used >= limit,
            "min_action_delay_seconds": min_delay,
            "seconds_until_next_action_allowed": wait,
            "flood_wait_last_seconds": flood_wait,
            "flood_wait_tracked": flood_wait is not None,
            "flood_wait_note": (
                ""
                if flood_wait is not None
                else "The session does not currently record flood waits, so no value is reported. "
                "A FloodWait that Telegram raises is still returned to the caller as a "
                "RATE_LIMITED failure with the exact number of seconds to wait."
            ),
            "paused": session.paused,
            "stopped": session.stopped,
            "gate_reason": session.gate_reason(),
            "note": (
                "Read-only accounting. Limits cannot be raised from here; they come from "
                "TELEGRAM_USER_DAILY_ACTION_LIMIT and TELEGRAM_USER_MIN_ACTION_DELAY_SECONDS."
            ),
        }
        return ToolResult.success(
            data=data, evidence=_evidence("account_read", account=session.label, actions_used=used)
        )


class TelegramGetPendingUserActionTool(BaseTool):
    """Is the session waiting on the operator, and for what exactly?"""

    name = "telegram_get_pending_user_action"
    version = "1.0.0"
    description = (
        "Report whether the Telegram session is currently waiting on the operator: paused or "
        "stopped, a NEEDS_USER_ACTION reason from a previous call (login required, session "
        "revoked, an unapprovable join), or a sticky waiting_reason. Use it when a task appears "
        "stuck. needs_user_action is true ONLY when one of those is real -- it never invents a "
        "blocker. When a blocker IS real the result status is NEEDS_USER_ACTION so the workflow "
        "waits for the operator instead of retrying; when there is none it is a plain SUCCESS."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        session, failure = _resolve(params)
        if failure is not None:
            return failure

        gate = session.gate_reason()
        waiting = session.waiting_reason
        blocked = bool(gate) or bool(waiting)

        if gate:
            reason = gate
            remedy = "An operator must resume or clear this session in the AirdropOS UI."
            kind = "operator_gate"
        elif waiting:
            reason = waiting
            remedy = (
                "An operator must complete the action Telegram asked for (for example the login "
                "script, or approving something in the app), then call telegram_resume_session."
            )
            kind = "needs_user_action"
        else:
            reason = None
            remedy = ""
            kind = None

        data: Dict[str, Any] = {
            "account": session.label,
            "needs_user_action": blocked,
            "kind": kind,
            "reason": reason,
            "what_the_operator_must_do": remedy,
            "paused": session.paused,
            "stopped": session.stopped,
            "gate_reason": gate,
            "waiting_reason": waiting,
            "last_error": session.last_error,
            "connected": session.is_connected(),
            "status_if_called_now": "NEEDS_USER_ACTION" if blocked else "SUCCESS",
            "resumable": blocked is False,
            "note": (
                "The full explanation is always in this payload. When a blocker is real the "
                "status is NEEDS_USER_ACTION as well, so a workflow that only reads the status "
                "waits for the operator instead of retrying into a gated session."
            ),
        }
        if blocked:
            # A real block is surfaced as NEEDS_USER_ACTION so a controller that only
            # looks at the status sees it; the full explanation stays in `data`.
            result = ToolResult.needs_user(reason or "Operator action required.", data=data)
            result.evidence = _evidence(
                "account_read", account=session.label, blocked=True, block_kind=kind
            )
            return result
        return ToolResult.success(
            data=data, evidence=_evidence("account_read", account=session.label, blocked=False)
        )


class TelegramResumeSessionTool(BaseTool):
    """Re-anchor the session to this task and make it usable again."""

    name = "telegram_resume_session"
    version = "1.0.0"
    description = (
        "Resume Telegram work for the current task after a pause or a stall: anchors the session to "
        "this task, reconnects if the connection is down, and reports whether Telegram is usable "
        "again. If an operator paused or stopped the session this does NOT lift that gate -- it "
        "returns NEEDS_USER_ACTION naming what the operator must do, because only the operator can "
        "clear it. Use it after telegram_get_pending_user_action reported a blocker that has been "
        "dealt with."
    )
    required_permissions = ["telegram.read"]
    side_effect = SideEffect.STATE_MUTATION
    timeout_seconds = 60
    is_idempotent = True
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        session, failure = _resolve(params)
        if failure is not None:
            return failure

        was_connected = session.is_connected()
        task_id = get_current_task_id()
        # Anchoring is the only state this tool writes, and it is process-local:
        # the live event stream then attributes this session's actions to the task
        # that resumed it. The task id is read from the execution context, never
        # from a parameter, so the agent cannot forge it.
        session.task_id = task_id

        gate = session.gate_reason()
        if gate:
            return ToolResult.needs_user(
                f"The Telegram session cannot be resumed from here: {gate} An operator must clear "
                "the session in the AirdropOS UI; this tool will not override them.",
                data={
                    "account": session.label,
                    "resumed": False,
                    "task_id": task_id,
                    "gate_reason": gate,
                    "operator_gate": True,
                    "session_state": _session_row(session),
                },
            )

        async def action(client: Any) -> ToolResult:
            connected = bool(client.is_connected())
            authorized = await _probe_authorized(client)
            usable = connected and authorized is not False
            return ToolResult.success(
                data={
                    "account": session.label,
                    "resumed": True,
                    "task_id": task_id,
                    "was_connected": was_connected,
                    "connected": connected,
                    "logged_in": authorized,
                    "reconnected": connected and not was_connected,
                    "usable": usable,
                    "previous_waiting_reason": session.waiting_reason,
                    "session_state": _session_row(session),
                    "capability": telegram_capability_report(),
                    "note": (
                        "A successful action clears the sticky waiting reason and last error. "
                        "Reconnect happened only if the connection was down."
                    ),
                },
                evidence=_evidence(
                    "account_read", account=session.label, resumed=True, task_id=task_id
                ),
            )

        return await session.run(
            action, interactive=False, label=f"Resumed Telegram session {session.label}"
        )


SESSION_TOOLS = (
    TelegramCheckConnectionTool,
    TelegramCheckLoginStatusTool,
    TelegramGetAccountProfileTool,
    TelegramGetSessionStatusTool,
    TelegramReconnectTool,
    TelegramDisconnectTool,
    TelegramGetRateLimitStatusTool,
    TelegramGetPendingUserActionTool,
    TelegramResumeSessionTool,
)


def register_session_tools(registry: Any) -> bool:
    """Register the session tools only when the Telegram user client is usable.

    Identical gate to ``register_telegram_user_tools()``: without the feature
    flag, the API credentials or telethon these tools have nothing to report and
    must not appear in the model's catalog at all.
    """
    if not settings.TELEGRAM_USER_ENABLED:
        logger.info(
            "Telegram session tools not registered: TELEGRAM_USER_ENABLED is false "
            "(set it to true and configure TELEGRAM_API_ID/TELEGRAM_API_HASH to enable)."
        )
        return False
    if not (settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH):
        logger.warning(
            "TELEGRAM_USER_ENABLED is true but TELEGRAM_API_ID/TELEGRAM_API_HASH are missing "
            "(missing configuration)."
        )
        return False
    if not TELETHON_AVAILABLE:
        logger.warning(
            "TELEGRAM_USER_ENABLED is true but telethon is not installed (missing dependency: "
            "pip install telethon)."
        )
        return False

    tools = [tool() for tool in SESSION_TOOLS]
    for tool in tools:
        tool.input_schema.setdefault("properties", {}).setdefault("account", ACCOUNT_PROPERTY)
    registry.register_or_replace_many(tools)
    return True