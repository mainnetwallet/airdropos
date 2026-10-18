"""Add the X.com integration tables: API configurations and activity history.

Revision ID: 11_x_integration
Revises: 10_task_execution_telemetry
Create Date: 2026-10-18

Two new tables, strictly additive. Nothing existing is dropped, renamed or
rewritten, so `downgrade` can only remove what this revision created.

* ``x_api_configurations`` - one row per X developer app + authorized account.
  Secrets are stored as AES-256-GCM envelopes, never as plaintext columns.
* ``x_activity_records``  - the integration's own history: which account, which
  method (api or browser), which operation, when, and whether the outcome is
  known. This is what makes duplicate prevention and post-hoc verification of an
  uncertain write possible.

Neither table has a foreign key into ``tasks`` or the configuration table on
purpose: an activity row is evidence of what was sent and must outlive the task
and the credential that produced it.

SQLite deployments receive both tables from ``create_all`` at startup; this
revision is for PostgreSQL / Alembic deployments.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "11_x_integration"
down_revision: Union[str, None] = "10_task_execution_telemetry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONFIGURATIONS = "x_api_configurations"
ACTIVITY = "x_activity_records"


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()

    if CONFIGURATIONS not in existing:
        op.create_table(
            CONFIGURATIONS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.String(length=400), nullable=False, server_default=""),
            sa.Column("client_id", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("client_secret_enc", sa.Text(), nullable=True),
            sa.Column("access_token_enc", sa.Text(), nullable=True),
            sa.Column("refresh_token_enc", sa.Text(), nullable=True),
            sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("scopes", postgresql.JSONB(), nullable=True),
            sa.Column("x_user_id", sa.String(length=32), nullable=True),
            sa.Column("x_username", sa.String(length=64), nullable=True),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="UNVERIFIED"),
            sa.Column("status_detail", sa.String(length=500), nullable=True),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rate_limit_remaining", sa.Integer(), nullable=True),
            sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", sa.String(length=128), nullable=False, server_default="operator"),
        )
        op.create_index("ix_x_api_configurations_name", CONFIGURATIONS, ["name"], unique=True)
        op.create_index("ix_x_api_configurations_enabled_default", CONFIGURATIONS, ["is_enabled", "is_default"])
        op.create_index("ix_x_api_configurations_x_user_id", CONFIGURATIONS, ["x_user_id"])
        op.create_index("ix_x_api_configurations_x_username", CONFIGURATIONS, ["x_username"])
        op.create_index("ix_x_api_configurations_status", CONFIGURATIONS, ["status"])

    if ACTIVITY not in existing:
        op.create_table(
            ACTIVITY,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("config_id", sa.String(length=64), nullable=True),
            sa.Column("config_name", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("browser_profile_id", sa.String(length=64), nullable=True),
            sa.Column("method", sa.String(length=16), nullable=False),
            sa.Column("operation", sa.String(length=48), nullable=False),
            sa.Column("target", sa.String(length=512), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("operation_key", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("outcome_uncertain", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("result_summary", postgresql.JSONB(), nullable=True),
            sa.Column("error", sa.String(length=600), nullable=True),
            sa.Column("error_category", sa.String(length=64), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("task_id", sa.String(length=64), nullable=True),
        )
        op.create_index("ix_x_activity_records_operation_key_created", ACTIVITY, ["operation_key", "created_at"])
        op.create_index("ix_x_activity_records_created_at", ACTIVITY, ["created_at"])
        op.create_index("ix_x_activity_records_operation", ACTIVITY, ["operation"])
        op.create_index("ix_x_activity_records_status", ACTIVITY, ["status"])
        op.create_index("ix_x_activity_records_config_id", ACTIVITY, ["config_id"])
        op.create_index("ix_x_activity_records_task_id", ACTIVITY, ["task_id"])


def downgrade() -> None:
    existing = _tables()
    # Only the tables this revision created are removed. No pre-existing table or
    # row is touched, so a rollback cannot lose operator data.
    if ACTIVITY in existing:
        op.drop_table(ACTIVITY)
    if CONFIGURATIONS in existing:
        op.drop_table(CONFIGURATIONS)