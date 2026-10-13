"""Scheduler, Notification, Integration & Diagnostics Database Models"""

from datetime import datetime
from typing import Optional, List, Dict, Any
import uuid

from sqlalchemy import Index, String, Float, Boolean, DateTime, ForeignKey, Text, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database.session import Base


class AutomationSchedule(Base):
    __tablename__ = "automation_schedules"

    job_name: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    job_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    # DISCOVERY, CAMPAIGN_MONITOR, ELIGIBILITY_RECHECK, CLAIM_MONITOR, TX_RECEIPT_CHECK,
    # BROWSER_HEALTH, RPC_HEALTH, RETRY_SCHEDULER, DAILY_REPORT, DEADLINE_ALERT,
    # MEMORY_MAINTENANCE, LEARNING_ANALYSIS, GOAL_EXECUTION

    trigger_type: Mapped[str] = mapped_column(String(50), default="INTERVAL", nullable=False)  # INTERVAL, CRON
    interval_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cron_expression: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    total_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ScheduledRun(Base):
    __tablename__ = "scheduled_runs"

    schedule_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("automation_schedules.id", ondelete="CASCADE"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="RUNNING", nullable=False)
    result_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class Notification(Base):
    __tablename__ = "notifications"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="INFO", nullable=False)
    related_task_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    notification_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("notifications.id", ondelete="CASCADE"), index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(50), nullable=False)  # TELEGRAM, DASHBOARD, DISCORD
    status: Mapped[str] = mapped_column(String(50), default="PENDING", nullable=False)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class IntegrationAccount(Base):
    __tablename__ = "integration_accounts"

    platform: Mapped[str] = mapped_column(String(50), index=True, nullable=False)  # TELEGRAM, DISCORD, TWITTER
    account_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_credentials: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    capabilities: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    rate_limit_info: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class IntegrationHealth(Base):
    __tablename__ = "integration_health"

    subsystem: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="UNKNOWN", nullable=False)
    detail: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DiagnosticRecord(Base):
    __tablename__ = "diagnostic_records"

    subsystem: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(20), default="INFO", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context_data: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    actor: Mapped[str] = mapped_column(String(128), index=True, nullable=False)  # user id, "system", or "agent"
    action: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    before_state: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    after_state: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    value: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class WorldStateObservation(Base):
    __tablename__ = "world_state_observations"

    entity_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    attribute: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_value: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    previous_value: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(50), default="UNVERIFIED", nullable=False)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceClaim(Base):
    __tablename__ = "source_claims"

    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), default="OFFICIAL", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    verification_classification: Mapped[str] = mapped_column(String(50), default="UNVERIFIED", nullable=False)
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    supporting_evidence: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    conflicting_evidence: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    last_recheck_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ChangeObservation(Base):
    __tablename__ = "change_observations"

    entity_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    change_type: Mapped[str] = mapped_column(String(100), nullable=False)
    old_value: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    new_value: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    impact: Mapped[str] = mapped_column(String(50), default="LOW", nullable=False)  # LOW, MEDIUM, HIGH, CRITICAL
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FailureRecord(Base):
    __tablename__ = "failure_records"

    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True)
    execution_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    error_category: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    # NETWORK_TIMEOUT, RATE_LIMITING, AUTH_FAILURE, BROWSER_SESSION_EXPIRED, INVALID_INPUT,
    # MISSING_PERMISSION, UNSUPPORTED_OPERATION, RPC_FAILURE, TX_REJECTED, TX_REVERTED,
    # INSUFFICIENT_BALANCE, NONCE_CONFLICT, CAMPAIGN_EXPIRED, ELIGIBILITY_FAILURE,
    # TOOL_FAILURE, DATABASE_FAILURE, EXTERNAL_SERVICE_OUTAGE, UNKNOWN

    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    diagnostics: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    is_retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recovery_strategy: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    final_outcome: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)


class ResourceUsage(Base):
    __tablename__ = "resource_usage"

    resource_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)  # GAS, API_CALL, LLM_TOKEN, BROWSER_SESSION
    amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)


