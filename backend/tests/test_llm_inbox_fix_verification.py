"""An operator's reply must not resume a task parked on a model error unless the configured
model actually works now.

Before: LLM_AUTH_FAILURE / LLM_BILLING_REFUSAL / LLM_NOT_FOUND / LLM_MODEL_MISMATCH had no
verifier, so `verify()` answered "no automated check exists", the reply path treated ANY reply as
the operator's confirmation, and the task resumed ("Resuming anyway") straight into the same
refusal. The guidance even told the operator "I do not call the model to test the fix".

The path under test is production: HTTP reply -> scope and ownership checks -> `ReplyRouter` ->
`verify` -> the registered verifier -> `_resume`. Only the model endpoint is replaced (its
`health_check`, the client's own probe); everything that decides what to do with the answer runs.
"""

import asyncio
import logging
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from backend.app.agent.controller import AgentController
from backend.app.api.v1.endpoints.inbox import router as inbox_router
from backend.app.core.config import settings
from backend.app.database.session import Base, get_db
from backend.app.inbox import verification
from backend.app.inbox.interpretation import InterpretationError, ReplyInterpretation
from backend.app.llm.client import LLMError, llm
from backend.app.models import (
    AgentConversation,
    ConversationMessage,
    InterventionRequest,
    Task,
    TaskEvent,
)
from backend.app.models.tasks import TaskCheckpoint
from backend.app.planning.planner import ExecutionPlanSpec, PlanStep, StepKind
from backend.app.security.auth import Principal, Scope, get_principal
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

ALICE = "alice@example.test"
BOB = "bob@example.test"
FULL = frozenset({Scope.READ.value, Scope.AGENT_CHAT.value, Scope.TASK_WRITE.value})
READ_ONLY = frozenset({Scope.READ.value})
RESUME = "I corrected the model settings, resume this task"
FAKE_KEY = "Zq9-plain-secret-value-4417"

REASONS = {
    "AUTH_FAILURE": "LLM_AUTH_FAILURE",
    "BILLING_REFUSAL": "LLM_BILLING_REFUSAL",
    "NOT_FOUND": "LLM_NOT_FOUND",
    "MODEL_MISMATCH": "LLM_MODEL_MISMATCH",
}
# What a still-broken model must tell the operator to look at, per category.
MUST_NAME = {
    "AUTH_FAILURE": ("LLM_API_KEY",),
    "BILLING_REFUSAL": ("credit",),
    "NOT_FOUND": ("LLM_BASE_URL", "LLM_MODEL_ID"),
    "MODEL_MISMATCH": ("LLM_MODEL_ID", "LLM_REQUIRE_SERVED_MODEL_MATCH"),
}
PHASES = ["planning", "midrun"]


# ----------------------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------------------
def healthy(model="gpt-4o"):
    return {
        "status": "HEALTHY", "latency_ms": 12.0, "requested_model": model, "served_model": model,
        "model_matches_request": True, "detail": "Fixed model endpoint responding",
    }


def unavailable(category, *, retryable=False, detail=None):
    return {
        "status": "UNAVAILABLE", "latency_ms": None, "model": "gpt-4o",
        "detail": detail or f"provider said no ({category})",
        "error_category": category, "retryable": retryable,
    }


@pytest.fixture
def probe(monkeypatch):
    """Replace the endpoint behind the client's own `health_check`; nothing else is mocked."""
    state = {"calls": 0, "result": healthy(), "raise": None, "delay": 0.0}

    async def health_check():  # takes no arguments: the probe can never be pointed elsewhere
        state["calls"] += 1
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        if state["raise"] is not None:
            raise state["raise"]
        return state["result"]

    async def forbidden(*a, **k):
        raise AssertionError("verification must probe the connection, not run a completion")

    monkeypatch.setattr(llm, "health_check", health_check)
    for name in ("complete", "complete_json", "stream", "stream_complete"):
        monkeypatch.setattr(llm, name, forbidden)
    return state


@pytest.fixture(autouse=True)
def fresh_rate_limiter():
    """The reply endpoint is rate limited per process; each test is a separate operator session."""
    from backend.app.security.auth import rate_limiter

    rate_limiter.reset()
    yield
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def interpreter_unavailable(monkeypatch):
    from backend.app.inbox import replies

    state = {"result": None}

    async def interpret(self, context, body):
        if state["result"] is not None:
            return state["result"]
        raise InterpretationError("model not reachable in tests", category="MODEL_UNAVAILABLE")

    monkeypatch.setattr(replies.ReplyInterpreter, "interpret", interpret)
    return state


