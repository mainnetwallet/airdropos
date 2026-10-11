"""Goals and Strategy Database Models"""

from datetime import datetime
from typing import Optional, List, Dict, Any
import uuid

from sqlalchemy import String, Boolean, DateTime, Float, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


class Goal(Base):
    __tablename__ = "goals"

    goal_code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE", index=True, nullable=False)  # ACTIVE, PAUSED, COMPLETED, CANCELLED
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    success_criteria: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    scheduling_policy: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    resource_limits: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    current_blockers: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    next_planned_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    tasks: Mapped[List["Task"]] = relationship("Task", back_populates="goal")
