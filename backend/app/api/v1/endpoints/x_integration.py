"""X.com integration endpoints.

Everything the X.com page needs, and nothing that exposes a secret:

* configuration CRUD, enable/disable, default selection, test connection
* the OAuth 2.0 authorization-code + PKCE start and completion
* bulk import with a validating dry run before anything is written
* browser-profile selection and live status
* execution preference
* activity / task history

Every write goes through the existing principal checks, the rate limiter and the
audit log. Secrets are accepted on the way in and never returned on the way out.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database.session import get_db
from backend.app.integrations.x import browser_adapter, routing, service
from backend.app.integrations.x.credentials import (
    CredentialError,
    ImportError_,
    parse_document,
    preview_import,
    validate_configuration,
)
from backend.app.integrations.x.crypto import VaultUnavailable
from backend.app.integrations.x.store import (
    StoreError,
    create_configuration,
    delete_configuration,
    get_configuration,
    list_activity,
    list_configurations,
    public_activity,
    public_configuration,
    set_default_configuration,
    update_configuration,
)
from backend.app.security.auth import (
    Principal,
    enforce_rate_limit,
    require_read,
    require_task_write,
    require_x_credentials,
    write_audit,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/x", tags=["x"])

X_ENTITY = "x_api_configuration"


def _wrap(exc: StoreError) -> HTTPException:
    detail: Dict[str, Any] = {"error": str(exc)}
    if exc.field:
        detail["field"] = exc.field
    return HTTPException(status_code=exc.status, detail=detail)


# ======================================================================
# Request bodies
# ======================================================================
class ConfigurationBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=400)
    client_id: str = Field(default="", max_length=255)
    # Secrets are write-only. An empty string on edit means "keep what is stored".
    client_secret: str = Field(default="", max_length=512)
    access_token: str = Field(default="", max_length=2048)
    refresh_token: str = Field(default="", max_length=2048)
    username: str = Field(default="", max_length=64)
    enabled: bool = True
    default: bool = False


class ConfigurationPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=400)
    client_id: Optional[str] = Field(default=None, max_length=255)
    client_secret: Optional[str] = Field(default=None, max_length=512)
    access_token: Optional[str] = Field(default=None, max_length=2048)
    refresh_token: Optional[str] = Field(default=None, max_length=2048)
    username: Optional[str] = Field(default=None, max_length=64)
    enabled: Optional[bool] = None
    default: Optional[bool] = None


class EnabledBody(BaseModel):
    enabled: bool


class DefaultBody(BaseModel):
    config_id: Optional[str] = Field(default=None, max_length=64)


class ImportBody(BaseModel):
    # JSON or CSV text. Cap is checked server-side; 2 MB is far above the
    # largest sane credential file and far below anything that could stall a
    # request.
    content: str = Field(min_length=1, max_length=2_000_000)
    filename: str = Field(default="", max_length=200)
    content_type: str = Field(default="", max_length=120)
    # When false (the default) nothing is written; the response is the preview.
    commit: bool = False
    # Only these preview rows are written on a commit. Empty means all of them.
    # Bounded: the document is already capped at MAX_IMPORT_ROWS, so an unbounded
    # list here would only let a caller send a huge array of integers to be turned
    # into a set.
    rows: List[int] = Field(default_factory=list, max_length=1000)


class CompleteAuthBody(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=256)


class PreferenceBody(BaseModel):
    preference: str = Field(min_length=1, max_length=32)


class DuplicateWindowBody(BaseModel):
    hours: float = Field(ge=0, le=720)


class BrowserProfileBody(BaseModel):
    profile_id: Optional[str] = Field(default=None, max_length=64)


# ======================================================================
# Overview
# ======================================================================
@router.get("/overview")
async def overview(request: Request, principal: Principal = Depends(require_read), db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Everything the X.com page's Overview needs, in one call."""
    # Read-side rate limiting matters here: this endpoint drives a live browser
    # inspection, so an unbounded caller would keep Chrome busy.
    enforce_rate_limit(request, "read")
    status = await service.configuration_status(db)
    if status.get("browser_profile_id"):
        try:
            status["browser"] = await browser_adapter.session_status(status["browser_profile_id"])
        except Exception as exc:
            status["browser"] = {
                "ok": False,
                "detail": f"{type(exc).__name__}: the browser session could not be inspected.",
            }
    else:
        status["browser"] = {"ok": False, "detail": status.get("browser_profile_reason", "")}
    activity = await list_activity(db, limit=25)
    status["recent_activity"] = [public_activity(row) for row in activity]
    status["counts"] = {
        "recent_success": sum(1 for r in activity if r.status == "SUCCESS"),
        "recent_failed": sum(1 for r in activity if r.status == "FAILED"),
        "recent_uncertain": sum(1 for r in activity if r.outcome_uncertain),
        "recent_needs_user": sum(1 for r in activity if r.status == "NEEDS_USER_ACTION"),
    }
    status["preference"] = await service.effective_preference(db)
    return status


