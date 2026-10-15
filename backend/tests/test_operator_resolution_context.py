"""After the operator answers a blocker, later LLM calls still know why it happened and what was said."""

from __future__ import annotations

from backend.app.agent.context import TaskContextService
from backend.app.database.session import session_scope
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus


async def _task(db):
    task = await TaskManager(db).create_task(
        title="PowerPoolX", description="Complete the airdrop", status=TaskStatus.IN_PROGRESS,
        owner_email="op@example.test",
    )
    await db.commit()
    return task


async def test_resolved_blocker_keeps_the_reason_the_reply_and_the_outcome():
    async with session_scope() as db:
        task = await _task(db)
        svc = TaskContextService(db)
        await svc.record_human_intervention(
            task, required=True, reason="Bot asked a verification the agent could not classify",
            step_index=2,
        )
        await svc.record_human_intervention(
            task, required=False, reason="ignored", resolved=True,
            resolution={
                "by": "op@example.test", "method": "operator_provided_information",
                "outcome": "Information supplied by the operator.",
                "operator_reply": "math captcha tumi korte paro, amake bolar dorkar nai",
                "interpretation": "Simple bot challenges may be handled by the agent.",
            },
        )
        state = (await svc.get(task)).human_intervention

    assert state["pending"] is False
    assert "could not classify" in state["summary"]
    assert "math captcha tumi korte paro" in state["summary"]
    assert "RESOLVED" in state["summary"]
    assert state["history"][-1]["step_index"] == 2


async def test_a_new_blocker_does_not_look_resolved_and_history_survives():
    async with session_scope() as db:
        task = await _task(db)
        svc = TaskContextService(db)
        await svc.record_human_intervention(task, required=True, reason="first problem")
        await svc.record_human_intervention(
            task, required=False, reason="x", resolved=True,
            resolution={"operator_reply": "done", "outcome": "Confirmed."},
        )
        await svc.record_human_intervention(task, required=True, reason="second problem")
        state = (await svc.get(task)).human_intervention

    assert state["pending"] is True and state["summary"] is None and state["resolution"] is None
    assert len(state["history"]) == 1 and "first problem" in state["history"][0]["summary"]


async def test_a_secret_in_the_reply_is_not_stored_in_the_context():
    secret = "ab12" * 16
    async with session_scope() as db:
        task = await _task(db)
        svc = TaskContextService(db)
        await svc.record_human_intervention(task, required=True, reason="needs key")
        await svc.record_human_intervention(
            task, required=False, reason="x", resolved=True,
            resolution={"operator_reply": f"use 0x{secret}", "outcome": "ok"},
        )
        state = (await svc.get(task)).human_intervention
    assert secret not in str(state)


async def test_standing_rules_are_kept_deduplicated_and_capped():
    async with session_scope() as db:
        task = await _task(db)
        svc = TaskContextService(db)
        await svc.add_standing_rules(task, ["Simple math questions may be answered", " "])
        await svc.add_standing_rules(task, ["Simple math questions may be answered"] + [f"r{i}" for i in range(12)])
        state = (await svc.get(task)).human_intervention
    assert len(state["standing_rules"]) == 8
    assert state["standing_rules"].count("Simple math questions may be answered") <= 1


async def test_the_digest_and_prompt_context_carry_the_operator_story():
    async with session_scope() as db:
        task = await _task(db)
        svc = TaskContextService(db)
        await svc.record_human_intervention(task, required=True, reason="captcha-like prompt")
        await svc.record_human_intervention(
            task, required=False, reason="x", resolved=True,
            resolution={"operator_reply": "done, i solved it", "outcome": "Confirmed by the operator."},
        )
        await svc.add_standing_rules(task, ["Simple button challenges may be tapped"])
        resolved = await svc.resolve(task)

    digest = resolved.digest()
    assert "OPERATOR ALREADY RESPONDED" in digest and "i solved it" in digest
    assert "RULES THE OPERATOR GAVE" in digest
    prompt = resolved.to_prompt_dict()
    assert "i solved it" in prompt["human_intervention"]["summary"]
