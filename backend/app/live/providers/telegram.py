"""Telegram (MTProto / Telethon) session.

This is an API session, not a screen: there is NO picture and NO clickable
surface. The card shows genuine activity events and metadata only.
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
    capabilities = Capabilities(visual=False, events=True, pause_resume=True, stop=True, manual_control=False)
    note = (
        "API session (Telethon). It shows real activity events only: there is no graphical screen and no "
        "screen streaming. Each logged-in account is its own session. Stop disconnects that account and blocks the agent until resumed."
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
