"""Tests for the live worker status: heartbeat, log redaction, and the API view.

Before this existed the worker never reported anything, so a dead or failing
worker looked identical to a healthy idle one: tasks just sat in READY.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models import IntegrationHealth, Task
from backend.app.scheduler import worker_status
from backend.app.scheduler.worker_status import (
    STALE_AFTER_SECONDS,
    LogRingHandler,
    WorkerState,
    redact,
)

# Reuse the real-auth fixtures (in-memory database, session factory, HTTP client).
from backend.tests.test_api_auth import (  # noqa: F401
    _make_user,
    anyio_backend,
    client,
    db_session_factory,
)
from backend.app.security.auth import Scope


# ----------------------------------------------------------------------
# Redaction and log buffer
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 36,
        "sk-" + "b" * 32,
        "0x" + "c" * 64,
        "123456789:" + "d" * 35,
    ],
)
def test_redact_hides_credentials(secret):
    assert secret not in redact(f"calling api with {secret} now")


def test_redact_hides_key_value_secrets():
    assert "hunter2" not in redact("login password=hunter2 ok")
    assert "abc123" not in redact("Authorization: Bearer abc123")


def test_redact_leaves_normal_text_alone():
    text = "Task AIR-2026-000002 finished: needs_user_action"
    assert redact(text) == text


def test_log_ring_keeps_recent_lines_and_redacts():
    handler = LogRingHandler(capacity=3)
    log = logging.getLogger("worker_status_test")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        for i in range(5):
            log.info("line %s", i)
        log.error("key %s", "sk-" + "z" * 30)
    finally:
        log.removeHandler(handler)

    messages = [r["message"] for r in handler.records]
    assert len(messages) == 3
    assert messages[0] == "line 3"
    assert "z" * 30 not in messages[-1]


def test_state_tracks_claim_finish_and_error():
    state = WorkerState()
    state.begin("worker-1", ["web_fetch", "telegram_open_bot"])
    state.task_claimed("AIR-2026-000002", "Join bot")
    assert state.snapshot()["current_task"]["task_id"] == "AIR-2026-000002"

    state.task_finished("AIR-2026-000002", "needs_user_action")
    snap = state.snapshot()
    assert snap["current_task"] is None
    assert snap["last_task"]["outcome"] == "needs_user_action"
    assert snap["telegram_tools"] == ["telegram_open_bot"]

    state.failed(ValueError("boom " + "sk-" + "q" * 30), "AIR-2026-000002")
    assert "q" * 30 not in state.snapshot()["last_error"]["message"]


# ----------------------------------------------------------------------
# API view
# ----------------------------------------------------------------------
async def _auth_headers(factory):
    token, _ = await _make_user(factory, scopes={Scope.READ.value})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_status_reports_never_started_worker(client, db_session_factory):
    headers = await _auth_headers(db_session_factory)
    body = (await client.get("/api/v1/worker/status", headers=headers)).json()
    assert body["alive"] is False
    assert body["heartbeat_age_seconds"] is None
    assert any("never reported" in p for p in body["problems"])


@pytest.mark.asyncio
async def test_status_reports_live_worker_and_queue(client, db_session_factory, monkeypatch):
    monkeypatch.setattr(worker_status, "AsyncSessionLocal", db_session_factory)
    fresh = WorkerState()
    fresh.begin("worker-9", ["web_fetch"])
    fresh.polled("idle: no runnable task")
    monkeypatch.setattr(worker_status, "state", fresh)

    await worker_status.publish_once()

    async with db_session_factory() as db:
        db.add(Task(task_id="AIR-2026-000001", title="t", status="READY"))
        await db.commit()

    headers = await _auth_headers(db_session_factory)
    body = (await client.get("/api/v1/worker/status", headers=headers)).json()
    assert body["alive"] is True
    assert body["worker"]["worker_id"] == "worker-9"
    assert body["queue"]["ready"] == 1
    assert body["problems"] == []


@pytest.mark.asyncio
async def test_stale_heartbeat_means_offline(client, db_session_factory, monkeypatch):
    monkeypatch.setattr(worker_status, "AsyncSessionLocal", db_session_factory)
    await worker_status.publish_once()

    async with db_session_factory() as db:
        row = await db.scalar(
            __import__("sqlalchemy").select(IntegrationHealth).where(
                IntegrationHealth.subsystem == worker_status.WORKER_SUBSYSTEM
            )
        )
        row.last_checked_at = datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_SECONDS + 60)
        await db.commit()

    headers = await _auth_headers(db_session_factory)
    body = (await client.get("/api/v1/worker/status", headers=headers)).json()
    assert body["alive"] is False
    assert any("not running" in p for p in body["problems"])

    diag = (await client.get("/api/v1/diagnostics", headers=headers)).json()
    assert diag["subsystems"]["worker"]["status"] == "OFFLINE"
