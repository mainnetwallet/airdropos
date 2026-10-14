"""Reproduction of the reported failure: 'agent joins a Telegram group then stops'.

Root cause A (telegram_user_tools._evidence): the evidence envelope carries NO
"kind". The controller's _verify_step only accepts evidence whose `kind` is in
EVIDENCE_KINDS[method]. For a Telegram step the planner writes a concrete method
like "group_joined"/"bot_started", which is NOT in EVIDENCE_KINDS, so
`allowed is None` -> that path returns True. But when the planner writes a
mapped method such as "page_observation" or "post_action_observation" for a
Telegram step (very plausible: reading a bot's reply IS a page observation),
kind="" is not in the allowed set -> verified=False -> the step is marked
"failed" -> its dependents (return to bot, next action) are never ready ->
_run_iteration sees no ready steps -> replan -> task stops.

Root cause B (planner._parse_steps / decompose): max_steps default is 12 but the
PLANNER_SYSTEM_PROMPT says "Keep plans SHORT: at most 6 steps". A Telegram
airdrop workflow (open bot, start, read, join, verify, return, read, act,
verify, notify) needs more than 6, so enforce_constraints raises PlanningError
and the whole task FAILS.
"""

from backend.app.agent.controller import (
    EVIDENCE_KINDS,
    EQUIVALENT_EVIDENCE_KINDS,
)
from backend.app.planning.planner import (
    ExecutionPlanSpec,
    PlanConstraints,
    PlanStep,
    StepKind,
)
from backend.app.tools.telegram_user_tools import _evidence


def test_telegram_join_succeeds_but_is_marked_unverified():
    """A successful join must produce evidence that satisfies verification."""
    evidence = _evidence("membership_verified", chat_id=123)

    assert evidence.get("authoritative") is True
    # The fix: evidence now carries an explicit `kind`.
    assert evidence.get("kind") == "membership_verified"
    assert evidence.get("source") == "telegram_user"
    # No session material may leak into evidence.
    assert "access_hash" not in evidence


def test_telegram_read_step_with_page_observation_method_verifies():
    """The exact scenario that used to stop the task after a successful action."""
    from backend.app.agent.controller import GENERIC_EVIDENCE_METHODS

    method = "page_observation"
    assert method in EVIDENCE_KINDS, "controller maps this method to concrete kinds"

    evidence = _evidence("message_observed", count=3)
    kind = str(evidence.get("kind") or "").strip().lower()

    # Replicate _verify_step's decision for authoritative evidence.
    allowed = EVIDENCE_KINDS[method] | EQUIVALENT_EVIDENCE_KINDS.get(method, frozenset())
    verified = method in GENERIC_EVIDENCE_METHODS or kind in allowed

    assert verified is True, (
        f"declared method {method!r} must be satisfied by telegram evidence kind {kind!r}; "
        "otherwise the step is marked failed and the workflow stops"
    )


def test_telegram_kinds_are_registered_in_controller_evidence_map():
    """Phase 7 requires explicit Telegram evidence kinds."""
    for method in (
        "bot_started",
        "group_joined",
        "membership_verified",
        "message_observed",
        "button_clicked",
    ):
        assert method in EVIDENCE_KINDS, f"{method} has no evidence-kind mapping"


def test_telegram_join_evidence_satisfies_its_own_method():
    """The join tool's evidence must satisfy the join step's verification method."""
    for method in ("group_joined", "membership_verified", "joined", "membership"):
        kind = "membership_verified"
        assert kind in EVIDENCE_KINDS[method], f"{method} not satisfied by {kind}"


def test_plan_constraints_allow_a_multi_step_telegram_workflow():
    """Root cause B: a 10-step Telegram workflow must be plannable (>=10 steps)."""
    constraints = PlanConstraints()
    assert constraints.max_steps >= 10, (
        f"max_steps={constraints.max_steps} is too small for a multi-stage "
        "Telegram airdrop workflow, so enforce_constraints raises PlanningError"
    )


def test_every_step_kind_the_spec_requires_exists():
    """Phase 2.1: the planner must distinguish these categories."""
    required = {
        "research",
        "navigate",          # navigation
        "telegram",
        "browser",
        "form",
        "human_verification",
        "wait",
        "verify",
        "user_intervention",
        "report",
    }
    have = {k.value for k in StepKind}
    missing = required - have
    assert not missing, f"StepKind is missing categories the spec requires: {sorted(missing)}"


def test_human_gated_kinds_are_declared_as_such():
    """A CAPTCHA step must be recognisable as human-gated without parsing text."""
    assert StepKind.HUMAN_VERIFICATION.requires_human is True
    assert StepKind.USER_INTERVENTION.requires_human is True
    assert StepKind.TELEGRAM.requires_human is False
    assert StepKind.RESEARCH.requires_human is False


def test_only_passive_kinds_may_complete_without_a_tool():
    """A tool-less telegram/verify step is a blocker, never a silent success."""
    for kind in (StepKind.ANALYZE, StepKind.NOTIFY, StepKind.REPORT, StepKind.WAIT):
        assert kind.is_passive is True, f"{kind.value} should be passive"
    for kind in (StepKind.TELEGRAM, StepKind.VERIFY, StepKind.BROWSER, StepKind.FORM):
        assert kind.is_passive is False, f"{kind.value} must require a tool"


def test_unknown_step_kind_degrades_to_research_not_transact():
    """A hallucinated or legacy kind must never become an irreversible step."""
    assert StepKind.coerce("something_invented") is StepKind.RESEARCH
    assert StepKind.coerce(None) is StepKind.RESEARCH
    assert StepKind.coerce("telegram") is StepKind.TELEGRAM
    assert StepKind.coerce("  TRANSACT ") is StepKind.TRANSACT


def test_step_has_prerequisites_and_verification_fields():
    step = PlanStep(index=0, description="x", kind=StepKind.TELEGRAM)
    assert hasattr(step, "prerequisites")
    assert hasattr(step, "depends_on")
    assert hasattr(step, "verification_method")
    assert hasattr(step, "expected_evidence")


def test_unverified_step_unblocks_dependents_but_is_not_verified():
    """THE continuation fix: an unverified action must not strand the rest of the plan.

    Before this, `ready_steps` only counted completed/skipped, so a step whose
    evidence did not match blocked every later step forever.
    """
    spec = ExecutionPlanSpec(
        objective="join the group then return to the bot",
        steps=[
            PlanStep(index=0, description="join group", kind=StepKind.TELEGRAM,
                     tool="telegram_join_chat", status="unverified"),
            PlanStep(index=1, description="return to bot", kind=StepKind.TELEGRAM,
                     tool="telegram_return_to_bot", depends_on=[0]),
        ],
    )
    ready = [s.index for s in spec.ready_steps()]
    assert ready == [1], "the dependent step must become ready so the workflow continues"

    # It is NOT complete: real work still remains, so the loop must keep going.
    assert spec.is_complete() is False, (
        "a pending dependent step means the plan is not finished"
    )

    # Now the workflow actually continues and finishes. Nothing is proven, so
    # finalization must not report the objective as verified.
    spec.steps[1].status = "unverified"
    assert spec.is_complete() is True, "no step is left to run"
    verified = [s.index for s in spec.steps if s.status == "completed" and s.tool]
    assert verified == [], "an unverified step must never count as verified evidence"
