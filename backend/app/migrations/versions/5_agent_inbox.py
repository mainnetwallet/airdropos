"""Agent Inbox: task-scoped conversations and intervention requests.

Revision ID: 5_agent_inbox
Revises: 4_autonomous_execution
Create Date: 2026-10-13

Brings the Agent Inbox schema in line with the models. Three things happen here.

First, `agent_conversations` and `conversation_messages` already existed but were never
written to, and the shape migration 1 created for them never matched the ORM: migration 1
gives `agent_conversations` a `role`/`content` pair that the ORM has no columns for, and
gives `conversation_messages` no foreign key at all. A database built by `alembic upgrade`
therefore could not have stored a message correctly. The columns the ORM declares are added
here, guarded so an already-correct `create_all` database is left alone.

Second, the conversation and message tables gain task scoping, an owner, a lifecycle status
and an idempotency token. Without the owner there is no way to answer "whose task is this",
and without the token a double-submitted reply could be stored twice and schedule two resumes.

Third, `intervention_requests` is created. It is the structured, durable record of a task
blocked on a person: what went wrong, what is needed, where, how it will be verified, and how
it was resolved. `tasks.operator_email`-style task ownership and `tasks.operator_instructions`
are added in the same revision so an intervention always has a task owner to address.

Every statement is written as a literal call rather than a loop over a table of names, so the
DDL stays greppable and the migration-consistency tests can see it.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5_agent_inbox"
down_revision: Union[str, None] = "4_autonomous_execution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONVERSATIONS = "agent_conversations"
MESSAGES = "conversation_messages"
INTERVENTIONS = "intervention_requests"
TASKS = "tasks"

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

    A column cannot be dropped while an index still covers it, and the set is not known up
    front: a create_all-built database has indexes for every indexed column, while one built
    through the revision chain has only the ones created below. Finding them from the live
    schema is what makes the downgrade work on both.
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


def _drop_columns(table: str, columns) -> None:
    """Drop `columns`, taking any dependent index with them. Idempotent."""
    present = _columns(table)
    targets = [name for name in columns if name in present]
    if not targets:
        return
    for index in _indexes_referencing(table, targets):
        op.drop_index(index, table_name=table)
    for name in targets:
        op.drop_column(table, name)


def upgrade() -> None:
    tables = _tables()

    # ------------------------------------------------------------------
    # Tasks: ownership and operator-supplied clarifications.
    # ------------------------------------------------------------------
    task_columns = _columns(TASKS)
    if task_columns:
        if "owner_email" not in task_columns:
            op.add_column(TASKS, sa.Column("owner_email", sa.String(length=255), nullable=True))
        if "operator_instructions" not in task_columns:
            op.add_column(TASKS, sa.Column("operator_instructions", JSONB, nullable=True))
        if "ix_tasks_owner_email" not in _indexes(TASKS):
            op.create_index("ix_tasks_owner_email", TASKS, ["owner_email"])

    # ------------------------------------------------------------------
    # Conversations: the ORM shape migration 1 never produced.
    # ------------------------------------------------------------------
    if CONVERSATIONS in tables:
        columns = _columns(CONVERSATIONS)
        if "channel_conversation_id" not in columns:
            op.add_column(
                CONVERSATIONS, sa.Column("channel_conversation_id", sa.String(length=255), nullable=True)
            )
        if "title" not in columns:
            op.add_column(CONVERSATIONS, sa.Column("title", sa.String(length=500), nullable=True))
        if "context_summary" not in columns:
            op.add_column(CONVERSATIONS, sa.Column("context_summary", sa.Text, nullable=True))
        if "updated_at" not in columns:
            op.add_column(
                CONVERSATIONS, sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
            )
        if "task_id" not in columns:
            op.add_column(CONVERSATIONS, sa.Column("task_id", UUID, nullable=True))
        if "owner_email" not in columns:
            op.add_column(CONVERSATIONS, sa.Column("owner_email", sa.String(length=255), nullable=True))
        if "status" not in columns:
            op.add_column(
                CONVERSATIONS,
                sa.Column("status", sa.String(length=50), nullable=False, server_default="OPEN"),
            )
        if "requires_reply" not in columns:
            op.add_column(
                CONVERSATIONS,
                sa.Column("requires_reply", sa.Boolean, nullable=False, server_default=sa.false()),
            )
        if "last_message_at" not in columns:
            op.add_column(
                CONVERSATIONS, sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True)
            )
        if "last_seq" not in columns:
            op.add_column(
                CONVERSATIONS,
                sa.Column("last_seq", sa.Integer, nullable=False, server_default="0"),
            )
    else:
        op.create_table(
            CONVERSATIONS,
            sa.Column("id", UUID, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("channel", sa.String(length=50), nullable=False),
            sa.Column("channel_conversation_id", sa.String(length=255), nullable=False),
            sa.Column("title", sa.String(length=500), nullable=True),
            sa.Column("context_summary", sa.Text, nullable=True),
            sa.Column("task_id", UUID, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True),
            sa.Column("owner_email", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="OPEN"),
            sa.Column("requires_reply", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seq", sa.Integer, nullable=False, server_default="0"),
        )

    # ------------------------------------------------------------------
    # Messages: the ORM shape, plus task scoping and an idempotency token.
    # ------------------------------------------------------------------
    if MESSAGES in tables:
        columns = _columns(MESSAGES)
        if "tool_calls" not in columns:
            op.add_column(MESSAGES, sa.Column("tool_calls", JSONB, nullable=True))
        if "tokens_used" not in columns:
            op.add_column(MESSAGES, sa.Column("tokens_used", sa.Integer, nullable=True))
        if "updated_at" not in columns:
            op.add_column(MESSAGES, sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        if "task_id" not in columns:
            op.add_column(MESSAGES, sa.Column("task_id", UUID, nullable=True))
        if "kind" not in columns:
            op.add_column(
                MESSAGES,
                sa.Column("kind", sa.String(length=50), nullable=False, server_default="AGENT"),
            )
        if "seq" not in columns:
            op.add_column(
                MESSAGES, sa.Column("seq", sa.Integer, nullable=False, server_default="0")
            )
        if "client_token" not in columns:
            op.add_column(MESSAGES, sa.Column("client_token", sa.String(length=128), nullable=True))
        if "intervention_request_id" not in columns:
            op.add_column(
                MESSAGES, sa.Column("intervention_request_id", sa.String(length=64), nullable=True)
            )
        # `role` needs no widening: revision 1 already declared it String(50), which is the
        # width the ORM uses. SQLite cannot ALTER COLUMN anyway, so a statement that would
        # only ever run on PostgreSQL is deliberately absent.
    else:
        op.create_table(
            MESSAGES,
            sa.Column("id", UUID, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "conversation_id",
                UUID,
                sa.ForeignKey(CONVERSATIONS + ".id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("role", sa.String(length=50), nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("tool_calls", JSONB, nullable=True),
            sa.Column("tokens_used", sa.Integer, nullable=True),
            sa.Column("task_id", UUID, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True),
            sa.Column("kind", sa.String(length=50), nullable=False, server_default="AGENT"),
            sa.Column("seq", sa.Integer, nullable=False, server_default="0"),
            sa.Column("client_token", sa.String(length=128), nullable=True),
            sa.Column("intervention_request_id", sa.String(length=64), nullable=True),
        )

    # ------------------------------------------------------------------
    # Intervention requests.
    # ------------------------------------------------------------------
    if INTERVENTIONS not in _tables():
        op.create_table(
            INTERVENTIONS,
            sa.Column("id", UUID, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("request_id", sa.String(length=64), nullable=False),
            sa.Column("task_id", UUID, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "conversation_id",
                UUID,
                sa.ForeignKey(CONVERSATIONS + ".id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("owner_email", sa.String(length=255), nullable=False),
            sa.Column("reason_code", sa.String(length=64), nullable=False),
            sa.Column("problem_summary", sa.String(length=500), nullable=False),
            sa.Column("detailed_explanation", sa.Text, nullable=True),
            sa.Column("required_action", sa.Text, nullable=True),
            sa.Column("instructions", JSONB, nullable=True),
            sa.Column("integration_ref", sa.String(length=255), nullable=True),
            sa.Column("blocking_step", sa.String(length=500), nullable=True),
            sa.Column("error_category", sa.String(length=100), nullable=True),
            sa.Column("checkpoint_name", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="PENDING"),
            sa.Column("created_at_task_status", sa.String(length=50), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_by", sa.String(length=255), nullable=True),
            sa.Column("verification_method", sa.String(length=100), nullable=True),
            sa.Column("verification_outcome", sa.Text, nullable=True),
            sa.Column("reply_message_id", UUID, nullable=True),
        )

    # ------------------------------------------------------------------
    # Indexes and uniqueness. Created after the columns: SQLite cannot add a UNIQUE
    # column, and a unique index over columns that may still be NULL is fine but has to
    # come after they exist.
    # ------------------------------------------------------------------
    if CONVERSATIONS in _tables():
        indexes = _indexes(CONVERSATIONS)
        if "ix_agent_conversations_channel_conversation_id" not in indexes:
            op.create_index(
                "ix_agent_conversations_channel_conversation_id",
                CONVERSATIONS,
                ["channel_conversation_id"],
            )
        if "ix_agent_conversations_task_id" not in indexes:
            op.create_index("ix_agent_conversations_task_id", CONVERSATIONS, ["task_id"])
        if "ix_agent_conversations_owner_email" not in indexes:
            op.create_index("ix_agent_conversations_owner_email", CONVERSATIONS, ["owner_email"])
        if "ix_agent_conversations_requires_reply" not in indexes:
            op.create_index("ix_agent_conversations_requires_reply", CONVERSATIONS, ["requires_reply"])
        if "ix_agent_conversations_last_message_at" not in indexes:
            op.create_index(
                "ix_agent_conversations_last_message_at", CONVERSATIONS, ["last_message_at"]
            )
        if "uq_agent_conversation_channel_key" not in indexes:
            op.create_index(
                "uq_agent_conversation_channel_key",
                CONVERSATIONS,
                ["channel", "channel_conversation_id"],
                unique=True,
            )

    if MESSAGES in _tables():
        indexes = _indexes(MESSAGES)
        if "ix_conversation_messages_conversation_id" not in indexes:
            op.create_index(
                "ix_conversation_messages_conversation_id", MESSAGES, ["conversation_id"]
            )
        if "ix_conversation_messages_task_id" not in indexes:
            op.create_index("ix_conversation_messages_task_id", MESSAGES, ["task_id"])
        if "ix_conversation_messages_intervention_request_id" not in indexes:
            op.create_index(
                "ix_conversation_messages_intervention_request_id",
                MESSAGES,
                ["intervention_request_id"],
            )
        if "uq_conversation_message_client_token" not in indexes:
            op.create_index(
                "uq_conversation_message_client_token",
                MESSAGES,
                ["conversation_id", "client_token"],
                unique=True,
            )

    if INTERVENTIONS in _tables():
        indexes = _indexes(INTERVENTIONS)
        if "ix_intervention_requests_request_id" not in indexes:
            op.create_index(
                "ix_intervention_requests_request_id", INTERVENTIONS, ["request_id"], unique=True
            )
        if "ix_intervention_requests_task_id" not in indexes:
            op.create_index("ix_intervention_requests_task_id", INTERVENTIONS, ["task_id"])
        if "ix_intervention_requests_conversation_id" not in indexes:
            op.create_index(
                "ix_intervention_requests_conversation_id", INTERVENTIONS, ["conversation_id"]
            )
        if "ix_intervention_requests_status" not in indexes:
            op.create_index("ix_intervention_requests_status", INTERVENTIONS, ["status"])
        if "ix_intervention_requests_reason_code" not in indexes:
            op.create_index("ix_intervention_requests_reason_code", INTERVENTIONS, ["reason_code"])


def downgrade() -> None:
    """Remove what this revision added, without destroying operator message history.

    Two columns are deliberately left in place: `task_id` on both conversation tables. They
    carry a foreign key, and SQLite cannot drop an FK-bearing column without rebuilding the
    whole table -- which is the one operation that would throw the operator's messages away.
    Leaving them is safe on both sides of the round trip: nothing in the pre-Inbox code reads
    them, and the upgrade above skips any column that is already present, so re-upgrading
    still produces the correct shape.
    """
    tables = _tables()

    if INTERVENTIONS in tables:
        op.drop_table(INTERVENTIONS)

    if MESSAGES in tables:
        _drop_columns(
            MESSAGES, ("client_token", "intervention_request_id", "seq", "kind")
        )

    if CONVERSATIONS in tables:
        _drop_columns(
            CONVERSATIONS,
            ("last_seq", "last_message_at", "requires_reply", "status", "owner_email"),
        )

    _drop_columns(TASKS, ("operator_instructions", "owner_email"))