"""Choosing how long a single tool invocation may take.

Every tool declares a `timeout_seconds` default, which is the right answer on average and
the wrong answer for the interesting cases. A browser tool that waits for a slow page load
and one that reads a cached DOM element cannot share a number. Nor can the very first
invocation of a tool and the fiftieth, once its real latency is known.

What this decides, and why each input earns its place:

* **The tool's own declared default.** The author of the tool knows more about it than any
  heuristic here does, so this is the base and never overridden downward silently.
* **Tool class.** A local arithmetic call, an outbound HTTP read, a browser page that has
  to render, and a Telegram round trip have genuinely different floors. Class policy only
  ever raises the floor for a class that is structurally slower; it does not shorten a
  tool's own deliberate value.
* **Observed latency.** Rolling per-tool observations, used to give a consistently slow
  tool room. Bounded, and only ever applied upward, and only after enough samples that a
  single outlier cannot move it.
* **The caller's remaining budget.** An invocation cannot be allowed to run longer than
  the window it is running in, or it outlives the thing that is waiting for it. This is a
  cap, never a floor.

What this deliberately does NOT do: accept an arbitrary number. `resolve_timeout` clamps a
requested value into `[min_allowed, max_allowed]` where both bounds come from policy, so a
caller cannot ask for an unbounded wait, and a malformed request degrades to the computed
default rather than raising into the middle of a tool dispatch.

History is process-local and deliberately lossy. It is a latency hint, not an audit
record, and losing it on restart costs at most one slow invocation before the default
applies again.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# Absolute ceiling for any single tool invocation, whatever any caller requests.
# A wait longer than this is a hung dependency, not a slow one.
HARD_TIMEOUT_CEILING_SECONDS = 600

# Floor for any single tool invocation. Below this a tool cannot have done any network
# or browser work at all, so a shorter wait only converts real work into a false timeout.
HARD_TIMEOUT_FLOOR_SECONDS = 5

# The absolute shortest wait the window cap may impose. Lower than the floor above,
# deliberately: a task at the very end of its execution window should get a SHORT wait,
# not a comfortable one. It is not zero, because cancelling every in-flight tool the
# moment a window closes would abandon operations whose outcome nobody then knows.
HARD_TIMEOUT_MINIMUM_SECONDS = 2.0

# Samples required before observed latency is allowed to influence anything. One slow
# call is an outlier; it is not evidence about the tool.
MIN_SAMPLES_FOR_ADAPTATION = 3

# How many recent observations to keep per tool.
LATENCY_WINDOW = 32


class TimeoutPolicy:
    """Per-tool-class bounds and multipliers applied on top of a declared default."""

    __slots__ = ("floor", "ceiling", "multiplier")

    def __init__(
        self,
        floor: float,
        ceiling: float,
        multiplier: float = 1.0,
    ) -> None:
        self.floor = floor
        self.ceiling = ceiling
        self.multiplier = multiplier


# One entry per structural class of work. The multiplier is unused and stays 1.0.
#
# The CEILING here DOES bound a tool's own declared value, and that is intended: a
# class-wide ceiling is what stops a single over-declared tool from pinning a worker.
# When it does, the reason string says `class_ceiling=`, so a clamp is visible in the log
# rather than looking like a tool that simply runs fast.
CLASS_POLICIES: Dict[str, TimeoutPolicy] = {
    # Pure local computation. Nothing to wait for.
    "local": TimeoutPolicy(floor=5, ceiling=30),
    # Outbound HTTP/JSON. Remote latency dominates.
    "network": TimeoutPolicy(floor=10, ceiling=180),
    # A browser: process launch, navigation, rendering and asset loading.
    "browser": TimeoutPolicy(floor=15, ceiling=300),
    # Messaging round trips, which are latency-bound and rate-limited upstream.
    "telegram": TimeoutPolicy(floor=10, ceiling=240),
    # Blocking work (sending mail, pushing a notification, waiting on a job).
    "blocking": TimeoutPolicy(floor=15, ceiling=300),
}

DEFAULT_CLASS = "network"


def tool_class_for(tool) -> str:
    """Best-effort structural class of a tool instance.

    Classification is by declared attributes rather than by name prefix, so a new tool is
    covered the moment it declares a browser or transaction side effect, and a tool that
    does not declare anything falls back to the most common class.
    """
    side_effect = getattr(tool, "side_effect", None)
    value = getattr(side_effect, "value", side_effect)
    if isinstance(value, str):
        lowered = value.lower()
        if "browser" in lowered or "web" in lowered:
            return "browser"
        if "transaction" in lowered or "sign" in lowered or "mutation" in lowered:
            return "blocking"
    module = (getattr(tool, "__module__", "") or "").lower()
    if "browser" in module or "miniapp" in module:
        return "browser"
    if "telegram" in module:
        return "telegram"
    if "arithmetic" in module:
        return "local"
    return DEFAULT_CLASS


class LatencyHistory:
    """Rolling observed durations per tool, used to give a slow tool room."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: Dict[str, Deque[float]] = {}

    def observe(self, tool_name: str, seconds: float) -> None:
        if seconds < 0:
            return
        with self._lock:
            self._samples.setdefault(tool_name, deque(maxlen=LATENCY_WINDOW)).append(seconds)

    def _sorted(self, tool_name: str) -> list:
        with self._lock:
            samples = list(self._samples.get(tool_name, ()))
        samples.sort()
        return samples

    def high_estimate(self, tool_name: str) -> Optional[float]:
        """The 90th-percentile observed duration, or None when there is too little data."""
        samples = self._sorted(tool_name)
        if len(samples) < MIN_SAMPLES_FOR_ADAPTATION:
            return None
        index = min(len(samples) - 1, int(round(0.90 * (len(samples) - 1))))
        return samples[index]

    def sample_count(self, tool_name: str) -> int:
        with self._lock:
            return len(self._samples.get(tool_name, ()))

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()


