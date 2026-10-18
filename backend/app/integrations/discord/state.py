"""The Discord part of a task checkpoint: what has been done, and what must not be redone.

AirdropOS already persists task checkpoints (``TaskCheckpointManager``), and this
module does not add a second store. It builds the state dictionary that goes into the
existing checkpoint and reads it back, layering Discord-specific keys on top of the
snapshot shape the rest of the platform already writes.

The rule this exists to enforce
-------------------------------
Resume must be idempotent. When a task comes back - after a login, a restart, a
retry - it has to be able to tell "this step already happened" from "this step never
ran", and to say so without re-reading anything irreversible.

Two consequences shape the data:

* **Completed steps are recorded by fingerprint, not by description.** The message
  body is hashed. A step that succeeded is identifiable without the text being
  stored, and a step that is merely *attempted* is recorded separately from one that
  is *known done* - because only the second is safe to skip.
* **An uncertain step is never treated as done.** ``pending_action`` survives a
  crash mid-send, so a resumed task verifies the channel instead of sending again.

Nothing here stores page contents wholesale. A bounded excerpt and a permalink are
enough to recognise what happened; the full scrollback is not persisted, because a
checkpoint that accumulates transcripts becomes a store of other people's messages.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.app.integrations.discord.models import MAX_STORED_EXCERPT, text_fingerprint

#: Keys this module owns inside the checkpoint ``state`` dict. Anything else in the
#: state belongs to the platform's own execution snapshot and is left untouched.
KEY_BROWSER_PROFILE = "discord_browser_profile_id"
KEY_OPERATION = "discord_operation"
KEY_CHANNEL = "discord_channel"
KEY_COMPLETED = "discord_completed_steps"
KEY_PENDING = "discord_pending_action"
KEY_EVIDENCE = "discord_last_evidence"
KEY_INTERVENTION = "discord_login_intervention_id"
KEY_ATTEMPTS = "discord_send_attempts"
KEY_DEADLINE = "discord_deadline"
KEY_INTENT = "discord_intent"
KEY_PAGE_STATE = "discord_last_page_state"

CHECKPOINT_LOGIN_WAITING = "awaiting_operator:discord_login_required"
CHECKPOINT_NORMAL = "discord_progress"

#: A page reading is a small, non-sensitive summary of where the browser was. Enough
#: to reorient after a restart; not a copy of the page.
MAX_PAGE_STATE_CHARS = 300


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_state(
    *,
    browser_profile_id: str,
    operation: str,
    iteration: int = 0,
    completed_action_ids: Optional[List[str]] = None,
    pending_actions: Optional[List[Dict[str, Any]]] = None,
    observations: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    channel: Optional[Dict[str, Any]] = None,
    intent: str = "",
    evidence_path: str = "",
    intervention_id: str = "",
    deadline: str = "",
) -> Dict[str, Any]:
    """Assemble a checkpoint state in the platform's snapshot shape, plus Discord keys."""
    from backend.app.tasks.checkpoints import CheckpointManager

    state = CheckpointManager.execution_snapshot(
        iteration,
        list(completed_action_ids or []),
        list(pending_actions or []),
        dict(observations or {}),
    )
    state.update(
        {
            KEY_BROWSER_PROFILE: str(browser_profile_id or ""),
            KEY_OPERATION: str(operation or ""),
            KEY_INTENT: str(intent or ""),
            KEY_CHANNEL: dict(channel or {}),
            KEY_COMPLETED: [],
            KEY_PENDING: "",
            KEY_EVIDENCE: str(evidence_path or ""),
            KEY_INTERVENTION: str(intervention_id or ""),
            KEY_ATTEMPTS: {},
            KEY_DEADLINE: str(deadline or ""),
        }
    )
    if task_id:
        state["task_id"] = str(task_id)
    return state


