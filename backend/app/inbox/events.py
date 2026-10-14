"""WebSocket notifications for the Agent Inbox.

The database is the source of truth; these events are invalidation hints only, so a
reconnecting client refetches the conversations it cares about and never loses a message.

AirdropOS runs the API and the worker as separate processes. The API owns the authenticated
WebSocket bus; the worker owns the live-session gateway that the API already polls. A single
async `publish` works in both: in the API process it reaches the dashboard sockets directly,
and in the worker process the same call leaves that bus empty and instead drops the event into
the live log the existing relay forwards. Callers never need to know which process they are in.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# WebSocket event type published by the API process.
INBOX_EVENT = "agent.inbox"

# Inner event type published into the worker's live event log. The existing relay forwards
# these to the dashboard as `live.event` with `integration == "agent"`, so the frontend can
# treat both shapes identically.
INBOX_LIVE_TYPE = "agent.inbox"
INBOX_INTEGRATION = "agent"

# Event names carried in the payload. Kept as constants so the frontend and backend agree.
EV_INTERVENTION_CREATED = "intervention.created"
EV_AGENT_MESSAGE = "message.agent"
EV_USER_MESSAGE = "message.user"
EV_VERIFICATION_STARTED = "verification.started"
EV_VERIFICATION_COMPLETED = "verification.completed"
EV_RESUME_SCHEDULED = "task.resume_scheduled"
EV_TASK_RESUMED = "task.resumed"
EV_TASK_PROGRESS = "task.progress"
EV_TASK_COMPLETED = "task.completed"
EV_TASK_FAILED = "task.failed"
EV_TASK_CANCELLED = "task.cancelled"
# Emitted when a task's execution context materially changes: a requirement was
# discovered, an action was proven, or a blocker was raised. The client refetches
# /tasks/{id}/context on this hint, so the dashboard shows what the agent actually
# learned rather than only that it is busy.
EV_TASK_CONTEXT_UPDATED = "task.context_updated"

EVENT_TYPES = frozenset(
    {
        EV_INTERVENTION_CREATED,
        EV_AGENT_MESSAGE,
        EV_USER_MESSAGE,
        EV_VERIFICATION_STARTED,
        EV_VERIFICATION_COMPLETED,
        EV_RESUME_SCHEDULED,
        EV_TASK_RESUMED,
        EV_TASK_PROGRESS,
        EV_TASK_COMPLETED,
        EV_TASK_FAILED,
        EV_TASK_CANCELLED,
        EV_TASK_CONTEXT_UPDATED,
    }
)


async def publish(
    event_type: str,
    *,
    task_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    request_id: Optional[str] = None,
    message_id: Optional[str] = None,
    summary: Optional[str] = None,
    **extra: Any,
) -> None:
    """Publish an Agent Inbox notification. Never raises.

    Everything a user needs is already committed to the database before this is called, so a
    dropped hint only costs a client its live refresh -- never a message.
    """
    payload: Dict[str, Any] = {"event": event_type}
    if task_id is not None:
        payload["task_id"] = task_id
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    if request_id is not None:
        payload["request_id"] = request_id
    if message_id is not None:
        payload["message_id"] = message_id
    if summary is not None:
        payload["summary"] = summary[:200]
    for key, value in extra.items():
        if value is not None:
            payload[key] = value

    await _publish_to_api_bus(payload)
    _publish_to_worker_log(event_type, payload)


async def _publish_to_api_bus(payload: Dict[str, Any]) -> None:
    """Deliver to sockets held by this process.

    In the worker process this bus simply has no clients, so it is a no-op there.
    """
    try:
        from backend.app.api.v1.endpoints.ws import bus

        await bus.broadcast(INBOX_EVENT, payload)
    except Exception as exc:  # pragma: no cover - notification must never break the caller
        logger.debug("Agent Inbox bus publish failed: %s", exc)


def _publish_to_worker_log(event_type: str, payload: Dict[str, Any]) -> None:
    """Deliver through the live event log the API already relays to dashboard sockets."""
    try:
        from backend.app.live import hub

        hub.events.emit(
            integration=INBOX_INTEGRATION,
            type=f"{INBOX_LIVE_TYPE}.{event_type}",
            message=str(payload.get("summary") or event_type),
            session_id=payload.get("task_id"),
            status="ok",
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("Agent Inbox live-log publish failed: %s", exc)