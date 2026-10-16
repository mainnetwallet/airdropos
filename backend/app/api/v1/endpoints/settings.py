"""Settings API Endpoints (v1).

Secret settings are never returned to non-admin callers, and never returned in
cleartext even to admins: the API exposes the fact that a secret is configured,
not its value. Writing a secret stores it; reading it back is not possible.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database.session import get_db
from backend.app.models import SystemSetting
from backend.app.security.auth import (
    Principal,
    enforce_rate_limit,
    require_read,
    require_system_admin,
    write_audit,
)

logger = logging.getLogger(__name__)
router = APIRouter()

REDACTED = "[REDACTED]"


class SystemSettingResponse(BaseModel):
    key: str
    value: Any
    is_secret: bool
    description: Optional[str] = None
    updated_at: Optional[str] = None


class SystemSettingUpdate(BaseModel):
    value: Any = None
    description: Optional[str] = None
    is_secret: Optional[bool] = None


def _setting_to_dict(setting: SystemSetting, principal: Principal) -> Dict[str, Any]:
    """Serialize a setting, redacting secret values unless the caller is an admin.

    Even an administrator never receives a stored secret back. Configuration is
    write-only by design: a leaked read path here would leak credentials.
    """
    is_secret = bool(setting.is_secret)

    return {
        "key": setting.key,
        "value": REDACTED if is_secret else setting.value,
        "is_secret": is_secret,
        "description": setting.description,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


@router.get("/settings", tags=["settings"])
async def list_settings(
    request: Request,
    key_prefix: Optional[str] = Query(None, description="Filter by key prefix"),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """List system settings. Secret values are redacted for every caller."""
    enforce_rate_limit(request, "read")
    query = select(SystemSetting).order_by(SystemSetting.key)
    if key_prefix:
        query = query.where(SystemSetting.key.like(f"{key_prefix}%"))
    rows = list((await db.execute(query)).scalars().all())
    return {
        "settings": [_setting_to_dict(s, principal) for s in rows],
        "count": len(rows),
        "secrets_redacted": sum(1 for s in rows if s.is_secret),
    }


@router.get("/settings/{setting_key}", tags=["settings"])
async def get_setting(
    setting_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """Get a single setting by key. Secret values are redacted."""
    enforce_rate_limit(request, "read")
    setting = await db.scalar(select(SystemSetting).where(SystemSetting.key == setting_key))
    if setting is None:
        raise HTTPException(status_code=404, detail="Setting not found")
    return _setting_to_dict(setting, principal)


@router.patch("/settings/{setting_key}", tags=["settings"])
async def update_setting(
    setting_key: str,
    payload: SystemSettingUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_system_admin),
) -> Dict[str, Any]:
    """Update a system setting. Requires the `system_admin` scope."""
    enforce_rate_limit(request, "write")
    setting = await db.scalar(select(SystemSetting).where(SystemSetting.key == setting_key))
    if setting is None:
        raise HTTPException(status_code=404, detail="Setting not found")

    before = {"value": REDACTED if setting.is_secret else setting.value,
              "is_secret": setting.is_secret}

    if payload.value is not None:
        setting.value = payload.value
    if payload.description is not None:
        setting.description = payload.description
    if payload.is_secret is not None:
        setting.is_secret = payload.is_secret

    await write_audit(
        db,
        principal,
        "SETTING_UPDATE",
        "setting",
        setting_key,
        before_state=before,
        after_state={
            "value": REDACTED if setting.is_secret else setting.value,
            "is_secret": setting.is_secret,
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(setting)
    return _setting_to_dict(setting, principal)


class SystemSettingCreate(SystemSettingUpdate):
    key: str = Field(min_length=1, max_length=128)


@router.post("/settings", status_code=201, tags=["settings"])
async def create_setting(
    payload: SystemSettingCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_system_admin),
) -> Dict[str, Any]:
    """Create a system setting. Requires the `system_admin` scope."""
    enforce_rate_limit(request, "write")
    key = payload.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="A non-empty key is required")

    existing = await db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Setting already exists")

    setting = SystemSetting(
        key=key,
        value=payload.value,
        description=payload.description,
        is_secret=bool(payload.is_secret),
    )
    db.add(setting)

    await write_audit(
        db,
        principal,
        "SETTING_CREATE",
        "setting",
        key,
        after_state={"is_secret": setting.is_secret},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(setting)
    return _setting_to_dict(setting, principal)