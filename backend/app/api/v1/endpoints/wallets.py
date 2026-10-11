"""Wallet Management API (v1).

Read endpoints require `wallet_read`. Every mutating endpoint requires the
separate `wallet_write` scope, so a read-only observer session can inspect
addresses without being able to import or delete key material.

Key material is accepted only on import, is encrypted before it reaches the
database, and is never returned by any endpoint. Responses expose
`has_key_material` and `can_sign` as capability flags instead.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database.session import get_db
from backend.app.security.auth import (
    Principal,
    enforce_rate_limit,
    require_wallet_read,
    require_wallet_write,
    write_audit,
)
from backend.app.wallets.service import (
    WalletError,
    WalletService,
    WalletValidationError,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ----------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------
class WalletResponse(BaseModel):
    id: str
    label: str
    address: str
    chain_type: str
    derivation_path: Optional[str] = None
    supported_networks: List[str] = []
    is_active: bool
    is_locked: bool
    has_key_material: bool
    can_sign: bool
    total_spent_usd: float
    notes: Optional[str] = None
    created_at: Optional[str] = None


class WalletImportRequest(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    secret: str = Field(min_length=1, description="Private key (0x…) or BIP-39 mnemonic")
    chain_type: str = Field(default="EVM", max_length=50)
    derivation_path: Optional[str] = Field(default=None, max_length=100)
    supported_networks: Optional[List[str]] = None
    notes: Optional[str] = None


class WalletWatchRequest(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    address: str = Field(min_length=42, max_length=42)
    chain_type: str = Field(default="EVM", max_length=50)
    supported_networks: Optional[List[str]] = None
    notes: Optional[str] = None


class WalletUpdateRequest(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=255)
    notes: Optional[str] = None
    supported_networks: Optional[List[str]] = None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
async def _load(db: AsyncSession, wallet_id: str):
    """Resolve a wallet id or 404. Parses the UUID so a bad id is a 400."""
    try:
        wallet_uuid = uuid.UUID(wallet_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid wallet ID")

    from backend.app.models import Wallet

    row = await db.get(Wallet, wallet_uuid)
    if row is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return row


def _service(db: AsyncSession) -> WalletService:
    return WalletService(db)


# ----------------------------------------------------------------------
# Read
# ----------------------------------------------------------------------
@router.get("/wallets", tags=["wallets"])
async def list_wallets(
    request: Request,
    is_active: Optional[bool] = Query(default=None),
    chain_type: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_wallet_read),
) -> Dict[str, Any]:
    """List wallets. Never returns key material."""
    enforce_rate_limit(request, "read")
    service = _service(db)
    rows = await service.list(
        is_active=is_active, chain_type=chain_type, limit=limit, offset=offset
    )
    return {
        "wallets": [service.to_dict(r) for r in rows],
        "count": len(rows),
        "offset": offset,
        "limit": limit,
    }


@router.get("/wallets/{wallet_id}", tags=["wallets"])
async def get_wallet(
    wallet_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_wallet_read),
) -> Dict[str, Any]:
    """Get a single wallet by ID."""
    enforce_rate_limit(request, "read")
    row = await _load(db, wallet_id)
    return _service(db).to_dict(row)


# ----------------------------------------------------------------------
# Write
# ----------------------------------------------------------------------
@router.post("/wallets/import", status_code=201, tags=["wallets"])
async def import_wallet(
    payload: WalletImportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_wallet_write),
) -> Dict[str, Any]:
    """Import a wallet from a private key or mnemonic.

    The secret is AES-256-GCM encrypted before it is stored and is never
    echoed back. Audit records contain the derived address, never the secret.
    """
    enforce_rate_limit(request, "write")
    service = _service(db)
    try:
        row = await service.import_wallet(
            label=payload.label,
            secret=payload.secret,
            chain_type=payload.chain_type,
            derivation_path=payload.derivation_path,
            supported_networks=payload.supported_networks,
            notes=payload.notes,
        )
    except WalletValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except WalletError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    await write_audit(
        db,
        principal,
        "WALLET_IMPORTED",
        "wallet",
        str(row.id),
        after_state={"label": row.label, "address": row.address},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return service.to_dict(row)


@router.post("/wallets/watch", status_code=201, tags=["wallets"])
async def watch_address(
    payload: WalletWatchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_wallet_write),
) -> Dict[str, Any]:
    """Register a public address held elsewhere, with no key material."""
    enforce_rate_limit(request, "write")
    service = _service(db)
    try:
        row = await service.watch_address(
            label=payload.label,
            address=payload.address,
            chain_type=payload.chain_type,
            supported_networks=payload.supported_networks,
            notes=payload.notes,
        )
    except WalletValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except WalletError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    await write_audit(
        db,
        principal,
        "WALLET_WATCH_REGISTERED",
        "wallet",
        str(row.id),
        after_state={"label": row.label, "address": row.address},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return service.to_dict(row)


@router.patch("/wallets/{wallet_id}", tags=["wallets"])
async def update_wallet(
    wallet_id: str,
    payload: WalletUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_wallet_write),
) -> Dict[str, Any]:
    """Update wallet metadata. Key material and address are immutable here."""
    enforce_rate_limit(request, "write")
    row = await _load(db, wallet_id)
    service = _service(db)
    before = await service.update(
        row,
        label=payload.label,
        notes=payload.notes,
        supported_networks=payload.supported_networks,
    )

    await write_audit(
        db,
        principal,
        "WALLET_UPDATED",
        "wallet",
        wallet_id,
        before_state=before,
        after_state=service.to_dict(row),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return service.to_dict(row)


@router.post("/wallets/{wallet_id}/lock", tags=["wallets"])
async def lock_wallet(
    wallet_id: str,
    request: Request,
    locked: bool = Query(default=True, description="true to lock, false to unlock"),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_wallet_write),
) -> Dict[str, Any]:
    """Lock or unlock a wallet.

    Locking blocks signing without deleting the encrypted key, so it is
    reversible. It does not engage the platform-wide emergency stop.
    """
    enforce_rate_limit(request, "write")
    row = await _load(db, wallet_id)
    service = _service(db)
    await service.set_locked(row, locked)

    await write_audit(
        db,
        principal,
        "WALLET_LOCKED" if locked else "WALLET_UNLOCKED",
        "wallet",
        wallet_id,
        after_state={"is_locked": row.is_locked, "can_sign": service.can_sign(row)},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return service.to_dict(row)


@router.post("/wallets/{wallet_id}/deactivate", tags=["wallets"])
async def deactivate_wallet(
    wallet_id: str,
    request: Request,
    active: bool = Query(default=False, description="true to reactivate"),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_wallet_write),
) -> Dict[str, Any]:
    """Deactivate or reactivate a wallet without deleting its history."""
    enforce_rate_limit(request, "write")
    row = await _load(db, wallet_id)
    service = _service(db)
    await service.set_active(row, active)

    await write_audit(
        db,
        principal,
        "WALLET_REACTIVATED" if active else "WALLET_DEACTIVATED",
        "wallet",
        wallet_id,
        after_state={"is_active": row.is_active, "can_sign": service.can_sign(row)},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return service.to_dict(row)


@router.delete("/wallets/{wallet_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["wallets"])
async def delete_wallet(
    wallet_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_wallet_write),
) -> None:
    """Permanently delete a wallet and its encrypted key.

    Transactions, claims, and eligibility rows cascade on delete. This is the
    only endpoint that destroys evidence, so it is deliberately not reversible.
    """
    enforce_rate_limit(request, "write")
    row = await _load(db, wallet_id)
    service = _service(db)
    snapshot = service.to_dict(row)

    await write_audit(
        db,
        principal,
        "WALLET_DELETED",
        "wallet",
        wallet_id,
        before_state=snapshot,
        ip_address=request.client.host if request.client else None,
    )
    await service.delete(row)
    await db.commit()
    return None