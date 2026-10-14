"""LLM-driven reply interpretation for the Agent Inbox.

The model interprets and proposes; deterministic code verifies and disposes. These tests pin
that division of labour:

* a useful reply (Bengali or English) that supplies missing information resumes the SAME task
  without a magic phrase;
* an insufficient reply gets a specific follow-up and the task stays parked;
* a claim ("I logged in") never replaces a real check of the external state;
* the model can neither approve, nor cancel, nor lift an Emergency Stop;
* a model failure or malformed answer loses nothing and leaves the task parked;
* duplicate tokens cost no second model call and no second resume;
* a reply can only ever affect the task of the conversation it was sent in.

Most tests script the interpreter directly. The pipeline tests (`fake_llm`) mock only the HTTP
boundary of the one fixed LLM client, so the real context builder, redaction and strict schema
validation run.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest
from sqlalchemy import select

from backend.app.agent.controller import AgentController, LoopOutcome, LoopReport
from backend.app.inbox import interpretation as interpretation_module
from backend.app.inbox import replies as replies_module
from backend.app.inbox.interpretation import (
    InterpretationResult,
    ReplyDecision,
    ReplyInterpretation,
    build_interpretation_context,
    interpret_reply,
    parse_interpretation,
)
from backend.app.inbox.replies import ReplyRouter
from backend.app.inbox.service import (
    HUMAN_APPROVAL,
    TELEGRAM_SESSION_UNAVAILABLE,
    UNKNOWN_REASON,
    ConversationService,
)
from backend.app.inbox.verification import VerificationResult
from backend.app.inbox import verification as verification_module
from backend.app.llm import client as llm_client_module
from backend.app.llm.client import LLMError
from backend.app.models import AgentConversation, ConversationMessage, Task, TaskCheckpoint
from backend.app.planning.planner import ExecutionPlanSpec, PlanStep, ReplanTrigger, StepKind
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

# Reuse the existing Inbox harness: same database, same operators, same park helper.
from backend.tests.test_agent_inbox import (  # noqa: F401  (fixtures are imported by name)
    OPERATOR_A,
    OPERATOR_B,
    _client,
    _first_conversation,
    _make_task,
    _park,
    _running_task,
    always_ok,
    factory,
    interpreter,
    never_ok,
    operator_a,
    operator_b,
    uncheckable,
)

# A blocker whose resolution is information only the operator has (no verifier exists for it).
NEEDS_NETWORK = dict(
    error_text="I cannot continue: which network should I claim on?",
    error_category=None,
    force_reason_code=UNKNOWN_REASON,
    integration_ref=None,
    blocking_step="Step 3: submit the claim",
)


# ======================================================================
# Helpers
# ======================================================================
def _decision(
    decision: ReplyDecision,
    *,
    info=(),
    missing=(),
    question=None,
    explanation="scripted",
) -> InterpretationResult:
    return InterpretationResult.success(
        ReplyInterpretation(
            decision=decision,
            explanation=explanation,
            extracted_information=[{"field": f, "value": v} for f, v in info],
            missing_information=list(missing),
            follow_up_question=question,
        )
    )


async def _send(factory, task_id: str, text: str, *, token=None, owner: str = OPERATOR_A):
    async with factory() as db:
        conversation = await _first_conversation(db, task_id)
        task = await TaskManager(db).get_by_task_id(task_id)
        outcome = await ReplyRouter(db).handle(
            conversation=conversation,
            task=task,
            owner_email=owner,
            text=text,
            client_token=token,
        )
        await db.commit()
        return outcome


async def _status(factory, task_id: str) -> str:
    async with factory() as db:
        return (await TaskManager(db).get_by_task_id(task_id)).status


async def _instructions(factory, task_id: str) -> List[str]:
    async with factory() as db:
        task = await TaskManager(db).get_by_task_id(task_id)
        return [i["instruction"] for i in (task.operator_instructions or [])]


async def _agent_texts(factory, task_id: str) -> List[str]:
    async with factory() as db:
        conversation = await _first_conversation(db, task_id)
        rows = await ConversationService(db).list_messages(conversation.id)
        return [m.content for m in rows if m.role == "assistant"]


class FakeLLM:
    """Replaces only `llm.complete_json`, the network boundary of the one fixed client."""

    def __init__(self) -> None:
        self.prompts: List[List[Any]] = []
        self.response: Any = {}

    async def __call__(self, messages, temperature=None, max_tokens=None):
        self.prompts.append(messages)
        outcome = self.response(messages) if callable(self.response) else self.response
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def fake_llm(monkeypatch):
    """Use the REAL interpreter, with the model's HTTP call replaced."""
    monkeypatch.setattr(replies_module, "interpret_reply", interpret_reply)
    fake = FakeLLM()
    monkeypatch.setattr(llm_client_module.llm, "complete_json", fake)
    return fake


