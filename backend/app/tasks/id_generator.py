"""Persistent Task ID Generator.

Format: AIR-YYYY-NNNNNN  e.g. AIR-2026-000001

Collision safety is enforced with a PostgreSQL unique index on tasks.task_id plus
an in-process lock and a retry loop, so concurrent workers cannot mint the same ID.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ID_PREFIX = "AIR"
_id_lock = asyncio.Lock()


def current_year() -> int:
    return datetime.now(timezone.utc).year


def format_task_id(sequence: int, year: Optional[int] = None) -> str:
    """Render a sequence number as a persistent task ID."""
    year = year or current_year()
    if sequence < 1:
        raise ValueError("Task sequence numbers start at 1")
    return f"{ID_PREFIX}-{year}-{sequence:06d}"


def parse_task_id(task_id: str) -> tuple[int, int]:
    """Parse AIR-YYYY-NNNNNN into (year, sequence). Raises on malformed input."""
    if not task_id:
        raise ValueError("Task ID cannot be empty")
    parts = task_id.split("-")
    if len(parts) != 3 or parts[0] != ID_PREFIX:
        raise ValueError(f"Malformed task ID '{task_id}': expected format AIR-YYYY-NNNNNN")
    try:
        year = int(parts[1])
        sequence = int(parts[2])
    except ValueError as exc:
        raise ValueError(f"Malformed task ID '{task_id}': non-numeric year or sequence") from exc
    if not 2000 <= year <= 2999:
        raise ValueError(f"Malformed task ID '{task_id}': implausible year {year}")
    if not 1 <= sequence <= 999_999:
        raise ValueError(f"Malformed task ID '{task_id}': sequence out of range")
    return year, sequence


def validate_task_id(task_id: str) -> bool:
    try:
        parse_task_id(task_id)
        return True
    except ValueError:
        return False


async def next_sequence(
    max_sequence: int = 0,
    year: Optional[int] = None,
    is_taken: Optional[Callable[[str], Any]] = None,
    max_attempts: int = 25,
) -> int:
    """Atomically advance the sequence, skipping any IDs already in use.

    `is_taken` is a callable returning True when a candidate ID already exists in
    the database. It may be sync or async; async is preferred because it closes
    the race where two processes compute the same next ID before either commits.
    """
    year = year or current_year()
    async with _id_lock:
        candidate = max_sequence + 1
        attempts = 0
        while attempts < max_attempts:
            attempts += 1
            if candidate > 999_999:
                raise RuntimeError(
                    f"Task ID space for {year} is exhausted; archive old records or roll to a new year."
                )
            if is_taken is None or not await _maybe_await(is_taken(format_task_id(candidate, year))):
                return candidate
            logger.warning("Task ID %s already exists; advancing sequence", format_task_id(candidate, year))
            candidate += 1
        raise RuntimeError("Could not allocate a collision-free task ID after repeated attempts.")


async def _maybe_await(value: Any) -> Any:
    """Await `value` when it is awaitable, otherwise return it unchanged.

    Lets `next_sequence` accept both sync predicates (tests, in-memory state) and
    async ones (real database lookups) without forcing callers to wrap sync code.
    """
    if inspect.isawaitable(value):
        return await value
    return value
