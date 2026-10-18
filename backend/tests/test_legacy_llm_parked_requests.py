"""Tasks parked on a model refusal BEFORE the LLM_* reasons existed.

Between b93a82e (park instead of retry) and 56b0e5f (LLM_* reasons) a model refusal was parked
under whatever `classify_reason` made of its text: AUTH_FAILURE became MISSING_AUTHENTICATION
(whose check is "is a wallet available?"), the other three became UNKNOWN (which any grounded
reply resumes). Those still-open requests therefore resumed straight into the same refusal.

They are recognised at read time from fields the platform itself wrote when it parked them:
the model client's category on the request AND the platform's own "The configured model refused
this task (<CATEGORY>)" wording. Stored rows are never rewritten (no history rewrite, no data
migration), a request that is already resolved is never reinterpreted, and anything a tool
raised, however similar its category, keeps its own reason.
"""

import pytest
from sqlalchemy import select

from backend.app.agent.controller import AgentController
from backend.app.inbox.raise_intervention import park_for_intervention
from backend.app.inbox.service import effective_reason_code
from backend.app.models import AgentConversation, InterventionRequest, Task
from backend.app.models.tasks import TaskCheckpoint
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

# Shared fixtures (the model endpoint stand-in, the HTTP client, a quiet interpreter).
from backend.tests.test_llm_inbox_fix_verification import (  # noqa: F401
    ALICE,
    client,
    fresh_rate_limiter,
    healthy,
    interpreter_unavailable,
    probe,
    unavailable,
)

# The refusal text the model client really raises, and the reason it was parked under before.
LEGACY = {
    "AUTH_FAILURE": (
        "MISSING_AUTHENTICATION",
        "LLM authentication failed (HTTP 401). Verify LLM_API_KEY for the configured endpoint.",
    ),
    "BILLING_REFUSAL": ("UNKNOWN", "The LLM endpoint refused the request for lack of credit."),
    "NOT_FOUND": (
        "UNKNOWN",
        "LLM endpoint or model not found (HTTP 404). Verify LLM_BASE_URL and LLM_MODEL_ID ('gpt-4o').",
    ),
    "MODEL_MISMATCH": (
        "UNKNOWN",
        "The LLM endpoint served 'other' but 'gpt-4o' is required. Refusing the response under "
        "LLM_REQUIRE_SERVED_MODEL_MATCH.",
    ),
}


def _old_blocker(category: str, text: str) -> str:
    """The exact wording b93a82e wrote (and 56b0e5f still writes) for a refused model."""
    return (
        f"The configured model refused this task ({category}): {text}. "
        "Fix the model settings or account, then resume the task. Nothing ran and "
        "everything already done is preserved."
    )


async def _park_as_before(factory, category, *, text=None, error_category="same", task_id="AIR-LG-1",
                          integration_ref=None):
    """Park a task the way the code before 56b0e5f did: classify from the text, no forced reason."""
    reason, message = LEGACY.get(category, ("UNKNOWN", "x"))
    async with factory() as db:
        task = Task(task_id=task_id, title=f"title {task_id}", status=TaskStatus.IN_PROGRESS.value,
                    max_retries=3, owner_email=ALICE,
                    error_details={"llm_category": category, "llm_retryable": False})
        db.add(task)
        await db.commit()
        pk = task.id
    async with factory() as db:
        task = await db.get(Task, pk)
        result = await park_for_intervention(
            db, task, owner_email=ALICE,
            error_text=text if text is not None else _old_blocker(category, message),
            error_category=category if error_category == "same" else error_category,
            integration_ref=integration_ref,
            iteration=0,
        )
        assert result is not None
    return pk, result["reason_code"]


async def _conversation_id(factory, pk):
    async with factory() as db:
        return str((await db.scalar(select(AgentConversation).where(AgentConversation.task_id == pk))).id)


async def _post(client, conversation_id, text="I fixed the model settings, resume this task", token=None):
    body = {"message": text}
    if token:
        body["client_token"] = token
    return await client.post(f"/inbox/conversations/{conversation_id}/messages", json=body)


async def _stored(factory, pk):
    async with factory() as db:
        r = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        return (r.reason_code, r.status, r.problem_summary, r.required_action, list(r.instructions or []),
                r.error_category)


async def _task_state(factory, pk):
    async with factory() as db:
        t = await db.get(Task, pk)
        ids = (await db.scalars(select(TaskCheckpoint.id).where(TaskCheckpoint.task_id == pk))).all()
        return t.status, t.retry_count, set(ids)


# ----------------------------------------------------------------------------------------
# The legacy fixtures are really what the old code produced.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("category", sorted(LEGACY))
async def test_setup_matches_what_the_old_code_stored(factory, category):
    pk, reason = await _park_as_before(factory, category)
    assert reason == LEGACY[category][0]