def _valid_answer(decision: str, **extra) -> Dict[str, Any]:
    body = {
        "decision": decision,
        "explanation": "scripted model answer",
        "extracted_information": [],
        "missing_information": [],
        "follow_up_question": None,
        "recommended_next_step": "NO_ACTION",
    }
    body.update(extra)
    return body


# ======================================================================
# 1-2. Useful replies resume the task, in Bengali and in English, with no magic phrase
# ======================================================================
class TestUsefulRepliesResume:
    @pytest.mark.asyncio
    async def test_a_short_bengali_answer_supplies_the_missing_network(self, factory, interpreter):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )

        # Two words, no address, no link: the old word-count heuristic could not call this
        # information, so the task used to stay parked.
        outcome = await _send(factory, task_id, "পলিগন নেটওয়ার্ক")

        assert outcome.status == "RESUMED" and outcome.resumed is True
        assert await _status(factory, task_id) == TaskStatus.READY.value
        saved = await _instructions(factory, task_id)
        assert len(saved) == 1 and "Polygon" in saved[0]
        assert len(interpreter.calls) == 1
        assert interpreter.calls[0]["reply"] == "পলিগন নেটওয়ার্ক"

    @pytest.mark.asyncio
    async def test_a_plain_english_answer_resumes_without_a_resume_phrase(self, factory, interpreter):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("account", "the second wallet")]
        )

        outcome = await _send(factory, task_id, "the second wallet")

        assert outcome.resumed is True
        assert await _status(factory, task_id) == TaskStatus.READY.value
        assert any("second wallet" in i for i in await _instructions(factory, task_id))

    @pytest.mark.asyncio
    async def test_the_reply_is_reported_in_the_same_conversation(self, factory, interpreter):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )
        await _send(factory, task_id, "Polygon")
        texts = await _agent_texts(factory, task_id)
        assert any("queued to resume" in t for t in texts)

    @pytest.mark.asyncio
    async def test_the_same_conversation_serves_a_later_intervention(self, factory, interpreter):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )
        await _send(factory, task_id, "Polygon")

        # The worker claims the task again and it hits a second blocker.
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            await TaskManager(db).transition(task, TaskStatus.IN_PROGRESS, reason="claimed")
            await db.commit()
        await _park(factory, task_id, **NEEDS_NETWORK)

        async with factory() as db:
            pk = (await TaskManager(db).get_by_task_id(task_id)).id
            conversations = (
                await db.execute(select(AgentConversation).where(AgentConversation.task_id == pk))
            ).scalars().all()
            assert len(conversations) == 1, "a new intervention opened a second conversation"
            assert conversations[0].requires_reply is True


