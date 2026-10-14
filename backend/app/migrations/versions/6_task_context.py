"""Persistent execution context: task context, observations, requirements, memory scope.

Revision ID: 6_task_context
Revises: 5_agent_inbox
Create Date: 2026-10-14

Everything the executor needs between steps - the goal, what was attempted, what each
attempt returned, what was learned, which requirements remain, which external resource
is relevant, and why the next action is next - was previously held only in the LLM
context window. A restart, a replan or a switch between integrations lost it, and a
resumed run could only re-derive it by replaying work.

Three tables are created:

* `task_contexts`     - exactly one row per task. The isolated execution context.
* `task_observations`- the action history of that one task, with a fingerprint of the
  tool plus its canonical arguments so a confirmed action is not repeated.
* `task_requirements`- requirements discovered while executing, each tied to the
  observation that produced or satisfied it.

`memory_records` gains `scope`, `task_id` and `campaign_id`. Without them every memory
was global, so any lookup returned one task's private history to another. Existing rows
default to `global`, which is exactly what they were before: their meaning does not
change.

Why a new revision rather than editing 5: revision 5 has already run on deployed
databases. Editing an applied migration means those databases never receive the tables,
and a fresh database would get them - two schemas behind one revision number.

No existing row is read, rewritten, or dropped. The new columns are nullable or carry a
server default, and the new tables are empty.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6_task_context"
down_revision: Union[str, None] = "5_agent_inbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TASK_CONTEXTS = "task_contexts"
TASK_OBSERVATIONS = "task_observations"
TASK_REQUIREMENTS = "task_requirements"
MEMORY_RECORDS = "memory_records"

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _indexes(table: str) -> set:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {ix["name"] for ix in inspector.get_indexes(table)}


def _indexes_referencing(table: str, columns) -> list:
    """Indexes that depend on `columns`.

    A column cannot be dropped while an index still covers it, and the set is not known
    up front: a create_all-built database has an index for every indexed column, while one
    built through the revision chain has only the ones created below. Finding them from
    the live schema is what makes the downgrade work on both.
    """
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return []
    wanted = set(columns)
    return [
        ix["name"]
        for ix in inspector.get_indexes(table)
        if wanted & set(ix.get("column_names") or [])
    ]


def upgrade() -> None:
    tables = _tables()

    # ------------------------------------------------------------------
    # One isolated execution context per task.
    # ------------------------------------------------------------------
    if TASK_CONTEXTS not in tables:
        op.create_table(
            TASK_CONTEXTS,
            sa.Column("id", UUID, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "task_id", UUID, sa.ForeignKey("tasks.id", ondelete="CASCADE"),
                nullable=False, unique=True,
            ),
            sa.Column(
                "campaign_id", UUID,
                sa.ForeignKey("airdrop_campaigns.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("goal", sa.Text, nullable=True),
            sa.Column("normalized_objective", sa.Text, nullable=True),
            sa.Column("success_criteria", JSONB, nullable=True),
            sa.Column("outstanding_requirements", JSONB, nullable=True),
            sa.Column("unresolved_questions", JSONB, nullable=True),
            sa.Column("resource_refs", JSONB, nullable=True),
            sa.Column("external_state", JSONB, nullable=True),
            sa.Column("next_step_rationale", sa.Text, nullable=True),
            sa.Column("current_step_index", sa.Integer, nullable=True),
            sa.Column("human_intervention", JSONB, nullable=True),
            sa.Column("authorization_state", JSONB, nullable=True),
            sa.Column("retry_summary", JSONB, nullable=True),
            sa.Column("last_error", sa.Text, nullable=True),
            sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
            sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if TASK_CONTEXTS in _tables():
        if "ix_task_contexts_task_id" not in _indexes(TASK_CONTEXTS):
            op.create_index("ix_task_contexts_task_id", TASK_CONTEXTS, ["task_id"], unique=True)
        if "ix_task_contexts_campaign_id" not in _indexes(TASK_CONTEXTS):
            op.create_index("ix_task_contexts_campaign_id", TASK_CONTEXTS, ["campaign_id"])

    # ------------------------------------------------------------------
    # The action history, with the fingerprint used to suppress repeats.
    # ------------------------------------------------------------------
    if TASK_OBSERVATIONS not in tables:
        op.create_table(
            TASK_OBSERVATIONS,
            sa.Column("id", UUID, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "task_id", UUID, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "context_id", UUID,
                sa.ForeignKey("task_contexts.id", ondelete="CASCADE"), nullable=True,
            ),
            sa.Column("iteration", sa.Integer, nullable=False, server_default="0"),
            sa.Column("step_index", sa.Integer, nullable=True),
            sa.Column("action", sa.String(length=500), nullable=False),
            sa.Column("tool", sa.String(length=128), nullable=True),
            sa.Column("arguments_digest", sa.String(length=64), nullable=True),
            sa.Column("fingerprint", sa.String(length=64), nullable=False),
            sa.Column("outcome", sa.String(length=40), nullable=False, server_default="attempted"),
            sa.Column("evidence_kind", sa.String(length=64), nullable=True),
            sa.Column("evidence_authoritative", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("observed_state", JSONB, nullable=True),
            sa.Column("result_digest", JSONB, nullable=True),
            sa.Column("integration_ref", sa.String(length=255), nullable=True),
            sa.Column("is_idempotent", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("error", sa.Text, nullable=True),
        )
    if TASK_OBSERVATIONS in _tables():
        existing = _indexes(TASK_OBSERVATIONS)
        if "ix_task_observations_task_id" not in existing:
            op.create_index("ix_task_observations_task_id", TASK_OBSERVATIONS, ["task_id"])
        if "ix_task_observations_context_id" not in existing:
            op.create_index("ix_task_observations_context_id", TASK_OBSERVATIONS, ["context_id"])
        if "ix_task_observations_tool" not in existing:
            op.create_index("ix_task_observations_tool", TASK_OBSERVATIONS, ["tool"])
        if "ix_task_observations_fingerprint" not in existing:
            op.create_index("ix_task_observations_fingerprint", TASK_OBSERVATIONS, ["fingerprint"])
        if "ix_task_observations_outcome" not in existing:
            op.create_index("ix_task_observations_outcome", TASK_OBSERVATIONS, ["outcome"])
        if "ix_task_observations_task_fingerprint" not in existing:
            op.create_index(
                "ix_task_observations_task_fingerprint",
                TASK_OBSERVATIONS, ["task_id", "fingerprint"],
            )
        if "ix_task_observations_task_step" not in existing:
            op.create_index(
                "ix_task_observations_task_step", TASK_OBSERVATIONS, ["task_id", "step_index"]
            )

    # ------------------------------------------------------------------
    # Requirements discovered while executing.
    # ------------------------------------------------------------------
    if TASK_REQUIREMENTS not in tables:
        op.create_table(
            TASK_REQUIREMENTS,
            sa.Column("id", UUID, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "task_id", UUID, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "context_id", UUID,
                sa.ForeignKey("task_contexts.id", ondelete="CASCADE"), nullable=True,
            ),
            sa.Column("text", sa.String(length=1000), nullable=False),
            sa.Column("origin", sa.String(length=50), nullable=False, server_default="tool_result"),
            sa.Column("source_ref", sa.String(length=512), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="discovered"),
            sa.Column(
                "satisfied_by_observation_id", UUID,
                sa.ForeignKey("task_observations.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("evidence", JSONB, nullable=True),
            sa.Column("is_campaign_scoped", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("is_account_specific", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("account_ref", sa.String(length=255), nullable=True),
        )
    if TASK_REQUIREMENTS in _tables():
        existing = _indexes(TASK_REQUIREMENTS)
        if "ix_task_requirements_task_id" not in existing:
            op.create_index("ix_task_requirements_task_id", TASK_REQUIREMENTS, ["task_id"])
        if "ix_task_requirements_context_id" not in existing:
            op.create_index("ix_task_requirements_context_id", TASK_REQUIREMENTS, ["context_id"])
        if "ix_task_requirements_status" not in existing:
            op.create_index("ix_task_requirements_status", TASK_REQUIREMENTS, ["status"])

    # ------------------------------------------------------------------
    # Memory hierarchy. Nullable, and `scope` defaults to the pre-existing meaning.
    # ------------------------------------------------------------------
    if MEMORY_RECORDS in _tables():
        columns = _columns(MEMORY_RECORDS)
        # Columns first. SQLite cannot ALTER COLUMN to add a foreign key to a populated
        # table, so the foreign keys live in the ORM and the migration keeps the column
        # definitions portable across both backends.
        if "scope" not in columns:
            op.add_column(
                MEMORY_RECORDS,
                sa.Column("scope", sa.String(length=20), nullable=False, server_default="global"),
            )
        if "task_id" not in columns:
            op.add_column(MEMORY_RECORDS, sa.Column("task_id", UUID, nullable=True))
        if "campaign_id" not in columns:
            op.add_column(MEMORY_RECORDS, sa.Column("campaign_id", UUID, nullable=True))

        indexes = _indexes(MEMORY_RECORDS)
        if "ix_memory_records_scope" not in indexes:
            op.create_index("ix_memory_records_scope", MEMORY_RECORDS, ["scope"])
        if "ix_memory_records_task_id" not in indexes:
            op.create_index("ix_memory_records_task_id", MEMORY_RECORDS, ["task_id"])
        if "ix_memory_records_campaign_id" not in indexes:
            op.create_index("ix_memory_records_campaign_id", MEMORY_RECORDS, ["campaign_id"])


def downgrade() -> None:
    # Memory columns first: the new tables reference tasks/airdrop_campaigns.
    if MEMORY_RECORDS in _tables():
        columns = _columns(MEMORY_RECORDS)
        targets = [name for name in ("scope", "task_id", "campaign_id") if name in columns]
        if targets:
            for index in _indexes_referencing(MEMORY_RECORDS, targets):
                op.drop_index(index, table_name=MEMORY_RECORDS)
            for name in targets:
                op.drop_column(MEMORY_RECORDS, name)

    for table in (TASK_REQUIREMENTS, TASK_OBSERVATIONS, TASK_CONTEXTS):
        if table in _tables():
            op.drop_table(table)