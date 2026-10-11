"""Multi-Channel Notification Dispatcher with deduplication.

Telegram and the dashboard share one notification record, so a user does not
receive the same alert twice through different channels.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

from backend.app.core.config import settings

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Channel(str, Enum):
    TELEGRAM = "telegram"
    DASHBOARD = "dashboard"
    DISCORD = "discord"


SEVERITY_EMOJI = {
    Severity.INFO: "\U0001F4E3",
    Severity.SUCCESS: "✅",
    Severity.WARNING: "⚠️",
    Severity.ERROR: "❌",
    Severity.CRITICAL: "\U0001F6A8",
}


class NotificationDispatcher:
    """Sends notifications and tracks what has already been delivered."""

    def __init__(self) -> None:
        self._sent: set[str] = set()
        self._history: List[Dict[str, Any]] = []

    @staticmethod
    def dedupe_key(*parts: Any) -> str:
        """Deterministic key so the same change never alerts twice."""
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    async def notify(
        self,
        title: str,
        body: str,
        severity: Severity = Severity.INFO,
        *,
        channels: Optional[List[Channel]] = None,
        dedupe_key: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Deliver a notification, skipping duplicates."""
        key = dedupe_key or self.dedupe_key(title, task_id or "", datetime.now(timezone.utc).date())
        if key in self._sent:
            logger.debug("Skipping duplicate notification '%s'", title)
            return {"sent": False, "reason": "duplicate", "dedupe_key": key}

        self._sent.add(key)
        target_channels = channels or [Channel.DASHBOARD]
        if settings.TELEGRAM_BOT_TOKEN and Channel.TELEGRAM not in target_channels:
            target_channels.append(Channel.TELEGRAM)

        rendered = f"{SEVERITY_EMOJI.get(severity, '')} *{title}*\n{body}"

        results: Dict[str, Any] = {}
        for channel in target_channels:
            if channel is Channel.TELEGRAM:
                results["telegram"] = await self._send_telegram(rendered)
            elif channel is Channel.DASHBOARD:
                # Persisted in the database by the caller; recorded here for tracing.
                results["dashboard"] = {"status": "queued"}
            elif channel is Channel.DISCORD:
                results["discord"] = {"status": "not_configured"}

        record = {
            "title": title,
            "body": body,
            "severity": severity.value,
            "task_id": task_id,
            "dedupe_key": key,
            "channels": [c.value for c in target_channels],
            "results": results,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        self._history.append(record)
        if len(self._history) > 500:
            self._history = self._history[-500:]
        return record

    async def _send_telegram(self, text: str) -> Dict[str, Any]:
        if not settings.TELEGRAM_BOT_TOKEN:
            return {"status": "not_configured"}
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        recipients = settings.TELEGRAM_AUTHORIZED_USER_IDS
        if not recipients:
            return {"status": "no_authorized_users"}

        sent, errors = 0, []
        async with httpx.AsyncClient(timeout=15) as client:
            for chat_id in recipients:
                try:
                    response = await client.post(
                        url,
                        json={
                            "chat_id": chat_id,
                            "text": text,
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True,
                        },
                    )
                    if response.status_code == 200:
                        sent += 1
                    else:
                        errors.append(f"{chat_id}: HTTP {response.status_code}")
                except Exception as exc:
                    errors.append(f"{chat_id}: {exc}")

        return {"status": "ok" if sent else "failed", "sent": sent, "errors": errors}

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._history[-limit:]


dispatcher = NotificationDispatcher()
