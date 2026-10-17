"""Regression tests: bounded observation retrieval and context-cache versioning.

Each test pins a defect that existed before the fix:

* observations were ordered by a random UUID primary key, so "the latest 200" and "the last
  observation" were arbitrary;
* a required authoritative observation older than the recent window never reached a prompt;
* the cached context was keyed on `TaskContext.revision` alone, so a campaign/global memory
  edit or delete, or a changed requirement/plan, left a stale context in place.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update

from backend.app.agent.context import (
    OLDER_EVIDENCE_LIMIT,
    RECENT_OBSERVATION_WINDOW,
    TaskContextService,
)
from backend.app.memory.manager import MemoryManager
from backend.app.models import (
    AirdropCampaign,
    ExecutionPlan,
    MemoryRecord,
    Task,
    TaskObservation,
    TaskRequirement,
)
from backend.app.models.context import (
    OUTCOME_ATTEMPTED,
    OUTCOME_BLOCKED,
    OUTCOME_CONFIRMED,
    OUTCOME_EXECUTED,
    OUTCOME_FAILED,
    OUTCOME_UNVERIFIED,
)

_n = 0
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def make_task(db) -> Task:
    global _n
    _n += 1
    task = Task(task_id=f"AIR-CTX-{_n:05d}", title="ctx", status="IN_PROGRESS")
    db.add(task)
    await db.commit()
    return task


def obs(task, i, *, outcome=OUTCOME_CONFIRMED, authoritative=False, idempotent=True, action=None):
    """One observation, stamped at a known time so ordering is deterministic."""
    return TaskObservation(
        task_id=task.id,
        iteration=i,
        action=action or f"action-{i}",
        tool="stub_tool",
        fingerprint=f"fp-{task.task_id}-{i}",
        outcome=outcome,
        evidence_authoritative=authoritative,
        is_idempotent=idempotent,
        observed_state={},
        created_at=T0 + timedelta(seconds=i),
    )


# ------------------------------------------------------------------ ordering
async def test_observations_are_returned_in_persisted_time_order(factory):
    async with factory() as db:
        task = await make_task(db)
        # Insert out of order: ordering must come from created_at/iteration, not insert
        # order or the random UUID.
        for i in (5, 1, 3, 2, 4):
            db.add(obs(task, i))
        await db.commit()

        svc = TaskContextService(db)
        listed = await svc.observations(task, limit=None)
        assert [o.iteration for o in listed] == [1, 2, 3, 4, 5]

        latest_two = await svc.observations(task, limit=2)
        assert [o.iteration for o in latest_two] == [4, 5]

        records, diag = await svc.retrieve_observations(task)
        assert [r.iteration for r in records] == [1, 2, 3, 4, 5]
        assert diag.total == 5 and diag.complete


async def test_equal_timestamps_fall_back_to_iteration(factory):
    async with factory() as db:
        task = await make_task(db)
        for i in (3, 1, 2):
            row = obs(task, i)
            row.created_at = T0
            db.add(row)
        await db.commit()
        listed = await TaskContextService(db).observations(task, limit=None)
        assert [o.iteration for o in listed] == [1, 2, 3]


# ------------------------------------------------- bounded historical retrieval
async def test_authoritative_proof_older_than_the_window_is_still_retrieved(factory):
    async with factory() as db:
        task = await make_task(db)
        # Row 0 is the only authoritative proof and is older than the 200-row window.
        db.add(obs(task, 0, authoritative=True, action="joined-the-group"))
        total = RECENT_OBSERVATION_WINDOW + 50
        for i in range(1, total):
            db.add(obs(task, i, outcome=OUTCOME_EXECUTED, action=f"noise-{i}"))
        await db.commit()

        records, diag = await TaskContextService(db).retrieve_observations(task)

    actions = [r.action for r in records]
    assert "joined-the-group" in actions, "older authoritative proof must reach the prompt"
    assert diag.recent_window == RECENT_OBSERVATION_WINDOW
    assert diag.total == total
    assert diag.older_selected >= 1
    # Chronological order is preserved, and the old proof comes first.
    assert actions[0] == "joined-the-group"


async def test_window_still_bounds_history_and_reports_what_it_dropped(factory):
    async with factory() as db:
        task = await make_task(db)
        total = RECENT_OBSERVATION_WINDOW + 40
        # Idempotent, confirmed but NOT authoritative: needed by nothing.
        for i in range(total):
            db.add(obs(task, i, outcome=OUTCOME_CONFIRMED, authoritative=False))
        await db.commit()
        records, diag = await TaskContextService(db).retrieve_observations(task)

    assert len(records) == RECENT_OBSERVATION_WINDOW, "history must never be loaded wholesale"
    assert diag.omitted_required == 0
    assert diag.omitted_other == 40, "older, non-required records are counted, not hidden"
    assert diag.complete


async def test_required_evidence_over_the_limit_is_reported_with_ids(factory):
    async with factory() as db:
        task = await make_task(db)
        older_required = OLDER_EVIDENCE_LIMIT + 7
        for i in range(older_required):
            db.add(obs(task, i, authoritative=True))
        for i in range(older_required, older_required + RECENT_OBSERVATION_WINDOW):
            db.add(obs(task, i, outcome=OUTCOME_EXECUTED))
        await db.commit()
        records, diag = await TaskContextService(db).retrieve_observations(task)

    assert diag.older_selected == OLDER_EVIDENCE_LIMIT
    assert diag.omitted_required == 7
    assert len(diag.omitted_required_ids) == 7
    assert not diag.complete
    assert any(w.startswith("observation_retrieval_omitted_required=7") for w in diag.warnings())
    # The ones that fit are the newest of the required class.
    included = {r.iteration for r in records}
    assert (older_required - 1) in included and 0 not in included


async def test_older_failures_and_non_idempotent_actions_are_retained(factory):
    async with factory() as db:
        task = await make_task(db)
        db.add(obs(task, 0, outcome=OUTCOME_FAILED, action="old-failure"))
        db.add(obs(task, 1, outcome=OUTCOME_UNVERIFIED, action="old-unknown"))
        db.add(obs(task, 2, outcome=OUTCOME_CONFIRMED, idempotent=False, action="old-transfer"))
        db.add(obs(task, 3, outcome=OUTCOME_BLOCKED, action="old-blocked"))
        db.add(obs(task, 4, outcome=OUTCOME_ATTEMPTED, action="old-attempt"))
        for i in range(5, 5 + RECENT_OBSERVATION_WINDOW):
            db.add(obs(task, i, outcome=OUTCOME_CONFIRMED))
        await db.commit()
        records, _ = await TaskContextService(db).retrieve_observations(task)

    actions = {r.action for r in records}
    assert {"old-failure", "old-unknown", "old-transfer", "old-blocked", "old-attempt"} <= actions


async def test_retrieval_is_isolated_per_task(factory):
    async with factory() as db:
        a = await make_task(db)
        b = await make_task(db)
        db.add(obs(a, 0, authoritative=True, action="belongs-to-a"))
        db.add(obs(b, 0, authoritative=True, action="belongs-to-b"))
        await db.commit()
        svc = TaskContextService(db)
        a_records, _ = await svc.retrieve_observations(a)
        b_records, _ = await svc.retrieve_observations(b)
    assert [r.action for r in a_records] == ["belongs-to-a"]
    assert [r.action for r in b_records] == ["belongs-to-b"]


async def test_resolve_exposes_source_ids_and_prompt_diagnostics(factory):
    async with factory() as db:
        task = await make_task(db)
        svc = TaskContextService(db)
        await svc.load_or_create(task)
        db.add(obs(task, 0, authoritative=True))
        await db.commit()
        resolved = await svc.resolve(task)
        prompt = resolved.to_prompt_dict()

    assert resolved.source_ids["observations"], "trace needs the ids of the feeding rows"
    assert prompt["retrieval_diagnostics"]["total_observations"] == 1
    assert len(prompt["verified_facts"]) == 1


# ------------------------------------------------------------ cache versioning
async def _campaign_task(db):
    """A task linked to a campaign, so campaign memory is a dependency of its context."""
    from backend.app.models import Project

    project = Project(name="p", slug="p")
    db.add(project)
    await db.flush()
    campaign = AirdropCampaign(project_id=project.id, title="c")
    db.add(campaign)
    await db.flush()
    task = await make_task(db)
    svc = TaskContextService(db)
    context = await svc.load_or_create(task)
    context.campaign_id = campaign.id
    await db.commit()
    return task, campaign, svc


async def test_version_is_stable_when_nothing_changed(factory):
    async with factory() as db:
        task = await make_task(db)
        svc = TaskContextService(db)
        await svc.load_or_create(task)
        await db.commit()
        first = await svc.context_version(task)
        second = await svc.context_version(task)
    assert first.generation == second.generation


async def test_global_memory_update_and_delete_change_the_generation(factory):
    async with factory() as db:
        task = await make_task(db)
        svc = TaskContextService(db)
        await svc.load_or_create(task)
        record = await MemoryManager(db).remember_scoped("VERIFIED_FACT", "fact one", "test")
        await db.commit()
        v0 = await svc.context_version(task)

        record.content = "fact one, edited in place"
        await db.commit()
        v1 = await svc.context_version(task)
        assert v1.generation != v0.generation, "an in-place content edit must invalidate"

        record.confidence = 0.1
        await db.commit()
        v2 = await svc.context_version(task)
        assert v2.generation != v1.generation, "a confidence change must invalidate"

        await db.execute(delete(MemoryRecord).where(MemoryRecord.id == record.id))
        await db.commit()
        v3 = await svc.context_version(task)
        assert v3.generation != v2.generation, "a delete must invalidate"


async def test_edit_that_keeps_length_and_confidence_is_still_detected(factory):
    """Same row count, same content length, same confidence: only `updated_at` moves."""
    async with factory() as db:
        task = await make_task(db)
        svc = TaskContextService(db)
        await svc.load_or_create(task)
        record = await MemoryManager(db).remember_scoped("VERIFIED_FACT", "aaaa", "test")
        await db.commit()
        v0 = await svc.context_version(task)
        await db.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id == record.id)
            .values(content="bbbb", updated_at=datetime.now(timezone.utc) + timedelta(seconds=5))
        )
        await db.commit()
        v1 = await svc.context_version(task)
    assert v1.generation != v0.generation


async def test_campaign_memory_changes_invalidate_only_linked_tasks(factory):
    async with factory() as db:
        task, campaign, svc = await _campaign_task(db)
        unlinked = await make_task(db)
        await svc.load_or_create(unlinked)
        await db.commit()

        linked_before = await svc.context_version(task)
        unlinked_before = await svc.context_version(unlinked)

        record = await MemoryManager(db).remember_scoped(
            "CAMPAIGN_REQUIREMENT", "needs a wallet", "test",
            scope="campaign", campaign_id=campaign.id,
        )
        await db.commit()
        linked_after = await svc.context_version(task)
        unlinked_after = await svc.context_version(unlinked)
        assert linked_after.generation != linked_before.generation
        assert unlinked_after.generation == unlinked_before.generation

        await db.execute(delete(MemoryRecord).where(MemoryRecord.id == record.id))
        await db.commit()
        assert (await svc.context_version(task)).generation == linked_before.generation


async def test_observation_requirement_and_plan_changes_move_the_generation(factory):
    async with factory() as db:
        task = await make_task(db)
        svc = TaskContextService(db)
        await svc.load_or_create(task)
        await db.commit()
        base = await svc.context_version(task)

        row = obs(task, 1, outcome=OUTCOME_ATTEMPTED)
        db.add(row)
        await db.commit()
        after_insert = await svc.context_version(task)
        assert after_insert.generation != base.generation

        # An outcome rewritten in place (e.g. attempted -> confirmed) moves updated_at.
        await db.execute(
            update(TaskObservation)
            .where(TaskObservation.id == row.id)
            .values(
                outcome=OUTCOME_CONFIRMED,
                updated_at=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
        )
        await db.commit()
        after_update = await svc.context_version(task)
        assert after_update.generation != after_insert.generation

        await db.execute(delete(TaskObservation).where(TaskObservation.id == row.id))
        await db.commit()
        after_delete = await svc.context_version(task)
        assert after_delete.generation != after_update.generation

        db.add(TaskRequirement(task_id=task.id, text="join the channel"))
        await db.commit()
        after_req = await svc.context_version(task)
        assert after_req.generation != after_delete.generation

        db.add(ExecutionPlan(task_id=task.id, revision=1, is_current=True, objective="o", steps=[]))
        await db.commit()
        v_plan1 = await svc.context_version(task)
        assert v_plan1.plan_revision == 1 and v_plan1.generation != after_req.generation

        await db.execute(update(ExecutionPlan).where(ExecutionPlan.task_id == task.id).values(is_current=False))
        db.add(ExecutionPlan(task_id=task.id, revision=2, is_current=True, objective="o", steps=[]))
        await db.commit()
        v_plan2 = await svc.context_version(task)
        assert v_plan2.plan_revision == 2 and v_plan2.generation != v_plan1.generation


async def test_version_reads_a_write_made_earlier_in_the_same_transaction(factory):
    async with factory() as db:
        task = await make_task(db)
        svc = TaskContextService(db)
        await svc.load_or_create(task)
        await db.commit()
        before = await svc.context_version(task)
        db.add(obs(task, 1))  # not committed, not flushed
        after = await svc.context_version(task)
    assert after.generation != before.generation
