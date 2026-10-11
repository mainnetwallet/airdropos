"""Initial schema migration.

Revision ID: 1_initial_schema
Revises:
Create Date: 2026-10-11 00:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "1_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create all tables in dependency order
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255)),
        sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
        sa.Column("is_superuser", sa.Boolean(), nullable=False, default=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "user_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.String(255), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address", sa.String(255)),
        sa.Column("user_agent", sa.String(500)),
        sa.Column("scopes", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )

    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", sa.String(50), nullable=False, unique=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.String(50), nullable=False, default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "goals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("goal_code", sa.String(50), nullable=False, unique=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, default="ACTIVE"),
        sa.Column("progress", sa.Float(), nullable=False, default=0.0),
        sa.Column("next_planned_action", sa.Text),
        sa.Column("current_blockers", postgresql.ARRAY(sa.Text)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", sa.String(50), nullable=False, unique=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("category", sa.String(100)),
        sa.Column("network", sa.String(50)),
        sa.Column("status", sa.String(50), nullable=False, default="PENDING"),
        sa.Column("progress", sa.Float(), nullable=False, default=0.0),
        sa.Column("retry_count", sa.Integer(), nullable=False, default=0),
        sa.Column("max_retries", sa.Integer(), nullable=False, default=3),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False, default=0.0),
        sa.Column("actual_cost_usd", sa.Float(), nullable=False, default=0.0),
        sa.Column("next_execution_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("planned_actions", postgresql.ARRAY(sa.JSONB), nullable=True),
        sa.Column("completed_actions", postgresql.ARRAY(sa.JSONB), nullable=True),
        sa.Column("failed_actions", postgresql.ARRAY(sa.JSONB), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False, default=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False, default=0),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "wallets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("wallet_id", sa.String(50), nullable=False, unique=True),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("address", sa.String(255), nullable=False),
        sa.Column("chain_type", sa.String(50), nullable=False),
        sa.Column("supported_networks", postgresql.ARRAY(sa.String)),  # e.g., ["ethereum", "arbitrum"]
        sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
        sa.Column("is_locked", sa.Boolean(), nullable=False, default=True),
        sa.Column("total_spent_usd", sa.Float(), nullable=False, default=0.0),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "blockchain_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("network", sa.String(50), nullable=False),
        sa.Column("chain_id", sa.Integer()),
        sa.Column("to_address", sa.String(255), nullable=False),
        sa.Column("from_address", sa.String(255)),
        sa.Column("value_wei", sa.String(255)),
        sa.Column("gas_limit", sa.String(255)),
        sa.Column("gas_price_wei", sa.String(255)),
        sa.Column("data", sa.Text),
        sa.Column("tx_hash", sa.String(255)),
        sa.Column("status", sa.String(50), nullable=False, default="PENDING"),
        sa.Column("confirmation_count", sa.Integer(), nullable=False, default=0),
        sa.Column("required_confirmations", sa.Integer(), nullable=False, default=12),
        sa.Column("max_fee_limit_usd", sa.Float()),
        sa.Column("actual_gas_cost_usd", sa.Float()),
        sa.Column("usd_value_transacted", sa.Float()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text),
        sa.Column("error_category", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"]),
    )

    op.create_table(
        "transaction_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("block_number", sa.Integer()),
        sa.Column("gas_used", sa.String(255)),
        sa.Column("gas_price_wei", sa.String(255)),
        sa.Column("cumulative_gas_used", sa.String(255)),
        sa.Column("effective_gas_price", sa.String(255)),
        sa.Column("status", sa.String(50)),
        sa.Column("logs", postgresql.JSONB),
        sa.Column("logs_bloom", sa.String(255)),
        sa.Column("root", sa.String(255)),
        sa.Column("contract_address", sa.String(255)),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["transaction_id"], ["blockchain_transactions.id"]),
    )

    op.create_table(
        "airdrop_campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("network", sa.String(50)),
        sa.Column("status", sa.String(50), nullable=False, default="DISCOVERED"),
        sa.Column("risk_score", sa.Float(), nullable=False, default=0.0),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "campaign_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_url", sa.String(500), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["campaign_id"], ["airdrop_campaigns.id"]),
    )

    op.create_table(
        "wallet_eligibility",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_eligible", sa.Boolean(), nullable=False, default=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("evidence", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["campaign_id"], ["airdrop_campaigns.id"]),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"]),
    )

    op.create_table(
        "claim_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_status", sa.String(50), nullable=False, default="NOT_STARTED"),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at_block", sa.Integer()),
        sa.Column("reward_amount", sa.Float()),
        sa.Column("reward_token", sa.String(100)),
        sa.Column("error_message", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["campaign_id"], ["airdrop_campaigns.id"]),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"]),
    )

    op.create_table(
        "tools",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tool_id", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("category", sa.String(50)),
        sa.Column("input_schema", postgresql.JSONB, nullable=False),
        sa.Column("output_schema", postgresql.JSONB, nullable=False),
        sa.Column("required_permissions", postgresql.ARRAY(sa.String)),
        sa.Column("side_effect", sa.String(50)),
        sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
        sa.Column("is_idempotent", sa.Boolean(), nullable=False, default=True),
        sa.Column("max_retries", sa.Integer(), nullable=False, default=2),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, default=60),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "tool_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tool_id", sa.String(100), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("parameters", postgresql.JSONB, nullable=False),
        sa.Column("result", postgresql.JSONB),
        sa.Column("status", sa.String(50), nullable=False, default="PENDING"),
        sa.Column("error_message", sa.Text),
        sa.Column("error_category", sa.String(100)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("attempt", sa.Integer(), nullable=False, default=0),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "generated_adapters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("adapter_id", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("source_code", sa.Text, nullable=False),
        sa.Column("input_schema", postgresql.JSONB, nullable=False),
        sa.Column("output_schema", postgresql.JSONB, nullable=False),
        sa.Column("required_permissions", postgresql.ARRAY(sa.String)),
        sa.Column("side_effect", sa.String(50)),
        sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "execution_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, default=1),
        sa.Column("is_current", sa.Boolean(), nullable=False, default=True),
        sa.Column("objective", sa.Text, nullable=False),
        sa.Column("steps", postgresql.JSONB, nullable=False),
        sa.Column("constraints", postgresql.JSONB),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False, default=0.0),
        sa.Column("estimated_duration_minutes", sa.Integer(), nullable=False, default=0),
        sa.Column("rationale", sa.Text),
        sa.Column("llm_reasoning", sa.Text),
        sa.Column("trigger_reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
    )

    op.create_table(
        "plan_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_revision", sa.Integer()),
        sa.Column("to_revision", sa.Integer(), nullable=False),
        sa.Column("trigger_category", sa.String(50)),
        sa.Column("reason", sa.Text),
        sa.Column("added_steps", postgresql.ARRAY(sa.String)),
        sa.Column("removed_steps", postgresql.ARRAY(sa.String)),
        sa.Column("modified_steps", postgresql.ARRAY(sa.String)),
        sa.Column("preserved_irreversible_step_ids", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["plan_id"], ["execution_plans.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
    )

    op.create_table(
        "execution_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lease_token", sa.String(255), nullable=False),
        sa.Column("worker_id", sa.String(255), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_iteration", sa.Integer(), nullable=False, default=0),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
    )

    op.create_table(
        "memory_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, default=0.5),
        sa.Column("is_inference", sa.Boolean(), nullable=False, default=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "knowledge_relationships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("from_memory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_memory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relationship_type", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["from_memory_id"], ["memory_records.id"]),
        sa.ForeignKeyConstraint(["to_memory_id"], ["memory_records.id"]),
    )

    op.create_table(
        "learning_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False, default=1),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "improvement_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("status", sa.String(50), nullable=False, default="DRAFT"),
        sa.Column("security_review_required", sa.Boolean(), nullable=False, default=False),
        sa.Column("affected_files", postgresql.ARRAY(sa.String)),
        sa.Column("required_tests", postgresql.ARRAY(sa.String)),
        sa.Column("branch_name", sa.String(100)),
        sa.Column("rollback_plan", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", sa.String(255), nullable=False),
        sa.Column("before_state", postgresql.JSONB),
        sa.Column("after_state", postgresql.JSONB),
        sa.Column("ip_address", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "system_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(100), nullable=False, unique=True),
        sa.Column("value", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("is_sensitive", sa.Boolean(), nullable=False, default=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "automation_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schedule_id", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("job_type", sa.String(100), nullable=False),
        sa.Column("cron_expression", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "scheduled_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schedule_id", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, default="PENDING"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("result", postgresql.JSONB),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("severity", sa.String(50), nullable=False, default="INFO"),
        sa.Column("channel", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, default="PENDING"),
        sa.Column("delivered_to", postgresql.ARRAY(sa.String)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(50), nullable=False),
        sa.Column("recipient", sa.String(255)),
        sa.Column("status", sa.String(50), nullable=False, default="PENDING"),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"]),
    )

    op.create_table(
        "integration_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subsystem", sa.String(100), nullable=False, unique=True),
        sa.Column("status", sa.String(50), nullable=False, default="UNKNOWN"),
        sa.Column("detail", sa.Text),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "integration_health",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subsystem", sa.String(100), nullable=False, unique=True),
        sa.Column("status", sa.String(50), nullable=False, default="UNKNOWN"),
        sa.Column("detail", sa.Text),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "diagnostic_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subsystem", sa.String(100), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("metadata", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "world_state_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("observation_type", sa.String(100), nullable=False),
        sa.Column("observation_data", postgresql.JSONB, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "source_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True)),
        sa.Column("claim", sa.Text, nullable=False),
        sa.Column("source_url", sa.String(500), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("classification", sa.String(50), nullable=False, default="UNVERIFIED_CLAIM"),
        sa.Column("confidence", sa.Float(), nullable=False, default=0.0),
        sa.Column("related_project", sa.String(255)),
        sa.Column("related_task", sa.String(50)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "change_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True)),
        sa.Column("change_type", sa.String(100), nullable=False),
        sa.Column("old_value", postgresql.JSONB),
        sa.Column("new_value", postgresql.JSONB),
        sa.Column("impact", sa.String(100), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, default=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "failure_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("error_category", sa.String(100), nullable=False),
        sa.Column("error_message", sa.Text, nullable=False),
        sa.Column("diagnosis", postgresql.JSONB, nullable=False),
        sa.Column("recovery_strategy", sa.String(100), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, default=0),
        sa.Column("resolved", sa.Boolean(), nullable=False, default=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "resource_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("usage_value", sa.Float(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "planning_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("trigger", sa.String(50), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("result", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "agent_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("channel", sa.String(50), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # Create indexes
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_projects_project_id", "projects", ["project_id"], unique=True)
    op.create_index("ix_goals_goal_code", "goals", ["goal_code"], unique=True)
    op.create_index("ix_tasks_task_id", "tasks", ["task_id"], unique=True)
    op.create_index("ix_wallets_wallet_id", "wallets", ["wallet_id"], unique=True)
    op.create_index("ix_blockchain_transactions_wallet_id", "blockchain_transactions", ["wallet_id"])
    op.create_index("ix_blockchain_transactions_task_id", "blockchain_transactions", ["task_id"])
    op.create_index("ix_blockchain_transactions_tx_hash", "blockchain_transactions", ["tx_hash"], unique=True)
    op.create_index("ix_airdrop_campaigns_campaign_id", "airdrop_campaigns", ["campaign_id"], unique=True)
    op.create_index("ix_campaign_sources_campaign_id", "campaign_sources", ["campaign_id"])
    op.create_index("ix_wallet_eligibility_campaign_id", "wallet_eligibility", ["campaign_id"])
    op.create_index("ix_wallet_eligibility_wallet_id", "wallet_eligibility", ["wallet_id"])
    op.create_index("ix_claim_records_campaign_id", "claim_records", ["campaign_id"])
    op.create_index("ix_claim_records_wallet_id", "claim_records", ["wallet_id"])
    op.create_index("ix_execution_plans_task_id", "execution_plans", ["task_id"])
    op.create_index("ix_plan_revisions_plan_id", "plan_revisions", ["plan_id"])
    op.create_index("ix_plan_revisions_task_id", "plan_revisions", ["task_id"])
    op.create_index("ix_execution_leases_task_id", "execution_leases", ["task_id"])
    op.create_index("ix_memory_records_expires_at", "memory_records", ["expires_at"])
    op.create_index("ix_world_state_observations_task_id", "world_state_observations", ["task_id"])
    op.create_index("ix_source_claims_campaign_id", "source_claims", ["campaign_id"])
    op.create_index("ix_change_observations_campaign_id", "change_observations", ["campaign_id"])
    op.create_index("ix_failure_records_task_id", "failure_records", ["task_id"])
    op.create_index("ix_planning_runs_task_id", "planning_runs", ["task_id"])
    op.create_index("ix_agent_conversations_task_id", "agent_conversations", ["task_id"])

    # Create trigger for updated_at
    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)

    # Attach trigger to all tables with updated_at
    tables_with_updated_at = [
        "users", "user_sessions", "projects", "goals", "tasks", "wallets",
        "blockchain_transactions", "transaction_receipts", "airdrop_campaigns",
        "campaign_sources", "wallet_eligibility", "claim_records", "tools",
        "tool_executions", "generated_adapters", "execution_plans", "plan_revisions",
        "execution_leases", "learning_records", "improvement_proposals", "audit_logs",
        "system_settings", "automation_schedules", "scheduled_runs", "notifications",
        "notification_deliveries", "integration_health", "diagnostic_records",
        "world_state_observations", "source_claims", "change_observations",
        "failure_records", "resource_usage", "planning_runs", "agent_conversations"
    ]

    for table in tables_with_updated_at:
        op.execute(f"""
            CREATE TRIGGER trigger_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
        """)


def downgrade() -> None:
    # Drop triggers
    for table in [
        "users", "user_sessions", "projects", "goals", "tasks", "wallets",
        "blockchain_transactions", "transaction_receipts", "airdrop_campaigns",
        "campaign_sources", "wallet_eligibility", "claim_records", "tools",
        "tool_executions", "generated_adapters", "execution_plans", "plan_revisions",
        "execution_leases", "learning_records", "improvement_proposals", "audit_logs",
        "system_settings", "automation_schedules", "scheduled_runs", "notifications",
        "notification_deliveries", "integration_health", "diagnostic_records",
        "world_state_observations", "source_claims", "change_observations",
        "failure_records", "resource_usage", "planning_runs", "agent_conversations"
    ]:
        op.execute(f"DROP TRIGGER IF EXISTS trigger_{table}_updated_at ON {table}")

    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column()")

    # Drop indexes
    op.drop_index("ix_agent_conversations_task_id", table_name="agent_conversations")
    op.drop_index("ix_planning_runs_task_id", table_name="planning_runs")
    op.drop_index("ix_failure_records_task_id", table_name="failure_records")
    op.drop_index("ix_change_observations_campaign_id", table_name="change_observations")
    op.drop_index("ix_source_claims_campaign_id", table_name="source_claims")
    op.drop_index("ix_world_state_observations_task_id", table_name="world_state_observations")
    op.drop_index("ix_memory_records_expires_at", table_name="memory_records")
    op.drop_index("ix_execution_leases_task_id", table_name="execution_leases")
    op.drop_index("ix_plan_revisions_task_id", table_name="plan_revisions")
    op.drop_index("ix_plan_revisions_plan_id", table_name="plan_revisions")
    op.drop_index("ix_execution_plans_task_id", table_name="execution_plans")
    op.drop_index("ix_claim_records_wallet_id", table_name="claim_records")
    op.drop_index("ix_claim_records_campaign_id", table_name="claim_records")
    op.drop_index("ix_wallet_eligibility_wallet_id", table_name="wallet_eligibility")
    op.drop_index("ix_wallet_eligibility_campaign_id", table_name="wallet_eligibility")
    op.drop_index("ix_campaign_sources_campaign_id", table_name="campaign_sources")
    op.drop_index("ix_airdrop_campaigns_campaign_id", table_name="airdrop_campaigns")
    op.drop_index("ix_blockchain_transactions_tx_hash", table_name="blockchain_transactions")
    op.drop_index("ix_blockchain_transactions_task_id", table_name="blockchain_transactions")
    op.drop_index("ix_blockchain_transactions_wallet_id", table_name="blockchain_transactions")
    op.drop_index("ix_wallets_wallet_id", table_name="wallets")
    op.drop_index("ix_tasks_task_id", table_name="tasks")
    op.drop_index("ix_goals_goal_code", table_name="goals")
    op.drop_index("ix_projects_project_id", table_name="projects")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_index("ix_users_email", table_name="users")

    # Drop tables in dependency order
    op.drop_table("conversation_messages")
    op.drop_table("agent_conversations")
    op.drop_table("planning_runs")
    op.drop_table("resource_usage")
    op.drop_table("failure_records")
    op.drop_table("change_observations")
    op.drop_table("source_claims")
    op.drop_table("world_state_observations")
    op.drop_table("diagnostic_records")
    op.drop_table("integration_health")
    op.drop_table("notification_deliveries")
    op.drop_table("notifications")
    op.drop_table("scheduled_runs")
    op.drop_table("automation_schedules")
    op.drop_table("system_settings")
    op.drop_table("audit_logs")
    op.drop_table("improvement_proposals")
    op.drop_table("learning_records")
    op.drop_table("memory_records")
    op.drop_table("knowledge_relationships")
    op.drop_table("execution_leases")
    op.drop_table("plan_revisions")
    op.drop_table("execution_plans")
    op.drop_table("generated_adapters")
    op.drop_table("tool_executions")
    op.drop_table("tools")
    op.drop_table("claim_records")
    op.drop_table("wallet_eligibility")
    op.drop_table("campaign_sources")
    op.drop_table("airdrop_campaigns")
    op.drop_table("transaction_receipts")
    op.drop_table("blockchain_transactions")
    op.drop_table("wallets")
    op.drop_table("tasks")
    op.drop_table("goals")
    op.drop_table("projects")
    op.drop_table("user_sessions")
    op.drop_table("users")
