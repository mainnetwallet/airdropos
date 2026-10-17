"""Regression tests: planner requests that are too big for the context window.

The planner embeds the resolved task context as JSON in the middle of a prose message. When such
a message must be reduced, compaction keeps the head and tail of prose and drops the middle, which
is where outstanding requirements and verified facts used to sit. These tests drive the REAL
planner, the REAL context service and the REAL compactor (head/tail reduction and its validation
gate); only the HTTP transport is faked.
"""

import json
import re

import pytest

from backend.app.agent.controller import AgentController
from backend.app.core.config import settings
from backend.app.llm.client import Message
from backend.app.llm.compaction import ContextCompactor
from backend.app.llm.token_budget import TokenBudget
from backend.app.models import TaskRequirement
from backend.app.models.context import OUTCOME_EXECUTED, REQUIREMENT_DISCOVERED
from backend.app.planning.planner import (
    CRITICAL_MAX_FACTS,
    CRITICAL_MAX_REQUIREMENTS,
    CRITICAL_TEXT_CLIP,
    ReplanTrigger,
    _critical_context_block,
)
from backend.app.tasks.telemetry import get_llm_traces, reset_llm_traces

from .test_controller_context_requests import make_task, obs, replan_reply, spec, transport  # noqa: F401
from .test_llm_trace_requests import ok_body

REQUIREMENT = "MUST-KEEP-REQUIREMENT accept the terms before joining"
PROVED_FACTS = 40
NEWEST_PROOF = f"proved-{PROVED_FACTS - 1}-"
OLDEST_PROOF = "proved-0-"


@pytest.fixture(autouse=True)
def _clean_traces():
    reset_llm_traces()
    yield
    reset_llm_traces()


def small_window(monkeypatch):
    """A window the oversized request cannot fit, with a head large enough for a digest."""
    monkeypatch.setattr(settings, "LLM_CONTEXT_WINDOW_TOKENS", 20000)
    monkeypatch.setattr(settings, "LLM_CONTEXT_RESERVED_OUTPUT_TOKENS", 1000)
    monkeypatch.setattr(settings, "LLM_CONTEXT_SAFETY_MARGIN_TOKENS", 500)
    monkeypatch.setattr(settings, "LLM_CONTEXT_COMPACTION_TRIGGER_TOKENS", 9000)
    monkeypatch.setattr(settings, "LLM_CONTEXT_COMPACTION_TARGET_TOKENS", 6000)


def deterministic_compactor():
    budget = TokenBudget(
        context_window=20000, reserved_output=1000, safety_margin=500,
        usable_input=18500, trigger=9000, target=6000,
    )
    return ContextCompactor(budget=budget, message_factory=lambda role, content: Message(role, content))


async def oversized_context(db, task, controller):
    """Real rows -> the real resolved context: one open requirement and a lot of proof."""
    db.add(TaskRequirement(
        task_id=task.id, text=REQUIREMENT, status=REQUIREMENT_DISCOVERED, origin="observation", source_ref="x",
    ))
    for i in range(PROVED_FACTS):
        db.add(obs(task, i, authoritative=True, action=f"proved-{i}-" + "x" * 400, state={"page": "y" * 300}))
    for i in range(PROVED_FACTS, PROVED_FACTS * 2):
        db.add(obs(task, i, outcome=OUTCOME_EXECUTED, action=f"unverified-{i}-" + "z" * 400))
    await db.commit()
    context, _campaign = await controller._resolved_planning_context(task)
    return context


def decomposition_reply():
    return ok_body(json.dumps({
        "rationale": "r",
        "steps": [{"description": "Open the channel", "kind": "navigate", "tool": None}],
    }))


def strip_digest(text):
    """The same message as before this change: no digest block."""
    stripped = re.sub(r"\n\nCRITICAL CONTEXT[^\n]*\n[^\n]*", "", text, count=1)
    assert stripped != text, "the request carried no digest to strip"
    return stripped


# --------------------------------------------------------------------------- unit: the digest
def test_digest_is_empty_when_the_context_has_nothing_to_protect():
    assert _critical_context_block(None) == ""
    assert _critical_context_block({}) == ""
    assert _critical_context_block({"goal": "x", "unrelated": [1, 2]}) == ""
    # A revision number alone is not worth a block.
    assert _critical_context_block({"plan_revision": 3, "context_generation": "g1"}) == ""


def test_digest_lists_requirements_and_newest_proof_first_with_revision():
    context = {
        "plan_revision": 4,
        "context_generation": "gen-7",
        "outstanding_requirements": [{"text": "accept the terms"}, {"text": "verify the email"}],
        "verified_facts": [
            {"action": "oldest-proof", "tool": "t", "iteration": 1},
            {"action": "newest-proof", "tool": "t", "iteration": 9},
        ],
    }
    block = _critical_context_block(context)
    payload = json.loads(block.split(":\n", 1)[1])

    assert payload["plan_revision"] == 4 and payload["context_generation"] == "gen-7"
    assert payload["outstanding_requirements"] == ["accept the terms", "verify the email"]
    assert [f["action"] for f in payload["verified_facts_newest_first"]] == ["newest-proof", "oldest-proof"]
    assert "ADDITIONAL CONTEXT" in block  # says where the full record is


