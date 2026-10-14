"""Regression tests for the evidence contract and verification (Phase 7 / 10: Test D).

The failure these guard: a tool returned `success: true` with authoritative evidence,
but the controller marked the step FAILED because the evidence had no `kind` (or the
`kind` did not match the step's declared method). A perfectly good action was then
recorded as a failure, and its dependents were never run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.agent.controller import (
    EQUIVALENT_EVIDENCE_KINDS,
    EVIDENCE_KINDS,
    GENERIC_EVIDENCE_METHODS,
    AgentController,
)
from backend.app.planning.planner import PlanStep, StepKind
from backend.app.tools.base import ToolResult, ToolResultStatus


def _verify(evidence: dict, data: dict, method: str) -> bool:
    """Replicate `AgentController._verify_step` for the evidence-envelope path."""
    if not evidence or not evidence.get("authoritative"):
        return False
    kind = str(evidence.get("kind") or "").strip().lower()
    if method in GENERIC_EVIDENCE_METHODS:
        return True
    if method == "unspecified":
        return False
    allowed = EVIDENCE_KINDS.get(method)
    if allowed is None:
        return True
    allowed = allowed | EQUIVALENT_EVIDENCE_KINDS.get(method, frozenset())
    return kind in allowed


# ======================================================================
# TEST D - Evidence contract
# ======================================================================
@pytest.mark.parametrize(
    "kind,method",
    [
        ("membership_verified", "group_joined"),
        ("membership_verified", "membership_verified"),
        ("membership_verified", "membership"),
        ("bot_started", "bot_started"),
        ("message_observed", "message_observed"),
        ("message_observed", "page_observation"),     # cross-integration equivalence
        ("message_observed", "post_action_observation"),
        ("button_clicked", "button_clicked"),
        ("message_sent", "message_sent"),
        ("dialog_listed", "dialog_listed"),
        ("miniapp_launch_detected", "miniapp_opened"),
        ("arithmetic_computed", "arithmetic_computed"),
    ],
)
def test_successful_telegram_evidence_satisfies_its_method(kind, method):
    """A real, proven action must never be recorded as a failure."""
    assert _verify({"authoritative": True, "kind": kind}, {}, method) is True


def test_simulation_evidence_cannot_satisfy_a_receipt_method():
    """Security invariant preserved: a dry run must not prove a broadcast."""
    assert _verify(
        {"authoritative": True, "kind": "eth_call_simulation"}, {}, "chain_receipt"
    ) is False


def test_unspecified_method_is_never_auto_verified():
    """The planner's "nobody said how this is proven" default stays unverified."""
    assert _verify({"authoritative": True, "kind": "bot_started"}, {}, "unspecified") is False


def test_non_authoritative_evidence_is_not_enough():
    assert _verify({"authoritative": False, "kind": "screenshot"}, {}, "page_observation") is False
    assert _verify(None, {}, "tool_evidence") is False


def test_missing_evidence_is_not_a_success():
    """`success: true` alone is never sufficient evidence."""
    controller = AgentController.__new__(AgentController)
    step = PlanStep(index=0, description="x", kind=StepKind.TELEGRAM,
                    verification_method="group_joined")
    result = ToolResult(status=ToolResultStatus.SUCCESS, data={"joined": True})
    task = SimpleNamespace(task_id="AIR-TEST")

    import asyncio
    verified, count = asyncio.run(controller._verify_step(task, step, result))
    assert verified is False, "a successful tool with NO evidence must not count as verified"
    assert count == 0


def _step(index: int, description: str, status: str = "pending", depends_on=None) -> PlanStep:
    return PlanStep(
        index=index,
        description=description,
        kind=StepKind.TELEGRAM,
        tool="telegram_return_to_bot",
        status=status,
        depends_on=list(depends_on or []),
    )


def test_evidence_schema_mismatch_does_not_mark_the_step_failed():
    """The core regression: mismatch = UNVERIFIED (re-runnable), not FAILED.

    `failed` blocked every dependent step forever, which is exactly the reported
    'joins the group, then stops' behaviour.
    """
    from backend.app.planning.planner import ExecutionPlanSpec, _SATISFIED_STATUSES

    assert "unverified" in _SATISFIED_STATUSES, "an unverified action satisfies dependencies"
    assert "failed" not in _SATISFIED_STATUSES, "a hard failure still blocks its dependents"

    spec = ExecutionPlanSpec(
        objective="continue",
        steps=[
            _step(0, "join", status="unverified"),
            _step(1, "return", depends_on=[0]),
        ],
    )
    assert [s.index for s in spec.ready_steps()] == [1], (
        "an unverified step must not strand its dependents"
    )
    assert spec.is_complete() is False, "pending work means the plan is not finished"

    spec.steps[1].status = "completed"
    assert spec.is_complete() is True
    # Completion of the plan does not mean the objective was VERIFIED.
    verified = [s for s in spec.steps if s.status == "completed" and s.tool]
    assert len(verified) == 1
    assert spec.steps[0].status == "unverified", "the join is still unproven"


def test_every_telegram_evidence_kind_has_a_matching_method():
    """Evidence a tool emits must be reachable by the verification methods."""
    from backend.app.tools.telegram_user_tools import TELEGRAM_EVIDENCE_KINDS

    for kind in TELEGRAM_EVIDENCE_KINDS:
        matched = any(kind in allowed for allowed in EVIDENCE_KINDS.values())
        assert matched, f"Telegram evidence kind {kind!r} has no verification method"
