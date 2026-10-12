"""Live Broadcast API.

Every route is authenticated and scope-checked here; the worker gateway behind
it trusts only this API. Reading needs ``read``. Anything that changes a session
or sends input needs ``task_write`` (the same scope as cancelling a task), so a
read-only observer session can watch but never click.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database.session import get_db
from backend.app.live import client
from backend.app.security.auth import (
    Principal,
    Scope,
    enforce_rate_limit,
    require_read,
    require_task_write,
    write_audit,
)
from backend.app.security.emergency_stop import emergency_stop
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/live", tags=["live"])

_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_INTEGRATION_RE = re.compile(r"^[a-z0-9_-]{1,24}$")
STATUSES = ("running", "waiting_for_user", "paused", "failed", "stopped")


def _check_ref(integration: str, key: str) -> None:
    if not _INTEGRATION_RE.match(integration) or not _KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="Invalid session reference.")


def _owner(principal: Principal) -> Dict[str, str]:
    return {"owner_id": str(principal.user_id or principal.email), "operator": principal.email}


async def _gateway(coro):
    """Translate gateway failures into clean HTTP errors."""
    try:
        return await coro
    except client.GatewayUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "The worker is not reachable, so there are no live sessions.", "hint": str(exc)},
        )
    except client.GatewayError as exc:
        raise HTTPException(status_code=exc.status, detail={"error": exc.message, "code": exc.code})


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
@router.get("/overview")
async def overview(principal: Principal = Depends(require_read)) -> Dict[str, Any]:
    """Everything the Live Broadcast page needs in one call."""
    from backend.app.live.providers import PROVIDERS

    gateway: Dict[str, Any] = {"available": True, "reason": None}
    sessions: List[Dict[str, Any]] = []
    resources: Dict[str, Any] = {}
    integrations = [p.descriptor().as_dict() for p in PROVIDERS.values()]
    degraded: List[str] = []
    try:
        data = await client.get_json("/sessions")
        integrations = (await client.get_json("/integrations"))["integrations"]
        # Only adopt the session list once BOTH calls worked: a half-answered overview must not
        # show sessions next to a "worker unreachable" banner (or the reverse).
        sessions = data["sessions"]
        resources = data.get("resources", {})
        degraded = [str(d) for d in data.get("degraded", [])]
    except client.GatewayUnavailable as exc:
        gateway = {
            "available": False,
            "reason": f"Worker gateway unreachable ({exc}). Is the worker running?",
        }
    except client.GatewayError as exc:
        gateway = {"available": False, "reason": exc.message}

    counts = {s: 0 for s in STATUSES}
    for session in sessions:
        counts[session["status"]] = counts.get(session["status"], 0) + 1
    by_integration: Dict[str, int] = {}
    for session in sessions:
        by_integration[session["integration"]] = by_integration.get(session["integration"], 0) + 1
    for item in integrations:
        item["session_count"] = by_integration.get(item["id"], 0)

    return {
        "gateway": gateway,
        "degraded": degraded,
        "integrations": integrations,
        "sessions": sessions,
        "counts": {**counts, "total": len(sessions)},
        "resources": resources,
        "can_control": principal.has(Scope.TASK_WRITE),
        "emergency_stop": emergency_stop.is_stopped,
    }


@router.get("/sessions/{integration}/{key}")
async def get_session(integration: str, key: str, principal: Principal = Depends(require_read)) -> Dict[str, Any]:
    _check_ref(integration, key)
    return await _gateway(client.get_json(f"/sessions/{integration}/{key}"))


# ---------------------------------------------------------------------------
# Frames (authenticated, never cached)
# ---------------------------------------------------------------------------
@router.get("/sessions/{integration}/{key}/frame")
async def frame(
    integration: str,
    key: str,
    quality: Literal["thumb", "full"] = "thumb",
    principal: Principal = Depends(require_read),
) -> Response:
    _check_ref(integration, key)
    resp = await _gateway(client.request("GET", f"/sessions/{integration}/{key}/frame", params={"quality": quality}))
    headers = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-frame") or k.lower() == "x-captured-at"}
    headers["Cache-Control"] = "no-store"
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "image/jpeg"), headers=headers)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@router.get("/events")
async def events(
    after: Optional[int] = None,
    epoch: Optional[str] = None,
    session: Optional[str] = Query(default=None, max_length=90),
    limit: int = Query(default=100, ge=1, le=300),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": limit}
    if after is not None:
        params["after"] = after
    if epoch:
        params["epoch"] = epoch
    if session:
        params["session"] = session
    return await _gateway(client.get_json("/events", params))


# ---------------------------------------------------------------------------
# Start / pause / resume / stop
# ---------------------------------------------------------------------------
class CreateSession(BaseModel):
    integration: Literal["browser"] = "browser"
    profile_id: str = Field(min_length=1, max_length=64)
    url: Optional[str] = Field(default=None, max_length=2048)


@router.post("/sessions", status_code=201)
async def create_session(
    body: CreateSession,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    if emergency_stop.is_stopped:
        raise HTTPException(status_code=409, detail={"error": "Emergency stop is active. Release it before starting a session."})
    result = await _gateway(client.post_json("/sessions", {**body.model_dump(), **_owner(principal)}))
    sid = result["session"]["id"]
    await write_audit(db, principal, "LIVE_SESSION_START", "live_session", sid,
                      after_state={"profile_id": body.profile_id})
    await db.commit()
    return result


async def _act(
    integration: str,
    key: str,
    action: str,
    principal: Principal,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _check_ref(integration, key)
    body = {"action": action, **_owner(principal), **(extra or {})}
    return await _gateway(client.post_json(f"/sessions/{integration}/{key}/action", body))


@router.post("/sessions/{integration}/{key}/pause")
async def pause(
    integration: str, key: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    result = await _act(integration, key, "pause", principal)
    await write_audit(db, principal, "LIVE_SESSION_PAUSE", "live_session", f"{integration}:{key}")
    await db.commit()
    return result


@router.post("/sessions/{integration}/{key}/resume")
async def resume(
    integration: str, key: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Resume the agent. For a session waiting on a human, the worker first re-inspects the
    real page; if verification is still required this returns 409 and nothing resumes."""
    enforce_rate_limit(request, "write")
    if emergency_stop.is_stopped:
        raise HTTPException(status_code=409, detail={"error": "Emergency stop is active. Release it before resuming the agent."})
    result = await _act(integration, key, "resume", principal)

    task_resumed = None
    task_id = result.get("task_id")
    if task_id:
        manager = TaskManager(db)
        task = await manager.get_by_task_id(task_id)
        if task is not None and task.status == TaskStatus.NEEDS_USER_ACTION.value:
            try:
                await manager.transition(task, TaskStatus.READY, reason="Operator completed verification; session resumed")
                task_resumed = task.task_id
            except Exception as exc:  # leave the task parked rather than guess
                logger.warning("Could not re-queue task %s: %s", task_id, exc)
    result["task_requeued"] = task_resumed
    await write_audit(db, principal, "LIVE_SESSION_RESUME", "live_session", f"{integration}:{key}",
                      after_state={"resumed_from": result.get("resumed_from"), "task_requeued": task_resumed})
    await db.commit()
    return result


