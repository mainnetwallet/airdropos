"""Durable, task-scoped conversations and structured human-intervention requests.

One live conversation per task, owned by the authenticated operator. A conversation is a
disposable view of "this task needed a person": deleting it (or its task) removes it for good,
and a later user action on a task that still exists opens a brand-new conversation. An intervention request is
persisted *before* the agent's message is published, so the conversation can never advertise a
request that does not exist, and the message can never claim a checkpoint was saved when the
save actually failed.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    AgentConversation,
    ConversationMessage,
    InterventionRequest,
    Task,
)

logger = logging.getLogger(__name__)

# The channel every Agent Inbox conversation is recorded under.
CHANNEL_TASK_INBOX = "TASK_INBOX"

MAX_SUMMARY = 500
MAX_MESSAGE_CHARS = 8000

# Used only for tasks created before task ownership was recorded, so an intervention can still
# be raised and shown rather than silently dropped. Defined here rather than in the agent
# controller because the controller and the concurrency layer import each other.
OPERATOR_FALLBACK_EMAIL = "operator@airdropos.local"


class InterventionReason(str, Enum):
    """Why a task is waiting on a person. Drives verification and the UI."""

    MISSING_AUTHENTICATION = "MISSING_AUTHENTICATION"
    TELEGRAM_SESSION_UNAVAILABLE = "TELEGRAM_SESSION_UNAVAILABLE"
    BROWSER_SESSION_UNAVAILABLE = "BROWSER_SESSION_UNAVAILABLE"
    EXTENSION_INSTALL_REQUIRED = "EXTENSION_INSTALL_REQUIRED"
    HUMAN_VERIFICATION = "HUMAN_VERIFICATION"
    MISSING_CONFIGURATION = "MISSING_CONFIGURATION"
    AMBIGUOUS_INSTRUCTION = "AMBIGUOUS_INSTRUCTION"
    OPERATOR_PAUSE = "OPERATOR_PAUSE"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    UNKNOWN = "UNKNOWN"


# Convenience module constants mirroring the enum.
MISSING_AUTHENTICATION = InterventionReason.MISSING_AUTHENTICATION.value
TELEGRAM_SESSION_UNAVAILABLE = InterventionReason.TELEGRAM_SESSION_UNAVAILABLE.value
BROWSER_SESSION_UNAVAILABLE = InterventionReason.BROWSER_SESSION_UNAVAILABLE.value
EXTENSION_INSTALL_REQUIRED = InterventionReason.EXTENSION_INSTALL_REQUIRED.value
HUMAN_VERIFICATION = InterventionReason.HUMAN_VERIFICATION.value
MISSING_CONFIGURATION = InterventionReason.MISSING_CONFIGURATION.value
AMBIGUOUS_INSTRUCTION = InterventionReason.AMBIGUOUS_INSTRUCTION.value
OPERATOR_PAUSE = InterventionReason.OPERATOR_PAUSE.value
HUMAN_APPROVAL = InterventionReason.HUMAN_APPROVAL.value
UNKNOWN_REASON = InterventionReason.UNKNOWN.value

# Reasons whose blocking condition can be checked by the backend. Anything outside this set is
# resolved by explicit operator intent, never by an informational message.
MACHINE_VERIFIABLE = frozenset(
    {
        MISSING_AUTHENTICATION,
        TELEGRAM_SESSION_UNAVAILABLE,
        BROWSER_SESSION_UNAVAILABLE,
        EXTENSION_INSTALL_REQUIRED,
        HUMAN_VERIFICATION,
        MISSING_CONFIGURATION,
    }
)

# Reasons that are a deliberate human decision: an operator pause or an approval. Only an
# explicit instruction ("resume this task") given through the authorisation flow clears one.
# No reply that merely supplies information -- however well an LLM understands it, and however
# grounded it is -- can clear one of these.
#
# AMBIGUOUS_INSTRUCTION is deliberately NOT here. An ambiguity is resolved by the operator
# answering the question, so a grounded answer clears it without a "resume" phrase (see
# INFORMATION_RESOLVABLE). That answer is information, never approval: it cannot authorise a
# transaction or a spend, which stay behind HUMAN_APPROVAL.
REQUIRES_EXPLICIT_AUTHORIZATION = frozenset({OPERATOR_PAUSE, HUMAN_APPROVAL})

# The same set under the name the routing code uses. One definition, so the API flag
# `requires_explicit_authorization` and the router can never disagree.
APPROVAL_GATED = REQUIRES_EXPLICIT_AUTHORIZATION

# Reasons whose blocker is missing *information* rather than a session, credential or human
# control. When no automatic check exists, an operator's substantive, grounded answer clears
# these and the task resumes on its own -- after the usual resume checks (Emergency Stop, retry
# budget, uncertain side effects) -- without a separate "resume" phrase.
# Session / authentication / CAPTCHA reasons are deliberately absent: "I logged in" is a claim,
# not proof, and only a real check may satisfy them.
INFORMATION_RESOLVABLE = frozenset({AMBIGUOUS_INSTRUCTION, MISSING_CONFIGURATION, UNKNOWN_REASON})

# Substrings in the real failure text that identify each reason. Checked in this order, so the
# more specific patterns come first.
_REASON_SIGNATURES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    # A Chrome Web Store install needs a person to press "Add to Chrome". Listed first: the
    # wording is unmistakable and must not fall through to UNKNOWN (which has no real check).
    (EXTENSION_INSTALL_REQUIRED, (
        "add to chrome", "chrome web store", "web store listing", "install the extension",
    )),
    (TELEGRAM_SESSION_UNAVAILABLE, (
        "telegram session", "telethon", "authkeyunregistered", "sessionrevoked",
        "sessionexpired", "userdeactivated", "sessionpasswordneeded", "apiidinvalid",
        "authkeyduplicated", "not logged in", "notloggedin",
    )),
    (BROWSER_SESSION_UNAVAILABLE, (
        "browser session", "chrome profile", "browser is not", "no browser",
        "cdp", "playwright", "browser profile",
    )),
    (HUMAN_VERIFICATION, (
        "captcha", "human verification", "mfa", "2fa", "two-factor", "otp",
        "verification code", "cloudflare", "hcaptcha", "recaptcha",
    )),
    (MISSING_AUTHENTICATION, (
        "not authenticated", "missing credentials", "unauthorized", "401",
        "forbidden", "access denied", "authentication failed", "auth failure",
        "missing wallet", "no wallet",
    )),
    (MISSING_CONFIGURATION, (
        "not configured", "missing configuration", "no api key", "api_id",
        "api_hash", "configuration error", "set the environment", "env var",
    )),
    (OPERATOR_PAUSE, ("emergency stop", "operator: pause", "paused by an operator", "manual pause")),
    (HUMAN_APPROVAL, ("approval required", "awaiting approval", "needs approval", "sign this")),
    (AMBIGUOUS_INSTRUCTION, ("ambiguous", "unclear which", "could refer to multiple tasks")),
)


def classify_reason(error_text: str, error_category: Optional[str] = None) -> str:
    """Map a real failure onto an intervention reason.

    Never guesses an actionable instruction: when nothing matches it returns UNKNOWN, which
    the caller reports as a diagnostic question rather than a fabricated fix.
    """
    text = (error_text or "").lower()
    if error_category:
        text = f"{text} {error_category.lower()}"

    for reason, needles in _REASON_SIGNATURES:
        if any(needle in text for needle in needles):
            return reason

    # The engine's own taxonomy already names several of these precisely.
    if error_category:
        category = error_category.upper()
        if category == "AUTH_FAILURE":
            return MISSING_AUTHENTICATION
        if category == "BROWSER_SESSION_EXPIRED":
            return BROWSER_SESSION_UNAVAILABLE
        if category == "HUMAN_INTERVENTION_REQUIRED":
            return UNKNOWN_REASON

    return UNKNOWN_REASON


class ConversationService:
    """Reads and writes one task's durable conversation."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Lookup / creation
    # ------------------------------------------------------------------
    @staticmethod
    def conversation_key(task_id: str) -> str:
        """Stable per-task key. Derived from the task id so it can never collide."""
        return f"task:{task_id.upper()}"

    async def get_for_task(self, task_pk: Any, owner_email: str) -> Optional[AgentConversation]:
        """Return this task's conversation for its owner, or None.

        Ownership is checked against the stored `owner_email`, never against a value the
        client supplied, so one operator can never read or answer another's task.
        """
        return await self.db.scalar(
            select(AgentConversation)
            .where(
                AgentConversation.task_id == task_pk,
                AgentConversation.owner_email == owner_email,
                AgentConversation.channel == CHANNEL_TASK_INBOX,
            )
            .limit(1)
        )

    async def get_or_create_for_task(self, task: Task, owner_email: str) -> AgentConversation:
        """Return this task's conversation, creating it on first use.

        Tasks created before ownership was tracked have no `owner_email`. The first operator
        to open a conversation claims the task, which is recorded on both records so the
        assignment is durable and stable from then on.

        Concurrent first-use from two coroutines is resolved by the unique constraint on
        (channel, channel_conversation_id): the loser re-reads the winner's row.
        """
        if task.owner_email is None:
            task.owner_email = owner_email
            await self.db.flush()

        existing = await self.get_for_task(task.id, owner_email)
        if existing is not None:
            return existing

        conversation = AgentConversation(
            channel=CHANNEL_TASK_INBOX,
            channel_conversation_id=self.conversation_key(task.task_id),
            title=task.title,
            task_id=task.id,
            owner_email=owner_email,
            status="OPEN",
            requires_reply=False,
            last_seq=0,
        )
        # The insert runs inside a SAVEPOINT so that losing a creation race only undoes this
        # one insert. A full rollback here would also discard whatever the caller had already
        # flushed in the same transaction (the task's claimed owner, a checkpoint).
        try:
            async with self.db.begin_nested():
                self.db.add(conversation)
                await self.db.flush()
        except IntegrityError:
            # Another writer created it first. Drop ours and take theirs.
            if conversation in self.db:
                self.db.expunge(conversation)
            existing = await self.get_for_task(task.id, owner_email)
            if existing is None:  # pragma: no cover - only if the row vanished
                raise
            return existing
        return conversation

    async def get_by_id(self, conversation_id: Any, owner_email: str) -> Optional[AgentConversation]:
        """Resolve a conversation and enforce ownership in one place."""
        return await self.db.scalar(
            select(AgentConversation).where(
                AgentConversation.id == conversation_id,
                AgentConversation.owner_email == owner_email,
            )
        )

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------
    async def delete_conversation(self, conversation: AgentConversation) -> Dict[str, int]:
        """Remove ONE conversation with its messages and intervention requests.

        Written as explicit DELETE statements instead of leaning on the database's ON DELETE
        CASCADE. SQLite does not enforce foreign keys here, and a database whose Inbox tables
        predate their foreign keys has none to cascade, so a cascade-only delete would leave
        orphaned rows behind. The Task is never touched: nothing below names the tasks table.

        Flushed, not committed; the caller owns the transaction. The conversation row is gone
        when this returns, so its (channel, key) slot is free for a fresh conversation.
        """
        conversation_id = conversation.id
        requests = await self.db.execute(
            delete(InterventionRequest).where(InterventionRequest.conversation_id == conversation_id)
        )
        messages = await self.db.execute(
            delete(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id)
        )
        await self.db.execute(delete(AgentConversation).where(AgentConversation.id == conversation_id))
        # The ORM may still hold the removed row (and its loaded `messages`); forget it so a
        # later lookup in this session cannot hand back a conversation that no longer exists.
        if conversation in self.db:
            self.db.expunge(conversation)
        await self.db.flush()
        return {
            "messages": int(messages.rowcount or 0),
            "intervention_requests": int(requests.rowcount or 0),
        }

    async def delete_for_task(self, task_pk: Any) -> Dict[str, int]:
        """Remove every Inbox record that belongs to one task.

        Selected by the task as well as by the conversation, so a message or request whose
        conversation row is already gone (an orphan from before this lifecycle existed) is
        still removed along with its task.
        """
        conversation_ids = [
            row
            for row in (
                await self.db.execute(
                    select(AgentConversation.id).where(AgentConversation.task_id == task_pk)
                )
            ).scalars()
        ]

        requests = await self.db.execute(
            delete(InterventionRequest).where(InterventionRequest.task_id == task_pk)
        )
        messages = await self.db.execute(
            delete(ConversationMessage).where(ConversationMessage.task_id == task_pk)
        )
        removed_messages = int(messages.rowcount or 0)
        removed_requests = int(requests.rowcount or 0)
        if conversation_ids:
            requests = await self.db.execute(
                delete(InterventionRequest).where(
                    InterventionRequest.conversation_id.in_(conversation_ids)
                )
            )
            messages = await self.db.execute(
                delete(ConversationMessage).where(
                    ConversationMessage.conversation_id.in_(conversation_ids)
                )
            )
            removed_requests += int(requests.rowcount or 0)
            removed_messages += int(messages.rowcount or 0)
            await self.db.execute(
                delete(AgentConversation).where(AgentConversation.id.in_(conversation_ids))
            )
        # Drop anything the session still holds for the removed rows.
        for obj in list(self.db.identity_map.values()):
            if isinstance(obj, (AgentConversation, ConversationMessage, InterventionRequest)) and (
                getattr(obj, "task_id", None) == task_pk
            ):
                self.db.expunge(obj)
        await self.db.flush()
        return {
            "conversations": len(conversation_ids),
            "messages": removed_messages,
            "intervention_requests": removed_requests,
        }

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    async def append_message(
        self,
        conversation: AgentConversation,
        *,
        role: str,
        content: str,
        kind: str = "AGENT",
        intervention_request_id: Optional[str] = None,
        client_token: Optional[str] = None,
    ) -> ConversationMessage:
        """Persist a message. Flushed, not committed: the caller owns the transaction.

        The sequence number and the conversation's `last_message_at` move with the message, so
        ordering always comes from durable timestamps and a monotonic counter rather than from
        insertion order.
        """
        conversation.last_seq = (conversation.last_seq or 0) + 1
        message = ConversationMessage(
            conversation_id=conversation.id,
            task_id=conversation.task_id,
            role=role,
            content=(content or "")[:MAX_MESSAGE_CHARS],
            kind=kind,
            seq=conversation.last_seq,
            client_token=client_token,
            intervention_request_id=intervention_request_id,
        )
        self.db.add(message)
        await self.db.flush()

        conversation.last_message_at = message.created_at or datetime.now(timezone.utc)
        await self.db.flush()
        return message

    async def list_messages(
        self, conversation_id: Any, *, limit: int = 100, before_seq: Optional[int] = None
    ) -> List[ConversationMessage]:
        """Newest page of a conversation, returned oldest-first for rendering.

        Ordering is by the persisted sequence, so paging can never reshuffle messages even if
        two rows share a timestamp.
        """
        limit = max(1, min(int(limit), 200))
        stmt = select(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id)
        if before_seq is not None:
            stmt = stmt.where(ConversationMessage.seq < before_seq)
        rows = (
            await self.db.execute(stmt.order_by(ConversationMessage.seq.desc()).limit(limit))
        ).scalars().all()
        return list(reversed(rows))

    async def find_by_client_token(
        self, conversation_id: Any, client_token: str
    ) -> Optional[ConversationMessage]:
        """Look up a previously accepted message so a double-submit is a no-op."""
        return await self.db.scalar(
            select(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.client_token == client_token,
            )
        )

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    @staticmethod
    def message_view(message: ConversationMessage) -> Dict[str, Any]:
        return {
            "id": str(message.id),
            "role": message.role,
            "kind": message.kind,
            "content": message.content,
            "seq": message.seq,
            "created_at": message.created_at.isoformat() if message.created_at else None,
            "intervention_request_id": message.intervention_request_id,
        }

    @staticmethod
    def conversation_view(conversation: AgentConversation, task: Optional[Task] = None) -> Dict[str, Any]:
        return {
            "conversation_id": str(conversation.id),
            "task_id": task.task_id if task is not None else None,
            "title": (task.title if task is not None else None) or conversation.title,
            "task_status": task.status if task is not None else None,
            "task_progress": task.progress if task is not None else None,
            "status": conversation.status,
            "requires_reply": bool(conversation.requires_reply),
            "last_message_at": conversation.last_message_at.isoformat() if conversation.last_message_at else None,
            "last_seq": conversation.last_seq or 0,
        }


