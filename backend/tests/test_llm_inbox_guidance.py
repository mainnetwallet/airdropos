"""Agent Inbox guidance for user-fixable model errors.

A task parked because the single configured model refused it must tell the operator what to
check -- using the real setting names -- instead of the generic "I could not determine a fix"
question, and must never be mistaken for a tool/wallet/Telegram blocker. Failures that are not
about the configured model keep their existing classification, and no secret may reach any
persisted or logged text.

These go through the controller's real parking path, so they pin behaviour, not wiring.
"""

import json
import logging

import pytest
from sqlalchemy import select

from backend.app.agent.controller import AgentController
from backend.app.core.config import settings
from backend.app.database.session import Base
from backend.app.inbox.raise_intervention import guidance_for, park_for_intervention
from backend.app.llm.client import LLMError
from backend.app.models import ConversationMessage, InterventionRequest, Task
from backend.app.models.tasks import TaskCheckpoint
from backend.app.tasks.state_machine import TaskStatus

OWNER = "operator@example.test"

# Shaped so the generic redaction patterns (sk-..., long hex) do NOT match it: only an exact
# scrub of the configured value can keep it out of a message the provider echoed back.
FAKE_KEY = "zz_live_FAKEKEY_0123456789abcdefXYZ"

# (category, the message llm/client.py really raises, the intended inbox reason)
CASES = {
    "AUTH_FAILURE": (
        "LLM authentication failed (HTTP 401). Verify LLM_API_KEY for the configured endpoint.",
        "LLM_AUTH_FAILURE",
    ),
    "BILLING_REFUSAL": (
        "The LLM endpoint refused the request for lack of credit.",
        "LLM_BILLING_REFUSAL",
    ),
    "NOT_FOUND": (
        "LLM endpoint or model not found (HTTP 404). Verify LLM_BASE_URL and LLM_MODEL_ID ('gpt-4o').",
        "LLM_NOT_FOUND",
    ),
    "MODEL_MISMATCH": (
        "The LLM endpoint served 'other-model' but 'gpt-4o' is required. "
        "Refusing the response under LLM_REQUIRE_SERVED_MODEL_MATCH.",
        "LLM_MODEL_MISMATCH",
    ),
}

# What each reason's guidance must name, taken from the real configuration (config.py).
MUST_MENTION = {
    "LLM_AUTH_FAILURE": ["LLM_API_KEY"],
    "LLM_BILLING_REFUSAL": ["billing"],
    "LLM_NOT_FOUND": ["LLM_BASE_URL", "LLM_MODEL_ID"],
    "LLM_MODEL_MISMATCH": ["LLM_MODEL_ID", "LLM_REQUIRE_SERVED_MODEL_MATCH"],
}

# Nothing here may suggest leaving the single fixed model, or point at an unrelated subsystem.
NEVER_MENTION = ["fallback", "switch", "wallet", "telegram", "browser"]


async def _park_through_controller(factory, monkeypatch, category, message, *, task_id="AIR-LG-1"):
    async with factory() as db:
        task = Task(
            task_id=task_id, title="guidance task", status=TaskStatus.IN_PROGRESS.value,
            max_retries=3, owner_email=OWNER,
        )
        db.add(task)
        await db.commit()
        pk = task.id

    async def broken(self, task, report):
        raise LLMError(message, category=category, retryable=False)

    monkeypatch.setattr(AgentController, "_ensure_plan", broken)
    async with factory() as db:
        task = await db.get(Task, pk)
        await AgentController(db).execute_task(task)
    return pk


async def _inbox_state(factory, pk):
    async with factory() as db:
        request = await db.scalar(select(InterventionRequest).where(InterventionRequest.task_id == pk))
        messages = (
            await db.scalars(
                select(ConversationMessage).where(ConversationMessage.task_id == pk)
            )
        ).all() if request is not None else []
        agent_text = "\n".join(m.content for m in messages)
        return request, agent_text


def _guidance_text(request, agent_text):
    return " ".join(
        [request.required_action or "", " ".join(request.instructions or []), agent_text]
    )


# ----------------------------------------------------------------------------------------
# 1. Each supported model error maps to its own reason with matching guidance.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("category", list(CASES))
async def test_model_error_is_classified_to_its_own_reason(factory, monkeypatch, category):
    message, expected = CASES[category]
    pk = await _park_through_controller(factory, monkeypatch, category, message)
    request, _ = await _inbox_state(factory, pk)
    assert request is not None, "a user-fixable model error must raise an inbox request"
    assert request.reason_code == expected
    assert request.error_category == category


