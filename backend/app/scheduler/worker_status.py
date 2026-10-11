"""Live worker status.

The worker is a separate process from the API, so the dashboard cannot see what
it is doing. Before this module the worker never reported anything: tasks sat in
READY and nobody could tell whether the worker was dead, idle, or failing.

The worker now:
  * keeps a small in-memory state (current task, counters, last error),
  * keeps the last ~200 log lines in a ring buffer (secrets redacted),
  * publishes both to the ``integration_health`` row ``worker`` every few
    seconds. That row doubles as the heartbeat: a stale ``last_checked_at``
    means the worker is not running.

The API reads this row in ``GET /worker/status``. No new table or migration.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from sqlalchemy import select

from backend.app.database.session import AsyncSessionLocal
from backend.app.models import IntegrationHealth

logger = logging.getLogger("airdropos.worker.status")

WORKER_SUBSYSTEM = "worker"
HEARTBEAT_INTERVAL_SECONDS = 5
# The API treats the worker as offline when the heartbeat is older than this.
STALE_AFTER_SECONDS = 20
MAX_LOG_LINES = 200
MAX_LOG_MESSAGE_CHARS = 600

# Logs are shown in the browser, so strip anything that looks like a credential.
_REDACTIONS = [
    (re.compile(r"(?i)\b(bearer|token|api[_-]?key|secret|password|authorization)(\s*[=:]\s*|\s+)(?:(?:bearer|basic)\s+)?[^\s,;'\"]+"), r"\1\2***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "***"),
    (re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b"), "***"),
    (re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{30,}\b"), "***"),  # Telegram bot tokens
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class LogRingHandler(logging.Handler):
    """Keeps the most recent log records so the dashboard can show them."""

    def __init__(self, capacity: int = MAX_LOG_LINES) -> None:
        super().__init__(level=logging.INFO)
        self.records: Deque[Dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                message = f"{message} | {type(exc).__name__}: {exc}"
            self.records.append(
                {
                    "t": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                    "level": record.levelname,
                    "source": record.name,
                    "message": redact(message)[:MAX_LOG_MESSAGE_CHARS],
                }
            )
        except Exception:
            self.handleError(record)


class WorkerState:
    """Mutable snapshot of what this worker process is doing."""

    def __init__(self) -> None:
        self.worker_id: str = ""
        self.started_at: Optional[datetime] = None
        self.tools: List[str] = []
        self.current_task: Optional[Dict[str, Any]] = None
        self.last_task: Optional[Dict[str, Any]] = None
        self.last_error: Optional[Dict[str, Any]] = None
        self.last_poll_at: Optional[datetime] = None
        self.polls = 0
        self.claimed = 0
        self.finished = 0
        self.note: str = "starting"
        self.log = LogRingHandler()

    # ------------------------------------------------------------------
    def attach_logging(self) -> None:
        root = logging.getLogger()
        if self.log not in root.handlers:
            root.addHandler(self.log)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)

    def begin(self, worker_id: str, tool_names: List[str]) -> None:
        self.worker_id = worker_id
        self.started_at = datetime.now(timezone.utc)
        self.tools = sorted(tool_names)
        self.note = "starting"

    def polled(self, note: str) -> None:
        self.polls += 1
        self.last_poll_at = datetime.now(timezone.utc)
        self.note = note

    def task_claimed(self, task_id: str, title: str) -> None:
        self.claimed += 1
        self.current_task = {
            "task_id": task_id,
            "title": title,
            "since": datetime.now(timezone.utc).isoformat(),
        }
        self.note = f"running {task_id}"

    def task_finished(self, task_id: str, outcome: str) -> None:
        self.finished += 1
        self.current_task = None
        self.last_task = {
            "task_id": task_id,
            "outcome": outcome,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self.note = "idle"

    def failed(self, exc: BaseException, task_id: Optional[str] = None) -> None:
        self.current_task = None
        self.last_error = {
            "at": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "type": type(exc).__name__,
            "message": redact(str(exc))[:400],
        }
        self.note = "error (see last_error)"

    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        def iso(value: Optional[datetime]) -> Optional[str]:
            return value.isoformat() if value else None

        return {
            "worker_id": self.worker_id,
            "started_at": iso(self.started_at),
            "tool_count": len(self.tools),
            "tools": self.tools,
            "telegram_tools": [t for t in self.tools if t.startswith("telegram")],
            "note": self.note,
            "current_task": self.current_task,
            "last_task": self.last_task,
            "last_error": self.last_error,
            "last_poll_at": iso(self.last_poll_at),
            "polls": self.polls,
            "claimed": self.claimed,
            "finished": self.finished,
            "logs": list(self.log.records),
        }


state = WorkerState()


async def publish_once() -> None:
    """Write the current snapshot to the heartbeat row."""
    now = datetime.now(timezone.utc)
    detail = state.snapshot()
    async with AsyncSessionLocal() as db:
        row = await db.scalar(
            select(IntegrationHealth).where(IntegrationHealth.subsystem == WORKER_SUBSYSTEM)
        )
        if row is None:
            db.add(
                IntegrationHealth(
                    subsystem=WORKER_SUBSYSTEM,
                    status="HEALTHY",
                    detail=detail,
                    last_checked_at=now,
                    success_count=1,
                    failure_count=0,
                )
            )
        else:
            row.status = "HEALTHY"
            row.detail = detail
            row.last_checked_at = now
            row.success_count = (row.success_count or 0) + 1
        await db.commit()


async def heartbeat_loop(shutdown_event: asyncio.Event) -> None:
    """Publish status until shutdown. A failure here must never stop the worker."""
    while not shutdown_event.is_set():
        try:
            await publish_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - depends on the database
            logger.warning("Could not publish worker heartbeat: %s", exc)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
