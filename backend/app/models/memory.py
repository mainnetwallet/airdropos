"""Persistent Memory, Knowledge Graph & Learning Database Models"""

from datetime import datetime
from typing import Optional, List, Dict, Any
import uuid

from sqlalchemy import String, Float, Boolean, DateTime, ForeignKey, Text, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


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
