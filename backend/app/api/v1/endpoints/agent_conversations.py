"""Console chat conversations: create, list, read, delete.

Each conversation has its own UUID and its own stored messages, owned by the authenticated
operator. A conversation that is not the caller's is reported as not found. Deleting one never
deletes a task: console chats carry no task id.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.chat_store import AgentChatStore
from backend.app.database.session import get_db
from backend.app.security.auth import Principal, enforce_rate_limit, require_agent_chat

router = APIRouter(prefix="/agent/conversations", tags=["agent"])


async def _owned(db: AsyncSession, conversation_id: str, principal: Principal):
    conversation = await AgentChatStore(db).get(conversation_id, principal.email)
    if conversation is None:
        # Same answer for "does not exist" and "belongs to someone else".
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation


@router.post("", status_code=201)
async def create_conversation(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_agent_chat),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    conversation = await AgentChatStore(db).create(principal.email)
    await db.commit()
    return AgentChatStore.view(conversation)


@router.get("")
async def list_conversations(
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_agent_chat),
) -> Dict[str, Any]:
    items = await AgentChatStore(db).list_for_owner(principal.email, limit)
    return {"count": len(items), "conversations": items}


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    limit: int = Query(200, ge=1, le=1000),
    before_seq: Optional[int] = Query(None, ge=1),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_agent_chat),
) -> Dict[str, Any]:
    """One conversation and its messages; pass `before_seq` to load older ones."""
    conversation = await _owned(db, conversation_id, principal)
    store = AgentChatStore(db)
    rows = await store.messages(conversation, limit=limit, before_seq=before_seq)
    return {
        **AgentChatStore.view(conversation),
        "messages": [AgentChatStore.message_view(m) for m in rows],
        "has_more": bool(rows) and rows[0].seq > 1,
    }


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_agent_chat),
) -> Dict[str, Any]:
    conversation = await _owned(db, conversation_id, principal)
    removed = await AgentChatStore(db).delete(conversation)
    await db.commit()
    return {"id": conversation_id, "deleted": True, "messages_removed": removed, "tasks_affected": 0}
