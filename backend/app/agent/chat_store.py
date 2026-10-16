"""Durable, per-conversation storage for the AI Agent Console.

Every console chat is one `AgentConversation` row on its own channel (`AGENT_CHAT`) with its own
UUID, owned by the authenticated operator. Its messages live in `ConversationMessage` rows keyed
by that UUID, and the history sent to the model is read from exactly those rows, so one chat can
never see another's messages, summaries or context.

Deliberately separate from tasks: the rows have `task_id = NULL`, so deleting a chat never
touches a task (or its inbox conversation), and the Agent Inbox - which only lists rows that
belong to a task - never shows a console chat.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import AgentConversation, ConversationMessage

CHANNEL_AGENT_CHAT = "AGENT_CHAT"
MAX_MESSAGE_CHARS = 200_000
TITLE_CHARS = 48
ROLES = {"user", "assistant"}


def parse_conversation_id(value: Any) -> Optional[uuid.UUID]:
    """A conversation id is always a UUID; anything else is treated as 'not found'."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _title(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= TITLE_CHARS else f"{flat[: TITLE_CHARS - 1]}…"


class AgentChatStore:
    """All reads and writes of console conversations. Every read is scoped to the owner."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- conversations -------------------------------------------------

    async def create(self, owner_email: str, conversation_id: Optional[uuid.UUID] = None) -> AgentConversation:
        new_id = conversation_id or uuid.uuid4()
        conversation = AgentConversation(
            id=new_id,
            channel=CHANNEL_AGENT_CHAT,
            channel_conversation_id=str(new_id),
            owner_email=owner_email,
            status="OPEN",
            requires_reply=False,
            last_seq=0,
            task_id=None,
        )
        self.db.add(conversation)
        await self.db.flush()
        return conversation

    async def get(self, conversation_id: Any, owner_email: str) -> Optional[AgentConversation]:
        """The caller's own console chat, else None - a stranger's chat looks like a missing one."""
        parsed = parse_conversation_id(conversation_id)
        if parsed is None:
            return None
        return await self.db.scalar(
            select(AgentConversation).where(
                AgentConversation.id == parsed,
                AgentConversation.owner_email == owner_email,
                AgentConversation.channel == CHANNEL_AGENT_CHAT,
            )
        )

    async def exists_for_anyone(self, conversation_id: uuid.UUID) -> bool:
        return (
            await self.db.scalar(
                select(AgentConversation.id).where(AgentConversation.id == conversation_id)
            )
        ) is not None

    async def list_for_owner(self, owner_email: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = (
            await self.db.execute(
                select(AgentConversation)
                .where(
                    AgentConversation.owner_email == owner_email,
                    AgentConversation.channel == CHANNEL_AGENT_CHAT,
                    AgentConversation.status != "ARCHIVED",
                    AgentConversation.last_seq > 0,
                )
                .order_by(AgentConversation.last_message_at.desc().nullslast())
                .limit(max(1, min(limit, 200)))
            )
        ).scalars().all()
        return [self.view(c) for c in rows]

    @staticmethod
    def view(conversation: AgentConversation) -> Dict[str, Any]:
        return {
            "id": str(conversation.id),
            "title": conversation.title or "New chat",
            "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
            "updated_at": conversation.last_message_at.isoformat()
            if conversation.last_message_at
            else None,
            "message_count": conversation.last_seq,
        }

    async def delete(self, conversation: AgentConversation) -> int:
        """Remove the chat and its messages. Tasks are never touched (task_id is NULL here)."""
        removed = await self.db.scalar(
            select(func.count()).select_from(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation.id
            )
        )
        await self.db.execute(
            delete(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id)
        )
        await self.db.execute(delete(AgentConversation).where(AgentConversation.id == conversation.id))
        await self.db.flush()
        return int(removed or 0)

    # --- messages ------------------------------------------------------

    async def append(
        self,
        conversation: AgentConversation,
        role: str,
        content: str,
        *,
        tool_calls: Optional[Dict[str, Any]] = None,
    ) -> ConversationMessage:
        if role not in ROLES:
            raise ValueError(f"role must be one of {sorted(ROLES)}")
        now = datetime.now(timezone.utc)
        conversation.last_seq = (conversation.last_seq or 0) + 1
        conversation.last_message_at = now
        if role == "user" and not conversation.title:
            conversation.title = _title(content)
        message = ConversationMessage(
            conversation_id=conversation.id,
            role=role,
            content=content[:MAX_MESSAGE_CHARS],
            tool_calls=tool_calls or {},
            task_id=None,
            kind="USER" if role == "user" else "AGENT",
            seq=conversation.last_seq,
        )
        self.db.add(message)
        await self.db.flush()
        return message

    async def messages(
        self,
        conversation: AgentConversation,
        *,
        limit: int = 200,
        before_seq: Optional[int] = None,
    ) -> List[ConversationMessage]:
        """The newest `limit` messages of THIS conversation, oldest first."""
        stmt = select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id)
        if before_seq is not None:
            stmt = stmt.where(ConversationMessage.seq < before_seq)
        rows = (
            await self.db.execute(stmt.order_by(ConversationMessage.seq.desc()).limit(max(1, min(limit, 1000))))
        ).scalars().all()
        return list(reversed(rows))

    async def truncate(self, conversation: AgentConversation, keep: int) -> None:
        """Keep the first `keep` messages and drop the rest (regenerate / edit-and-resend)."""
        keep = max(0, min(int(keep), conversation.last_seq or 0))
        await self.db.execute(
            delete(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation.id,
                ConversationMessage.seq > keep,
            )
        )
        conversation.last_seq = keep

    async def history_for_llm(self, conversation: AgentConversation) -> List[Dict[str, str]]:
        """What the model is shown: only this conversation's own turns, in order."""
        rows = await self.messages(conversation, limit=1000)
        return [{"role": m.role, "content": m.content} for m in rows if m.role in ROLES and m.content.strip()]

    @staticmethod
    def message_view(message: ConversationMessage) -> Dict[str, Any]:
        return {
            "id": str(message.id),
            "seq": message.seq,
            "role": message.role,
            "content": message.content,
            "at": message.created_at.isoformat() if message.created_at else None,
        }
