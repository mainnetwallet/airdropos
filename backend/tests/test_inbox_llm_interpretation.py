"""Agent Inbox: the operator's reply is understood by the fixed LLM, then checked and applied by code.

What these lock down:

* a useful reply (Bengali, Banglish or English) resumes a parked task WITHOUT a magic phrase;
* an insufficient reply gets a specific follow-up question and the task stays parked;
* the model only proposes: a claimed login still has to pass the real session check, an
  operator pause or approval cannot be cleared by information, and a model cannot cancel;
* the clarification is persisted before the requeue and reaches the worker's next planning call;
* completed work and the checkpoint survive the resume;
* LLM failure, malformed output, secrets and invented values leave the task safely parked;
* a repeated client token never costs a second LLM call or a second worker.

The LLM is replaced with `FakeInterpretationLLM`; nothing here contacts a model, Telegram or a chain.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import select

from backend.app.inbox import interpretation as interpretation_module
from backend.app.inbox import verification as verification_module
from backend.app.inbox.replies import _AUTHORISE_PATTERNS, ReplyRouter, _matches
from backend.app.inbox.service import (
    AMBIGUOUS_INSTRUCTION,
    APPROVAL_GATED,
    HUMAN_APPROVAL,
    INFORMATION_RESOLVABLE,
    MACHINE_VERIFIABLE,
    OPERATOR_PAUSE,
    REQUIRES_EXPLICIT_AUTHORIZATION,
    TELEGRAM_SESSION_UNAVAILABLE,
    UNKNOWN_REASON,
    ConversationService,
    InterventionService,
    normalise_reply,
)
from backend.app.inbox.verification import VerificationResult
from backend.app.llm.client import LLMError
from backend.app.models import AgentConversation, Task
from backend.app.tasks.checkpoints import CheckpointManager
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus
from backend.tests.fake_llm import FakeInterpretationLLM, decision

# Shared harness. Imported by name so pytest registers the fixtures in this module.
from backend.tests.test_agent_inbox import (  # noqa: F401
    OPERATOR_A,
    OPERATOR_B,
    FakeTelegram,
    _client,
    _first_conversation,
    _make_task,
    _park,
    factory,
    operator_a,
    operator_b,
    telegram,
)


# ======================================================================
# Helpers
# ======================================================================
def install_llm(monkeypatch, answer) -> FakeInterpretationLLM:
    fake = FakeInterpretationLLM(answer)
    monkeypatch.setattr(interpretation_module, "llm", fake)
    return fake


async def reply(
    factory, task_id: str, text: str, *, token: Optional[str] = None, owner: str = OPERATOR_A
):
    async with factory() as db:
        conversation = await _first_conversation(db, task_id)
        task = await TaskManager(db).get_by_task_id(task_id)
        outcome = await ReplyRouter(db).handle(
            conversation=conversation, task=task, owner_email=owner, text=text, client_token=token
        )
        await db.commit()
        return outcome


async def snapshot(factory, task_id: str) -> Dict[str, Any]:
    async with factory() as db:
        task = await TaskManager(db).get_by_task_id(task_id)
        request = await InterventionService(db).active_for_task(task.id)
        conversation = await _first_conversation(db, task_id)
        messages = await ConversationService(db).list_messages(conversation.id)
        return {
            "status": task.status,
            "progress": task.progress,
            "retry_count": task.retry_count,
            "instructions": list(task.operator_instructions or []),
            "completed_actions": list(task.completed_actions or []),
            "open_request": request,
            "messages": messages,
        }


async def park_missing_information(factory, task_id: str):
    """A task that stopped because it does not know which network to use."""
    return await _park(
        factory, task_id,
        error_text="Ambiguous instruction: unclear which network to use.",
        error_category=None, force_reason_code=AMBIGUOUS_INSTRUCTION, integration_ref=None,
    )


async def park_pause(factory, task_id: str):
    return await _park(
        factory, task_id, error_text="Emergency stop engaged; execution halted.",
        error_category=None, force_reason_code=OPERATOR_PAUSE, integration_ref=None,
    )


async def park_approval(factory, task_id: str):
    return await _park(
        factory, task_id, error_text="Approval required to sign this transaction.",
        error_category=None, force_reason_code=HUMAN_APPROVAL, integration_ref=None,
    )


def agent_text(snap: Dict[str, Any]) -> str:
    return "\n".join(m.content for m in snap["messages"] if m.role == "assistant")


@pytest.fixture
def verifier_ok(monkeypatch):
    async def _verify(db, task, request):
        return VerificationResult.satisfied(method="test_double", outcome="The double confirms it.")

    monkeypatch.setitem(verification_module._REGISTRY, TELEGRAM_SESSION_UNAVAILABLE, _verify)


@pytest.fixture
def verifier_unchecked(monkeypatch):
    async def _verify(db, task, request):
        return VerificationResult.unchecked(
            method="test_double", outcome="The gateway is not reachable.",
            question="Please tell me what you did and what you saw.",
        )

    monkeypatch.setitem(verification_module._REGISTRY, TELEGRAM_SESSION_UNAVAILABLE, _verify)


@pytest.fixture
def verifier_failed(monkeypatch):
    async def _verify(db, task, request):
        return VerificationResult.failed(
            method="test_double", outcome="Still signed out.",
            question="Please sign the session in, then tell me to re-check.",
        )

    monkeypatch.setitem(verification_module._REGISTRY, TELEGRAM_SESSION_UNAVAILABLE, _verify)


# ======================================================================
# 1-2. A useful reply resumes the task, in either language, with no magic phrase
# ======================================================================
class TestUsefulRepliesResume:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text, extracted",
        [
            # Bengali: "I want you to do this task on the Arbitrum network."
            ("আমি চাই তুমি কাজটা Arbitrum নেটওয়ার্কে করো", "Run the task on the Arbitrum network"),
            # Banglish.
            ("task ta Arbitrum network e koro, ar kichu lagbe na", "Run the task on the Arbitrum network"),
            # English.
            ("The campaign lives on the Base network, and please use my second account.",
             "Network: Base; use the operator's second account"),
        ],
    )
    async def test_a_useful_reply_supplies_the_missing_information(
        self, factory, monkeypatch, text, extracted
    ):
        assert not _matches(_AUTHORISE_PATTERNS, normalise_reply(text)), (
            "this test is only meaningful if the reply contains no magic resume phrase"
        )
        fake = install_llm(
            monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=[extracted])
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, text)

        assert outcome.status == "RESUMED" and outcome.resumed is True
        assert fake.call_count == 1, "the fixed LLM must actually be consulted"
        assert fake.calls[0]["operator_reply"] == text
        assert outcome.interpretation["decision"] == "SUFFICIENT_INFORMATION"

        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.READY.value
        assert snap["open_request"] is None, "the request closes once the task is requeued"
        assert len(snap["instructions"]) == 1
        assert extracted in snap["instructions"][0]["instruction"]
        assert snap["instructions"][0]["source"] == "llm_interpreted_reply"

    @pytest.mark.asyncio
    async def test_a_missing_url_given_in_plain_words_resumes_an_unclassified_block(
        self, factory, monkeypatch
    ):
        url = "https://example.org/airdrop/claim"
        install_llm(
            monkeypatch,
            decision("SUFFICIENT_INFORMATION", extracted=[f"The claim page is {url}"]),
        )
        task_id = await _make_task(factory)
        await _park(
            factory, task_id, error_text="Something went sideways in a way nobody has classified",
            error_category=None, integration_ref=None,
        )
        outcome = await reply(factory, task_id, f"the claim page is {url} go on from there")
        assert outcome.status == "RESUMED"
        snap = await snapshot(factory, task_id)
        assert url in snap["instructions"][0]["instruction"]


# ======================================================================
# 3. An insufficient reply gets a specific question and changes nothing
# ======================================================================
class TestInsufficientReplies:
    @pytest.mark.asyncio
    async def test_needs_clarification_asks_the_specific_question(self, factory, monkeypatch):
        fake = install_llm(
            monkeypatch,
            decision(
                "NEEDS_CLARIFICATION",
                explanation="You named a chain but not which account.",
                missing=["Which of your two wallets should I use on Base?"],
            ),
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "use base")

        assert outcome.status == "NEEDS_MORE_INFO" and outcome.resumed is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["open_request"] is not None, "the request stays open"
        assert snap["instructions"] == [], "a half answer is not saved as an instruction"
        assert "Which of your two wallets should I use on Base?" in agent_text(snap)
        assert fake.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["hello there", "ok", "ধন্যবাদ"])
    async def test_unrelated_or_empty_replies_do_not_resume(self, factory, monkeypatch, text):
        install_llm(monkeypatch, decision("UNRELATED_OR_INSUFFICIENT_REPLY"))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, text)

        assert outcome.resumed is False and outcome.status == "NEEDS_MORE_INFO"
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["open_request"] is not None
        assert "still parked" in agent_text(snap)


# ======================================================================
# 4. A reported login is a claim; the real check decides
# ======================================================================
class TestClaimsAreVerifiedForReal:
    @pytest.mark.asyncio
    async def test_a_login_claim_is_checked_by_the_real_telegram_readiness_check(
        self, factory, monkeypatch
    ):
        """No verifier double: the real `_verify_telegram_session` runs and finds Telegram unset."""
        install_llm(monkeypatch, decision("BLOCKER_REQUIRES_VERIFICATION"))
        task_id = await _make_task(factory)
        await _park(factory, task_id)  # TELEGRAM_SESSION_UNAVAILABLE

        outcome = await reply(
            factory, task_id, "Telegram login complete kore disi, ekhon task continue koro."
        )

        # The real check still runs and its finding is kept, but it no longer holds the task
        # back: the operator's reply is the confirmation.
        assert outcome.resumed is True and outcome.status == "RESUMED"
        assert outcome.verification["method"] == "operator_confirmed"
        assert outcome.verification["detail"]["automatic_check"] == "telegram_session_readiness", (
            "the real check must have run, not a hard-coded pass"
        )
        assert outcome.verification["detail"]["automatic_check_ok"] is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.READY.value
        assert snap["open_request"] is None

    @pytest.mark.asyncio
    async def test_sufficient_information_resumes_even_when_the_check_fails(
        self, factory, monkeypatch, verifier_failed
    ):
        install_llm(
            monkeypatch,
            decision("SUFFICIENT_INFORMATION", extracted=["I signed the agent account in"]),
        )
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        outcome = await reply(factory, task_id, "I signed the agent account in just now")

        assert outcome.resumed is True
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.READY.value
        assert "still blocked" not in agent_text(snap).lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["BLOCKER_REQUIRES_VERIFICATION", "SUFFICIENT_INFORMATION"])
    async def test_an_uncheckable_session_blocker_does_not_hold_the_task_back(
        self, factory, monkeypatch, verifier_unchecked, kind
    ):
        """If the platform cannot look, the operator's word is taken: the task resumes."""
        install_llm(
            monkeypatch,
            decision(kind, extracted=["Logged in to Telegram"] if kind.startswith("SUFF") else []),
        )
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        outcome = await reply(factory, task_id, "I have logged in to Telegram already, please go on")

        assert outcome.resumed is True
        assert outcome.verification["method"] == "operator_confirmed"
        assert outcome.verification["detail"]["automatic_check_checkable"] is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.READY.value

    @pytest.mark.asyncio
    async def test_a_passing_check_after_a_claim_resumes(
        self, factory, monkeypatch, verifier_ok
    ):
        install_llm(monkeypatch, decision("BLOCKER_REQUIRES_VERIFICATION"))
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        outcome = await reply(factory, task_id, "Telegram login complete kore disi")

        assert outcome.status == "RESUMED"
        assert outcome.verification["ok"] is True
        assert (await snapshot(factory, task_id))["status"] == TaskStatus.READY.value

    @pytest.mark.asyncio
    async def test_the_spec_example_end_to_end_over_http(
        self, factory, operator_a, monkeypatch, telegram
    ):
        install_llm(monkeypatch, decision("BLOCKER_REQUIRES_VERIFICATION"))
        task_id = await _make_task(factory)
        await _park(factory, task_id)
        async with factory() as db:
            conversation_id = str((await _first_conversation(db, task_id)).id)
        text = "Telegram login complete kore disi, ekhon task continue koro."
        url = f"/api/v1/inbox/conversations/{conversation_id}/messages"

        # Telegram is NOT signed in here, and the reply still resumes the task.
        async with _client(operator_a) as client:
            after = (await client.post(url, json={"message": text, "client_token": "t-1"})).json()
        assert after["status"] == "RESUMED" and after["resumed"] is True
        assert after["verification"]["method"] == "operator_confirmed"
        assert after["interpretation"]["decision"] == "BLOCKER_REQUIRES_VERIFICATION"

    @pytest.mark.asyncio
    async def test_a_human_verification_reply_resumes_even_with_no_session_on_record(
        self, factory, monkeypatch
    ):
        """The reported bug: 'I am complete the verification' got 'still blocked / NOT_CHECKED'."""
        install_llm(monkeypatch, decision("NEEDS_CLARIFICATION"))
        task_id = await _make_task(factory)
        await _park(
            factory, task_id,
            error_text="CAPTCHA required: the operator must complete human verification.",
            error_category=None, force_reason_code="HUMAN_VERIFICATION", integration_ref=None,
        )

        outcome = await reply(factory, task_id, "i am complete the verification")

        assert outcome.resumed is True and outcome.status == "RESUMED"
        assert outcome.verification["method"] == "operator_confirmed"
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.READY.value
        assert snap["open_request"] is None
        text = agent_text(snap).lower()
        assert "still blocked" not in text and "not_checked" not in text
        assert any(
            "completed the manual step" in str(i.get("instruction", ""))
            for i in snap["instructions"]
        ), "the planner must be told the manual step was done"

    @pytest.mark.asyncio
    async def test_a_missing_information_blocker_still_needs_the_actual_answer(
        self, factory, monkeypatch
    ):
        """Only action blockers take the operator's word; an information blocker needs the info."""
        install_llm(monkeypatch, decision("UNRELATED_OR_INSUFFICIENT_REPLY"))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "ok done")

        assert outcome.resumed is False
        assert (await snapshot(factory, task_id))["status"] == TaskStatus.NEEDS_USER_ACTION.value


# ======================================================================
# 5-6. Resuming keeps the work and the planner receives the answer
# ======================================================================
class TestResumePreservesWorkAndReachesThePlanner:
    @pytest.mark.asyncio
    async def test_resume_is_from_the_checkpoint_and_keeps_completed_actions(
        self, factory, monkeypatch
    ):
        fake = install_llm(
            monkeypatch,
            decision("SUFFICIENT_INFORMATION", extracted=["Use the Arbitrum network"]),
        )
        task_id = await _make_task(factory, progress=0.5)
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            task.completed_actions = [
                {"id": "join-channel", "summary": "Joined the channel"},
                {"id": "read-rules", "summary": "Read the rules"},
            ]
            await db.commit()
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "ei kaj ta Arbitrum e koro")
        assert outcome.status == "RESUMED"

        # The model was shown the saved progress, not just the reply.
        assert fake.calls[0]["checkpoint"]["completed_action_ids"] == ["join-channel", "read-rules"]
        assert fake.calls[0]["intervention"]["reason_code"] == AMBIGUOUS_INSTRUCTION

        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.READY.value
        assert [a["id"] for a in snap["completed_actions"]] == ["join-channel", "read-rules"]
        assert snap["progress"] == 0.5, "progress must not be reset by the requeue"
        assert snap["retry_count"] == 0, "an answered question is not a retry"

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            checkpoint = await CheckpointManager(db).load_latest(task)
            assert checkpoint is not None
            assert checkpoint.state["completed_action_ids"] == ["join-channel", "read-rules"]
            assert CheckpointManager.should_resume(task.status)
        assert "Resuming from checkpoint" in agent_text(snap)

    @pytest.mark.asyncio
    async def test_the_next_planning_call_receives_the_operators_answer(
        self, factory, monkeypatch
    ):
        """Reply -> persisted instruction -> the controller's real _ensure_plan -> planner prompt."""
        from backend.app.agent.controller import AgentController, LoopOutcome, LoopReport
        from backend.app.planning import planner as planner_module
        from backend.app.planning.planner import ExecutionPlanSpec, PlanStep, Planner, StepKind

        install_llm(
            monkeypatch,
            decision("SUFFICIENT_INFORMATION", extracted=["Run the task on the Arbitrum network"]),
        )
        task_id = await _make_task(factory)

        # The task already has a plan with one finished step and one still to do.
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            task.completed_actions = [{"id": "join-channel", "summary": "Joined the channel"}]
            spec = ExecutionPlanSpec(
                objective="Join the channel, then claim on the right network",
                steps=[
                    PlanStep(index=0, description="Join the channel", kind=StepKind.RESEARCH,
                             status="completed"),
                    PlanStep(index=1, description="Claim on the chosen network",
                             kind=StepKind.RESEARCH, depends_on=[0], status="pending"),
                ],
            )
            await Planner(db).persist_plan(task, spec)
            await db.commit()
        await park_missing_information(factory, task_id)

        assert (await reply(factory, task_id, "ei kaj ta Arbitrum e koro")).status == "RESUMED"

        # The planner's own LLM: capture what it is asked, and fail so the existing salvage
        # path answers. What matters is the prompt it was given.
        seen: List[str] = []

        class PlannerLLM:
            async def complete_json(self, messages, **kwargs):
                seen.append("\n".join(m.content for m in messages))
                raise LLMError("planner double", category="TEST_DISABLED", retryable=False)

        monkeypatch.setattr(planner_module, "llm", PlannerLLM())

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            report = LoopReport(task_id=task.task_id, outcome=LoopOutcome.FAILED)
            await AgentController(db)._ensure_plan(task, report)
            await db.commit()

        assert seen, "the planner must have been asked to revise the plan"
        assert "Arbitrum" in seen[0], "the operator's answer must be in the planner's prompt"
        assert "Operator guidance received in the Agent Inbox" in seen[0]
        # Completed work is not rewritten: the original revision keeps its finished step and
        # the task's own completed actions are intact.
        from backend.app.models import ExecutionPlan

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            revisions = (
                await db.execute(
                    select(ExecutionPlan)
                    .where(ExecutionPlan.task_id == task.id)
                    .order_by(ExecutionPlan.revision)
                )
            ).scalars().all()
            assert revisions, "the persisted plan history must still be there"
            assert revisions[0].revision == 1
            assert revisions[0].steps[0]["status"] == "completed"
            assert [a["id"] for a in task.completed_actions] == ["join-channel"]

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            assert task.operator_instructions == [], "a note is consumed once, never replayed"