@pytest.mark.parametrize("category", list(CASES))
async def test_guidance_names_the_right_configuration(factory, monkeypatch, category):
    message, expected = CASES[category]
    pk = await _park_through_controller(factory, monkeypatch, category, message)
    request, agent_text = await _inbox_state(factory, pk)
    text = _guidance_text(request, agent_text)
    lowered = text.lower()

    for needle in MUST_MENTION[expected]:
        assert needle.lower() in lowered, f"{expected} guidance should mention {needle!r}"
    for needle in NEVER_MENTION:
        assert needle not in lowered, f"{expected} guidance must not mention {needle!r}"
    assert "could not determine a specific fix" not in lowered  # not the generic fallback


@pytest.mark.parametrize("reason", sorted(MUST_MENTION))
def test_guidance_for_each_llm_reason_is_specific(reason):
    guidance = guidance_for(reason, "", None)
    text = json.dumps(guidance).lower()
    for needle in MUST_MENTION[reason]:
        assert needle.lower() in text
    assert "question" not in guidance, "a known blocker gets instructions, not a diagnostic question"
    assert guidance["instructions"], "guidance must be actionable"


def test_billing_guidance_is_about_the_provider_account_not_a_setting():
    text = json.dumps(guidance_for("LLM_BILLING_REFUSAL", "", None)).lower()
    assert "credit" in text or "quota" in text
    assert "provider" in text
    assert "llm_model_id" not in text  # a billing problem is not fixed by changing the model


def test_model_mismatch_guidance_explains_served_versus_configured():
    text = json.dumps(guidance_for("LLM_MODEL_MISMATCH", "", None)).lower()
    assert "served" in text and "configured" in text


# ----------------------------------------------------------------------------------------
# 2. NOT_FOUND and MODEL_MISMATCH stay distinct from each other and from tool blockers.
# ----------------------------------------------------------------------------------------
async def test_not_found_and_model_mismatch_are_not_confused(factory, monkeypatch):
    pk_a = await _park_through_controller(
        factory, monkeypatch, "NOT_FOUND", CASES["NOT_FOUND"][0], task_id="AIR-LG-A"
    )
    pk_b = await _park_through_controller(
        factory, monkeypatch, "MODEL_MISMATCH", CASES["MODEL_MISMATCH"][0], task_id="AIR-LG-B"
    )
    request_a, _ = await _inbox_state(factory, pk_a)
    request_b, _ = await _inbox_state(factory, pk_b)
    assert request_a.reason_code == "LLM_NOT_FOUND"
    assert request_b.reason_code == "LLM_MODEL_MISMATCH"


async def test_model_error_text_cannot_pull_the_task_into_another_reason(factory, monkeypatch):
    # Words that the text signatures key on (401, forbidden, mfa, cdp) appear in the model
    # error. The category from the model client is authoritative, not those substrings.
    message = (
        "LLM authentication failed (HTTP 401 forbidden). Verify LLM_API_KEY; "
        "the gateway mentioned a captcha, mfa and cdp."
    )
    pk = await _park_through_controller(factory, monkeypatch, "AUTH_FAILURE", message)
    request, _ = await _inbox_state(factory, pk)
    assert request.reason_code == "LLM_AUTH_FAILURE"


# ----------------------------------------------------------------------------------------
# 3. Unrelated and generic errors keep their existing, safe handling.
# ----------------------------------------------------------------------------------------
async def test_tool_not_found_is_not_treated_as_a_model_endpoint_problem(factory):
    async with factory() as db:
        task = Task(task_id="AIR-LG-T1", title="t", status=TaskStatus.IN_PROGRESS.value, owner_email=OWNER)
        db.add(task)
        await db.commit()
        result = await park_for_intervention(
            db, task, owner_email=OWNER,
            error_text="Telegram reports this invitation link is invalid.",
            error_category="NOT_FOUND",
        )
        pk = task.id
    assert not result["reason_code"].startswith("LLM_")
    request, agent_text = await _inbox_state(factory, pk)
    assert "LLM_BASE_URL" not in _guidance_text(request, agent_text)


async def test_tool_auth_failure_keeps_its_existing_reason(factory):
    async with factory() as db:
        task = Task(task_id="AIR-LG-T2", title="t", status=TaskStatus.IN_PROGRESS.value, owner_email=OWNER)
        db.add(task)
        await db.commit()
        result = await park_for_intervention(
            db, task, owner_email=OWNER,
            error_text="The airdrop site answered HTTP 401 for the saved session.",
            error_category="AUTH_FAILURE",
        )
    assert result["reason_code"] == "MISSING_AUTHENTICATION"


