"""Per-browser-session state shared by the browser tools and the gateway.

Everything here lives in the WORKER process, next to the real Playwright contexts.

It answers three questions the agent must respect before touching a page:
  * has an operator paused or stopped this session?
  * is it waiting for a human (CAPTCHA / MFA / login)?
  * does an operator hold the exclusive manual-control lock right now?
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.app.core.config import settings
from backend.app.live.events import EventLog, safe_url
from backend.app.live.models import Conflict, SessionStatus

CONTROL_MODES = ("click", "full")


@dataclass
class ControlLock:
    session_key: str
    owner_id: str
    owner_label: str
    mode: str
    token: str
    acquired_at: float
    expires_at: float
    last_seq: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    window_start: float = 0.0
    window_count: int = 0

    def public(self) -> Dict[str, Any]:
        return {
            "owner": self.owner_label,
            "mode": self.mode,
            "expires_in": max(0, int(self.expires_at - time.monotonic())),
        }


@dataclass
class SessionRecord:
    key: str
    ordinal: int
    profile_id: Optional[str] = None
    profile_name: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    created_mono: float = field(default_factory=time.monotonic)
    stopped_at: Optional[float] = None
    stopped_by_operator: bool = False
    paused: bool = False
    paused_by: Optional[str] = None
    waiting: bool = False
    waiting_reason: Optional[str] = None
    waiting_since: Optional[float] = None
    waiting_timed_out: bool = False
    failed: bool = False
    last_error: Optional[str] = None
    last_url: str = ""
    last_title: str = ""
    last_activity_at: float = field(default_factory=time.time)
    task_id: Optional[str] = None
    task_title: Optional[str] = None
    control: Optional[ControlLock] = None
    stopped: bool = False

    @property
    def status(self) -> SessionStatus:
        if self.stopped:
            return SessionStatus.STOPPED
        if self.failed:
            return SessionStatus.FAILED
        if self.waiting:
            return SessionStatus.WAITING_FOR_USER
        if self.paused:
            return SessionStatus.PAUSED
        return SessionStatus.RUNNING


class BrowserSessionRegistry:
    def __init__(self, events: EventLog) -> None:
        self._events = events
        self.records: Dict[str, SessionRecord] = {}
        self._ordinal = 0

    # ------------------------------------------------------------------ records
    def get(self, key: str) -> Optional[SessionRecord]:
        return self.records.get(key)

    def ensure(self, key: str, *, profile_id: Optional[str] = None, profile_name: Optional[str] = None) -> SessionRecord:
        rec = self.records.get(key)
        if rec is None:
            self._ordinal += 1
            rec = SessionRecord(key=key, ordinal=self._ordinal)
            self.records[key] = rec
            self._emit(rec, "session.started", "Session started", SessionStatus.RUNNING)
        elif rec.stopped:
            # Re-opened after it ended: a fresh run that keeps its number and profile.
            rec = SessionRecord(
                key=key, ordinal=rec.ordinal, profile_id=rec.profile_id, profile_name=rec.profile_name
            )
            self.records[key] = rec
            self._emit(rec, "session.started", "Session started", SessionStatus.RUNNING)
        if profile_id:
            rec.profile_id = profile_id
        if profile_name:
            rec.profile_name = profile_name
        return rec

    def touch(self, key: str, *, url: Optional[str] = None, title: Optional[str] = None) -> SessionRecord:
        rec = self.ensure(key)
        rec.last_activity_at = time.time()
        if url is not None:
            rec.last_url = url
        if title is not None:
            rec.last_title = title[:160]
        # Which task is the single-threaded worker executing right now?
        try:
            from backend.app.scheduler.worker_status import state as worker_state

            cur = worker_state.current_task
        except Exception:
            cur = None
        if cur:
            rec.task_id = cur.get("task_id")
            rec.task_title = cur.get("title")
        if rec.failed:
            rec.failed = False
            rec.last_error = None
            self._emit(rec, "session.recovered", "Session recovered", SessionStatus.RUNNING)
        return rec

    # ------------------------------------------------------------------ agent gate
    def gate(self, key: str) -> Optional[str]:
        """Reason the AGENT must not act on this session right now, or None."""
        rec = self.records.get(key)
        if rec is None:
            return None
        self._expire_lock(rec)
        if rec.control is not None:
            return "An operator has manual control of this browser session. The agent is holding off."
        if rec.stopped and rec.stopped_by_operator:
            return "This session was stopped by an operator. Start a new session from Live Broadcast to continue."
        if rec.paused:
            return "This session was paused by an operator."
        if rec.waiting:
            return f"Waiting for operator verification: {rec.waiting_reason or 'human action required'}"
        return None

    # ------------------------------------------------------------------ state changes
    def mark_waiting(self, key: str, reason: str) -> None:
        rec = self.ensure(key)
        if rec.waiting and rec.waiting_reason == reason:
            return
        rec.waiting = True
        rec.waiting_reason = reason[:200]
        rec.waiting_since = time.time()
        rec.waiting_timed_out = False
        self._emit(rec, "verification.required", f"Waiting for you: {reason}", SessionStatus.WAITING_FOR_USER)

    def clear_waiting(self, key: str, note: str) -> None:
        rec = self.records.get(key)
        if rec and rec.waiting:
            rec.waiting = False
            rec.waiting_reason = None
            rec.waiting_since = None
            rec.waiting_timed_out = False
            self._emit(rec, "verification.cleared", note, SessionStatus.RUNNING)

    def pause(self, key: str, by: str) -> None:
        rec = self._need(key)
        if not rec.paused:
            rec.paused = True
            rec.paused_by = by
            self._emit(rec, "session.paused", f"Paused by {by}", SessionStatus.PAUSED)

    def clear_pause(self, key: str, by: str) -> None:
        rec = self._need(key)
        if rec.paused:
            rec.paused = False
            rec.paused_by = None
            self._emit(rec, "session.resumed", f"Resumed by {by}", SessionStatus.RUNNING)

    def mark_error(self, key: str, detail: str) -> None:
        rec = self.ensure(key)
        rec.failed = True
        rec.last_error = detail[:200]
        self._emit(rec, "session.error", f"Error: {detail[:160]}", SessionStatus.FAILED)

    def mark_stopped(self, key: str, *, by_operator: bool, reason: str) -> None:
        rec = self.records.get(key)
        if rec is None or rec.stopped:
            return
        rec.stopped = True
        rec.stopped_at = time.time()
        rec.stopped_by_operator = by_operator
        rec.control = None
        rec.waiting = False
        rec.paused = False
        self._emit(rec, "session.stopped", reason, SessionStatus.STOPPED)

    def restart(self, key: str) -> SessionRecord:
        """Operator-initiated start: forget an earlier operator stop."""
        old = self.records.get(key)
        if old is not None and old.stopped:
            self.records.pop(key, None)
            self._ordinal = max(self._ordinal, old.ordinal)
            rec = self.ensure(key)
            rec.ordinal = old.ordinal
            rec.profile_id, rec.profile_name = old.profile_id, old.profile_name
            return rec
        return self.ensure(key)

    # ------------------------------------------------------------------ control lock
    def _expire_lock(self, rec: SessionRecord) -> None:
        lock = rec.control
        if lock is not None and lock.expires_at <= time.monotonic():
            rec.control = None
            self._emit(rec, "control.expired", f"Manual control by {lock.owner_label} expired (no activity)", None)

    def acquire(self, key: str, *, owner_id: str, owner_label: str, mode: str) -> ControlLock:
        if mode not in CONTROL_MODES:
            raise Conflict("Unknown control mode.", "bad_mode")
        rec = self._need(key)
        if rec.stopped:
            raise Conflict("Session is stopped.", "stopped")
        self._expire_lock(rec)
        cur = rec.control
        ttl = settings.LIVE_CONTROL_LOCK_TTL_SECONDS
        now = time.monotonic()
        if cur is not None and cur.owner_id != owner_id:
            raise Conflict(f"Another operator ({cur.owner_label}) already controls this session.", "locked")
        if cur is not None:  # same owner: renew / change mode, keep the token
            cur.mode = mode
            cur.expires_at = now + ttl
            self._emit(rec, "control.mode", f"Manual control mode set to '{mode}'", None)
            return cur
        lock = ControlLock(
            session_key=key,
            owner_id=owner_id,
            owner_label=owner_label,
            mode=mode,
            token=secrets.token_urlsafe(24),
            acquired_at=now,
            expires_at=now + ttl,
        )
        rec.control = lock
        self._emit(rec, "control.acquired", f"{owner_label} took manual control ({mode})", None)
        return lock

    def renew(self, key: str, *, owner_id: str, token: str) -> ControlLock:
        lock = self.require_lock(key, owner_id=owner_id, token=token)
        lock.expires_at = time.monotonic() + settings.LIVE_CONTROL_LOCK_TTL_SECONDS
        return lock

    def release(self, key: str, *, owner_id: str, token: Optional[str]) -> Dict[str, int]:
        rec = self.records.get(key)
        if rec is None or rec.control is None:
            return {}
        lock = rec.control
        if lock.owner_id != owner_id or (token is not None and not secrets.compare_digest(lock.token, token)):
            raise Conflict("You do not hold the control lock for this session.", "not_owner")
        rec.control = None  # token dies here: queued/stale input is rejected from now on
        counts = dict(lock.counts)
        summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no input sent"
        self._emit(rec, "control.released", f"{lock.owner_label} released manual control ({summary})", None)
        return counts

    def require_lock(self, key: str, *, owner_id: str, token: str) -> ControlLock:
        rec = self.records.get(key)
        if rec is None:
            raise Conflict("No such session.", "no_session")
        self._expire_lock(rec)
        lock = rec.control
        if lock is None or not secrets.compare_digest(lock.token, token or "") or lock.owner_id != owner_id:
            raise Conflict("Manual control is not active for this session.", "no_lock")
        return lock

    # ------------------------------------------------------------------ housekeeping
    def sweep(self) -> None:
        timeout = settings.LIVE_HUMAN_WAIT_TIMEOUT_SECONDS
        now = time.time()
        for rec in list(self.records.values()):
            self._expire_lock(rec)
            if rec.waiting and not rec.waiting_timed_out and rec.waiting_since and now - rec.waiting_since > timeout:
                rec.waiting_timed_out = True
                if rec.control is not None:
                    rec.control = None
                self._emit(
                    rec,
                    "verification.timeout",
                    f"No operator response within {timeout // 60} min. Session stays parked; resume or stop it.",
                    SessionStatus.WAITING_FOR_USER,
                )

    def prune_stopped(self, keep: int = 20) -> None:
        stopped = sorted((r for r in self.records.values() if r.stopped), key=lambda r: r.stopped_at or 0)
        for rec in stopped[:-keep] if len(stopped) > keep else []:
            self.records.pop(rec.key, None)

    # ------------------------------------------------------------------ helpers
    def _need(self, key: str) -> SessionRecord:
        rec = self.records.get(key)
        if rec is None:
            raise KeyError(key)
        return rec

    def _emit(self, rec: SessionRecord, type_: str, message: str, status: Optional[SessionStatus]) -> None:
        self._events.emit(
            integration="browser",
            session_id=f"browser:{rec.key}",
            type=type_,
            message=message,
            status=status.value if status else None,
        )

    def url_for_display(self, rec: SessionRecord) -> str:
        return safe_url(rec.last_url)
