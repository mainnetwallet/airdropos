"""Replan CONSTRAINTS must not sit in the part of the prompt head/tail compaction cuts.

The real planner, context service and tool registry build the request; the real
`reduce_single_message` (the head/tail reducer compaction uses) reduces it. Only the transport
is faked. The reducer is called directly so the result does not depend on the separate
validation gate's marker heuristic.
"""

import pytest

from backend.app.agent.controller import AgentController
from backend.app.llm.compaction import reduce_single_message
from backend.app.planning.planner import ReplanTrigger
from backend.app.tools import bootstrap

from .test_controller_context_requests import make_task, replan_reply, spec, transport  # noqa: F401
from .test_planner_context_budget import oversized_context

CONSTRAINTS = "CONSTRAINTS (hard limits):"
ALLOWANCES = (13_000, 20_000, 30_000)  # characters the reducer is allowed to keep


@pytest.fixture
def real_registry(monkeypatch):
    """The production tool catalog (dozens of tools), with the global registry restored after.

    `bootstrap_tools` is a one-shot, so the flag is reset to get a populated catalog in every
    test, and the registry's state is snapshotted and put back so nothing leaks to other tests.
    """
    registry = bootstrap.registry
    saved = {k: (v.copy() if isinstance(v, (dict, set, list)) else v) for k, v in vars(registry).items()}
    registry._tools.clear()
    monkeypatch.setattr(bootstrap, "_bootstrapped", False)
    bootstrap.bootstrap_tools()
    assert len(registry.catalog_for_llm()) >= 50, "expected the real, large tool catalog"
    yield registry
    vars(registry).clear()
    vars(registry).update(saved)


def _catalog_chars(prompt):
    """Characters from the tool catalog heading to the end: what the reducer treats as the tail."""
    return len(prompt) - prompt.index("AVAILABLE TOOLS")


async def _replan_prompt(factory, transport, registry, *, with_context):
    transport.replies = [replan_reply("second plan")]
    async with factory() as db:
        task = await make_task(db)
        controller = AgentController(db)
        controller.planner.registry = registry
        first = spec()
        await controller.planner.persist_plan(task, first)
        await controller.context.load_or_create(task)
        context = await oversized_context(db, task, controller) if with_context else None
        await controller.planner.replan(
            task, first, trigger=ReplanTrigger.FAILURE, reason="r", context=context
        )
    return transport.payloads[-1]["messages"][-1]["content"]


def _move_constraints_after_context(prompt):
    """The pre-change layout: CONSTRAINTS between ADDITIONAL CONTEXT and AVAILABLE TOOLS."""
    start = prompt.index(CONSTRAINTS)
    end = prompt.index("ADDITIONAL CONTEXT:")
    block = prompt[start:end]
    without = prompt[:start] + prompt[end:]
    tools = without.index("AVAILABLE TOOLS")
    return without[:tools] + block + "\n" + without[tools:]


async def test_with_context_constraints_precede_the_context_sections(factory, transport, real_registry):
    prompt = await _replan_prompt(factory, transport, real_registry, with_context=True)

    assert prompt.count(CONSTRAINTS) == 1
    assert prompt.index("STEPS THAT FAILED OR REMAIN") < prompt.index(CONSTRAINTS)
    assert prompt.index(CONSTRAINTS) < prompt.index("ADDITIONAL CONTEXT:") < prompt.index("AVAILABLE TOOLS")


async def test_without_context_the_layout_is_unchanged(factory, transport, real_registry):
    prompt = await _replan_prompt(factory, transport, real_registry, with_context=False)

    assert prompt.count(CONSTRAINTS) == 1
    assert "ADDITIONAL CONTEXT" not in prompt and "CRITICAL CONTEXT" not in prompt
    # Still immediately before the tool catalog, exactly as before this change.
    assert prompt.index(CONSTRAINTS) < prompt.index("AVAILABLE TOOLS")
    between = prompt[prompt.index(CONSTRAINTS) : prompt.index("AVAILABLE TOOLS")]
    assert between.endswith("}\n\n") and between.count("\n\n") == 1


@pytest.mark.parametrize("allow", ALLOWANCES)
async def test_constraints_survive_head_tail_reduction_with_the_real_catalog(
    factory, transport, real_registry, allow
):
    prompt = await _replan_prompt(factory, transport, real_registry, with_context=True)
    # Non-vacuous: the catalog really is in the prompt, and it alone is larger than the tail the
    # reducer keeps at any allowance used here.
    assert _catalog_chars(prompt) > 15_000 and len(prompt) > 2 * max(ALLOWANCES)

    reduced, removed, strategy = reduce_single_message(prompt, allow, "replan")

    assert strategy == "head_tail" and removed > 0 and "[COMPACTED" in reduced
    assert CONSTRAINTS in reduced
    assert '"allow_transactions"' in reduced  # the limits themselves, not just their heading


@pytest.mark.parametrize("allow", ALLOWANCES)
async def test_control_the_old_layout_loses_constraints_in_the_same_reduction(
    factory, transport, real_registry, allow
):
    """Proves the test above is not vacuous: this is where the section used to sit."""
    prompt = await _replan_prompt(factory, transport, real_registry, with_context=True)
    assert _catalog_chars(prompt) > 15_000 and len(prompt) > 2 * max(ALLOWANCES)
    old_layout = _move_constraints_after_context(prompt)
    assert old_layout.index("ADDITIONAL CONTEXT:") < old_layout.index(CONSTRAINTS)

    reduced, removed, _ = reduce_single_message(old_layout, allow, "replan")

    assert removed > 0
    assert CONSTRAINTS not in reduced
