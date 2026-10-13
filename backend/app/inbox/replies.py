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

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.inbox import events
from backend.app.inbox.service import (
    REQUIRES_EXPLICIT_AUTHORIZATION,
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
)

# Explicit authorisation. Required for operator pauses, approvals and ambiguous instructions.
_AUTHORISE_PATTERNS = (
    r"\bi (?:confirm|authorise|authorize) (?:and )?(?:resume|continue|proceed)\b",
    r"\bresume (?:this|the) task\b",
    r"\bcontinue (?:this|the) task\b",
    r"\byes[, ]+resume\b",
    r"\bgo ahead (?:and|with) resume\b",
    r"\bproceed (?:with|to) (?:this|the) task\b",
)

# Directives that change the task's direction rather than clear its blocker.
_CLARIFICATION_PATTERNS = (
    r"\buse (?P<target>[a-z0-9 \-]{2,40}?)(?: instead| rather)\b",
    r"\bswitch to (?P<target>[a-z0-9 \-]{2,40}?)(?: instead)?\b",
    r"\bchange (?:the )?network to (?P<target>[a-z0-9 \-]{2,40}?)(?: instead)?\b",
)

_NETWORK_HINT = re.compile(
    r"\b(ethereum|eth|polygon|matic|bsc|bnb|binance|arbitrum|arb|optimism|op|avalanche|avax|"
    r"base|fantom|scroll|linea|zksync|solana|sol|celo|gnosis|mainnet|testnet|sepolia)\b",
    re.IGNORECASE,
)


def _matches(patterns, text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


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

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.conversations = ConversationService(db)
        self.interventions = InterventionService(db)
        self.tasks = TaskManager(db)

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
        message = await self.conversations.append_message(
            conversation,
            role="user",
            content=body,
            kind="USER",
            intervention_request_id=request.request_id if request is not None else None,
            client_token=client_token,
        )

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
        if _matches(_CANCEL_PATTERNS, normalise_reply(body)):
            return await self._cancel(conversation, task, owner_email, base)

        # --- A note with no open blocker: record it, change nothing. ------------
        if request is None:
            return await self._record_without_blocker(conversation, task, clarification, base)

        # --- Operator pause / approval / ambiguity: explicit intent required. ----
        if request.reason_code in REQUIRES_EXPLICIT_AUTHORIZATION:
            return await self._handle_authorisation(conversation, task, owner_email, body, base, request)

        # --- Machine-verifiable blocker: check the real condition. --------------
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

        if not result.ok:
            question = result.question or (
                "I re-checked and the blocking condition is still in place. "
                "Could you tell me what you did and what you saw?"
            )
            noted = (
                f"I have recorded your instruction to use {clarification} instead.\n\n"
                if clarification
                else ""
            )
            return await self._reply_agent(
                conversation, task, base,
                f"{noted}I checked and this task is still blocked.\n\n"
                f"Check performed: {result.method}\nResult: {result.outcome}\n\n{question}",
                status="NEEDS_MORE_INFO",
                verification=_verification_view(result),
            )

        return await self._resume(
            conversation, task, owner_email, base, request, result, clarification=clarification
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
        """Operator pauses, approvals and ambiguities need an explicit instruction."""
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
        return await self._reply_agent(
            conversation, task, base,
            f"{noted}Verified: {result.outcome}\n\n"
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
        """Pull a concrete 'use X instead' instruction out of a reply, if there is one."""
        for pattern in _CLARIFICATION_PATTERNS:
            match = re.search(pattern, normalise_reply(body))
            if not match:
                continue
            target = (match.groupdict().get("target") or "").strip()
            if target and _NETWORK_HINT.search(target):
                return target
        if _NETWORK_HINT.search(body) and _matches(_CLARIFICATION_PATTERNS, normalise_reply(body)):
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