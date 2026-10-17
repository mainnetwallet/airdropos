"""Safe per-call LLM context tracing.

What this answers: for one request the application actually sent, *which* task and plan
revision was it built for, *which* source records fed it, how big was the final request,
and did compaction or a retry change it on the way out.

What it never holds: prompt text, message content, tool arguments, credentials or page
content. A trace is ids, counts, lengths, category names and one digest of the final
serialised request. Anything free-form that reaches a trace field is reduced to a short
identifier-safe token first.

Propagation is a `ContextVar`, not a module global, so two tasks running concurrently in
one process each see only their own scope. A scope opened in a task is inherited by the
tasks it spawns (`asyncio.create_task` copies the context) and is restored on exit.

Storage lives in `backend.app.tasks.telemetry` beside the usage counters; this module only
builds the record. Telemetry failure never reaches the caller: every entry point here
swallows its own errors.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_.:\-]")
MAX_TOKEN_LEN = 64
MAX_IDS_PER_KIND = 200
MAX_WARNINGS = 20
MAX_MESSAGE_METRICS = 64


def safe_token(value: Any, *, limit: int = MAX_TOKEN_LEN) -> Optional[str]:
    """Reduce free text to an identifier-safe, bounded token (or None when empty)."""
    if value is None:
        return None
    text = _SAFE_TOKEN.sub("_", str(value).strip())[:limit]
    return text or None


@dataclass(frozen=True)
class LLMTraceScope:
    """Metadata an enclosing piece of work publishes for the requests made inside it."""

    purpose: Optional[str] = None
    task_id: Optional[str] = None
    conversation_id: Optional[str] = None
    execution_window: Optional[int] = None
    plan_revision: Optional[int] = None
    context_revision: Optional[int] = None
    context_generation: Optional[str] = None
    # kind -> ids, e.g. {"observations": [...], "requirements": [...], "memories": [...],
    # "checkpoints": [...]}. Ids only.
    source_ids: Dict[str, List[str]] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()
    # Sources the retrieval or budget logic wanted to include and could not.
    excluded_sources: Tuple[str, ...] = ()


_scope: contextvars.ContextVar[Optional[LLMTraceScope]] = contextvars.ContextVar(
    "airdropos_llm_trace_scope", default=None
)
def current_scope() -> LLMTraceScope:
    return _scope.get() or LLMTraceScope()


@contextmanager
def llm_trace_scope(**fields: Any) -> Iterator[LLMTraceScope]:
    """Open a scope. Unset fields are inherited from the enclosing scope; set ones override.

    `warnings`, `excluded_sources` and `source_ids` are merged rather than replaced, so a
    nested scope adds to what its parent already reported.
    """
    try:
        parent = current_scope()
        merged: Dict[str, Any] = {}
        for key, value in fields.items():
            if value is None or not hasattr(parent, key):
                continue
            if key in ("warnings", "excluded_sources"):
                merged[key] = tuple(getattr(parent, key)) + tuple(str(v) for v in value)
            elif key == "source_ids":
                combined = {k: list(v) for k, v in parent.source_ids.items()}
                for kind, ids in (value or {}).items():
                    combined[kind] = list(dict.fromkeys([*combined.get(kind, []), *map(str, ids)]))
                merged[key] = combined
            elif key == "purpose":
                merged[key] = safe_token(value)
            elif key in ("task_id", "conversation_id", "context_generation"):
                merged[key] = safe_token(value, limit=128)
            else:
                merged[key] = value
        scope = replace(parent, **merged)
    except Exception as exc:  # a malformed scope must not stop the work it describes
        logger.debug("Could not build LLM trace scope: %s", exc)
        scope = current_scope()
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


@dataclass
class LLMCallTrace:
    """One request, as it was finally sent."""

    trace_id: str
    purpose: str = "unlabelled"
    parent_trace_id: Optional[str] = None
    task_id: Optional[str] = None
    conversation_id: Optional[str] = None
    execution_window: Optional[int] = None
    plan_revision: Optional[int] = None
    context_revision: Optional[int] = None
    context_generation: Optional[str] = None
    source_ids: Dict[str, List[str]] = field(default_factory=dict)
    streaming: bool = False
    json_mode: bool = False
    message_count: int = 0
    roles: Dict[str, int] = field(default_factory=dict)
    message_chars: List[int] = field(default_factory=list)
    total_chars: int = 0
    estimated_tokens: Optional[int] = None
    request_sha256: Optional[str] = None
    compaction: Optional[Dict[str, Any]] = None
    overflow_retries: int = 0
    empty_content_retries: int = 0
    attempts: int = 0
    warnings: List[str] = field(default_factory=list)
    excluded_sources: List[str] = field(default_factory=list)
    outcome: str = "pending"
    error_category: Optional[str] = None
    requested_model: Optional[str] = None
    served_model: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "parent_trace_id": self.parent_trace_id,
            "purpose": self.purpose,
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "execution_window": self.execution_window,
            "plan_revision": self.plan_revision,
            "context_revision": self.context_revision,
            "context_generation": self.context_generation,
            "source_ids": {k: list(v) for k, v in self.source_ids.items()},
            "streaming": self.streaming,
            "json_mode": self.json_mode,
            "message_count": self.message_count,
            "roles": dict(self.roles),
            "message_chars": list(self.message_chars),
            "total_chars": self.total_chars,
            "estimated_tokens": self.estimated_tokens,
            "request_sha256": self.request_sha256,
            "compaction": self.compaction,
            "overflow_retries": self.overflow_retries,
            "empty_content_retries": self.empty_content_retries,
            "attempts": self.attempts,
            "warnings": list(self.warnings),
            "excluded_sources": list(self.excluded_sources),
            "outcome": self.outcome,
            "error_category": self.error_category,
            "requested_model": self.requested_model,
            "served_model": self.served_model,
        }


def new_trace(*, streaming: bool = False, json_mode: bool = False, purpose: Optional[str] = None,
              requested_model: Optional[str] = None,
              parent_trace_id: Optional[str] = None) -> LLMCallTrace:
    """Start a trace from the ambient scope. Never raises."""
    try:
        scope = current_scope()
        return LLMCallTrace(
            trace_id=uuid.uuid4().hex[:16],
            purpose=safe_token(purpose) or scope.purpose or "unlabelled",
            parent_trace_id=parent_trace_id,
            task_id=scope.task_id,
            conversation_id=scope.conversation_id,
            execution_window=scope.execution_window,
            plan_revision=scope.plan_revision,
            context_revision=scope.context_revision,
            context_generation=scope.context_generation,
            source_ids={k: list(v)[:MAX_IDS_PER_KIND] for k, v in scope.source_ids.items()},
            streaming=streaming,
            json_mode=json_mode,
            warnings=list(scope.warnings)[:MAX_WARNINGS],
            excluded_sources=list(scope.excluded_sources)[:MAX_WARNINGS],
            requested_model=requested_model,
        )
    except Exception:  # pragma: no cover - defensive
        return LLMCallTrace(trace_id=uuid.uuid4().hex[:16])


def describe_final_request(trace: LLMCallTrace, messages: Sequence[Any],
                           estimated_tokens: Optional[int] = None) -> None:
    """Fill in the size, role, id and digest fields from the messages ACTUALLY being sent.

    Called with the post-compaction list, and again after an overflow retry rebuilds it, so
    the trace describes the final request and not the one the caller first assembled.
    """
    try:
        roles: Dict[str, int] = {}
        sizes: List[int] = []
        total = 0
        digest = hashlib.sha256()
        tool_call_ids: List[str] = []
        for message in messages:
            role = getattr(message, "role", "?")
            content = getattr(message, "content", "") or ""
            roles[role] = roles.get(role, 0) + 1
            size = len(content) if isinstance(content, str) else len(json.dumps(content, default=str))
            sizes.append(size)
            total += size
            payload = message.to_payload() if hasattr(message, "to_payload") else {"role": role}
            digest.update(json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
            call_id = getattr(message, "tool_call_id", None)
            if call_id:
                tool_call_ids.append(safe_token(call_id, limit=128) or "")
        trace.message_count = len(messages)
        trace.roles = roles
        trace.message_chars = sizes[:MAX_MESSAGE_METRICS]
        trace.total_chars = total
        trace.estimated_tokens = estimated_tokens
        trace.request_sha256 = digest.hexdigest()[:16]
        if tool_call_ids:
            trace.source_ids["tool_invocations"] = list(dict.fromkeys(tool_call_ids))[:MAX_IDS_PER_KIND]
    except Exception as exc:  # pragma: no cover - telemetry is best effort
        logger.debug("Could not describe LLM request for tracing: %s", exc)


def finish_trace(trace: LLMCallTrace, *, outcome: str, error_category: Optional[str] = None,
                 served_model: Optional[str] = None) -> None:
    """Close a trace and hand it to the telemetry store. Never raises."""
    try:
        trace.outcome = outcome
        trace.error_category = safe_token(error_category)
        trace.served_model = safe_token(served_model, limit=128)
        from backend.app.tasks.telemetry import record_llm_trace

        record_llm_trace(trace)
    except Exception as exc:  # pragma: no cover - telemetry is best effort
        logger.debug("Could not record LLM trace: %s", exc)
