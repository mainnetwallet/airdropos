"""Browser profile management (one page, many profiles).

A profile is configuration plus a real Chrome user-data directory. Running browsers
are separate and appear in Live Broadcast.

Everything here is derived from real state. The API process never talks to Chrome: it
asks the worker (the only process that owns the browsers) through the internal gateway
and returns exactly what the worker observed. When the worker cannot be reached the
status is ``unknown``, never "not running".
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.browser import chrome
from backend.app.browser import profiles as store
from backend.app.browser.profiles import ProfileError
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
    browser_type: str = Field(default="chrome", max_length=16)
    launch_mode: str = Field(default="managed", max_length=16)
    executable_path: str = Field(default="", max_length=1024)
    user_data_dir: str = Field(default="", max_length=1024)
    headless: Optional[bool] = None
    debugger_host: str = Field(default="", max_length=64)
    debugger_port: int = Field(default=0, ge=0, le=65535)
    unpacked_extensions: str = Field(default="", max_length=4000)


class ProfilePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    description: Optional[str] = Field(default=None, max_length=200)
    viewport_width: Optional[int] = Field(default=None, ge=0, le=3840)
    viewport_height: Optional[int] = Field(default=None, ge=0, le=2160)
    locale: Optional[str] = Field(default=None, max_length=16)
    timezone_id: Optional[str] = Field(default=None, max_length=64)
    start_url: Optional[str] = Field(default=None, max_length=2048)
    browser_type: Optional[str] = Field(default=None, max_length=16)
    launch_mode: Optional[str] = Field(default=None, max_length=16)
    executable_path: Optional[str] = Field(default=None, max_length=1024)
    user_data_dir: Optional[str] = Field(default=None, max_length=1024)
    headless: Optional[bool] = None
    debugger_host: Optional[str] = Field(default=None, max_length=64)
    debugger_port: Optional[int] = Field(default=None, ge=0, le=65535)
    unpacked_extensions: Optional[str] = Field(default=None, max_length=4000)


class OpenBody(BaseModel):
    url: str = Field(default="", max_length=2048)


class DeleteDataBody(BaseModel):
    """Deleting the Chrome profile data is irreversible, so it needs the profile id back."""
    confirm: str = Field(min_length=1, max_length=64)


def _wrap(exc: ProfileError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"error": str(exc)})


# Result of asking the worker about a profile's browser session.
#   a status string -> the worker answered and a session record exists
#   NOT_RUNNING     -> the worker answered and there is definitely no session for this profile
#   UNKNOWN         -> the worker could not be asked (down, unreachable, rejected our key, ...)
# UNKNOWN is never treated as "not running": destructive operations must fail closed.
NOT_RUNNING = "none"
UNKNOWN = "unknown"


async def _live_status(profile_id: str) -> str:
    """Status of the running session for a profile: a status, NOT_RUNNING or UNKNOWN."""
    try:
        data = await client.get_json(f"/sessions/browser/{profile_id}")
        return str(data["session"]["status"])
    except client.GatewayError as exc:
        # Only OUR gateway's explicit "no such session" counts. A bare 404 could come from a
        # wrong LIVE_GATEWAY_URL pointing at some other server, which must not unlock deletes.
        if exc.status == 404 and exc.code == "not_found":
            return NOT_RUNNING
        return UNKNOWN
    except Exception:
        return UNKNOWN


async def _worker_profile_statuses() -> Optional[Dict[str, Dict[str, Any]]]:
    """Every profile's live state, in ONE call to the worker.

    The page polls this every few seconds; asking per profile meant N worker round trips
    (and N reconciles) per poll. None is returned when the worker cannot be reached.
    """
    try:
        data = await client.get_json("/browser/profiles")
        statuses = data.get("statuses")
        return statuses if isinstance(statuses, dict) else None
    except Exception:
        return None


async def _worker_profile_status(profile_id: str) -> Optional[Dict[str, Any]]:
    """The worker's own view of a profile's browser, or None when it cannot be asked."""
    statuses = await _worker_profile_statuses()
    if statuses is None:
        return None
    found = statuses.get(profile_id)
    return found if isinstance(found, dict) else None


def _public(
    profile: store.BrowserProfile,
    status: Optional[str],
    *,
    worker: Optional[Dict[str, Any]] = None,
    default_id: str = "",
) -> Dict[str, Any]:
    """``status`` is None when no session exists; the UI also receives "unknown" when the worker is unreachable."""
    data = profile.as_dict()
    data["has_saved_state"] = store.has_state(profile.id)
    data["session_status"] = None if status in (None, NOT_RUNNING) else status
    data["resolved_user_data_dir"] = profile.resolved_user_data_dir
    data["resolved_executable"] = profile.resolved_executable()
    data["is_default_agent"] = profile.id == default_id
    data["configuration_problems"] = store.validate_profile_config(profile)
    if worker:
        data["browser_running"] = bool(worker.get("running"))
        data["connection"] = worker.get("connection")
        data["tabs"] = worker.get("tabs") or []
        data["tab_count"] = worker.get("tab_count")
        data["active_url"] = worker.get("active_url") or ""
        data["active_title"] = worker.get("active_title") or ""
        data["extensions"] = (worker.get("extensions") or {}).get("installed") or []
        data["extension_count"] = (worker.get("extensions") or {}).get("installed_count", 0)
        data["last_error"] = worker.get("last_error")
        data["last_recovery"] = worker.get("last_recovery")
        data["limitations"] = worker.get("limitations") or []
    else:
        data["browser_running"] = False
        data["connection"] = None
        data["tabs"] = []
        data["tab_count"] = None
        data["active_url"] = ""
        data["active_title"] = ""
        data["extensions"] = []
        data["extension_count"] = None
    return data


async def _require_profile(profile_id: str) -> store.BrowserProfile:
    if not store.valid_profile_id(profile_id):
        raise HTTPException(status_code=400, detail={"error": "Invalid profile id."})
    profile = store.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail={"error": "Profile not found."})
    return profile


@router.get("/profiles")
async def list_profiles(principal: Principal = Depends(require_read)) -> Dict[str, Any]:
    profiles = store.list_profiles()
    default_id = store.default_agent_profile_id()
    reachable = True
    statuses: Dict[str, str] = {}
    if profiles:
        try:
            data = await client.get_json("/sessions")
            statuses = {
                str(x.get("key")): str(x.get("status"))
                for x in data.get("sessions", [])
                if x.get("integration") == "browser"
            }
        except Exception:
            reachable = False
    worker_statuses = await _worker_profile_statuses() if reachable else None
    if worker_statuses is None:
        reachable = False
    default = store.get_default_agent_profile()
    out = []
    for profile in profiles:
        status = statuses.get(profile.id, NOT_RUNNING) if reachable else UNKNOWN
        worker = worker_statuses.get(profile.id) if worker_statuses else None
        out.append(_public(profile, status, worker=worker, default_id=default_id))
    chrome_info = chrome.find_chrome_executable()
    return {
        "profiles": out,
        "default_agent_profile": default.id if default else "",
        "worker_reachable": reachable,
        "chrome": {
            "found": bool(chrome_info.get("exists")),
            "path": chrome_info.get("path", ""),
            "source": chrome_info.get("source", ""),
            "version": chrome.executable_version(str(chrome_info.get("path") or "")) or "",
            "detail": chrome_info.get("detail", ""),
        },
        "profile_root": str(chrome.profile_root()),
        "persistent_profiles_enabled": _persistent_enabled(),
    }


def _persistent_enabled() -> bool:
    from backend.app.core.config import settings

    return bool(settings.BROWSER_PERSISTENT_PROFILES)


@router.get("/profiles/default")
async def get_default_profile(principal: Principal = Depends(require_read)) -> Dict[str, Any]:
    default = store.get_default_agent_profile()
    return {
        "default_agent_profile": default.id if default else None,
        "name": default.name if default else None,
    }


@router.post("/profiles/default")
async def set_default_profile(
    body: dict, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Choose (or clear) the profile the agent uses for browser tasks.

    Persisted, so it survives a restart. An agent browser task that does not name a
    profile will use this one from then on.
    """
    enforce_rate_limit(request, "write")
    profile_id = str(body.get("profile_id") or "").strip()
    try:
        profile = store.set_default_agent_profile(profile_id or None)
    except ProfileError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "BROWSER_PROFILE_SET_DEFAULT", "browser_profile", profile_id or "-")
    await db.commit()
    return {"default_agent_profile": profile.id if profile else None}


