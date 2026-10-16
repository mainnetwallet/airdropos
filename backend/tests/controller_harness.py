"""Deterministic harness for driving `AgentController` end to end without a real model,
a real tool or a real worker.

The point of this file is that the controller's behaviour is decided by three collaborators
-- the planner, the tool registry and the LLM -- and all three can be replaced with
deterministic doubles. Nothing here touches the network, the provider or the filesystem.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.agent.controller import AgentController
from backend.app.database.session import Base
from backend.app.planning.planner import (
    ExecutionPlanSpec,
    PlanStep,
    StepKind,
)
from backend.app.tools.base import SideEffect, ToolResult, ToolResultStatus
from backend.app.tools.timeouts import resolve_timeout
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def step(index: int, description: str, **kw) -> PlanStep:
    return PlanStep(
        index=index,
        description=description,
        kind=kw.pop("kind", StepKind.TELEGRAM),
        tool=kw.pop("tool", "stub_read"),
        parameters=kw.pop("parameters", {}),
        **kw,
    )


def spec(steps: List[PlanStep], objective: str = "do the thing") -> ExecutionPlanSpec:
    return ExecutionPlanSpec(objective=objective, steps=steps)


# ---------------------------------------------------------------------------
# Planner double
# ---------------------------------------------------------------------------

class StubPlanner:
    """A planner that returns plans the test dictates.

    Keeps the controller's own bookkeeping honest -- step statuses are written back into
    the current spec exactly as the real planner persists them -- while letting a test say
    "the next replan produces THIS plan" and see what the controller does with it.
    """

    def __init__(self, db=None) -> None:
        self.current: Optional[ExecutionPlanSpec] = None
        # Each entry is consumed in order by one replan. `None` means "produce the same
        # plan again", which is how a no-change replan is simulated.
        self.replans: List[Optional[List[PlanStep]]] = []
        self.revision = 0
        self.decompositions: List[List[PlanStep]] = []
        self.replan_reasons: List[str] = []
        self.replan_triggers: List[str] = []
        self.db = db

    def bind(self, db) -> "StubPlanner":
        self.db = db
        return self

    async def decompose(self, *, objective, task, constraints, context=None,
                        campaign_context=None) -> ExecutionPlanSpec:
        steps = self.decompositions.pop(0) if self.decompositions else [step(1, "first")]
        return spec(steps, objective=objective)

    async def persist_plan(self, task, plan: ExecutionPlanSpec) -> None:
        self.current = plan
        self.revision += 1
        await self._persist(task, plan, "INITIAL", "")

    async def _persist(self, task, plan, trigger: str, reason: str) -> None:
        """Write a real ExecutionPlan + PlanRevision row.

        The controller counts discovery and recovery replans by querying `PlanRevision`, so a
        stub that keeps that table empty would test nothing about persistence.
        """
        from backend.app.models import ExecutionPlan, PlanRevision

        previous = self.revision
        self.revision += 1
        row = ExecutionPlan(
            task_id=task.id, revision=self.revision, is_current=True,
            objective=plan.objective, steps=[s.to_dict() for s in plan.steps],
        )
        self.db.add(row)
        await self.db.flush()
        self.db.add(
            PlanRevision(
                plan_id=row.id, task_id=task.id, from_revision=previous,
                to_revision=self.revision, trigger_category=trigger, reason=reason[:200],
            )
        )
        await self.db.flush()

    async def current_plan(self, task):
        from sqlalchemy import select as sa_select

        from backend.app.models import ExecutionPlan

        row = await self.db.scalar(
            sa_select(ExecutionPlan)
            .where(ExecutionPlan.task_id == task.id, ExecutionPlan.is_current.is_(True))
            .order_by(ExecutionPlan.revision.desc())
            .limit(1)
        )
        return row

    async def load_plan_spec(self, task) -> Optional[ExecutionPlanSpec]:
        return self.current

    async def update_step_status(self, task, index, status, *, result=None,
                                 failure_reason=None) -> None:
        if self.current is None:
            return
        for s in self.current.steps:
            if s.index == index:
                s.status = status
                if failure_reason is not None:
                    s.failure_reason = failure_reason
                if result is not None:
                    s.result = result
                return

    async def replan(self, task, current, *, trigger, reason, context=None,
                     campaign_context=None, context_notes=None, evidence=None):
        self.replan_reasons.append(reason)
        self.replan_triggers.append(getattr(trigger, "value", str(trigger)))
        steps = self.replans.pop(0) if self.replans else None
        if steps is None:
            # The same steps, re-offered as runnable. This is what a useless replan looks
            # like in practice: the planner restates the same intent, so a step that just
            # failed becomes pending again and the loop keeps trying it.
            steps = [
                PlanStep(
                    index=s.index, description=s.description, kind=s.kind, tool=s.tool,
                    parameters=dict(s.parameters), depends_on=list(s.depends_on),
                    prerequisites=list(s.prerequisites),
                    verification_method=s.verification_method,
                    estimated_cost_usd=s.estimated_cost_usd,
                    status="completed" if s.status == "completed" else "pending",
                )
                for s in current.steps
            ]
        else:
            steps = [
                PlanStep(
                    index=s.index, description=s.description, kind=s.kind, tool=s.tool,
                    parameters=dict(s.parameters), depends_on=list(s.depends_on),
                    prerequisites=list(s.prerequisites),
                    verification_method=s.verification_method,
                    estimated_cost_usd=s.estimated_cost_usd,
                )
                for s in steps
            ]
        self.current = spec(steps, objective=current.objective)
        if self.db is not None:
            await self._persist(task, self.current, getattr(trigger, "value", "INITIAL"), reason)
        return self.current

    async def evaluate_prerequisites(self, task, step):
        return True, ""

    def decide_retry(self, step, *, attempt, failure_category):
        return False, "not retryable in this test"


# ---------------------------------------------------------------------------
# Registry double
# ---------------------------------------------------------------------------

class StubTool:
    def __init__(self, name="stub_read", *, idempotent=True, side_effect=SideEffect.EXTERNAL_READ):
        self.name = name
        self.is_idempotent = idempotent
        self.side_effect = side_effect


class StubRegistry:
    """Records every invocation and answers with whatever the test queued.

    `script` maps a tool name to a callable (or a fixed `ToolResult`) invoked in order.
    Once a tool's script is exhausted, the last answer repeats.
    """

    def __init__(self, script: Optional[Dict[str, Any]] = None) -> None:
        self.script = script or {}
        self.calls: List[str] = []
        # What the orchestrator asked for on each invocation, so a test can assert the
        # request without this harness reimplementing the dispatcher's clamp.
        self.timeouts: List[Dict[str, Any]] = []
        self.policy = None
        self._cursors: Dict[str, int] = {}

    def get(self, name):
        return StubTool(name)

    def catalog_for_llm(self, policy=None):
        return [{"name": n, "description": n, "input_schema": {}, "side_effect": "none"}
                for n in self.script]

    def disable(self, name, reason):
        pass

    async def invoke(
        self,
        name,
        parameters,
        policy=None,
        task_id=None,
        timeout_seconds=None,
        remaining_budget=None,
    ):
        # Mirrors `ToolRegistry.invoke`. The timeout parameters are accepted and recorded
        # rather than enforced, so a test can assert what the orchestrator ASKED for
        # without this harness having to reimplement the clamp.
        self.calls.append(name)
        # Run the REAL policy, not a copy of it. Recording the raw arguments alone left
        # every arithmetic defect in `resolve_timeout` structurally invisible to CI:
        # nothing in the suite ever asked the policy what it decided.
        try:
            effective, why = resolve_timeout(
                self.get(name),
                requested=timeout_seconds,
                remaining_budget=remaining_budget,
            )
        except Exception as exc:  # a policy failure must surface, not be swallowed
            effective, why = None, f"POLICY RAISED {type(exc).__name__}: {exc}"
        self.timeouts.append(
            {
                "tool": name,
                "requested": timeout_seconds,
                "remaining_budget": remaining_budget,
                "effective": effective,
                "why": why,
            }
        )
        entry = self.script.get(name)
        if entry is None:
            return ToolResult.success(data={"ok": True})
        cursor = self._cursors.get(name, 0)
        self._cursors[name] = cursor + 1
        if isinstance(entry, list):
            answer = entry[cursor] if cursor < len(entry) else entry[-1]
        else:
            answer = entry
        # A scripted entry may itself be a callable, so "fail three times then succeed"
        # reads the same whether it is written as a list or as one function.
        if callable(answer):
            answer = answer(cursor, parameters or {})
        return answer


# ---------------------------------------------------------------------------
# Verified / unverified results
# ---------------------------------------------------------------------------

def authoritative(kind="page_observation", **data) -> ToolResult:
    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        data={"page": "ok", **data},
        evidence={"authoritative": True, "kind": kind, "source": "test"},
    )


def not_authoritative(kind="attempted", **data) -> ToolResult:
    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        data={"page": "ok", **data},
        evidence={"authoritative": False, "kind": kind, "source": "test"},
    )


def unevidenced(**data) -> ToolResult:
    """A tool that succeeded and produced no proof at all."""
    return ToolResult(status=ToolResultStatus.SUCCESS, data={"page": "ok", **data})


def failing(category="TOOL_FAILURE", *, retryable=False) -> ToolResult:
    return ToolResult.failure("it broke", category=category, retryable=retryable)


# ---------------------------------------------------------------------------
# Controller assembly
# ---------------------------------------------------------------------------

async def make_task(db, *, title="long task", status=TaskStatus.IN_PROGRESS,
                    max_retries=3) -> Any:
    task = await TaskManager(db).create_task(
        title=title, description="Complete the airdrop", status=status,
        owner_email="op@example.test", max_retries=max_retries,
    )
    await db.commit()
    return task


def build_controller(db, *, planner: StubPlanner, registry: StubRegistry) -> AgentController:
    controller = AgentController(db, registry=registry, policy=None)
    planner.bind(db)
    controller.planner = planner
    return controller