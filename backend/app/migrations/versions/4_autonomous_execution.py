"""Add autonomous-execution columns to `transaction_approvals`.

Revision ID: 4_autonomous_execution
Revises: 3_transaction_approvals
Create Date: 2026-10-13

Three columns, all additive:

* `idempotency_key` (unique) - a digest of exactly what a transfer does. A repeated
  request for the same intent reuses the existing approval instead of creating a
  second one, so a retried command cannot broadcast twice.
* `policy_rule_id` - which configured autonomous rule authorized an approval that no
  person did. NULL for a human approval. Makes "why did this send money unattended"
  answerable from the record.
* `broadcast_status` - written the moment a node accepts a transaction, before any
  receipt wait, so a crash between the two still leaves evidence on disk.

Why a new revision rather than editing 3: revision 3 has already run on deployed
databases. Editing an applied migration means those databases never receive the
columns, and the next fresh database would have them - two different schemas behind one
revision number.

No existing row is read, rewritten, or dropped. The unique index is created only once
the column exists, which is why the column is added first.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4_autonomous_execution"
down_revision: Union[str, None] = "3_transaction_approvals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "transaction_approvals"

NEW_COLUMNS = (
    ("idempotency_key", sa.String(length=128)),
    ("policy_rule_id", sa.String(length=128)),
    ("broadcast_status", sa.String(length=30)),
)


def _columns() -> set:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    columns = _columns()
    if not columns:
        # Revision 3 has not run here (a create_all-built database, for example).
        # Nothing to add; the table will be created with the full shape by 3.
        return

    # Columns first. `idempotency_key` carries a UNIQUE index, which SQLite cannot
    # create with ALTER TABLE ADD COLUMN.
    for name, spec in NEW_COLUMNS:
        if name not in columns:
            op.add_column(TABLE, sa.Column(name, spec, nullable=True))

    indexes = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    if "ix_transaction_approvals_idempotency_key" not in indexes:
        op.create_index(
            "ix_transaction_approvals_idempotency_key", TABLE, ["idempotency_key"], unique=True
        )
    if "ix_transaction_approvals_policy_rule_id" not in indexes:
        op.create_index("ix_transaction_approvals_policy_rule_id", TABLE, ["policy_rule_id"])


def downgrade() -> None:
    columns = _columns()
    if not columns:
        return
    indexes = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    if "ix_transaction_approvals_policy_rule_id" in indexes:
        op.drop_index("ix_transaction_approvals_policy_rule_id", table_name=TABLE)
    if "ix_transaction_approvals_idempotency_key" in indexes:
        op.drop_index("ix_transaction_approvals_idempotency_key", table_name=TABLE)
    # SQLite supports DROP COLUMN from 3.35; wrapped so an older runtime fails
    # loudly rather than silently leaving a half-reverted schema.
    for name, _spec in NEW_COLUMNS:
        if name in columns:
            op.drop_column(TABLE, name)