@pytest.fixture
async def client(factory):
    app = FastAPI()
    app.include_router(inbox_router)

    async def _db():
        async with factory() as session:
            yield session

    caller = {"email": ALICE, "scopes": FULL}

    async def _principal():
        return Principal(user_id=None, email=caller["email"], scopes=caller["scopes"])

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_principal] = _principal
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        http.as_caller = lambda email, scopes=FULL: caller.update(email=email, scopes=scopes)
        yield http


def _spec():
    return ExecutionPlanSpec(
        objective="o",
        steps=[PlanStep(index=i, description=f"do {i}", kind=StepKind.INTERACT, tool="probe") for i in range(2)],
    )


async def _park(factory, monkeypatch, category, *, phase="planning", task_id="AIR-FV-1"):
    """Park a task on a model refusal exactly as production does, before or after planning."""
    from backend.app.agent.controller import StepResult

    err = LLMError(f"provider refused ({category})", category=category, retryable=False)

    async def ensure_plan(self, task, report):
        if phase == "planning":
            raise err
        return _spec()

    async def run_iteration(self, task, iteration, report, plan_spec):
        if iteration == 2:
            raise err
        return StepResult(iteration, "did 1", "probe", {"ok": True}, True, "ok", 0), False, None

    monkeypatch.setattr(AgentController, "_ensure_plan", ensure_plan)
    monkeypatch.setattr(AgentController, "_run_iteration", run_iteration)
    async with factory() as db:
        task = Task(task_id=task_id, title=f"title {task_id}", status=TaskStatus.IN_PROGRESS.value,
                    max_retries=3, owner_email=ALICE)
        db.add(task)
        await db.commit()
        pk = task.id
    async with factory() as db:
        await AgentController(db).execute_task(await db.get(Task, pk))
        await db.commit()
    async with factory() as db:
        assert (await db.get(Task, pk)).status == TaskStatus.NEEDS_USER_ACTION.value, "setup: must be parked"
    return pk


async def _conversation_id(factory, pk):
    async with factory() as db:
        return str((await db.scalar(select(AgentConversation).where(AgentConversation.task_id == pk))).id)


async def _snapshot(factory, pk):
    async with factory() as db:
        task = await db.get(Task, pk)
        checkpoints = (await db.scalars(select(TaskCheckpoint.id).where(TaskCheckpoint.task_id == pk))).all()
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk)
                                  .order_by(InterventionRequest.created_at.desc()))
        return {
            "status": task.status, "retry_count": task.retry_count,
            "next_execution_at": task.next_execution_at, "checkpoints": set(checkpoints),
            "request_status": request.status if request else None,
        }


async def _post(client, conversation_id, text=RESUME, token=None):
    body = {"message": text}
    if token:
        body["client_token"] = token
    return await client.post(f"/inbox/conversations/{conversation_id}/messages", json=body)


async def _agent_texts(factory, conversation_id):
    # The conversation id arrives as the string the API returns; the UUID column needs a UUID.
    conversation_id = uuid.UUID(str(conversation_id))
    async with factory() as db:
        rows = (await db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id, ConversationMessage.role == "assistant")
            .order_by(ConversationMessage.created_at)
        )).all()
        return [m.content for m in rows]


async def _everything_persisted(factory) -> str:
    chunks = []
    async with factory() as db:
        for table in Base.metadata.sorted_tables:
            for row in (await db.execute(select(table))).mappings().all():
                chunks.append(" ".join(str(v) for v in row.values() if v is not None))
    return "\n".join(chunks)


async def _claimable(factory):
    async with factory() as db:
        return await TaskManager(db).claim_next_task("worker-1")