class InterventionService:
    """Creates and resolves the persistent records that block a task."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.conversations = ConversationService(db)

    @staticmethod
    def new_request_id() -> str:
        return f"IR-{secrets.token_hex(8).upper()}"

    async def active_for_conversation(self, conversation_id: Any) -> Optional[InterventionRequest]:
        """The one request currently blocking this conversation, if any."""
        return await self.db.scalar(
            select(InterventionRequest)
            .where(
                InterventionRequest.conversation_id == conversation_id,
                InterventionRequest.status == "PENDING",
            )
            .order_by(InterventionRequest.created_at.desc())
            .limit(1)
        )

    async def active_for_task(self, task_pk: Any) -> Optional[InterventionRequest]:
        return await self.db.scalar(
            select(InterventionRequest)
            .where(
                InterventionRequest.task_id == task_pk,
                InterventionRequest.status == "PENDING",
            )
            .order_by(InterventionRequest.created_at.desc())
            .limit(1)
        )

    async def create(
        self,
        *,
        task: Task,
        conversation: AgentConversation,
        reason_code: str,
        problem_summary: str,
        detailed_explanation: Optional[str] = None,
        required_action: Optional[str] = None,
        instructions: Optional[List[str]] = None,
        integration_ref: Optional[str] = None,
        blocking_step: Optional[str] = None,
        error_category: Optional[str] = None,
        checkpoint_name: Optional[str] = None,
    ) -> Tuple[InterventionRequest, bool]:
        """Create a request, or return the existing one for the same unresolved condition.

        Deduplication matters: a worker retry must not stack duplicate requests (or duplicate
        chat messages) on every attempt. A new request is only opened when the task is not
        already blocked on the same reason.
        """
        existing = await self.active_for_conversation(conversation.id)
        if existing is not None and existing.reason_code == reason_code:
            logger.info(
                "Reusing open intervention %s for task %s (%s)",
                existing.request_id, task.task_id, reason_code,
            )
            return existing, False

        if existing is not None:
            # The condition genuinely changed; the old request is no longer the blocker.
            existing.status = "SUPERSEDED"
            existing.resolved_at = datetime.now(timezone.utc)
            existing.verification_outcome = "Superseded by a different blocking condition."

        request = InterventionRequest(
            request_id=self.new_request_id(),
            task_id=task.id,
            conversation_id=conversation.id,
            owner_email=conversation.owner_email,
            reason_code=reason_code,
            problem_summary=(problem_summary or "")[:MAX_SUMMARY] or "Operator input is required.",
            detailed_explanation=detailed_explanation,
            required_action=required_action,
            instructions=list(instructions or []),
            integration_ref=integration_ref,
            blocking_step=blocking_step,
            error_category=error_category,
            checkpoint_name=checkpoint_name,
            status="PENDING",
            created_at_task_status=task.status,
        )
        self.db.add(request)
        await self.db.flush()

        conversation.requires_reply = True
        await self.db.flush()
        return request, True

    async def mark_resolved(
        self,
        request: InterventionRequest,
        *,
        resolved_by: str,
        outcome: str,
        verification_method: Optional[str] = None,
        reply_message_id: Optional[Any] = None,
        operator_reply: Optional[str] = None,
        interpretation: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Close a request. Persisted in the same transaction as the state change it authorises."""
        request.status = "RESOLVED"
        request.resolved_at = datetime.now(timezone.utc)
        request.resolved_by = resolved_by
        request.verification_outcome = outcome[:2000]
        request.verification_method = verification_method
        request.reply_message_id = _as_uuid(reply_message_id)
        await self.db.flush()

        conversation = await self.db.get(AgentConversation, request.conversation_id)
        if conversation is not None and not await self._has_other_pending(conversation.id):
            conversation.requires_reply = False
            await self.db.flush()

        # The task's own execution context records that it was waiting on a person, so
        # that "blocked on a human" stays a fact about this task and not only about this
        # inbox. Cleared here, where the blocker is genuinely resolved, so a resumed run
        # does not go on reporting a blocker nobody is looking at any more.
        if request.task_id is not None:
            try:
                from backend.app.agent.context import TaskContextService
                from backend.app.models import Task as _Task

                task_row = await self.db.get(_Task, request.task_id)
                if task_row is not None:
                    await TaskContextService(self.db).record_human_intervention(
                        task_row,
                        required=False,
                        reason=f"Resolved by {resolved_by}: {outcome}"[:500],
                        resolved=True,
                        resolution={
                            "by": resolved_by,
                            "method": verification_method,
                            "outcome": outcome,
                            "operator_reply": operator_reply,
                            "interpretation": (interpretation or {}).get("explanation"),
                            "decision": (interpretation or {}).get("decision"),
                        },
                    )
            except Exception as exc:  # never let bookkeeping block the resume
                logger.debug("Could not clear the task context blocker: %s", exc)

    async def cancel_for_task(self, task_pk: Any, *, reason: str) -> int:
        """Close every open request on a task. Used when the task is cancelled."""
        rows = (
            await self.db.execute(
                select(InterventionRequest).where(
                    InterventionRequest.task_id == task_pk,
                    InterventionRequest.status == "PENDING",
                )
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        for row in rows:
            row.status = "CANCELLED"
            row.resolved_at = now
            row.verification_outcome = reason[:2000]
            conversation = await self.db.get(AgentConversation, row.conversation_id)
            if conversation is not None:
                conversation.requires_reply = False
        if rows:
            await self.db.flush()
        return len(rows)

    async def _has_other_pending(self, conversation_id: Any) -> bool:
        count = await self.db.scalar(
            select(func.count(InterventionRequest.id)).where(
                InterventionRequest.conversation_id == conversation_id,
                InterventionRequest.status == "PENDING",
            )
        )
        return bool(count)

    @staticmethod
    def request_view(request: InterventionRequest) -> Dict[str, Any]:
        return {
            "request_id": request.request_id,
            "task_id": str(request.task_id),
            "conversation_id": str(request.conversation_id),
            "reason_code": request.reason_code,
            "problem_summary": request.problem_summary,
            "detailed_explanation": request.detailed_explanation,
            "required_action": request.required_action,
            "instructions": list(request.instructions or []),
            "integration_ref": request.integration_ref,
            "blocking_step": request.blocking_step,
            "error_category": request.error_category,
            "checkpoint_name": request.checkpoint_name,
            "status": request.status,
            "created_at": request.created_at.isoformat() if request.created_at else None,
            "resolved_at": request.resolved_at.isoformat() if request.resolved_at else None,
            "resolved_by": request.resolved_by,
            "verification_method": request.verification_method,
            "verification_outcome": request.verification_outcome,
            "machine_verifiable": request.reason_code in MACHINE_VERIFIABLE,
            "requires_explicit_authorization": request.reason_code in REQUIRES_EXPLICIT_AUTHORIZATION,
        }


def build_agent_message(
    *,
    task: Task,
    reason_code: str,
    problem_summary: str,
    detailed_explanation: str,
    required_action: str,
    instructions: List[str],
    where_to_act: Optional[str],
    how_verified: str,
    what_happens_next: str,
    state_saved: bool,
) -> str:
    """Compose the message the agent posts when a task genuinely needs a person.

    Every section is supplied by the caller from real state. Nothing here invents progress,
    checkpoints or verification, and the "I have saved the current task state" line is only
    ever included when persistence actually succeeded.
    """
    lines: List[str] = [
        f"Task {task.task_id} - {task.title} needs your input.",
        "",
        f"What happened: {problem_summary}",
    ]
    if detailed_explanation:
        lines += ["", f"Why execution cannot continue: {detailed_explanation}"]

    if state_saved:
        lines += ["", "I have saved the current task state, so this task can continue from where it stopped."]

    lines += ["", f"What you need to do: {required_action}"]
    if where_to_act:
        lines.append(f"Where: {where_to_act}")
    if instructions:
        lines += ["", "Instructions:"] + [f"  {i + 1}. {step}" for i, step in enumerate(instructions)]

    lines += [
        "",
        f"How I will verify this: {how_verified}",
        f"What happens after you reply: {what_happens_next}",
    ]
    return "\n".join(lines)


_WHITESPACE = re.compile(r"\s+")


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    """Coerce a uuid or its string form. Anything unrecognised is dropped, never guessed."""
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def normalise_reply(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "").strip().lower())