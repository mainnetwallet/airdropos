"""Telegram (MTProto / Telethon) session.

This is an API session, not a screen, so there is no picture to stream. Instead the
dashboard renders a real-Telegram-like CHAT view of the account (chat list, messages,
inline buttons, send box) built from live Telethon data; see ``live/telegram_chat.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List

from backend.app.core.config import settings
from backend.app.live import hub
from backend.app.live.events import redact_text
from backend.app.live.models import Capabilities, Conflict, NotSupported, SessionNotFound
from backend.app.live.providers.base import SessionProvider


def _sessions():
    """[(key, session)] for every Telegram account used in this process. The legacy account keeps key 'agent'."""
    from backend.app.integrations.telegram_accounts import slugify
    from backend.app.tools.telegram_user_tools import _extra_sessions, _session as legacy

    return [("agent", legacy), *[(slugify(s.label), s) for s in _extra_sessions.values()]]


def _session_by_key(key: str):
    return next((tg for k, tg in _sessions() if k == key), None)


STOPPED_VISIBLE_SECONDS = 5  # how long a stopped card stays on Live Broadcast


class TelegramProvider(SessionProvider):
    integration = "telegram"
    label = "Telegram"
    capabilities = Capabilities(visual=False, events=True, pause_resume=True, stop=True, manual_control=False, chat=True)
    note = (
        "API session (Telethon). There is no screen streaming; the dashboard shows a live chat view of the account "
        "(chats, messages, buttons, send box) plus real activity events. Each logged-in account is its own session. "
        "Stop disconnects that account and blocks the agent until resumed."
    )

    _stopped_seen: Dict[str, float] = {}  # per account key: when this provider first saw it stopped

    def configured(self) -> bool:
        return bool(settings.TELEGRAM_USER_ENABLED and settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH)

    async def list_sessions(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for ordinal, (key, tg) in enumerate(_sessions(), start=1):
            card = self._card(key, tg, ordinal)
            if card is not None:
                out.append(card)
        return out

    def _card(self, key: str, tg: Any, ordinal: int) -> Dict[str, Any] | None:
        if not tg.ever_used:
            return None  # nothing real to show until the agent has used the account
        if tg.stopped:
            # Show "Stopped" briefly, then hide the card. The stopped flag itself stays set, so the
            # agent remains blocked from using the account until the session is resumed/worker restarted.
            now = time.time()
            seen = self._stopped_seen.setdefault(key, now)
            if now - max(seen, tg.last_action_at or 0) > STOPPED_VISIBLE_SECONDS:
                return None
        else:
            self._stopped_seen.pop(key, None)
        if tg.stopped:
            status, detail = "stopped", "Stopped by an operator"
        elif tg.paused:  # the operator's own pause is what they need to see first
            status, detail = "paused", "Paused by an operator"
        elif tg.waiting_reason:
            status, detail = "waiting_for_user", redact_text(tg.waiting_reason)
        else:
            status, detail = "running", None
        created = tg.started_at or time.time()
        account = tg.label if tg.session_path else (Path(settings.TELEGRAM_SESSION_PATH).name or "agent")
        return {
            "id": tg.SESSION_ID,
            "integration": "telegram",
            "key": key,
            "ordinal": ordinal,
            "name": f"Telegram #{ordinal}" if ordinal == 1 and tg.label == "default" else f"Telegram · {tg.label}",
            "subtitle": tg.task_title,
            "profile": None,
            "account": account,
            "status": status,
            "status_detail": detail,
            "created_at": created,
            "elapsed_seconds": max(0, int(time.time() - created)),
            "last_activity_at": tg.last_action_at or created,
            "task": {"id": tg.task_id, "title": tg.task_title} if tg.task_id else None,
            "url": "",
            "title": "",
            "viewport": None,
            "capabilities": self.capabilities.as_dict(),
            "control": None,
            "waiting": {"reason": redact_text(tg.waiting_reason), "since": None, "timed_out": False}
            if tg.waiting_reason
            else None,
            "error": redact_text(tg.last_error) if tg.last_error else None,
            "meta": {
                "connected": tg.is_connected(),
                "actions_today": tg.actions_today(),
                "daily_action_limit": settings.TELEGRAM_USER_DAILY_ACTION_LIMIT,
            },
        }

    @staticmethod
    def _key_for(label: str, tg: Any) -> str:
        """The key this account's card uses (the legacy single account keeps 'agent')."""
        return next((k for k, s in _sessions() if s is tg), None) or "agent"

    async def accounts(self) -> List[Dict[str, Any]]:
        """Every logged-in Telegram account that can be opened, with whether it is open now."""
        from backend.app.integrations import telegram_accounts
        from backend.app.tools.telegram_user_tools import session_for

        out: List[Dict[str, Any]] = []
        for acc in telegram_accounts.list_accounts():
            tg = session_for(acc["label"])
            card = self._card(self._key_for(acc["label"], tg), tg, 0) if tg.ever_used else None
            out.append(
                {
                    "label": acc["label"],
                    "name": acc.get("profile_name"),
                    "username": acc.get("username"),
                    "is_default": bool(acc.get("is_default")),
                    "session_ready": bool(acc.get("session_ready")),
                    "open": card is not None and card["status"] != "stopped",
                }
            )
        return out

    async def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Open a Telegram account in Live Broadcast, the way a browser profile is opened.

        The account does not have to have been used by the agent first. Opening connects to
        Telegram (proving the saved login still works) and clears a previous operator
        stop/pause on this account. Opening an already open account just returns it.
        """
        from backend.app.integrations import telegram_accounts
        from backend.app.live import telegram_chat
        from backend.app.tools.telegram_user_tools import UnknownAccount, session_for

        if not self.configured():
            raise Conflict(
                "Telegram is not enabled. Set TELEGRAM_USER_ENABLED, TELEGRAM_API_ID and TELEGRAM_API_HASH first.",
                "not_configured",
            )
        label = str(payload.get("account") or "").strip()
        account = telegram_accounts.get_account(label or None)
        if account is None:
            raise Conflict(
                "No logged-in Telegram account. Run `python scripts/telegram_login.py` once in a terminal.",
                "no_account",
            )
        if not account.get("session_ready"):
            raise Conflict(
                f"Account '{account['label']}' has no saved login. Run `python scripts/telegram_login.py` again.",
                "not_logged_in",
            )
        try:
            tg = session_for(account["label"])
        except UnknownAccount as exc:
            raise Conflict(str(exc), "no_account")

        was_closed = tg.stopped or not tg.ever_used
        prev_stopped, prev_paused = tg.stopped, tg.paused
        tg.stopped = False  # opening an account is an explicit operator action: it lifts their own stop
        tg.paused = False
        try:
            await telegram_chat._with_client(tg, lambda client: client.get_me())  # connects; proves the login works
        except Exception:
            tg.stopped, tg.paused = prev_stopped, prev_paused  # a failed open changes nothing
            raise
        if not tg.ever_used:
            tg.ever_used = True
            tg.started_at = time.time()
        if was_closed:
            tg._emit("session.started", f"Opened by {payload.get('operator') or 'operator'}", "ok")
        self._stopped_seen.pop(self._key_for(account["label"], tg), None)
        return await self.get_session(self._key_for(account["label"], tg))

    async def chat(self, key: str, op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        from backend.app.live import telegram_chat

        tg = _session_by_key(key)
        if tg is None:
            raise SessionNotFound(key)
        if op == "miniapp":
            return await self._open_miniapp(tg, payload)
        return await telegram_chat.run(tg, op, payload)

    async def _open_miniapp(self, tg: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Open a Mini App button in a browser session (the one the operator picked)."""
        import logging

        from backend.app.live import telegram_chat
        from backend.app.live.providers import PROVIDERS

        profile_id = str(payload.get("profile_id") or "")
        if not profile_id:
            raise Conflict("Choose a browser profile to open the Mini App in.", "no_profile")
        url = await telegram_chat.miniapp_url(tg, payload)  # a credential: stays in this function
        try:
            session = await PROVIDERS["browser"].open_url(
                {"profile_id": profile_id, "url": url, "operator": payload.get("operator")}
            )
        except Conflict as exc:
            raise Conflict(telegram_chat.scrub(str(exc), url), exc.code) from None
        except Exception as exc:  # noqa: BLE001 - the message could carry the link
            logging.getLogger(__name__).error("Mini App open failed (%s)", type(exc).__name__)
            raise Conflict("The browser could not open the Mini App.", "browser_error") from None
        tg._emit("telegram.operator", "Operator opened a Mini App in the browser", "ok")
        return {"session": session}

    async def media(self, key: str, query: Dict[str, Any]):
        from backend.app.live import telegram_chat

        tg = _session_by_key(key)
        if tg is None:
            raise SessionNotFound(key)
        return await telegram_chat.media(
            tg,
            telegram_chat._peer(query.get("peer")),
            int(query.get("message_id") or 0),
            str(query.get("variant") or "preview"),
        )

    async def action(self, key: str, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        tg = _session_by_key(key)
        if tg is None or not tg.ever_used:
            raise SessionNotFound(key)
        who = str(payload.get("operator") or "operator")
        if action == "pause":
            if tg.stopped:
                raise Conflict("The Telegram session is stopped. Resume it first.", "stopped")
            if not tg.paused:  # a repeated click must not log a second pause
                tg.paused = True
                tg._emit("session.paused", f"Paused by {who}", "paused")
        elif action == "resume":
            resumed_from: List[str] = []
            if tg.stopped:
                resumed_from.append("stopped")
            if tg.paused:
                resumed_from.append("paused")
            if tg.waiting_reason:
                resumed_from.append("waiting_for_user")
            tg.paused = tg.stopped = False
            tg.waiting_reason = None  # re-evaluated by the next real action
            if resumed_from:
                tg._emit("session.resumed", f"Resumed by {who}", "ok")
            return {"status": (await self.get_session(key))["status"], "task_id": tg.task_id, "resumed_from": resumed_from}
        elif action == "stop":
            # An account that unfinished tasks are bound to cannot be stopped under them.
            from backend.app.database.session import AsyncSessionLocal
            from backend.app.integrations import telegram_leases

            try:
                async with AsyncSessionLocal() as db:
                    await telegram_leases.assert_free(db, tg.label)
            except telegram_leases.AccountInUse as busy:
                raise Conflict(str(busy), "account_in_use")
            if not tg.stopped:
                self._stopped_seen[key] = time.time()
                tg.stopped = True  # set first: queued agent calls re-check this before they touch the client
                disconnected = await tg.disconnect_quietly()
                tg._emit(
                    "session.stopped",
                    f"Stopped by {who}" + (" and disconnected" if disconnected else "; the disconnect did not complete"),
                    "stopped",
                )
        elif action.startswith("control."):
            raise NotSupported("Telegram API sessions have no screen to control.")
        else:
            raise NotSupported(f"Unknown action '{action}'.")
        return {"status": (await self.get_session(key))["status"], "task_id": tg.task_id}
