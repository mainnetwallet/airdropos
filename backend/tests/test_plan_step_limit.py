"""A plan may have any number of steps."""

from types import SimpleNamespace

from backend.app.planning.planner import (
    ExecutionPlanSpec, PlanConstraints, PlanStep, Planner, StepKind,
)


def _spec(n):
    steps = [PlanStep(index=i, description=f"step {i}", kind=StepKind.RESEARCH) for i in range(n)]
    return ExecutionPlanSpec(objective="o", steps=steps, constraints={}, rationale="")


def test_no_step_ceiling_by_default():
    assert PlanConstraints().max_steps is None
    assert PlanConstraints().to_dict()["max_steps"] is None


def test_a_large_plan_is_accepted():
    planner = Planner(db=SimpleNamespace())
    spec = _spec(200)
    planner.enforce_constraints(spec, PlanConstraints())
    assert len(spec.steps) == 200
