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
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from backend.app.core.config import settings

try:  # POSIX advisory locks.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # Windows byte-range locks.
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Sentinel: "do not check the state file's signature before replacing it".
_ANY_SIGNATURE = object()


def _try_lock(handle) -> None:
    """Take an exclusive, non-blocking cross-process lock on ``handle``.

    Raises ``OSError`` when the lock is held elsewhere OR when this platform has no locking
    primitive at all. The latter is deliberate: a missing lock must surface as a failure the
    caller handles, never as silently running without one.
    """
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        raise OSError("no cross-process file-locking primitive is available on this platform")


def _unlock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


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


class EmergencyStopPersistenceError(Exception):
    """The stop is engaged in THIS process but could not be shared or kept.

    Other processes (the worker) and a restart will not see it. Callers must never report this as a
    successful activation.
    """

    def __init__(self, message: str, event: Optional[Dict[str, object]] = None):
        super().__init__(message)
        self.event = event or {}


class EmergencyStopAuthorizationError(Exception):
    """Release was attempted by a non-human or anonymous actor."""


# Identities that may never release a stop: the platform's own processes and the agent.
NON_HUMAN_ACTORS = frozenset(
    {"", "system", "worker", "agent", "scheduler", "environment", "anonymous", "none", "null"}
)


class EmergencyStopController:
    """Stop interlock with an auditable transition log.

    State is persisted to a small JSON file that every process (API, worker)
    reads, so a stop engaged in one process is honoured by all of them and
    survives restarts. The file is replaced atomically on every transition.
    Reads fail CLOSED: a state file that exists but cannot be parsed is treated
    as "stopped", never as "running".
    """

    MAX_HISTORY = 50
    LOCK_TIMEOUT_SECONDS = 3.0

    def __init__(self, state_file: Optional[str] = None) -> None:
        self._state_file = Path(state_file or settings.EMERGENCY_STOP_STATE_FILE)
        self._lock_path = self._state_file.with_name(self._state_file.name + ".lock")
        # Written after the first successful persist. If it exists but the state file does not,
        # the state was lost (deleted, wrong volume) and a restart must not read that as first boot.
        self._marker_path = self._state_file.with_name(self._state_file.name + ".initialized")
        self._lock = threading.RLock()
        self._file_sig: Optional[tuple] = None
        self._stopped: bool = False
        self._activated_at: Optional[datetime] = None
        self._activated_by: Optional[str] = None
        self._reason: Optional[str] = None
        self._policy: set[str] = set(StopPolicy.ALLOWED)
        self._history: list[Dict[str, object]] = []
        self._persist_error: Optional[str] = None

        # EMERGENCY_STOP_ENABLED seeds the state only when no persisted state
        # exists yet. Once the file exists it is the source of truth, so an
        # operator's resume is not silently undone by the environment flag on
        # the next restart.
        if not self._state_file.exists() and bool(settings.EMERGENCY_STOP_ENABLED):
            self._stopped = True
            self._reason = "EMERGENCY_STOP_ENABLED is set in the environment"
            self._activated_by = "environment"
            self._activated_at = datetime.now(timezone.utc)
            try:
                self._persist()
            except Exception:
                self._persist_error = "initial state could not be persisted"
                logger.critical("Emergency stop seed could not be persisted.", exc_info=True)
        elif not self._state_file.exists() and not self._storage_writable():
            # No state yet AND nowhere to keep one: a stop engaged later could not be shared with
            # the worker or survive a restart, so start stopped rather than pretend to be safe.
            self._fail_closed("Emergency stop state storage is not writable; cannot guarantee a stop would persist.")
            self._persist_error = "state storage is not writable"
        elif not self._state_file.exists() and (
            self._marker_path.exists() or bool(getattr(settings, "EMERGENCY_STOP_REQUIRE_STATE", False))
        ):
            # The state file is missing although state was written here before (marker), or the
            # operator declared that a missing file is never acceptable. Whatever it said is unknown,
            # so start stopped. Only an operator resume (which recreates the file) clears this.
            self._fail_closed(
                "Emergency stop state file is missing at startup; failing closed until an operator resumes."
            )
        else:
            self._refresh()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _storage_writable(self) -> bool:
        """True when the nearest existing ancestor of the state file is a writable directory."""
        probe = self._state_file.parent
        while not probe.exists():
            if probe.parent == probe:
                return False
            probe = probe.parent
        return probe.is_dir() and os.access(str(probe), os.W_OK | os.X_OK)

    def _signature(self) -> Optional[tuple]:
        try:
            st = self._state_file.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _fail_closed(self, reason: str) -> None:
        """Engage the stop because the true state cannot be established. Never raises."""
        with self._lock:
            already = self._stopped and self._reason == reason
            self._stopped = True
            self._reason = reason
            if self._activated_at is None:
                self._activated_at = datetime.now(timezone.utc)
            self._activated_by = self._activated_by or "system"
            self._policy = set(StopPolicy.ALLOWED)
            if not already:
                self._history.append(
                    {
                        "action": "EMERGENCY_STOP_FAILED_CLOSED",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "by": "system",
                        "reason": reason,
                    }
                )
                self._history = self._history[-self.MAX_HISTORY:]
            logger.critical("Emergency stop failing CLOSED: %s", reason)

    @contextmanager
    def _exclusive(self, required: bool = False):
        """In-process lock plus a cross-process file lock around read-modify-write transitions.

        Without it an API process resuming and a worker process activating can interleave and the
        stop is silently lost. ``required`` callers (resume) refuse to proceed unlocked, on every
        platform: if no lock can be taken, the state is left unchanged and the stop stays engaged.
        Activation never waits indefinitely, because engaging the stop must not be blockable; it
        proceeds without the lock (loudly), which is safe because a concurrent resume re-checks the
        state file's signature before it writes (see ``resume``) and so cannot overwrite it.
        """
        with self._lock:
            handle = None
            try:
                self._lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self._lock_path, "a+")
                deadline = time.monotonic() + self.LOCK_TIMEOUT_SECONDS
                while True:
                    try:
                        _try_lock(handle)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.02)
            except OSError as exc:
                if handle is not None:
                    handle.close()
                    handle = None
                if required:
                    raise EmergencyStopPersistenceError(
                        f"Could not lock the emergency stop state ({type(exc).__name__}); state unchanged."
                    ) from exc
                logger.critical("Emergency stop lock unavailable (%s); proceeding without it.", exc)
            try:
                yield
            finally:
                if handle is not None:
                    try:
                        _unlock(handle)
                    finally:
                        handle.close()

    def _refresh(self) -> None:
        """Adopt the persisted state if another process (or restart) changed it."""
        with self._lock:
            try:
                sig = self._signature()
            except OSError as exc:  # permissions, I/O error: the state cannot be established
                self._fail_closed(f"Emergency stop state is unreadable ({type(exc).__name__}); failing closed.")
                return
            if sig == self._file_sig:
                return
            if sig is None:
                if self._file_sig is not None:
                    # The file existed and is gone (deleted, volume lost). Whatever it said is
                    # unknown, so do not carry on as if running. Only an operator resume clears this.
                    self._file_sig = None
                    self._fail_closed("Emergency stop state file disappeared; failing closed until an operator resumes.")
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

    def _persist(self, expected_signature: object = _ANY_SIGNATURE) -> None:
        """Atomically replace the state file.

        ``expected_signature`` makes the write a compare-and-swap: if the file changed since the
        caller read it, nothing is written and :class:`EmergencyStopPersistenceError` is raised.
        Resume uses this so a stop engaged by another process in the meantime is never overwritten,
        even when the cross-process lock could not be taken.
        """
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
                if expected_signature is not _ANY_SIGNATURE and self._signature() != expected_signature:
                    raise EmergencyStopPersistenceError(
                        "The emergency stop state changed while it was being updated; "
                        "nothing was written. Re-check the state and retry."
                    )
                os.replace(tmp, self._state_file)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._file_sig = self._signature()
            try:  # best effort: the marker only ever makes a later missing file fail closed
                self._marker_path.touch(exist_ok=True)
            except OSError:
                logger.warning("Could not write the emergency stop marker file.", exc_info=True)

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
        """Engage the stop. Idempotent; re-activation updates the reason.

        The stop is engaged in this process BEFORE anything else, and it is never undone by a
        failure. If it cannot be persisted (so the worker and a restart would not see it) this
        raises :class:`EmergencyStopPersistenceError` instead of returning a success event.
        A stop that is already active is never narrowed by a later activation.
        """
        with self._exclusive():
            self._refresh()
            was_stopped = self._stopped
            requested = (set(policy) & StopPolicy.ALLOWED) if policy else set()
            # An empty or invalid policy would "engage" a stop that halts nothing: use the full one.
            requested = requested or set(StopPolicy.ALLOWED)
            self._stopped = True
            self._activated_at = datetime.now(timezone.utc)
            self._activated_by = activated_by
            self._reason = reason
            self._policy = (self._policy | requested) if was_stopped else requested

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
                self._persist_error = None
            except Exception as exc:
                self._persist_error = type(exc).__name__
                logger.critical(
                    "EMERGENCY STOP could not be persisted to %s; other processes will NOT see it.",
                    self._state_file, exc_info=True,
                )
                raise EmergencyStopPersistenceError(
                    "Emergency stop is engaged in this process only; it could not be saved, so the "
                    "worker and a restart will NOT honour it.",
                    event,
                ) from exc
            return event

    def resume(self, resumed_by: str = "operator") -> Dict[str, object]:
        """Controlled resumption. Operator-controlled, never automatic.

        Refused for non-human identities (the worker, scheduler, agent, environment).
        """
        if str(resumed_by or "").strip().lower() in NON_HUMAN_ACTORS:
            raise EmergencyStopAuthorizationError(
                "Only an authenticated human operator can release the emergency stop."
            )
        with self._exclusive(required=True):
            self._refresh()
            # The state this release is based on; _persist refuses to write over anything newer.
            based_on = self._file_sig
            previous = (self._stopped, self._activated_at, self._activated_by, self._reason, self._policy)
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
                self._persist(expected_signature=based_on)
                self._persist_error = None
            except Exception:
                # If the resume cannot be shared, stay stopped everywhere
                # rather than run in only one process.
                self._history.pop()
                self._stopped, self._activated_at, self._activated_by, self._reason, self._policy = previous
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
            "persisted": self._persist_error is None,
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
