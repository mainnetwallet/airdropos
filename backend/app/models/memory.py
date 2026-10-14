"""Persistent Memory, Knowledge Graph & Learning Database Models"""

from datetime import datetime
from typing import Optional, List, Dict, Any
import uuid

from sqlalchemy import String, Float, Boolean, DateTime, ForeignKey, Text, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


# Memory scopes. They are a strict hierarchy, not labels: a record written at TASK
# scope is readable only by that task, a CAMPAIGN-scope record only by tasks linked to
# that campaign, and only GLOBAL records are offered to any task as general knowledge.
# Without this, one task's private history would be recalled into another's prompt.
SCOPE_GLOBAL = "global"
SCOPE_CAMPAIGN = "campaign"
SCOPE_TASK = "task"

MEMORY_SCOPES = (SCOPE_GLOBAL, SCOPE_CAMPAIGN, SCOPE_TASK)


class MemoryRecord(Base):
    __tablename__ = "memory_records"

    category: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    # VERIFIED_FACT, USER_PREFERENCE, PROJECT_KNOWLEDGE, CAMPAIGN_REQUIREMENT,
    # SUCCESSFUL_WORKFLOW, FAILURE_PATTERN, TOOL_RELIABILITY, SOURCE_RELIABILITY,
    # EXECUTION_OUTCOME, UNRESOLVED_BLOCKER

    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    related_entities: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    is_inference: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ------------------------------------------------------------------
    # Context hierarchy. Defaults to the historical behaviour (an unscoped
    # record is general knowledge) so rows written before this existed keep
    # the meaning they had.
    # ------------------------------------------------------------------
    scope: Mapped[str] = mapped_column(String(20), default=SCOPE_GLOBAL, index=True, nullable=False)
    # Populated only when scope is TASK. Cascade: a deleted task's private history
    # goes with it rather than lingering as orphaned context another task could reach.
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"),
        index=True, nullable=True,
    )
    # Populated only when scope is CAMPAIGN. SET NULL: campaign facts outlive the
    # task that discovered them, and lose their owner when the campaign is deleted.
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("airdrop_campaigns.id", ondelete="SET NULL"),
        index=True, nullable=True,
    )

    access_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    review_policy: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)


class KnowledgeRelationship(Base):
    __tablename__ = "knowledge_relationships"

    source_entity_type: Mapped[str] = mapped_column(String(50), nullable=False)  # PROJECT, WALLET, TASK, CAMPAIGN, TOOL, SOURCE
    source_entity_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    target_entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_entity_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(100), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    properties: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)


class LearningRecord(Base):
    __tablename__ = "learning_records"

    topic: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    pattern_type: Mapped[str] = mapped_column(String(100), nullable=False)  # SUCCESS, FAILURE, TOOL_PERFORMANCE, DURATION
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    metrics: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    recommendation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    is_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ImprovementProposal(Base):
    __tablename__ = "improvement_proposals"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    problem_statement: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    proposed_change: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    affected_files: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    required_tests: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    security_review_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    status: Mapped[str] = mapped_column(String(50), default="PROPOSED", index=True, nullable=False)
    # PROPOSED, BRANCH_CREATED, VALIDATION_PENDING, VALIDATED, DEPLOYED, ROLLED_BACK, REJECTED
    branch_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    rollback_plan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version_history: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