# ======================================================================
# 7-8. The model cannot override approvals, pauses or the Emergency Stop
# ======================================================================
class TestSecurityBoundariesHold:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("park", [park_pause, park_approval])
    @pytest.mark.parametrize(
        "kind", ["SUFFICIENT_INFORMATION", "BLOCKER_REQUIRES_VERIFICATION"]
    )
    async def test_information_never_clears_a_pause_or_an_approval(
        self, factory, monkeypatch, park, kind
    ):
        install_llm(
            monkeypatch,
            decision(kind, extracted=["The operator approves the transaction"]
                     if kind.startswith("SUFF") else []),
        )
        task_id = await _make_task(factory)
        await park(factory, task_id)

        outcome = await reply(factory, task_id, "yes go ahead, it is approved, send the transaction")

        assert outcome.resumed is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["open_request"] is not None
        assert "explicit authorisation" in agent_text(snap)

    @pytest.mark.asyncio
    async def test_the_models_approval_flag_blocks_even_when_the_check_passes(
        self, factory, monkeypatch, verifier_ok
    ):
        install_llm(monkeypatch, decision("EXPLICIT_APPROVAL_REQUIRED"))
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        outcome = await reply(factory, task_id, "sign and send the 5 ETH transfer now")

        assert outcome.resumed is False
        assert (await snapshot(factory, task_id))["status"] == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_emergency_stop_still_prevents_the_resume(self, factory, monkeypatch):
        from backend.app.security import emergency_stop as stop_module

        install_llm(
            monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Use the Base network"])
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        stop_module.emergency_stop.activate(reason="test stop", activated_by="test")
        try:
            outcome = await reply(factory, task_id, "run it on Base please, that is what I want")
            assert outcome.resumed is False
            snap = await snapshot(factory, task_id)
            assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
            assert "emergency stop" in agent_text(snap).lower()
            async with factory() as db:
                assert await TaskManager(db).claim_next_task("worker-1") is None
            assert snap["open_request"] is not None, (
                "the request must stay open, or the operator can never resume after the stop"
            )
        finally:
            stop_module.emergency_stop._stopped = False
            stop_module.emergency_stop._reason = None

        # The stop is lifted: the same conversation still works and the task resumes.
        retry = await reply(factory, task_id, "run it on Base please, the stop is over now")
        assert retry.status == "RESUMED"
        assert (await snapshot(factory, task_id))["status"] == TaskStatus.READY.value


# ======================================================================
# 9. LLM errors and malformed output leave the task safely parked
# ======================================================================
_BAD_ANSWERS = {
    "transport_error": LLMError("down", category="CONNECTION_ERROR"),
    "timeout": LLMError("slow", category="TIMEOUT"),
    "prose_not_json": "Sure! I think the task should resume now.",
    "unexpected_exception": RuntimeError("boom"),
    "missing_decision": {"explanation": "x", "extracted_information": []},
    "unknown_decision": decision("RESUME_NOW"),
    "sufficient_without_content": decision("SUFFICIENT_INFORMATION", extracted=[]),
    "wrong_types": {"decision": "SUFFICIENT_INFORMATION", "explanation": "x",
                    "extracted_information": "use base"},
    "oversized_item": decision("SUFFICIENT_INFORMATION", extracted=["x" * 400]),
    "invented_link": decision(
        "SUFFICIENT_INFORMATION", extracted=["The page is https://evil.example/claim"]
    ),
    "secret_in_output": decision(
        "SUFFICIENT_INFORMATION", extracted=["use key 0x" + "a" * 64]
    ),
    # Unknown fields are rejected, not silently dropped (strict schema, extra="forbid").
    "unknown_extra_field": {
        **decision("SUFFICIENT_INFORMATION", extracted=["Base network"]),
        "approve_and_resume": True,
    },
    # A value the operator never wrote (their reply says Base, not Polygon).
    "invented_network": decision(
        "SUFFICIENT_INFORMATION", extracted=["Use the Polygon network"]
    ),
    "invented_number": decision(
        "SUFFICIENT_INFORMATION", extracted=["Base network, 5 transactions"]
    ),
}


class TestFailuresLeaveTheTaskParked:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(_BAD_ANSWERS))
    async def test_a_bad_model_answer_parks_the_task_and_keeps_the_message(
        self, factory, monkeypatch, name
    ):
        install_llm(monkeypatch, _BAD_ANSWERS[name])
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "ei kaj ta Base network e koro, tomar kono somossya nai")

        assert outcome.resumed is False and outcome.status == "NEEDS_MORE_INFO"
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["open_request"] is not None
        assert snap["instructions"] == [], "nothing from a bad answer reaches the planner"
        users = [m for m in snap["messages"] if m.role == "user"]
        assert [m.content for m in users] == ["ei kaj ta Base network e koro, tomar kono somossya nai"]
        assert "could not interpret" in agent_text(snap)

    @pytest.mark.asyncio
    async def test_a_bare_acknowledgement_cannot_become_a_stored_instruction(
        self, factory, monkeypatch
    ):
        """The model may not turn "ok" into "Use the Polygon network" and save it."""
        install_llm(
            monkeypatch,
            decision("SUFFICIENT_INFORMATION", extracted=["Use the Polygon network"]),
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "ok")

        assert outcome.resumed is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["open_request"] is not None
        assert snap["instructions"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text, extracted",
        [
            ("Base network e koro", ["Base network"]),
            ("use the second account please", ["Use the second account"]),
            ("yes go ahead, it is approved", ["The operator approved it"]),
        ],
    )
    async def test_information_grounded_in_the_reply_is_still_accepted(
        self, factory, monkeypatch, text, extracted
    ):
        install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=extracted))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        await reply(factory, task_id, text)

        snap = await snapshot(factory, task_id)
        assert snap["instructions"], "a grounded answer must still reach the planner"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parker", [park_missing_information, park_pause, park_approval])
    async def test_an_explicit_resume_carrying_a_secret_does_not_resume(
        self, factory, monkeypatch, parker
    ):
        fake = install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["x"]))
        task_id = await _make_task(factory)
        await parker(factory, task_id)

        outcome = await reply(
            factory, task_id, "resume this task, my private key is 0x" + "b" * 64
        )

        assert fake.call_count == 0
        assert outcome.resumed is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["open_request"] is not None
        assert snap["instructions"] == []
        assert "secret" in agent_text(snap)

    @pytest.mark.asyncio
    async def test_the_message_survives_even_if_processing_is_interrupted(
        self, factory, monkeypatch
    ):
        """A client disconnect mid-call must not lose what the operator wrote."""
        install_llm(monkeypatch, asyncio.CancelledError())
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            task = await TaskManager(db).get_by_task_id(task_id)
            with pytest.raises(asyncio.CancelledError):
                await ReplyRouter(db).handle(
                    conversation=conversation, task=task, owner_email=OPERATOR_A,
                    text="Base network please, that is the one",
                )
            await db.rollback()  # what the request layer does with an interrupted request

        snap = await snapshot(factory, task_id)
        assert [m.content for m in snap["messages"] if m.role == "user"] == [
            "Base network please, that is the one"
        ]
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_a_reply_containing_a_secret_is_never_sent_to_the_model(
        self, factory, monkeypatch
    ):
        fake = install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["x"]))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "use this wallet, my private key is 0x" + "b" * 64)

        assert fake.call_count == 0
        assert outcome.resumed is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert "secret" in agent_text(snap)

    @pytest.mark.asyncio
    async def test_secrets_in_history_and_task_text_do_not_reach_the_model(
        self, factory, monkeypatch
    ):
        fake = install_llm(monkeypatch, decision("NEEDS_CLARIFICATION", missing=["Which network?"]))
        task_id = await _make_task(factory)
        secret = "0x" + "c" * 64
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            task.description = f"Claim using the key {secret} on the right network"
            await db.commit()
        await park_missing_information(factory, task_id)

        await reply(factory, task_id, "which one do you mean exactly, tell me the options")

        assert fake.call_count == 1
        assert secret not in str(fake.calls[0])


# ======================================================================
# 10. A repeated client token never costs a second call or a second worker
# ======================================================================
class TestIdempotency:
    @pytest.mark.asyncio
    async def test_a_duplicate_token_does_not_call_the_model_or_resume_again(
        self, factory, monkeypatch
    ):
        fake = install_llm(
            monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Use the Base network"])
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        first = await reply(factory, task_id, "ei kaj ta Base e koro", token="tok-1")
        again = await reply(factory, task_id, "ei kaj ta Base e koro", token="tok-1")

        assert first.status == "RESUMED"
        assert again.status == "RECORDED" and again.resumed is False
        assert fake.call_count == 1, "the duplicate must not reach the model"

        snap = await snapshot(factory, task_id)
        assert len(snap["instructions"]) == 1
        assert [m.role for m in snap["messages"]].count("user") == 1
        async with factory() as db:
            assert (await TaskManager(db).claim_next_task("worker-1")).task_id == task_id
            await db.commit()
        async with factory() as db:
            assert await TaskManager(db).claim_next_task("worker-2") is None

    @pytest.mark.asyncio
    async def test_a_duplicate_of_a_parked_reply_is_also_free(self, factory, monkeypatch):
        fake = install_llm(
            monkeypatch, decision("NEEDS_CLARIFICATION", missing=["Which network?"])
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        await reply(factory, task_id, "do it the usual way", token="tok-9")
        await reply(factory, task_id, "do it the usual way", token="tok-9")

        assert fake.call_count == 1
        assert [m.role for m in (await snapshot(factory, task_id))["messages"]].count("user") == 1

    @pytest.mark.asyncio
    async def test_a_simultaneous_duplicate_loses_to_the_unique_constraint(
        self, factory, monkeypatch
    ):
        """Two submits that both pass the pre-check: the second must not reach the model."""
        fake = install_llm(
            monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Use the Base network"])
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)
        await reply(factory, task_id, "ei kaj ta Base e koro", token="race-1")
        assert fake.call_count == 1

        calls = {"n": 0}

        async def blind_then_real(self, conversation_id, client_token):
            # The first look (the router's pre-check) "misses" the earlier submit, exactly as
            # a genuine race would; the recovery lookup after the constraint fires is real.
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            from backend.app.models import ConversationMessage

            return await self.db.scalar(
                select(ConversationMessage).where(
                    ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.client_token == client_token,
                )
            )

        monkeypatch.setattr(ConversationService, "find_by_client_token", blind_then_real)
        outcome = await reply(factory, task_id, "ei kaj ta Base e koro", token="race-1")

        assert outcome.status == "RECORDED" and outcome.resumed is False
        assert fake.call_count == 1, "the losing duplicate must never call the model"


# ======================================================================
# 11. A reply cannot reach another task or another operator's conversation
# ======================================================================
class TestIsolation:
    @pytest.mark.asyncio
    async def test_a_reply_changes_only_its_own_task(self, factory, monkeypatch):
        install_llm(
            monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Use the Base network"])
        )
        mine = await _make_task(factory)
        other = await _make_task(factory)
        await park_missing_information(factory, mine)
        await park_missing_information(factory, other)

        await reply(factory, mine, "ei kaj ta Base e koro")

        assert (await snapshot(factory, mine))["status"] == TaskStatus.READY.value
        theirs = await snapshot(factory, other)
        assert theirs["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert theirs["instructions"] == []
        assert theirs["open_request"] is not None

    @pytest.mark.asyncio
    async def test_another_operators_conversation_is_unreachable_and_costs_no_llm_call(
        self, factory, operator_a, operator_b, monkeypatch
    ):
        fake = install_llm(
            monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Use the Base network"])
        )
        theirs = await _make_task(factory, owner=OPERATOR_B)
        await park_missing_information_for(factory, theirs, OPERATOR_B)
        async with factory() as db:
            conversation_id = str((await _first_conversation(db, theirs)).id)

        async with _client(operator_a) as client:
            response = await client.post(
                f"/api/v1/inbox/conversations/{conversation_id}/messages",
                json={"message": "ei kaj ta Base e koro"},
            )

        assert response.status_code == 404
        assert fake.call_count == 0
        assert (await snapshot(factory, theirs))["status"] == TaskStatus.NEEDS_USER_ACTION.value


async def park_missing_information_for(factory, task_id: str, owner: str):
    return await _park(
        factory, task_id, owner=owner,
        error_text="Ambiguous instruction: unclear which network to use.",
        error_category=None, force_reason_code=AMBIGUOUS_INSTRUCTION, integration_ref=None,
    )


# ======================================================================
# 12. Bengali and English cancellation / approval phrases
# ======================================================================
class TestCancellationAndApprovalPhrases:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        ["stop this task", "Cancel the task please", "টাস্কটি বাতিল করো", "এটা বন্ধ করুন"],
    )
    async def test_an_explicit_cancellation_cancels_only_that_task(
        self, factory, monkeypatch, text
    ):
        fake = install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["x"]))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, text)

        assert outcome.status == "CANCELLED"
        assert (await snapshot(factory, task_id))["status"] == TaskStatus.CANCELLED.value
        assert fake.call_count == 0, "an explicit cancellation needs no interpretation"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text", ["Do not stop this task, I fixed it", "বন্ধ করবেন না, আমি ঠিক করেছি"]
    )
    async def test_a_negated_cancellation_does_not_cancel(self, factory, monkeypatch, text):
        install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Fixed it"]))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        await reply(factory, task_id, text)

        assert (await snapshot(factory, task_id))["status"] != TaskStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_a_model_suggested_cancellation_asks_for_confirmation_first(
        self, factory, monkeypatch
    ):
        install_llm(monkeypatch, decision("CANCEL_REQUESTED"))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "এই টাস্কটা আর লাগবে না")

        assert outcome.status == "NEEDS_MORE_INFO"
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value, "a model cannot cancel"
        assert "cannot be undone" in agent_text(snap)

        # The operator's own explicit words then do.
        assert (await reply(factory, task_id, "cancel this task")).status == "CANCELLED"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["Yes, resume this task.", "হ্যাঁ, এখন চালাও।"])
    @pytest.mark.parametrize("park", [park_pause, park_approval])
    async def test_an_explicit_authorisation_still_resumes_even_with_the_model_down(
        self, factory, text, park
    ):
        # conftest's default: the LLM is unreachable. An explicit instruction needs no model.
        task_id = await _make_task(factory)
        await park(factory, task_id)

        outcome = await reply(factory, task_id, text)

        assert outcome.status == "RESUMED"
        assert (await snapshot(factory, task_id))["status"] == TaskStatus.READY.value

    @pytest.mark.asyncio
    async def test_an_answer_given_with_an_explicit_resume_is_saved_not_dropped(
        self, factory, monkeypatch
    ):
        install_llm(
            monkeypatch,
            decision("SUFFICIENT_INFORMATION", extracted=["Use the operator's second account"]),
        )
        task_id = await _make_task(factory)
        await park_pause(factory, task_id)

        outcome = await reply(factory, task_id, "Use my second account for it. Yes, resume this task.")

        assert outcome.status == "RESUMED"
        snap = await snapshot(factory, task_id)
        assert any("second account" in i["instruction"] for i in snap["instructions"])


