"""Small helpers so live worker log lines are greppable and never leak values.

Every run-level line uses the same ``KEY k=v k=v`` shape (``WORKER``, ``RUN``, ``STEP``,
``TOOL``, ``PARK``, ``REPLAN``, ``FINALIZE``, ``REPLY``), so one task can be followed with
``grep "task=T-123" logs/airdropos.log`` and an error can be traced to the step, tool and
iteration that produced it.

Parameter *values* are never logged here, only their names: a step's parameters can hold
addresses, keys or session data. Free text is collapsed to one line, clipped and passed
through the platform's redactor before it reaches a log line.
"""

from __future__ import annotations

from typing import Any, Iterable, List


def clip(value: Any, limit: int = 300) -> str:
    """One-line, redacted, length-bounded text for a log line. Never raises."""
    try:
        from backend.app.terminal.events import redact

        text = " ".join(str(value if value is not None else "").split())
        text = redact(text)
        return text if len(text) <= limit else text[: limit - 3] + "..."
    except Exception:  # logging must never break the run
        return "<unprintable>"


def param_keys(params: Any) -> List[str]:
    """Names of a tool's parameters, without their values."""
    try:
        return sorted(str(k) for k in (params or {}).keys())
    except Exception:
        return []


def count(items: Iterable[Any] | None) -> int:
    try:
        return len(list(items or []))
    except Exception:
        return 0
