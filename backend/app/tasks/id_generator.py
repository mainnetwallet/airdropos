"""Persistent Task ID Generator.

Format: AIR-YYYY-MM-DD-NNNNNN  e.g. AIR-2026-10-15-483920
(today's date, then 6 random digits).

Every ID that is issued is recorded in ``issued_task_ids`` and that record is never removed, so
an ID that belonged to a deleted task is never handed out again. The unique constraint on that
table is what makes this safe when several workers create tasks at the same time.

Older IDs of the form AIR-YYYY-NNNNNN stay valid; they are only read, never generated.
"""

from __future__ import annotations

import inspect
import logging
import re
import secrets
from datetime import date, datetime
from typing import Any, Callable, Optional

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

ID_PREFIX = "AIR"
_DATED_ID = re.compile(r"^AIR-(\d{4})-(\d{2})-(\d{2})-(\d{6})$")
_LEGACY_ID = re.compile(r"^AIR-(\d{4})-(\d{6})$")


def today() -> date:
    """The operator's local calendar date (the machine's timezone)."""
    return datetime.now().astimezone().date()


def random_suffix() -> str:
    """Six random digits from the OS CSPRNG (100000-999999, so always six digits)."""
    return f"{secrets.randbelow(900_000) + 100_000:06d}"


def format_task_id(day: Optional[date] = None, suffix: Optional[str] = None) -> str:
    """Render AIR-YYYY-MM-DD-NNNNNN for ``day`` (default: today)."""
    day = day or today()
    return f"{ID_PREFIX}-{day.year:04d}-{day.month:02d}-{day.day:02d}-{suffix or random_suffix()}"


def validate_task_id(task_id: str) -> bool:
    """True for a current (dated) or legacy (AIR-YYYY-NNNNNN) task ID."""
    if not task_id:
        return False
    dated = _DATED_ID.match(task_id)
    if dated:
        try:
            date(int(dated.group(1)), int(dated.group(2)), int(dated.group(3)))
        except ValueError:
            return False
        return True
    return _LEGACY_ID.match(task_id) is not None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def reserve_task_id(
    db: Any,
    day: Optional[date] = None,
    is_taken: Optional[Callable[[str], Any]] = None,
    max_attempts: int = 50,
) -> str:
    """Mint a fresh random ID and record it in the permanent ledger.

    The ledger row is written with the caller's session. Depending on the database, an ID drawn for a task
    that is never saved may stay burned, which is harmless (it is skipped, never reused). A collision (with a live task or any ID issued before) just draws a
    new random number.
    """
    from backend.app.models.tasks import IssuedTaskId

    for _ in range(max_attempts):
        candidate = format_task_id(day)
        if is_taken is not None and await _maybe_await(is_taken(candidate)):
            continue
        try:
            async with db.begin_nested():
                await db.execute(insert(IssuedTaskId).values(task_id=candidate))
        except IntegrityError:
            logger.warning("Task ID %s was issued before; drawing another", candidate)
            continue
        return candidate
    raise RuntimeError("Could not allocate a collision-free task ID after repeated attempts.")