def load(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Read the Discord keys back out of a checkpoint, tolerating an older shape.

    Returns empty values rather than raising when a key is absent: a checkpoint
    written before this feature existed, or by a different subsystem, must still be
    loadable. Refusing to read it would strand the task instead of resuming it.
    """
    raw = state if isinstance(state, dict) else {}
    completed = raw.get(KEY_COMPLETED)
    attempts = raw.get(KEY_ATTEMPTS)
    channel = raw.get(KEY_CHANNEL)
    return {
        "browser_profile_id": str(raw.get(KEY_BROWSER_PROFILE) or ""),
        "operation": str(raw.get(KEY_OPERATION) or ""),
        "intent": str(raw.get(KEY_INTENT) or ""),
        "channel": dict(channel) if isinstance(channel, dict) else {},
        "completed_steps": list(completed) if isinstance(completed, list) else [],
        "pending_action": str(raw.get(KEY_PENDING) or ""),
        "last_evidence": str(raw.get(KEY_EVIDENCE) or ""),
        "login_intervention_id": str(raw.get(KEY_INTERVENTION) or ""),
        "send_attempts": dict(attempts) if isinstance(attempts, dict) else {},
        "deadline": str(raw.get(KEY_DEADLINE) or ""),
        "last_page_state": dict(raw.get(KEY_PAGE_STATE) or {})
        if isinstance(raw.get(KEY_PAGE_STATE), dict)
        else {},
        "task_id": str(raw.get("task_id") or ""),
    }


def with_completed(
    state: Dict[str, Any], *, step: str, channel: Optional[Dict[str, Any]] = None, evidence_path: str = ""
) -> Dict[str, Any]:
    """Return a copy of ``state`` with a step marked genuinely finished.

    ``step`` should be an operation name, optionally suffixed with the fingerprint of
    what it acted on (``send_message:<fingerprint>``) so two different sends are
    distinguishable.
    """
    merged = dict(state or {})
    completed = list(load(merged)["completed_steps"])
    if step and step not in completed:
        completed.append(step)
    merged[KEY_COMPLETED] = completed
    merged[KEY_PENDING] = ""
    if channel:
        merged[KEY_CHANNEL] = dict(channel)
    if evidence_path:
        merged[KEY_EVIDENCE] = str(evidence_path)
    merged[KEY_PAGE_STATE] = _page_state(merged.get(KEY_PAGE_STATE), channel)
    merged["saved_at"] = now_iso()
    return merged


def with_pending(
    state: Dict[str, Any], *, step: str, fingerprint: str = "", channel: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Record a step that STARTED and has not been confirmed.

    This is written *before* an irreversible action, not after. If the process dies
    between the click and the confirmation, this is the record that stops the resumed
    task from sending a second copy.
    """
    merged = dict(state or {})
    attempts = dict(load(merged)["send_attempts"])
    merged[KEY_COMPLETED] = list(load(merged)["completed_steps"])
    merged[KEY_PENDING] = f"{step}:{fingerprint}" if fingerprint else str(step)
    if fingerprint:
        attempts[fingerprint] = {"step": step, "started_at": now_iso(), "confirmed": False}
    merged[KEY_ATTEMPTS] = attempts
    if channel:
        merged[KEY_CHANNEL] = dict(channel)
    merged["saved_at"] = now_iso()
    return merged


def clear_pending(state: Dict[str, Any], *, fingerprint: str = "") -> Dict[str, Any]:
    """Clear the pending marker once the step's outcome is known.

    ``fingerprint`` is deliberately not required to be present: an UNCERTAIN send is
    cleared from the *pending* slot (so the task stops treating it as in-flight) while
    remaining visible in ``send_attempts`` as unconfirmed, which is what makes the
    resume path verify instead of repeat.
    """
    merged = dict(state or {})
    merged[KEY_PENDING] = ""
    merged["saved_at"] = now_iso()
    return merged


def is_step_completed(state: Optional[Dict[str, Any]], step: str) -> bool:
    return step in load(state)["completed_steps"]


def has_pending(state: Optional[Dict[str, Any]]) -> bool:
    return bool(load(state)["pending_action"])


def pending_fingerprint(state: Optional[Dict[str, Any]]) -> str:
    pending = load(state)["pending_action"]
    _, _, fingerprint = pending.partition(":")
    return fingerprint or pending


def unconfirmed_attempts(state: Optional[Dict[str, Any]]) -> List[str]:
    """Fingerprints of sends that started and were never confirmed."""
    attempts = load(state)["send_attempts"]
    return [key for key, value in attempts.items() if not value.get("confirmed")]


def confirm_attempt(state: Optional[Dict[str, Any]], fingerprint: str) -> Dict[str, Any]:
    merged = dict(state or {})
    attempts = dict(load(merged)["send_attempts"])
    if fingerprint in attempts:
        attempts[fingerprint] = {**attempts[fingerprint], "confirmed": True, "confirmed_at": now_iso()}
    merged[KEY_ATTEMPTS] = attempts
    merged["saved_at"] = now_iso()
    return merged


def with_login_intervention(state: Dict[str, Any], *, intervention_id: str) -> Dict[str, Any]:
    merged = dict(state or {})
    merged[KEY_INTERVENTION] = str(intervention_id or "")
    merged[KEY_BROWSER_PROFILE] = load(merged)["browser_profile_id"]
    merged["saved_at"] = now_iso()
    return merged


def resume_snapshot(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A compact, non-sensitive summary of where a paused task was.

    This is what the operator sees next to a login request and what the planner reads
    on resume. It deliberately carries identifiers and a bounded excerpt rather than
    message bodies.
    """
    view = load(state)
    return {
        "browser_profile_id": view["browser_profile_id"],
        "operation": view["operation"],
        "intent": view["intent"],
        "channel": view["channel"],
        "completed_steps": view["completed_steps"],
        "pending_action": view["pending_action"],
        "unconfirmed_sends": unconfirmed_attempts(state),
        "last_evidence": view["last_evidence"],
        "login_intervention_id": view["login_intervention_id"],
        "deadline": view["deadline"],
        "last_page_state": view["last_page_state"],
    }


def message_step(operation: str, body: str, *, reply_to: str = "", channel_id: str = "") -> str:
    """The step key for a send/reply: operation plus a hash of what was sent.

    The body itself is never part of the key, so a checkpoint cannot be read back
    into the text the operator wrote.
    """
    return f"{operation}:{text_fingerprint(body, reply_to=reply_to)}"


def bounded_excerpt(text: str) -> str:
    body = " ".join(str(text or "").split())
    return body[:MAX_STORED_EXCERPT]


def _page_state(previous: Any, channel: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep only where the browser was, not what was on it."""
    out: Dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
    if channel:
        out.update({k: str(v)[:120] for k, v in channel.items() if k in ("channel_id", "channel_name", "server_id", "label")})
    out["at"] = now_iso()
    return out