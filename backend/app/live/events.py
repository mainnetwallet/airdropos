"""Unified activity-event log.

One model for every integration: event id, session id, integration type,
timestamp, event type, status and a SAFE description. Events are produced by
real code paths only (tool executions, state changes, operator actions); nothing
here invents activity.
"""

from __future__ import annotations

import re
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import urlsplit

MAX_EVENTS = 1500
MAX_MESSAGE = 300


def safe_url(url: Optional[str]) -> str:
    """Strip query string, fragment and credentials: URLs often carry tokens."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except Exception:
        return ""
    if parts.scheme not in ("http", "https"):
        return parts.scheme + ":" if parts.scheme else ""
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 80:
        path = path[:77] + "..."
    return f"{parts.scheme}://{host}{path}"


def _redact(text: str) -> str:
    try:  # reuse the worker's credential redactor when importable
        from backend.app.scheduler.worker_status import redact

        return redact(text)
    except Exception:  # pragma: no cover - defensive
        return re.sub(r"(?i)(token|secret|password|api[_-]?key)\s*[=:]\s*\S+", r"\1=***", text)


def redact_text(text: Optional[str]) -> str:
    """Public wrapper: strip credential-looking values from text shown to dashboard viewers."""
    return _redact(text or "")


class EventLog:
    def __init__(self, capacity: int = MAX_EVENTS) -> None:
        self._events: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0
        # A new epoch tells API-side cursors that the worker restarted.
        self.epoch = uuid.uuid4().hex[:8]

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(
        self,
        *,
        integration: str,
        type: str,
        message: str,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._seq += 1
        event = {
            "seq": self._seq,
            "id": uuid.uuid4().hex[:12],
            "session_id": session_id,
            "integration": integration,
            "at": datetime.now(timezone.utc).isoformat(),
            "type": type,
            "status": status,
            "message": _redact(message)[:MAX_MESSAGE],
        }
        self._events.append(event)
        return event

    def since(self, after: int, *, limit: int = 200, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        out = [e for e in self._events if e["seq"] > after and (session_id is None or e["session_id"] == session_id)]
        # The OLDEST events after the cursor: a caller that advances its cursor to the last one it
        # received must never skip events in between (taking the newest would silently drop them).
        return out[:limit]

    def recent(self, *, limit: int = 100, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        out = [e for e in self._events if session_id is None or e["session_id"] == session_id]
        return out[-limit:]
