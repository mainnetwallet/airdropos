"""Per-task execution telemetry.

Answers the questions an operator actually asks when a task stops: how much reasoning did
this cost, how many tools ran, how often did it replan, did it stall, and what was the
blocker. All of it is **cumulative for the lifetime of the task** and written to columns on
the `Task` row, so the numbers survive an execution window boundary and a worker restart
instead of being overwritten by whatever the most recent run happened to report.

Two hard rules, both enforced here rather than at the call sites:

* **Unknown is not zero.** When the provider reports no usage block, the token figures stay
  ``None`` and a separate `without_usage` counter moves. Writing ``0`` would understate
  spend on exactly the endpoints that omit usage, and would make a later report
  indistinguishable from a task that genuinely used nothing.
* **Requested model is not served model.** Both are kept. The configured id is what the
  application requested; what the provider reported is a separate fact, and the difference
  is recorded rather than assumed away.

Nothing here is a secret: only counts, token figures and model identifiers are stored. No
prompts, no tool arguments, no credentials.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)


@dataclass
class TaskUsage:
    """Accumulated AI/tool usage for one task, since its counters began."""

    llm_requests: int = 0
    # None means "the provider did not report this", which is not the same as zero.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    # A provider that reports input tokens but omits output tokens has NOT reported zero
    # output tokens. Adding the missing side as 0 turns an unknown into a fabricated
    # figure and understates cost with no trace. These counters make the subtotals above
    # readable as a FLOOR rather than a total: if a counter is non-zero, at least that
    # many calls contributed an amount nobody can name.
    unknown_input_calls: int = 0
    unknown_output_calls: int = 0
    calls_without_usage: int = 0
    served_models: set = field(default_factory=set)
    mismatches: int = 0
    last_served_model: Optional[str] = None

    @property
    def totals_are_exact(self) -> bool:
        """True only when every recorded call reported both token counts."""
        return self.unknown_input_calls == 0 and self.unknown_output_calls == 0

    def merge_usage(self, usage: Optional[Dict[str, Any]]) -> None:
        """Fold one provider usage block in, keeping "unknown" distinct from zero."""
        if not isinstance(usage, dict) or not usage:
            self.calls_without_usage += 1
            self.unknown_input_calls += 1
            self.unknown_output_calls += 1
            return

        def _read(*keys: str) -> Optional[int]:
            for key in keys:
                value = usage.get(key)
                if value is None:
                    continue
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
            return None

        prompt = _read("prompt_tokens", "input_tokens")
        completion = _read("completion_tokens", "output_tokens")

        if prompt is None and completion is None:
            self.calls_without_usage += 1
            self.unknown_input_calls += 1
            self.unknown_output_calls += 1
            return

        # Each side is folded independently. A missing side is counted as unknown and
        # contributes NOTHING to the subtotal, so the subtotal never claims to be more
        # complete than the provider's reporting actually was.
        if prompt is None:
            self.unknown_input_calls += 1
        else:
            self.input_tokens = (self.input_tokens or 0) + prompt

        if completion is None:
            self.unknown_output_calls += 1
        else:
            self.output_tokens = (self.output_tokens or 0) + completion

    def merge_model(self, *, requested_model: str, served_model: Optional[str]) -> None:
        """Record what the provider said it served, beside what was asked for."""
        if served_model:
            # Provider-controlled text going into a sized column: bounded here rather than
            # letting a long value raise a DataError on PostgreSQL and take the whole
            # telemetry flush down with it.
            served_model = served_model[:128]
            self.served_models.add(served_model)
            self.last_served_model = served_model
            if served_model != requested_model and not served_model.startswith(
                f"{requested_model}-"
            ):
                # A dated variant of the same model is normal; a different model is not.
                self.mismatches += 1


def aggregate_usage_blocks(
    blocks: Sequence[Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Sum several provider usage blocks into one, per token direction.

    Each direction is summed only over the blocks that actually reported it. A block that
    omits `completion_tokens` says nothing about output cost, and subtracting or zeroing
    it would both overstate precision and misreport a partial answer as a free one. Where
    any block was silent the aggregate carries an explicit `_partial` marker plus the
    number of silent blocks, so a consumer can tell "the model generated nothing" from
    "the provider declined to say".

    Retries are never folded in twice: the caller decides which blocks correspond to
    distinct physical requests, and this function only adds.
    """
    total = TaskUsage()
    for block in blocks:
        total.merge_usage(block)

    result: Dict[str, Any] = {"requests": len(blocks)}
    # `None` here means "nobody reported it at all", which is different from 0.
    result["prompt_tokens"] = total.input_tokens
    result["completion_tokens"] = total.output_tokens
    if not total.totals_are_exact:
        result["partial"] = True
        result["blocks_missing_input_tokens"] = total.unknown_input_calls
        result["blocks_missing_completion_tokens"] = total.unknown_output_calls
    return result


