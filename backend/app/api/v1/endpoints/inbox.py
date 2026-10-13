"""Agent Inbox API: task-scoped conversations and human-intervention requests.

Read endpoints need `read`. Sending a reply needs `agent_chat` because it is a conversation
action, plus `task_write` because a sufficiently clear reply can requeue a task -- an
informational message never does, which is enforced in the reply router rather than here.

Every handler resolves the conversation server-side and checks ownership against the stored
record. A task id from the request body is never trusted on its own.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database.session import get_db
from backend.app.inbox.replies import ReplyRouter
from backend.app.inbox.service import (
    ConversationService,
    InterventionService,
)
from backend.app.models import AgentConversation, Task
from backend.app.security.auth import Principal, Scope, enforce_rate_limit, require_scopes, write_audit
from backend.app.tasks.state_machine import TaskStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inbox", tags=["agent-inbox"])

MAX_PAGE = 200


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    # Client-generated idempotency token. A repeated submit with the same token is stored
    # once and cannot schedule a second resume.
    client_token: Optional[str] = Field(default=None, max_length=128)


async def _load_owned_conversation(
    db: AsyncSession, conversation_id: Any, principal: Principal
) -> AgentConversation:
    conversation = await ConversationService(db).get_by_id(conversation_id, principal.email)
    if conversation is None:
        # 404 rather than 403: a conversation that is not the caller's must be
        # indistinguishable from one that does not exist.
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation


async def _task_for_conversation(
    db: AsyncSession, conversation: AgentConversation
) -> Task:
    if conversation.task_id is None:
        raise HTTPException(status_code=409, detail="This conversation is not linked to a task.")
    task = await db.get(Task, conversation.task_id)
    if task is None:
        # The task was deleted. The conversation is kept, but there is nothing to resume.
        raise HTTPException(status_code=409, detail="The task for this conversation no longer exists.")
    return task


@router.get("/conversations")
async def list_conversations(
    request: Request,
    search: Optional[str] = Query(default=None, max_length=200),
    needs_reply: Optional[bool] = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=MAX_PAGE),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_scopes(Scope.READ)),
) -> Dict[str, Any]:
    """Task-associated conversations for the signed-in operator.

    Conversations needing a reply come first, then the most recently updated. Only tasks
    that actually have a conversation appear: the Inbox is not a log of every execution
    event.
    """
    enforce_rate_limit(request, "read")
    service = ConversationService(db)

    stmt = select(AgentConversation).where(
        AgentConversation.owner_email == principal.email,
        AgentConversation.task_id.is_not(None),
    )
    if not include_archived:
        stmt = stmt.where(AgentConversation.status != "ARCHIVED")
    if needs_reply is not None:
        stmt = stmt.where(AgentConversation.requires_reply.is_(needs_reply))

    rows = (
        await db.execute(stmt.order_by(AgentConversation.last_message_at.desc().nullslast()))
    ).scalars().all()

    tasks = await _tasks_by_pk(db, [c.task_id for c in rows if c.task_id is not None])

    items: List[Dict[str, Any]] = []
    for conversation in rows:
        task = tasks.get(conversation.task_id)
        view = service.conversation_view(conversation, task)
        view["last_message_preview"] = await _latest_preview(db, conversation)
        items.append(view)

    if search:
        needle = search.strip().lower()
        items = [
            i for i in items
            if needle in (i.get("title") or "").lower() or needle in (i.get("task_id") or "").lower()
        ]

    # Requiring a reply is the whole point of the Inbox, so those sort above everything else.
    items.sort(key=lambda i: (not i["requires_reply"], i.get("last_message_at") or ""))

    pending_total = sum(1 for i in items if i["requires_reply"])
    return {
        "conversations": items[:limit],
        "count": len(items[:limit]),
        "total": len(items),
        "needs_reply": pending_total,
    }


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=MAX_PAGE),
    before_seq: Optional[int] = Query(default=None, ge=0),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_scopes(Scope.READ)),
) -> Dict[str, Any]:
    """A conversation with its persisted message history and open request, if any."""
    enforce_rate_limit(request, "read")
    conversation = await _load_owned_conversation(db, _as_uuid(conversation_id), principal)
    service = ConversationService(db)

    messages = await service.list_messages(conversation.id, limit=limit, before_seq=before_seq)
    request_row = await InterventionService(db).active_for_conversation(conversation.id)

    task: Optional[Task] = None
    task_summary: Optional[Dict[str, Any]] = None
    if conversation.task_id is not None:
        task = await db.get(Task, conversation.task_id)
        if task is not None:
            task_summary = {
                "task_id": task.task_id,
                "title": task.title,
                "status": task.status,
                "progress": task.progress,
                "retry_count": task.retry_count,
                "max_retries": task.max_retries,
                "next_planned_action": task.next_planned_action,
                "next_execution_at": task.next_execution_at.isoformat() if task.next_execution_at else None,
                "last_updated": task.updated_at.isoformat() if task.updated_at else None,
                "error_details": task.error_details or {},
                "detail_url": f"/tasks/{task.task_id}",
            }

    payload = service.conversation_view(conversation, task)
    payload.update(
        {
            "messages": [service.message_view(m) for m in messages],
            "message_count": len(messages),
            "has_more": len(messages) >= limit,
            "open_request": InterventionService.request_view(request_row) if request_row else None,
            "task": task_summary,
        }
    )
    return payload


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    request: Request,
    payload: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(
        require_scopes(Scope.AGENT_CHAT, Scope.TASK_WRITE)
    ),
) -> Dict[str, Any]:
    """Send a reply into one task's conversation.

    The task is resolved from the conversation, the conversation from the caller's session,
    and the intervention request from the conversation. Nothing is trusted from the body
    except the text and its idempotency token.
    """
    enforce_rate_limit(request, "chat")

    conversation = await _load_owned_conversation(db, _as_uuid(conversation_id), principal)
    task = await _task_for_conversation(db, conversation)

    router_ = ReplyRouter(db)
    outcome = await router_.handle(
        conversation=conversation,
        task=task,
        owner_email=principal.email,
        text=payload.message,
        client_token=payload.client_token,
    )
    await db.commit()

    await write_audit(
        db,
        principal,
        "AGENT_INBOX_REPLY",
        "task",
        task.task_id,
        after_state={"outcome": outcome.status, "resumed": outcome.resumed},
    )
    await db.commit()
    return outcome.to_dict()


@router.get("/summary")
async def inbox_summary(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_scopes(Scope.READ)),
) -> Dict[str, Any]:
    """Counts used by the navigation badge and the mobile alert indicator."""
    enforce_rate_limit(request, "read")

    rows = (
        await db.execute(
            select(AgentConversation.task_id, AgentConversation.requires_reply).where(
                AgentConversation.owner_email == principal.email,
                AgentConversation.task_id.is_not(None),
                AgentConversation.status != "ARCHIVED",
            )
        )
    ).all()

    tasks = await _tasks_by_pk(db, [pk for pk, _ in rows if pk is not None])
    needs_reply = sum(1 for _, flag in rows if flag)
    running = sum(
        1 for pk, _ in rows
        if tasks.get(pk) is not None and tasks[pk].status == TaskStatus.IN_PROGRESS.value
    )
    return {
        "conversations": len(rows),
        "needs_reply": needs_reply,
        "active": running,
    }


# ----------------------------------------------------------------------
def _as_uuid(value: str) -> Any:
    import uuid

    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid conversation id.")


async def _tasks_by_pk(db: AsyncSession, pks: List[Any]) -> Dict[Any, Task]:
    pks = [pk for pk in pks if pk is not None]
    if not pks:
        return {}
    rows = (await db.execute(select(Task).where(Task.id.in_(pks)))).scalars().all()
    return {t.id: t for t in rows}


async def _latest_preview(db: AsyncSession, conversation: AgentConversation) -> Optional[str]:
    """One line of the most recent message. Read from the stored message, never invented."""
    from backend.app.models import ConversationMessage

    latest = await db.scalar(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation.id)
        .order_by(ConversationMessage.seq.desc())
        .limit(1)
    )
    if latest is None:
        return None
    text = " ".join((latest.content or "").split())
    return text[:160] if text else None