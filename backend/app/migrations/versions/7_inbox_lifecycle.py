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

2. On PostgreSQL each expected relationship is made to carry exactly one correct foreign key,
   after step 1 so the constraint can validate. "Correct" means all three of: it constrains
   that one column, it references the expected table's `id`, and its ON DELETE action is
   CASCADE. A foreign key on the column that fails any of them (it points at the wrong table or
   column, or it has no cascade, which makes the task DELETE fail instead of cleaning up) is
   dropped and replaced; a correct one is left exactly as it is, whatever it is called, so a
   valid constraint is never rebuilt and no duplicate is added. Drop and create run in this
   revision's own transaction, which PostgreSQL DDL honours, so a replacement that cannot
   validate rolls the whole revision back and leaves the original constraint in place.

   SQLite cannot ADD or ALTER a constraint on an existing table without rebuilding it, and a
   rebuild is the one operation that could lose the operator's messages. So on SQLite no
   constraint is touched at all, and the explicit deletes in the application are what keep the
   tables consistent. The decision of what would change is made by `plan_foreign_keys`, which
   only reads the schema, so it is exercised on SQLite too even though only PostgreSQL acts
   on it.

   A foreign key that is wrong but has no name cannot be dropped by name. That relationship is
   reported in the migration log and left untouched rather than given a second constraint
   next to the bad one. PostgreSQL always names its constraints, so this is a guard, not an
   expected path.

Nothing about the unique index on (channel, channel_conversation_id) changes. A conversation
that is deleted frees its key, which is what lets a task that needs a person again open a
fresh conversation instead of reusing the deleted one.

The downgrade drops only the constraints this revision created, matched by name and by what
they constrain. A constraint it replaced is not restored to its earlier, wrong definition, and
a valid constraint it found already in place is not ours to remove, so it keeps its own name
and survives. Deleted orphans cannot be restored and are not meant to be: nothing could ever
reach them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import sqlalchemy as sa
from alembic import op

log = logging.getLogger("alembic.runtime.migration")

revision: str = "7_inbox_lifecycle"
down_revision: Union[str, None] = "6_task_context"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONVERSATIONS = "agent_conversations"
MESSAGES = "conversation_messages"
INTERVENTIONS = "intervention_requests"
TASKS = "tasks"

# Every foreign key this revision manages references the `id` of its parent table.
TARGET_COLUMN = "id"

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


def _is_cascade(fk: Dict[str, Any]) -> bool:
    ondelete = (fk.get("options") or {}).get("ondelete")
    return str(ondelete or "").strip().upper() == "CASCADE"


def _targets(fk: Dict[str, Any], referred: str, default_schema: Optional[str]) -> bool:
    """True when the foreign key references `referred`.id in the default schema."""
    return (
        fk.get("referred_schema") in (None, default_schema)
        and fk.get("referred_table") == referred
        and list(fk.get("referred_columns") or []) == [TARGET_COLUMN]
    )


