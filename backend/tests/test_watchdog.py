"""Tests for the diagnostics watchdog.

The watchdog replaces an endpoint that assembled health by hand: the database
entry was hardcoded `{"status": "ok"}` and never probed, `scheduler` reported the
notification dispatcher rather than the scheduler, and `worker` was hardcoded
`UNKNOWN`. These tests pin the properties that make it an actual check.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from backend.app.diagnostics import watchdog as wd
from backend.app.models import Base, DiagnosticRecord, IntegrationHealth


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003
    return "JSON"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ======================================================================
# Report aggregation
# ======================================================================
def test_healthy_report_is_healthy():
    report = wd.WatchdogReport(
        started_at=None,
        results=[wd.ProbeResult(subsystem="a"), wd.ProbeResult(subsystem="b")],
    )
    assert report.overall == wd.HEALTHY
    assert report.failing() == []


def test_degraded_promotes_over_healthy():
    report = wd.WatchdogReport(
        started_at=None,
        results=[
            wd.ProbeResult(subsystem="a"),
            wd.ProbeResult(subsystem="b", status=wd.DEGRADED),
        ],
    )
    assert report.overall == wd.DEGRADED
    assert [r.subsystem for r in report.failing()] == ["b"]


def test_unavailable_outranks_degraded():
    report = wd.WatchdogReport(
        started_at=None,
        results=[
            wd.ProbeResult(subsystem="a", status=wd.DEGRADED),
            wd.ProbeResult(subsystem="b", status=wd.UNAVAILABLE),
        ],
    )
    assert report.overall == wd.UNAVAILABLE


def test_not_configured_counts_as_degraded_not_healthy():
    """An unconfigured optional integration is not a failure, but is not green."""
    report = wd.WatchdogReport(
        started_at=None,
        results=[wd.ProbeResult(subsystem="telegram", status=wd.NOT_CONFIGURED)],
    )
    assert report.overall == wd.DEGRADED
    assert not report.results[0].ok


def test_probe_result_ok_accepts_lowercase_ok():
    """`IntegrationHealth` rows written by older code use lowercase 'ok'."""
    assert wd.ProbeResult(subsystem="x", status="ok").ok


def test_report_serializes():
    from datetime import datetime, timezone

    report = wd.WatchdogReport(
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        results=[wd.ProbeResult(subsystem="a", detail={"k": "v"}, latency_ms=1.5)],
    )
    payload = report.to_dict()
    assert payload["overall"] == wd.HEALTHY
    assert payload["subsystems"]["a"]["detail"] == {"k": "v"}
    assert payload["checked_at"].startswith("2026-01-01")


# ======================================================================
# Probe isolation
# ======================================================================
@pytest.mark.asyncio
async def test_timed_converts_exception_to_unavailable():
    async def boom():
        raise RuntimeError("probe exploded")

    result = await wd._timed("flaky", boom)
    assert result.status == wd.UNAVAILABLE
    assert result.detail["error"] == "RuntimeError"
    assert result.latency_ms is not None


@pytest.mark.asyncio
async def test_timed_passes_through_healthy():
    async def fine():
        return {"block": 1}

    result = await wd._timed("rpc:x", fine)
    assert result.status == wd.HEALTHY
    assert result.detail["block"] == 1


@pytest.mark.asyncio
async def test_timed_honours_status_override():
    async def degraded():
        return {"_status": wd.DEGRADED, "reason": "slow"}

    result = await wd._timed("slow", degraded)
    assert result.status == wd.DEGRADED
    # The private control key must not leak into persisted detail.
    assert "_status" not in result.detail


@pytest.mark.asyncio
async def test_database_probe_actually_queries(db_session_factory):
    """The old endpoint hardcoded this to ok without touching the database."""
    async with db_session_factory() as db:
        assert await wd.probe_database(db) == {"status": wd.HEALTHY}


@pytest.mark.asyncio
async def test_emergency_stop_probe_reports_without_mutating():
    from backend.app.security.emergency_stop import emergency_stop

    before = emergency_stop.is_stopped
    result = await wd.probe_emergency_stop()
    assert result["stopped"] is before
    # The watchdog must never change the interlock.
    assert emergency_stop.is_stopped is before


# ======================================================================
# Persistence
# ======================================================================
@pytest.mark.asyncio
async def test_sweep_persists_health_and_anomalies(db_session_factory, monkeypatch):
    from sqlalchemy import select

    async def fake_db_probe(_db):
        return {"status": wd.HEALTHY}

    async def fake_broken_probe():
        raise RuntimeError("down")

    monkeypatch.setattr(wd, "probe_llm", fake_broken_probe)
    monkeypatch.setattr(wd, "probe_database", fake_db_probe)
    monkeypatch.setattr(wd, "probe_database_backlog", fake_db_probe)
    monkeypatch.setattr(wd, "probe_browser", fake_db_probe)
    monkeypatch.setattr(wd, "probe_telegram", fake_db_probe)
    monkeypatch.setattr(wd, "probe_tools", fake_db_probe)
    monkeypatch.setattr(wd, "probe_emergency_stop", fake_db_probe)
    monkeypatch.setattr(wd, "probe_rpc", fake_broken_probe)

    watchdog = wd.Watchdog()
    watchdog.networks = ["ethereum"]

    async with db_session_factory() as db:
        report = await watchdog.sweep(db, notify=False)

        assert report.overall == wd.UNAVAILABLE
        assert "llm" in [r.subsystem for r in report.failing()]
        assert "rpc:ethereum" in [r.subsystem for r in report.failing()]

        rows = list((await db.execute(select(IntegrationHealth))).scalars().all())
        subsystems = {r.subsystem: r.status for r in rows}
        assert subsystems["database"] == wd.HEALTHY
        assert subsystems["llm"] == wd.UNAVAILABLE

        anomalies = list((await db.execute(select(DiagnosticRecord))).scalars().all())
        assert anomalies, "anomalies must be recorded for failures"
        assert {a.level for a in anomalies} == {"ERROR"}


@pytest.mark.asyncio
async def test_repeat_sweeps_update_rather_than_duplicate(db_session_factory, monkeypatch):
    from sqlalchemy import select

    async def ok_probe(_db=None):
        return {"status": wd.HEALTHY}

    for name in (
        "probe_database",
        "probe_database_backlog",
        "probe_llm",
        "probe_browser",
        "probe_telegram",
        "probe_tools",
        "probe_emergency_stop",
        "probe_rpc",
    ):
        monkeypatch.setattr(wd, name, ok_probe)

    watchdog = wd.Watchdog()
    watchdog.networks = ["ethereum"]

    async with db_session_factory() as db:
        await watchdog.sweep(db, notify=False)
        await watchdog.sweep(db, notify=False)

        rows = list((await db.execute(select(IntegrationHealth))).scalars().all())
        # One row per subsystem, not one per sweep.
        assert len(rows) == len({r.subsystem for r in rows})
        assert len([r for r in rows if r.subsystem == "database"]) == 1


@pytest.mark.asyncio
async def test_streak_counters_track_transitions_not_sweeps(db_session_factory, monkeypatch):
    """success/failure counts should reflect streaks, not every sweep."""
    from sqlalchemy import select

    state = {"fail": True}

    async def varying(_db=None):
        return {"_status": wd.DEGRADED if state["fail"] else wd.HEALTHY}

    for name in (
        "probe_database_backlog",
        "probe_llm",
        "probe_browser",
        "probe_telegram",
        "probe_tools",
        "probe_emergency_stop",
        "probe_rpc",
    ):
        monkeypatch.setattr(wd, name, varying)

    async def db_ok(_db):
        return {"status": wd.HEALTHY}

    monkeypatch.setattr(wd, "probe_database", db_ok)
    monkeypatch.setattr(wd, "probe_llm", varying)

    watchdog = wd.Watchdog()
    watchdog.networks = []

    async with db_session_factory() as db:
        # Three failing sweeps.
        for _ in range(3):
            await watchdog.sweep(db, notify=False)

        row = await db.scalar(
            select(IntegrationHealth).where(IntegrationHealth.subsystem == "llm")
        )
        assert row.failure_count == 1, "count must increment only on a status transition"
        assert row.success_count == 0

        state["fail"] = False
        await watchdog.sweep(db, notify=False)
        await db.refresh(row)
        assert row.success_count == 1
        assert row.failure_count == 1


@pytest.mark.asyncio
async def test_alerts_carry_a_stable_dedupe_key(db_session_factory, monkeypatch):
    """A repeating condition must reuse one key so the dispatcher can suppress it.

    The dispatcher, not the watchdog, enforces suppression (`key in self._sent`),
    so the contract to pin here is that the watchdog supplies a deterministic key
    for the same subsystem+status+hour, and a different key when the status flips.
    """
    calls: list[str] = []

    class FakeDispatcher:
        async def notify(self, title, body, severity=None, **kwargs):
            calls.append(kwargs.get("dedupe_key", ""))
            return {"sent": True}

    monkeypatch.setattr(
        "backend.app.notifications.dispatcher.dispatcher", FakeDispatcher()
    )

    async def broken(_db=None):
        raise RuntimeError("down")

    for name in (
        "probe_database",
        "probe_database_backlog",
        "probe_llm",
        "probe_browser",
        "probe_telegram",
        "probe_tools",
        "probe_emergency_stop",
        "probe_rpc",
    ):
        monkeypatch.setattr(wd, name, broken)

    watchdog = wd.Watchdog()
    watchdog.networks = []

    async with db_session_factory() as db:
        await watchdog.sweep(db, notify=True)
    first = list(calls)

    calls.clear()
    async with db_session_factory() as db:
        await watchdog.sweep(db, notify=True)
    second = list(calls)

    assert first, "failing probes must raise an alert"
    assert second == first, "the same failure in the same hour must reuse its dedupe key"
    assert all(key for key in first), "every alert needs an explicit dedupe key"


@pytest.mark.asyncio
async def test_alerting_failure_never_breaks_the_sweep(db_session_factory, monkeypatch):
    """A broken notifier must not lose the health data already collected."""

    class BrokenDispatcher:
        async def notify(self, *args, **kwargs):
            raise RuntimeError("telegram down")

    monkeypatch.setattr(
        "backend.app.notifications.dispatcher.dispatcher", BrokenDispatcher()
    )

    async def broken(_db=None):
        raise RuntimeError("down")

    for name in (
        "probe_database",
        "probe_database_backlog",
        "probe_llm",
        "probe_browser",
        "probe_telegram",
        "probe_tools",
        "probe_emergency_stop",
        "probe_rpc",
    ):
        monkeypatch.setattr(wd, name, broken)

    watchdog = wd.Watchdog()
    watchdog.networks = []

    async with db_session_factory() as db:
        report = await watchdog.sweep(db, notify=True)
        assert report.overall == wd.UNAVAILABLE