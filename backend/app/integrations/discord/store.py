"""Persisting and querying Discord activity.

One table, three jobs:

* **Record** what an operation did, so the page can show a history and a task can
  point at the evidence.
* **Refuse a duplicate send.** Before anything is typed into the composer, a send is
  checked against a fingerprint of "this exact message, from this exact profile".
  A previous success means the action is already done and is reported rather than
  repeated.
* **Refuse to repeat an uncertain send.** This branch is not governed by any window
  and never expires: if a previous attempt's outcome is unknown, repeating it is how
  an operator ends up with the same message twice. The caller must verify first.

The fingerprint is a hash, so the stored value cannot be reversed into the message
the operator wrote, and two identical sends an hour apart still collide - which is
exactly the duplicate we are trying to stop.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.discord_integration import DiscordActivityRecord, public_activity
from backend.app.integrations.discord.models import FAILED, OK, UNCERTAIN, text_fingerprint

logger = logging.getLogger(__name__)


class StoreError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def send_key(
    profile_id: str,
    body: str,
    *,
    reply_to: str = "",
    channel_id: str = "",
    task_id: str = "",
) -> str:
    """Fingerprint for "this exact message, to this exact channel, by this task".

    Three parts, each of which fixes a defect found in review:

    * **channel_id** is OBSERVED from the page, not supplied by the caller. Keyed on a
      caller argument, the guard was wrong twice over: it failed to catch a genuine
      duplicate when one caller knew the channel id and another did not, and it
      blocked a legitimate message to a different channel when neither did. An
      unresolved channel is written as ``channel:?`` so it can never silently collide
      with a resolved one.
    * **task_id** scopes the guard to the work that is retrying. Without it, two
      unrelated tasks that both send a short message such as "ok" would block each
      other for the whole window, which is a false positive, not a safety win.
    * **text_fingerprint** keeps the body out of storage while still colliding two
      identical messages.

    Anything short of all three means the guard is guessing, and a guard that guesses
    both ways - blocks too little and too much - is worse than one that says so.
    """
    scope = f"channel:{channel_id}" if channel_id else "channel:?"
    task = f"task:{task_id}" if task_id else "task:-"
    return f"send:{profile_id}:{scope}:{task}:{text_fingerprint(body, reply_to=reply_to)}"[:160]


async def record_activity(
    db: AsyncSession,
    *,
    browser_profile_id: Optional[str],
    operation: str,
    intent: str,
    status: str,
    target: str = "",
    operation_key: str = "",
    outcome_uncertain: bool = False,
    result_summary: Optional[Dict[str, Any]] = None,
    evidence_path: str = "",
    error: str = "",
    error_category: str = "",
    duration_ms: Optional[int] = None,
    task_id: Optional[str] = None,
) -> DiscordActivityRecord:
    """Write one activity row.

    ``result_summary`` is expected to already be sanitised by the caller: bounded
    excerpt, channel ids, message id, permalink. This function does not inspect it
    further, because by the time it arrives it has already left the browser.
    """
    row = DiscordActivityRecord(
        browser_profile_id=str(browser_profile_id or "") or None,
        operation=str(operation)[:48],
        intent=str(intent or "read")[:16],
        target=str(target or "")[:512],
        status=str(status)[:32],
        operation_key=str(operation_key or "")[:160],
        outcome_uncertain=bool(outcome_uncertain),
        result_summary=dict(result_summary or {}),
        evidence_path=str(evidence_path or "")[:512] or None,
        error=str(error or "")[:600] or None,
        error_category=str(error_category or "")[:64] or None,
        duration_ms=duration_ms,
        task_id=str(task_id or "") or None,
    )
    db.add(row)
    await db.flush()
    return row


async def find_uncertain(db: AsyncSession, operation_key: str) -> Optional[DiscordActivityRecord]:
    """The most recent attempt at this fingerprint whose fate is unknown."""
    if not operation_key:
        return None
    result = await db.execute(
        select(DiscordActivityRecord)
        .where(
            DiscordActivityRecord.operation_key == operation_key,
            DiscordActivityRecord.outcome_uncertain.is_(True),
        )
        .order_by(desc(DiscordActivityRecord.created_at))
        .limit(1)
    )
    return result.scalars().first()


async def find_completed(
    db: AsyncSession, operation_key: str, *, within_seconds: int = 21600
) -> Optional[DiscordActivityRecord]:
    """The most recent KNOWN-SUCCESS at this fingerprint, inside the window."""
    if not operation_key:
        return None
    query = (
        select(DiscordActivityRecord)
        .where(
            DiscordActivityRecord.operation_key == operation_key,
            DiscordActivityRecord.status == OK,
        )
        .order_by(desc(DiscordActivityRecord.created_at))
        .limit(1)
    )
    if within_seconds > 0:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
        query = query.where(DiscordActivityRecord.created_at >= cutoff)
    result = await db.execute(query)
    return result.scalars().first()


async def resolve_uncertain(
    db: AsyncSession, operation_key: str, *, resolution: str, note: str = ""
) -> Optional[DiscordActivityRecord]:
    """Close an UNCERTAIN attempt with an outcome a human stated.

    An uncertain send is deliberately not resolvable on a timer and not resolvable by
    the model - "it has been six hours" is not evidence. Only an explicit human
    decision closes it, and the decision is written as a new activity row so the
    history records who concluded what.

    ``resolution`` is ``sent`` or ``not_sent``. ``not_sent`` is what unblocks a
    legitimate retry; ``sent`` converts the row into a known duplicate.
    """
    if not operation_key:
        return None
    result = await db.execute(
        select(DiscordActivityRecord)
        .where(
            DiscordActivityRecord.operation_key == operation_key,
            DiscordActivityRecord.outcome_uncertain.is_(True),
        )
        .order_by(desc(DiscordActivityRecord.created_at))
    )
    row = result.scalars().first()
    if row is None:
        return None

    sent = str(resolution) == "sent"
    detail = str(note or "")[:600]
    if sent:
        row.outcome_uncertain = False
        row.status = OK
        row.error = None
    else:
        # The attempt is abandoned. It stays on record as an abandoned attempt rather
        # than being deleted: "we tried and someone decided it never landed" is
        # exactly the evidence that stops the next operator repeating the mistake.
        row.outcome_uncertain = False
        row.status = "RESOLVED_NOT_SENT"
        row.error = (detail or "An operator confirmed this message was not sent.")[:600]
    row.result_summary = {**(row.result_summary or {}), "resolution": detail or resolution}
    await db.flush()
    return row


async def list_activity(
    db: AsyncSession,
    *,
    limit: int = 50,
    browser_profile_id: str = "",
    operation: str = "",
    status: str = "",
    task_id: str = "",
) -> List[DiscordActivityRecord]:
    query = select(DiscordActivityRecord).order_by(desc(DiscordActivityRecord.created_at)).limit(limit)
    if browser_profile_id:
        query = query.where(DiscordActivityRecord.browser_profile_id == browser_profile_id)
    if operation:
        query = query.where(DiscordActivityRecord.operation == operation)
    if status:
        query = query.where(DiscordActivityRecord.status == status)
    if task_id:
        query = query.where(DiscordActivityRecord.task_id == task_id)
    result = await db.execute(query)
    return list(result.scalars().all())


async def activity_for_task(db: AsyncSession, task_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
    rows = await list_activity(db, limit=limit, task_id=task_id)
    return [public_activity(row) for row in rows]


async def counts(db: AsyncSession, *, limit: int = 100) -> Dict[str, int]:
    """Rollup for the page's stat tiles. Reads only what was already loaded."""
    rows = await list_activity(db, limit=limit)
    return {
        "recent_total": len(rows),
        "recent_success": sum(1 for r in rows if r.status == OK),
        "recent_failed": sum(1 for r in rows if r.status == FAILED),
        "recent_uncertain": sum(1 for r in rows if r.outcome_uncertain),
        "recent_needs_user": sum(1 for r in rows if r.status == "NEEDS_USER_ACTION"),
    }