# ======================================================================
# 3. An insufficient reply gets a specific question and nothing resumes
# ======================================================================
class TestInsufficientReply:
    @pytest.mark.asyncio
    async def test_a_vague_reply_asks_a_specific_question_and_stays_parked(self, factory, interpreter):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.NEEDS_CLARIFICATION,
            missing=["which network"],
            question="Should I claim on Polygon or on BNB Smart Chain?",
        )

        outcome = await _send(factory, task_id, "the other one")

        assert outcome.status == "NEEDS_MORE_INFO" and outcome.resumed is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value
        assert await _instructions(factory, task_id) == []
        reply = (await _agent_texts(factory, task_id))[-1]
        assert "Polygon or on BNB Smart Chain" in reply
        assert "which network" in reply

    @pytest.mark.asyncio
    async def test_sufficient_with_nothing_extracted_is_not_information(self, factory, interpreter):
        """A model that says 'sufficient' but names nothing has not handed over anything."""
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(ReplyDecision.SUFFICIENT_INFORMATION, info=[])

        outcome = await _send(factory, task_id, "you know what I mean")

        assert outcome.resumed is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_an_unrelated_reply_on_an_uncheckable_blocker_does_not_resume(
        self, factory, interpreter
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(ReplyDecision.UNRELATED_OR_INSUFFICIENT_REPLY)

        outcome = await _send(factory, task_id, "thanks, talk later")

        assert outcome.resumed is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value


# ======================================================================
# 4. A reported login does not bypass the real session check
# ======================================================================
class TestClaimsAreNotProof:
    @pytest.mark.asyncio
    async def test_a_login_claim_does_not_resume_when_the_session_is_still_signed_out(
        self, factory, interpreter, never_ok
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id)  # Telegram session blocker
        interpreter.result = _decision(ReplyDecision.BLOCKER_REQUIRES_VERIFICATION)

        outcome = await _send(factory, task_id, "I completed the Telegram login on the server")

        assert outcome.resumed is False
        assert outcome.verification["ok"] is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_a_login_claim_does_not_resume_when_the_session_cannot_be_inspected(
        self, factory, interpreter, uncheckable
    ):
        """The bug this replaces: a long enough reply used to stand in for an unavailable check.

        Even when the model reads the reply as 'sufficient information', the state of a login
        is not the operator's to assert.
        """
        task_id = await _running_task(factory)
        await _park(factory, task_id)  # Telegram session blocker
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("login", "completed on the server")]
        )

        outcome = await _send(
            factory, task_id,
            "I completed the Telegram login and the chat list is showing now",
        )

        assert outcome.resumed is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_a_login_claim_resumes_only_when_the_real_check_passes(
        self, factory, interpreter, always_ok
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id)
        interpreter.result = _decision(ReplyDecision.BLOCKER_REQUIRES_VERIFICATION)

        outcome = await _send(factory, task_id, "Telegram login complete kore disi")

        assert outcome.resumed is True
        assert always_ok, "the real verifier was never consulted"
        assert await _status(factory, task_id) == TaskStatus.READY.value

    @pytest.mark.asyncio
    async def test_sufficient_information_still_needs_the_check_to_pass(
        self, factory, interpreter, never_ok
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("account", "main")]
        )

        outcome = await _send(factory, task_id, "use the main account for telegram")

        assert outcome.resumed is False
        # The information is kept for the next attempt rather than thrown away.
        assert any("main" in i for i in await _instructions(factory, task_id))
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value


# ======================================================================
# 5-6. Resume from the checkpoint, keep completed work, and plan with the answer
# ======================================================================
class TestResumeFromCheckpoint:
    @pytest.mark.asyncio
    async def test_the_same_task_resumes_from_its_checkpoint_with_completed_work_intact(
        self, factory, interpreter
    ):
        task_id = await _running_task(factory)
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            task.completed_actions = [{"id": "a1", "description": "Joined the channel"}]
            task.progress = 0.4
            await db.commit()
        await _park(factory, task_id, **NEEDS_NETWORK)

        async with factory() as db:
            before = (await db.execute(select(TaskCheckpoint))).scalars().all()
            assert before, "parking must have saved a checkpoint"
            names_before = sorted(c.checkpoint_name for c in before)

        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )
        outcome = await _send(factory, task_id, "Polygon")
        assert outcome.resumed is True

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            assert task.status == TaskStatus.READY.value
            assert task.completed_actions == [{"id": "a1", "description": "Joined the channel"}]
            assert abs(task.progress - 0.4) < 1e-9, "progress was reset on resume"
            after = (await db.execute(select(TaskCheckpoint))).scalars().all()
            assert sorted(c.checkpoint_name for c in after) == names_before, (
                "resume must reuse the saved checkpoint, not discard or fabricate one"
            )
        assert any("checkpoint" in t.lower() for t in await _agent_texts(factory, task_id))

    @pytest.mark.asyncio
    async def test_the_next_planning_call_receives_the_operators_answer(self, factory, interpreter):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )
        await _send(factory, task_id, "Polygon")

        seen: Dict[str, Any] = {}
        existing = ExecutionPlanSpec(
            objective="Claim the airdrop",
            steps=[
                PlanStep(0, "Join the channel", StepKind.TELEGRAM, status="completed"),
                PlanStep(1, "Submit the claim", StepKind.BROWSER),
            ],
        )

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            controller = AgentController(db)

            async def _load(_task):
                return existing

            async def _replan(_task, current, trigger, reason, **kwargs):
                seen.update(trigger=trigger, reason=reason, current=current)
                return current

            controller.planner.load_plan_spec = _load
            controller.planner.replan = _replan
            report = LoopReport(task_id=task.task_id, outcome=LoopOutcome.COMPLETED)
            plan = await controller._ensure_plan(task, report)

            assert seen["trigger"] == ReplanTrigger.OPERATOR_REQUEST
            assert "Polygon" in seen["reason"], "the clarification never reached the planner"
            assert plan.steps[0].status == "completed", "completed work was not preserved"
            assert (task.operator_instructions or []) == [], "the note would replan forever"