@router.get("/configurations")
async def list_configs(request: Request, principal: Principal = Depends(require_read), db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    enforce_rate_limit(request, "read")
    rows = await list_configurations(db)
    return {"configurations": [public_configuration(r) for r in rows],
            "preference": await service.effective_preference(db)}


@router.get("/configurations/{config_id}")
async def get_config(config_id: str, request: Request, principal: Principal = Depends(require_read), db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    enforce_rate_limit(request, "read")
    try:
        row = await get_configuration(db, config_id)
    except StoreError as exc:
        raise _wrap(exc)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "Configuration not found."})
    return {"configuration": public_configuration(row)}


@router.post("/configurations", status_code=201)
async def create_config(
    body: ConfigurationBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    try:
        clean, _warnings = validate_configuration(body.model_dump())
        row = await create_configuration(db, clean, actor=principal.email)
    except CredentialError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc), "field": exc.field})
    except StoreError as exc:
        raise _wrap(exc)
    except VaultUnavailable as exc:
        # The vault key is unsafe on this deployment. Failing closed is right;
        # failing with an actionable 409 instead of an opaque 500 is better.
        raise HTTPException(status_code=409, detail={"error": str(exc), "field": "client_secret"})
    await write_audit(db, principal, "X_CONFIG_CREATE", X_ENTITY, str(row.id),
                      after_state={"name": row.name, "client_id_preview": public_configuration(row)["client_id_preview"]})
    await db.commit()
    return {"configuration": public_configuration(row)}


@router.patch("/configurations/{config_id}")
async def patch_config(
    config_id: str, body: ConfigurationPatch, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    """Update a configuration. Secrets are write-only; blank means unchanged."""
    enforce_rate_limit(request, "write")
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        if changes.get("name") is not None or changes.get("client_id"):
            # Re-run field validation only for the fields that are actually being set.
            probe = dict(changes)
            probe.setdefault("name", "probe")
            probe.setdefault("client_id", "probe")
            validate_configuration({**probe, "client_secret": "x" * 32})
        row = await update_configuration(db, config_id, changes)
    except CredentialError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc), "field": exc.field})
    except StoreError as exc:
        raise _wrap(exc)
    except VaultUnavailable as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc)})
    await write_audit(db, principal, "X_CONFIG_UPDATE", X_ENTITY, str(row.id),
                      after_state={"name": row.name, "enabled": row.is_enabled, "default": row.is_default})
    await db.commit()
    return {"configuration": public_configuration(row)}


@router.post("/configurations/{config_id}/enabled")
async def set_enabled(
    config_id: str, body: EnabledBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    try:
        row = await update_configuration(db, config_id, {"enabled": body.enabled})
    except StoreError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "X_CONFIG_ENABLED" if body.enabled else "X_CONFIG_DISABLED",
                      X_ENTITY, str(row.id), after_state={"enabled": body.enabled})
    await db.commit()
    return {"configuration": public_configuration(row)}


@router.post("/configurations/default")
async def set_default(
    body: DefaultBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    """Choose which authorized account the agent prefers.

    This selects the IDENTITY the agent posts as, so it sits with the other
    credential controls rather than with task creation.
    """
    enforce_rate_limit(request, "write")
    try:
        await set_default_configuration(db, body.config_id)
    except StoreError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "X_CONFIG_SET_DEFAULT", X_ENTITY, body.config_id or "-")
    await db.commit()
    return {"default_config_id": body.config_id or ""}