# ----------------------------------------------------------------------------------------
# 1. Legacy model refusals now follow the model-verification rules.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("category", sorted(LEGACY))
async def test_a_legacy_parked_task_is_not_resumed_while_the_model_is_still_broken(
    factory, client, probe, category
):
    pk, _ = await _park_as_before(factory, category)
    conversation_id = await _conversation_id(factory, pk)
    before = await _task_state(factory, pk)
    probe["result"] = unavailable(category)

    response = await _post(client, conversation_id)

    assert response.json()["resumed"] is False
    assert await _task_state(factory, pk) == before
    assert probe["calls"] == 1
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("w") is None


@pytest.mark.parametrize("category", sorted(LEGACY))
async def test_a_legacy_parked_task_resumes_once_the_model_works(factory, client, probe, category):
    pk, _ = await _park_as_before(factory, category)
    conversation_id = await _conversation_id(factory, pk)
    before = await _task_state(factory, pk)
    probe["result"] = healthy()

    response = await _post(client, conversation_id, token="tok")

    assert response.json()["resumed"] is True
    status, retries, checkpoints = await _task_state(factory, pk)
    assert status == TaskStatus.READY.value and retries == before[1] == 0
    assert before[2] <= checkpoints
    async with factory() as db:
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        assert request.status == "RESOLVED" and request.verification_method == "llm_probe"
    assert (await _post(client, conversation_id, token="tok")).json()["resumed"] is False
    assert probe["calls"] == 1


@pytest.mark.parametrize("category", ["BILLING_REFUSAL", "NOT_FOUND", "MODEL_MISMATCH"])
async def test_a_grounded_answer_no_longer_resumes_a_legacy_unknown_model_refusal(
    factory, client, probe, interpreter_unavailable, category
):
    """UNKNOWN is 'information resolvable': an answer alone used to be enough to resume."""
    from backend.app.inbox.interpretation import ReplyInterpretation

    pk, reason = await _park_as_before(factory, category)
    assert reason == "UNKNOWN"
    conversation_id = await _conversation_id(factory, pk)
    probe["result"] = unavailable(category)
    interpreter_unavailable["result"] = ReplyInterpretation(
        decision="SUFFICIENT_INFORMATION", explanation="The operator answered.",
        extracted_information=["topped up the account"],
    )

    response = await _post(client, conversation_id, text="I topped up the account")

    assert response.json()["resumed"] is False
    assert (await _task_state(factory, pk))[0] == TaskStatus.NEEDS_USER_ACTION.value


# ----------------------------------------------------------------------------------------
# 2. No history is rewritten.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("category", sorted(LEGACY))
async def test_stored_rows_are_never_rewritten(factory, client, probe, category):
    pk, _ = await _park_as_before(factory, category)
    conversation_id = await _conversation_id(factory, pk)
    stored_before = await _stored(factory, pk)
    probe["result"] = unavailable(category)

    await _post(client, conversation_id)
    await client.get(f"/inbox/conversations/{conversation_id}")

    assert await _stored(factory, pk) == stored_before  # reason, summary, guidance: all as written


@pytest.mark.parametrize("category", sorted(LEGACY))
async def test_a_resolved_legacy_request_keeps_its_original_reason_after_resuming(
    factory, client, probe, category
):
    pk, reason = await _park_as_before(factory, category)
    conversation_id = await _conversation_id(factory, pk)
    probe["result"] = healthy()
    await _post(client, conversation_id)
    stored = await _stored(factory, pk)
    assert stored[0] == reason and stored[1] == "RESOLVED"


@pytest.mark.parametrize("category", sorted(LEGACY))
async def test_the_operator_sees_the_real_reason_for_an_open_legacy_request(factory, client, category):
    pk, legacy_reason = await _park_as_before(factory, category)
    expected = {
        "AUTH_FAILURE": "LLM_AUTH_FAILURE", "BILLING_REFUSAL": "LLM_BILLING_REFUSAL",
        "NOT_FOUND": "LLM_NOT_FOUND", "MODEL_MISMATCH": "LLM_MODEL_MISMATCH",
    }[category]

    body = (await client.get(f"/inbox/conversations/{await _conversation_id(factory, pk)}")).json()
    view = body["open_request"]

    assert view["reason_code"] == expected
    assert view["machine_verifiable"] is True
    assert "wallet" not in " ".join([view["required_action"] or ""] + view["instructions"]).lower()
    assert (await _stored(factory, pk))[0] == legacy_reason  # the view differs; the row does not


