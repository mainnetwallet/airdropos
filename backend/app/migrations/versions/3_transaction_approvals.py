"""Add `transaction_approvals`: the server-side record of a human approving a transfer.

Revision ID: 3_transaction_approvals
Revises: 2_reconcile_schema_with_models
Create Date: 2026-10-13

Why this table exists
---------------------
`TransactionEngine.submit_raw` refuses to broadcast without an independent,
server-side proof that a named human approved *that exact transaction*, and refuses
to accept a caller-supplied "approved" flag. Until now there was no store for that
proof, so the engine's `approval_verifier` was always `None` and every broadcast was
correctly refused - which also meant the agent could never prepare, sign or submit a
real transfer.

This migration is strictly additive: one new table and its indexes. It creates
nothing else, drops nothing else, and rewrites no existing row. `downgrade` removes
only the table this revision created, so a rollback cannot touch operator data.

Every AirdropOS database in the wild was built with
`Base.metadata.create_all` rather than from the migration chain, so the table may
already exist. The guard below makes this idempotent in that case.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3_transaction_approvals"
down_revision: Union[str, None] = "2_reconcile_schema_with_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "transaction_approvals"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE in set(inspector.get_table_names()):
        # Already present (create_all-built database). Nothing to add.
        return

    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approval_id", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=False),
        sa.Column("network", sa.String(length=50), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=True),
        sa.Column("wallet_address", sa.String(length=128), nullable=False),
        sa.Column("to_address", sa.String(length=128), nullable=True),
        sa.Column("value_wei", sa.String(length=78), nullable=True),
        sa.Column("tx_hash", sa.String(length=128), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("binding", sa.JSON(), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_transaction_approvals_approval_id", TABLE, ["approval_id"], unique=True)
    op.create_index("ix_transaction_approvals_approved_by", TABLE, ["approved_by"])
    op.create_index("ix_transaction_approvals_wallet_address", TABLE, ["wallet_address"])
    op.create_index("ix_transaction_approvals_tx_hash", TABLE, ["tx_hash"])
    op.create_index("ix_transaction_approvals_proposal_id", TABLE, ["proposal_id"])
    op.create_index("ix_transaction_approvals_expires_at", TABLE, ["expires_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in set(inspector.get_table_names()):
        return
    # Only the table this revision created is removed. No pre-existing table or row
    # is touched, so rolling back cannot lose operator data.
    op.drop_table(TABLE)