class PlanningRun(Base):
    __tablename__ = "planning_runs"

    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=True)
    goal_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("goals.id", ondelete="CASCADE"), index=True, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    plan: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trigger_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AgentConversation(Base):
    """A durable agent conversation.

    Historically this carried the dashboard's general agent console and was never written to.
    It now also backs the Agent Inbox: one conversation per task, scoped to the owning operator,
    so a reply can only ever land on the task whose conversation it was sent from.
    """

    __tablename__ = "agent_conversations"

    channel: Mapped[str] = mapped_column(String(50), nullable=False)  # DASHBOARD, TELEGRAM, TASK_INBOX
    channel_conversation_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    context_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Agent Inbox scoping -------------------------------------------------
    # The task this conversation belongs to. NULL for the general console conversation.
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # The authenticated operator who owns the conversation. A reply is authorised against
    # this, never against a task id supplied by the client.
    owner_email: Mapped[Optional[str]] = mapped_column(String(255), index=True, nullable=True)
    # OPEN, ARCHIVED
    status: Mapped[str] = mapped_column(String(50), default="OPEN", index=True, nullable=False)
    # True while an unresolved intervention request is waiting for this conversation's owner.
    requires_reply: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    # Monotonic per-conversation counter giving every message a stable total order.
    last_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # A unique INDEX rather than a UniqueConstraint: on SQLite a constraint becomes an
    # anonymous auto-index that introspection cannot see, which makes it impossible to drop
    # in a downgrade. The migration creates an index too, so both paths agree.
    __table_args__ = (
        Index("uq_agent_conversation_channel_key", "channel", "channel_conversation_id", unique=True),
    )

    messages: Mapped[List["ConversationMessage"]] = relationship("ConversationMessage", back_populates="conversation", cascade="all, delete-orphan")


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)  # user, assistant, system, tool
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, default=dict)
    tokens_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # --- Agent Inbox scoping -------------------------------------------------
    # Denormalised from the conversation so a message can never be read with the wrong task.
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # AGENT, USER, SYSTEM, INTERVENTION_REQUEST, RESUMPTION, VERIFICATION, FOLLOW_UP
    kind: Mapped[str] = mapped_column(String(50), default="AGENT", index=True, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Client-supplied idempotency token. Unique per conversation so a double-submit cannot
    # persist two copies of the same reply or launch two resume attempts.
    client_token: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    intervention_request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)

    conversation: Mapped["AgentConversation"] = relationship("AgentConversation", back_populates="messages")

    # Client-supplied idempotency token. Unique per conversation so a double-submit cannot
    # persist two copies of the same reply or launch two resume attempts. A unique INDEX,
    # for the same reason as the conversation key above.
    __table_args__ = (
        Index(
            "uq_conversation_message_client_token",
            "conversation_id",
            "client_token",
            unique=True,
        ),
    )


class InterventionRequest(Base):
    """A persistent, structured request for human input that is blocking one task.

    Created before the agent's message is published, so the conversation can never advertise a
    request that does not exist. Resolved only by an authenticated reply routed through this
    same conversation, never by the task engine.
    """

    __tablename__ = "intervention_requests"

    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    owner_email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    # MISSING_AUTHENTICATION, TELEGRAM_SESSION_UNAVAILABLE, BROWSER_SESSION_UNAVAILABLE,
    # HUMAN_VERIFICATION, MISSING_CONFIGURATION, AMBIGUOUS_INSTRUCTION, OPERATOR_PAUSE,
    # HUMAN_APPROVAL, UNKNOWN
    reason_code: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    problem_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    detailed_explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    required_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    instructions: Mapped[Optional[List[str]]] = mapped_column(JSONB, default=list)
    # Integration / session reference the operator must fix, when one is known.
    integration_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    blocking_step: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    error_category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Name of the checkpoint written when this task was parked, so the operator can see
    # exactly where a resume would pick up from. Null when the checkpoint could not be saved.
    checkpoint_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # PENDING, RESOLVED, RESUME_SCHEDULED, CANCELLED, SUPERSEDED
    status: Mapped[str] = mapped_column(String(50), default="PENDING", index=True, nullable=False)
    created_at_task_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # How the external condition was checked, and what it reported. Never asserted by the client.
    verification_method: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    verification_outcome: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reply_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    conversation: Mapped["AgentConversation"] = relationship("AgentConversation")
