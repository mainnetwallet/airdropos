"""Browser profile management (one page, many profiles).

A profile is configuration plus encrypted saved session state. Running browsers
are separate and appear in Live Broadcast.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.browser import profiles as store
from backend.app.database.session import get_db
from backend.app.live import client
from backend.app.security.auth import (
    Principal,
    enforce_rate_limit,
    require_read,
    require_task_write,
    write_audit,
)

router = APIRouter(prefix="/browser", tags=["browser"])


class ProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)
    viewport_width: int = Field(default=0, ge=0, le=3840)
    viewport_height: int = Field(default=0, ge=0, le=2160)
    locale: str = Field(default="", max_length=16)
    timezone_id: str = Field(default="", max_length=64)
    start_url: str = Field(default="", max_length=2048)


class ProfilePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    description: Optional[str] = Field(default=None, max_length=200)
    viewport_width: Optional[int] = Field(default=None, ge=0, le=3840)
    viewport_height: Optional[int] = Field(default=None, ge=0, le=2160)
    locale: Optional[str] = Field(default=None, max_length=16)
    timezone_id: Optional[str] = Field(default=None, max_length=64)
    start_url: Optional[str] = Field(default=None, max_length=2048)


def _wrap(exc: store.ProfileError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"error": str(exc)})


async def _live_status(profile_id: str) -> Optional[str]:
    """Status of the running session for a profile, or None (also when the worker is down)."""
    try:
        data = await client.get_json(f"/sessions/browser/{profile_id}")
        return data["session"]["status"]
    except Exception:
        return None


def _public(profile: store.BrowserProfile, status: Optional[str]) -> Dict[str, Any]:
    data = profile.as_dict()
    data["has_saved_state"] = store.has_state(profile.id)
    data["session_status"] = status
    return data


@router.get("/profiles")
async def list_profiles(principal: Principal = Depends(require_read)) -> Dict[str, Any]:
    out = []
    for profile in store.list_profiles():
        out.append(_public(profile, await _live_status(profile.id)))
    return {"profiles": out}


@router.post("/profiles", status_code=201)
async def create_profile(
    body: ProfileBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    try:
        profile = store.create_profile(body.model_dump())
    except store.ProfileError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "BROWSER_PROFILE_CREATE", "browser_profile", profile.id,
                      after_state={"name": profile.name})
    await db.commit()
    return _public(profile, None)


@router.patch("/profiles/{profile_id}")
async def update_profile(
    profile_id: str, body: ProfilePatch, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    if not store.valid_profile_id(profile_id):
        raise HTTPException(status_code=400, detail={"error": "Invalid profile id."})
    try:
        profile = store.update_profile(profile_id, body.model_dump(exclude_none=True))
    except store.ProfileError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "BROWSER_PROFILE_UPDATE", "browser_profile", profile.id)
    await db.commit()
    return _public(profile, await _live_status(profile.id))


async def _require_not_running(profile_id: str) -> None:
    status = await _live_status(profile_id)
    if status is not None and status != "stopped":
        raise HTTPException(
            status_code=409,
            detail={"error": "A browser session is running for this profile. Stop it in Live Broadcast first."},
        )


@router.delete("/profiles/{profile_id}")
async def delete_profile(
    profile_id: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    if not store.valid_profile_id(profile_id):
        raise HTTPException(status_code=400, detail={"error": "Invalid profile id."})
    await _require_not_running(profile_id)
    try:
        store.delete_profile(profile_id)
    except store.ProfileError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "BROWSER_PROFILE_DELETE", "browser_profile", profile_id)
    await db.commit()
    return {"deleted": profile_id}


@router.delete("/profiles/{profile_id}/state")
async def clear_profile_state(
    profile_id: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Forget saved cookies/local storage (logs the profile out everywhere)."""
    enforce_rate_limit(request, "write")
    if not store.valid_profile_id(profile_id) or store.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail={"error": "Profile not found."})
    await _require_not_running(profile_id)
    store.clear_state(profile_id)
    await write_audit(db, principal, "BROWSER_PROFILE_CLEAR_STATE", "browser_profile", profile_id)
    await db.commit()
    return {"cleared": profile_id}
