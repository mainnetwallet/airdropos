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

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
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
    """Stop interlock with an auditable transition log.

    State is persisted to a small JSON file that every process (API, worker)
    reads, so a stop engaged in one process is honoured by all of them and
    survives restarts. The file is replaced atomically on every transition.
    Reads fail CLOSED: a state file that exists but cannot be parsed is treated
    as "stopped", never as "running".
    """

    MAX_HISTORY = 50

    def __init__(self, state_file: Optional[str] = None) -> None:
        self._state_file = Path(state_file or settings.EMERGENCY_STOP_STATE_FILE)
        self._lock = threading.RLock()
        self._file_sig: Optional[tuple] = None
        self._stopped: bool = False
        self._activated_at: Optional[datetime] = None
        self._activated_by: Optional[str] = None
        self._reason: Optional[str] = None
        self._policy: set[str] = set(StopPolicy.ALLOWED)
        self._history: list[Dict[str, object]] = []

        # EMERGENCY_STOP_ENABLED seeds the state only when no persisted state
        # exists yet. Once the file exists it is the source of truth, so an
        # operator's resume is not silently undone by the environment flag on
        # the next restart.
        if not self._state_file.exists() and bool(settings.EMERGENCY_STOP_ENABLED):
            self._stopped = True
            self._reason = "EMERGENCY_STOP_ENABLED is set in the environment"
            self._activated_by = "environment"
            self._activated_at = datetime.now(timezone.utc)
            self._persist()
        else:
            self._refresh()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _signature(self) -> Optional[tuple]:
        try:
            st = self._state_file.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _refresh(self) -> None:
        """Adopt the persisted state if another process (or restart) changed it."""
        with self._lock:
            sig = self._signature()
            if sig == self._file_sig:
                return
            if sig is None:
                # File removed: keep the last known in-memory state.
                self._file_sig = None
                return
            try:
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("stopped"), bool):
                    raise ValueError("malformed emergency stop state")
                activated_at = data.get("activated_at")
                policy = set(data.get("policy") or []) & StopPolicy.ALLOWED
                self._stopped = data["stopped"]
                self._activated_at = datetime.fromisoformat(activated_at) if activated_at else None
                self._activated_by = data.get("activated_by")
                self._reason = data.get("reason")
                self._policy = policy or set(StopPolicy.ALLOWED)
                self._history = list(data.get("history") or [])[-self.MAX_HISTORY:]
            except Exception as exc:  # unreadable state must never mean "running"
                logger.critical("Emergency stop state file unreadable (%s); failing CLOSED.", exc)
                self._stopped = True
                self._reason = "Emergency stop state file is unreadable; failing closed."
                self._policy = set(StopPolicy.ALLOWED)
            self._file_sig = sig

    def _persist(self) -> None:
        with self._lock:
            payload = {
                "stopped": self._stopped,
                "activated_at": self._activated_at.isoformat() if self._activated_at else None,
                "activated_by": self._activated_by,
                "reason": self._reason,
                "policy": sorted(self._policy),
                "history": self._history[-self.MAX_HISTORY:],
            }
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._state_file.parent), prefix=".estop-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self._state_file)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._file_sig = self._signature()

    # ------------------------------------------------------------------
    # State inspection
    # ------------------------------------------------------------------
    @property
    def is_stopped(self) -> bool:
        self._refresh()
        return self._stopped

    def assert_running(self, operation: str) -> None:
        """Guard called before any state-changing agent operation."""
        self._refresh()
        if not self._stopped:
            return
        # Exact match: `operation in "halt_wallet_signing"` is a substring test
        # and would match fragments such as "sign" or "halt".
        if operation in (StopPolicy.HALT_WALLET_SIGNING, StopPolicy.HALT_TRANSACTION_SUBMISSION):
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
        self._refresh()
        return not (self._stopped and StopPolicy.HALT_TASK_EXECUTION in self._policy)

    def can_submit_transactions(self) -> bool:
        self._refresh()
        return not (self._stopped and StopPolicy.HALT_TRANSACTION_SUBMISSION in self._policy)

    def can_sign(self) -> bool:
        self._refresh()
        return not (self._stopped and StopPolicy.HALT_WALLET_SIGNING in self._policy)

    def can_run_jobs(self) -> bool:
        self._refresh()
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
        self._refresh()
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
        try:
            self._persist()
        except Exception:
            # The stop is already engaged in this process. Failing to share it
            # must never undo it; make the gap loud instead.
            logger.critical(
                "EMERGENCY STOP could not be persisted to %s; other processes will NOT see it.",
                self._state_file, exc_info=True,
            )
        return event

    def resume(self, resumed_by: str = "operator") -> Dict[str, object]:
        """Controlled resumption. Operator-controlled, never automatic."""
        with self._lock:
            self._refresh()
            previous = (self._stopped, self._activated_at, self._activated_by, self._reason)
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
            try:
                self._persist()
            except Exception:
                # If the resume cannot be shared, stay stopped everywhere
                # rather than run in only one process.
                self._history.pop()
                self._stopped, self._activated_at, self._activated_by, self._reason = previous
                logger.critical("Could not persist emergency stop resume; remaining STOPPED.", exc_info=True)
                raise
        logger.warning("Emergency stop resumed by %s", resumed_by)
        return event

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, object]:
        self._refresh()
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
