"""Discord (browser-only) integration models.

One table:

* ``discord_activity_records`` - the integration's own history: which browser
  profile acted, which operation, on what target, when, what came back, and whether
  the outcome is known.

Why a table at all, when tasks and evidence already exist
--------------------------------------------------------
Task evidence answers "what happened inside THIS task", which is what the task
monitor needs. It cannot answer the two questions this integration must answer:

* *"Has this exact message already been sent from this profile?"* Duplicate
  suppression needs a lookup keyed by a fingerprint, across tasks and across
  restarts.
* *"Did this send land?"* Resolving an UNCERTAIN outcome needs the same lookup.

Both need an indexed, queryable key, so the record is stored here. It carries no
foreign key into ``tasks`` on purpose: an activity row is evidence of what was
actually sent from an account and must outlive the task that produced it.

There is deliberately NO configuration table. Discord has no credentials in this
integration - no bot token, no API key, no secret of any kind. An account is just a
persistent Chrome profile the operator signed into by hand, so there is nothing to
configure beyond what the existing browser profile system already stores.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.database.session import Base


class DiscordActivityRecord(Base):
    """One Discord browser operation attempt, from any task."""

    __tablename__ = "discord_activity_records"
    __table_args__ = (
        Index("ix_discord_activity_records_operation_key_created", "operation_key", "created_at"),
        Index("ix_discord_activity_records_created_at", "created_at"),
    )

    # The account is a browser profile, never a stored credential.
    browser_profile_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    operation: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    intent: Mapped[str] = mapped_column(String(16), default="read", nullable=False)

    # A short, non-secret description of what was acted on: a channel label, a
    # message id, or "text:<sha256 prefix>". Never the message body itself.
    target: Mapped[str] = mapped_column(String(512), default="", nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Stable fingerprint used to refuse a duplicate send. See `models.operation_key`.
    operation_key: Mapped[str] = mapped_column(String(160), default="", nullable=False)

    # True when the write may or may not have landed. A timeout, a lost response or
    # an unconfirmed send sets this; it is what stops an automatic retry.
    outcome_uncertain: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Where the outcome is known: channel ids, a message id or permalink, and a
    # bounded excerpt. Never cookies, tokens or a full transcript.
    result_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    evidence_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String(600), nullable=True)
    error_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    task_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    @property
    def is_write(self) -> bool:
        return self.intent == "write"


def public_activity(row: DiscordActivityRecord) -> Dict[str, Any]:
    """What the Discord page is allowed to see about an activity record.

    Nothing here is a secret: no cookie, no token, no password, and the excerpt is
    bounded by ``models.MAX_STORED_EXCERPT``.
    """
    summary = row.result_summary or {}
    return {
        "id": str(row.id),
        "browser_profile_id": row.browser_profile_id or "",
        "operation": row.operation,
        "intent": row.intent,
        "target": row.target,
        "status": row.status,
        "operation_key": row.operation_key,
        "outcome_uncertain": bool(row.outcome_uncertain),
        "channel": summary.get("channel", {}),
        "message_id": summary.get("message_id", ""),
        "permalink": summary.get("permalink", ""),
        "excerpt": summary.get("excerpt", ""),
        "evidence_path": row.evidence_path or "",
        "error": row.error or "",
        "error_category": row.error_category or "",
        "duration_ms": row.duration_ms,
        "task_id": row.task_id or "",
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }