"""Tasks gain `telegram_account`: the Telegram account a task is bound to.

Revision ID: 9_task_telegram_account
Revises: 8_wallet_default
Create Date: 2026-10-16

Additive only. Existing tasks get NULL, which means "use the default account" exactly as
before. SQLite databases receive the same column from `schema_sync` at startup; this
revision is for PostgreSQL / Alembic deployments.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9_task_telegram_account"
down_revision: Union[str, None] = "8_wallet_default"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return "telegram_account" in {c["name"] for c in inspector.get_columns("tasks")}


def upgrade() -> None:
    if _has_column():
        return
    op.add_column("tasks", sa.Column("telegram_account", sa.String(length=40), nullable=True))
    op.create_index("ix_tasks_telegram_account", "tasks", ["telegram_account"])


def downgrade() -> None:
    if _has_column():
        op.drop_index("ix_tasks_telegram_account", table_name="tasks")
        with op.batch_alter_table("tasks") as batch:
            batch.drop_column("telegram_account")
