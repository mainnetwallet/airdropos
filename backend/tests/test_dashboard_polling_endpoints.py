"""The dashboard polls /overview and /worker/status every few seconds.

These pin the shape of both answers after they were made cheaper (one grouped task COUNT, and a
short cache around the Telegram readiness check).
"""

import pytest

from backend.app.api.v1.endpoints import system as system_endpoints
from backend.app.tasks.state_machine import TaskStatus
from backend.tests.test_agent_inbox import _client, _make_task, factory, operator_a  # noqa: F401


@pytest.mark.asyncio
async def test_overview_counts_tasks_by_status(factory, operator_a):  # noqa: F811
    await _make_task(factory, status=TaskStatus.IN_PROGRESS)
    await _make_task(factory, status=TaskStatus.COMPLETED)
    await _make_task(factory, status=TaskStatus.FAILED)
    async with _client(operator_a) as client:
        resp = await client.get("/api/v1/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_tasks"] == 1
    assert body["completed_tasks"] == 1
    assert body["failed_tasks"] == 1
    assert body["needs_user_action"] == 0


@pytest.mark.asyncio
async def test_worker_status_reuses_telegram_readiness_briefly(factory, operator_a, monkeypatch):  # noqa: F811
    calls = {"n": 0}

    def fake_readiness():
        calls["n"] += 1
        return {"ready": False, "hint": "test"}

    monkeypatch.setattr(system_endpoints, "_telegram_readiness", fake_readiness)
    system_endpoints._telegram_readiness_memo.update({"at": 0.0, "value": None})
    async with _client(operator_a) as client:
        for _ in range(3):
            resp = await client.get("/api/v1/worker/status")
            assert resp.status_code == 200
    assert calls["n"] == 1
