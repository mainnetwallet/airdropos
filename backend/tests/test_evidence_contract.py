"""Regression tests for the evidence contract and verification (Phase 7 / 10: Test D).

The failure these guard: a tool returned `success: true` with authoritative evidence,
but the controller marked the step FAILED because the evidence had no `kind` (or the
`kind` did not match the step's declared method). A perfectly good action was then
recorded as a failure, and its dependents were never run.
"""

from __future__ import annotations

import asyncio
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
    """Call the REAL `AgentController._verify_step`.

    This used to be a hand-written replica of the controller's decision. That is a
    standing invitation to the two drifting apart: the replica kept returning `True` for
    an unmapped verification method after the controller was changed to refuse one, so
    the suite would have gone on reporting a behaviour the agent no longer had.
    """
    controller = AgentController.__new__(AgentController)
    step = PlanStep(
        index=0, description="x", kind=StepKind.TELEGRAM, verification_method=method
    )
    result = ToolResult(status=ToolResultStatus.SUCCESS, data=data, evidence=evidence)
    task = SimpleNamespace(task_id="AIR-TEST")

    verified, _count = asyncio.run(controller._verify_step(task, step, result))
    return verified


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


def test_a_pending_transaction_is_not_verified_by_its_own_hash():
    """A tx hash proves submission, never execution.

    `transfer_tools` returns `authoritative: False` when a broadcast is pending
    or the receipt cannot be read, and still includes `tx_hash` in the payload.
    The legacy payload fallback used to rescue exactly that case and mark the
    step verified, so a transfer sitting in the mempool could complete a task
    and be reported as "authoritative evidence" in the task report.
    """
    for evidence in (
        {"authoritative": False, "kind": "chain_receipt", "tx_hash": "0xabc"},
        {"authoritative": False, "kind": "chain_receipt"},
    ):
        for method in ("chain_receipt", "tx_hash", "receipt"):
            assert _verify(evidence, {"tx_hash": "0xabc"}, method) is False, (
                f"pending tx verified via method={method!r}"
            )


def test_a_reverted_receipt_is_not_verified_by_its_hash():
    assert _verify(
        {"authoritative": False, "kind": "chain_receipt", "status": "REVERTED"},
        {"tx_hash": "0xabc", "status": "REVERTED"},
        "chain_receipt",
    ) is False


def test_the_legacy_payload_fallback_still_works_for_genuinely_legacy_tools():
    """A tool from before the evidence envelope must not be broken by the fix.

    The fallback is restricted to results carrying no evidence block at all, so
    an old tool that only ever reported `tx_hash` still verifies.
    """
    assert _verify(None, {"tx_hash": "0xabc"}, "chain_receipt") is True
    assert _verify(None, {"screenshot_path": "/tmp/x.png"}, "page_observation") is True
    assert _verify(None, {"expected_text_seen": True}, "expected_text_seen") is True


def test_a_confirmed_receipt_is_still_verified():
    assert _verify(
        {"authoritative": True, "kind": "chain_receipt", "tx_hash": "0xabc"},
        {"tx_hash": "0xabc", "status": "CONFIRMED"},
        "chain_receipt",
    ) is True


def test_unspecified_method_is_never_auto_verified():
    """The planner's "nobody said how this is proven" default stays unverified."""
    assert _verify({"authoritative": True, "kind": "bot_started"}, {}, "unspecified") is False


def test_an_unrecognised_method_is_not_satisfied_by_any_evidence():
    """An unmapped method is free text the planner invented.

    It previously returned True for any authoritative evidence, which meant a step
    promising something specific could be closed by an unrelated observation. The safe
    default matches `unspecified`: unproven.
    """
    assert _verify(
        {"authoritative": True, "kind": "page_observation"}, {}, "transaction_confirmed"
    ) is False
    assert _verify(
        {"authoritative": True, "kind": "chain_receipt"}, {}, "some_invented_method"
    ) is False


def test_a_recognised_method_still_verifies_against_its_own_evidence():
    """The strict default must not break the methods that DO have a mapping."""
    assert _verify(
        {"authoritative": True, "kind": "chain_receipt"}, {}, "chain_receipt"
    ) is True


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
