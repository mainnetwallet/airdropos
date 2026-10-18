"""Add the Discord browser integration's activity history table.

Revision ID: 12_discord_integration
Revises: 11_x_integration
Create Date: 2026-10-18

One new table, strictly additive. Nothing existing is dropped, renamed or
rewritten, so ``downgrade`` can only remove what this revision created.

* ``discord_activity_records`` - the integration's own history: which browser
  profile acted, which operation, on what target, when, and whether the outcome is
  known. This is what makes duplicate-send suppression and post-hoc verification of
  an uncertain send possible.

There is deliberately no configuration table: the Discord integration is
browser-only and stores no credential of any kind, so there is nothing to configure
beyond what the existing browser profile system already holds.

The table has no foreign key into ``tasks`` on purpose: an activity row is evidence
of what was actually sent from an account and must outlive the task that produced it.

SQLite deployments receive the table from ``create_all`` at startup; this revision
is for PostgreSQL / Alembic deployments.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "12_discord_integration"
down_revision: Union[str, None] = "11_x_integration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVITY = "discord_activity_records"


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if ACTIVITY in _tables():
        return

    op.create_table(
        ACTIVITY,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("browser_profile_id", sa.String(length=64), nullable=True),
        sa.Column("operation", sa.String(length=48), nullable=False),
        sa.Column("intent", sa.String(length=16), nullable=False, server_default="read"),
        sa.Column("target", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("operation_key", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("outcome_uncertain", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("result_summary", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_path", sa.String(length=512), nullable=True),
        sa.Column("error", sa.String(length=600), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_discord_activity_records_operation_key_created", ACTIVITY, ["operation_key", "created_at"]
    )
    op.create_index("ix_discord_activity_records_created_at", ACTIVITY, ["created_at"])
    op.create_index("ix_discord_activity_records_operation", ACTIVITY, ["operation"])
    op.create_index("ix_discord_activity_records_status", ACTIVITY, ["status"])
    op.create_index("ix_discord_activity_records_error_category", ACTIVITY, ["error_category"])
    op.create_index("ix_discord_activity_records_browser_profile_id", ACTIVITY, ["browser_profile_id"])
    op.create_index("ix_discord_activity_records_task_id", ACTIVITY, ["task_id"])


def downgrade() -> None:
    # Only the table this revision created is removed. No pre-existing table or row is
    # touched, so a rollback cannot lose operator data.
    if ACTIVITY in _tables():
        op.drop_table(ACTIVITY)