"""Ambient execution context for tools.

``ToolRegistry.invoke`` is told which task a call belongs to (the public
``AIR-YYYY-NNNNNN`` id), but a tool's ``execute`` only receives its validated
parameters. Rather than widen every tool schema (which the model could then forge),
the registry publishes the task id here for the duration of the call and tools read it.

The value is never taken from model-supplied arguments, so the agent cannot attach
evidence to a task it is not running.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_current_task_id: ContextVar[Optional[str]] = ContextVar("airdropos_current_task_id", default=None)


def get_current_task_id() -> Optional[str]:
    """Public id of the task the running tool call belongs to, or None (e.g. operator chat)."""
    value = _current_task_id.get()
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


@contextmanager
def task_context(task_id: Optional[str]) -> Iterator[None]:
    """Publish ``task_id`` to tools for the duration of the ``with`` block."""
    token = _current_task_id.set(task_id)
    try:
        yield
    finally:
        _current_task_id.reset(token)
