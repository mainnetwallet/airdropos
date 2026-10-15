"""Routing a reply from the Agent Inbox back to the right task, and resuming it safely.

The order of operations here is deliberate:

1. resolve the conversation server-side and prove ownership;
2. resolve the task *from that conversation*, never from a client-supplied task id;
3. confirm the intervention request is still open;
4. persist the reply (idempotently, and committed, before anything else happens);
5. have the fixed LLM *interpret* the reply against the task, the blocker and the checkpoint;
6. validate that proposal with deterministic code and verify the blocking condition for real;
7. only then requeue the task.

The LLM proposes, this module disposes. A model decision is never authorisation and never
verification: it cannot clear an operator pause or approval, it cannot cancel a task, and a
"sufficient information" verdict still has to pass whatever real check exists for the blocker.
Every branch that does not reach step 7 says why, in the same conversation, and leaves the
task parked. A reply in one conversation can never move another task.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.inbox import events
from backend.app.agent.guidance import build_record_context
from backend.app.inbox.interpretation import (
    InterpretationError,
    ReplyDecision,
    ReplyInterpretation,
    ReplyInterpreter,
    build_context,
    contains_secret,
)
from backend.app.inbox.service import (
    APPROVAL_GATED,
    INFORMATION_RESOLVABLE,
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

# Explicit authorisation. Required for operator pauses and approvals. Not needed to answer a
# clarification question: a grounded answer resumes an ambiguous task by itself.
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

# A chat reply is information, never approval of a transaction, spend or signature. These two
# groups are matched together: approval wording alongside a financial action ("yes it is
# approved, send the transaction") is never processed as an answer to a clarification question.
# This is a deterministic backstop behind the model's own EXPLICIT_APPROVAL_REQUIRED decision.
_APPROVAL_WORDING = re.compile(
    r"\b(?:approv\w*|authori[sz]\w*|permission|consent\w*|grant\w*|confirm\w*|"
    r"go ahead|go-ahead|sign[- ]?off|okay to|ok to)\b"
    r"|অনুমোদন|অনুমতি|সম্মতি",
    re.IGNORECASE,
)
_FINANCIAL_ACTION = re.compile(
    r"\b(?:transactions?|transfer\w*|send\w*|sent|pay\w*|spend\w*|spent|swap\w*|"
    r"withdraw\w*|bridg\w*|stak\w*|purchas\w*|buy\w*|sign\w*|signature|tx|funds?)\b"
    r"|লেনদেন|ট্রান্সফার|পাঠা|পেমেন্ট|সই|খরচ|কিন(?:ুন|ো)",
    re.IGNORECASE,
)


def claims_financial_approval(*texts: str) -> bool:
    """Whether text tries to approve a transaction, spend or signature."""
    joined = " ".join(t for t in texts if t)
    return bool(_APPROVAL_WORDING.search(joined) and _FINANCIAL_ACTION.search(joined))


_FINANCIAL_APPROVAL_NOTICE = (
    "What you wrote looks like an approval for a transaction, spend or signature. A chat "
    "message cannot grant that, so I did not treat it as an answer, I have not saved it and "
    "I have not changed the task.\n\n"
    "Outstanding: {outstanding}\n"
    "Approve it through the approval flow, then answer the question above on its own."
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
    interpretation: Optional[Dict[str, Any]] = None

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
        if self.interpretation:
            payload["interpretation"] = self.interpretation
        return payload


class ReplyRouter:
    """Handles one user reply against one conversation."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.conversations = ConversationService(db)
        self.interventions = InterventionService(db)
        self.tasks = TaskManager(db)
        # The instruction record written for the reply being handled, if any, so a richer
        # interpreted record can replace it instead of sitting beside it.
        self._turn_record: Optional[Dict[str, Any]] = None

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
        # Checked before anything expensive: a repeated token never reaches the LLM.
        if client_token:
            previous = await self.conversations.find_by_client_token(conversation.id, client_token)
            if previous is not None:
                return self._duplicate_outcome(conversation, task, previous)

        request = await self.interventions.active_for_conversation(conversation.id)

        # --- 5. Persist the reply before anything can change the task. -----------
        # A savepoint, so two simultaneous submits of the same token lose cleanly to the
        # unique constraint instead of both going on to call the model.
        try:
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
            # The savepoint rollback expires everything it touched, including `conversation`;
            # reload it before reading any attribute (async sessions cannot lazy-load).
            await self.db.refresh(conversation)
            previous = await self.conversations.find_by_client_token(
                conversation.id, client_token or ""
            )
            if previous is None:
                raise
            return self._duplicate_outcome(conversation, task, previous)

        # Committed here, not left to the endpoint: interpretation can take a while and can
        # fail in ways that raise. The operator's message must survive either.
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

    def _duplicate_outcome(
        self, conversation: AgentConversation, task: Task, previous: ConversationMessage
    ) -> ReplyOutcome:
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

        clarification = self._extract_clarification(body)
        self._turn_record: Optional[Dict[str, Any]] = None

        # --- A stopped task never resumes, whatever the reply says. -------------
        if task.status == TaskStatus.CANCELLED.value:
            return await self._reply_agent(
                conversation, task, base,
                f"Task {task.task_id} was cancelled, so it will not be resumed. "
                "Your message is saved in this conversation for the record.",
                status="RECORDED",
            )

        normalised = normalise_reply(body)

        # --- Explicit cancellation, scoped to this task only. -------------------
        # Deterministic on purpose: the model may suggest a cancellation, it may not make one.
        if _is_cancellation(normalised):
            return await self._cancel(conversation, task, owner_email, base)

        # --- Messages that must never be treated as an answer. ------------------
        # These return before anything is stored or sent to the model, so a refused message
        # leaves no instruction behind that a later bare "resume" could pick up.
        if request is not None:
            refusal = await self._refuse_unsafe_reply(
                conversation, task, base, request, body, normalised
            )
            if refusal is not None:
                return refusal

        # A directive ("use X instead") is an instruction, not a claim that the blocker was
        # fixed. Record it against the task first so it survives whichever branch follows --
        # losing an operator instruction because a request happened to be open would be a bug.
        # It is tagged with the open request (if any), so it only ever counts as an answer to
        # *that* blocker.
        if clarification is not None:
            self._turn_record = self._store_clarification(task, clarification, request)
            await self.db.flush()

        # --- A note with no open blocker: record it, change nothing. ------------
        if request is None:
            return await self._record_without_blocker(conversation, task, clarification, base)

        explicit = _matches(_AUTHORISE_PATTERNS, normalised)

        # --- An explicit instruction ("resume this task"). -----------------------
        # The model is still asked, best effort, so an answer given alongside "resume this
        # task" ("use the second account, and resume") is not thrown away.
        if explicit:
            interpretation = await self._try_interpret(conversation, task, request, body, message)
            persisted = False
            if (
                interpretation is not None
                and interpretation.kind is ReplyDecision.SUFFICIENT_INFORMATION
            ):
                await self._persist_information(
                    task, interpretation, clarification, message, request=request
                )
                persisted = True
            # A pause or an approval is cleared only by this explicit instruction.
            if request.reason_code in APPROVAL_GATED:
                return await self._handle_authorisation(
                    conversation, task, owner_email, body, base, request
                )
            # Every other blocker still has to be resolved: verified for real where a check
            # exists, or answered by the operator where it does not.
            return await self._verify_then_resume(
                conversation, task, owner_email, base, request,
                clarification=clarification,
                interpretation=interpretation,
                info_saved=persisted,
            )

        # --- Everything else is interpreted by the fixed LLM. -------------------
        interpretation, failure = await self._interpret(conversation, task, request, body, message)
        if interpretation is None:
            if self._reply_confirms_action(request):
                # The model being unavailable must not hold up a task that is only waiting
                # for the operator to say they did the step.
                return await self._verify_then_resume(
                    conversation, task, owner_email, base, request,
                    clarification=clarification, interpretation=None, info_saved=False,
                )
            return await self._park_after_failed_interpretation(
                conversation, task, base, request, failure
            )

        return await self._dispose(
            conversation, task, owner_email, base, request, message,
            interpretation, clarification,
        )

    async def _refuse_unsafe_reply(
        self, conversation, task, base, request, body: str, normalised: str
    ) -> Optional[ReplyOutcome]:
        """Refuse, without acting, a reply that must never move a task. None if it is fine."""
        # A secret is never sent to the model, and an explicit "resume" riding on the same
        # message is not acted on either: the operator should resend without the secret.
        # Cancellation was handled before this, so a stop is never blocked by it.
        if contains_secret(body):
            return await self._reply_agent(
                conversation, task, base,
                "Your message looks like it contains a secret (a key, seed phrase or token). "
                "I did not send it to the language model and I have not acted on it, so the "
                "task has not been resumed. Please never share secrets here. Send your "
                "instruction again without the secret.\n\n"
                f"Outstanding: {request.problem_summary}",
                status="NEEDS_MORE_INFO",
                detail="secret_in_reply",
            )

        # Approving a transaction or spend is not something a chat answer can do. Pauses and
        # approvals have their own handling (and their own message); for every other blocker
        # such a message is not treated as an answer at all: not saved, not interpreted.
        if request.reason_code not in APPROVAL_GATED and claims_financial_approval(normalised):
            return await self._reply_agent(
                conversation, task, base,
                _FINANCIAL_APPROVAL_NOTICE.format(outstanding=request.problem_summary),
                status="NEEDS_MORE_INFO",
                detail="financial_approval_in_reply",
            )
        return None

    # ------------------------------------------------------------------
    # Interpretation
    # ------------------------------------------------------------------
    async def _interpret(
        self, conversation, task, request, body: str, message: ConversationMessage
    ) -> tuple[Optional[ReplyInterpretation], Optional[str]]:
        """Ask the fixed LLM what the reply achieves. Never raises."""
        try:
            context = await build_context(
                self.db, task=task, request=request, conversation=conversation,
                reply=body, current_message_id=message.id,
            )
            interpretation = await ReplyInterpreter().interpret(context, body)
        except InterpretationError as exc:
            logger.warning("Reply on %s was not interpreted: %s", task.task_id, exc.category)
            return None, exc.category
        except Exception as exc:  # defensive: interpretation must never take the reply down
            logger.exception("Unexpected failure interpreting a reply on %s", task.task_id)
            return None, type(exc).__name__
        logger.info(
            "Reply on %s interpreted as %s", task.task_id, interpretation.decision
        )
        return interpretation, None

    async def _try_interpret(
        self, conversation, task, request, body: str, message: ConversationMessage
    ) -> Optional[ReplyInterpretation]:
        interpretation, _ = await self._interpret(conversation, task, request, body, message)
        return interpretation

    async def _persist_information(
        self,
        task: Task,
        interpretation: ReplyInterpretation,
        clarification: Optional[str],
        message: ConversationMessage,
        request=None,
    ) -> None:
        """Save the operator's answer where the worker's planner will read it.

        This is the same `operator_instructions` list the controller already folds into its
        next planning cycle, so no new channel into the worker is created. It is written
        *before* any requeue: a worker that claims the task must already find it.
        """
        parts: List[str] = []
        if clarification:
            parts.append(f"use {clarification} instead")
        for item in interpretation.extracted_information:
            if item.lower() not in " ".join(parts).lower():
                parts.append(item)
        text = "Operator answer from the Agent Inbox: " + "; ".join(parts)

        # The regex pass may already have stored this turn's "use X instead"; the richer
        # record replaces it so one reply is one instruction, not two.
        records = [r for r in (task.operator_instructions or []) if r is not self._turn_record]
        records.append(
            {
                "instruction": text[:300],
                "at": datetime.now(timezone.utc).isoformat(),
                "source": "llm_interpreted_reply",
                "message_id": str(message.id),
                # The blocker this answer is for. A later bare "resume" only counts an answer
                # given to the *current* request, never one left over from another.
                "request_id": getattr(message, "intervention_request_id", None),
                # Why the task was parked, the blocked step and the operator's own words, so
                # the worker's planner is not left to guess what this answer is an answer to.
                **build_record_context(request, message.content, interpretation),
            }
        )
        task.operator_instructions = records
        self._turn_record = records[-1]
        await self.db.flush()

    async def _park_after_failed_interpretation(
        self, conversation, task, base, request, failure: Optional[str]
    ) -> ReplyOutcome:
        if failure == "SECRET_IN_REPLY":
            text = (
                "Your message looks like it contains a secret (a key, seed phrase or token). "
                "I did not send it to the language model and I have not acted on it. Please "
                "never share secrets here; tell me what you need in plain words instead."
            )
        else:
            text = (
                "I saved your message, but I could not interpret it just now "
                f"({failure or 'unknown error'}), so I have not changed the task and it stays "
                "parked.\n\n"
                f"Outstanding: {request.problem_summary}\n"
                "Please send your reply again in a moment. If you have already fixed the "
                "problem, you can also reply with an explicit instruction such as "
                "\"resume this task\" and I will check it and continue."
            )
        return await self._reply_agent(
            conversation, task, base, text, status="NEEDS_MORE_INFO",
            detail=f"interpretation_failed:{failure}",
        )

    # ------------------------------------------------------------------
    # Disposition of an interpreted reply
    # ------------------------------------------------------------------
    async def _dispose(
        self,
        conversation: AgentConversation,
        task: Task,
        owner_email: str,
        base: Dict[str, Any],
        request,
        message: ConversationMessage,
        interpretation: ReplyInterpretation,
        clarification: Optional[str],
    ) -> ReplyOutcome:
        decision = interpretation.kind
        view = interpretation.to_view()

        # A pause or an approval is a human decision. Information never clears one, whatever
        # the model concluded; the operator must use the explicit authorisation flow.
        if request.reason_code in APPROVAL_GATED:
            if decision is ReplyDecision.SUFFICIENT_INFORMATION:
                await self._persist_information(
                    task, interpretation, clarification, message, request=request
                )
            if decision is ReplyDecision.CANCEL_REQUESTED:
                return await self._confirm_cancellation(conversation, task, base, view)
            saved = (
                "I have saved the information you gave and will use it when this task runs.\n\n"
                if decision is ReplyDecision.SUFFICIENT_INFORMATION
                else ""
            )
            return await self._reply_agent(
                conversation, task, base,
                f"{saved}I will not continue this task on an informational message alone.\n\n"
                f"Outstanding: {request.problem_summary}\n"
                "This needs your explicit authorisation. When you are ready, reply with an "
                "explicit instruction such as \"resume this task\" (or \"এখন চালাও\"). If you "
                "meant to change the task instead, tell me what to change.",
                status="NEEDS_MORE_INFO",
                interpretation=view,
            )

        if decision is ReplyDecision.CANCEL_REQUESTED:
            return await self._confirm_cancellation(conversation, task, base, view)

        # An answer is information, never approval. Besides the model's own decision, text the
        # model extracted that reads as approving a transaction or spend is refused here too,
        # and it is never saved for the planner.
        if decision is ReplyDecision.SUFFICIENT_INFORMATION and claims_financial_approval(
            *interpretation.extracted_information
        ):
            return await self._reply_agent(
                conversation, task, base,
                _FINANCIAL_APPROVAL_NOTICE.format(outstanding=request.problem_summary),
                status="NEEDS_MORE_INFO",
                detail="financial_approval_in_extracted_information",
                interpretation=view,
            )

        if decision is ReplyDecision.EXPLICIT_APPROVAL_REQUIRED:
            return await self._reply_agent(
                conversation, task, base,
                "What you wrote involves an authorisation (a transaction, a spend, a signature "
                "or lifting a stop). A chat message cannot grant that, so I have not changed "
                "the task.\n\n"
                f"Outstanding: {request.problem_summary}\n"
                "Please approve it through the approval flow, then reply with an explicit "
                "instruction such as \"resume this task\".",
                status="NEEDS_MORE_INFO",
                interpretation=view,
            )

        # Only a blocker that is missing *information* needs a follow-up question. When the
        # task was waiting for the operator to DO something (a CAPTCHA, a login, an install),
        # any reply from them means it is done, so it goes straight on to resume below.
        if decision in (
            ReplyDecision.NEEDS_CLARIFICATION,
            ReplyDecision.UNRELATED_OR_INSUFFICIENT_REPLY,
        ) and not self._reply_confirms_action(request):
            return await self._ask_follow_up(conversation, task, base, request, interpretation, view)

        # SUFFICIENT_INFORMATION or BLOCKER_REQUIRES_VERIFICATION. The model only classified the
        # reply; deterministic code decides. The blocker is verified for real where a check
        # exists, or (for an information blocker with no check) resolved by the operator's
        # grounded answer, and then `_resume` runs the Emergency Stop, retry-budget and
        # uncertain-side-effect checks. If all pass the task resumes by itself: the operator
        # does not have to add "resume" to an answer.
        sufficient = decision is ReplyDecision.SUFFICIENT_INFORMATION
        if sufficient:
            await self._persist_information(
                    task, interpretation, clarification, message, request=request
                )

        return await self._verify_then_resume(
            conversation, task, owner_email, base, request,
            clarification=None if sufficient else clarification,
            interpretation=interpretation,
            info_saved=sufficient,
        )

    async def _verify_then_resume(
        self,
        conversation: AgentConversation,
        task: Task,
        owner_email: str,
        base: Dict[str, Any],
        request,
        *,
        clarification: Optional[str],
        interpretation: Optional[ReplyInterpretation],
        info_saved: bool = False,
    ) -> ReplyOutcome:
        """Run the real check for this blocker; resume only if it allows."""
        view = interpretation.to_view() if interpretation is not None else None

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
                clarification=clarification, info_provided=info_saved, interpretation=view,
            )

        # The automatic check could not confirm the blocker is gone (or said it is not). That
        # never holds the task back: the operator is the one running this agent, and their
        # reply is the confirmation. The check result is kept in the message as a note, and
        # the planner is told the manual step was done so it does not ask for it again.
        # A blocker that is *missing information* is resumed with that information as before.
        # `_resume` still runs its own checks (Emergency Stop, retry budget, uncertain side
        # effects) before anything is queued.
        if (
            not result.checkable
            and request.reason_code in INFORMATION_RESOLVABLE
            and (info_saved or self._information_on_file(task, request))
        ):
            accepted = VerificationResult.satisfied(
                method="operator_provided_information",
                outcome="Information supplied by the operator in the Agent Inbox "
                "(not machine-verified; the plan will be revised to use it).",
            )
            return await self._resume(
                conversation, task, owner_email, base, request, accepted,
                clarification=clarification, info_provided=True, interpretation=view,
            )

        if not self._reply_confirms_action(request):
            # A blocker that is missing *information*: resuming without it would only park
            # the task again, so the reply has to actually carry the answer.
            question = result.question or (
                "I need the missing information to continue. Could you tell me what to use?"
            )
            noted = ""
            if info_saved:
                noted = "I have saved the information you gave and will use it once the task can run.\n\n"
            elif clarification:
                noted = f"I have recorded your instruction to use {clarification} instead.\n\n"
            return await self._reply_agent(
                conversation, task, base,
                f"{noted}I still need your answer before this task can continue.\n\n"
                f"Check performed: {result.method}\nResult: {result.outcome}\n\n{question}",
                status="NEEDS_MORE_INFO",
                verification=_verification_view(result),
                interpretation=view,
            )

        if not info_saved:
            self._record_operator_confirmation(task, request, base)
            await self.db.flush()
        outcome = "Confirmed by the operator in the Agent Inbox."
        if result.checkable and not result.ok:
            # A conclusive negative is worth showing; an unchecked one is just noise.
            outcome += f" (Automatic check said: {result.outcome} Resuming anyway.)"
        accepted = VerificationResult.satisfied(
            method="operator_confirmed",
            outcome=outcome,
            automatic_check=result.method,
            automatic_check_checkable=result.checkable,
            automatic_check_ok=result.ok,
        )
        return await self._resume(
            conversation, task, owner_email, base, request, accepted,
            clarification=clarification, info_provided=info_saved, interpretation=view,
        )

    def _record_operator_confirmation(self, task: Task, request, base: Dict[str, Any]) -> None:
        """Tell the planner the operator did the manual step this request was waiting on.

        Goes through the same `operator_instructions` list the worker already folds into its
        next planning cycle. Skipped when the operator has already answered this request.
        """
        if request is None or self._information_on_file(task, request):
            return
        step = (request.blocking_step or request.problem_summary or "the manual step").strip()
        records = list(task.operator_instructions or [])
        records.append(
            {
                "instruction": (
                    "Operator confirmed in the Agent Inbox that they completed the manual step "
                    f"this task was waiting on: {step}"
                )[:300],
                "at": datetime.now(timezone.utc).isoformat(),
                "source": "operator_confirmation",
                "message_id": str(base.get("message_id")),
                "request_id": request.request_id,
                **build_record_context(request, ""),
            }
        )
        task.operator_instructions = records

    async def _ask_follow_up(
        self, conversation, task, base, request, interpretation: ReplyInterpretation, view
    ) -> ReplyOutcome:
        """Ask for exactly what is missing and leave the task parked."""
        questions = interpretation.missing_information
        if questions:
            ask = "To continue I still need:\n" + "\n".join(f"- {q}" for q in questions)
        else:
            ask = (
                "I could not tell how that answers what the task is waiting for. "
                f"Could you tell me specifically how to resolve: {request.problem_summary}"
            )
        return await self._reply_agent(
            conversation, task, base,
            f"Thanks. {interpretation.explanation}\n\n{ask}\n\n"
            "The task is still parked; I have not changed it.",
            status="NEEDS_MORE_INFO",
            interpretation=view,
        )

    async def _confirm_cancellation(self, conversation, task, base, view) -> ReplyOutcome:
        """The model may suggest cancelling; only the operator's explicit words cancel."""
        return await self._reply_agent(
            conversation, task, base,
            "It sounds like you want to stop this task. Cancelling cannot be undone, so I "
            "have not done it.\n\n"
            "If you do want it cancelled, reply \"cancel this task\" (or \"টাস্কটি বাতিল করো\"). "
            "Otherwise tell me how you want to proceed.",
            status="NEEDS_MORE_INFO",
            interpretation=view,
        )

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
    ) -> ReplyOutcome:
        """Operator pauses and approvals need an explicit instruction."""
        if not _matches(_AUTHORISE_PATTERNS, normalise_reply(body)):
            return await self._reply_agent(
                conversation, task, base,
                f"I will not continue this task on an informational message alone.\n\n"
                f"Outstanding: {request.problem_summary}\n"
                "If you have resolved it, reply with an explicit instruction such as "
                "\"resume this task\" and I will continue. If you meant to change the task "
                "instead, tell me what to change.",
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
        interpretation: Optional[Dict[str, Any]] = None,
    ) -> ReplyOutcome:
        """Requeue the SAME task from its last safe state. Never from scratch."""
        eligible, why = await self._resumable(task)
        if not eligible:
            # The request stays OPEN. Closing it here (as this branch used to) left the task
            # parked with nothing to reply to, so once the cause was cleared -- an engaged
            # Emergency Stop being lifted, say -- the operator had no way to resume it.
            return await self._reply_agent(
                conversation, task, base,
                f"I cannot safely resume this task yet: {why}. Nothing was changed and your request "
                "is still open; reply again once that is sorted out.",
                status="NEEDS_MORE_INFO",
                detail=why,
                verification=_verification_view(result),
                interpretation=interpretation,
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
                interpretation=interpretation,
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
        verified = result.method not in ("operator_provided_information", "operator_confirmed")
        if info_provided:
            noted = (
                "Thanks, I have saved the information you sent and will use it when I revise "
                "the plan on the next run.\n\n"
            )
        label = "Verified" if verified else "Recorded"
        return await self._reply_agent(
            conversation, task, base,
            f"{noted}{label}: {result.outcome}\n\n"
            f"Task {task.task_id} is queued to resume from its last saved state."
            f"{checkpoint_note}\n"
            "I will report what actually happens here as it runs.",
            status="RESUMED",
            resumed=True,
            verification=_verification_view(result),
            interpretation=interpretation,
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
        interpretation: Optional[Dict[str, Any]] = None,
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
            interpretation=interpretation,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _reply_confirms_action(request) -> bool:
        """Whether this blocker is waiting for the operator to DO something.

        True for session, login, CAPTCHA, extension and similar blockers: the operator's reply
        is their confirmation that it is done. False for pauses and approvals, which have
        their own explicit-instruction flow, and for blockers that are missing information,
        where the reply has to actually contain it.
        """
        if request is None:
            return False
        return (
            request.reason_code not in APPROVAL_GATED
            and request.reason_code not in INFORMATION_RESOLVABLE
        )

    @staticmethod
    def _information_on_file(task: Task, request) -> bool:
        """Whether the operator has already answered *this* blocker.

        Lets a later bare "resume this task" finish a resume that was blocked after the answer
        was saved (an engaged Emergency Stop, say) without asking the same question again.
        Records without this request's id -- another blocker's, or from before requests were
        tagged -- never count, and neither does anything when there is no open request.
        """
        if request is None:
            return False
        return any(
            isinstance(r, dict) and r.get("request_id") == request.request_id
            for r in (task.operator_instructions or [])
        )

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

    def _store_clarification(
        self, task: Task, clarification: str, request=None
    ) -> Dict[str, Any]:
        record = {
            "instruction": clarification,
            "at": datetime.now(timezone.utc).isoformat(),
            "request_id": request.request_id if request is not None else None,
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