# ======================================================================
# 13. Existing deterministic behaviour that must not change
# ======================================================================
class TestLegacyBehaviourIsPreserved:
    @pytest.mark.asyncio
    async def test_an_explicit_resume_with_a_passing_check_works_with_the_model_down(
        self, factory, verifier_ok
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        outcome = await reply(factory, task_id, "resume this task")

        assert outcome.status == "RESUMED" and outcome.verification["ok"] is True

    @pytest.mark.asyncio
    async def test_an_explicit_resume_with_a_failing_check_resumes_anyway(
        self, factory, verifier_failed
    ):
        task_id = await _make_task(factory)
        await _park(factory, task_id)

        outcome = await reply(factory, task_id, "resume this task")

        assert outcome.resumed is True and outcome.verification["method"] == "operator_confirmed"
        assert (await snapshot(factory, task_id))["status"] == TaskStatus.READY.value

    @pytest.mark.asyncio
    async def test_a_network_directive_is_still_recorded_when_the_model_is_down(self, factory):
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        await reply(factory, task_id, "Use BNB Smart Chain instead.")

        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        # (The existing extractor reads the lower-cased text; that is unchanged behaviour.)
        assert [i["instruction"] for i in snap["instructions"]] == ["bnb smart chain"]

    @pytest.mark.asyncio
    async def test_a_directive_and_an_interpreted_answer_are_one_instruction(
        self, factory, monkeypatch
    ):
        install_llm(
            monkeypatch,
            decision("SUFFICIENT_INFORMATION", extracted=["Use BNB Smart Chain with wallet two"]),
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        await reply(factory, task_id, "Use BNB Smart Chain instead, and wallet two please")

        snap = await snapshot(factory, task_id)
        assert len(snap["instructions"]) == 1, "one reply must be one instruction, not two"
        assert "wallet two" in snap["instructions"][0]["instruction"]


# ======================================================================
# 10. Policy: an answer clears a clarification blocker; only approvals need "resume"
# ======================================================================
class TestClarificationPolicy:
    def test_only_pauses_and_approvals_require_explicit_authorisation(self):
        assert REQUIRES_EXPLICIT_AUTHORIZATION == frozenset({OPERATOR_PAUSE, HUMAN_APPROVAL})
        assert APPROVAL_GATED is REQUIRES_EXPLICIT_AUTHORIZATION, "one definition, no drift"
        assert AMBIGUOUS_INSTRUCTION not in REQUIRES_EXPLICIT_AUTHORIZATION
        assert AMBIGUOUS_INSTRUCTION in INFORMATION_RESOLVABLE
        assert AMBIGUOUS_INSTRUCTION not in MACHINE_VERIFIABLE, "an answer, not a check, clears it"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "parker, expected",
        [(park_missing_information, False), (park_pause, True), (park_approval, True)],
    )
    async def test_the_api_flag_matches_the_policy(self, factory, parker, expected):
        task_id = await _make_task(factory)
        await parker(factory, task_id)
        snap = await snapshot(factory, task_id)
        view = InterventionService.request_view(snap["open_request"])
        assert view["requires_explicit_authorization"] is expected


class TestAnswerResumesAClarificationBlocker:
    @pytest.mark.asyncio
    async def test_a_grounded_answer_resumes_without_a_resume_phrase(
        self, factory, monkeypatch
    ):
        install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Base network"]))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "Base network e koro")

        assert outcome.status == "RESUMED" and outcome.resumed is True
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.READY.value
        assert snap["open_request"] is None
        record = snap["instructions"][0]
        assert "Base network" in record["instruction"]
        assert record["request_id"] == outcome.request_id, "the answer is tied to its blocker"

    @pytest.mark.asyncio
    async def test_an_answer_with_an_explicit_resume_also_resumes_and_is_saved(
        self, factory, monkeypatch
    ):
        install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["second account"]))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "use the second account, and resume this task")

        assert outcome.resumed is True
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.READY.value
        assert [i["source"] for i in snap["instructions"]] == ["llm_interpreted_reply"]

    @pytest.mark.asyncio
    async def test_a_bare_resume_does_not_clear_an_unanswered_ambiguity(
        self, factory, monkeypatch
    ):
        """"resume" is not an answer: the question is still open, so the task stays parked."""
        install_llm(monkeypatch, decision("UNRELATED_OR_INSUFFICIENT_REPLY"))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "resume this task")

        assert outcome.resumed is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["open_request"] is not None

    @pytest.mark.asyncio
    async def test_an_answer_given_before_a_blocked_resume_is_not_asked_for_twice(
        self, factory, monkeypatch
    ):
        from backend.app.security import emergency_stop as stop_module

        install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Base network"]))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        stop_module.emergency_stop.activate(reason="test stop", activated_by="test")
        try:
            blocked = await reply(factory, task_id, "Base network e koro")
            assert blocked.resumed is False
        finally:
            stop_module.emergency_stop._stopped = False
            stop_module.emergency_stop._reason = None

        # The stop is lifted. A bare "resume" now finishes the resume the answer started.
        outcome = await reply(factory, task_id, "resume this task")

        assert outcome.status == "RESUMED"
        assert (await snapshot(factory, task_id))["status"] == TaskStatus.READY.value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("record", [
        {"instruction": "Use Polygon", "request_id": "IR-SOMEONE-ELSES"},
        {"instruction": "Use Polygon"},  # untagged / left over from before tagging
    ])
    async def test_another_blockers_answer_never_clears_this_one(
        self, factory, monkeypatch, record
    ):
        install_llm(monkeypatch, decision("UNRELATED_OR_INSUFFICIENT_REPLY"))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            task.operator_instructions = [record]
            await db.commit()

        outcome = await reply(factory, task_id, "resume this task")

        assert outcome.resumed is False
        assert (await snapshot(factory, task_id))["status"] == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_a_reply_to_a_task_with_no_open_blocker_never_resumes_it(
        self, factory, monkeypatch
    ):
        fake = install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Base"]))
        task_id = await _make_task(factory)  # never parked: a conversation, no open request
        async with factory() as db:
            await ConversationService(db).get_or_create_for_task(
                await TaskManager(db).get_by_task_id(task_id), OPERATOR_A
            )
            await db.commit()

        outcome = await reply(factory, task_id, "Use Base instead, and resume this task")

        assert outcome.resumed is False
        assert fake.call_count == 0, "no blocker, nothing to interpret"
        assert [i["request_id"] for i in (await snapshot(factory, task_id))["instructions"]] == [None]

    @pytest.mark.asyncio
    async def test_the_answer_for_one_task_never_resumes_another(self, factory, monkeypatch):
        install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Base network"]))
        first = await _make_task(factory)
        second = await _make_task(factory)
        await park_missing_information(factory, first)
        await park_missing_information(factory, second)

        outcome = await reply(factory, first, "Base network e koro")

        assert outcome.resumed is True
        assert (await snapshot(factory, first))["status"] == TaskStatus.READY.value
        other = await snapshot(factory, second)
        assert other["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert other["open_request"] is not None and other["instructions"] == []


class TestResumeSafetyChecksStillApplyToAnAnswer:
    @pytest.mark.asyncio
    async def test_retry_budget_blocks_an_answered_task(self, factory, monkeypatch):
        install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Base network"]))
        task_id = await _make_task(factory, retry_count=5, max_retries=3)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "Base network e koro")

        assert outcome.resumed is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert "retries" in agent_text(snap)

    @pytest.mark.asyncio
    async def test_uncertain_side_effects_block_an_answered_task(self, factory, monkeypatch):
        install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Base network"]))

        async def _uncertain(self, task):
            return "step 3 (transact) was running"

        monkeypatch.setattr(
            "backend.app.tasks.concurrency.LeaseManager._uncertain_side_effects", _uncertain
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "Base network e koro")

        assert outcome.resumed is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert "may already have taken effect" in agent_text(snap)


