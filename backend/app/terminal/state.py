"""Collect the live state the dashboard renders.

Every value here is read from a source the platform already maintains:

* **Backend / frontend / gateway** - a real probe of the thing being reported.
  The API is asked for its health endpoint; the frontend and the live gateway
  are connected to, because a listening socket is the only evidence that either
  exists. Nothing is inferred from "this process is running".
* **Worker** - the ``integration_health`` row the worker republishes every few
  seconds. A stale timestamp is exactly how the API decides the worker is gone,
  so the dashboard cannot disagree with it.
* **Task counts, scheduler jobs, job outcomes** - the same tables the API reads.
* **Emergency stop** - the shared state file every process honours.
* **Events and alerts** - the structured stream in
  :mod:`backend.app.terminal.events`, not the terminal.

A probe that fails is reported as failed. A probe that cannot run is reported as
``unknown``. Neither is ever dressed up as healthy.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from backend.app.terminal.catalog import ToolInventory
from backend.app.terminal.theme import DOT_ERROR, DOT_INFO, DOT_SUCCESS, MUTED

# How long a service may be unreachable after startup before it is reported as
# stopped rather than starting. Anything slower than this is a real failure.
STARTUP_GRACE_SECONDS = 20.0

PROBE_TIMEOUT_SECONDS = 1.5

RUNNING = "running"
STARTING = "starting"
STOPPED = "stopped"
ERROR = "error"
DISABLED = "disabled"
UNKNOWN = "unknown"

_LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "SUCCESS": 2, "WARNING": 3, "ERROR": 4, "CRITICAL": 4}

_JOB_LABELS = {
    "airdrop_discovery": "Airdrop Discovery",
    "campaign_monitoring": "Campaign Monitoring",
    "claim_monitoring": "Claim Monitoring",
    "receipt_monitoring": "Receipt Monitoring",
    "deadline_alerts": "Deadline Alerts",
    "rpc_health_check": "RPC Health Check",
    "watchdog": "Watchdog",
    "memory_maintenance": "Memory Maintenance",
    "learning_analysis": "Learning Analysis",
}


# ----------------------------------------------------------------------
# Value objects
# ----------------------------------------------------------------------
@dataclass
class ServiceState:
    name: str
    state: str = UNKNOWN
    detail: Tuple[str, ...] = ()
    note: str = ""


@dataclass
class JobView:
    key: str
    label: str
    interval_minutes: Optional[int]
    active: Optional[bool]
    marker: str
    marker_style: str
    detail: str = ""


@dataclass
class TaskCounts:
    running: int = 0
    pending: int = 0
    completed: int = 0
    failed: int = 0
    other: int = 0
    known: bool = False

    @property
    def total(self) -> int:
        return self.running + self.pending + self.completed + self.failed + self.other


# How many lines each log panel keeps. Past this, the oldest line is dropped as
# each new one arrives, so the panel always holds the newest HISTORY_LIMIT lines.
HISTORY_LIMIT = 150


@dataclass
class EventLine:
    ts: float
    time: str
    level: str
    source: str
    proc: str
    message: str
    seq: int = 0

    @property
    def rank(self) -> int:
        return _LEVEL_RANK.get(self.level, 1)

    @property
    def is_alert(self) -> bool:
        return self.rank >= 3


@dataclass
class RuntimeState:
    started_at: float
    version: str = ""
    environment: str = ""
    model: str = ""
    endpoint: str = ""
    log_level: str = ""
    log_path: str = ""

    backend: ServiceState = field(default_factory=lambda: ServiceState("Backend API"))
    frontend: ServiceState = field(default_factory=lambda: ServiceState("Frontend (Vite)"))
    worker: ServiceState = field(default_factory=lambda: ServiceState("Agent Worker"))
    gateway: ServiceState = field(default_factory=lambda: ServiceState("Live Gateway"))

    auth_enabled: Optional[bool] = None
    emergency_stop: Dict[str, Any] = field(default_factory=dict)
    emergency_stop_error: str = ""

    tools: ToolInventory = field(default_factory=ToolInventory)
    jobs: List[JobView] = field(default_factory=list)
    scheduler_known: bool = False
    tasks: TaskCounts = field(default_factory=TaskCounts)

    events: Deque[EventLine] = field(default_factory=lambda: deque(maxlen=HISTORY_LIMIT))
    # Warning/error records kept apart from the activity feed, so a burst of
    # routine INFO lines can never push an error out of its own panel.
    alert_events: Deque[EventLine] = field(default_factory=lambda: deque(maxlen=HISTORY_LIMIT))
    # True once a Collector owns alert_events. Then an empty alert_events means
    # "cleared", not "not tracked", and alerts must not be rebuilt from events.
    alerts_tracked: bool = False
    # Short confirmation ("Copied 150 lines") shown for a few seconds after a key.
    notice: str = ""
    problems: List[str] = field(default_factory=list)

    worker_id: str = ""
    worker_note: str = ""

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    @property
    def alerts(self) -> List[EventLine]:
        """Everything currently wrong: real warning/error records, newest first.

        Probe failures that produce no log record of their own (an unreachable
        database, an emergency-stop state that cannot be read) are added here
        too, so a genuinely broken deployment can never render an empty alert
        panel.
        """
        source = self.alert_events if self.alerts_tracked else self.events
        found = [event for event in source if event.is_alert]
        now = time.time()
        for problem in self.problems:
            found.append(
                EventLine(
                    ts=now,
                    time=datetime.now().strftime("%H:%M:%S"),
                    level="ERROR",
                    source="dashboard",
                    proc="dashboard",
                    message=problem,
                )
            )
        found.sort(key=lambda event: event.ts, reverse=True)
        return found[:HISTORY_LIMIT]

    @property
    def everything_operational(self) -> bool:
        return not self.alerts and all(
            service.state in (RUNNING, DISABLED)
            for service in (self.backend, self.frontend, self.worker, self.gateway)
        )


# ----------------------------------------------------------------------
# Event stream reader
# ----------------------------------------------------------------------
class EventTail:
    """Reads the JSON-lines event stream from the beginning of the run.

    The launcher truncates the file once, before any service starts, so reading
    from offset 0 yields exactly this run's history and nothing older. Records
    are read in whole lines only: a partially written final line is held back
    until the rest of it arrives, so the feed never shows half a message.
    """

    def __init__(self, path: Any) -> None:
        self.path = path
        self.offset = 0
        self.pending = ""

    def poll(self, limit: int = 400) -> List[EventLine]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:  # truncated or replaced: start again
            self.offset = 0
            self.pending = ""
        if size == self.offset:
            return []
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(size - self.offset)
        except OSError:
            return []
        self.offset += len(chunk)
        pieces = (self.pending + chunk.decode("utf-8", errors="replace")).split("\n")
        self.pending = pieces.pop()  # incomplete tail
        events: List[EventLine] = []
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            try:
                data = json.loads(piece)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            events.append(
                EventLine(
                    ts=float(data.get("ts") or 0.0),
                    time=str(data.get("t") or ""),
                    level=str(data.get("level") or "INFO").upper(),
                    source=str(data.get("source") or ""),
                    proc=str(data.get("proc") or ""),
                    message=str(data.get("message") or ""),
                )
            )
        return events[-limit:]


# ----------------------------------------------------------------------
# Low-level probes
# ----------------------------------------------------------------------
def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_get(url: str, timeout: float) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost URL built from CLI flags
        body = response.read(64_000)
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        payload = {}
    return int(response.status), payload if isinstance(payload, dict) else {}


def _host_port(url: str, default_port: int) -> Tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"http://{url}")
    return parsed.hostname or "127.0.0.1", parsed.port or default_port


def _age_seconds(value: Any) -> Optional[float]:
    if not isinstance(value, datetime):
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - aware).total_seconds()


# Winsock/errno values that describe an unreachable service. The default
# ``str(exception)`` is a full sentence that fills a whole panel for no extra
# information.
_UNREACHABLE = {
    10061: "connection refused",
    10060: "timed out",
    111: "connection refused",
    113: "no route to host",
}


def _short(exc: BaseException) -> str:
    """One short phrase for why a probe could not reach its target."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, BaseException):
        reason = getattr(reason, "reason", reason)
    code = getattr(reason, "winerror", None) or getattr(reason, "errno", None)
    if isinstance(code, int) and code in _UNREACHABLE:
        return _UNREACHABLE[code]
    if isinstance(reason, socket.timeout):
        return "timed out"
    text = str(exc).strip() or type(exc).__name__
    if text.startswith("["):
        text = text.split("]")[-1].strip()
    if "<urlopen error" in text:
        text = text.split("<urlopen error")[-1].lstrip(" [")
    return text[:60]