class _TaskRegistry:
    """Process-local accumulation, keyed by public task id.

    Deliberately not a lock-protected singleton: the values are telemetry, so losing a
    sample must never fail a request, and the authoritative totals are re-written to the
    task row on every execution window. The lock keeps concurrent worker slots from
    interleaving a read-modify-write on the same task.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_task: Dict[str, TaskUsage] = {}

    def get(self, task_id: str) -> TaskUsage:
        key = task_id.strip().upper()
        with self._lock:
            return self._by_task.setdefault(key, TaskUsage())

    def take(self, task_id: str) -> TaskUsage:
        """Atomically read and remove a task's accumulated usage.

        Read-then-remove as two steps loses whatever a concurrent `record_llm_call` adds in
        between, so a background extraction finishing at the wrong moment would silently
        under-report. Doing both under the lock cannot: the count is either in the returned
        snapshot or still in the registry, never neither.
        """
        key = task_id.strip().upper()
        with self._lock:
            return self._by_task.pop(key, None) or TaskUsage()

    def peek(self, task_id: str) -> TaskUsage:
        key = task_id.strip().upper()
        with self._lock:
            found = self._by_task.get(key)
        return found if found is not None else TaskUsage()

    def clear(self, task_id: Optional[str] = None) -> None:
        with self._lock:
            if task_id is None:
                self._by_task.clear()
            else:
                self._by_task.pop(task_id.strip().upper(), None)

    def restore(self, task_id: str, usage: TaskUsage) -> None:
        """Put a taken snapshot back, if persisting it failed.

        Losing the counts would under-report spend silently, so a failed flush returns
        them and they are counted again on the next attempt.
        """
        key = task_id.strip().upper()
        with self._lock:
            existing = self._by_task.setdefault(key, TaskUsage())
            existing.llm_requests += usage.llm_requests
            existing.calls_without_usage += usage.calls_without_usage
            existing.unknown_input_calls += usage.unknown_input_calls
            existing.unknown_output_calls += usage.unknown_output_calls
            existing.mismatches += usage.mismatches
            if usage.input_tokens is not None:
                existing.input_tokens = (existing.input_tokens or 0) + usage.input_tokens
            if usage.output_tokens is not None:
                existing.output_tokens = (existing.output_tokens or 0) + usage.output_tokens
            if usage.last_served_model:
                existing.last_served_model = usage.last_served_model
                existing.served_models.update(usage.served_models)


registry = _TaskRegistry()


def record_llm_call(
    *,
    usage: Optional[Dict[str, Any]],
    requested_model: str,
    served_model: Optional[str],
    task_id: Optional[str] = None,
) -> None:
    """Record one application-generated model request.

    Safe to call on every path, including failures: telemetry must never break a request,
    and a request the provider rejected still cost a round trip.
    """
    try:
        target = registry.get(task_id) if task_id else None
        if target is None:
            return
        target.llm_requests += 1
        target.merge_usage(usage)
        target.merge_model(requested_model=requested_model, served_model=served_model)
    except Exception as exc:  # pragma: no cover - telemetry is best effort
        logger.debug("Could not record LLM usage: %s", exc)


def reset_task_usage(task_id: Optional[str] = None) -> None:
    """Test hook, and the seam a worker uses after it has persisted the totals."""
    registry.clear(task_id)