# ======================================================================
# 7-8. Sensitive decisions and the Emergency Stop are untouched by the model
# ======================================================================
class TestSecurityBoundaries:
    @pytest.mark.asyncio
    async def test_information_never_counts_as_approval(self, factory, interpreter):
        task_id = await _running_task(factory)
        await _park(
            factory, task_id, error_text="Approval required to sign this transfer.",
            force_reason_code=HUMAN_APPROVAL, integration_ref=None,
        )
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("recipient", "0x" + "ab" * 20)]
        )

        outcome = await _send(factory, task_id, "send it to 0x" + "ab" * 20)

        assert outcome.resumed is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value
        assert "informational message alone" in (await _agent_texts(factory, task_id))[-1]

    @pytest.mark.asyncio
    async def test_a_model_that_claims_approval_cannot_authorise_anything(
        self, factory, interpreter, always_ok
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id)  # a machine-verifiable blocker whose check WOULD pass
        interpreter.result = _decision(ReplyDecision.EXPLICIT_APPROVAL_REQUIRED)

        outcome = await _send(factory, task_id, "yes approve the transfer of 5 ETH")

        assert outcome.resumed is False
        assert not always_ok, "an approval request must not even reach verification"
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_the_emergency_stop_still_prevents_a_sufficient_answer_from_running(
        self, factory, interpreter
    ):
        from backend.app.security import emergency_stop as stop_module

        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )
        stop_module.emergency_stop._stopped = True
        stop_module.emergency_stop._reason = "test"
        try:
            outcome = await _send(factory, task_id, "Polygon")
        finally:
            stop_module.emergency_stop._stopped = False
            stop_module.emergency_stop._reason = None

        assert outcome.resumed is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value
        assert "emergency stop" in (await _agent_texts(factory, task_id))[-1].lower()


