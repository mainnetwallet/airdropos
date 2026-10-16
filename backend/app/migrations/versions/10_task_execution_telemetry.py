"""Tasks gain cumulative execution telemetry and the execution-window yield counter.

Revision ID: 10_task_execution_telemetry
Revises: 9_task_telegram_account
Create Date: 2026-10-16

Additive only, and every column has a safe default, so an existing installation keeps
working unchanged and an existing task simply starts reporting zeros.

What each column is for:

* `execution_windows` / `unproductive_windows` — how many times this task has been handed
  to a worker, and how many of those achieved nothing. These are what make a long
  objective resumable *and* what stop it being resumed forever.
* `llm_requests`, `llm_input_tokens`, `llm_output_tokens`, `llm_calls_without_usage` —
  cumulative AI cost. The token columns are nullable on purpose: NULL means the provider
  never reported the figure (UNKNOWN), 0 means it reported zero. Collapsing those two would
  understate spend on exactly the router endpoints that omit usage.
* `served_model_mismatches` / `last_served_model` — what the provider said it served
  beside what was requested, so a router is visible rather than assumed away.
* `tool_calls` / `tool_failures` — execution volume and reliability.
* `replans_total` / `recovery_replans_total` / `discovery_replans_total` — planning churn,
  split by cause so a task that keeps *learning* is distinguishable from one that keeps
  *failing*.
* `last_blocker` — the precise reason a task stopped, so it survives a restart and is
  visible without re-deriving it from a log.

SQLite databases receive the same columns from `schema_sync` at startup; this revision is
for PostgreSQL / Alembic deployments.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "10_task_execution_telemetry"
down_revision: Union[str, None] = "9_task_telegram_account"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COUNT_COLUMNS = (
    ("execution_windows", sa.Integer(), "0"),
    ("llm_requests", sa.Integer(), "0"),
    ("llm_calls_without_usage", sa.Integer(), "0"),
    ("served_model_mismatches", sa.Integer(), "0"),
    ("tool_calls", sa.Integer(), "0"),
    ("tool_failures", sa.Integer(), "0"),
    ("replans_total", sa.Integer(), "0"),
    ("recovery_replans_total", sa.Integer(), "0"),
    ("discovery_replans_total", sa.Integer(), "0"),
    ("unproductive_windows", sa.Integer(), "0"),
)

# Nullable with no default: NULL is the honest "unknown" for a token figure the provider
# never reported. A default of 0 would assert the task used no tokens, which is a claim.
_NULLABLE_COLUMNS = (
    ("llm_input_tokens", sa.Integer()),
    ("llm_output_tokens", sa.Integer()),
    ("last_served_model", sa.String(length=128)),
    ("last_blocker", sa.Text()),
)


def _existing() -> set:
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns("tasks")}


def upgrade() -> None:
    present = _existing()
    for name, column_type, default in _COUNT_COLUMNS:
        if name in present:
            continue
        op.add_column(
            "tasks",
            sa.Column(
                name,
                column_type,
                nullable=False,
                server_default=sa.text(default),
            ),
        )
    for name, column_type in _NULLABLE_COLUMNS:
        if name in present:
            continue
        op.add_column("tasks", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    present = _existing()
    for name, _column_type in reversed(_NULLABLE_COLUMNS):
        if name in present:
            with op.batch_alter_table("tasks") as batch:
                batch.drop_column(name)
    for name, _column_type, _default in reversed(_COUNT_COLUMNS):
        if name in present:
            with op.batch_alter_table("tasks") as batch:
                batch.drop_column(name)