def _display_path(path: Any) -> str:
    try:
        return str(path.relative_to(os.getcwd())).replace("\\", "/")
    except (ValueError, OSError):
        return str(path).replace("\\", "/")


def _display_url(url: str) -> str:
    """Loopback addresses are shown as ``localhost``.

    Same host, and two characters shorter, which is the difference between a
    service address that fits on its card and one that does not.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = "localhost" if parsed.hostname in ("127.0.0.1", "::1", "0.0.0.0") else (parsed.hostname or "")
    if parsed.port:
        host = f"{host}:{parsed.port}"
    prefix = parsed.scheme or "http"
    return f"{prefix}://{host}"


async def _read(query):
    """Run one read in a short-lived session. Returns None if the DB is unusable."""
    from backend.app.database.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            return await query(db)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


# ----------------------------------------------------------------------
# Collector
# ----------------------------------------------------------------------
class Collector:
    """Builds a :class:`RuntimeState`. Safe to run every couple of seconds."""

    def __init__(
        self,
        *,
        started_at: float,
        backend_url: str,
        api_prefix: str,
        frontend_url: Optional[str],
        gateway_host: str,
        gateway_port: int,
    ) -> None:
        self.started_at = started_at
        self.backend_url = backend_url.rstrip("/")
        self.api_prefix = api_prefix
        self.frontend_url = frontend_url
        self.gateway_host = gateway_host
        self.gateway_port = gateway_port
        self.tools = ToolInventory()
        self.settings = None
        self._identity_loaded = False
        from backend.app.terminal.events import EVENT_LOG

        self.tail = EventTail(EVENT_LOG)
        # A fresh RuntimeState is built on every reading, so the history has to
        # live here or each reading would show only the last couple of seconds.
        self.activity: Deque[EventLine] = deque(maxlen=HISTORY_LIMIT)
        self.alert_log: Deque[EventLine] = deque(maxlen=HISTORY_LIMIT)
        self._seq = 0

    # ------------------------------------------------------------------
    def clear_activity(self) -> int:
        """Empty the RECENT ACTIVITY buffer. Returns how many lines were dropped."""
        dropped = len(self.activity)
        self.activity.clear()
        return dropped

    def clear_alerts(self) -> int:
        """Empty the WARNINGS & ERRORS buffer. Returns how many lines were dropped."""
        dropped = len(self.alert_log)
        self.alert_log.clear()
        return dropped

    # ------------------------------------------------------------------
    def _uptime(self) -> float:
        return time.time() - self.started_at

    def _late(self, service: ServiceState, reason: str = "") -> ServiceState:
        """Starting while the run is young, stopped once it is not."""
        service.note = reason
        service.state = STARTING if self._uptime() < STARTUP_GRACE_SECONDS else STOPPED
        return service

    # ------------------------------------------------------------------
    async def collect(self) -> RuntimeState:
        state = RuntimeState(started_at=self.started_at)
        self._load_identity(state)
        for event in self.tail.poll(400):
            self._seq += 1
            event.seq = self._seq
            self.activity.append(event)
            if event.is_alert:
                self.alert_log.append(event)
        state.events = deque(self.activity, maxlen=HISTORY_LIMIT)
        state.alert_events = deque(self.alert_log, maxlen=HISTORY_LIMIT)
        state.alerts_tracked = True

        loop = asyncio.get_running_loop()
        pending = [
            asyncio.create_task(self._probe_worker(state)),
            asyncio.create_task(self._probe_tasks(state)),
            asyncio.create_task(self._probe_scheduler(state)),
            asyncio.create_task(self._probe_stop(state)),
        ]
        try:
            state.backend, state.frontend, state.gateway = await asyncio.gather(
                loop.run_in_executor(None, self._probe_backend),
                loop.run_in_executor(None, self._probe_frontend),
                loop.run_in_executor(None, self._probe_gateway),
            )
            await asyncio.gather(*pending)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - the dashboard must never die
            state.problems.append(f"state collection failed: {type(exc).__name__}: {exc}")
        finally:
            for task in pending:
                task.cancel()

        state.tools = self.tools
        for service in (state.backend, state.frontend, state.worker, state.gateway):
            # A service that is down is an alert in its own right. It produces no
            # log record, so without this a broken deployment could still render
            # an empty "no warnings or errors" panel.
            if service.state in (STOPPED, ERROR):
                reason = f" ({service.note})" if service.note else ""
                state.problems.append(f"{service.name} is {service.state.lower()}{reason}")
        return state

    # ------------------------------------------------------------------
    def _load_identity(self, state: RuntimeState) -> None:
        """Configuration and the tool registry. Loaded once; both are static."""
        if self._identity_loaded:
            state.version = self._version
            state.environment = self._environment
            state.model = self._model
            state.endpoint = self._endpoint
            state.log_level = self._log_level
            state.auth_enabled = self._auth_enabled
            state.log_path = self._log_path
            return

        self._version = ""
        self._environment = ""
        self._model = ""
        self._endpoint = ""
        self._log_level = ""
        self._auth_enabled: Optional[bool] = None
        self._log_path = ""

        try:
            import backend.app

            self._version = str(getattr(backend.app, "__version__", "") or "1.0.0")
        except Exception:  # pragma: no cover
            self._version = "1.0.0"
        try:
            from backend.app.core.config import settings

            self.settings = settings
            self._environment = str(settings.ENVIRONMENT)
            self._model = str(settings.LLM_MODEL_ID)
            self._endpoint = str(settings.LLM_BASE_URL)
            self._log_level = str(settings.LOG_LEVEL)
            self._auth_enabled = bool(settings.AUTH_ENABLED)
        except Exception as exc:  # pragma: no cover
            state.problems.append(f"configuration unreadable: {type(exc).__name__}: {exc}")
        try:
            from backend.app.terminal.catalog import build_inventory

            self.tools = build_inventory()
        except Exception as exc:  # pragma: no cover
            self.tools = ToolInventory(loaded=False, detail=f"{type(exc).__name__}: {exc}")
        try:
            from backend.app.terminal.events import DIAG_LOG

            self._log_path = _display_path(DIAG_LOG)
        except Exception:  # pragma: no cover
            self._log_path = "logs/airdropos.log"

        self._identity_loaded = True
        state.version = self._version
        state.environment = self._environment
        state.model = self._model
        state.endpoint = self._endpoint
        state.log_level = self._log_level
        state.auth_enabled = self._auth_enabled
        state.log_path = self._log_path

    # ------------------------------------------------------------------
    def _probe_backend(self) -> ServiceState:
        service = ServiceState("Backend API", detail=(_display_url(self.backend_url), "(docs: /docs)"))
        try:
            status, payload = _http_get(
                f"{self.backend_url}{self.api_prefix}/health", PROBE_TIMEOUT_SECONDS
            )
        except urllib.error.HTTPError as exc:
            service.state = ERROR
            service.note = f"HTTP {exc.code}"
            return service
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return self._late(service, _short(exc))
        if status != 200:
            service.state = ERROR
            service.note = f"HTTP {status}"
            return service
        if payload.get("status") != "ok":
            service.state = ERROR
            service.note = f"reported '{payload.get('status')}'"
            return service
        service.state = RUNNING
        return service

    def _probe_frontend(self) -> ServiceState:
        if not self.frontend_url:
            service = ServiceState("Frontend (Vite)", state=DISABLED)
            service.note = "--no-frontend"
            return service
        service = ServiceState("Frontend (Vite)", detail=(_display_url(self.frontend_url),))
        host, port = _host_port(self.frontend_url, 5173)
        try:
            reachable = _port_open(host, port)
        except OSError as exc:  # pragma: no cover - create_connection swallows its own errors
            service.state = ERROR
            service.note = _short(exc)
            return service
        if not reachable:
            return self._late(service)
        service.state = RUNNING
        return service

    def _probe_gateway(self) -> ServiceState:
        address = f"{self.gateway_host}:{self.gateway_port}"
        service = ServiceState("Live Gateway", detail=(address,))
        if self.settings is not None:
            try:
                if not bool(self.settings.LIVE_GATEWAY_ENABLED):
                    service.state = DISABLED
                    service.note = "LIVE_GATEWAY_ENABLED=false"
                    return service
            except Exception:  # pragma: no cover
                pass
        try:
            reachable = _port_open(self.gateway_host, self.gateway_port)
        except OSError as exc:  # pragma: no cover
            service.state = ERROR
            service.note = _short(exc)
            return service
        if not reachable:
            return self._late(service)
        service.state = RUNNING
        return service

    # ------------------------------------------------------------------
    async def _probe_worker(self, state: RuntimeState) -> None:
        from sqlalchemy import select

        from backend.app.database.session import AsyncSessionLocal
        from backend.app.models import IntegrationHealth
        from backend.app.scheduler.worker_status import STALE_AFTER_SECONDS, WORKER_SUBSYSTEM

        async def query(db):
            return await db.scalar(
                select(IntegrationHealth).where(IntegrationHealth.subsystem == WORKER_SUBSYSTEM)
            )

        row = await _read(query)
        service = state.worker
        if row is None:
            self._late(service, "no heartbeat recorded")
            return
        detail = dict(getattr(row, "detail", None) or {})
        state.worker_id = str(detail.get("worker_id") or "")
        state.worker_note = str(detail.get("note") or "")
        age = _age_seconds(getattr(row, "last_checked_at", None))
        if age is None:
            service.state = STOPPED
            service.note = "heartbeat never timestamped"
            return
        if age > STALE_AFTER_SECONDS:
            service.state = STOPPED
            service.note = f"last heartbeat {int(age)}s ago"
            return
        service.state = RUNNING
        service.detail = (state.worker_id or "worker", state.worker_note or "idle")
        last_error = detail.get("last_error")
        if isinstance(last_error, dict) and last_error.get("message"):
            state.problems.append(
                f"worker last error ({last_error.get('type', 'error')}): {last_error['message']}"
            )

    async def _probe_tasks(self, state: RuntimeState) -> None:
        from sqlalchemy import func, select

        from backend.app.models import Task

        async def query(db):
            return (await db.execute(select(Task.status, func.count(Task.id)).group_by(Task.status))).all()

        rows = await _read(query)
        if rows is None:
            state.problems.append("database unreachable: task counts unavailable")
            return
        state.tasks = _tally_tasks({str(name): int(total) for name, total in rows})
        state.tasks.known = True

    async def _probe_scheduler(self, state: RuntimeState) -> None:
        from sqlalchemy import select

        from backend.app.models import AutomationSchedule, ScheduledRun
        from backend.app.scheduler.scheduler import DEFAULT_JOBS

        async def schedules(db):
            return (
                await db.execute(select(AutomationSchedule).order_by(AutomationSchedule.job_name))
            ).scalars().all()

        async def runs(db):
            return (
                await db.execute(
                    select(ScheduledRun.schedule_id, ScheduledRun.status).order_by(
                        ScheduledRun.created_at.desc()
                    )
                )
            ).all()

        rows = await _read(schedules)
        if rows is None:
            state.jobs = [_pending_job(name, interval) for name, _, interval in DEFAULT_JOBS]
            state.scheduler_known = False
            return
        recent = await _read(runs) or []
        last_runs: Dict[str, str] = {}
        for schedule_id, run_status in recent:
            last_runs.setdefault(str(schedule_id), str(run_status))

        by_name = {str(row.job_name): row for row in rows}
        ordered = [name for name, _, _ in DEFAULT_JOBS] + sorted(set(by_name) - {n for n, _, _ in DEFAULT_JOBS})
        defaults = {name: interval for name, _, interval in DEFAULT_JOBS}

        jobs: List[JobView] = []
        for key in ordered:
            row = by_name.get(key)
            if row is None:
                jobs.append(_pending_job(key, defaults.get(key)))
                continue
            marker, style = _job_marker(last_runs.get(str(row.id)), row.last_run_at)
            notes = []
            if row.failure_count:
                notes.append(f"{row.failure_count} failed")
            if not row.is_active:
                notes.append("paused")
            if row.interval_minutes is None:
                notes.append(str(row.cron_expression or "no interval"))
            jobs.append(
                JobView(
                    key=key,
                    label=_job_label(key),
                    interval_minutes=row.interval_minutes,
                    active=bool(row.is_active),
                    marker=marker,
                    marker_style=style,
                    detail=" · ".join(notes),
                )
            )
        state.jobs = jobs
        state.scheduler_known = True

    async def _probe_stop(self, state: RuntimeState) -> None:
        try:
            from backend.app.security.emergency_stop import emergency_stop

            state.emergency_stop = dict(emergency_stop.status())
        except Exception as exc:
            state.emergency_stop = {}
            state.emergency_stop_error = f"{type(exc).__name__}: {exc}"


# ----------------------------------------------------------------------
# Folding helpers
# ----------------------------------------------------------------------
_RUNNING_STATES = {"IN_PROGRESS", "PLANNING", "RESEARCHING", "RETRYING"}
_PENDING_STATES = {"CREATED", "READY", "WAITING", "NEEDS_USER_ACTION"}
_COMPLETED_STATES = {"COMPLETED", "PARTIALLY_COMPLETED"}
_FAILED_STATES = {"FAILED"}


def _tally_tasks(tally: Dict[str, int]) -> TaskCounts:
    """Fold the task state machine onto the four dashboard counters.

    Every status is counted and anything outside the four buckets is kept in
    ``other``, so the numbers on screen always add up to what is in the database
    instead of quietly losing a state.
    """
    counts = TaskCounts()
    for status_name, total in tally.items():
        if status_name in _RUNNING_STATES:
            counts.running += total
        elif status_name in _PENDING_STATES:
            counts.pending += total
        elif status_name in _COMPLETED_STATES:
            counts.completed += total
        elif status_name in _FAILED_STATES:
            counts.failed += total
        else:
            counts.other += total
    return counts


def _job_marker(run_status: Optional[str], last_run: Any) -> Tuple[str, str]:
    """Marker for a scheduler job, taken only from what was actually recorded."""
    if run_status is None and last_run is None:
        return ("·", MUTED)
    if run_status in ("FAILED", "CANCELLED"):
        return (DOT_ERROR, "#f87171")
    if run_status == "RUNNING":
        return (DOT_INFO, "#22d3ee")
    return (DOT_SUCCESS, "#4ade80")


def _pending_job(key: str, interval: Optional[int]) -> JobView:
    """A configured job with no persisted state yet: shown, but never as healthy."""
    return JobView(
        key=key,
        label=_job_label(key),
        interval_minutes=interval,
        active=None,
        marker="·",
        marker_style=MUTED,
        detail="not persisted yet",
    )


def _job_label(key: str) -> str:
    return _JOB_LABELS.get(key) or key.replace("_", " ").strip().title()


__all__ = [
    "Collector",
    "EventLine",
    "JobView",
    "RuntimeState",
    "ServiceState",
    "TaskCounts",
]