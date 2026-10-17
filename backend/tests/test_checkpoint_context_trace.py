"""Checkpoints record which context and plan row they were taken against.

Diagnostic fields only: resume still rebuilds its state from the database rows (covered in
test_controller_context_requests). Real controller, planner, context service and checkpoint
table; only the LLM transport is faked.
"""

from backend.app.agent.controller import AgentController
from backend.app.planning.planner import ReplanTrigger

from .test_controller_context_requests import make_task, replan_reply, spec, transport  # noqa: F401


async def _latest(controller, task):
    latest = await controller.checkpoints.load_latest(task)
    return latest.state["observations"]


async def test_checkpoint_names_the_context_generation_and_plan_row(factory, transport):
    async with factory() as db:
        task = await make_task(db)
        controller = AgentController(db)
        await controller.planner.persist_plan(task, spec())
        await controller.context.load_or_create(task)
        await db.commit()
        expected = await controller.context.context_version(task)
        plan = await controller.planner.current_plan(task)

        await controller._checkpoint(task, 1, "iteration_1")
        saved = await _latest(controller, task)

    assert saved["context_generation"] == expected.generation
    assert saved["task_context_revision"] == expected.task_context_revision
    assert saved["plan_id"] == str(plan.id)
    assert saved["plan_revision"] == 1


async def test_a_replan_moves_the_checkpointed_plan_row_and_generation(factory, transport):
    transport.replies = [replan_reply("replacement")]
    async with factory() as db:
        task = await make_task(db)
        controller = AgentController(db)
        first = spec()
        await controller.planner.persist_plan(task, first)
        await controller.context.load_or_create(task)
        await controller._checkpoint(task, 1, "iteration_1")
        before = await _latest(controller, task)

        await controller.planner.replan(task, first, trigger=ReplanTrigger.FAILURE, reason="x")
        await controller._checkpoint(task, 2, "replanned_failure")
        after = await _latest(controller, task)

    assert (before["plan_revision"], after["plan_revision"]) == (1, 2)
    assert after["plan_id"] != before["plan_id"]
    assert after["context_generation"] != before["context_generation"]


async def test_a_context_version_failure_does_not_cost_the_checkpoint(factory, transport, monkeypatch):
    async with factory() as db:
        task = await make_task(db)
        controller = AgentController(db)
        await controller.planner.persist_plan(task, spec())
        await controller.context.load_or_create(task)

        async def broken(_task):
            raise RuntimeError("version unavailable")

        monkeypatch.setattr(controller.context, "context_version", broken)
        await controller._checkpoint(task, 3, "iteration_3")
        saved = await _latest(controller, task)

    assert saved["plan_revision"] == 1 and saved["plan_id"]
    assert "context_generation" not in saved and "task_context_revision" not in saved
