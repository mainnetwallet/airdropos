"""Resuming a task parked on a model error, through the real Agent Inbox reply endpoint.

The path under test is the production one: HTTP `POST /inbox/conversations/{id}/messages` ->
scope check -> server-side ownership lookup -> `ReplyRouter.handle` -> verification ->
`_resume` -> the task manager's compare-and-set transition -> worker claim -> a planning pass.

Only two things are replaced, both at the edges and neither inside a security check:
  * `get_principal` (bearer-token session lookup) returns a test principal. `require_scopes`,
    the ownership check and every other production dependency run unchanged.
  * the planner and the reply interpreter, which would call the external model.
"""

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from backend.app.agent.controller import AgentController
from backend.app.api.v1.endpoints.inbox import router as inbox_router
from backend.app.database.session import get_db
from backend.app.inbox.interpretation import InterpretationError
from backend.app.llm.client import LLMError
from backend.app.models import AgentConversation, ConversationMessage, InterventionRequest, Task
from backend.app.models.tasks import TaskCheckpoint
from backend.app.security.auth import Principal, Scope, get_principal
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

ALICE = "alice@example.test"
BOB = "bob@example.test"
FULL = frozenset({Scope.READ.value, Scope.AGENT_CHAT.value, Scope.TASK_WRITE.value})
READ_ONLY = frozenset({Scope.READ.value})
RESUME = "I corrected the model settings, resume this task"

AUTH_MSG = "LLM authentication failed (HTTP 401). Verify LLM_API_KEY for the configured endpoint."
NOT_FOUND_MSG = "LLM endpoint or model not found (HTTP 404). Verify LLM_BASE_URL and LLM_MODEL_ID ('gpt-4o')."


class _PlanningReached(BaseException):
    """Raised by the stubbed planner so a pass can be observed without a real model."""


@pytest.fixture
def planner(monkeypatch):
    """Stub the model-backed planner. `calls` records every planning pass by task id."""
    state = {"calls": [], "mode": "refuse", "message": AUTH_MSG, "category": "AUTH_FAILURE"}

    async def stub(self, task, report):
        state["calls"].append(task.task_id)
        if state["mode"] == "refuse":
            raise LLMError(state["message"], category=state["category"], retryable=False)
        raise _PlanningReached(task.task_id)

    monkeypatch.setattr(AgentController, "_ensure_plan", stub)
    return state


@pytest.fixture(autouse=True)
def interpreter_unavailable(monkeypatch):
    """The reply interpreter would call the model; make it unavailable (the router must cope)."""
    from backend.app.inbox import replies

    async def unavailable(self, context, body):
        raise InterpretationError("model not reachable in tests", category="MODEL_UNAVAILABLE")

    monkeypatch.setattr(replies.ReplyInterpreter, "interpret", unavailable)


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


async def _park(factory, task_id, owner=ALICE):
    """Create a task and park it exactly as the worker does: by running it into a model refusal."""
    async with factory() as db:
        task = Task(task_id=task_id, title=f"title {task_id}", status=TaskStatus.IN_PROGRESS.value,
                    max_retries=3, owner_email=owner)
        db.add(task)
        await db.commit()
        pk = task.id
    async with factory() as db:
        await AgentController(db).execute_task(await db.get(Task, pk))
        await db.commit()  # the worker commits after a run; the lease release rides on it
    return pk


async def _conversation_id(factory, task_pk):
    async with factory() as db:
        conversation = await db.scalar(select(AgentConversation).where(AgentConversation.task_id == task_pk))
        return str(conversation.id)


async def _snapshot(factory, pk):
    async with factory() as db:
        task = await db.get(Task, pk)
        checkpoints = (await db.scalars(select(TaskCheckpoint.id).where(TaskCheckpoint.task_id == pk))).all()
        return {
            "status": task.status, "retry_count": task.retry_count, "task_id": task.task_id,
            "checkpoint_ids": set(checkpoints),
        }


async def _count(factory, model):
    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def _post(client, conversation_id, text=RESUME, token=None):
    body = {"message": text}
    if token:
        body["client_token"] = token
    return await client.post(f"/inbox/conversations/{conversation_id}/messages", json=body)


