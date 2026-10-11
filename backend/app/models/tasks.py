"""Task Engine Database Models"""

from datetime import datetime
from typing import Optional, List, Dict, Any
import uuid

from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, ForeignKey, Text
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)  # AIR-2026-000001
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="CREATED", index=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=5, index=True, nullable=False)  # 1=highest, 10=lowest

    goal_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("goals.id", ondelete="SET NULL"), index=True, nullable=True)
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), index=True, nullable=True)
    parent_task_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True)

    network: Mapped[Optional[str]] = mapped_column(String(100), index=True, nullable=True)
    assigned_wallet_ids: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)  # List of Wallet UUIDs

    planned_actions: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSONB, default=list)
    completed_actions: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSONB, default=list)
    failed_actions: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSONB, default=list)
    dependencies: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)  # List of task_ids

    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # 0.0 to 1.0
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    next_planned_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_details: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    execution_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    next_execution_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verification_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    goal: Mapped[Optional["Goal"]] = relationship("Goal", back_populates="tasks")
    project: Mapped[Optional["Project"]] = relationship("Project", back_populates="tasks")
    parent_task: Mapped[Optional["Task"]] = relationship("Task", remote_side="Task.id", backref="subtasks")
    executions: Mapped[List["TaskExecution"]] = relationship("TaskExecution", back_populates="task", cascade="all, delete-orphan")
    checkpoints: Mapped[List["TaskCheckpoint"]] = relationship("TaskCheckpoint", back_populates="task", cascade="all, delete-orphan")
    events: Mapped[List["TaskEvent"]] = relationship("TaskEvent", back_populates="task", cascade="all, delete-orphan")
    evidence: Mapped[List["TaskEvidence"]] = relationship("TaskEvidence", back_populates="task", cascade="all, delete-orphan")
    transactions: Mapped[List["BlockchainTransaction"]] = relationship("BlockchainTransaction", back_populates="task")


class TaskExecution(Base):
    __tablename__ = "task_executions"

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    plan: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    observation: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(50), default="RUNNING", nullable=False)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    task: Mapped["Task"] = relationship("Task", back_populates="executions")


class TaskCheckpoint(Base):
    __tablename__ = "task_checkpoints"

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    checkpoint_name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    iteration: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    task: Mapped["Task"] = relationship("Task", back_populates="checkpoints")


class TaskEvent(Base):
    __tablename__ = "task_events"

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="INFO", nullable=False)
    payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)

    task: Mapped["Task"] = relationship("Task", back_populates="events")


class TaskEvidence(Base):
    __tablename__ = "task_evidence"

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)  # SCREENSHOT, HASH, RECEIPT, JSON, TEXT
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Truncated/plaintext; full in filesystem
    uri_reference: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Optional link to the transaction this evidence proves. Nullable because
    # evidence also covers screenshots and JSON snapshots that have no chain tie.
    blockchain_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("blockchain_transactions.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    task: Mapped["Task"] = relationship("Task", back_populates="evidence")
    blockchain_transaction: Mapped[Optional["BlockchainTransaction"]] = relationship(
        "BlockchainTransaction", back_populates="evidence"
    )