@router.delete("/configurations/{config_id}")
async def remove_config(
    config_id: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    """Delete a configuration and its stored secrets.

    Activity history is deliberately KEPT: it is the evidence of what was sent
    with this credential, and it outlives the credential on purpose.
    """
    enforce_rate_limit(request, "write")
    try:
        await delete_configuration(db, config_id)
    except StoreError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "X_CONFIG_DELETE", X_ENTITY, config_id,
                      after_state={"activity_history": "kept"})
    await db.commit()
    return {"deleted": config_id, "activity_history": "kept"}


@router.post("/configurations/{config_id}/test")
async def test_config(
    config_id: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    """Prove the configuration works, with a real call to X."""
    enforce_rate_limit(request, "write")
    try:
        result = await service.test_connection(db, config_id)
    except StoreError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "X_CONFIG_TEST", X_ENTITY, config_id,
                      after_state={"status": result.get("status")})
    await db.commit()
    return result


# ======================================================================
# OAuth 2.0 (authorization code + PKCE)
# ======================================================================
@router.post("/configurations/{config_id}/authorize")
async def start_authorization(
    config_id: str, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    try:
        result = await service.begin_authorization(db, config_id)
    except StoreError as exc:
        raise _wrap(exc)
    await write_audit(db, principal, "X_CONFIG_AUTHORIZE_START", X_ENTITY, config_id,
                      after_state={"scopes": result.get("scopes")})
    await db.commit()
    return result


@router.post("/configurations/{config_id}/authorize/complete")
async def finish_authorization(
    config_id: str, body: CompleteAuthBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "write")
    try:
        result = await service.complete_authorization(db, config_id, body.code, body.state)
    except StoreError as exc:
        raise _wrap(exc)
    # The response deliberately contains no token value, only whether one was stored.
    await write_audit(db, principal, "X_CONFIG_AUTHORIZE_COMPLETE", X_ENTITY, config_id,
                      after_state={"has_access_token": result.get("has_access_token"),
                                   "has_refresh_token": result.get("has_refresh_token")})
    await db.commit()
    return result


# ======================================================================
# Bulk import
# ======================================================================
@router.post("/import/preview")
async def preview_import_endpoint(
    body: ImportBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    """Validate a JSON/CSV document and report what would happen. Writes nothing."""
    enforce_rate_limit(request, "write")
    try:
        rows = parse_document(body.content, content_type=body.content_type, filename=body.filename)
    except ImportError_ as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)})

    existing = await list_configurations(db)
    current_default = next((r.name for r in existing if r.is_default), None)
    preview = preview_import(rows, existing_names=[r.name for r in existing], current_default=current_default)
    await write_audit(db, principal, "X_IMPORT_PREVIEW", "x_import", body.filename or "pasted",
                      after_state={"valid": preview.valid_count, "errors": len(preview.errors)})
    await db.commit()
    return preview.as_dict()


@router.post("/import")
async def import_configurations(
    body: ImportBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    """Import several configurations at once, through the same validation path.

    Secrets arrive here, are sealed immediately, and are never echoed back: the
    response reports names and counts only.
    """
    enforce_rate_limit(request, "write")
    try:
        rows = parse_document(body.content, content_type=body.content_type, filename=body.filename)
    except ImportError_ as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)})

    existing = await list_configurations(db)
    current_default = next((r.name for r in existing if r.is_default), None)
    preview = preview_import(rows, existing_names=[r.name for r in existing], current_default=current_default)

    wanted = set(body.rows) if body.rows else None
    created: List[str] = []
    skipped: List[Dict[str, Any]] = []
    for row in preview.rows:
        if wanted is not None and row["row"] not in wanted:
            skipped.append({"row": row["row"], "name": row["name"], "reason": "not selected"})
            continue
        secrets = preview.secrets_for(row["row"])
        payload = {
            "name": row["name"],
            "description": row.get("description", ""),
            "client_id": preview.client_id_for(row["row"]),
            "username": row.get("username", ""),
            "enabled": row.get("enabled", True),
            "default": row.get("default", False),
            **secrets,
        }
        try:
            new_row = await create_configuration(db, payload, actor=principal.email)
            created.append(new_row.name)
        except StoreError as exc:
            skipped.append({"row": row["row"], "name": row["name"], "reason": str(exc)})
        except VaultUnavailable as exc:
            skipped.append({"row": row["row"], "name": row["name"], "reason": str(exc)})

    await write_audit(db, principal, "X_IMPORT_COMMIT", "x_import", body.filename or "pasted",
                      after_state={"created": len(created), "skipped": len(skipped),
                                   "errors": len(preview.errors)})
    await db.commit()
    rows_after = await list_configurations(db)
    return {
        "created": created,
        "created_count": len(created),
        "skipped": skipped,
        "errors": preview.errors,
        "warnings": preview.warnings,
        "configurations": [public_configuration(r) for r in rows_after],
    }