# ----------------------------------------------------------------------------------------
async def test_operator_reply_resumes_the_same_task_and_a_new_planning_pass_runs(
    factory, client, planner
):
    pk = await _park(factory, "AIR-RS-A")
    other_pk = await _park(factory, "AIR-RS-B")  # an unrelated parked task of the same operator
    conversation_id = await _conversation_id(factory, pk)
    other_conversation_id = await _conversation_id(factory, other_pk)

    parked = await _snapshot(factory, pk)
    assert parked["status"] == TaskStatus.NEEDS_USER_ACTION.value
    assert parked["retry_count"] == 0 and parked["checkpoint_ids"]
    async with factory() as db:
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        assert request.reason_code == "LLM_AUTH_FAILURE" and request.status == "PENDING"
    tasks_before = await _count(factory, Task)
    conversations_before = await _count(factory, AgentConversation)

    # --- Nothing runs while the intervention is unresolved. ---------------------------
    assert planner["calls"] == ["AIR-RS-A", "AIR-RS-B"]  # the two initial, refused passes only
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("worker-1") is None

    # --- Wrong operator: indistinguishable from "no such conversation". -----------------
    client.as_caller(BOB)
    async with factory() as db:
        messages_before = await db.scalar(select(func.count()).select_from(ConversationMessage))
    denied = await _post(client, conversation_id)
    assert denied.status_code == 404
    assert (await _post(client, other_conversation_id)).status_code == 404
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(ConversationMessage)) == messages_before

    # --- Right operator, wrong scope. ------------------------------------------------
    client.as_caller(ALICE, READ_ONLY)
    assert (await _post(client, conversation_id)).status_code == 403

    # None of that moved either task or let anything execute.
    assert await _snapshot(factory, pk) == parked
    assert (await _snapshot(factory, other_pk))["status"] == TaskStatus.NEEDS_USER_ACTION.value
    async with factory() as db:
        assert await TaskManager(db).claim_next_task("worker-1") is None
    assert planner["calls"] == ["AIR-RS-A", "AIR-RS-B"]

    # --- The owner replies. ------------------------------------------------------------
    client.as_caller(ALICE)
    response = await _post(client, conversation_id, token="tok-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == "AIR-RS-A"
    assert body["resumed"] is True and body["status"] == "RESUMED"

    resumed = await _snapshot(factory, pk)
    assert resumed["status"] == TaskStatus.READY.value
    assert resumed["task_id"] == "AIR-RS-A"
    assert resumed["retry_count"] == 0  # a resume is not a retry
    assert parked["checkpoint_ids"] <= resumed["checkpoint_ids"]  # persisted checkpoints kept
    async with factory() as db:
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        assert request.status == "RESOLVED" and request.resolved_by == ALICE
        assert request.reason_code == "LLM_AUTH_FAILURE"

    # The unrelated task is untouched, and nothing was duplicated.
    assert (await _snapshot(factory, other_pk))["status"] == TaskStatus.NEEDS_USER_ACTION.value
    assert await _count(factory, Task) == tasks_before
    assert await _count(factory, AgentConversation) == conversations_before
    assert planner["calls"] == ["AIR-RS-A", "AIR-RS-B"]  # replying did not itself run anything

    # --- A repeated submit (same token) cannot queue a second run. ----------------------
    again = await _post(client, conversation_id, token="tok-1")
    assert again.status_code == 200 and again.json()["resumed"] is False
    assert (await _snapshot(factory, pk))["retry_count"] == 0

    # --- The worker claims the SAME task, and the next planning pass runs for it. -------
    planner["mode"] = "reach"
    async with factory() as db:
        claimed = await TaskManager(db).claim_next_task("worker-1")
        await db.commit()  # the worker persists the claim before it starts the run
        assert claimed is not None and claimed.id == pk and claimed.task_id == "AIR-RS-A"
        assert claimed.retry_count == 0
        with pytest.raises(_PlanningReached):
            await AgentController(db).execute_task(claimed)
    assert planner["calls"] == ["AIR-RS-A", "AIR-RS-B", "AIR-RS-A"]
    assert await _count(factory, Task) == tasks_before  # still no new task
    assert (await _snapshot(factory, other_pk))["status"] == TaskStatus.NEEDS_USER_ACTION.value


async def test_a_reply_to_one_task_never_resumes_another(factory, client, planner):
    pk_a = await _park(factory, "AIR-RS-A")
    planner["category"], planner["message"] = "NOT_FOUND", NOT_FOUND_MSG
    pk_b = await _park(factory, "AIR-RS-B")
    conversation_b = await _conversation_id(factory, pk_b)

    response = await _post(client, conversation_b)
    assert response.status_code == 200 and response.json()["task_id"] == "AIR-RS-B"
    assert (await _snapshot(factory, pk_b))["status"] == TaskStatus.READY.value
    assert (await _snapshot(factory, pk_a))["status"] == TaskStatus.NEEDS_USER_ACTION.value
    async with factory() as db:
        claimed = await TaskManager(db).claim_next_task("worker-1")
        assert claimed.id == pk_b
        assert await TaskManager(db).claim_next_task("worker-2") is None  # A is still waiting


async def test_a_refused_resume_leaves_the_request_open_and_the_task_parked(
    factory, client, planner, monkeypatch
):
    pk = await _park(factory, "AIR-RS-A")
    conversation_id = await _conversation_id(factory, pk)
    # Retry budget already spent: the existing resume check must still refuse, not bypass.
    async with factory() as db:
        task = await db.get(Task, pk)
        task.retry_count = task.max_retries + 1
        await db.commit()

    response = await _post(client, conversation_id)
    assert response.status_code == 200 and response.json()["resumed"] is False
    snapshot = await _snapshot(factory, pk)
    assert snapshot["status"] == TaskStatus.NEEDS_USER_ACTION.value
    async with factory() as db:
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        assert request.status == "PENDING"
        assert await TaskManager(db).claim_next_task("worker-1") is None