@router.post("/sessions/{integration}/{key}/stop")
async def stop(
    integration: str, key: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    result = await _act(integration, key, "stop", principal)
    await write_audit(db, principal, "LIVE_SESSION_STOP", "live_session", f"{integration}:{key}")
    await db.commit()
    return result


# ---------------------------------------------------------------------------
# Manual control: exclusive lock + input
# ---------------------------------------------------------------------------
class ControlBody(BaseModel):
    mode: Literal["click", "full"] = "click"


class TokenBody(BaseModel):
    token: str = Field(min_length=8, max_length=128)


@router.post("/sessions/{integration}/{key}/control")
async def acquire_control(
    integration: str, key: str, body: ControlBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Enable Click / Take Control. Fails with 409 when someone else holds the lock."""
    enforce_rate_limit(request, "write")
    result = await _act(integration, key, "control.acquire", principal, {"mode": body.mode})
    await write_audit(db, principal, "LIVE_CONTROL_ACQUIRE", "live_session", f"{integration}:{key}",
                      after_state={"mode": result.get("mode")})
    await db.commit()
    return result


@router.post("/sessions/{integration}/{key}/control/renew")
async def renew_control(
    integration: str, key: str, body: TokenBody, principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    return await _act(integration, key, "control.renew", principal, {"token": body.token})


@router.post("/sessions/{integration}/{key}/control/release")
async def release_control(
    integration: str, key: str, body: TokenBody,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    result = await _act(integration, key, "control.release", principal, {"token": body.token})
    await write_audit(db, principal, "LIVE_CONTROL_RELEASE", "live_session", f"{integration}:{key}",
                      after_state={"input_counts": result.get("counts")})
    await db.commit()
    return result


class InputBody(BaseModel):
    token: str = Field(min_length=8, max_length=128)
    seq: int = Field(ge=1)
    type: Literal["click", "dblclick", "wheel", "key", "text"]
    fx: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    fy: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    dx: Optional[float] = Field(default=None, ge=-3000, le=3000)
    dy: Optional[float] = Field(default=None, ge=-3000, le=3000)
    frame_w: Optional[int] = Field(default=None, ge=1, le=10000)
    frame_h: Optional[int] = Field(default=None, ge=1, le=10000)
    key: Optional[str] = Field(default=None, max_length=24)
    text: Optional[str] = Field(default=None, max_length=200)


@router.post("/sessions/{integration}/{key}/input")
async def send_input(
    integration: str, key: str, body: InputBody, principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Forward ONE input event to the real session. Requires the live lock token issued
    to this very operator for this very session; anything else is rejected by the worker."""
    _check_ref(integration, key)
    payload = {**body.model_dump(exclude_none=True), **_owner(principal)}
    return await _gateway(client.post_json(f"/sessions/{integration}/{key}/input", payload))
