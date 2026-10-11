"""Failure Diagnosis and Recovery Engine (Specification Part 22).

Classifies errors into structured categories, decides retryability in
deterministic code, records the diagnosis, and selects a recovery strategy.
Never retries a permanent failure and never blindly repeats an irreversible
operation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import FailureRecord, Task
from backend.app.tasks.state_machine import TaskStatus

logger = logging.getLogger(__name__)


class ErrorCategory(str, Enum):
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    RATE_LIMITING = "RATE_LIMITING"
    AUTH_FAILURE = "AUTH_FAILURE"
    BROWSER_SESSION_EXPIRED = "BROWSER_SESSION_EXPIRED"
    INVALID_INPUT = "INVALID_INPUT"
    MISSING_PERMISSION = "MISSING_PERMISSION"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    RPC_FAILURE = "RPC_FAILURE"
    TX_REJECTED = "TX_REJECTED"
    TX_REVERTED = "TX_REVERTED"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    NONCE_CONFLICT = "NONCE_CONFLICT"
    CAMPAIGN_EXPIRED = "CAMPAIGN_EXPIRED"
    ELIGIBILITY_FAILURE = "ELIGIBILITY_FAILURE"
    TOOL_FAILURE = "TOOL_FAILURE"
    DATABASE_FAILURE = "DATABASE_FAILURE"
    EXTERNAL_SERVICE_OUTAGE = "EXTERNAL_SERVICE_OUTAGE"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    HUMAN_INTERVENTION_REQUIRED = "HUMAN_INTERVENTION_REQUIRED"
    UNKNOWN = "UNKNOWN"


# Categories worth another attempt, with escalating backoff.
RETRYABLE = {
    ErrorCategory.NETWORK_TIMEOUT,
    ErrorCategory.RATE_LIMITING,
    ErrorCategory.RPC_FAILURE,
    ErrorCategory.EXTERNAL_SERVICE_OUTAGE,
    ErrorCategory.NONCE_CONFLICT,
}

# Categories that will never succeed on retry without a state change.
PERMANENT = {
    ErrorCategory.INVALID_INPUT,
    ErrorCategory.MISSING_PERMISSION,
    ErrorCategory.UNSUPPORTED_OPERATION,
    ErrorCategory.CAMPAIGN_EXPIRED,
    ErrorCategory.ELIGIBILITY_FAILURE,
    ErrorCategory.TX_REVERTED,
    ErrorCategory.EMERGENCY_STOP,
    ErrorCategory.HUMAN_INTERVENTION_REQUIRED,
}

# Categories where the operator must intervene.
REQUIRES_HUMAN = {
    ErrorCategory.AUTH_FAILURE,
    ErrorCategory.BROWSER_SESSION_EXPIRED,
    ErrorCategory.HUMAN_INTERVENTION_REQUIRED,
}

# Substring signatures, checked in order (first match wins).
_SIGNATURES: List[tuple[str, ErrorCategory]] = [
    (r"timed out|timeout", ErrorCategory.NETWORK_TIMEOUT),
    (r"rate limit|429|too many requests", ErrorCategory.RATE_LIMITING),
    (r"401|403|unauthorized|forbidden|authentication", ErrorCategory.AUTH_FAILURE),
    (r"session expired|please log ?in|sign in to continue", ErrorCategory.BROWSER_SESSION_EXPIRED),
    (r"captcha|human verification|verify you are human", ErrorCategory.HUMAN_INTERVENTION_REQUIRED),
    (r"two-factor|2-step|authenticator|otp", ErrorCategory.HUMAN_INTERVENTION_REQUIRED),
    (r"missing required parameter|unexpected parameter|must be a|must be an", ErrorCategory.INVALID_INPUT),
    (r"not authorized|permission", ErrorCategory.MISSING_PERMISSION),
    (r"unavailable|no tool named|unsupported", ErrorCategory.UNSUPPORTED_OPERATION),
    (r"emergency stop", ErrorCategory.EMERGENCY_STOP),
    (r"insufficient balance|insufficient funds", ErrorCategory.INSUFFICIENT_BALANCE),
    (r"nonce", ErrorCategory.NONCE_CONFLICT),
    (r"revert|execution reverted", ErrorCategory.TX_REVERTED),
    (r"rejected|underpriced|replacement transaction underpriced", ErrorCategory.TX_REJECTED),
    (r"rpc|could not connect to|connection refused", ErrorCategory.RPC_FAILURE),
    (r"expired|claim period closed|no longer active", ErrorCategory.CAMPAIGN_EXPIRED),
    (r"not eligible|ineligible", ErrorCategory.ELIGIBILITY_FAILURE),
    (r"database|sqlalchemy|connection pool", ErrorCategory.DATABASE_FAILURE),
]


@dataclass
class Diagnosis:
    """Structured classification of a failure."""

    category: ErrorCategory
    retryable: bool
    requires_human: bool
    summary: str
    recovery_strategy: str
    backoff_seconds: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "retryable": self.retryable,
            "requires_human": self.requires_human,
            "summary": self.summary,
            "recovery_strategy": self.recovery_strategy,
            "backoff_seconds": self.backoff_seconds,
        }


class FailureDiagnosisEngine:
    """Classifies errors and selects recovery strategies."""

    def classify(self, message: str, error_category: Optional[str] = None) -> ErrorCategory:
        """Map an error message to a structured category."""
        if error_category:
            for category in ErrorCategory:
                if category.value == error_category.upper():
                    return category

        lowered = (message or "").lower()
        for pattern, category in _SIGNATURES:
            if re.search(pattern, lowered):
                return category
        return ErrorCategory.UNKNOWN

    def diagnose(
        self,
        message: str,
        error_category: Optional[str] = None,
        attempt: int = 0,
    ) -> Diagnosis:
        """Produce a full diagnosis with a deterministic recovery strategy."""
        category = self.classify(message, error_category)
        retryable = category in RETRYABLE
        requires_human = category in REQUIRES_HUMAN

        backoff = 0
        if retryable:
            backoff = min(300, 2 ** max(0, attempt))

        if requires_human:
            strategy = (
                "Park the task in NEEDS_USER_ACTION, checkpoint progress, notify the operator, "
                "and resume once the blocking condition is resolved. Do not attempt to bypass "
                "CAPTCHA, MFA, or access controls."
            )
            retryable = False
        elif category is ErrorCategory.MISSING_PERMISSION:
            strategy = (
                "Do not retry. The operator must grant the missing permission, or the plan "
                "must be replanned around a permitted alternative."
            )
        elif category is ErrorCategory.CAMPAIGN_EXPIRED:
            strategy = "Do not retry. Mark the campaign expired and notify the operator."
        elif category is ErrorCategory.TX_REVERTED:
            strategy = (
                "Do not retry the same call. The on-chain state proves it would revert again. "
                "Replan with corrected parameters, and verify before resubmitting."
            )
        elif category is ErrorCategory.NONCE_CONFLICT:
            strategy = "Re-read the pending nonce and retry with the corrected value."
        elif category is ErrorCategory.RATE_LIMITING:
            strategy = f"Back off for {backoff}s, then retry. Reduce the request rate meanwhile."
        elif category is ErrorCategory.EXTERNAL_SERVICE_OUTAGE:
            strategy = (
                f"Back off for {backoff}s and retry. Stop dispatching new work to this "
                "subsystem while it is degraded."
            )
        elif category in (ErrorCategory.TX_REJECTED, ErrorCategory.INSUFFICIENT_BALANCE):
            strategy = "Do not retry unchanged. Replan once the underlying condition is resolved."
        else:
            strategy = "Retry once with backoff. Escalate to manual review if it fails again."

        summary = f"{category.value}: {(message or '')[:300]}"

        return Diagnosis(
            category=category,
            retryable=retryable,
            requires_human=requires_human,
            summary=summary,
            recovery_strategy=strategy,
            backoff_seconds=backoff,
        )

    # ------------------------------------------------------------------
    async def record(
        self,
        db: AsyncSession,
        task: Optional[Task],
        diagnosis: Diagnosis,
        execution_id: Optional[str] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
        retry_count: int = 0,
        final_outcome: Optional[str] = None,
    ) -> FailureRecord:
        """Persist the diagnosis so failure patterns are analysable later."""
        record = FailureRecord(
            task_id=task.id if task else None,
            execution_id=execution_id,
            error_category=diagnosis.category.value,
            error_message=diagnosis.summary[:2000],
            # Diagnostics are screened so no secret can reach the error log.
            diagnostics=_sanitize_diagnostics(diagnostics or {}),
            is_retryable=diagnosis.retryable,
            retry_count=retry_count,
            recovery_strategy=diagnosis.recovery_strategy,
            final_outcome=final_outcome,
        )
        db.add(record)
        await db.flush()
        return record

    # ------------------------------------------------------------------
    async def recent_failures(self, db: AsyncSession, limit: int = 50) -> List[Dict[str, Any]]:
        rows = list(
            (
                await db.execute(
                    select(FailureRecord).order_by(FailureRecord.created_at.desc()).limit(limit)
                )
            ).scalars().all()
        )
        return [
            {
                "category": r.error_category,
                "message": r.error_message[:300],
                "retryable": r.is_retryable,
                "retry_count": r.retry_count,
                "strategy": r.recovery_strategy,
                "task_id": str(r.task_id) if r.task_id else None,
                "at": r.created_at.isoformat(),
            }
            for r in rows
        ]


_SENSITIVE_KEYS = ("key", "secret", "token", "password", "seed", "mnemonic", "authorization")


def _sanitize_diagnostics(data: Dict[str, Any]) -> Dict[str, Any]:
    """Strip anything secret-shaped before diagnostics are persisted."""
    clean: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        if any(marker in key.lower() for marker in _SENSITIVE_KEYS):
            clean[key] = "[REDACTED]"
        elif isinstance(value, dict):
            clean[key] = _sanitize_diagnostics(value)
        elif isinstance(value, str) and len(value) > 2000:
            clean[key] = value[:2000] + "...[truncated]"
        else:
            clean[key] = value
    return clean


def next_retry_at(backoff_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)
