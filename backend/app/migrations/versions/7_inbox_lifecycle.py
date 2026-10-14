"""Agent Inbox lifecycle: remove orphaned Inbox rows, restore cascade where it can be.

Revision ID: 7_inbox_lifecycle
Revises: 6_task_context
Create Date: 2026-10-15

Deleting a task must delete its Inbox conversations, messages and intervention requests, and
deleting one conversation must never touch its task. The application now does both with
explicit DELETE statements, because the database alone cannot be trusted to:

* SQLite does not enforce foreign keys unless asked, and AirdropOS does not ask.
* Revision 5 added `task_id` to `agent_conversations` and `conversation_messages` with
  `add_column` when those tables already existed. That creates the column WITHOUT the
  `ON DELETE CASCADE` foreign key the models declare, and revision 1 created
  `conversation_messages.conversation_id` with no foreign key at all. A database built
  through the revision chain therefore has no cascade to rely on.

Two things happen here.

1. Orphans are removed. Any conversation whose task no longer exists, and any message or
   intervention request whose conversation or task no longer exists, can only have come from
   a task deletion that did not cascade. They point at nothing and can never be answered or
   resumed, so they are deleted (children first). Conversations with no task at all (the
   general console) are not touched, and no row that still has a live task is touched.

2. On PostgreSQL the missing foreign keys are added with ON DELETE CASCADE, after step 1 so
   the constraint can validate. SQLite cannot ADD a constraint to an existing table without
   rebuilding it, and a rebuild is the one operation that could lose the operator's messages,
   so on SQLite the explicit deletes in the application are what keep the tables consistent.
   A foreign key that already exists on a column is left alone, whatever it is called.

Nothing about the unique index on (channel, channel_conversation_id) changes. A conversation
that is deleted frees its key, which is what lets a task that needs a person again open a
fresh conversation instead of reusing the deleted one.

The downgrade drops only the constraints this revision added. Deleted orphans cannot be
restored and are not meant to be: nothing could ever reach them.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7_inbox_lifecycle"
down_revision: Union[str, None] = "6_task_context"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONVERSATIONS = "agent_conversations"
MESSAGES = "conversation_messages"
INTERVENTIONS = "intervention_requests"
TASKS = "tasks"

# (table, column, referred table, constraint name) for each cascade this revision may add.
CASCADES = (
    (CONVERSATIONS, "task_id", TASKS, "fk_agent_conversations_task_id_tasks"),
    (MESSAGES, "conversation_id", CONVERSATIONS, "fk_conversation_messages_conversation_id"),
    (MESSAGES, "task_id", TASKS, "fk_conversation_messages_task_id_tasks"),
    (INTERVENTIONS, "task_id", TASKS, "fk_intervention_requests_task_id_tasks"),
    (INTERVENTIONS, "conversation_id", CONVERSATIONS, "fk_intervention_requests_conversation_id"),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_columns(table: str, *columns: str) -> bool:
    inspector = _inspector()
    if table not in set(inspector.get_table_names()):
        return False
    present = {c["name"] for c in inspector.get_columns(table)}
    return all(name in present for name in columns)


def _delete(statement: str) -> None:
    op.get_bind().execute(sa.text(statement))


def _remove_orphans() -> None:
    have_tasks = _has_columns(TASKS, "id")
    have_conv = _has_columns(CONVERSATIONS, "id", "task_id")
    have_msg = _has_columns(MESSAGES, "conversation_id", "task_id")
    have_req = _has_columns(INTERVENTIONS, "conversation_id", "task_id")

    # Children of conversations whose task is gone, before the conversations themselves.
    if have_tasks and have_conv:
        orphan_conversations = (
            f"SELECT id FROM {CONVERSATIONS} "
            f"WHERE task_id IS NOT NULL AND task_id NOT IN (SELECT id FROM {TASKS})"
        )
        if have_req:
            _delete(f"DELETE FROM {INTERVENTIONS} WHERE conversation_id IN ({orphan_conversations})")
        if have_msg:
            _delete(f"DELETE FROM {MESSAGES} WHERE conversation_id IN ({orphan_conversations})")
        _delete(
            f"DELETE FROM {CONVERSATIONS} "
            f"WHERE task_id IS NOT NULL AND task_id NOT IN (SELECT id FROM {TASKS})"
        )

    # Rows that survived with a dead task or a dead conversation of their own.
    if have_req and have_tasks:
        _delete(f"DELETE FROM {INTERVENTIONS} WHERE task_id NOT IN (SELECT id FROM {TASKS})")
    if have_req and have_conv:
        _delete(
            f"DELETE FROM {INTERVENTIONS} WHERE conversation_id NOT IN (SELECT id FROM {CONVERSATIONS})"
        )
    if have_msg and have_tasks:
        _delete(
            f"DELETE FROM {MESSAGES} "
            f"WHERE task_id IS NOT NULL AND task_id NOT IN (SELECT id FROM {TASKS})"
        )
    if have_msg and have_conv:
        _delete(
            f"DELETE FROM {MESSAGES} WHERE conversation_id NOT IN (SELECT id FROM {CONVERSATIONS})"
        )


def _fk_covering(table: str, column: str) -> bool:
    return any(
        list(fk.get("constrained_columns") or []) == [column]
        for fk in _inspector().get_foreign_keys(table)
    )


def _constraint_names(table: str) -> set:
    return {fk["name"] for fk in _inspector().get_foreign_keys(table) if fk.get("name")}


def upgrade() -> None:
    _remove_orphans()

    # SQLite cannot add a constraint in place; see the module docstring.
    if op.get_bind().dialect.name != "postgresql":
        return

    for table, column, referred, name in CASCADES:
        if not _has_columns(table, column) or not _has_columns(referred, "id"):
            continue
        if _fk_covering(table, column):
            continue
        op.create_foreign_key(name, table, referred, [column], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    """Drop only the constraints this revision created, by name. Idempotent."""
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, _column, _referred, name in CASCADES:
        if _has_columns(table, "id") and name in _constraint_names(table):
            op.drop_constraint(name, table, type_="foreignkey")
