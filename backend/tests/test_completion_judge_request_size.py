"""The completion judge's request: how big it can get, and whether omissions are visible."""

import json

from backend.app.agent.context import DIAGNOSTIC_ID_CAP, OLDER_EVIDENCE_LIMIT
from backend.app.agent.controller import AgentController
from backend.app.llm.token_budget import TokenBudget, estimate_text_tokens
from backend.app.models.context import OUTCOME_CONFIRMED, OUTCOME_FAILED
from backend.app.tasks.telemetry import get_llm_traces

from .test_controller_context_requests import (  # noqa: F401  (fixtures + helpers)
    _clean_traces,
    make_task,
    obs,
    spec,
    transport,
    user_payload,
)
from .test_llm_trace_requests import ok_body

JUDGE_RECENT_LIMIT = 60  # mirrors retrieve_observations(recent_limit=60) in the judge


async def _seed_worst_case(db, task, count):
    big = "x" * 5000
    for i in range(count):
        row = obs(
            task,
            i,
            outcome=OUTCOME_CONFIRMED if i % 2 else OUTCOME_FAILED,
            authoritative=bool(i % 2),
            action=f"action-{i}-" + "a" * 500,
            state={"text": big},
        )
        row.error = "e" * 1000
        db.add(row)
    await db.commit()


def _reply():
    return [ok_body(content=json.dumps({"complete": False, "reason": "r", "missing": []}))]


async def _run_judge(factory, transport, count):
    transport.replies = _reply()
    async with factory() as db:
        task = await make_task(db)
        controller = AgentController(db)
        plan = spec()
        await controller.planner.persist_plan(task, plan)
        await controller.context.load_or_create(task)
        await _seed_worst_case(db, task, count)
        await controller._agent_completion_verdict(task, plan)
    (trace,) = [t for t in get_llm_traces(task_id=task.task_id) if t["purpose"] == "completion_verification"]
    request = transport.payloads[0]
    return request, user_payload(request), trace


async def test_judge_request_is_bounded_even_when_every_observation_is_maximal(factory, transport):
    request, payload, _ = await _run_judge(factory, transport, 400)

    # Recent window plus older evidence: a hard ceiling, however long the task has run.
    assert len(payload["what_happened"]) == JUDGE_RECENT_LIMIT + OLDER_EVIDENCE_LIMIT

    # Every clipped field stays clipped (action 200, observed 600, error 300).
    for item in payload["what_happened"]:
        assert len(item["action"]) <= 200
        assert len(item["observed"]) <= 600
        assert len(item["error"]) <= 300

    # The whole request, estimated the way the client estimates it, sits well under the
    # compaction trigger, so the judge is not the call that forces compaction.
    budget = TokenBudget.from_settings()
    estimated = sum(estimate_text_tokens(m["content"]) for m in request["messages"])
    assert estimated < budget.trigger
    assert estimated < budget.trigger * 0.5, "headroom: the judge request should stay a small share"


async def test_judge_reports_omitted_required_history_in_payload_and_trace(factory, transport):
    _, payload, trace = await _run_judge(factory, transport, 400)

    completeness = payload["history_completeness"]
    assert completeness["total_observations"] == 400
    assert completeness["recent_window"] == JUDGE_RECENT_LIMIT
    assert completeness["older_authoritative_or_unresolved_included"] == OLDER_EVIDENCE_LIMIT
    # Every observation here is a required class, so all 280 that did not fit are named.
    assert completeness["older_matching"] == 340
    assert completeness["omitted_required"] == 280
    assert completeness["complete"] is False
    assert 0 < len(completeness["omitted_required_ids"]) <= DIAGNOSTIC_ID_CAP

    # The ids reported as omitted are real rows that really were left out of the request.
    sent = set(trace["source_ids"]["observations"])
    assert len(sent) == JUDGE_RECENT_LIMIT + OLDER_EVIDENCE_LIMIT
    assert not sent & set(completeness["omitted_required_ids"])

    # The trace carries the same fact as a warning, so it is visible without the prompt.
    assert "observation_retrieval_omitted_required=280" in (trace.get("warnings") or [])


async def test_judge_reports_complete_history_when_nothing_was_left_out(factory, transport):
    _, payload, trace = await _run_judge(factory, transport, 30)

    completeness = payload["history_completeness"]
    assert len(payload["what_happened"]) == 30
    assert completeness["omitted_required"] == 0
    assert completeness["omitted_required_ids"] == []
    assert completeness["complete"] is True
    assert not (trace.get("warnings") or [])