# ----------------------------------------------------------------------------------------
# 1. Still broken -> the task stays parked and the operator is told what to fix.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("category", sorted(REASONS))
async def test_a_reply_does_not_resume_a_task_while_the_model_is_still_broken(
    factory, client, probe, monkeypatch, category, phase
):
    pk = await _park(factory, monkeypatch, category, phase=phase)
    conversation_id = await _conversation_id(factory, pk)
    parked = await _snapshot(factory, pk)
    probe["result"] = unavailable(category)

    response = await _post(client, conversation_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumed"] is False and body["status"] == "NEEDS_MORE_INFO"
    assert body["verification"]["verified"] is False if "verified" in body.get("verification", {}) else True
    assert await _snapshot(factory, pk) == parked  # nothing moved: status, retries, checkpoints, request
    assert await _claimable(factory) is None
    assert probe["calls"] == 1
    last = (await _agent_texts(factory, conversation_id))[-1]
    assert "still" in last.lower() and "not resumed" in last.lower().replace("has not been resumed", "not resumed")
    for name in MUST_NAME[category]:
        assert name.lower() in last.lower(), f"{name} missing from: {last}"


@pytest.mark.parametrize("category", sorted(REASONS))
async def test_an_informational_reply_cannot_resume_a_broken_model_either(
    factory, client, probe, monkeypatch, interpreter_unavailable, category
):
    pk = await _park(factory, monkeypatch, category)
    conversation_id = await _conversation_id(factory, pk)
    parked = await _snapshot(factory, pk)
    probe["result"] = unavailable(category)
    interpreter_unavailable["result"] = ReplyInterpretation(
        decision="SUFFICIENT_INFORMATION",
        explanation="The operator says the setting was changed.",
        extracted_information=["the setting was changed"],
    )

    response = await _post(client, conversation_id, text="I changed it, should be fine now")

    assert response.json()["resumed"] is False
    assert await _snapshot(factory, pk) == parked
    assert probe["calls"] == 1


async def test_the_failure_now_reported_is_the_current_one_not_the_original(
    factory, client, probe, monkeypatch
):
    pk = await _park(factory, monkeypatch, "AUTH_FAILURE")
    conversation_id = await _conversation_id(factory, pk)
    probe["result"] = unavailable("NOT_FOUND")  # the key was fixed; the model id is now wrong

    response = await _post(client, conversation_id)

    assert response.json()["resumed"] is False
    last = (await _agent_texts(factory, conversation_id))[-1]
    assert "LLM_MODEL_ID" in last and "LLM_BASE_URL" in last
    assert (await _snapshot(factory, pk))["status"] == TaskStatus.NEEDS_USER_ACTION.value


@pytest.mark.parametrize(
    "result,raises",
    [
        (unavailable("CONNECTION_ERROR", retryable=True), None),
        (unavailable("TIMEOUT", retryable=True), None),
        (unavailable("PROVIDER_ERROR", retryable=True), None),
        (unavailable("BAD_REQUEST"), None),
        (None, RuntimeError("boom")),
    ],
)
async def test_an_inconclusive_check_keeps_the_task_parked(
    factory, client, probe, monkeypatch, result, raises
):
    pk = await _park(factory, monkeypatch, "AUTH_FAILURE")
    conversation_id = await _conversation_id(factory, pk)
    parked = await _snapshot(factory, pk)
    probe["result"], probe["raise"] = result, raises

    response = await _post(client, conversation_id)

    assert response.json()["resumed"] is False
    assert await _snapshot(factory, pk) == parked
    last = (await _agent_texts(factory, conversation_id))[-1]
    assert "reply again" in last.lower()


async def test_a_hung_check_times_out_and_keeps_the_task_parked(factory, client, probe, monkeypatch):
    monkeypatch.setattr(verification, "VERIFY_TIMEOUT_SECONDS", 0.05)
    pk = await _park(factory, monkeypatch, "BILLING_REFUSAL")
    conversation_id = await _conversation_id(factory, pk)
    probe["delay"] = 5.0

    response = await _post(client, conversation_id)

    assert response.json()["resumed"] is False
    assert (await _snapshot(factory, pk))["status"] == TaskStatus.NEEDS_USER_ACTION.value
    assert await _claimable(factory) is None


# ----------------------------------------------------------------------------------------
# 2. Fixed -> the SAME task resumes, verified for real, without spending a retry.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("category", sorted(REASONS))
async def test_a_working_model_resumes_the_same_task(factory, client, probe, monkeypatch, category, phase):
    pk = await _park(factory, monkeypatch, category, phase=phase)
    conversation_id = await _conversation_id(factory, pk)
    parked = await _snapshot(factory, pk)
    probe["result"] = healthy()
    async with factory() as db:
        tasks_before = await db.scalar(select(func.count()).select_from(Task))

    response = await _post(client, conversation_id, token="tok-1")

    assert response.status_code == 200, response.text
    assert response.json()["resumed"] is True and response.json()["status"] == "RESUMED"
    after = await _snapshot(factory, pk)
    assert after["status"] == TaskStatus.READY.value
    assert after["retry_count"] == parked["retry_count"] == 0
    assert parked["checkpoints"] <= after["checkpoints"]
    async with factory() as db:
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        assert request.status == "RESOLVED" and request.reason_code == REASONS[category]
        assert request.verification_method == "llm_probe"  # a real check, not "operator_confirmed"
        assert await db.scalar(select(func.count()).select_from(Task)) == tasks_before
    claimed = await _claimable(factory)
    assert claimed is not None and claimed.id == pk and claimed.task_id == "AIR-FV-1"
    assert "verified" in (await _agent_texts(factory, conversation_id))[-1].lower()


async def test_a_reply_that_is_not_an_instruction_is_verified_the_same_way(factory, client, probe, monkeypatch):
    pk = await _park(factory, monkeypatch, "AUTH_FAILURE")
    conversation_id = await _conversation_id(factory, pk)
    probe["result"] = healthy()
    response = await _post(client, conversation_id, text="I rotated the key and restarted")
    assert response.json()["resumed"] is True
    assert probe["calls"] == 1


# ----------------------------------------------------------------------------------------
# 3. The fixed provider/model is never switched and never asked anything else.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("result", [unavailable("AUTH_FAILURE"), healthy()])
async def test_verification_never_changes_the_configured_model_or_provider(
    factory, client, probe, monkeypatch, result
):
    pk = await _park(factory, monkeypatch, "AUTH_FAILURE")
    conversation_id = await _conversation_id(factory, pk)
    before = (settings.LLM_MODEL_ID, settings.LLM_BASE_URL, settings.LLM_API_KEY, llm.model_id, llm.base_url, llm.api_key)
    probe["result"] = result

    await _post(client, conversation_id)

    after = (settings.LLM_MODEL_ID, settings.LLM_BASE_URL, settings.LLM_API_KEY, llm.model_id, llm.base_url, llm.api_key)
    assert after == before
    assert probe["calls"] == 1  # one probe of the one fixed client; the completion spies never fired


# ----------------------------------------------------------------------------------------
# 4. No credential is ever exposed by a verification.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("category", sorted(REASONS))
async def test_a_secret_in_the_probe_result_never_reaches_the_operator_or_storage(
    factory, client, probe, monkeypatch, caplog, category
):
    monkeypatch.setattr(settings, "LLM_API_KEY", FAKE_KEY)
    pk = await _park(factory, monkeypatch, category)
    conversation_id = await _conversation_id(factory, pk)
    probe["result"] = unavailable(
        category, detail=f"rejected {FAKE_KEY} at https://user:{FAKE_KEY}@api.example.test/v1"
    )

    with caplog.at_level(logging.DEBUG):
        response = await _post(client, conversation_id)

    assert FAKE_KEY not in response.text
    assert FAKE_KEY not in await _everything_persisted(factory)
    assert FAKE_KEY not in caplog.text


async def test_a_secret_in_a_raised_probe_error_is_not_exposed(factory, client, probe, monkeypatch, caplog):
    monkeypatch.setattr(settings, "LLM_API_KEY", FAKE_KEY)
    pk = await _park(factory, monkeypatch, "AUTH_FAILURE")
    conversation_id = await _conversation_id(factory, pk)
    probe["raise"] = RuntimeError(f"connect failed with Authorization: Bearer {FAKE_KEY}")

    with caplog.at_level(logging.DEBUG):
        response = await _post(client, conversation_id)

    assert FAKE_KEY not in response.text
    assert FAKE_KEY not in await _everything_persisted(factory)
    assert FAKE_KEY not in caplog.text


# ----------------------------------------------------------------------------------------
# 5. Authorization, duplicates, retry budget.
# ----------------------------------------------------------------------------------------
async def test_nobody_else_can_trigger_a_model_probe_or_a_resume(factory, client, probe, monkeypatch):
    pk = await _park(factory, monkeypatch, "AUTH_FAILURE")
    conversation_id = await _conversation_id(factory, pk)
    parked = await _snapshot(factory, pk)
    probe["result"] = healthy()

    client.as_caller(BOB)
    assert (await _post(client, conversation_id)).status_code == 404
    client.as_caller(ALICE, READ_ONLY)
    assert (await _post(client, conversation_id)).status_code == 403

    assert probe["calls"] == 0  # an unauthorised caller cannot make the server call the provider
    assert await _snapshot(factory, pk) == parked


async def test_a_duplicate_submission_probes_and_resumes_once(factory, client, probe, monkeypatch):
    pk = await _park(factory, monkeypatch, "AUTH_FAILURE")
    conversation_id = await _conversation_id(factory, pk)
    probe["result"] = healthy()

    first = await _post(client, conversation_id, token="tok-dup")
    again = await _post(client, conversation_id, token="tok-dup")

    assert first.json()["resumed"] is True
    assert again.status_code == 200 and again.json()["resumed"] is False
    assert probe["calls"] == 1
    async with factory() as db:
        to_ready = (await db.scalars(select(TaskEvent).where(TaskEvent.task_id == pk))).all()
        assert len([e for e in to_ready if (e.payload or {}).get("to") == "READY"]) == 1


async def test_a_second_reply_after_the_resume_does_not_queue_another_run(factory, client, probe, monkeypatch):
    pk = await _park(factory, monkeypatch, "AUTH_FAILURE")
    conversation_id = await _conversation_id(factory, pk)
    probe["result"] = healthy()

    assert (await _post(client, conversation_id, token="a")).json()["resumed"] is True
    second = await _post(client, conversation_id, token="b")

    assert second.json()["resumed"] is False
    async with factory() as db:
        events = (await db.scalars(select(TaskEvent).where(TaskEvent.task_id == pk))).all()
        assert len([e for e in events if (e.payload or {}).get("to") == "READY"]) == 1
    assert (await _snapshot(factory, pk))["retry_count"] == 0


async def test_a_failed_check_repeated_with_the_same_token_probes_once(factory, client, probe, monkeypatch):
    pk = await _park(factory, monkeypatch, "AUTH_FAILURE")
    conversation_id = await _conversation_id(factory, pk)
    probe["result"] = unavailable("AUTH_FAILURE")
    await _post(client, conversation_id, token="tok-x")
    await _post(client, conversation_id, token="tok-x")
    assert probe["calls"] == 1
    assert len(await _agent_texts(factory, conversation_id)) == 2  # the request + ONE explanation


async def test_a_working_model_does_not_bypass_an_exhausted_retry_budget(factory, client, probe, monkeypatch):
    pk = await _park(factory, monkeypatch, "AUTH_FAILURE")
    conversation_id = await _conversation_id(factory, pk)
    async with factory() as db:
        task = await db.get(Task, pk)
        task.retry_count = task.max_retries + 1
        await db.commit()
    probe["result"] = healthy()

    response = await _post(client, conversation_id)

    assert response.json()["resumed"] is False
    snapshot = await _snapshot(factory, pk)
    assert snapshot["status"] == TaskStatus.NEEDS_USER_ACTION.value
    assert snapshot["request_status"] == "PENDING"


# ----------------------------------------------------------------------------------------
# 6. Other blockers are untouched: no model probe, same behaviour as before.
# ----------------------------------------------------------------------------------------
async def _park_tool_blocker(factory, text, category):
    async with factory() as db:
        task = Task(task_id="AIR-FV-T", title="t", status=TaskStatus.IN_PROGRESS.value, max_retries=3,
                    owner_email=ALICE)
        db.add(task)
        await db.commit()
        pk = task.id
    async with factory() as db:
        result = await AgentController(db)._request_operator_input(
            await db.get(Task, pk), iteration=1, error_text=text, error_category=category
        )
        assert result is not None
    return pk, result["reason_code"]


async def test_a_tool_auth_failure_is_still_checked_as_a_wallet_not_as_the_model(factory, client, probe):
    pk, reason = await _park_tool_blocker(factory, "Step 2 failed: the site returned 401 unauthorized", "AUTH_FAILURE")
    assert reason == "MISSING_AUTHENTICATION"
    probe["result"] = unavailable("AUTH_FAILURE")  # a broken model must not matter to this blocker

    response = await _post(client, await _conversation_id(factory, pk))

    assert probe["calls"] == 0
    assert response.json()["resumed"] is True  # unchanged behaviour: the operator's confirmation counts


async def test_an_unclassified_tool_blocker_never_touches_the_model(factory, client, probe):
    pk, reason = await _park_tool_blocker(factory, "the page layout changed unexpectedly", "NOT_FOUND")
    assert reason == "UNKNOWN"
    probe["result"] = unavailable("AUTH_FAILURE")

    await _post(client, await _conversation_id(factory, pk))

    assert probe["calls"] == 0
