"""Diagnostics Watchdog.

A single place that probes every subsystem, records what it found, and decides
whether the platform is healthy enough to keep acting autonomously.

This exists because subsystem health was previously assembled ad hoc inside the
`/diagnostics` endpoint: the database entry was hardcoded to `{"status": "ok"}`
and never probed, the `scheduler` entry tested the notification dispatcher rather
than the scheduler, and the `worker` entry was hardcoded `UNKNOWN`. The endpoint
was a report, not a check.

Design:

- :class:`Watchdog` runs an ordered list of :class:`Probe` callables. Each probe
  returns a :class:`ProbeResult`; a probe that raises is recorded as a failure
  rather than aborting the sweep, so one broken subsystem cannot hide the state
  of the others.
- Results are upserted into ``IntegrationHealth`` (the same table the dashboard
  reads) and anomalies are appended to ``DiagnosticRecord``.
- Alerting goes through the existing notification dispatcher with an explicit
  ``dedupe_key`` so a repeating condition does not notify every sweep.

The watchdog is read-only with respect to safety controls: it never activates
the emergency stop, never resumes it, and never retries a failed probe against a
different network. Diagnosing is not authorizing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.blockchain.chains import known_networks
from backend.app.models import DiagnosticRecord, IntegrationHealth
from backend.app.tasks.state_machine import TaskStatus

logger = logging.getLogger(__name__)

# Status vocabulary shared with `IntegrationHealth.status`.
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
UNAVAILABLE = "UNAVAILABLE"
NOT_CONFIGURED = "NOT_CONFIGURED"
UNKNOWN = "UNKNOWN"

# `IntegrationHealth.status` values that do not count as a failure.
OK_STATUSES = frozenset({"ok", HEALTHY})


@dataclass
class ProbeResult:
    """The outcome of a single subsystem probe."""

    subsystem: str
    status: str = HEALTHY
    detail: Dict[str, Any] = field(default_factory=dict)
    latency_ms: Optional[float] = None

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES


@dataclass
class WatchdogReport:
    """Aggregated outcome of one full sweep."""

    started_at: datetime
    results: List[ProbeResult] = field(default_factory=list)
    anomalies: List[DiagnosticRecord] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if any(r.status == UNAVAILABLE for r in self.results):
            return UNAVAILABLE
        if any(r.status == DEGRADED for r in self.results):
            return DEGRADED
        if any(r.status == NOT_CONFIGURED for r in self.results):
            return DEGRADED
        return HEALTHY

    def failing(self) -> List[ProbeResult]:
        return [r for r in self.results if not r.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall": self.overall,
            "checked_at": self.started_at.isoformat(),
            "subsystems": {
                r.subsystem: {
                    "status": r.status,
                    "latency_ms": r.latency_ms,
                    "detail": r.detail,
                }
                for r in self.results
            },
            "failing": [r.subsystem for r in self.failing()],
        }


# ----------------------------------------------------------------------
# Probes
# ----------------------------------------------------------------------
async def _timed(subsystem: str, coro_factory: Callable[[], Awaitable[Dict[str, Any]]]) -> ProbeResult:
    """Run a probe, converting any exception into an UNAVAILABLE result."""
    started = time.perf_counter()
    try:
        detail = await coro_factory()
    except Exception as exc:
        latency = (time.perf_counter() - started) * 1000
        logger.warning("Watchdog probe '%s' failed: %s: %s", subsystem, type(exc).__name__, exc)
        return ProbeResult(
            subsystem=subsystem,
            status=UNAVAILABLE,
            detail={"error": type(exc).__name__, "message": str(exc)[:500]},
            latency_ms=round(latency, 2),
        )

    latency = round((time.perf_counter() - started) * 1000, 2)
    status = str(detail.pop("_status", HEALTHY))
    return ProbeResult(subsystem=subsystem, status=status, detail=detail, latency_ms=latency)


async def probe_database(db: AsyncSession) -> Dict[str, Any]:
    """Actually execute a query. The old endpoint hardcoded this to "ok"."""
    from sqlalchemy import text

    await db.execute(text("SELECT 1"))
    return {"status": HEALTHY}


async def probe_llm() -> Dict[str, Any]:
    from backend.app.llm.client import llm

    result = await llm.health_check()
    healthy = bool(result.get("ok", result.get("status") == "ok"))
    detail = {k: v for k, v in result.items() if k != "api_key"}
    detail["_status"] = HEALTHY if healthy else DEGRADED
    return detail


async def probe_rpc(network: str) -> Dict[str, Any]:
    """Query block height for one network."""
    from backend.app.transactions.engine import engine as tx_engine

    url = tx_engine.rpc_url(network)
    if not url:
        return {"_status": NOT_CONFIGURED, "reason": "no RPC URL configured"}
    block = await tx_engine._rpc(network, "eth_blockNumber", [])
    return {"block": int(block, 16)}


async def probe_browser() -> Dict[str, Any]:
    from backend.app.browser.controller import browser_controller

    health = await browser_controller.health()
    available = bool(health.get("available", health.get("status") in ("ok", HEALTHY)))
    return {**health, "_status": HEALTHY if available else DEGRADED}


async def probe_telegram() -> Dict[str, Any]:
    from backend.app.integrations.telegram import telegram_bot

    health = await telegram_bot.health()
    configured = bool(health.get("configured", health.get("status") in ("ok", HEALTHY)))
    return {**health, "_status": HEALTHY if configured else NOT_CONFIGURED}


async def probe_emergency_stop() -> Dict[str, Any]:
    """Report the interlock state. Never changes it."""
    from backend.app.security.emergency_stop import emergency_stop

    stopped = emergency_stop.is_stopped
    return {
        "stopped": stopped,
        "reason": emergency_stop.status().get("reason"),
        "_status": DEGRADED if stopped else HEALTHY,
    }


async def probe_tools() -> Dict[str, Any]:
    from backend.app.tools.registry import registry

    count = len(registry._tools)
    disabled = sorted(registry._disabled)
    return {"registered": count, "disabled": disabled, "_status": HEALTHY}


async def probe_database_backlog(db: AsyncSession) -> Dict[str, Any]:
    """Flag work that has been stuck, which is a silent failure mode."""
    from backend.app.models import Task

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    stuck = await db.scalar(
        select(func.count(Task.id)).where(
            Task.status == TaskStatus.IN_PROGRESS.value,
            Task.execution_started_at < cutoff,
        )
    )
    detail: Dict[str, Any] = {"tasks_in_progress_over_1h": int(stuck or 0)}
    detail["_status"] = DEGRADED if stuck else HEALTHY
    return detail


class Watchdog:
    """Runs every probe and persists the outcome."""

    def __init__(self) -> None:
        # Networks probed each sweep. Derived from the chain catalog rather than
        # re-spelled here: this list was the eighth copy of the same six names.
        self.networks: List[str] = list(known_networks())

    def _core_probes(self, db: AsyncSession) -> List[tuple[str, Callable[[], Awaitable[Dict[str, Any]]]]]:
        return [
            ("database", lambda: probe_database(db)),
            ("database_backlog", lambda: probe_database_backlog(db)),
            ("llm", probe_llm),
            ("browser", probe_browser),
            ("telegram", probe_telegram),
            ("emergency_stop", probe_emergency_stop),
            ("tools", probe_tools),
        ]

    async def sweep(self, db: AsyncSession, *, notify: bool = True) -> WatchdogReport:
        """Probe every subsystem concurrently and persist the results."""
        started = datetime.now(timezone.utc)
        report = WatchdogReport(started_at=started)

        probes = self._core_probes(db)
        probes += [(f"rpc:{n}", lambda n=n: probe_rpc(n)) for n in self.networks]

        gathered = await asyncio.gather(
            *(_timed(name, factory) for name, factory in probes),
            return_exceptions=True,
        )

        for item in gathered:
            if isinstance(item, BaseException):
                # _timed already converts exceptions; this is a backstop.
                report.results.append(
                    ProbeResult(subsystem="unknown", status=UNAVAILABLE, detail={"error": str(item)})
                )
            else:
                report.results.append(item)

        anomalies = await self._persist(db, report)
        report.anomalies = anomalies

        if notify and report.failing():
            await self._alert(report)

        return report

    async def _persist(self, db: AsyncSession, report: WatchdogReport) -> List[DiagnosticRecord]:
        """Upsert IntegrationHealth and append anomalies to DiagnosticRecord."""
        created: List[DiagnosticRecord] = []
        now = datetime.now(timezone.utc)

        for result in report.results:
            row = await db.scalar(
                select(IntegrationHealth).where(IntegrationHealth.subsystem == result.subsystem)
            )
            if row is None:
                db.add(
                    IntegrationHealth(
                        subsystem=result.subsystem,
                        status=result.status,
                        detail=result.detail,
                        latency_ms=result.latency_ms,
                        last_checked_at=now,
                        success_count=1 if result.ok else 0,
                        failure_count=0 if result.ok else 1,
                    )
                )
            else:
                # Only count a success after an actual failure so the counters
                # reflect streaks rather than every sweep.
                if result.ok and row.status not in OK_STATUSES:
                    row.success_count += 1
                elif not result.ok and row.status in OK_STATUSES:
                    row.failure_count += 1
                row.status = result.status
                row.detail = result.detail
                row.latency_ms = result.latency_ms
                row.last_checked_at = now

            if not result.ok:
                record = DiagnosticRecord(
                    subsystem=result.subsystem,
                    level="WARN" if result.status == DEGRADED else "ERROR",
                    message=f"{result.subsystem} reported {result.status}",
                    context_data={
                        "status": result.status,
                        "detail": result.detail,
                        "latency_ms": result.latency_ms,
                    },
                )
                db.add(record)
                created.append(record)

        await db.commit()
        return created

    async def _alert(self, report: WatchdogReport) -> None:
        """Notify through the dispatcher, deduplicated per subsystem per hour."""
        from backend.app.notifications.dispatcher import Severity, dispatcher

        for result in report.failing():
            hour = report.started_at.strftime("%Y%m%d%H")
            try:
                await dispatcher.notify(
                    f"Watchdog: {result.subsystem} is {result.status}",
                    str(result.detail)[:500],
                    Severity.WARNING if result.status == DEGRADED else Severity.ERROR,
                    dedupe_key=f"watchdog-{result.subsystem}-{result.status}-{hour}",
                )
            except Exception:
                logger.exception("Failed to deliver watchdog alert for %s", result.subsystem)


# Process-wide instance used by the scheduler job and the API endpoint.
watchdog = Watchdog()