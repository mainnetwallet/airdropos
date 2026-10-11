"""Emergency Stop Interlock (Specification Part 30)

A global, deterministic kill switch. When active it MUST:
  - Prevent new task execution from starting.
  - Prevent new signing requests from reaching the vault.
  - Prevent new transaction submissions.
  - Preserve task records and checkpoints.
  - Record the stop event and notify the user.

It MUST NOT claim that already-submitted on-chain transactions were cancelled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from backend.app.core.config import settings

logger = logging.getLogger(__name__)


class StopPolicy:
    """Defines what a stop actually halts."""

    HALT_TASK_EXECUTION = "halt_task_execution"
    HALT_TRANSACTION_SUBMISSION = "halt_transaction_submission"
    HALT_WALLET_SIGNING = "halt_wallet_signing"
    HALT_SCHEDULED_JOBS = "halt_scheduled_jobs"

    ALLOWED = {HALT_TASK_EXECUTION, HALT_TRANSACTION_SUBMISSION, HALT_WALLET_SIGNING, HALT_SCHEDULED_JOBS}


class EmergencyStopError(Exception):
    """Raised when an operation is refused because emergency stop is active."""

    def __init__(self, operation: str, reason: str):
        super().__init__(f"Operation '{operation}' refused: {reason}")
        self.operation = operation
        self.reason = reason


class EmergencyStopController:
    """Process-wide stop interlock with an auditable transition log."""

    def __init__(self) -> None:
        self._stopped: bool = bool(settings.EMERGENCY_STOP_ENABLED)
        self._activated_at: Optional[datetime] = None
        self._activated_by: Optional[str] = None
        self._reason: Optional[str] = None
        self._policy: set[str] = set(StopPolicy.ALLOWED)
        self._history: list[Dict[str, object]] = []

    # ------------------------------------------------------------------
    # State inspection
    # ------------------------------------------------------------------
    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def assert_running(self, operation: str) -> None:
        """Guard called before any state-changing agent operation."""
        if not self._stopped:
            return
        if operation in StopPolicy.HALT_WALLET_SIGNING or operation in StopPolicy.HALT_TRANSACTION_SUBMISSION:
            raise EmergencyStopError(
                operation,
                f"Emergency stop activated at {self._activated_at.isoformat() if self._activated_at else 'startup'} "
                f"by {self._activated_by or 'operator'}. Reason: {self._reason or 'unspecified'}. "
                "Already-submitted on-chain transactions cannot be recalled.",
            )
        raise EmergencyStopError(
            operation,
            f"Emergency stop active. Reason: {self._reason or 'unspecified'}",
        )

    def can_execute_tasks(self) -> bool:
        return not (self._stopped and StopPolicy.HALT_TASK_EXECUTION in self._policy)

    def can_submit_transactions(self) -> bool:
        return not (self._stopped and StopPolicy.HALT_TRANSACTION_SUBMISSION in self._policy)

    def can_sign(self) -> bool:
        return not (self._stopped and StopPolicy.HALT_WALLET_SIGNING in self._policy)

    def can_run_jobs(self) -> bool:
        return not (self._stopped and StopPolicy.HALT_SCHEDULED_JOBS in self._policy)

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------
    def activate(
        self,
        reason: str,
        activated_by: str = "operator",
        policy: Optional[set[str]] = None,
    ) -> Dict[str, object]:
        """Engage the stop. Idempotent; re-activation updates the reason."""
        self._stopped = True
        self._activated_at = datetime.now(timezone.utc)
        self._activated_by = activated_by
        self._reason = reason
        if policy:
            self._policy = set(policy) & StopPolicy.ALLOWED

        event = {
            "action": "EMERGENCY_STOP_ACTIVATED",
            "at": self._activated_at.isoformat(),
            "by": activated_by,
            "reason": reason,
            "policy": sorted(self._policy),
        }
        self._history.append(event)
        logger.critical("EMERGENCY STOP ACTIVATED by %s. Reason: %s. Policy: %s",
                        activated_by, reason, sorted(self._policy))
        return event

    def resume(self, resumed_by: str = "operator") -> Dict[str, object]:
        """Controlled resumption. Operator-controlled, never automatic."""
        was_stopped = self._stopped
        self._stopped = False
        self._activated_at = None
        self._activated_by = None
        self._reason = None

        event = {
            "action": "EMERGENCY_STOP_RESUMED",
            "at": datetime.now(timezone.utc).isoformat(),
            "by": resumed_by,
            "was_stopped": was_stopped,
        }
        self._history.append(event)
        logger.warning("Emergency stop resumed by %s", resumed_by)
        return event

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, object]:
        return {
            "emergency_stop_enabled": self._stopped,
            "activated_at": self._activated_at.isoformat() if self._activated_at else None,
            "activated_by": self._activated_by,
            "reason": self._reason,
            "policy": sorted(self._policy),
            "can_execute_tasks": self.can_execute_tasks(),
            "can_submit_transactions": self.can_submit_transactions(),
            "can_sign": self.can_sign(),
            "can_run_jobs": self.can_run_jobs(),
            "history": self._history[-20:],
            "disclaimer": (
                "Stopping the platform prevents NEW submissions and signing. "
                "Transactions already broadcast to a network cannot be cancelled."
            ),
        }


# Process-wide singleton.
emergency_stop = EmergencyStopController()
