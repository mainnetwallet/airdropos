"""Airdrop Campaign & Claim Lifecycle Database Models"""

from datetime import datetime
from typing import Optional, List, Dict, Any
import uuid

from sqlalchemy import String, Float, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


class AirdropCampaign(Base):
    __tablename__ = "airdrop_campaigns"

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(50), default="DISCOVERED", index=True, nullable=False)
    # DISCOVERED, RESEARCHING, ELIGIBILITY_UNKNOWN, ELIGIBLE, INELIGIBLE, PARTICIPATION_AVAILABLE,
    # PARTICIPATION_IN_PROGRESS, PARTICIPATION_COMPLETED, SNAPSHOT_ANNOUNCED, SNAPSHOT_COMPLETED,
    # REWARD_ANNOUNCED, CLAIM_NOT_OPEN, CLAIM_OPEN, CLAIM_SUBMITTED, CLAIM_CONFIRMED,
    # DISTRIBUTION_PENDING, REWARD_RECEIVED, EXPIRED, CANCELLED, UNVERIFIED

    network: Mapped[Optional[str]] = mapped_column(String(100), index=True, nullable=True)
    requirements: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    eligibility_rules: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reward_info: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    risk_factors: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    source_reliability: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    is_duplicate_of: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    snapshot_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship("Project", back_populates="campaigns")
    claims: Mapped[List["ClaimRecord"]] = relationship("ClaimRecord", back_populates="campaign", cascade="all, delete-orphan")
    eligibility: Mapped[List["WalletEligibility"]] = relationship(
        "WalletEligibility", back_populates="campaign"
    )


class CampaignSource(Base):
    __tablename__ = "campaign_sources"

    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("airdrop_campaigns.id", ondelete="CASCADE"), index=True, nullable=False)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), default="OFFICIAL", nullable=False)  # OFFICIAL, COMMUNITY, ONCHAIN, RESEARCH
    credibility_score: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    raw_content_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class WalletEligibility(Base):
    __tablename__ = "wallet_eligibility"

    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("airdrop_campaigns.id", ondelete="CASCADE"), index=True, nullable=False)
    wallet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("wallets.id", ondelete="CASCADE"), index=True, nullable=False)
    eligibility_status: Mapped[str] = mapped_column(String(50), default="UNKNOWN", nullable=False)  # ELIGIBLE, INELIGIBLE, UNKNOWN
    participation_status: Mapped[str] = mapped_column(String(50), default="NOT_STARTED", nullable=False)
    tasks_completed_count: Mapped[int] = mapped_column(default=0, nullable=False)
    cost_incurred_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped["AirdropCampaign"] = relationship(
        "AirdropCampaign", back_populates="eligibility"
    )
    wallet: Mapped["Wallet"] = relationship("Wallet", back_populates="eligibility")


class ClaimRecord(Base):
    __tablename__ = "claim_records"

    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("airdrop_campaigns.id", ondelete="CASCADE"), index=True, nullable=False)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    wallet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("wallets.id", ondelete="CASCADE"), index=True, nullable=False)
    network: Mapped[str] = mapped_column(String(100), nullable=False)

    claim_contract_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    token_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tx_hash: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    receipt_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    claimed_amount: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)

    claim_status: Mapped[str] = mapped_column(String(50), default="PENDING", index=True, nullable=False)  # PENDING, SUBMITTED, CONFIRMED, DISTRIBUTED, FAILED, EXPIRED
    distribution_status: Mapped[str] = mapped_column(String(50), default="PENDING", nullable=False)
    evidence_reference: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped["AirdropCampaign"] = relationship("AirdropCampaign", back_populates="claims")
    wallet: Mapped["Wallet"] = relationship("Wallet", back_populates="claims")
