"""WebSocket Event Bus for real-time dashboard updates.

Live progress never bypasses authentication or authorization (Part 32).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.app.core.config import settings
from backend.app.database.session import AsyncSessionLocal
from backend.app.security.auth import (
    AuthError,
    Principal,
    Scope,
    hash_token,
    resolve_session,
)
from backend.app.security.emergency_stop import emergency_stop

logger = logging.getLogger(__name__)

MAX_WS_CLIENTS = 200
# How often an open socket's session is re-read from the database. This bounds how long a session
# revoked by anything other than this process's own logout (account deactivation, a direct database
# revoke, a second API process) can keep streaming. Session EXPIRY does not wait for this: the
# watcher ends the socket at the exact expiry time. Each check is one indexed read (no write).
WS_REVALIDATE_SECONDS = 10
# Consecutive failed checks (database down, etc.) after which the socket is closed rather than left
# streaming on a session nobody can verify. One or two blips do not drop legitimate connections.
WS_MAX_CHECK_FAILURES = 3
# Browsers cannot set an Authorization header on a WebSocket, so the token rides in
# the `Sec-WebSocket-Protocol` handshake: `new WebSocket(url, ["bearer", token])`.
# Unlike a `?token=` query string it is not written to URLs, access logs or history.
WS_AUTH_SUBPROTOCOL = "bearer"

router = APIRouter()


class EventBus:
    """Fan-out of agent events to connected dashboard clients."""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        # Sockets allowed to receive live-session activity (browser/Telegram session events).
        self._live_ok: Set[WebSocket] = set()
        # Which session (token hash) each authenticated socket belongs to, so a
        # logout can end that session's streams immediately.
        self._sessions: Dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()
        self._recent: List[Dict[str, Any]] = []

    async def connect(
        self,
        websocket: WebSocket,
        live_ok: bool = True,
        subprotocol: Optional[str] = None,
        session_hash: Optional[str] = None,
    ) -> None:
        await websocket.accept(subprotocol=subprotocol)
        async with self._lock:
            self._clients.add(websocket)
            if live_ok:
                self._live_ok.add(websocket)
            if session_hash:
                self._sessions[websocket] = session_hash

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)
            self._live_ok.discard(websocket)
            self._sessions.pop(websocket, None)

    async def close_session(self, token: str) -> int:
        """Close every socket opened with this session token. Used on logout."""
        digest = hash_token(token)
        async with self._lock:
            targets = [ws for ws, h in self._sessions.items() if h == digest]
        closed = 0
        for client in targets:
            try:
                await client.close(code=4401, reason="Session ended")
                closed += 1
            except Exception:  # already gone
                pass
            await self.disconnect(client)
        return closed

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
            # What the operator's sessions are doing is not for anonymous sockets.
            targets = list(self._live_ok if event_type == "live.event" else self._clients)

        for client in targets:
            try:
                await client.send_json(event)
            except Exception:
                dead.append(client)

        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.discard(client)
                    self._live_ok.discard(client)
                    self._sessions.pop(client, None)

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._recent[-limit:]

    @property
    def client_count(self) -> int:
        return len(self._clients)


bus = EventBus()


def _extract_ws_token(websocket: WebSocket) -> Optional[str]:
    """Pull the session token from the handshake. Never from the query string.

    Accepted: `Authorization: Bearer <token>` (non-browser clients) or the
    `Sec-WebSocket-Protocol: bearer, <token>` subprotocol pair (browsers).
    """
    header = websocket.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()

    protocols = [p.strip() for p in websocket.scope.get("subprotocols", []) if p.strip()]
    if len(protocols) >= 2 and protocols[0] == WS_AUTH_SUBPROTOCOL:
        return protocols[1]
    return None


async def _authenticate_websocket(token: Optional[str], touch: bool = True) -> Optional[Principal]:
    """Resolve a WebSocket caller through the same code path REST uses.

    Returns None when the token is absent, unknown, expired, revoked, belongs to
    an inactive account, or lacks the `read` scope that every live-data REST
    route also requires. Anything else (e.g. the database being unreachable) is
    raised, not mistaken for "valid".

    ``touch=False`` is for periodic revalidation: all checks, no ``last_used_at`` write.
    """
    if not token:
        return None

    async with AsyncSessionLocal() as db:
        try:
            principal = await resolve_session(db, token, touch=touch)
        except AuthError:
            return None
        if touch:
            await db.commit()  # persists last_used_at

    if not principal.has(Scope.READ):
        return None
    return replace(principal, channel="websocket")


async def _close_quietly(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # already gone
        pass


async def _watch_session(
    websocket: WebSocket, token: str, expires_at: Optional[datetime] = None
) -> None:
    """Close the socket as soon as its session stops being valid.

    Revalidation must not depend on the client sending a heartbeat: a client that
    never pings would otherwise keep streaming after logout or expiry.

    * Expiry is enforced at the exact expiry time, without a database round trip.
    * Revocation is picked up on the next revalidation (``WS_REVALIDATE_SECONDS``).
    * A check that cannot be completed is not treated as success: after
      ``WS_MAX_CHECK_FAILURES`` consecutive failures the socket is closed (fail closed).
      The watcher never dies silently, which would leave the socket unchecked forever.
    """
    failures = 0
    while True:
        delay = WS_REVALIDATE_SECONDS
        if expires_at is not None:
            remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                await _close_quietly(websocket, 4401, "Session ended")
                return
            delay = min(delay, remaining)
        await asyncio.sleep(delay)

        if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
            await _close_quietly(websocket, 4401, "Session ended")
            return
        try:
            principal = await _authenticate_websocket(token, touch=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            logger.warning(
                "WebSocket session check failed (%s), attempt %s/%s",
                type(exc).__name__, failures, WS_MAX_CHECK_FAILURES,
            )
            if failures >= WS_MAX_CHECK_FAILURES:
                await _close_quietly(websocket, 1011, "Session could not be verified")
                return
            continue
        failures = 0
        if principal is None:
            await _close_quietly(websocket, 4401, "Session ended")
            return


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Authenticated live event stream.

    Authorization is identical to REST: the token is resolved by the same
    `resolve_session`, and the `read` scope is required. Presenting an arbitrary
    string, or a token in the URL, is not enough.
    """
    principal: Optional[Principal] = None
    token: Optional[str] = None

    if settings.AUTH_ENABLED:
        token = _extract_ws_token(websocket)
        if not token:
            await websocket.close(code=4401, reason="Authentication required")
            return
        principal = await _authenticate_websocket(token)
        if principal is None:
            await websocket.close(code=4401, reason="Invalid, expired or unauthorized session")
            return
    elif settings.ENVIRONMENT == "production":
        # REST answers 503 here; the stream must not be more permissive than REST.
        await websocket.close(code=1011, reason="Authentication is misconfigured")
        return

    if bus.client_count >= MAX_WS_CLIENTS:
        await websocket.close(code=1013, reason="Too many connections")
        return

    # Only the marker subprotocol is echoed back, never the token itself.
    offered = websocket.scope.get("subprotocols", [])
    await bus.connect(
        websocket,
        live_ok=principal is not None or settings.ENVIRONMENT != "production",
        subprotocol=WS_AUTH_SUBPROTOCOL if WS_AUTH_SUBPROTOCOL in offered else None,
        session_hash=hash_token(token) if token else None,
    )
    watcher: Optional[asyncio.Task] = None
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
        if principal is not None and token:
            watcher = asyncio.create_task(
                _watch_session(websocket, token, principal.session_expires_at)
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
        if watcher is not None:
            watcher.cancel()
        await bus.disconnect(websocket)
