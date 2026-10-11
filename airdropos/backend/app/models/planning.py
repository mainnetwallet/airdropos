"""Planning models: persistent execution plans, revisions, and execution leases.

`PlanningRun` (in models/scheduler.py) records *that* a plan was produced. These
tables record the plan itself, its revision history, and the lease that prevents
two workers from executing the same plan step concurrently.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, ForeignKey, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


class ExecutionPlan(Base):
    """A persisted, multi-step plan for a task.

    One task may have many plan revisions; `current_revision` points at the
    active one. Steps are ordered and may declare dependencies by index.
    """

    __tablename__ = "execution_plans"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)

    objective: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[List[Dict[str, Any]]] = mapped_column(JSONB, default=list)
    constraints: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    estimated_duration_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trigger_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    llm_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("task_id", "revision", name="uq_execution_plan_task_revision"),
    )

    revisions: Mapped[List["PlanRevision"]] = relationship(
        "PlanRevision", back_populates="plan", cascade="all, delete-orphan"
    )


class PlanRevision(Base):
    """An immutable diff entry describing why a plan changed.

    Revisions are append-only so replanning history is auditable. A revision
    never mutates an earlier plan; it supersedes it.
    """

    __tablename__ = "plan_revisions"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("execution_plans.id", ondelete="CASCADE"), index=True, nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )

    from_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    to_revision: Mapped[int] = mapped_column(Integer, nullable=False)

    trigger_category: Mapped[str] = mapped_column(String(100), default="INITIAL", nullable=False)
    # INITIAL, FAILURE, EXTERNAL_CHANGE, DEADLINE_PRESSURE, BUDGET, VERIFICATION_FAILURE,
    # DEPENDENCY_UNBLOCKED, OPERATOR_REQUEST

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    added_steps: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    removed_steps: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    modified_steps: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    preserved_irreversible_step_ids: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)

    plan: Mapped["ExecutionPlan"] = relationship("ExecutionPlan", back_populates="revisions")


class ExecutionLease(Base):
    """A time-bounded claim on a task by one worker.

    Prevents duplicate execution after a crash: a lease whose `expires_at` has
    passed is stale and may be broken by another worker. Combined with the
    row-level lock in TaskManager.claim_next_task, this covers both the
    "two live workers" and "one dead worker" cases.
    """

    __tablename__ = "execution_leases"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    worker_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)

    lease_token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    renewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    last_iteration: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    broken_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # At most one live (unreleased) lease per task.
        UniqueConstraint("task_id", name="uq_execution_lease_task"),
    )