async def test_a_resolved_request_is_shown_as_it_was_recorded(factory, client, probe):
    pk, legacy_reason = await _park_as_before(factory, "BILLING_REFUSAL")
    conversation_id = await _conversation_id(factory, pk)
    probe["result"] = healthy()
    await _post(client, conversation_id)
    async with factory() as db:
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        assert effective_reason_code(request) == legacy_reason  # history is history


# ----------------------------------------------------------------------------------------
# 3. Nothing unrelated is misclassified.
# ----------------------------------------------------------------------------------------
async def _park_unrelated(factory, text, category, task_id="AIR-LG-U"):
    return await _park_as_before(factory, category or "TOOL", text=text, error_category=category,
                                 task_id=task_id)


@pytest.mark.parametrize(
    "text,category,expected",
    [
        # A tool's own AUTH_FAILURE: same category, different origin.
        ("Step 2 failed: the site returned 401 unauthorized", "AUTH_FAILURE", "MISSING_AUTHENTICATION"),
        # A tool's NOT_FOUND.
        ("the page layout changed unexpectedly", "NOT_FOUND", "UNKNOWN"),
        # Model-like wording from a tool, without the platform's own refusal wording.
        ("LLM authentication failed (HTTP 401) calling the partner API", "AUTH_FAILURE",
         "MISSING_AUTHENTICATION"),
        # The platform's wording but no model-client category on the request.
        (_old_blocker("AUTH_FAILURE", "x"), None, "UNKNOWN"),
        (_old_blocker("BILLING_REFUSAL", "x"), None, "UNKNOWN"),
        # The platform's wording with a category the model client does not report as fixable.
        (_old_blocker("CONTEXT_OVERFLOW", "x"), "CONTEXT_OVERFLOW", "UNKNOWN"),
        # Category and wording that disagree.
        (_old_blocker("NOT_FOUND", "x"), "AUTH_FAILURE", "MISSING_AUTHENTICATION"),
    ],
)
async def test_unrelated_blockers_are_not_taken_for_model_refusals(factory, client, probe, text, category, expected):
    pk, reason = await _park_unrelated(factory, text, category)
    assert reason == expected, "setup"
    async with factory() as db:
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        assert effective_reason_code(request) == expected
    probe["result"] = unavailable("AUTH_FAILURE")

    await _post(client, await _conversation_id(factory, pk))

    assert probe["calls"] == 0  # the model's state is irrelevant to this blocker


async def test_a_blocker_with_another_stored_reason_is_never_reclassified(factory):
    pk, reason = await _park_as_before(
        factory, "AUTH_FAILURE", text=_old_blocker("AUTH_FAILURE", "telegram session expired"),
    )
    async with factory() as db:
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        for stored in ("TELEGRAM_SESSION_UNAVAILABLE", "HUMAN_VERIFICATION", "OPERATOR_PAUSE", "HUMAN_APPROVAL",
                       "MISSING_CONFIGURATION", "AMBIGUOUS_INSTRUCTION"):
            request.reason_code = stored
            assert effective_reason_code(request) == stored


@pytest.mark.parametrize("status", ["RESOLVED", "CANCELLED", "SUPERSEDED", "RESUME_SCHEDULED"])
async def test_only_an_open_request_is_reinterpreted(factory, status):
    pk, reason = await _park_as_before(factory, "NOT_FOUND")
    async with factory() as db:
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        request.status = status
        assert effective_reason_code(request) == reason


def test_the_new_reasons_are_unchanged_by_the_lookup():
    from types import SimpleNamespace

    for code in ("LLM_AUTH_FAILURE", "LLM_BILLING_REFUSAL", "LLM_NOT_FOUND", "LLM_MODEL_MISMATCH"):
        request = SimpleNamespace(reason_code=code, status="PENDING", error_category="AUTH_FAILURE",
                                  problem_summary="anything")
        assert effective_reason_code(request) == code


async def test_a_task_that_was_not_parked_by_a_model_refusal_is_left_alone(factory, client, probe):
    """A mid-run tool failure on a task that earlier hit a model refusal keeps its own reason."""
    async with factory() as db:
        task = Task(task_id="AIR-LG-M", title="t", status=TaskStatus.IN_PROGRESS.value, max_retries=3,
                    owner_email=ALICE, error_details={"llm_category": "AUTH_FAILURE", "llm_retryable": False})
        db.add(task)
        await db.commit()
        pk = task.id
    async with factory() as db:
        result = await AgentController(db)._request_operator_input(
            await db.get(Task, pk), iteration=3, error_text="Step 4 failed: login rejected, 401",
            error_category="AUTH_FAILURE",
        )
    assert result["reason_code"] == "MISSING_AUTHENTICATION"
    probe["result"] = unavailable("AUTH_FAILURE")

    await _post(client, await _conversation_id(factory, pk))

    assert probe["calls"] == 0
