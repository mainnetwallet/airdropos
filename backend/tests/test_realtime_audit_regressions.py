"""Audit tests: streamed-turn disconnect semantics and atomic task claiming."""

import asyncio
import json

import pytest

from backend.app.llm.client import FixedLLMClient
from backend.app.models import Task
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

from backend.tests.test_chat_loop_requests import body, make_loop


def _reply_with_task():
    return json.dumps(
        {
            "reply": "On it.",
            "tool_calls": [],
            "create_task": {"title": "Do the thing", "description": "d"},
        }
    )


async def test_disconnect_during_final_model_round_creates_no_task(monkeypatch):
    """`should_stop` is documented as 'a stopped turn creates no task'.

    The client leaves while the model is still answering its last round. No later safe
    boundary polls `should_stop`, so the task used to be persisted for a client that left.
    """
    gone = {"value": False}

    async def fake_post(self, path, payload):
        gone["value"] = True  # the browser disconnects mid model call
        return body(_reply_with_task())

    monkeypatch.setattr(FixedLLMClient, "_post", fake_post)
    created = []

    async def create_task(spec):
        created.append(spec)
        return {"task_id": "AIR-X"}, None

    result = await make_loop().run(
        db=None, message="do it", history=[], create_task=create_task,
        should_stop=lambda: gone["value"],
    )
    assert created == [], "a task was persisted after the client disconnected"
    assert result.truncated is True


async def test_connected_client_still_gets_its_task(monkeypatch):
    async def fake_post(self, path, payload):
        return body(_reply_with_task())

    monkeypatch.setattr(FixedLLMClient, "_post", fake_post)
    created = []

    async def create_task(spec):
        created.append(spec)
        return {"task_id": "AIR-X"}, None

    result = await make_loop().run(
        db=None, message="do it", history=[], create_task=create_task,
        should_stop=lambda: False,
    )
    assert len(created) == 1
    assert result.task == {"task_id": "AIR-X"}


async def _ready(factory, n):
    async with factory() as db:
        mgr = TaskManager(db)
        for _ in range(n):
            await mgr.create_task("t", status=TaskStatus.READY)
        await db.commit()


async def test_two_workers_cannot_claim_the_same_task(factory, monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "MAX_CONCURRENT_TASKS", 5)
    await _ready(factory, 1)

    async def claim(worker):
        async with factory() as db:
            task = await TaskManager(db).claim_next_task(worker)
            await db.commit()
            return task.task_id if task else None

    results = await asyncio.gather(claim("w1"), claim("w2"))
    assert sorted(r for r in results if r) and len([r for r in results if r]) == 1


async def test_claim_respects_execution_limit(factory, monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "MAX_CONCURRENT_TASKS", 1)
    await _ready(factory, 2)
    async with factory() as db:
        first = await TaskManager(db).claim_next_task("w1")
        await db.commit()
    async with factory() as db:
        second = await TaskManager(db).claim_next_task("w2")
    assert first is not None and second is None
