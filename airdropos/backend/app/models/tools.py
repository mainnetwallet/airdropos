"""Tool Registry & Dynamic Tool Development Database Models"""

from typing import Optional, List, Dict, Any
import uuid

from sqlalchemy import String, Float, Boolean, ForeignKey, Text, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


class Tool(Base):
    __tablename__ = "tools"

    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(String(50), default="1.0.0", nullable=False)
    module_path: Mapped[str] = mapped_column(String(512), nullable=False)

    input_schema: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    output_schema: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    required_permissions: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    supported_networks: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)

    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    retry_policy: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    validation_rules: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)

    health_status: Mapped[str] = mapped_column(String(50), default="UNKNOWN", nullable=False)
    development_status: Mapped[str] = mapped_column(String(50), default="PRODUCTION", nullable=False)
    # STAGING (requires review), PRODUCTION (active), QUARANTINED (auto-generated, blocked)
    test_status: Mapped[str] = mapped_column(String(50), default="NOT_RUN", nullable=False)

    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_duration_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_used_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    executions: Mapped[List["ToolExecution"]] = relationship("ToolExecution", back_populates="tool", cascade="all, delete-orphan")


class ToolExecution(Base):
    __tablename__ = "tool_executions"

    tool_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tools.id", ondelete="CASCADE"), index=True, nullable=False)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True)
    input_payload: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    output_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    tool: Mapped["Tool"] = relationship("Tool", back_populates="executions")


class GeneratedAdapter(Base):
    __tablename__ = "generated_adapters"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    target_platform: Mapped[str] = mapped_column(String(255), nullable=False)
    documentation_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    auth_requirements: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    adapter_specification: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    generated_source_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    security_review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="QUARANTINED", nullable=False)
    version_history: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
