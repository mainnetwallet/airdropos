"""Which Telegram accounts are in use by which tasks.

A task is bound to one Telegram account (``Task.telegram_account``). The binding doubles as
a lock: while any bound task is unfinished its account cannot be stopped, disconnected or
removed. When the last such task ends the account's connection is closed, and a later task
simply opens it again. Many tasks may share one account, and tasks on different accounts run
at the same time.

The truth lives in the database, because the API and the worker are separate processes.
The small in-memory map below only tells the tools of *this* process which account the task
they are running for was bound to, so the tool call path never needs a database round trip.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.integrations import telegram_accounts
from backend.app.models import Task

logger = logging.getLogger(__name__)

# A task in one of these states no longer needs its account. Everything else (queued, running,
# waiting, retrying, needs-user-action) still does, so the account stays locked for it.
RELEASED_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "PARTIALLY_COMPLETED", "UNVERIFIED"})

_bindings: Dict[str, Optional[str]] = {}


class AccountInUse(Exception):
    """The account is bound to unfinished tasks and so cannot be stopped or removed."""

    def __init__(self, label: str, task_ids: List[str]) -> None:
        self.label = label
        self.task_ids = task_ids
        shown = ", ".join(task_ids[:5]) + (" ..." if len(task_ids) > 5 else "")
        super().__init__(
            f"Telegram account '{label}' is in use by {len(task_ids)} unfinished task(s): {shown}. "
            "It cannot be stopped or removed until they finish. Cancel those tasks to free it."
        )


def normalize_label(label: Optional[str]) -> Optional[str]:
    """The registered spelling of ``label`` (case-insensitive), or None for no label.

    Raises ``ValueError`` for a label that names no logged-in account.
    """
    if label is None or not str(label).strip():
        return None
    account = telegram_accounts.get_account(str(label).strip())
    if account is None:
        known = ", ".join(a["label"] for a in telegram_accounts.list_accounts()) or "none"
        raise ValueError(f"Unknown Telegram account '{str(label)[:40]}'. Logged-in accounts: {known}.")
    return account["label"]


# -- queries ----------------------------------------------------------------------------------
async def active_task_ids(db: AsyncSession, label: str) -> List[str]:
    """Public ids of unfinished tasks bound to this account (case-insensitive)."""
    rows = await db.execute(
        select(Task.task_id, Task.telegram_account, Task.status).where(Task.telegram_account.isnot(None))
    )
    wanted = (label or "").strip().lower()
    return sorted(
        tid for tid, acct, status in rows.all()
        if (acct or "").lower() == wanted and status not in RELEASED_STATUSES
    )


async def labels_in_use(db: AsyncSession) -> Dict[str, List[str]]:
    """Map of account label (lower-case) to the unfinished task ids bound to it."""
    rows = await db.execute(
        select(Task.task_id, Task.telegram_account, Task.status).where(Task.telegram_account.isnot(None))
    )
    out: Dict[str, List[str]] = {}
    for tid, acct, status in rows.all():
        if status not in RELEASED_STATUSES:
            out.setdefault(acct.lower(), []).append(tid)
    return {k: sorted(v) for k, v in out.items()}


async def assert_free(db: AsyncSession, label: str, *, ignore_task: Optional[str] = None) -> None:
    """Raise ``AccountInUse`` if unfinished tasks (other than ``ignore_task``) hold the account."""
    ids = [t for t in await active_task_ids(db, label) if t != (ignore_task or "").upper()]
    if ids:
        raise AccountInUse(label, ids)


async def bind_if_unbound(db: AsyncSession, task_id: str, label: str) -> bool:
    """Record ``label`` on a task that has none yet. Returns whether this call bound it."""
    result = await db.execute(
        update(Task)
        .where(Task.task_id == task_id, Task.telegram_account.is_(None))
        .values(telegram_account=label)
    )
    return (result.rowcount or 0) == 1


# -- per-process map for the tool path ----------------------------------------------------------
def begin_run(task_id: str, label: Optional[str]) -> None:
    _bindings[task_id.upper()] = label


def end_run(task_id: str) -> None:
    _bindings.pop(task_id.upper(), None)


def bound_label(task_id: Optional[str]) -> Optional[str]:
    return _bindings.get(task_id.upper()) if task_id else None


def remember_binding(task_id: str, label: str) -> None:
    _bindings[task_id.upper()] = label


def is_running_here(task_id: Optional[str]) -> bool:
    return bool(task_id) and task_id.upper() in _bindings
