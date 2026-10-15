"""Hard-delete a task together with everything that depends on it.

PostgreSQL cascades a task delete through its foreign keys. SQLite does not: the app never turns
``PRAGMA foreign_keys`` on, so a plain ``DELETE FROM tasks`` leaves the task's events, plans,
executions, evidence and context behind as orphans. This walks the declared foreign keys and
applies each one's ON DELETE rule itself, so the result is the same on both databases:

* ``ON DELETE CASCADE``  -> the dependent rows (and, recursively, their dependents) are deleted.
* ``ON DELETE SET NULL`` -> the reference is cleared and the row is kept.

A foreign key with no ON DELETE rule is left alone; the caller refuses to delete in that case
(a task that owns blockchain transactions) before getting here.
"""

from __future__ import annotations

from typing import Any, Set

from sqlalchemy import Table, delete, select, update

from backend.app.database.session import Base


def _referencing_keys(parent: Table):
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table is parent:
                yield table, fk


async def _purge(db: Any, table: Table, where: Any, seen: Set[str]) -> None:
    """Delete the rows of ``table`` matching ``where`` after clearing what points at them."""
    if table.name in seen:  # a cascade cycle would never end; schema has none today
        return
    seen = seen | {table.name}

    pk = list(table.primary_key.columns)[0]
    matching = select(pk).where(where)

    for child, fk in _referencing_keys(table):
        rule = (fk.ondelete or "").upper()
        child_col = fk.parent
        if rule == "CASCADE":
            await _purge(db, child, child_col.in_(matching), seen)
        elif rule == "SET NULL":
            await db.execute(
                update(child).where(child_col.in_(matching)).values({child_col.name: None})
            )

    await db.execute(delete(table).where(where))


async def delete_task_cascade(db: Any, task_pk: Any) -> None:
    """Delete one task row and all rows that depend on it. Does not commit."""
    tasks = Base.metadata.tables["tasks"]
    await _purge(db, tasks, tasks.c.id == task_pk, set())