# ======================================================================
# 9. Model failures leave the task safely parked and lose nothing
# ======================================================================
class TestModelFailureIsSafe:
    @pytest.mark.asyncio
    async def test_a_failed_interpretation_keeps_the_message_and_parks_the_task(
        self, factory, interpreter
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = InterpretationResult.failure("The model is unreachable.", "CONNECTION_ERROR")

        outcome = await _send(factory, task_id, "Polygon")

        assert outcome.resumed is False and outcome.status == "NEEDS_MORE_INFO"
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            rows = await ConversationService(db).list_messages(conversation.id)
            assert any(m.role == "user" and m.content == "Polygon" for m in rows), (
                "the operator's message was lost"
            )
        assert "could not interpret" in (await _agent_texts(factory, task_id))[-1]

    @pytest.mark.asyncio
    async def test_an_interpreter_that_raises_is_contained(self, factory, interpreter):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = RuntimeError("unexpected")

        outcome = await _send(factory, task_id, "Polygon")

        assert outcome.resumed is False and outcome.status == "NEEDS_MORE_INFO"
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_an_llm_provider_error_is_contained(self, factory, fake_llm):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        fake_llm.response = LLMError("boom", category="CONNECTION_ERROR")

        outcome = await _send(factory, task_id, "Polygon")

        assert outcome.resumed is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "malformed",
        [
            {"decision": "RESUME_NOW", "explanation": "x"},               # unknown decision
            {"explanation": "no decision"},                                 # missing key
            _valid_answer("SUFFICIENT_INFORMATION", run_tool="transfer"),  # extra, executable-looking key
            _valid_answer("SUFFICIENT_INFORMATION", explanation=""),       # empty explanation
            _valid_answer("SUFFICIENT_INFORMATION", extracted_information="Polygon"),  # wrong type
            _valid_answer("SUFFICIENT_INFORMATION", recommended_next_step="EXECUTE"),
        ],
    )
    async def test_malformed_structured_output_resumes_nothing(self, factory, fake_llm, malformed):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        fake_llm.response = malformed

        outcome = await _send(factory, task_id, "Polygon")

        assert outcome.resumed is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value
        assert await _instructions(factory, task_id) == []

    @pytest.mark.asyncio
    async def test_non_object_output_is_rejected(self):
        for raw in ([], "SUFFICIENT_INFORMATION", None, 3):
            with pytest.raises((ValueError, Exception)):
                parse_interpretation(raw)

    @pytest.mark.asyncio
    async def test_an_explicit_resume_still_works_without_the_model_but_stays_verified(
        self, factory, interpreter, always_ok
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id)
        interpreter.result = InterpretationResult.failure("down", "TIMEOUT")

        outcome = await _send(factory, task_id, "resume this task")

        assert outcome.resumed is True
        assert always_ok, "the explicit path must still run the real check"

    @pytest.mark.asyncio
    async def test_an_explicit_resume_without_the_model_is_still_refused_when_the_check_fails(
        self, factory, interpreter, never_ok
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id)
        interpreter.result = InterpretationResult.failure("down", "TIMEOUT")

        outcome = await _send(factory, task_id, "resume this task")

        assert outcome.resumed is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value


# ======================================================================
# 10. Duplicate submissions cost nothing
# ======================================================================
class TestDuplicateSubmissions:
    @pytest.mark.asyncio
    async def test_a_repeated_token_triggers_no_second_model_call_and_no_second_resume(
        self, factory, interpreter
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )

        first = await _send(factory, task_id, "Polygon", token="tok-1")
        second = await _send(factory, task_id, "Polygon", token="tok-1")

        assert first.resumed is True
        assert second.status == "RECORDED" and second.resumed is False
        assert len(interpreter.calls) == 1, "a duplicate reply called the model again"
        assert len(await _instructions(factory, task_id)) == 1

    @pytest.mark.asyncio
    async def test_racing_replies_start_one_worker_and_record_one_resume(self, factory, interpreter):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )

        first, second = await asyncio.gather(
            _send(factory, task_id, "Polygon", token="a"),
            _send(factory, task_id, "Polygon", token="b"),
        )

        assert sorted([first.status, second.status]) == ["RECORDED", "RESUMED"]
        assert await _status(factory, task_id) == TaskStatus.READY.value


    @pytest.mark.asyncio
    async def test_two_simultaneous_submits_of_one_token_call_the_model_once(
        self, factory, interpreter
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )

        results = await asyncio.gather(
            _send(factory, task_id, "Polygon", token="same"),
            _send(factory, task_id, "Polygon", token="same"),
            return_exceptions=True,
        )

        failures = [r for r in results if isinstance(r, Exception)]
        assert not failures, f"a simultaneous duplicate raised instead of being ignored: {failures}"
        assert len(interpreter.calls) == 1, "the model was called more than once for one token"
        assert sorted(r.status for r in results) == ["RECORDED", "RESUMED"]
        assert await _status(factory, task_id) == TaskStatus.READY.value
        # Not asserted: how many user rows exist afterwards. This harness gives both sessions
        # ONE shared SQLite connection (StaticPool), so the loser's savepoint rollback also
        # discards the winner's uncommitted row. With separate connections (production) the
        # loser waits for the winner's commit, so the winner's row is durable by then.


