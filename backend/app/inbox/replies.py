"""Routing a reply from the Agent Inbox back to the right task, and resuming it safely.

The order of operations here is deliberate:

1. resolve the conversation server-side and prove ownership;
2. resolve the task *from that conversation*, never from a client-supplied task id;
3. confirm the intervention request is still open;
4. persist the reply (idempotently, before anything else happens);
5. verify the blocking condition for real;
6. only then requeue the task.

Every branch that does not reach step 6 says why, in the same conversation, and leaves the
task parked. A reply in one conversation can never move another task.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.inbox import events
from backend.app.inbox.interpretation import (
    InterpretationResult,
    ReplyDecision,
    ReplyInterpretation,
    interpret_reply,
)
from backend.app.inbox.service import (
    BROWSER_SESSION_UNAVAILABLE,
    HUMAN_VERIFICATION,
    MISSING_AUTHENTICATION,
    REQUIRES_EXPLICIT_AUTHORIZATION,
    TELEGRAM_SESSION_UNAVAILABLE,
    ConversationService,
    InterventionService,
    normalise_reply,
)
from backend.app.inbox.verification import VerificationResult, verify
from backend.app.models import AgentConversation, ConversationMessage, Task
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

logger = logging.getLogger(__name__)

# Terminal states. A reply to one of these is recorded and reported, never resumed.
_TERMINAL = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value, TaskStatus.FAILED.value}
)

# The states a reply is allowed to requeue from. Everything else is either terminal or already
# running, and requeueing it would double up with a live execution.
_RESUMABLE_FROM = (
    TaskStatus.NEEDS_USER_ACTION,
    TaskStatus.WAITING,
    TaskStatus.PARTIALLY_COMPLETED,
    TaskStatus.RETRYING,
    TaskStatus.UNVERIFIED,
)

# Deliberately narrow. "stop" alone must not cancel a task; the operator has to actually say so.
_CANCEL_PATTERNS = (
    r"\bstop (?:this|the) task\b",
    r"\bcancel (?:this|the) task\b",
    r"\babort (?:this|the) task\b",
    r"\bstop this\b",
    r"\bcancel it\b",
    r"\bdo not (?:run|execute) (?:this|the) task\b",
    # Bengali: "stop it", "cancel it", "halt".
    r"বন্ধ\s*কর(?:ো|ুন)",
    r"বাতিল\s*কর(?:ো|ুন)",
    r"রদ\s*কর(?:ো|ুন)",
    r"থাম(?:াও|ান)",
)

# A negation governing the instruction. Checked BEFORE the cancellation patterns, because
# "do not stop this task, I fixed it" is the opposite of a cancellation and used to cancel.
_NEGATION_PATTERNS = (
    r"\b(?:do not|don'?t|do n[o']t|never|no need to)\s+(?:stop|cancel|abort|kill)\b",
    r"\bstop\s+no\b",
    # Bengali negation before a stop verb: "don't stop it".
    r"(?:করবেন\s*না|করো\s*না|বন্ধ\s*করবেন\s*না|বন্ধ\s*করো\s*না)\s*",
)

# Explicit authorisation. Required for operator pauses, approvals and ambiguous instructions.
_AUTHORISE_PATTERNS = (
    r"\bi (?:confirm|authorise|authorize) (?:and )?(?:resume|continue|proceed)\b",
    r"\bresume (?:this|the) task\b",
    r"\bcontinue (?:this|the) task\b",
    r"\byes[, ]+resume\b",
    r"\bgo ahead (?:and|with) resume\b",
    r"\bproceed (?:with|to) (?:this|the) task\b",
    # Bengali. A verb is required, exactly as in English: a bare "হ্যাঁ" ("yes") on its own
    # is informational and must not restart a task somebody deliberately stopped.
    r"এখন\s*চালাও",
    r"চালিয়ে\s*দ(?:াও|েন)",
    r"শুরু\s*কর(?:ো|ুন)",
    r"এগিয়ে\s*য(?:াও|ান)",
    r"সম্পন্ন\s*কর(?:ো|ুন)",
    r"রিজিউম\s*কর(?:ো|ুন)",
    r"চালু\s*কর(?:ো|ুন)",
)

# Directives that change the task's direction rather than clear its blocker.
_CLARIFICATION_PATTERNS = (
    r"\buse (?P<target>[a-z0-9 \-]{2,40}?)(?: instead| rather)\b",
    r"\bswitch to (?P<target>[a-z0-9 \-]{2,40}?)(?: instead)?\b",
    r"\bchange (?:the )?network to (?P<target>[a-z0-9 \-]{2,40}?)(?: instead)?\b",
    # Bengali: "use <network> now".
    r"(?:ব্যবহার\s*কর(?:ো|ুন)|ব্যবহার\s*করে)",
)

_NETWORK_HINT = re.compile(
    r"\b(ethereum|eth|polygon|matic|bsc|bnb|binance|arbitrum|arb|optimism|op|avalanche|avax|"
    r"base|fantom|scroll|linea|zksync|solana|sol|celo|gnosis|mainnet|testnet|sepolia)\b",
    re.IGNORECASE,
)


def _matches(patterns, text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def _is_cancellation(text: str) -> bool:
    """Whether this reply cancels the task.

    A negation governs the instruction: "do not stop this task" is the opposite of a
    cancellation, and treating it as one destroys work the operator wanted kept.
    """
    if _matches(_NEGATION_PATTERNS, text):
        return False
    return _matches(_CANCEL_PATTERNS, text)


# Concrete things an operator can hand over that no automatic check can confirm: a wallet or
# contract address, a link, a code. A reply containing one of these is information, not chatter.
_INFO_TOKEN = re.compile(
    r"0x[0-9a-fA-F]{40}\b"          # EVM address
    r"|https?://\S+"                # link
    r"|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"  # base58-style address (Solana etc.)
)
# Replies that only ask for another look. They carry no information and must not resume a task.
_RECHECK_ONLY = re.compile(
    r"^(?:please\s+)?(?:re-?check|check(?:\s+again)?|try\s+again|retry|ok(?:ay)?|done|yes|hi|hello|\d{1,4})[\s.!?]*$"
)
_MIN_WORDS_FOR_INFO = 5


def operator_information(body: str) -> Optional[str]:
    """The operator's reply, if it actually hands the agent something to work with.

    Used only when the blocking condition cannot be checked by machine. There the operator is
    the only source of truth, so a substantive reply is recorded as their information and fed
    to the planner. Short acknowledgements and bare re-check requests are not: they would
    resume a task with nothing new and send it straight back into the same block.
    """
    text = " ".join((body or "").split())
    if not text or _RECHECK_ONLY.match(text.lower()):
        return None
    if _INFO_TOKEN.search(text) or len(text.split()) >= _MIN_WORDS_FOR_INFO:
        return text[:280]
    return None


# Blockers that are a STATE of an external system (a login, a session, a challenge). The operator
# saying "I did it" is a claim about that system, never proof. When the system cannot be
# inspected, such a reply is not enough to resume, whatever the model made of it.
_STATE_BLOCKERS = frozenset(
    {
        TELEGRAM_SESSION_UNAVAILABLE,
        BROWSER_SESSION_UNAVAILABLE,
        HUMAN_VERIFICATION,
        MISSING_AUTHENTICATION,
    }
)

# The model may name anything; only this much of its wording is ever shown back to the operator.
_MAX_SHOWN_QUESTION = 400


@dataclass
class ReplyOutcome:
    """What the API returns for one reply."""

    status: str  # RESUMED | CLARIFIED | CANCELLED | NEEDS_MORE_INFO | RECORDED | IGNORED
    task_id: str
    conversation_id: str
    message_id: str
    request_id: Optional[str]
    task_status: str
    resumed: bool
    agent_message_id: Optional[str] = None
    detail: Optional[str] = None
    verification: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "status": self.status,
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "agent_message_id": self.agent_message_id,
            "request_id": self.request_id,
            "task_status": self.task_status,
            "resumed": self.resumed,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.verification:
            payload["verification"] = self.verification
        return payload


class ReplyRouter:
    """Handles one user reply against one conversation."""

    def __init__(self, db: AsyncSession, interpreter=None) -> None:
        self.db = db
        self.conversations = ConversationService(db)
        self.interventions = InterventionService(db)
        self.tasks = TaskManager(db)
        # Injectable so tests can substitute a deterministic model. Production uses the one
        # fixed LLM through `interpret_reply`.
        self._interpreter = interpreter or interpret_reply

    # ------------------------------------------------------------------
    async def handle(
        self,
        *,
        conversation: AgentConversation,
        task: Task,
        owner_email: str,
        text: str,
        client_token: Optional[str] = None,
    ) -> ReplyOutcome:
        body = (text or "").strip()

        # --- 4. Idempotency: a duplicate submit returns the original result. -----
        if client_token:
            previous = await self.conversations.find_by_client_token(conversation.id, client_token)
            if previous is not None:
                logger.info("Duplicate reply ignored for conversation %s", conversation.id)
                return ReplyOutcome(
                    status="RECORDED",
                    task_id=task.task_id,
                    conversation_id=str(conversation.id),
                    message_id=str(previous.id),
                    request_id=previous.intervention_request_id,
                    task_status=task.status,
                    resumed=False,
                    detail="This message was already received.",
                )

        request = await self.interventions.active_for_conversation(conversation.id)

        # --- 5. Persist the reply before anything can change the task. -----------
        task_id_text, conversation_pk, task_status = task.task_id, conversation.id, task.status
        conversation_id_text = str(conversation_pk)
        try:
            # A savepoint, so losing a race on the unique (conversation, token) index undoes
            # only this insert and leaves the rest of the session usable.
            async with self.db.begin_nested():
                message = await self.conversations.append_message(
                    conversation,
                    role="user",
                    content=body,
                    kind="USER",
                    intervention_request_id=request.request_id if request is not None else None,
                    client_token=client_token,
                )
        except IntegrityError:
            if not client_token:
                raise
            # The unique (conversation, token) index only fires when this token was already
            # accepted, so this is a duplicate whether or not the winning row is visible to this
            # session yet (it is not, until the other request commits). Never a server error,
            # and never a second model call or a second resume.
            original = await self.conversations.find_by_client_token(conversation_pk, client_token)
            logger.info("Concurrent duplicate reply ignored for conversation %s", conversation_id_text)
            return ReplyOutcome(
                status="RECORDED",
                task_id=task_id_text,
                conversation_id=conversation_id_text,
                message_id=str(original.id) if original is not None else "",
                request_id=original.intervention_request_id if original is not None else None,
                task_status=task_status,
                resumed=False,
                detail="This message was already received.",
            )
        # Durable BEFORE the model is consulted. Interpretation can take seconds and can fail;
        # neither may cost the operator their message.
        await self.db.commit()

        outcome = await self._route(
            conversation=conversation,
            task=task,
            owner_email=owner_email,
            body=body,
            message=message,
            request=request,
        )

        await events.publish(
            events.EV_USER_MESSAGE,
            task_id=task.task_id,
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            summary="Operator replied",
        )
        return outcome

    # ------------------------------------------------------------------
    async def _route(
        self,
        *,
        conversation: AgentConversation,
        task: Task,
        owner_email: str,
        body: str,
        message: ConversationMessage,
        request,
    ) -> ReplyOutcome:
        base = dict(
            task_id=task.task_id,
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            request_id=request.request_id if request is not None else None,
        )

        # A directive ("use X instead") is an instruction, not a claim that the blocker was
        # fixed. Record it against the task first so it survives whichever branch follows --
        # losing an operator instruction because a request happened to be open would be a bug.
        clarification = self._extract_clarification(body)
        if clarification is not None:
            self._store_clarification(task, clarification)
            await self.db.flush()

        # --- A stopped task never resumes, whatever the reply says. -------------
        if task.status == TaskStatus.CANCELLED.value:
            return await self._reply_agent(
                conversation, task, base,
                f"Task {task.task_id} was cancelled, so it will not be resumed. "
                "Your message is saved in this conversation for the record.",
                status="RECORDED",
            )

        # --- Explicit cancellation, scoped to this task only. -------------------
        if _is_cancellation(normalise_reply(body)):
            return await self._cancel(conversation, task, owner_email, base)

        # --- A note with no open blocker: record it, change nothing. ------------
        if request is None:
            return await self._record_without_blocker(conversation, task, clarification, base)

        # --- Operator pause / approval / ambiguity: explicit intent required. ----
        if request.reason_code in REQUIRES_EXPLICIT_AUTHORIZATION:
            return await self._handle_authorisation(
                conversation, task, owner_email, body, base, request, message=message
            )

        # --- Everything else: let the fixed LLM read the reply, then dispose in code. --
        try:
            interpretation = await self._interpreter(
                self.db,
                task=task,
                conversation=conversation,
                request=request,
                reply=body,
                current_message_id=message.id,
            )
        except Exception as exc:  # the saved message and the parked task must survive anything
            logger.warning("Reply interpreter raised for %s: %s", task.task_id, type(exc).__name__)
            interpretation = InterpretationResult.failure(
                "The interpreter failed unexpectedly.", "INTERPRETER_ERROR"
            )

        # The model call can take seconds. Re-read the task so everything below (the resumable
        # check, the stored note) works from the current row, not from what it was before.
        try:
            await self.db.refresh(task)
        except Exception as exc:
            logger.warning("Could not refresh task %s after interpretation: %s", task.task_id, exc)

        if not interpretation.ok:
            # The message is already saved and the task is untouched. The one thing that can
            # still proceed without the model is an operator who explicitly said "resume":
            # that needs no interpretation, and it is still gated by the real verification.
            if _matches(_AUTHORISE_PATTERNS, normalise_reply(body)):
                return await self._verify_and_dispose(
                    conversation, task, owner_email, body, base, request,
                    clarification=clarification, interpretation=None,
                )
            return await self._reply_agent(
                conversation, task, base,
                "I could not interpret your reply just now, so I have not changed anything. "
                f"{interpretation.error or ''}\n\n"
                f"Your message is saved and task {task.task_id} is still waiting for you "
                f"(outstanding: {request.problem_summary}).\n"
                "Please send it again in a moment. If you have already fixed the problem, you "
                "can also reply with an explicit instruction such as \"resume this task\" and "
                "I will check the real state first.",
                status="NEEDS_MORE_INFO",
                detail=interpretation.category,
            )

        return await self._verify_and_dispose(
            conversation, task, owner_email, body, base, request,
            clarification=clarification, interpretation=interpretation.interpretation,
        )

    # ------------------------------------------------------------------
    async def _verify_and_dispose(
        self,
        conversation: AgentConversation,
        task: Task,
        owner_email: str,
        body: str,
        base: Dict[str, Any],
        request,
        *,
        clarification: Optional[str],
        interpretation: Optional[ReplyInterpretation],
    ) -> ReplyOutcome:
        """Turn a model proposal into an action, or into a question. Code decides, not the model.

        `interpretation` is None only on the explicit-instruction path where the model was
        unavailable; every other caller passes a validated decision.
        """
        decision = interpretation.decision if interpretation is not None else None

        # --- The model may suggest these; it can never perform them. ------------
        if decision == ReplyDecision.CANCEL_REQUESTED:
            # Cancelling is final for the task. A model reading is not enough: the operator
            # must say it in words the deterministic matcher accepts (negations included).
            return await self._reply_agent(
                conversation, task, base,
                "It sounds like you may want to cancel this task. I have not cancelled it. "
                "If you do, reply \"cancel this task\" (or the Bengali equivalent) and I will; "
                "otherwise tell me what you would like me to do.",
                status="NEEDS_MORE_INFO",
            )

        if decision == ReplyDecision.EXPLICIT_APPROVAL_REQUIRED:
            return await self._reply_agent(
                conversation, task, base,
                "That reads as an approval or a decision on a sensitive action. A chat reply is "
                "not an approval, and I will not treat it as one. Spending, signing, wallet and "
                "transaction decisions go through the explicit authorisation controls, and the "
                "task stays parked until then.\n\n"
                f"Outstanding: {request.problem_summary}",
                status="NEEDS_MORE_INFO",
            )

        if decision == ReplyDecision.NEEDS_CLARIFICATION or (
            decision == ReplyDecision.SUFFICIENT_INFORMATION
            and not interpretation.extracted_information
        ):
            # A "sufficient" verdict with nothing extracted is not information. Treat it as a
            # request for it rather than resuming on the model's say-so.
            return await self._reply_agent(
                conversation, task, base,
                self._follow_up_text(task, request, interpretation),
                status="NEEDS_MORE_INFO",
            )

        # --- Record what the operator actually supplied BEFORE anything can resume. ----
        information: Optional[str] = None
        if decision == ReplyDecision.SUFFICIENT_INFORMATION:
            information = interpretation.clarification_text()
            if information and not (clarification and clarification.lower() in information.lower()):
                self._store_clarification(task, f"Operator-provided information: {information}")
                await self.db.flush()

        # --- Check the real condition where it can be checked. ---------------------
        await events.publish(
            events.EV_VERIFICATION_STARTED,
            task_id=task.task_id,
            conversation_id=str(conversation.id),
            request_id=request.request_id,
        )
        result = await verify(self.db, task, request)
        await events.publish(
            events.EV_VERIFICATION_COMPLETED,
            task_id=task.task_id,
            conversation_id=str(conversation.id),
            request_id=request.request_id,
            verified=result.ok,
        )

        if result.ok:
            return await self._resume(
                conversation, task, owner_email, base, request, result,
                clarification=clarification, info_provided=bool(information),
            )

        # Cannot be checked from here. For information that is genuinely the operator's to
        # give (a choice, a resource, a URL) they are the source of truth. For the state of
        # an external system (a login, a session, a challenge) their word is a claim, and a
        # claim never replaces the check.
        if not result.checkable and request.reason_code not in _STATE_BLOCKERS:
            if information or (interpretation is None and operator_information(body)):
                note = information or operator_information(body)
                if interpretation is None:
                    self._store_clarification(task, f"Operator-provided information: {note}")
                    await self.db.flush()
                satisfied = VerificationResult.satisfied(
                    method="operator_provided_information",
                    outcome="Information supplied by the operator in the Agent Inbox "
                    "(not machine-verified; the plan will be revised to use it).",
                    information=note,
                )
                return await self._resume(
                    conversation, task, owner_email, base, request, satisfied,
                    clarification=clarification, info_provided=True,
                )

        question = result.question or (
            "I re-checked and the blocking condition is still in place. "
            "Could you tell me what you did and what you saw?"
        )
        prefix = ""
        if clarification:
            prefix = f"I have recorded your instruction to use {clarification} instead.\n\n"
        elif information:
            prefix = "I have saved the information you gave me for this task.\n\n"
        return await self._reply_agent(
            conversation, task, base,
            f"{prefix}I checked and this task is still blocked.\n\n"
            f"Check performed: {result.method}\nResult: {result.outcome}\n\n{question}",
            status="NEEDS_MORE_INFO",
            verification=_verification_view(result),
        )

    @staticmethod
    def _follow_up_text(task: Task, request, interpretation: Optional[ReplyInterpretation]) -> str:
        """A specific follow-up question, built from the model's but bounded by code."""
        from backend.app.llm.compaction import redact_secrets

        question = ""
        missing: list = []
        if interpretation is not None:
            question = " ".join((interpretation.follow_up_question or "").split())
            missing = list(interpretation.missing_information)
        question = redact_secrets(question)[:_MAX_SHOWN_QUESTION]
        if not question:
            question = (
                "Could you tell me specifically what the task is missing? "
                f"Outstanding: {request.problem_summary}"
            )
        lines = [f"Thanks, but that is not enough for me to continue task {task.task_id}."]
        if missing:
            lines.append("Still missing: " + "; ".join(redact_secrets(m)[:120] for m in missing[:5]) + ".")
        lines.append(question)
        lines.append("The task stays parked until I have it.")
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    async def _cancel(
        self, conversation: AgentConversation, task: Task, owner_email: str, base: Dict[str, Any]
    ) -> ReplyOutcome:
        """Apply the existing cancellation mechanism to this task only."""
        if task.status in _TERMINAL:
            return await self._reply_agent(
                conversation, task, base,
                f"Task {task.task_id} is already {task.status}; there is nothing to cancel.",
                status="RECORDED",
            )
        try:
            await self.tasks.transition(
                task,
                TaskStatus.CANCELLED,
                reason="Cancelled by the operator from the Agent Inbox",
                progress=task.progress,
            )
        except Exception as exc:
            return await self._reply_agent(
                conversation, task, base,
                f"I could not cancel this task: {exc}",
                status="IGNORED",
            )

        await self.interventions.cancel_for_task(
            task.id, reason="Task cancelled by the operator from the Agent Inbox."
        )
        agent_message = await self._reply_agent(
            conversation, task, base,
            f"Task {task.task_id} has been cancelled. Completed work and this conversation are kept.",
            status="CANCELLED",
            resumed=False,
        )
        await events.publish(
            events.EV_TASK_CANCELLED,
            task_id=task.task_id,
            conversation_id=str(conversation.id),
        )
        return agent_message

    async def _record_without_blocker(
        self, conversation: AgentConversation, task: Task, clarification: Optional[str], base: Dict[str, Any]
    ) -> ReplyOutcome:
        """No open blocker: keep the note on the task, do not requeue anything."""
        if clarification is None:
            return await self._reply_agent(
                conversation, task, base,
                f"Task {task.task_id} has no open request for input right now (status {task.status}). "
                "Your message is saved here and I have not changed the task.",
                status="RECORDED",
            )

        return await self._reply_agent(
            conversation, task, base,
            f"Noted for task {task.task_id}: use {clarification} instead.\n\n"
            "I have recorded this against the task and I did not restart it. "
            "Tell me to resume the task and I will pick this up on the next run.",
            status="CLARIFIED",
        )

    async def _handle_authorisation(
        self,
        conversation: AgentConversation,
        task: Task,
        owner_email: str,
        body: str,
        base: Dict[str, Any],
        request,
        *,
        message: Optional[ConversationMessage] = None,
    ) -> ReplyOutcome:
        """Operator pauses, approvals and ambiguities need an explicit instruction.

        The model is consulted here only to understand and record what the operator said. It has
        no say in whether the task resumes: an informational reply, however useful, never
        counts as authorisation.
        """
        if not _matches(_AUTHORISE_PATTERNS, normalise_reply(body)):
            saved = ""
            question = ""
            try:
                interpretation = await self._interpreter(
                    self.db,
                    task=task,
                    conversation=conversation,
                    request=request,
                    reply=body,
                    current_message_id=message.id if message is not None else None,
                )
            except Exception as exc:  # an interpreter must not break the safe default
                logger.warning("Interpreter failed on an authorisation reply: %s", type(exc).__name__)
                interpretation = InterpretationResult.failure("interpreter error", "ERROR")
            if interpretation.ok and interpretation.interpretation is not None:
                decided = interpretation.interpretation
                if (
                    decided.decision == ReplyDecision.SUFFICIENT_INFORMATION
                    and decided.extracted_information
                ):
                    note = decided.clarification_text()
                    self._store_clarification(task, f"Operator-provided information: {note}")
                    await self.db.flush()
                    saved = "I have saved the information you gave me for this task.\n\n"
                elif decided.decision == ReplyDecision.NEEDS_CLARIFICATION and decided.follow_up_question:
                    question = "\n\n" + self._follow_up_text(task, request, decided)
            return await self._reply_agent(
                conversation, task, base,
                f"{saved}I will not continue this task on an informational message alone.\n\n"
                f"Outstanding: {request.problem_summary}\n"
                "If you have resolved it, reply with an explicit instruction such as "
                "\"resume this task\" and I will continue. If you meant to change the task "
                f"instead, tell me what to change.{question}",
                status="NEEDS_MORE_INFO",
            )

        clarification = self._extract_clarification(body)
        result = VerificationResult.satisfied(
            method="operator_authorization",
            outcome=f"Explicitly authorised in the Agent Inbox by {owner_email}.",
        )
        return await self._resume(
            conversation, task, owner_email, base, request, result, clarification=clarification
        )

    # ------------------------------------------------------------------
    async def _resume(
        self,
        conversation: AgentConversation,
        task: Task,
        owner_email: str,
        base: Dict[str, Any],
        request,
        result: VerificationResult,
        *,
        clarification: Optional[str] = None,
        info_provided: bool = False,
    ) -> ReplyOutcome:
        """Requeue the SAME task from its last safe state. Never from scratch."""
        eligible, why = await self._resumable(task)
        if not eligible:
            await self.interventions.mark_resolved(
                request,
                resolved_by=owner_email,
                outcome=f"Condition verified but the task was not resumed: {why}",
                verification_method=result.method,
            )
            return await self._reply_agent(
                conversation, task, base,
                f"I verified the condition, but I cannot safely resume this task: {why}",
                status="NEEDS_MORE_INFO",
                detail=why,
                verification=_verification_view(result),
            )

        try:
            # Compare-and-set on the blocked states. If another reply, or the operator's own
            # cancel, already moved this task, this returns False and nothing is requeued --
            # which is what stops two racing replies from producing two workers.
            moved = await self.tasks.transition_if_current(
                task,
                _RESUMABLE_FROM,
                TaskStatus.READY,
                reason=f"Resumed from the Agent Inbox after reply on {request.request_id}",
                # Without this the transition would reset progress to the default for READY.
                progress=task.progress,
            )
        except Exception as exc:
            return await self._reply_agent(
                conversation, task, base,
                f"The condition is verified but requeueing failed: {exc}",
                status="NEEDS_MORE_INFO",
                verification=_verification_view(result),
            )

        if not moved:
            return await self._reply_agent(
                conversation, task, base,
                f"This task is already {task.status}; it has not been queued twice.",
                status="RECORDED",
            )

        # READY is claimable as soon as next_execution_at is in the past. Clear any stale value
        # so the waiting operator does not also have to wait out a backoff.
        task.next_execution_at = None
        await self.db.flush()

        await self.interventions.mark_resolved(
            request,
            resolved_by=owner_email,
            outcome=result.outcome,
            verification_method=result.method,
            reply_message_id=base.get("message_id"),
        )

        await events.publish(
            events.EV_RESUME_SCHEDULED,
            task_id=task.task_id,
            conversation_id=str(conversation.id),
            request_id=request.request_id,
            summary=f"Task {task.task_id} queued to resume",
        )

        checkpoint_note = await self._checkpoint_summary(task)
        noted = (
            f"I have recorded your instruction to use {clarification} instead and will apply it "
            "to the plan on the next run.\n\n"
            if clarification
            else ""
        )
        if info_provided:
            noted = (
                "Thanks, I have saved the information you sent and will use it when I revise "
                "the plan on the next run.\n\n"
            )
        label = "Recorded" if info_provided else "Verified"
        return await self._reply_agent(
            conversation, task, base,
            f"{noted}{label}: {result.outcome}\n\n"
            f"Task {task.task_id} is queued to resume from its last saved state."
            f"{checkpoint_note}\n"
            "I will report what actually happens here as it runs.",
            status="RESUMED",
            resumed=True,
            verification=_verification_view(result),
        )

    async def _resumable(self, task: Task) -> tuple[bool, str]:
        """Eligibility checks that must pass before a parked task is requeued."""
        if task.status in _TERMINAL:
            return False, f"it is {task.status}"
        if task.status not in [s.value for s in _RESUMABLE_FROM]:
            return False, f"it is {task.status} and is not waiting for operator input"

        # Never repeat a step whose real-world outcome is unknown.
        try:
            from backend.app.tasks.concurrency import LeaseManager

            uncertain = await LeaseManager(self.db)._uncertain_side_effects(task)
            if uncertain:
                return False, (
                    f"a previous step may already have taken effect ({uncertain}). "
                    "I will not repeat it without you confirming the outcome"
                )
        except Exception as exc:
            logger.warning("Could not check side effects for %s: %s", task.task_id, exc)
            return False, "I could not confirm that repeating the interrupted step is safe"

        # The worker's claim query refuses a task that has used its whole retry budget.
        if task.retry_count > task.max_retries:
            return False, (
                f"it has already used all {task.max_retries} retries. "
                "Please create a new task or raise its retry budget"
            )

        from backend.app.security.emergency_stop import emergency_stop

        if emergency_stop.is_stopped and not emergency_stop.can_execute_tasks():
            return False, "the emergency stop is still engaged"

        return True, ""

    async def _checkpoint_summary(self, task: Task) -> str:
        """Report the real last checkpoint, or say plainly that there is none."""
        from backend.app.tasks.checkpoints import CheckpointManager

        latest = await CheckpointManager(self.db).load_latest(task)
        if latest is None:
            return (
                "\n\nNo checkpoint was saved for this task, so it will restart from its "
                "persisted plan. Steps already marked complete are not repeated."
            )
        iteration = int(latest.state.get("iteration", 0)) if latest.state else 0
        return f"\n\nResuming from checkpoint '{latest.checkpoint_name}' (iteration {iteration})."

    # ------------------------------------------------------------------
    async def _reply_agent(
        self,
        conversation: AgentConversation,
        task: Task,
        base: Dict[str, Any],
        content: str,
        *,
        status: str,
        resumed: bool = False,
        detail: Optional[str] = None,
        verification: Optional[Dict[str, Any]] = None,
    ) -> ReplyOutcome:
        message = await self.conversations.append_message(
            conversation, role="assistant", content=content, kind="AGENT",
        )
        await events.publish(
            events.EV_AGENT_MESSAGE,
            task_id=task.task_id,
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            summary="Agent replied",
        )
        return ReplyOutcome(
            status=status,
            task_id=task.task_id,
            conversation_id=str(conversation.id),
            message_id=str(base["message_id"]),
            request_id=base.get("request_id"),
            task_status=task.status,
            resumed=resumed,
            agent_message_id=str(message.id),
            detail=detail,
            verification=verification,
        )

    # ------------------------------------------------------------------
    def _extract_clarification(self, body: str) -> Optional[str]:
        """Pull a concrete 'use X instead' instruction out of a reply, if there is one.

        Bengali is handled alongside English because the operator may write in either.
        The network is read from the same allow-list either way, so an unfamiliar word is
        never adopted as an instruction.
        """
        normalised = normalise_reply(body)
        for pattern in _CLARIFICATION_PATTERNS:
            match = re.search(pattern, normalised)
            if not match:
                continue
            target = (match.groupdict().get("target") or "").strip()
            if target and _NETWORK_HINT.search(target):
                return target
            if not target:
                # A bare Bengali "use ..." verb: the network comes from the allow-list.
                hint = _NETWORK_HINT.search(body)
                if hint:
                    return hint.group(0)
        if _NETWORK_HINT.search(body) and _matches(_CLARIFICATION_PATTERNS, normalised):
            hint = _NETWORK_HINT.search(body)
            return hint.group(0) if hint else None
        return None

    def _store_clarification(self, task: Task, clarification: str) -> Dict[str, Any]:
        record = {
            "instruction": clarification,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        existing = list(task.operator_instructions or [])
        existing.append(record)
        task.operator_instructions = existing
        return record


def _verification_view(result: VerificationResult) -> Dict[str, Any]:
    return {
        "method": result.method,
        "ok": result.ok,
        "checkable": result.checkable,
        "outcome": result.outcome,
        "detail": result.detail or {},
    }