@router.post("/profiles", status_code=201)
async def create_profile(
    body: ProfileBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    try:
        profile = store.create_profile(body.model_dump())
    except ProfileError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "BROWSER_PROFILE_CREATE", "browser_profile", profile.id,
                      after_state={"name": profile.name, "launch_mode": profile.launch_mode})
    await db.commit()
    return _public(profile, None, default_id=store.default_agent_profile_id())


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
    except ProfileError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "BROWSER_PROFILE_UPDATE", "browser_profile", profile.id)
    await db.commit()
    return _public(
        profile,
        await _live_status(profile.id),
        worker=await _worker_profile_status(profile.id),
        default_id=store.default_agent_profile_id(),
    )


@router.post("/profiles/{profile_id}/open")
async def open_profile(
    profile_id: str, body: OpenBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Start (or reuse) the real browser for this profile and return what actually happened.

    Refuses when the worker cannot be reached: reporting "running" for a browser that
    was never started would be a lie the operator acts on.
    """
    enforce_rate_limit(request, "write")
    profile = await _require_profile(profile_id)
    try:
        data = await client.post_json("/browser/profiles/open", {"profile_id": profile.id, "url": body.url})
    except client.GatewayUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": f"The browser worker is not reachable ({exc}), so no browser was started."},
        )
    except client.GatewayError as exc:
        raise HTTPException(status_code=409 if exc.status < 500 else 502,
                            detail={"error": exc.message or "The browser could not be started."})
    status = data.get("status") or {}
    if not status.get("running"):
        raise HTTPException(
            status_code=409,
            detail={"error": status.get("last_error") or "The browser did not actually start."},
        )
    await write_audit(db, principal, "BROWSER_PROFILE_OPEN", "browser_profile", profile.id,
                      after_state={"connection": status.get("connection"), "reused": bool(data.get("reused"))})
    await db.commit()
    return {"profile": _public(profile, await _live_status(profile.id), worker=status,
                                default_id=store.default_agent_profile_id()),
            "reused": bool(data.get("reused"))}


@router.post("/profiles/{profile_id}/reconnect")
async def reconnect_profile(
    profile_id: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Re-establish the connection to this profile's browser (health check or restart)."""
    enforce_rate_limit(request, "write")
    profile = await _require_profile(profile_id)
    try:
        data = await client.post_json("/browser/profiles/reconnect", {"profile_id": profile.id})
    except client.GatewayUnavailable as exc:
        raise HTTPException(status_code=503,
                            detail={"error": f"The browser worker is not reachable ({exc})."})
    except client.GatewayError as exc:
        raise HTTPException(status_code=409 if exc.status < 500 else 502,
                            detail={"error": exc.message or "The browser could not be reconnected."})
    status = data.get("status") or {}
    await write_audit(db, principal, "BROWSER_PROFILE_RECONNECT", "browser_profile", profile.id)
    await db.commit()
    return {"profile": _public(profile, await _live_status(profile.id), worker=status,
                                default_id=store.default_agent_profile_id())}


@router.post("/profiles/{profile_id}/close")
async def close_profile(
    profile_id: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Stop this profile's browser. Its Chrome profile data is left untouched on disk."""
    enforce_rate_limit(request, "write")
    profile = await _require_profile(profile_id)
    try:
        data = await client.post_json("/browser/profiles/close", {"profile_id": profile.id})
    except client.GatewayUnavailable as exc:
        raise HTTPException(status_code=503,
                            detail={"error": f"The browser worker is not reachable ({exc})."})
    except client.GatewayError as exc:
        raise HTTPException(status_code=409 if exc.status < 500 else 502,
                            detail={"error": exc.message or "The browser could not be stopped."})
    await write_audit(db, principal, "BROWSER_PROFILE_CLOSE", "browser_profile", profile.id)
    await db.commit()
    return {"closed": profile.id, "was_running": bool(data.get("was_running")),
            "profile_data_kept": True}


@router.get("/profiles/{profile_id}/extensions")
async def profile_extensions(profile_id: str, principal: Principal = Depends(require_read)) -> Dict[str, Any]:
    """Extensions actually installed in this profile, read from its own Chrome state."""
    await _require_profile(profile_id)
    try:
        data = await client.get_json(f"/browser/profiles/{profile_id}/extensions")
    except client.GatewayUnavailable:
        return {"extensions": [], "installed_count": 0,
                "note": "The browser worker is not reachable, so the installed extension list is unknown."}
    except Exception:
        return {"extensions": [], "installed_count": 0,
                "note": "The installed extension list could not be read from the worker."}
    return data.get("extensions") or {"extensions": [], "installed_count": 0}


async def _require_not_running(profile_id: str) -> None:
    """Fail closed: only a definite "no session" / "stopped" answer allows a destructive change."""
    status = await _live_status(profile_id)
    if status in (NOT_RUNNING, "stopped"):
        return
    if status == UNKNOWN:
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    "Cannot confirm whether a browser session is running for this profile "
                    "(the worker is unreachable). Nothing was changed; try again once the worker is up."
                )
            },
        )
    raise HTTPException(
        status_code=409,
        detail={"error": "A browser session is running for this profile. Stop it in Live Broadcast first."},
    )


