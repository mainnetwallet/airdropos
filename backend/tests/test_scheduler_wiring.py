"""Tests for scheduler job wiring.

The worker previously bound handlers with a ternary:

    handler = handle_rpc_health if job_type == "RPC_HEALTH" else handle_airdrop_discovery

so six of the eight default jobs -- campaign monitoring, claim monitoring,
receipt monitoring, deadline alerts, memory maintenance, and learning analysis
-- silently re-ran airdrop discovery instead of their own work. Memory was never
consolidated and the learning engine never ran, while `ScheduledRun` rows
reported SUCCESS.
"""

from __future__ import annotations

import inspect

import pytest

from backend.app.scheduler import worker
from backend.app.scheduler.scheduler import DEFAULT_JOBS


def test_every_default_job_type_has_a_handler():
    """No job may fall through to an unrelated handler."""
    unmapped = [job for job in DEFAULT_JOBS if job[1] not in worker.JOB_HANDLERS]
    assert not unmapped, f"job types without a handler: {unmapped}"


def test_handlers_are_distinct_per_job_type():
    """Guards against two job types sharing one handler by accident."""
    by_handler: dict = {}
    for name, job_type, _interval in DEFAULT_JOBS:
        by_handler.setdefault(worker.JOB_HANDLERS[job_type], []).append(name)

    shared = {h.__name__: names for h, names in by_handler.items() if len(names) > 1}
    assert not shared, f"one handler bound to multiple jobs: {shared}"


def test_no_ternary_routing_remains():
    """The routing expression itself must not reappear."""
    source = inspect.getsource(worker)
    assert "if job_type ==" not in source, (
        "job routing must use the JOB_HANDLERS mapping, not a conditional"
    )


def test_memory_maintenance_calls_consolidate():
    """The job that was silently running discovery must now do its own work."""
    source = inspect.getsource(worker.handle_memory_maintenance)
    assert "consolidate" in source
    assert "discovery" not in source


def test_learning_analysis_calls_the_learning_engine():
    source = inspect.getsource(worker.handle_learning_analysis)
    assert "LearningEngine" in source
    assert "discovery" not in source


def test_receipt_monitoring_only_observes():
    """Reconciliation must never resubmit anything.

    Checked against the compiled code object rather than the source text, so a
    docstring that legitimately says "submitted" does not trip the assertion.
    """
    import dis

    source = inspect.getsource(worker.handle_receipt_monitoring)
    assert "reconcile_transactions" in source

    names = {
        instr.argval
        for instr in dis.get_instructions(worker.handle_receipt_monitoring)
        if instr.opname in ("LOAD_GLOBAL", "LOAD_METHOD", "LOAD_ATTR")
    }
    assert "reconcile_transactions" in names
    for forbidden in ("submit_raw", "send_raw_transaction", "sign_transaction"):
        assert forbidden not in names, f"receipt monitoring must not call {forbidden}"


def test_watchdog_job_is_registered():
    assert any(job_type == "WATCHDOG" for _, job_type, _ in DEFAULT_JOBS)
    assert "WATCHDOG" in worker.JOB_HANDLERS


@pytest.mark.asyncio
async def test_watchdog_handler_returns_a_report():
    """The job handler must produce a real sweep, not a stub."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.pool import StaticPool

    from backend.app.models import Base, IntegrationHealth

    @compiles(JSONB, "sqlite")
    def _render_jsonb_as_json(type_, compiler, **kw):  # noqa: ANN001, ANN003
        return "JSON"

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as db:
            report = await worker.handle_watchdog(db, {})
            assert "overall" in report
            assert "subsystems" in report

            rows = list((await db.execute(select(IntegrationHealth))).scalars().all())
            assert rows, "the watchdog must persist subsystem health"
    finally:
        await engine.dispose()