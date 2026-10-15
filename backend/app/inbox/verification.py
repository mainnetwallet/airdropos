"""Verification of the external conditions that block a task.

A verifier answers one question: *is the blocking condition actually resolved right now?* It
may only report what it actually checked. When a condition cannot be checked from this
process, it says so and supplies a specific question for the operator instead of guessing --
the feature must never claim a task is unblocked because nobody looked.

Verifiers are registered per reason code so the reply path stays deterministic and tests can
substitute doubles without touching the real Telegram or browser stack.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.inbox.service import (
    BROWSER_SESSION_UNAVAILABLE,
    EXTENSION_INSTALL_REQUIRED,
    HUMAN_VERIFICATION,
    MISSING_AUTHENTICATION,
    MISSING_CONFIGURATION,
    TELEGRAM_SESSION_UNAVAILABLE,
    UNKNOWN_REASON,
    InterventionRequest,
    classify_reason,
)
from backend.app.models import Task

logger = logging.getLogger(__name__)

# A verifier that reports "I could not check" rather than "it is fine".
NOT_CHECKED = "NOT_CHECKED"

# How long a single verifier may take before we treat it as inconclusive.
VERIFY_TIMEOUT_SECONDS = 15.0


@dataclass
class VerificationResult:
    """The honest outcome of one check.

    `ok` is True only when the condition was positively confirmed. `checkable` is False when
    this process genuinely cannot tell, which produces a diagnostic question rather than a
    silent resume.
    """

    ok: bool
    method: str
    outcome: str
    checkable: bool = True
    question: Optional[str] = None
    detail: Dict[str, object] = field(default_factory=dict)

    @classmethod
    def satisfied(cls, method: str, outcome: str, **detail: object) -> "VerificationResult":
        return cls(ok=True, method=method, outcome=outcome, checkable=True, detail=dict(detail))

    @classmethod
    def failed(cls, method: str, outcome: str, question: Optional[str] = None, **detail: object) -> "VerificationResult":
        return cls(ok=False, method=method, outcome=outcome, checkable=True, question=question, detail=dict(detail))

    @classmethod
    def unchecked(cls, method: str, outcome: str, question: str) -> "VerificationResult":
        return cls(ok=False, method=method, outcome=outcome, checkable=False, question=question)


Verifier = Callable[[AsyncSession, Task, InterventionRequest], Awaitable[VerificationResult]]

_REGISTRY: Dict[str, Verifier] = {}


def register(reason_code: str, verifier: Verifier) -> None:
    _REGISTRY[reason_code] = verifier


def verifier_for(reason_code: str) -> Optional[Verifier]:
    return _REGISTRY.get(reason_code)


async def verify(
    db: AsyncSession, task: Task, request: InterventionRequest
) -> VerificationResult:
    """Run the verifier registered for this request's reason code."""
    reason_code = request.reason_code
    if reason_code == UNKNOWN_REASON and verifier_for(reason_code) is None:
        # A request raised before extension installs had their own reason was stored as
        # UNKNOWN even though its text names the blocker precisely. Re-derive that one case
        # from the recorded text so the real check runs; nothing else is re-classified.
        text = f"{request.problem_summary or ''} {request.blocking_step or ''}"
        if classify_reason(text) == EXTENSION_INSTALL_REQUIRED:
            reason_code = EXTENSION_INSTALL_REQUIRED
    verifier = verifier_for(reason_code)
    if verifier is None:
        return VerificationResult.unchecked(
            method=NOT_CHECKED,
            outcome=f"No automated check exists for '{request.reason_code}'.",
            question=(
                "I cannot verify this condition automatically. Please confirm what you changed "
                "and where, so I can record it against this task before continuing."
            ),
        )
    try:
        result = await asyncio.wait_for(verifier(db, task, request), timeout=VERIFY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return VerificationResult.unchecked(
            method=reason_code,
            outcome=f"The check did not finish within {VERIFY_TIMEOUT_SECONDS:.0f}s.",
            question="The check timed out. Please confirm the condition is resolved, or tell me what is still failing.",
        )
    except Exception as exc:
        logger.warning("Verification for %s failed: %s", request.request_id, exc)
        return VerificationResult.unchecked(
            method=reason_code,
            outcome=f"The check could not run: {type(exc).__name__}.",
            question="I could not run the check. Please tell me the current state of the thing that is blocking this task.",
        )
    return result


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------
async def _verify_telegram_session(
    db: AsyncSession, task: Task, request: InterventionRequest
) -> VerificationResult:
    """Check the Telegram user session the task depends on.

    Asks the WORKER, which is the process that owns the Telethon client, through the
    gateway it already serves. This deliberately does not build a TelegramClient of its
    own: two independent clients on the same SQLite session file contend for it, and
    Telethon raises AuthKeyDuplicatedError when the same auth key is used twice at once.
    Opening one here was a corruption risk, not a verification.

    The configuration pre-checks below need no client at all and report the first concrete
    reason the deployment is not ready.
    """
    from pathlib import Path

    from backend.app.core.config import settings

    problems: List[str] = []
    if not settings.TELEGRAM_USER_ENABLED:
        problems.append("TELEGRAM_USER_ENABLED is not set to true.")
    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        problems.append("TELEGRAM_API_ID or TELEGRAM_API_HASH is not configured.")
    if not Path(f"{settings.TELEGRAM_SESSION_PATH}.session").exists():
        problems.append("No Telegram session file exists yet.")
    if problems:
        return VerificationResult.failed(
            method="telegram_session_readiness",
            outcome="Telegram is not ready: " + " ".join(problems),
            question=(
                "Telegram is not configured for this deployment. Can you confirm the intended "
                "Telegram account, or tell me to continue without it?"
            ),
        )

    # Configured. Ask the worker for the state of the client it actually holds.
    from backend.app.live import client as gateway

    try:
        payload = await gateway.get_json("/sessions/telegram/agent")
    except gateway.GatewayUnavailable as exc:
        return VerificationResult.unchecked(
            method="telegram_worker_session",
            outcome=f"The worker holding the Telegram session is not reachable ({exc}).",
            question=(
                "I cannot ask the worker whether the Telegram session is signed in, because it "
                "is not reachable. Please tell me the sign-in state, or bring the worker back "
                "and I will re-check."
            ),
        )
    except gateway.GatewayError as exc:
        if exc.status == 404 or exc.code == "not_found":
            # The worker has never used the account, so no client exists to authorise.
            return VerificationResult.failed(
                method="telegram_worker_session",
                outcome="The worker has not opened the Telegram session yet.",
                question=(
                    "The agent has not signed in to Telegram yet. Please run the interactive "
                    "Telegram sign-in, then tell me to re-check."
                ),
            )
        return VerificationResult.unchecked(
            method="telegram_worker_session",
            outcome=f"The worker gateway returned {exc.status} ({exc.code}).",
            question="I could not read the Telegram session state. Please tell me what the sign-in step reported.",
        )
    except Exception as exc:
        return VerificationResult.unchecked(
            method="telegram_worker_session",
            outcome=f"The Telegram session check could not run: {type(exc).__name__}.",
            question="I could not read the Telegram session state. Please tell me what the sign-in step reported.",
        )

    session = _extract_session(payload)
    if session is None:
        return VerificationResult.unchecked(
            method="telegram_worker_session",
            outcome="The worker gateway returned a response that is not a session object.",
            question="I could not read the Telegram session state. Please tell me what the sign-in step reported.",
        )

    # The worker may report a session that is not the one we asked about.
    if str(session.get("integration") or "") != "telegram" or str(session.get("key") or "agent") != "agent":
        return VerificationResult.unchecked(
            method="telegram_worker_session",
            outcome="The gateway answered about a different session than the Telegram agent session.",
            question="That session reference looks stale. Tell me which account you signed in, and I will re-check.",
        )

    status = str(session.get("status") or "").lower()
    if status not in KNOWN_SESSION_STATUSES:
        return VerificationResult.unchecked(
            method="telegram_worker_session",
            outcome=f"The worker reported an unfamiliar Telegram session status '{status}'.",
            question="I do not recognise that session state, so I will not guess. Please tell me the sign-in state.",
        )

    if status == SESSION_STOPPED:
        return VerificationResult.failed(
            method="telegram_worker_session",
            outcome="The Telegram session was stopped by an operator.",
            question="The Telegram session is stopped. Please resume it from Live Broadcast, then tell me to re-check.",
        )
    if status == SESSION_PAUSED:
        return VerificationResult.failed(
            method="telegram_worker_session",
            outcome="The Telegram session is paused by an operator.",
            question="The Telegram session is paused. Please resume it, then tell me to re-check.",
        )
    if status == SESSION_WAITING:
        reason = str((session.get("status_detail") or "") or "the session is waiting")
        return VerificationResult.failed(
            method="telegram_worker_session",
            outcome=f"The Telegram session is still waiting: {reason}",
            question="The Telegram session is still waiting on something. Please finish that, then tell me to re-check.",
        )

    # Status alone is not authorisation. The worker's own client must report a live
    # connection; that is the strongest signal available without opening a second client.
    meta = session.get("meta")
    connected = meta.get("connected") if isinstance(meta, dict) else None
    if connected is not True:
        return VerificationResult.failed(
            method="telegram_worker_session",
            outcome=(
                "The worker reports no live Telegram connection"
                if connected is False
                else "The worker did not report a Telegram connection state"
            ),
            question=(
                "The agent's Telegram client is not connected. Please run the interactive "
                "Telegram sign-in, then tell me to re-check."
            ),
        )

    return VerificationResult.satisfied(
        method="telegram_worker_session",
        outcome="The worker's Telegram client reports a live connection with status 'running'.",
        status=status,
    )


# ----------------------------------------------------------------------
# Browser / live sessions
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Gateway session shape
# ----------------------------------------------------------------------
# The worker gateway answers GET /sessions/{integration}/{key} with a WRAPPER:
#   {"session": {"id": ..., "integration": ..., "key": ..., "status": ..., "waiting": ...}}
# `status` is a live.models.SessionStatus value. Only RUNNING means the session is usable;
# everything else is either still blocked or gone.
SESSION_OK = "running"
SESSION_WAITING = "waiting_for_user"
SESSION_STOPPED = "stopped"
SESSION_FAILED = "failed"
SESSION_PAUSED = "paused"
KNOWN_SESSION_STATUSES = frozenset(
    {SESSION_OK, SESSION_WAITING, SESSION_STOPPED, SESSION_FAILED, SESSION_PAUSED}
)


def _extract_session(payload: Any) -> Optional[Dict[str, Any]]:
    """Pull the session out of the gateway response, or None if the shape is wrong.

    Strict on purpose. A response we do not understand must never be read as an active
    session, because that is how a task gets resumed while the human is still waiting.
    """
    if not isinstance(payload, dict):
        return None
    inner = payload.get("session", payload)
    if not isinstance(inner, dict):
        return None
    return inner


async def _verify_live_session(
    db: AsyncSession, task: Task, request: InterventionRequest
) -> VerificationResult:
    """Check the live session that is holding this task up.

    Asks the worker that owns the session. If the gateway is unreachable, or answers
    something we do not recognise, the result is inconclusive rather than a silent pass.
    """
    from backend.app.live import client as gateway

    ref = request.integration_ref or ""
    integration, _, key = ref.partition(":")
    if not integration or not key:
        return VerificationResult.unchecked(
            method=NOT_CHECKED,
            outcome="No live session is recorded on this request.",
            question="Which session should I re-check? Tell me the session or page you completed the step on.",
        )

    try:
        payload = await gateway.get_json(f"/sessions/{integration}/{key}")
    except gateway.GatewayUnavailable as exc:
        return VerificationResult.unchecked(
            method="live_session_state",
            outcome=f"The worker gateway is not reachable, so the session cannot be inspected ({exc}).",
            question="I cannot inspect the live session right now. Please tell me whether the step you were asked to do is now complete.",
        )
    except gateway.GatewayError as exc:
        # A session the worker says does not exist is a definite negative, not an unknown.
        if exc.status == 404 or exc.code == "not_found":
            return VerificationResult.failed(
                method="live_session_state",
                outcome=f"That {integration} session is no longer running.",
                question="That session has ended. Please start it again, or tell me to continue this task another way.",
            )
        return VerificationResult.unchecked(
            method="live_session_state",
            outcome=f"The worker gateway returned {exc.status} ({exc.code}).",
            question="I could not read the session state. Please tell me what you saw on that page.",
        )
    except Exception as exc:
        return VerificationResult.unchecked(
            method="live_session_state",
            outcome=f"The session check could not run: {type(exc).__name__}.",
            question="I could not read the session state. Please tell me what you saw on that page.",
        )

    session = _extract_session(payload)
    if session is None:
        return VerificationResult.unchecked(
            method="live_session_state",
            outcome="The gateway returned a response that is not a session object.",
            question="I could not read the session state. Please tell me what you saw on that page.",
        )

    # Identity: a stale reference must not be satisfied by some other session's state.
    returned_key = session.get("key")
    returned_integration = session.get("integration")
    if returned_key is not None and str(returned_key) != key:
        return VerificationResult.unchecked(
            method="live_session_state",
            outcome=f"The gateway answered about session '{returned_key}', not '{key}'.",
            question="That session reference looks stale. Tell me which session you worked in, and I will re-check.",
        )
    if returned_integration is not None and str(returned_integration) != integration:
        return VerificationResult.unchecked(
            method="live_session_state",
            outcome=f"The gateway answered about integration '{returned_integration}', not '{integration}'.",
            question="That session reference looks stale. Tell me which session you worked in, and I will re-check.",
        )

    status = session.get("status")
    if not isinstance(status, str) or not status:
        return VerificationResult.unchecked(
            method="live_session_state",
            outcome="The gateway did not report a session status.",
            question="I could not confirm the session is usable. Please tell me whether the page is showing the completed step.",
        )
    status = status.lower()
    if status not in KNOWN_SESSION_STATUSES:
        return VerificationResult.unchecked(
            method="live_session_state",
            outcome=f"The gateway reported an unfamiliar session status '{status}'.",
            question="I do not recognise that session state, so I will not guess. Please tell me what you see on that page.",
        )

    waiting = session.get("waiting")
    if status == SESSION_WAITING or isinstance(waiting, dict):
        reason = ""
        if isinstance(waiting, dict):
            reason = str(waiting.get("reason") or "")
        if not reason:
            reason = str(session.get("status_detail") or "") or "the session is still waiting for you"
        return VerificationResult.failed(
            method="live_session_state",
            outcome=f"The session is still waiting: {reason}",
            question="That session still reports it is waiting. Please finish the step it is asking for, then tell me to re-check.",
        )
    if status in (SESSION_STOPPED, SESSION_FAILED):
        return VerificationResult.failed(
            method="live_session_state",
            outcome=f"The session is {status}.",
            question="That session has ended. Please start it again, or tell me to continue this task another way.",
        )
    if status == SESSION_PAUSED:
        return VerificationResult.failed(
            method="live_session_state",
            outcome="The session is paused.",
            question="That session is paused. Please resume it, then tell me to re-check.",
        )

    return VerificationResult.satisfied(
        method="live_session_state",
        outcome=f"The {integration} session reports status '{status}' with no outstanding verification.",
        status=status,
    )


# ----------------------------------------------------------------------
# Browser extension install
# ----------------------------------------------------------------------
_STORE_EXTENSION_ID = re.compile(r"/([a-p]{32})(?:[/?#]|$)")


def _extension_targets(plan: Any, request: InterventionRequest) -> List[Dict[str, Optional[str]]]:
    """The extensions this task asked the operator to install, read from its own plan.

    Taken from the plan's Web Store steps, never from the operator's chat message, so a reply
    cannot name a different extension and have it accepted.
    """
    targets: List[Dict[str, Optional[str]]] = []
    for step in getattr(plan, "steps", None) or []:
        if getattr(step, "tool", None) != "browser_extensions":
            continue
        params = getattr(step, "parameters", None) or {}
        if params.get("action") != "web_store":
            continue
        match = _STORE_EXTENSION_ID.search(str(params.get("store_url") or ""))
        ext_id = (match.group(1) if match else str(params.get("extension_id") or "")).strip().lower()
        query = str(params.get("query") or "").strip()
        if not ext_id and not query:
            continue
        targets.append(
            {
                "extension_id": ext_id or None,
                "query": query or None,
                "profile": str(
                    params.get("session_key") or params.get("profile_id")
                    or request.integration_ref or ""
                ).strip() or None,
            }
        )
    return targets


async def _verify_extension_installed(
    db: AsyncSession, task: Task, request: InterventionRequest
) -> VerificationResult:
    """Check that the extension is really installed in the browser profile.

    Reads the profile's own installed-extension state (the same read `browser_extensions
    action=check` does). A reply of "installed" is a claim; this is the proof. Chrome's
    'Add to Chrome' confirmation is never clicked by AirdropOS, so the operator's click is the
    only thing that can make this pass.
    """
    from backend.app.browser.profiles import resolve_session_key
    from backend.app.planning.planner import Planner
    from backend.app.tools import browser_tools

    plan = await Planner(db).load_plan_spec(task)
    targets = _extension_targets(plan, request)
    if not targets:
        return VerificationResult.unchecked(
            method=NOT_CHECKED,
            outcome="This task's plan names no Chrome Web Store extension to look for.",
            question="Which extension should I look for? Tell me its name and I will check the profile.",
        )

    missing: List[str] = []
    found: List[str] = []
    where = ""
    for target in targets:
        key = resolve_session_key(target["profile"])
        status = await browser_tools.controller.extension_status(key)
        if not status.get("ok"):
            return VerificationResult.unchecked(
                method="extension_installed",
                outcome=f"'{key}' is not a saved browser profile, so its extensions cannot be read.",
                question="Which browser profile did you install it in? Create or select it on the Browser Profiles page.",
            )
        where = str(status.get("profile_dir") or key)
        label = target["query"] or target["extension_id"] or "the extension"
        if target["extension_id"]:
            hit = [e for e in status.get("installed", []) if str(e.get("id", "")).lower() == target["extension_id"]]
        else:
            hit = await browser_tools._find_extension(status, target["query"] or "")
        if hit:
            found.append(str(hit[0].get("name") or label))
        else:
            missing.append(label)

    if missing:
        return VerificationResult.failed(
            method="extension_installed",
            outcome=(
                f"Chrome does not list {', '.join(missing)} as installed in the AirdropOS "
                f"browser profile ({where})."
            ),
            question=(
                "It is not installed in the profile AirdropOS opened. Make sure you clicked 'Add "
                "to Chrome' in that AirdropOS browser window (not your everyday Chrome), accept "
                "the prompt, then reply and I will check again."
            ),
        )
    return VerificationResult.satisfied(
        method="extension_installed",
        outcome=f"Installed in the browser profile: {', '.join(found)}.",
        extensions=found,
    )


# ----------------------------------------------------------------------
# Authentication / configuration
# ----------------------------------------------------------------------
async def _verify_authentication(
    db: AsyncSession, task: Task, request: InterventionRequest
) -> VerificationResult:
    """Check whether the wallet/credential this task needs exists and is usable."""
    from backend.app.wallets.service import WalletService

    try:
        wallets = await WalletService(db).list_wallets()
    except Exception as exc:
        return VerificationResult.unchecked(
            method=NOT_CHECKED,
            outcome=f"The wallet service could not be queried: {type(exc).__name__}.",
            question="I could not read the wallet vault. Please tell me which wallet this task should use.",
        )

    active = [w for w in wallets if getattr(w, "is_active", False)]
    if not active:
        return VerificationResult.failed(
            method="wallet_available",
            outcome="No active wallet is available to this task.",
            question="No wallet is available. Please import or activate a wallet, then tell me to re-check.",
        )
    return VerificationResult.satisfied(
        method="wallet_available",
        outcome=f"{len(active)} active wallet(s) are available.",
    )


async def _verify_configuration(
    db: AsyncSession, task: Task, request: InterventionRequest
) -> VerificationResult:
    """Report configuration readiness for the subsystem named on the request."""
    from backend.app.api.v1.endpoints.system import _telegram_readiness

    ref = (request.integration_ref or "").lower()
    if "telegram" in ref:
        readiness = _telegram_readiness()
        if readiness.get("ready"):
            return VerificationResult.satisfied(
                method="telegram_readiness",
                outcome="Telegram is configured and a session file exists.",
            )
        return VerificationResult.failed(
            method="telegram_readiness",
            outcome=str(readiness.get("hint") or "Telegram is not configured."),
            question=str(readiness.get("hint") or "Please confirm the Telegram configuration, then tell me to re-check."),
        )

    return VerificationResult.unchecked(
        method=NOT_CHECKED,
        outcome="No automated configuration check covers this request.",
        question="Please tell me which setting you changed, so I can check it before continuing.",
    )


def register_default_verifiers() -> None:
    """Install the built-in verifiers. Safe to call more than once."""
    register(TELEGRAM_SESSION_UNAVAILABLE, _verify_telegram_session)
    register(BROWSER_SESSION_UNAVAILABLE, _verify_live_session)
    register(EXTENSION_INSTALL_REQUIRED, _verify_extension_installed)
    register(HUMAN_VERIFICATION, _verify_live_session)
    register(MISSING_AUTHENTICATION, _verify_authentication)
    register(MISSING_CONFIGURATION, _verify_configuration)


register_default_verifiers()