"""The completion judgment must actually run, and the live run log must name what happened."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from backend.app.agent.controller import AgentController
from backend.app.core.runlog import clip, param_keys
from backend.app.database.session import session_scope
from backend.app.llm import client
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus


async def _task(db):
    task = await TaskManager(db).create_task(
        title="PowerPoolX", description="Complete the airdrop", status=TaskStatus.IN_PROGRESS,
        owner_email="op@example.test",
    )
    await db.commit()
    return task


async def test_completion_verdict_runs_and_sends_the_operator_story(monkeypatch):
    # Regression: `json` was never imported at module level, so this call raised NameError
    # and every task that reached finalisation crashed instead of being judged.
    sent = []

    async def fake_complete_json(messages, **_):
        sent.append(messages)
        return {"complete": True, "reason": "bot confirmed", "missing": []}

    monkeypatch.setattr(client.llm, "complete_json", fake_complete_json)
    plan = SimpleNamespace(
        objective="Complete the airdrop",
        steps=[SimpleNamespace(index=1, description="join", tool="telegram_send", status="completed")],
    )
    async with session_scope() as db:
        task = await _task(db)
        controller = AgentController(db)
        await controller.context.record_human_intervention(task, required=True, reason="captcha")
        await controller.context.record_human_intervention(
            task, required=False, reason="x", resolved=True,
            resolution={"operator_reply": "solved it", "outcome": "Confirmed."},
        )
        verdict = await controller._agent_completion_verdict(task, plan)

    assert verdict == (True, "bot confirmed", [])
    assert "solved it" in sent[0][1].content  # the judge was given the operator interventions


async def test_run_log_lines_name_the_task_and_never_print_param_values(caplog):
    caplog.set_level(logging.INFO)
    async with session_scope() as db:
        task = await _task(db)
        controller = AgentController(db)
        await controller._request_operator_input(
            task, iteration=3, error_text="bot asked a question", error_category="HUMAN_VERIFICATION",
            blocking_step="Step 2 (telegram_send): reply",
        )
    text = caplog.text
    assert "PARK task=" in text and task.task_id in text and "iter=3" in text


def test_clip_is_one_line_bounded_and_redacted():
    secret = "ab12" * 16
    out = clip(f"line one\nline two 0x{secret}", 40)
    assert "\n" not in out and len(out) <= 40 and secret not in out


def test_param_keys_returns_names_only():
    assert param_keys({"address": "0xSECRET", "chain": "base"}) == ["address", "chain"]
    assert param_keys(None) == []
