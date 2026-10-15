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
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from sqlalchemy import select

from backend.app.database.session import AsyncSessionLocal
from backend.app.models import IntegrationHealth

# Logs are shown in the browser, so strip anything that looks like a credential.
# The rules live in the terminal event stream, which writes the same kind of
# record to disk and needs the same guarantee; importing them here keeps one list
# instead of two that drift apart.
from backend.app.terminal.events import redact

logger = logging.getLogger("airdropos.worker.status")

WORKER_SUBSYSTEM = "worker"
HEARTBEAT_INTERVAL_SECONDS = 5
# The API treats the worker as offline when the heartbeat is older than this.
STALE_AFTER_SECONDS = 20
MAX_LOG_LINES = 200
MAX_LOG_MESSAGE_CHARS = 600


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
        # Every task running right now, keyed by public id. The worker runs several at once;
        # ``current_task`` stays as the most recently started one for older dashboard code.
        self.running: Dict[str, Dict[str, Any]] = {}
        self.last_task: Optional[Dict[str, Any]] = None
        self.last_error: Optional[Dict[str, Any]] = None
        self.last_poll_at: Optional[datetime] = None
        self.polls = 0
        self.claimed = 0
        self.finished = 0
        self.note: str = "starting"
        self.log = LogRingHandler()
        # ISO time of the last "Clear" pressed on the dashboard (set by the API).
        self.logs_cleared_at: Optional[str] = None

    # ------------------------------------------------------------------
    def attach_logging(self) -> None:
        root = logging.getLogger()
        if self.log not in root.handlers:
            root.addHandler(self.log)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)

    def clear_logs(self, cleared_at: Optional[str] = None) -> None:
        """Drop every buffered log line (the dashboard's Clear button)."""
        self.log.records.clear()
        if cleared_at:
            self.logs_cleared_at = cleared_at

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
        self.running[task_id] = self.current_task
        self.note = f"running {task_id}"

    def task_finished(self, task_id: str, outcome: str) -> None:
        self.finished += 1
        self.running.pop(task_id, None)
        self.current_task = next(reversed(self.running.values()), None) if self.running else None
        self.last_task = {
            "task_id": task_id,
            "outcome": outcome,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self.note = f"running {len(self.running)} task(s)" if self.running else "idle"

    def failed(self, exc: BaseException, task_id: Optional[str] = None) -> None:
        if task_id:
            self.running.pop(task_id, None)
        self.current_task = next(reversed(self.running.values()), None) if self.running else None
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
            "running_tasks": list(self.running.values()),
            "last_task": self.last_task,
            "last_error": self.last_error,
            "last_poll_at": iso(self.last_poll_at),
            "polls": self.polls,
            "claimed": self.claimed,
            "finished": self.finished,
            "logs": list(self.log.records),
            "logs_cleared_at": self.logs_cleared_at,
        }


state = WorkerState()


async def publish_once() -> None:
    """Write the current snapshot to the heartbeat row."""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        row = await db.scalar(
            select(IntegrationHealth).where(IntegrationHealth.subsystem == WORKER_SUBSYSTEM)
        )
        # The API cannot reach this process directly, so "Clear log" leaves a marker
        # in the heartbeat row. Honour it here, then carry it forward in every publish.
        marker = (row.detail or {}).get("logs_cleared_at") if row is not None else None
        if marker and marker != state.logs_cleared_at:
            state.clear_logs(marker)
        detail = state.snapshot()
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
