"""Audit Log API Endpoints (v1)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

from backend.app.database.session import get_db
from backend.app.models import AuditLog
from backend.app.security.auth import (
    Principal,
    enforce_rate_limit,
    require_audit_read,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class AuditLogResponse(BaseModel):
    id: str
    actor: str
    action: str
    entity_type: str
    entity_id: str
    before_state: Optional[Dict[str, Any]]
    after_state: Optional[Dict[str, Any]]
    ip_address: Optional[str]
    created_at: str


@router.get("/audit", tags=["audit"])
async def list_audit_logs(
    request: Request,
    entity_type: Optional[str] = Query(None, description="Filter by entity type"),
    entity_id: Optional[str] = Query(None, description="Filter by entity ID"),
    action: Optional[str] = Query(None, description="Filter by action"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_audit_read),
) -> Dict[str, Any]:
    """List audit logs with optional filtering."""
    enforce_rate_limit(request, "read")
    query = select(AuditLog).order_by(desc(AuditLog.created_at))
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    if action:
        query = query.where(AuditLog.action == action)
    query = query.offset(offset).limit(limit)
    logs = list((await db.execute(query)).scalars().all())
    return {
        "audit_logs": [
            AuditLogResponse.from_orm(log).dict() for log in logs
        ],
        "count": len(logs),
        "offset": offset,
        "limit": limit,
    }


@router.get("/audit/{audit_id}", tags=["audit"])
async def get_audit_log(
    audit_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_audit_read),
) -> Dict[str, Any]:
    """Get a single audit log by ID."""
    enforce_rate_limit(request, "read")
    try:
        audit_uuid = uuid.UUID(audit_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid audit log ID")
    log = await db.get(AuditLog, audit_uuid)
    if log is None:
        raise HTTPException(status_code=404, detail="Audit log not found")
    return AuditLogResponse.from_orm(log).dict()


# Helper to convert ORM model to dict (Pydantic v1 style)
def _audit_to_dict(log: AuditLog) -> Dict[str, Any]:
    return {
        "id": str(log.id),
        "actor": log.actor,
        "action": log.action,
        "entity_type": log.entity_type,
        "entity_id": log.entity_id,
        "before_state": log.before_state,
        "after_state": log.after_state,
        "ip_address": log.ip_address,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


# Attach the helper to the Response model for convenience
AuditLogResponse.from_orm = lambda obj: AuditLogResponse(**_audit_to_dict(obj))