history = LatencyHistory()


def resolve_timeout(
    tool,
    *,
    requested: Optional[float] = None,
    remaining_budget: Optional[float] = None,
) -> Tuple[float, str]:
    """Decide this invocation's timeout. Returns `(seconds, why)`.

    `requested` is a caller's justified request for more room. It is CLAMPED, never
    obeyed literally: a caller asking for 100000s gets the hard ceiling, and a caller
    asking for -5 or for a string gets the computed default instead of raising into the
    middle of a dispatch.
    """
    declared_raw = getattr(tool, "timeout_seconds", 60)
    try:
        declared = float(declared_raw or 60)
    except (TypeError, ValueError):
        # A tool with an unusable declared timeout still gets a working default rather
        # than an exception raised inside a dispatch, halfway through an invocation.
        logger.warning(
            "TOOL declared_timeout_invalid name=%s value=%r; using the class default",
            getattr(tool, "name", "?"), declared_raw,
        )
        declared = 60.0
    tool_name = getattr(tool, "name", "?")
    tool_class = tool_class_for(tool)
    policy = CLASS_POLICIES.get(tool_class, CLASS_POLICIES[DEFAULT_CLASS])

    # Base: the tool author's value, lifted to the class floor so a structurally slow
    # class is not cut off by an optimistic default.
    seconds = max(declared, policy.floor)
    reasons = [f"declared={declared:g}s"]

    if declared < policy.floor:
        reasons.append(f"class_floor={policy.floor:g}s")

    # Observed latency. Only ever raises, and only once there is enough evidence for the
    # tail to mean anything.
    p90 = history.high_estimate(tool_name)
    if p90 is not None and p90 > seconds:
        # 1.5x headroom over the observed tail, so a normally slow tool is not timed out
        # by its own worst ordinary case.
        adapted = p90 * 1.5
        if adapted > seconds:
            seconds = adapted
            reasons.append(f"observed_p90={p90:.1f}s")

    if seconds > policy.ceiling:
        # The class ceiling DOES bound a tool's own declared value, so say so: a silent
        # shortening of a value the tool author chose is indistinguishable, in the log,
        # from the tool simply being fast.
        reasons.append(f"class_ceiling={policy.ceiling:g}s")
    seconds = min(seconds, policy.ceiling)
    if seconds >= HARD_TIMEOUT_CEILING_SECONDS:
        reasons.append("hard_ceiling")
    seconds = min(seconds, HARD_TIMEOUT_CEILING_SECONDS)

    # A justified request for more room, still clamped to the same bounds.
    if requested is not None:
        try:
            asked = float(requested)
        except (TypeError, ValueError):
            logger.warning(
                "TOOL timeout_request_invalid name=%s requested=%r; using the computed default",
                tool_name, requested,
            )
        else:
            if asked <= 0:
                logger.warning(
                    "TOOL timeout_request_invalid name=%s requested=%g; using the computed default",
                    tool_name, asked,
                )
            elif asked > seconds:
                reasons.append(f"requested={asked:g}s")
                seconds = min(asked, policy.ceiling, HARD_TIMEOUT_CEILING_SECONDS)

    # The window this invocation runs in cannot be shorter than the invocation, or the
    # invocation outlives the thing waiting for it. Only ever a cap.
    #
    # This cap is allowed to go BELOW the class floor, down to a small absolute minimum:
    # a cap that could never do that would not be a cap. Near the end of a window a tool
    # gets a short wait rather than being allowed to overrun the window entirely.
    if remaining_budget is not None:
        try:
            cap = float(remaining_budget)
        except (TypeError, ValueError):
            logger.warning(
                "TOOL remaining_budget_invalid name=%s value=%r; ignoring it",
                tool_name, remaining_budget,
            )
        else:
            if cap > 0 and cap < seconds:
                reasons.append(f"window_cap={cap:.1f}s")
                seconds = max(cap, HARD_TIMEOUT_MINIMUM_SECONDS)

    return round(max(seconds, HARD_TIMEOUT_MINIMUM_SECONDS), 3), ",".join(reasons)


__all__ = [
    "CLASS_POLICIES",
    "DEFAULT_CLASS",
    "HARD_TIMEOUT_CEILING_SECONDS",
    "HARD_TIMEOUT_FLOOR_SECONDS",
    "HARD_TIMEOUT_MINIMUM_SECONDS",
    "MIN_SAMPLES_FOR_ADAPTATION",
    "LatencyHistory",
    "history",
    "resolve_timeout",
    "tool_class_for",
]