async def test_unrecognised_failure_still_gets_the_safe_generic_guidance(factory):
    async with factory() as db:
        task = Task(task_id="AIR-LG-T3", title="t", status=TaskStatus.IN_PROGRESS.value, owner_email=OWNER)
        db.add(task)
        await db.commit()
        result = await park_for_intervention(
            db, task, owner_email=OWNER, error_text="Something unexpected happened in step 3."
        )
        pk = task.id
    assert result["reason_code"] == "UNKNOWN"
    request, agent_text = await _inbox_state(factory, pk)
    text = _guidance_text(request, agent_text)
    assert "could not determine a specific fix" in text
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID"):
        assert name not in text


@pytest.mark.parametrize("category", ["CONTEXT_OVERFLOW", "MALFORMED_RESPONSE", "EMPTY_CONTENT"])
async def test_unfixable_model_errors_are_not_presented_as_configuration_problems(
    factory, monkeypatch, category
):
    pk = await _park_through_controller(
        factory, monkeypatch, category, f"provider said no ({category})"
    )
    request, _ = await _inbox_state(factory, pk)
    assert request is None
    async with factory() as db:
        assert (await db.get(Task, pk)).status == TaskStatus.FAILED.value


async def test_retryable_error_stays_on_the_backoff_path_even_with_a_fixable_category(
    factory, monkeypatch
):
    async with factory() as db:
        task = Task(task_id="AIR-LG-R1", title="t", status=TaskStatus.IN_PROGRESS.value,
                    max_retries=3, owner_email=OWNER)
        db.add(task)
        await db.commit()
        pk = task.id

    async def broken(self, task, report):
        raise LLMError("transient auth hiccup", category="AUTH_FAILURE", retryable=True)

    monkeypatch.setattr(AgentController, "_ensure_plan", broken)
    async with factory() as db:
        await AgentController(db).execute_task(await db.get(Task, pk))
    async with factory() as db:
        task = await db.get(Task, pk)
        assert task.status == TaskStatus.RETRYING.value
        assert task.retry_count == 1 and task.next_execution_at is not None
    request, _ = await _inbox_state(factory, pk)
    assert request is None


# ----------------------------------------------------------------------------------------
# 4. Preserve checkpoint, identity and retry accounting while classifying.
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("category", list(CASES))
async def test_classification_keeps_task_identity_checkpoint_and_retry_budget(
    factory, monkeypatch, category
):
    message, expected = CASES[category]
    pk = await _park_through_controller(factory, monkeypatch, category, message)
    async with factory() as db:
        task = await db.get(Task, pk)
        assert task.task_id == "AIR-LG-1"
        assert task.status == TaskStatus.NEEDS_USER_ACTION.value
        assert task.retry_count == 0 and task.next_execution_at is None
        names = [
            c.checkpoint_name
            for c in (await db.scalars(select(TaskCheckpoint).where(TaskCheckpoint.task_id == pk))).all()
        ]
        assert f"awaiting_operator:{expected.lower()}" in names


# ----------------------------------------------------------------------------------------
# 5. No secret reaches any persisted or logged text.
# ----------------------------------------------------------------------------------------
async def _everything_persisted(factory) -> str:
    """Every column of every row in the database, as text."""
    chunks = []
    async with factory() as db:
        for table in Base.metadata.sorted_tables:
            for row in (await db.execute(select(table))).mappings().all():
                chunks.append(" ".join(str(v) for v in row.values() if v is not None))
    return "\n".join(chunks)


@pytest.mark.parametrize("category", list(CASES))
async def test_a_secret_echoed_by_the_provider_never_reaches_the_inbox_or_logs(
    factory, monkeypatch, caplog, category
):
    monkeypatch.setattr(settings, "LLM_API_KEY", FAKE_KEY)
    message, _ = CASES[category]
    echoed = f"{message} Upstream said: invalid key {FAKE_KEY} for this account."
    with caplog.at_level(logging.DEBUG):
        await _park_through_controller(factory, monkeypatch, category, echoed)

    assert FAKE_KEY not in await _everything_persisted(factory)
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize("reason", sorted(MUST_MENTION))
def test_guidance_never_embeds_the_configured_key(monkeypatch, reason):
    monkeypatch.setattr(settings, "LLM_API_KEY", FAKE_KEY)
    assert FAKE_KEY not in json.dumps(guidance_for(reason, "", None))