def test_digest_is_bounded_and_counts_what_it_leaves_out():
    context = {
        "outstanding_requirements": [{"text": f"req-{i} " + "w" * 1000} for i in range(CRITICAL_MAX_REQUIREMENTS + 5)],
        "verified_facts": [{"action": f"fact-{i}", "tool": "t", "iteration": i} for i in range(CRITICAL_MAX_FACTS + 7)],
    }
    block = _critical_context_block(context)
    payload = json.loads(block.split(":\n", 1)[1])

    assert len(payload["outstanding_requirements"]) == CRITICAL_MAX_REQUIREMENTS
    assert payload["outstanding_requirements_not_listed_here"] == 5
    assert all(len(text) <= CRITICAL_TEXT_CLIP for text in payload["outstanding_requirements"])
    assert len(payload["verified_facts_newest_first"]) == CRITICAL_MAX_FACTS
    assert payload["verified_facts_not_listed_here"] == 7
    # Newest first, so what is left out is the OLDEST proof.
    assert payload["verified_facts_newest_first"][0]["action"] == f"fact-{CRITICAL_MAX_FACTS + 6}"
    assert len(block) < 8000


# ----------------------------------------------------------- prompt content is otherwise unchanged
async def test_full_context_is_still_sent_in_full_and_no_context_means_no_digest(factory, transport):
    transport.replies = [replan_reply("second plan"), replan_reply("third plan")]
    async with factory() as db:
        task = await make_task(db)
        controller = AgentController(db)
        first = spec()
        await controller.planner.persist_plan(task, first)
        await controller.context.load_or_create(task)
        context = await oversized_context(db, task, controller)
        await controller.planner.replan(task, first, trigger=ReplanTrigger.FAILURE, reason="r", context=context)
        await controller.planner.replan(task, first, trigger=ReplanTrigger.FAILURE, reason="r2")  # no context

    with_context = transport.payloads[0]["messages"][-1]["content"]
    without_context = transport.payloads[1]["messages"][-1]["content"]

    # Not under the trigger's reach here only because the default window is large: the request is
    # sent whole, including every proved fact in ADDITIONAL CONTEXT.
    assert "ADDITIONAL CONTEXT:" in with_context
    # The newest proof is in the digest AND the full record; the oldest only in the full record
    # (the digest lists the newest few and counts the rest).
    assert with_context.count(NEWEST_PROOF) == 2
    assert with_context.count(OLDEST_PROOF) == 1
    assert "CRITICAL CONTEXT" not in without_context


# ------------------------------------------------- the actual hazard, and the fix, both planners
async def _replan_message(factory, transport):
    transport.replies = [replan_reply("second plan")]
    async with factory() as db:
        task = await make_task(db)
        controller = AgentController(db)
        first = spec()
        await controller.planner.persist_plan(task, first)
        await controller.context.load_or_create(task)
        context = await oversized_context(db, task, controller)
        await controller.planner.replan(task, first, trigger=ReplanTrigger.FAILURE, reason="r", context=context)
    return transport.payloads[-1]["messages"], task.task_id


async def _decompose_message(factory, transport):
    transport.replies = [decomposition_reply()]
    async with factory() as db:
        task = await make_task(db)
        controller = AgentController(db)
        await controller.context.load_or_create(task)
        context = await oversized_context(db, task, controller)
        await controller.planner.decompose("Join the Example channel", task=task, context=context)
    return transport.payloads[-1]["messages"], task.task_id


@pytest.mark.parametrize("build", [_replan_message, _decompose_message], ids=["replan", "decompose"])
async def test_requirements_and_newest_proof_survive_head_tail_reduction(factory, transport, build):
    sent, _task_id = await build(factory, transport)
    original = [Message(m["role"], m["content"]) for m in sent]

    reduced = deterministic_compactor().preflight(original, aggressive=True)  # includes the validation gate
    text = reduced.messages[-1].content

    assert reduced.report.characters_removed > 0 and "head_tail" in " ".join(reduced.report.strategies)
    assert len(text) < len(original[-1].content)
    assert REQUIREMENT in text
    assert NEWEST_PROOF in text  # the newest proof survives, not just the oldest
    assert '"plan_revision"' in text or "PLAN REVISION BEING REPLACED" in text
    assert "[COMPACTED" in text  # the cut is announced


@pytest.mark.parametrize("build", [_replan_message, _decompose_message], ids=["replan", "decompose"])
async def test_without_the_digest_the_same_reduction_loses_the_requirement(factory, transport, build):
    """Control: proves the test above is not vacuous. This is the behaviour before the change."""
    sent, _task_id = await build(factory, transport)
    before_change = [Message(m["role"], m["content"]) for m in sent]
    before_change[-1] = Message("user", strip_digest(before_change[-1].content))

    # Reduce what the old prompt looked like. Depending on where the cut falls the validation gate
    # may refuse the request outright; both outcomes are a failure to carry the requirement.
    from backend.app.llm.compaction import ContextCompactionError

    try:
        text = deterministic_compactor().preflight(before_change, aggressive=True).messages[-1].content
    except ContextCompactionError:
        return
    assert REQUIREMENT not in text or NEWEST_PROOF not in text


# ---------------------------------------------------- through the real client, end to end
@pytest.mark.parametrize(
    ("build", "purpose"), [(_replan_message, "replan"), (_decompose_message, "plan")], ids=["replan", "decompose"]
)
async def test_oversized_planner_request_through_the_real_client(factory, transport, monkeypatch, build, purpose):
    small_window(monkeypatch)
    sent, task_id = await build(factory, transport)

    # The model-driven summary is unusable here (the scripted reply is not a summary), so the
    # deterministic head/tail reduction is what produced the request that was finally sent.
    final = sent[-1]["content"]
    assert REQUIREMENT in final and NEWEST_PROOF in final
    assert "[COMPACTED" in final

    (trace,) = [t for t in get_llm_traces(task_id=task_id) if t["purpose"] == purpose]
    assert trace["compaction"]["characters_removed"] > 0
    assert any("head_tail" in strategy for strategy in trace["compaction"]["strategies"])
    assert "preflight_over_trigger_compacted" in trace["warnings"]
    assert trace["outcome"] == "ok"