@router.delete("/profiles/{profile_id}")
async def delete_profile(
    profile_id: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Remove the profile CONFIGURATION only.

    The Chrome profile directory, with every cookie, login and installed extension in it,
    is deliberately left on disk. Deleting that data is a separate, explicitly confirmed
    operation (``DELETE /browser/profiles/{id}/data``).
    """
    enforce_rate_limit(request, "write")
    profile = await _require_profile(profile_id)
    await _require_not_running(profile_id)
    try:
        store.delete_profile(profile_id)
    except ProfileError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "BROWSER_PROFILE_DELETE", "browser_profile", profile_id,
                      after_state={"chrome_data_kept_at": profile.resolved_user_data_dir})
    await db.commit()
    return {"deleted": profile_id, "chrome_data_kept_at": profile.resolved_user_data_dir}


@router.delete("/profiles/{profile_id}/data")
async def delete_profile_data(
    profile_id: str, body: DeleteDataBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Permanently delete the Chrome profile DATA. Requires sending the profile id back."""
    enforce_rate_limit(request, "write")
    await _require_profile(profile_id)
    await _require_not_running(profile_id)
    try:
        result = store.delete_profile_data(profile_id, confirm=body.confirm)
    except ProfileError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "BROWSER_PROFILE_DELETE_DATA", "browser_profile", profile_id,
                      after_state=result)
    await db.commit()
    return result


@router.delete("/profiles/{profile_id}/state")
async def clear_profile_state(
    profile_id: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_task_write),
) -> Dict[str, Any]:
    """Forget the saved AirdropOS session snapshot (logs the profile out of the ephemeral fallback).

    This does NOT touch a managed Chrome profile: Chrome stores its own cookies in its own
    directory, and they are only removed with the explicit data deletion above.
    """
    enforce_rate_limit(request, "write")
    if not store.valid_profile_id(profile_id) or store.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail={"error": "Profile not found."})
    await _require_not_running(profile_id)
    store.clear_state(profile_id)
    await write_audit(db, principal, "BROWSER_PROFILE_CLEAR_STATE", "browser_profile", profile_id)
    await db.commit()
    return {"cleared": profile_id}