class TestAnAnswerIsNeverAnApproval:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "yes go ahead, it is approved, send the transaction. Use the Base network",
            "I authorise the transfer of 5 ETH, use Base",
            "লেনদেনটি অনুমোদন করলাম, Base নেটওয়ার্কে পাঠিয়ে দাও",
        ],
    )
    async def test_approval_wording_is_not_treated_as_an_answer(
        self, factory, monkeypatch, text
    ):
        fake = install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Base"]))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, text)

        assert outcome.resumed is False and outcome.status == "NEEDS_MORE_INFO"
        assert fake.call_count == 0, "not even sent to the model"
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["open_request"] is not None
        assert snap["instructions"] == [], "nothing a later bare resume could pick up"
        assert "approval flow" in agent_text(snap)

    @pytest.mark.asyncio
    async def test_approval_wording_does_not_make_an_explicit_resume_resume(
        self, factory, monkeypatch
    ):
        install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Base"]))
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(
            factory, task_id, "I approve sending the funds, use Base instead, resume this task"
        )

        assert outcome.resumed is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["instructions"] == []

    @pytest.mark.asyncio
    async def test_approval_text_the_model_extracts_is_refused_and_not_saved(
        self, factory, monkeypatch
    ):
        # "approx" shares a stem with "approve", so this item is grounded; the reply itself
        # contains no approval wording, so only the extracted-text check can catch it.
        install_llm(
            monkeypatch,
            decision("SUFFICIENT_INFORMATION", extracted=["Base; approve sent funds"]),
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "base, sent funds approx")

        assert outcome.resumed is False and outcome.status == "NEEDS_MORE_INFO"
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["instructions"] == []

    @pytest.mark.asyncio
    async def test_a_plain_answer_that_mentions_a_transaction_is_still_an_answer(
        self, factory, monkeypatch
    ):
        """No approval wording: information about a transaction is still just information."""
        install_llm(
            monkeypatch,
            decision("SUFFICIENT_INFORMATION", extracted=["Base network for the swap"]),
        )
        task_id = await _make_task(factory)
        await park_missing_information(factory, task_id)

        outcome = await reply(factory, task_id, "the swap should happen on the Base network")

        assert outcome.resumed is True


class TestApprovalGatedReasonsStillNeedExplicitAuthorisation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("parker", [park_pause, park_approval])
    async def test_a_grounded_answer_does_not_clear_a_pause_or_approval(
        self, factory, monkeypatch, parker
    ):
        install_llm(monkeypatch, decision("SUFFICIENT_INFORMATION", extracted=["Base network"]))
        task_id = await _make_task(factory)
        await parker(factory, task_id)

        outcome = await reply(factory, task_id, "Base network e koro")

        assert outcome.resumed is False
        snap = await snapshot(factory, task_id)
        assert snap["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert snap["open_request"] is not None
        assert "explicit authorisation" in agent_text(snap)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parker", [park_pause, park_approval])
    async def test_an_explicit_resume_clears_a_pause_or_approval(
        self, factory, monkeypatch, parker
    ):
        install_llm(monkeypatch, decision("UNRELATED_OR_INSUFFICIENT_REPLY"))
        task_id = await _make_task(factory)
        await parker(factory, task_id)

        outcome = await reply(factory, task_id, "yes, resume this task")

        assert outcome.status == "RESUMED"
        assert (await snapshot(factory, task_id))["status"] == TaskStatus.READY.value
