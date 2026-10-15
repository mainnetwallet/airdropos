"""Wallets gain `is_default`: the wallet the agent uses when a task names none.

Revision ID: 8_wallet_default
Revises: 7_inbox_lifecycle
Create Date: 2026-10-15

Additive only. Existing wallets default to false, so nothing changes until the operator
picks a default on the Wallets page. SQLite databases receive the same column from
`schema_sync` at startup; this revision is for PostgreSQL / Alembic deployments.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8_wallet_default"
down_revision: Union[str, None] = "7_inbox_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return "is_default" in {c["name"] for c in inspector.get_columns("wallets")}


def upgrade() -> None:
    if _has_column():
        return
    op.add_column(
        "wallets",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    if _has_column():
        with op.batch_alter_table("wallets") as batch:
            batch.drop_column("is_default")
