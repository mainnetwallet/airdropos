"""Campaign API Endpoints (v1)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database.session import get_db
from backend.app.models import AirdropCampaign, WalletEligibility
from backend.app.security.auth import (
    Principal,
    enforce_rate_limit,
    require_read,
    require_system_admin,
    require_wallet_read,
    write_audit,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class CampaignResponse(BaseModel):
    id: str
    project_id: str
    title: str
    lifecycle_status: str
    network: Optional[str]
    requirements: Dict[str, Any]
    eligibility_rules: Dict[str, Any]
    estimated_cost_usd: float
    reward_info: Dict[str, Any]
    risk_factors: List[str]
    source_reliability: float
    is_duplicate_of: Optional[str]
    start_date: Optional[str]
    end_date: Optional[str]
    snapshot_date: Optional[str]
    claim_deadline: Optional[str]
    last_checked_at: Optional[str]


@router.get("/campaigns", tags=["campaigns"])
async def list_campaigns(
    request: Request,
    project_id: Optional[str] = Query(None, description="Filter by project ID"),
    status: Optional[str] = Query(None, description="Filter by lifecycle status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """List airdrop campaigns with optional filtering."""
    enforce_rate_limit(request, "read")
    query = select(AirdropCampaign).order_by(AirdropCampaign.created_at.desc())
    if project_id:
        try:
            project_uuid = uuid.UUID(project_id)
            query = query.where(AirdropCampaign.project_id == project_uuid)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid project ID")
    if status:
        query = query.where(AirdropCampaign.lifecycle_status == status)
    query = query.offset(offset).limit(limit)
    campaigns = list((await db.execute(query)).scalars().all())
    return {
        "campaigns": [CampaignResponse.from_orm(c).dict() for c in campaigns],
        "count": len(campaigns),
        "offset": offset,
        "limit": limit,
    }


@router.get("/campaigns/{campaign_id}", tags=["campaigns"])
async def get_campaign(
    campaign_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_read),
) -> Dict[str, Any]:
    """Get a single campaign by ID."""
    enforce_rate_limit(request, "read")
    try:
        campaign_uuid = uuid.UUID(campaign_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid campaign ID")
    campaign = await db.get(AirdropCampaign, campaign_uuid)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignResponse.from_orm(campaign).dict()


@router.post("/campaigns/{campaign_id}/refresh", tags=["campaigns"])
async def refresh_campaign(
    campaign_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_system_admin),
) -> Dict[str, Any]:
    """Trigger a refresh of a campaign's data (placeholder)."""
    enforce_rate_limit(request, "write")
    try:
        campaign_uuid = uuid.UUID(campaign_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid campaign ID")
    campaign = await db.get(AirdropCampaign, campaign_uuid)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    # In a real implementation, this would trigger discovery/verification for this campaign
    campaign.last_checked_at = datetime.now(timezone.utc)
    await write_audit(
        db,
        principal,
        "CAMPAIGN_REFRESH",
        "campaign",
        campaign_id,
        after_state={"refreshed_at": campaign.last_checked_at.isoformat()},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"id": campaign_id, "refreshed_at": campaign.last_checked_at.isoformat()}


@router.get("/campaigns/{campaign_id}/eligibility", tags=["campaigns"])
async def get_campaign_eligibility(
    campaign_id: str,
    request: Request,
    wallet_id: str = Query(..., description="Wallet ID to check eligibility for"),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_wallet_read),
) -> Dict[str, Any]:
    """Get eligibility status for a specific wallet and campaign."""
    enforce_rate_limit(request, "read")
    try:
        campaign_uuid = uuid.UUID(campaign_id)
        wallet_uuid = uuid.UUID(wallet_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ID format")
    eligibility = await db.scalar(
        select(WalletEligibility).where(
            WalletEligibility.campaign_id == campaign_uuid,
            WalletEligibility.wallet_id == wallet_uuid,
        )
    )
    if eligibility is None:
        # Return unknown eligibility if no record exists
        return {
            "campaign_id": campaign_id,
            "wallet_id": wallet_id,
            "eligibility_status": "UNKNOWN",
            "participation_status": "NOT_STARTED",
            "tasks_completed_count": 0,
            "cost_incurred_usd": 0.0,
            "last_verified_at": None,
        }
    return {
        "campaign_id": campaign_id,
        "wallet_id": wallet_id,
        "eligibility_status": eligibility.eligibility_status,
        "participation_status": eligibility.participation_status,
        "tasks_completed_count": eligibility.tasks_completed_count,
        "cost_incurred_usd": eligibility.cost_incurred_usd,
        "last_verified_at": (
            eligibility.last_verified_at.isoformat()
            if eligibility.last_verified_at
            else None
        ),
    }


# Helper to convert ORM model to dict ( Pydantic v1 style )
def _campaign_to_dict(campaign: AirdropCampaign) -> Dict[str, Any]:
    return {
        "id": str(campaign.id),
        "project_id": str(campaign.project_id),
        "title": campaign.title,
        "lifecycle_status": campaign.lifecycle_status,
        "network": campaign.network,
        "requirements": campaign.requirements or {},
        "eligibility_rules": campaign.eligibility_rules or {},
        "estimated_cost_usd": campaign.estimated_cost_usd,
        "reward_info": campaign.reward_info or {},
        "risk_factors": campaign.risk_factors or [],
        "source_reliability": campaign.source_reliability,
        "is_duplicate_of": (
            str(campaign.is_duplicate_of) if campaign.is_duplicate_of else None
        ),
        "start_date": (
            campaign.start_date.isoformat() if campaign.start_date else None
        ),
        "end_date": campaign.end_date.isoformat() if campaign.end_date else None,
        "snapshot_date": (
            campaign.snapshot_date.isoformat() if campaign.snapshot_date else None
        ),
        "claim_deadline": (
            campaign.claim_deadline.isoformat() if campaign.claim_deadline else None
        ),
        "last_checked_at": (
            campaign.last_checked_at.isoformat() if campaign.last_checked_at else None
        ),
    }


# Attach the helper to the Response model for convenience
CampaignResponse.from_orm = lambda obj: CampaignResponse(**_campaign_to_dict(obj))