# ======================================================================
# Preferences and browser profiles
# ======================================================================
@router.get("/preference")
async def get_preference(
    request: Request, principal: Principal = Depends(require_read), db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    enforce_rate_limit(request, "read")
    return {
        "preference": await service.effective_preference(db),
        "options": list(routing.PREFERENCES),
        "explanation": _preference_help(),
        "duplicate_window_hours": await service.duplicate_window_seconds(db) / 3600,
    }


@router.post("/duplicate-window")
async def set_duplicate_window(
    body: DuplicateWindowBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    """Change the duplicate-prevention window.

    Gated on `x_credentials`, not `task_write`: this weakens a safety mechanism,
    so a session that may merely create tasks must not be able to turn it off.
    Setting it to 0 relaxes DELIBERATE repeats of a known-completed write; an
    UNCERTAIN write is still never repeated, at any window.
    """
    enforce_rate_limit(request, "write")
    if not 0 <= body.hours <= 720:
        raise HTTPException(status_code=422, detail={"error": "hours must be between 0 and 720."})
    from backend.app.services.preferences import X_DUPLICATE_WINDOW_HOURS, set_preference as store_pref

    value = f"{body.hours:g}"
    await store_pref(
        db, X_DUPLICATE_WINDOW_HOURS, value,
        description="Hours in which an identical completed X write is refused as a duplicate",
    )
    await write_audit(db, principal, "X_DUPLICATE_WINDOW_SET", "x_setting", X_DUPLICATE_WINDOW_HOURS,
                      after_state={"hours": body.hours})
    await db.commit()
    return {"duplicate_window_hours": await service.duplicate_window_seconds(db) / 3600}


@router.post("/preference")
async def set_preference(
    body: PreferenceBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    """Change how X tasks are routed by default.

    Stored in the ``system_settings`` table rather than in a process-local
    variable, so it survives a restart and is seen identically by the API and the
    worker (which are separate processes with separate registries).

    ``x_credentials``, not ``task_write``: this decides how the agent acts on a
    third-party service, which is not the same authority as creating a task.
    """
    enforce_rate_limit(request, "write")
    if body.preference not in routing.PREFERENCES:
        raise HTTPException(
            status_code=422,
            detail={"error": f"Unknown preference '{body.preference}'. Use one of: {', '.join(routing.PREFERENCES)}."},
        )
    from backend.app.services.preferences import X_EXECUTION_PREFERENCE, set_preference as store_preference

    await store_preference(
        db, X_EXECUTION_PREFERENCE, body.preference,
        description="X.com execution routing preference",
    )
    await write_audit(db, principal, "X_PREFERENCE_SET", "x_setting", X_EXECUTION_PREFERENCE,
                      after_state={"preference": body.preference})
    await db.commit()
    return {"preference": body.preference, "options": list(routing.PREFERENCES)}


@router.post("/browser-profile")
async def choose_browser_profile(
    body: BrowserProfileBody, request: Request,
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(require_x_credentials),
) -> Dict[str, Any]:
    """Pin (or unpin) the browser profile used for X browser mode.

    ``x_credentials``: this selects which signed-in X account the browser half
    acts as, which is an identity choice, not a task-authorisation one.
    """
    enforce_rate_limit(request, "write")
    from backend.app.browser import profiles as profile_store
    from backend.app.services.preferences import X_BROWSER_PROFILE_ID, set_preference as store_preference

    profile_id = (body.profile_id or "").strip()
    if profile_id:
        if not profile_store.valid_profile_id(profile_id) or profile_store.get_profile(profile_id) is None:
            raise HTTPException(status_code=404, detail={"error": "No browser profile with that id."})
    await store_preference(
        db, X_BROWSER_PROFILE_ID, profile_id,
        description="Browser profile used for X browser mode; empty means the default agent profile",
    )
    await write_audit(db, principal, "X_BROWSER_PROFILE_SET", "browser_profile", profile_id or "-")
    await db.commit()
    return {"browser_profile_id": profile_id}


@router.get("/browser-profile")
async def get_browser_profile(
    request: Request, principal: Principal = Depends(require_read), db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Which browser profile X mode would use, and why."""
    enforce_rate_limit(request, "read")
    from backend.app.integrations.x.store import select_browser_profile
    from backend.app.services.preferences import X_BROWSER_PROFILE_ID, get_preference as read_preference

    pinned = await read_preference(db, X_BROWSER_PROFILE_ID, default="")
    profile_id, reason = select_browser_profile(pinned or None)
    return {"browser_profile_id": profile_id or "", "pinned_profile_id": pinned, "reason": reason}


@router.get("/browser-status")
async def browser_status(
    request: Request,
    profile_id: Optional[str] = None,
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """Live browser state for the X page: running, URL, and X login status."""
    # This one drives a live Chrome inspection for an arbitrary profile id, so it
    # is rate limited like a read that costs something.
    enforce_rate_limit(request, "read")
    from backend.app.integrations.x.store import select_browser_profile

    target, reason = select_browser_profile(profile_id)
    if not target:
        return {"ok": False, "detail": reason}
    try:
        return await browser_adapter.session_status(target)
    except Exception:
        # Only the exception TYPE: a Playwright error string can quote page
        # content, and this value goes straight to the browser.
        return {"ok": False, "detail": "The browser session could not be inspected."}


# ======================================================================
# Activity
# ======================================================================
@router.get("/activity")
async def activity(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    method: str = "",
    status: str = "",
    config_id: str = "",
    principal: Principal = Depends(require_read), db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Every recorded X action: account, method, operation, outcome, task."""
    enforce_rate_limit(request, "read")
    rows = await list_activity(db, limit=limit, method=method, status=status, config_id=config_id)
    return {
        "activity": [public_activity(r) for r in rows],
        "filters": {"method": method, "status": status, "config_id": config_id},
        "note": (
            "An UNCERTAIN row means the request was sent but no answer came back, so the "
            "operation may or may not have applied. Verify with x_verify_action before retrying."
        ),
    }


@router.get("/catalog")
async def catalog(request: Request, principal: Principal = Depends(require_read)) -> Dict[str, Any]:
    enforce_rate_limit(request, "read")
    """What each method can do. Drives the Execution Settings table."""
    return routing.describe_catalog()


# ======================================================================
# Helpers
# ======================================================================
def _preference() -> str:
    """The environment default.

    The STORED preference is per-session and is read by ``overview`` /
    ``list_configs`` through :func:`effective_preference`, which is the value
    that actually routes a task. This helper is only the fallback used where no
    session is available.
    """
    from backend.app.core.config import settings

    value = str(getattr(settings, "X_EXECUTION_PREFERENCE", routing.MODE_AUTO) or routing.MODE_AUTO).lower()
    return value if value in routing.PREFERENCES else routing.MODE_AUTO


def _preference_help() -> Dict[str, str]:
    return {
        routing.MODE_AUTO: (
            "The agent picks per task. Work phrased as website work goes to the signed-in "
            "browser; posts, likes and follows go to the official API when an account is "
            "configured and authorized, because that returns a verifiable post id."
        ),
        routing.MODE_API_PREFERRED: (
            "The official API is tried first for anything it supports. The browser is used only "
            "when the API genuinely cannot do it."
        ),
        routing.MODE_BROWSER_PREFERRED: (
            "The signed-in browser is used for navigation and anything about the page. The API "
            "is used when a task explicitly asks for it or the operation has no UI equivalent."
        ),
    }