def _classify_foreign_keys(
    foreign_keys: Sequence[Dict[str, Any]],
    column: str,
    referred: str,
    default_schema: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split the single-column foreign keys on `column` into (correct, incorrect).

    Correct means it references `referred`.id AND cascades on delete. A foreign key that
    spans several columns is not a constraint on `column` alone and is ignored.
    """
    correct: List[Dict[str, Any]] = []
    incorrect: List[Dict[str, Any]] = []
    for fk in foreign_keys:
        if list(fk.get("constrained_columns") or []) != [column]:
            continue
        if _targets(fk, referred, default_schema) and _is_cascade(fk):
            correct.append(fk)
        else:
            incorrect.append(fk)
    return correct, incorrect


def _constraint_names_in_use(inspector: Any, table: str, foreign_keys: Sequence[Dict[str, Any]]) -> set:
    """Every constraint name on `table`. PostgreSQL keeps one namespace per table, so a
    unique, check or primary-key constraint blocks a new foreign key of the same name too."""
    names = {fk["name"] for fk in foreign_keys if fk.get("name")}
    names |= {c["name"] for c in inspector.get_unique_constraints(table) if c.get("name")}
    names |= {c["name"] for c in inspector.get_check_constraints(table) if c.get("name")}
    pk_name = (inspector.get_pk_constraint(table) or {}).get("name")
    if pk_name:
        names.add(pk_name)
    return names


def plan_foreign_keys(inspector: Any) -> List[Tuple[Any, ...]]:
    """Work out what it takes for every expected relationship to carry one correct cascade.

    Only reads the schema; nothing is changed here. Returns, in the order they must run:

        ("drop", table, constraint_name)
        ("create", table, constraint_name, column, referred_table)
        ("unresolved", table, column, reason)      # reported, never acted on

    A relationship whose foreign key is already correct produces no action at all. A drop is
    only ever planned together with the create that replaces it (or beside an existing correct
    constraint): if the replacement cannot be created, the plan holds "unresolved" alone.
    """
    tables = set(inspector.get_table_names())
    default_schema = getattr(inspector, "default_schema_name", None)
    plan: List[Tuple[Any, ...]] = []

    for table, column, referred, name in CASCADES:
        if table not in tables or referred not in tables:
            continue
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        if TARGET_COLUMN not in {c["name"] for c in inspector.get_columns(referred)}:
            continue

        foreign_keys = inspector.get_foreign_keys(table)
        correct, incorrect = _classify_foreign_keys(foreign_keys, column, referred, default_schema)

        if any(not fk.get("name") for fk in incorrect):
            plan.append(
                ("unresolved", table, column, "a wrong foreign key without a name cannot be dropped")
            )
            continue

        drops = [("drop", table, fk["name"]) for fk in incorrect]

        if correct:
            # A correct cascade already exists; only stale duplicates beside it go.
            plan.extend(drops)
            continue

        # Every precondition for the replacement is settled BEFORE anything destructive is
        # planned. If the expected name is held by a constraint that is not itself being
        # dropped, no replacement can be created, so the wrong constraint is left exactly
        # where it is rather than removed with nothing to take its place.
        dropped = {fk["name"] for fk in incorrect}
        if name in _constraint_names_in_use(inspector, table, foreign_keys) - dropped:
            plan.append(
                ("unresolved", table, column, f"constraint name {name!r} is already in use elsewhere")
            )
            continue

        plan.extend(drops)
        plan.append(("create", table, name, column, referred))

    return plan


def upgrade() -> None:
    _remove_orphans()

    # SQLite cannot add or alter a constraint in place; see the module docstring.
    if op.get_bind().dialect.name != "postgresql":
        return

    for action in plan_foreign_keys(_inspector()):
        kind = action[0]
        if kind == "drop":
            _, table, constraint = action
            op.drop_constraint(constraint, table, type_="foreignkey")
        elif kind == "create":
            _, table, constraint, column, referred = action
            op.create_foreign_key(
                constraint, table, referred, [column], [TARGET_COLUMN], ondelete="CASCADE"
            )
        else:
            _, table, column, reason = action
            log.warning(
                "Inbox lifecycle: left %s.%s unchanged: %s. Fix that constraint by hand.",
                table, column, reason,
            )


def downgrade() -> None:
    """Drop only the constraints this revision created. Idempotent."""
    if op.get_bind().dialect.name != "postgresql":
        return
    inspector = _inspector()
    tables = set(inspector.get_table_names())
    for table, column, referred, name in CASCADES:
        if table not in tables:
            continue
        for fk in inspector.get_foreign_keys(table):
            # By name AND by what it constrains, so an unrelated constraint that happens to
            # share the name is never dropped.
            if (
                fk.get("name") == name
                and list(fk.get("constrained_columns") or []) == [column]
                and fk.get("referred_table") == referred
            ):
                op.drop_constraint(name, table, type_="foreignkey")
