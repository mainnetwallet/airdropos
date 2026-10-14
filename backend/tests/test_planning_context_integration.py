"""Initial planning receives the resolved task and campaign context.

The planner always *accepted* `context=` and `campaign_context=`, but the initial
`Planner.decompose()` call in the controller passed neither: a task was planned from
its objective string alone, even though the system had already persisted the goal, the
verified facts, the failed attempts, the newly discovered requirements and (where one
exists) the campaign's shared facts. Replanning was in the same position.

These tests pin the integration and its boundaries:

* the initial decomposition receives the *resolved* task context, source-labelled, with
  verified facts kept distinct from unverified observations and failed attempts;
* campaign context is attached only when the task is genuinely linked to a campaign;
* unrelated campaign data is never injected, and one task never receives another
  task's execution history;
* the replan path receives the same context, so newly discovered requirements and
  evidence stay available;
* resolving twice in one planning cycle does not re-read the same rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.agent.context import TaskContextService
from backend.app.agent.controller import AgentController, LoopOutcome, LoopReport
from backend.app.airdrops.campaign_context import CampaignContextService
from backend.app.memory.manager import MemoryManager
from backend.app.models import (
    AirdropCampaign,
    Base,
    Project,
    Task,
)
from backend.app.models.context import (
    OUTCOME_CONFIRMED,
    OUTCOME_FAILED,
    OUTCOME_UNVERIFIED,
)
from backend.app.models.memory import SCOPE_CAMPAIGN
from backend.app.planning.planner import (
    ExecutionPlanSpec,
    PlanConstraints,
    PlanStep,
    ReplanTrigger,
    StepKind,
)


# ======================================================================
# Harness
# ======================================================================
@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    made = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield made
    await engine.dispose()


async def _task(factory, title: str, *, status: str = "IN_PROGRESS") -> str:
    async with factory() as db:
        task = Task(
            task_id=f"AIR-2026-{abs(hash(title)) % 900000 + 100000:06d}",
            title=title,
            description=f"{title} (full description)",
            status=status,
            owner_email="operator@test.local",
        )
        db.add(task)
        await db.commit()
        return task.id


async def _campaign(factory, title: str) -> str:
    async with factory() as db:
        project = Project(name=f"p-{uuid.uuid4().hex[:8]}", slug=f"s-{uuid.uuid4().hex[:8]}")
        db.add(project)
        await db.flush()
        campaign = AirdropCampaign(
            project_id=project.id,
            title=title,
            lifecycle_status="RESEARCHING",
            requirements={"tasks": ["join the channel"]},
            reward_info={"token": "TKN"},
            risk_factors=[],
            source_reliability=0.9,
            last_checked_at=datetime.now(timezone.utc),
        )
        db.add(campaign)
        await db.commit()
        return campaign.id


async def _load(factory, task_id):
    async with factory() as db:
        return await db.get(Task, task_id)


def _spec(objective: str) -> ExecutionPlanSpec:
    return ExecutionPlanSpec(
        objective=objective,
        steps=[PlanStep(index=0, description="do the thing", kind=StepKind.RESEARCH)],
        constraints=PlanConstraints().to_dict(),
    )


class _Recorder:
    """Stands in for `Planner.decompose`, capturing exactly what it was called with."""

    def __init__(self):
        self.kwargs = {}

    async def __call__(self, objective, task=None, context=None, constraints=None,
                       campaign_context=None):
        self.kwargs = {
            "objective": objective,
            "task": task,
            "context": context,
            "constraints": constraints,
            "campaign_context": campaign_context,
        }
        return _spec(objective)


class _ReplanRecorder:
    def __init__(self):
        self.kwargs = {}

    async def __call__(self, task, current_spec, trigger, reason, constraints=None,
                       context_notes=None, evidence=None, context=None, campaign_context=None):
        self.kwargs = {
            "task": task,
            "trigger": trigger,
            "reason": reason,
            "context_notes": context_notes,
            "evidence": evidence,
            "context": context,
            "campaign_context": campaign_context,
        }
        revised = _spec(current_spec.objective)
        revised.steps[0].status = "pending"
        return revised


# ======================================================================
# Initial planning
# ======================================================================
@pytest.mark.asyncio
async def test_initial_decomposition_receives_the_resolved_task_context(factory):
    task_id = await _task(factory, "Claim the Retrodrop")
    async with factory() as db:
        task = await db.get(Task, task_id)
        service = TaskContextService(db)
        await service.record_observation(
            task,
            action="fetched the claim page",
            outcome=OUTCOME_CONFIRMED,
            tool="web_fetch",
            iteration=1,
            evidence={"authoritative": True, "kind": "post_action_observation"},
            observed_state={"claim": "open"},
            integration_ref="browser:default",
        )
        await service.record_observation(
            task,
            action="joined the channel",
            outcome=OUTCOME_UNVERIFIED,
            tool="telegram_join",
            iteration=2,
        )
        await service.record_observation(
            task,
            action="submitted the form",
            outcome=OUTCOME_FAILED,
            tool="form_submit",
            iteration=3,
            error="the button was disabled",
        )
        await service.record_requirement(task, text="complete the quiz")
        await db.commit()

        recorder = _Recorder()
        controller = AgentController(db)
        controller.planner.decompose = recorder

        spec = await controller._ensure_plan(task, LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED))

    assert isinstance(spec, ExecutionPlanSpec)
    context = recorder.kwargs["context"]
    assert context is not None, "initial planning was given no persisted context at all"
    assert context["goal"] == task.title
    assert context["level"] == "task"
    assert context["isolated_to_task"] is True

    # Verified facts, unverified observations and failures stay distinguishable.
    assert [f["action"] for f in context["verified_facts"]] == ["fetched the claim page"]
    assert context["verified_facts"][0]["evidence_authoritative"] is True
    assert context["verified_facts"][0]["evidence_kind"] == "post_action_observation"
    assert context["verified_facts"][0]["source"] == "browser:default"
    assert [o["action"] for o in context["unverified_observations"]] == ["joined the channel"]
    assert [f["action"] for f in context["failed_attempts"]] == ["submitted the form"]
    assert context["failed_attempts"][0]["error"] == "the button was disabled"

    # Outstanding requirements and references travel with the context.
    assert [r["text"] for r in context["outstanding_requirements"]] == ["complete the quiz"]
    assert context["external_state"]["claim"] == "open"


@pytest.mark.asyncio
async def test_initial_decomposition_receives_the_matching_campaign_context(factory):
    campaign_id = await _campaign(factory, "Retrodrop Season 3")
    task_id = await _task(factory, "Join the Retrodrop channel")

    async with factory() as db:
        task = await db.get(Task, task_id)
        campaign = await db.get(AirdropCampaign, campaign_id)
        await CampaignContextService(db).link_task(task, campaign)
        await MemoryManager(db).remember_scoped(
            "CAMPAIGN_REQUIREMENT",
            "hold at least 100 TKN before the snapshot",
            "official page",
            scope=SCOPE_CAMPAIGN,
            campaign_id=campaign.id,
            confidence=0.9,
        )
        await db.commit()

        recorder = _Recorder()
        controller = AgentController(db)
        controller.planner.decompose = recorder
        await controller._ensure_plan(task, LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED))

    campaign_context = recorder.kwargs["campaign_context"]
    assert campaign_context is not None, "a linked task was planned without its campaign"
    assert campaign_context["campaign_id"] == str(campaign_id)
    assert campaign_context["title"] == "Retrodrop Season 3"
    assert "hold at least 100 TKN before the snapshot" in campaign_context["shared_facts"]
    assert campaign_context["lifecycle_status"] == "RESEARCHING"

    context = recorder.kwargs["context"]
    assert context["campaign_id"] == str(campaign_id)
    # Campaign facts are labelled as campaign-scoped, never as this task's own state.
    assert context["campaign_facts"]
    assert all(f["scope"] == "campaign" for f in context["campaign_facts"])
    assert all(f["shared_with_other_tasks_on_this_campaign"] is True for f in context["campaign_facts"])


@pytest.mark.asyncio
async def test_a_task_without_a_campaign_gets_no_campaign_context(factory):
    task_id = await _task(factory, "A standalone task")

    async with factory() as db:
        task = await db.get(Task, task_id)
        recorder = _Recorder()
        controller = AgentController(db)
        controller.planner.decompose = recorder
        await controller._ensure_plan(task, LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED))

    assert recorder.kwargs["campaign_context"] is None
    assert recorder.kwargs["context"] is not None
    assert recorder.kwargs["context"]["campaign_id"] is None
    assert recorder.kwargs["context"]["campaign_facts"] == []


@pytest.mark.asyncio
async def test_unrelated_campaign_context_is_never_injected(factory):
    campaign_a = await _campaign(factory, "Campaign Alpha")
    campaign_b = await _campaign(factory, "Campaign Bravo")
    task_a = await _task(factory, "Task on Alpha")
    task_b = await _task(factory, "Task on Bravo")

    async with factory() as db:
        campaign = await db.get(AirdropCampaign, campaign_a)
        await MemoryManager(db).remember_scoped(
            "CAMPAIGN_REQUIREMENT", "alpha-only fact", "alpha source",
            scope=SCOPE_CAMPAIGN, campaign_id=campaign.id, confidence=0.9,
        )
        other = await db.get(AirdropCampaign, campaign_b)
        await MemoryManager(db).remember_scoped(
            "CAMPAIGN_REQUIREMENT", "bravo-only secret requirement", "bravo source",
            scope=SCOPE_CAMPAIGN, campaign_id=other.id, confidence=0.9,
        )
        await CampaignContextService(db).link_task(await db.get(Task, task_a), campaign)
        await CampaignContextService(db).link_task(await db.get(Task, task_b), other)
        await db.commit()

        recorder = _Recorder()
        controller = AgentController(db)
        controller.planner.decompose = recorder

        await controller._ensure_plan(
            await db.get(Task, task_a), LoopReport(task_id=task_a, outcome=LoopOutcome.FAILED)
        )
        alpha = recorder.kwargs

        await controller._ensure_plan(
            await db.get(Task, task_b), LoopReport(task_id=task_b, outcome=LoopOutcome.FAILED)
        )
        bravo = recorder.kwargs

    assert alpha["campaign_context"]["campaign_id"] == str(campaign_a)
    assert bravo["campaign_context"]["campaign_id"] == str(campaign_b)

    def _serialisable(captured):
        return json.dumps(
            {"context": captured["context"], "campaign_context": captured["campaign_context"]}
        )

    alpha_blob = _serialisable(alpha)
    bravo_blob = _serialisable(bravo)
    assert "bravo-only secret requirement" not in alpha_blob
    assert "Campaign Bravo" not in alpha_blob
    assert "alpha-only fact" not in bravo_blob
    assert "Campaign Alpha" not in bravo_blob


@pytest.mark.asyncio
async def test_one_task_never_receives_another_tasks_execution_history(factory):
    campaign_id = await _campaign(factory, "Shared Campaign")
    first = await _task(factory, "First task on the campaign")
    second = await _task(factory, "Second task on the campaign")

    async with factory() as db:
        campaign = await db.get(AirdropCampaign, campaign_id)
        linker = CampaignContextService(db)
        await linker.link_task(await db.get(Task, first), campaign)
        await linker.link_task(await db.get(Task, second), campaign)

        # The first task proves something about ITSELF. That is task-scoped state.
        service = TaskContextService(db)
        await service.record_observation(
            await db.get(Task, first),
            action="joined the channel for wallet 0xaaa",
            outcome=OUTCOME_CONFIRMED,
            tool="telegram_join",
            evidence={"authoritative": True, "kind": "membership"},
        )
        await service.record_requirement(await db.get(Task, first), text="wallet 0xaaa must claim")
        await db.commit()

        recorder = _Recorder()
        controller = AgentController(db)
        controller.planner.decompose = recorder
        await controller._ensure_plan(
            await db.get(Task, second), LoopReport(task_id=second, outcome=LoopOutcome.FAILED)
        )

    blob = json.dumps(
        {"context": recorder.kwargs["context"], "campaign": recorder.kwargs["campaign_context"]}
    )
    assert "0xaaa" not in blob, "one task's account-specific history leaked into another task's plan"
    assert recorder.kwargs["context"]["verified_facts"] == []
    assert recorder.kwargs["context"]["outstanding_requirements"] == []
    # It still shares the campaign level, which is the whole point of the link.
    assert recorder.kwargs["campaign_context"]["campaign_id"] == str(campaign_id)


# ======================================================================
# The real planner prompt carries the context
# ======================================================================
@pytest.mark.asyncio
async def test_the_planner_prompt_contains_the_resolved_context(factory, monkeypatch):
    campaign_id = await _campaign(factory, "Prompt Campaign")
    task_id = await _task(factory, "Plan with real context")

    async with factory() as db:
        task = await db.get(Task, task_id)
        await CampaignContextService(db).link_task(task, await db.get(AirdropCampaign, campaign_id))
        service = TaskContextService(db)
        await service.record_requirement(task, text="complete the KYC step")
        await db.commit()

        captured: dict = {}

        async def fake_complete_json(messages, **kwargs):
            captured["messages"] = messages
            return {
                "rationale": "because",
                "steps": [{"description": "complete the KYC step", "kind": "verify"}],
            }

        monkeypatch.setattr("backend.app.planning.planner.llm.complete_json", fake_complete_json)

        controller = AgentController(db)  # the REAL planner, no stub
        spec = await controller._ensure_plan(
            task, LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED)
        )

    assert spec.steps
    user_prompt = captured["messages"][-1].content
    assert "ADDITIONAL CONTEXT:" in user_prompt
    assert "complete the KYC step" in user_prompt
    assert "CAMPAIGN CONTEXT:" in user_prompt
    assert "Prompt Campaign" in user_prompt


# ======================================================================
# The replan path
# ======================================================================
@pytest.mark.asyncio
async def test_a_discovery_replan_receives_context_and_campaign_context(factory):
    campaign_id = await _campaign(factory, "Replan Campaign")
    task_id = await _task(factory, "Replan with context")

    async with factory() as db:
        task = await db.get(Task, task_id)
        await CampaignContextService(db).link_task(task, await db.get(AirdropCampaign, campaign_id))
        await TaskContextService(db).record_requirement(task, text="join @channel")
        await db.commit()

        recorder = _ReplanRecorder()
        controller = AgentController(db)
        controller.planner.replan = recorder
        plan = _spec(task.description)
        revised = await controller._maybe_replan_for_discovery(
            task, iteration=1, report=LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED),
            plan=plan,
        )

    assert revised is not None
    assert recorder.kwargs["context"] is not None
    assert recorder.kwargs["campaign_context"]["campaign_id"] == str(campaign_id)
    assert [r["text"] for r in recorder.kwargs["context"]["outstanding_requirements"]] == [
        "join @channel"
    ]
    assert recorder.kwargs["trigger"] is ReplanTrigger.DISCOVERY
    # The requirements discovered while executing are still passed explicitly.
    assert recorder.kwargs["context_notes"] == ["join @channel"]


@pytest.mark.asyncio
async def test_a_failure_replan_receives_context(factory):
    task_id = await _task(factory, "Replan after failure")

    async with factory() as db:
        task = await db.get(Task, task_id)
        service = TaskContextService(db)
        # What executing the plan already learned, which a revision must see.
        await service.record_observation(
            task,
            action="clicked the claim button",
            outcome=OUTCOME_FAILED,
            tool="browser_click",
            iteration=1,
            error="the button was disabled",
        )
        await service.record_requirement(task, text="connect a wallet first")
        await db.commit()

        recorder = _ReplanRecorder()
        controller = AgentController(db)
        controller.planner.replan = recorder

        await controller._replan(
            task,
            iteration=1,
            report=LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED),
            plan=_spec(task.description),
            trigger=ReplanTrigger.FAILURE,
            reason="the button was disabled",
            outcome=LoopOutcome.PARTIALLY_COMPLETED,
        )

    context = recorder.kwargs["context"]
    assert context is not None
    assert context["level"] == "task"
    assert context["isolated_to_task"] is True
    # The failure that triggered the replan, and the requirement it uncovered, are both
    # in front of the planner rather than only the failure text.
    assert [f["action"] for f in context["failed_attempts"]] == ["clicked the claim button"]
    assert [r["text"] for r in context["outstanding_requirements"]] == ["connect a wallet first"]
    assert recorder.kwargs["campaign_context"] is None


# ======================================================================
# Resolution cost
# ======================================================================
@pytest.mark.asyncio
async def test_resolving_twice_in_one_cycle_reads_the_rows_once(factory, monkeypatch):
    task_id = await _task(factory, "Resolve once per cycle")

    async with factory() as db:
        task = await db.get(Task, task_id)
        controller = AgentController(db)

        calls = {"n": 0}
        original = TaskContextService.resolve

        async def counting_resolve(self, target, **kwargs):
            calls["n"] += 1
            return await original(self, target, **kwargs)

        monkeypatch.setattr(TaskContextService, "resolve", counting_resolve)

        controller.begin_planning_cycle(task)
        first = await controller._resolved_planning_context(task)
        second = await controller._resolved_planning_context(task)

        assert first == second
        assert calls["n"] == 1, "the same planning cycle re-read the context"

        # A new cycle must see a context that may have moved on.
        controller.begin_planning_cycle(task)
        await controller._resolved_planning_context(task)
        assert calls["n"] == 2


# ======================================================================
# Optional spend ceilings: None means "no ceiling", a set value still binds
# ======================================================================
import math  # noqa: E402

from backend.app.core.config import settings as _settings  # noqa: E402


@pytest.mark.parametrize(
    "per_tx, daily, spent, expected",
    [
        (None, None, 0.0, math.inf),     # no ceilings configured: unbounded, no TypeError
        (None, None, 99.0, math.inf),    # spend so far is irrelevant without a daily ceiling
        (10.0, None, 0.0, 10.0),         # only the per-transaction ceiling
        (None, 25.0, 5.0, 20.0),         # only the daily ceiling: what is left today
        (10.0, 25.0, 5.0, 10.0),         # both set: the tighter one wins ...
        (50.0, 25.0, 5.0, 20.0),         # ... in either direction
        (None, 25.0, 40.0, 0.0),         # already over the daily ceiling: nothing left, never negative
    ],
)
def test_remaining_budget_treats_none_as_no_ceiling_and_still_enforces_set_ones(
    per_tx, daily, spent, expected
):
    constraints = PlanConstraints(
        max_cost_usd=per_tx, max_daily_spend_usd=daily, spent_already_usd=spent
    )
    assert constraints.remaining_budget_usd() == expected
    payload = constraints.to_dict()
    assert payload["budget_is_unbounded"] is math.isinf(expected)
    assert payload["remaining_budget_usd"] == (None if math.isinf(expected) else expected)


async def _replan_trigger(factory, monkeypatch, *, ceiling, step_cost):
    task_id = await _task(factory, "Budget trigger")
    monkeypatch.setattr(_settings, "MAX_TRANSACTION_VALUE_USD", ceiling, raising=False)
    async with factory() as db:
        task = await db.get(Task, task_id)
        recorder = _ReplanRecorder()
        controller = AgentController(db)
        controller.planner.replan = recorder
        plan = _spec(task.description)
        plan.steps[0].estimated_cost_usd = step_cost
        await controller._replan(
            task,
            iteration=1,
            report=LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED),
            plan=plan,
            trigger=ReplanTrigger.FAILURE,
            reason="boom",
            outcome=LoopOutcome.PARTIALLY_COMPLETED,
        )
    return recorder.kwargs["trigger"]


@pytest.mark.asyncio
async def test_a_failure_replan_is_not_a_budget_replan_when_no_ceiling_is_configured(
    factory, monkeypatch
):
    trigger = await _replan_trigger(factory, monkeypatch, ceiling=None, step_cost=1_000_000.0)
    assert trigger is ReplanTrigger.FAILURE


@pytest.mark.asyncio
async def test_a_failure_replan_becomes_a_budget_replan_when_a_set_ceiling_is_exceeded(
    factory, monkeypatch
):
    trigger = await _replan_trigger(factory, monkeypatch, ceiling=10.0, step_cost=50.0)
    assert trigger is ReplanTrigger.BUDGET


@pytest.mark.asyncio
async def test_a_failure_replan_stays_a_failure_replan_within_a_set_ceiling(factory, monkeypatch):
    trigger = await _replan_trigger(factory, monkeypatch, ceiling=10.0, step_cost=5.0)
    assert trigger is ReplanTrigger.FAILURE
