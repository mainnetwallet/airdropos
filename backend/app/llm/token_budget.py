"""Input-context token accounting for the single fixed LLM.

This module answers three questions and nothing else:

  1. How many input tokens will this request cost?
  2. How many input tokens may this request cost?
  3. How close was the estimate to what the provider actually billed?

It never decides what to remove. That is `compaction.py`'s job, and keeping the
two apart is what stops a token counter from ever becoming a content filter.

WHY THERE IS NO TOKENIZER DEPENDENCY
------------------------------------
The configured endpoint is a router: the model id in the configuration is not
in its catalog and the served model varies per request. Any exact tokenizer
would be an exact tokenizer for the *wrong* model, so it would be confidently
inaccurate. A documented, deliberately conservative estimator is more honest
here than a precise-looking wrong number, and it adds no dependency.

MEASURED BASIS FOR THE ESTIMATOR
--------------------------------
Against the configured endpoint, a prompt of 720,000 characters reported
``usage.prompt_tokens == 160017``, i.e. about 4.50 characters per token. We
count at ``LLM_CONTEXT_ESTIMATOR_CHARS_PER_TOKEN`` (default 3.2), so a request
of ordinary prose is counted roughly 40% ABOVE its true cost. The bias is in
the safe direction: an over-count costs a little headroom, an under-count costs
a failed request. Code and dense JSON tokenize less efficiently than prose, so
the same ratio still over-counts them.

LIMITATION, stated plainly: this is an estimate, not a count. The compaction
path re-counts and the client re-validates before submitting, so an estimate is
never the last thing standing between a request and the provider.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

# Per-message framing the estimator must assume even for an empty message:
# the role token, the separators, and the closing turn marker.
_TOKENS_PER_MESSAGE = 4

# The JSON envelope around the messages array (model, temperature, object type).
_TOKENS_PER_PAYLOAD = 8

# Fractions used when an operator did not pin an explicit threshold.
_TRIGGER_FRACTION = 0.80
_TARGET_FRACTION = 0.62


def estimate_text_tokens(text: Optional[str]) -> int:
    """Estimate the tokens one string of text will cost."""
    if not text:
        return 0
    chars_per_token = settings.LLM_CONTEXT_ESTIMATOR_CHARS_PER_TOKEN
    # Round UP, with a floor of 1: any non-empty text costs at least a token,
    # and rounding must never be in the optimistic direction.
    return max(1, math.ceil(len(text) / chars_per_token))


def _message_tokens(message: Any) -> int:
    """Estimate one message's cost, including its role and framing."""
    content = getattr(message, "content", None)
    total = _TOKENS_PER_MESSAGE + estimate_text_tokens(content)
    name = getattr(message, "name", None)
    if name:
        total += estimate_text_tokens(name)
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id:
        total += estimate_text_tokens(tool_call_id)
    return total


def count_request_tokens(messages: Sequence[Any]) -> int:
    """Estimate the input tokens of a whole chat request.

    Accepts anything with ``.role`` and ``.content`` (i.e. `Message`) so this
    stays testable without importing the client, which would be circular.
    """
    return _TOKENS_PER_PAYLOAD + sum(_message_tokens(m) for m in messages)


@dataclass(frozen=True)
class TokenBudget:
    """The validated input-context budget for one request lifecycle."""

    context_window: int
    reserved_output: int
    safety_margin: int
    usable_input: int
    trigger: int
    target: int

    @classmethod
    def from_settings(cls, cfg: Any = None) -> "TokenBudget":
        cfg = cfg or settings
        usable = (
            cfg.LLM_CONTEXT_WINDOW_TOKENS
            - cfg.LLM_CONTEXT_RESERVED_OUTPUT_TOKENS
            - cfg.LLM_CONTEXT_SAFETY_MARGIN_TOKENS
        )
        trigger = cfg.LLM_CONTEXT_COMPACTION_TRIGGER_TOKENS or int(usable * _TRIGGER_FRACTION)
        target = cfg.LLM_CONTEXT_COMPACTION_TARGET_TOKENS or int(usable * _TARGET_FRACTION)
        return cls(
            context_window=cfg.LLM_CONTEXT_WINDOW_TOKENS,
            reserved_output=cfg.LLM_CONTEXT_RESERVED_OUTPUT_TOKENS,
            safety_margin=cfg.LLM_CONTEXT_SAFETY_MARGIN_TOKENS,
            usable_input=usable,
            trigger=trigger,
            target=target,
        )

    def describe(self) -> Dict[str, int]:
        """Safe, content-free description for diagnostics and the dashboard."""
        return {
            "context_window": self.context_window,
            "reserved_output": self.reserved_output,
            "safety_margin": self.safety_margin,
            "usable_input": self.usable_input,
            "trigger": self.trigger,
            "target": self.target,
        }


# ----------------------------------------------------------------------
# Estimator accuracy
# ----------------------------------------------------------------------
# Usage telemetry is best-effort by design: a metrics failure must never break
# an otherwise valid request, so every write here is guarded and the counters
# are plain process-local values.


@dataclass
class _UsageTotals:
    samples: int = 0
    estimated: int = 0
    actual: int = 0

    @property
    def ratio(self) -> Optional[float]:
        return (self.actual / self.estimated) if self.estimated else None


_usage_totals = _UsageTotals()


def record_usage(estimated_input_tokens: int, usage: Optional[Dict[str, Any]]) -> None:
    """Fold one provider-reported usage block into the accuracy totals.

    Tolerates a missing, partial or oddly typed ``usage``: telemetry is never
    allowed to break a request.
    """
    try:
        if not isinstance(usage, dict):
            return
        actual = usage.get("prompt_tokens", usage.get("input_tokens"))
        if actual is None:
            return
        actual = int(actual)
    except (TypeError, ValueError):
        return

    if estimated_input_tokens <= 0 or actual <= 0:
        return

    _usage_totals.samples += 1
    _usage_totals.estimated += estimated_input_tokens
    _usage_totals.actual += actual

    ratio = _usage_totals.ratio
    if ratio is not None and (
        _usage_totals.samples in (1, 25) or _usage_totals.samples % 100 == 0
    ):
        # Only ever logged, never acted on: the budget must not change
        # underneath a running task because a counter moved.
        logger.info(
            "llm.token_budget estimator accuracy after %s samples: estimated=%s actual=%s "
            "actual/estimated=%.3f",
            _usage_totals.samples,
            _usage_totals.estimated,
            _usage_totals.actual,
            ratio,
        )


def usage_snapshot() -> Dict[str, Any]:
    """Current estimator-accuracy totals, for the system status endpoint."""
    return {
        "samples": _usage_totals.samples,
        "estimated_input_tokens": _usage_totals.estimated,
        "actual_input_tokens": _usage_totals.actual,
        "actual_over_estimated": _usage_totals.ratio,
    }


def reset_usage_snapshot() -> None:
    """Test hook."""
    _usage_totals.samples = 0
    _usage_totals.estimated = 0
    _usage_totals.actual = 0