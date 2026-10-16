"""Transaction API Endpoints (v1)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

from backend.app.database.session import get_db
from backend.app.models import BlockchainTransaction, TransactionReceipt
from backend.app.security.auth import (
    Principal,
    enforce_rate_limit,
    require_read,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class TransactionResponse(BaseModel):
    id: str
    task_id: Optional[str]
    wallet_id: str
    network: str
    chain_id: Optional[int]
    to_address: str
    from_address: str
    value_wei: Optional[str]
    gas_limit: Optional[str]
    gas_price_wei: Optional[str]
    data: Optional[str]
    tx_hash: Optional[str]
    status: str
    confirmation_count: int
    required_confirmations: int
    max_fee_limit_usd: Optional[float]
    actual_gas_cost_usd: Optional[float]
    usd_value_transacted: Optional[float]
    submitted_at: Optional[str]
    confirmed_at: Optional[str]
    expires_at: Optional[str]
    error_message: Optional[str]
    error_category: Optional[str]


class TransactionReceiptResponse(BaseModel):
    id: str
    transaction_id: str
    block_number: Optional[int]
    gas_used: Optional[str]
    gas_price_wei: Optional[str]
    cumulative_gas_used: Optional[str]
    effective_gas_price: Optional[str]
    status: str
    logs: Optional[List[Dict[str, Any]]]
    logs_bloom: Optional[str]
    root: Optional[str]
    timestamp: str
    contract_address: Optional[str]


@router.get("/transactions", tags=["transactions"])
async def list_transactions(
    request: Request,
    wallet_id: Optional[str] = Query(None, description="Filter by wallet ID"),
    task_id: Optional[str] = Query(None, description="Filter by task ID"),
    status: Optional[str] = Query(None, description="Filter by transaction status"),
    network: Optional[str] = Query(None, description="Filter by network"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """List blockchain transactions with optional filtering."""
    enforce_rate_limit(request, "read")
    query = select(BlockchainTransaction).order_by(BlockchainTransaction.submitted_at.desc())
    if wallet_id:
        try:
            wallet_uuid = uuid.UUID(wallet_id)
            query = query.where(BlockchainTransaction.wallet_id == wallet_uuid)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid wallet ID")
    if task_id:
        try:
            task_uuid = uuid.UUID(task_id)
            query = query.where(BlockchainTransaction.task_id == task_uuid)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid task ID")
    if status:
        query = query.where(BlockchainTransaction.status == status)
    if network:
        query = query.where(BlockchainTransaction.network == network)
    query = query.offset(offset).limit(limit)
    transactions = list((await db.execute(query)).scalars().all())
    return {
        "transactions": [TransactionResponse.from_orm(t).dict() for t in transactions],
        "count": len(transactions),
        "offset": offset,
        "limit": limit,
    }


@router.get("/transactions/{transaction_id}", tags=["transactions"])
async def get_transaction(
    transaction_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """Get a single transaction by ID."""
    enforce_rate_limit(request, "read")
    try:
        transaction_uuid = uuid.UUID(transaction_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid transaction ID")
    transaction = await db.get(BlockchainTransaction, transaction_uuid)
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return TransactionResponse.from_orm(transaction).dict()


@router.get("/transactions/{transaction_id}/receipt", tags=["transactions"])
async def get_transaction_receipt(
    transaction_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """Get the receipt for a transaction."""
    enforce_rate_limit(request, "read")
    try:
        transaction_uuid = uuid.UUID(transaction_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid transaction ID")
    transaction = await db.get(BlockchainTransaction, transaction_uuid)
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    # Get the receipt (assuming one-to-one or we take the first)
    receipt = await db.scalar(
        select(TransactionReceipt).where(TransactionReceipt.transaction_id == transaction_uuid)
    )
    if receipt is None:
        # Return empty receipt or 404? Let's return 404 for consistency.
        raise HTTPException(status_code=404, detail="Receipt not found for transaction")
    return TransactionReceiptResponse.from_orm(receipt).dict()


# Helper to convert ORM model to dict (Pydantic v1 style)
def _transaction_to_dict(tx: BlockchainTransaction) -> Dict[str, Any]:
    return {
        "id": str(tx.id),
        "task_id": str(tx.task_id) if tx.task_id else None,
        "wallet_id": str(tx.wallet_id),
        "network": tx.network,
        "chain_id": tx.chain_id,
        "to_address": tx.to_address,
        "from_address": tx.from_address,
        "value_wei": tx.value_wei,
        "gas_limit": tx.gas_limit,
        "gas_price_wei": tx.gas_price_wei,
        "data": tx.data,
        "tx_hash": tx.tx_hash,
        "status": tx.status,
        "confirmation_count": tx.confirmation_count,
        "required_confirmations": tx.required_confirmations,
        "max_fee_limit_usd": tx.max_fee_limit_usd,
        "actual_gas_cost_usd": tx.actual_gas_cost_usd,
        "usd_value_transacted": tx.usd_value_transacted,
        "submitted_at": tx.submitted_at.isoformat() if tx.submitted_at else None,
        "confirmed_at": tx.confirmed_at.isoformat() if tx.confirmed_at else None,
        "expires_at": tx.expires_at.isoformat() if tx.expires_at else None,
        "error_message": tx.error_message,
        "error_category": tx.error_category,
    }


def _receipt_to_dict(rx: TransactionReceipt) -> Dict[str, Any]:
    return {
        "id": str(rx.id),
        "transaction_id": str(rx.transaction_id),
        "block_number": rx.block_number,
        "gas_used": rx.gas_used,
        "gas_price_wei": rx.gas_price_wei,
        "cumulative_gas_used": rx.cumulative_gas_used,
        "effective_gas_price": rx.effective_gas_price,
        "status": rx.status,
        "logs": rx.logs,
        "logs_bloom": rx.logs_bloom,
        "root": rx.root,
        "timestamp": rx.timestamp.isoformat(),
        "contract_address": rx.contract_address,
    }


# Attach the helpers to the Response models for convenience
TransactionResponse.from_orm = lambda obj: TransactionResponse(**_transaction_to_dict(obj))
TransactionReceiptResponse.from_orm = lambda obj: TransactionReceiptResponse(**_receipt_to_dict(obj))