# ======================================================================
# 11. Isolation between tasks and between operators
# ======================================================================
class TestIsolation:
    @pytest.mark.asyncio
    async def test_a_reply_only_changes_the_task_of_its_own_conversation(self, factory, interpreter):
        mine = await _running_task(factory)
        theirs = await _running_task(factory)
        await _park(factory, mine, **NEEDS_NETWORK)
        await _park(factory, theirs, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )

        await _send(factory, mine, "Polygon")

        assert await _status(factory, mine) == TaskStatus.READY.value
        assert await _status(factory, theirs) == TaskStatus.NEEDS_USER_ACTION.value
        assert await _instructions(factory, theirs) == []

    @pytest.mark.asyncio
    async def test_the_model_never_sees_another_tasks_data(self, factory, fake_llm):
        mine = await _running_task(factory)
        theirs = await _running_task(factory)
        await _park(factory, mine, **NEEDS_NETWORK)
        await _park(factory, theirs, **NEEDS_NETWORK)
        async with factory() as db:
            other = await TaskManager(db).get_by_task_id(theirs)
            other.title = "TOPSECRET-OTHER-TASK-TITLE"
            await db.commit()
        fake_llm.response = _valid_answer("UNRELATED_OR_INSUFFICIENT_REPLY")

        await _send(factory, mine, "hello there")

        sent = "\n".join(m.content for prompt in fake_llm.prompts for m in prompt)
        assert "TOPSECRET-OTHER-TASK-TITLE" not in sent

    @pytest.mark.asyncio
    async def test_another_operator_cannot_trigger_interpretation(
        self, factory, interpreter, operator_a, operator_b
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        async with factory() as db:
            conversation = await _first_conversation(db, task_id)
            conversation_id = str(conversation.id)

        async with _client(operator_b) as client:
            response = await client.post(
                f"/api/v1/inbox/conversations/{conversation_id}/messages",
                json={"message": "Polygon"},
            )

        assert response.status_code == 404
        assert interpreter.calls == [], "a stranger's message reached the model"
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value

    @pytest.mark.asyncio
    async def test_the_owner_can_reply_over_http_and_the_task_resumes(
        self, factory, interpreter, operator_a
    ):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        interpreter.result = _decision(
            ReplyDecision.SUFFICIENT_INFORMATION, info=[("network", "Polygon")]
        )
        async with factory() as db:
            conversation_id = str((await _first_conversation(db, task_id)).id)

        async with _client(operator_a) as client:
            response = await client.post(
                f"/api/v1/inbox/conversations/{conversation_id}/messages",
                json={"message": "Polygon", "client_token": "http-1"},
            )

        assert response.status_code == 200, response.text
        assert response.json()["resumed"] is True
        assert await _status(factory, task_id) == TaskStatus.READY.value


# ======================================================================
# 12. Cancellation and approval phrases, in both languages
# ======================================================================
class TestPhrases:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "phrase", ["Cancel this task.", "stop this task", "এই কাজটি বাতিল করো", "কাজটি বন্ধ করুন"]
    )
    async def test_real_cancellations_cancel_only_this_task(self, factory, interpreter, phrase):
        mine = await _running_task(factory)
        other = await _running_task(factory)
        await _park(factory, mine)
        await _park(factory, other)

        outcome = await _send(factory, mine, phrase)

        assert outcome.status == "CANCELLED"
        assert await _status(factory, mine) == TaskStatus.CANCELLED.value
        assert await _status(factory, other) == TaskStatus.NEEDS_USER_ACTION.value
        assert interpreter.calls == [], "a plain cancellation needs no model"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "phrase", ["Do not stop this task, I fixed it", "বন্ধ করবেন না, আমি ঠিক করেছি"]
    )
    async def test_negated_cancellations_do_not_cancel(self, factory, interpreter, never_ok, phrase):
        task_id = await _running_task(factory)
        await _park(factory, task_id)

        await _send(factory, task_id, phrase)

        assert await _status(factory, task_id) != TaskStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_a_model_cannot_cancel_a_task_on_its_own(self, factory, interpreter):
        task_id = await _running_task(factory)
        await _park(factory, task_id)
        interpreter.result = _decision(ReplyDecision.CANCEL_REQUESTED)

        outcome = await _send(factory, task_id, "maybe we should just drop this whole thing")

        assert outcome.status == "NEEDS_MORE_INFO"
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value
        assert "cancel this task" in (await _agent_texts(factory, task_id))[-1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phrase", ["Yes, resume this task.", "হ্যাঁ, এখন চালাও।"])
    async def test_explicit_approval_phrases_resume_an_operator_pause(self, factory, interpreter, phrase):
        task_id = await _running_task(factory)
        await _park(
            factory, task_id, error_text="Emergency stop engaged.",
            force_reason_code=HUMAN_APPROVAL, integration_ref=None,
        )

        outcome = await _send(factory, task_id, phrase)

        assert outcome.resumed is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phrase", ["yes", "হ্যাঁ", "looks good to me"])
    async def test_bare_agreement_is_not_authorisation(self, factory, interpreter, phrase):
        task_id = await _running_task(factory)
        await _park(
            factory, task_id, error_text="Emergency stop engaged.",
            force_reason_code=HUMAN_APPROVAL, integration_ref=None,
        )

        outcome = await _send(factory, task_id, phrase)

        assert outcome.resumed is False
        assert await _status(factory, task_id) == TaskStatus.NEEDS_USER_ACTION.value


# ======================================================================
# The context the model is given
# ======================================================================
class TestInterpretationContext:
    @pytest.mark.asyncio
    async def test_the_context_carries_the_task_the_blocker_and_the_checkpoint(self, factory):
        task_id = await _running_task(factory)
        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            task.completed_actions = [{"id": "a1", "description": "Joined the channel"}]
            task.operator_instructions = [{"instruction": "Earlier note: use the main wallet"}]
            await db.commit()
        await _park(factory, task_id, **NEEDS_NETWORK)

        async with factory() as db:
            task = await TaskManager(db).get_by_task_id(task_id)
            conversation = await _first_conversation(db, task_id)
            from backend.app.inbox.service import InterventionService

            request = await InterventionService(db).active_for_conversation(conversation.id)
            context = await build_interpretation_context(
                db, task=task, conversation=conversation, request=request, reply="Polygon"
            )

        assert context["task"]["objective"]
        assert context["task"]["status"] == TaskStatus.NEEDS_USER_ACTION.value
        assert any("Joined the channel" in a for a in context["task"]["completed_actions"])
        assert context["intervention"]["reason_code"] == UNKNOWN_REASON
        assert "which network" in context["intervention"]["blocker"]
        assert context["checkpoint"]["available"] is True
        assert context["already_recorded_operator_information"] == ["Earlier note: use the main wallet"]
        assert context["latest_operator_reply"] == "Polygon"
        assert context["recent_conversation"], "the agent's own question was not included"

    @pytest.mark.asyncio
    async def test_secrets_never_reach_the_model(self, factory, fake_llm):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        fake_llm.response = _valid_answer("UNRELATED_OR_INSUFFICIENT_REPLY")
        key = "0x" + "a1" * 32

        await _send(factory, task_id, f"Polygon, and my private key is {key}")

        sent = "\n".join(m.content for prompt in fake_llm.prompts for m in prompt)
        assert key not in sent, "a raw private key was sent to the model"
        assert "a1a1a1a1" not in sent
        assert "[REDACTED" in sent, "the reply was not redacted before being sent"

    @pytest.mark.asyncio
    async def test_the_real_pipeline_resumes_on_a_valid_model_answer(self, factory, fake_llm):
        task_id = await _running_task(factory)
        await _park(factory, task_id, **NEEDS_NETWORK)
        fake_llm.response = _valid_answer(
            "SUFFICIENT_INFORMATION",
            extracted_information=[{"field": "network", "value": "Polygon"}],
            recommended_next_step="RESUME_TASK",
        )

        outcome = await _send(factory, task_id, "পলিগন নেটওয়ার্ক")

        assert outcome.resumed is True
        assert len(fake_llm.prompts) == 1
        assert await _instructions(factory, task_id) == ["Operator-provided information: network: Polygon"]
