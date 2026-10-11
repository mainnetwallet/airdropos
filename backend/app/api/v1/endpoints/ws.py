"""WebSocket Event Bus for real-time dashboard updates.

Live progress never bypasses authentication or authorization (Part 32).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.database.session import AsyncSessionLocal, get_db
from backend.app.models import User, UserSession
from backend.app.security.auth import (
    ALL_SCOPES,
    Principal,
    Scope,
    _scopes_for,
    hash_token,
)
from backend.app.security.emergency_stop import emergency_stop

logger = logging.getLogger(__name__)

router = APIRouter()


class EventBus:
    """Fan-out of agent events to connected dashboard clients."""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._recent: List[Dict[str, Any]] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Deliver an event to all clients. Dead sockets are pruned."""
        event = {
            "type": event_type,
            "payload": payload,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self._recent.append(event)
        if len(self._recent) > 200:
            self._recent = self._recent[-200:]

        dead: List[WebSocket] = []
        async with self._lock:
            targets = list(self._clients)

        for client in targets:
            try:
                await client.send_json(event)
            except Exception:
                dead.append(client)

        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.discard(client)

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._recent[-limit:]

    @property
    def client_count(self) -> int:
        return len(self._clients)


bus = EventBus()


async def _authenticate_websocket(token: str) -> Optional[Principal]:
    """Resolve a WebSocket caller from a bearer token.

    The token is verified against the same hashed session store used by REST, so a
    revoked or expired session cannot keep streaming events. Returns None when the
    token is absent, unknown, expired, revoked, or belongs to an inactive account.
    """
    if not token:
        return None

    async with AsyncSessionLocal() as db:
        session = await db.scalar(
            select(UserSession).where(UserSession.token == hash_token(token))
        )
        if session is None or session.revoked_at is not None:
            return None

        now = datetime.now(timezone.utc)
        expires = session.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            return None

        user = await db.get(User, session.user_id)
        if user is None or not user.is_active:
            return None

        session_scopes = session.scopes or []
        scopes = (
            frozenset(s for s in session_scopes if s in ALL_SCOPES)
            if session_scopes
            else frozenset(_scopes_for(user))
        )
        session.last_used_at = now
        await db.commit()

        return Principal(
            user_id=str(user.id),
            email=user.email,
            scopes=scopes,
            session_token=token,
            channel="websocket",
            is_operator=bool(user.is_superuser),
        )


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Authenticated live event stream.

    The token is bound to a real, unexpired, unrevoked session whenever
    authentication is enabled. Presenting an arbitrary string is not enough.
    """
    principal: Optional[Principal] = None

    if settings.AUTH_ENABLED:
        # Resolve the caller from a bearer token using the provided DB session
        if not token:
            await websocket.close(code=4401, reason="Authentication required")
            return

        session = await db.scalar(
            select(UserSession).where(UserSession.token == hash_token(token))
        )
        if session is None or session.revoked_at is not None:
            await websocket.close(code=4401, reason="Invalid or revoked session")
            return

        now = datetime.now(timezone.utc)
        expires = session.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            await websocket.close(code=4401, reason="Session expired. Log in again.")
            return

        user = await db.get(User, session.user_id)
        if user is None or not user.is_active:
            await websocket.close(code=4401, reason="Account is inactive.")
            return

        # A session may hold fewer scopes than the user's role permits. An explicit
        # empty scope list grants nothing, so scope narrowing works as intended.
        session_scopes = session.scopes or []
        if session_scopes:
            scopes = frozenset(s for s in session_scopes if s in ALL_SCOPES)
        else:
            scopes = frozenset(_scopes_for(user))

        session.last_used_at = datetime.now(timezone.utc)
        await db.commit()

        principal = Principal(
            user_id=str(user.id),
            email=user.email,
            scopes=scopes,
            session_token=token,
            channel="websocket",
            is_operator=bool(user.is_superuser),
        )

        # Live telemetry is operator-level information; require the read scope.
        if Scope.READ.value not in principal.scopes:
            await websocket.close(code=4403, reason="Insufficient permissions")
            return
    elif not token and settings.ENVIRONMENT == "production":
        # Auth disabled: production still refuses a completely anonymous stream.
        await websocket.close(code=4401, reason="Authentication required")
        return

    await bus.connect(websocket)
    try:
        await websocket.send_json(
            {
                "type": "connected",
                "payload": {
                    "environment": settings.ENVIRONMENT,
                    "emergency_stop": emergency_stop.is_stopped,
                    "authenticated": principal is not None,
                    "scopes": sorted(principal.scopes) if principal else [],
                },
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        while True:
            # Client heartbeats keep the socket alive and detect disconnects.
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_json({"type": "pong", "payload": {}})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket closed: %s", exc)
    finally:
        await bus.disconnect(websocket)
