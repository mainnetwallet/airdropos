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
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.inbox.service import (
    BROWSER_SESSION_UNAVAILABLE,
    HUMAN_VERIFICATION,
    MISSING_AUTHENTICATION,
    MISSING_CONFIGURATION,
    TELEGRAM_SESSION_UNAVAILABLE,
    InterventionRequest,
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
    verifier = verifier_for(request.reason_code)
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
            method=request.reason_code,
            outcome=f"The check did not finish within {VERIFY_TIMEOUT_SECONDS:.0f}s.",
            question="The check timed out. Please confirm the condition is resolved, or tell me what is still failing.",
        )
    except Exception as exc:
        logger.warning("Verification for %s failed: %s", request.request_id, exc)
        return VerificationResult.unchecked(
            method=request.reason_code,
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

    Reports the first concrete reason it is not usable. When Telethon and credentials are
    present it performs the real authorisation check rather than inferring readiness from a
    file being on disk.
    """
    from pathlib import Path

    from backend.app.core.config import settings

    problems: List[str] = []
    if not settings.TELEGRAM_USER_ENABLED:
        problems.append("TELEGRAM_USER_ENABLED is not set to true.")
    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        problems.append("TELEGRAM_API_ID or TELEGRAM_API_HASH is not configured.")
    session_file = Path(f"{settings.TELEGRAM_SESSION_PATH}.session")
    if not session_file.exists():
        problems.append("No Telegram session file exists yet.")

    try:
        import telethon  # noqa: F401
    except Exception:
        problems.append("Telethon is not installed in this process.")
        return VerificationResult.failed(
            method="telegram_session_readiness",
            outcome="Telegram is not ready: " + " ".join(problems),
            question="Telethon is not installed on the server. Install it, then tell me to re-check.",
        )

    if problems:
        return VerificationResult.failed(
            method="telegram_session_readiness",
            outcome="Telegram is not ready: " + " ".join(problems),
            question="Telegram is not configured for this deployment. Can you confirm the intended Telegram account, or tell me to continue without it?",
        )

    # Everything is configured and a session file exists. Ask Telethon directly.
    try:
        from telethon import TelegramClient

        client = TelegramClient(
            settings.TELEGRAM_SESSION_PATH,
            int(settings.TELEGRAM_API_ID),
            str(settings.TELEGRAM_API_HASH),
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return VerificationResult.failed(
                    method="telegram_is_user_authorized",
                    outcome="The Telegram session file exists but the account is not authorised.",
                    question="Please complete the Telegram sign-in for this session, then tell me to re-check.",
                )
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    except Exception as exc:
        return VerificationResult.unchecked(
            method="telegram_is_user_authorized",
            outcome=f"The Telegram session check could not complete: {type(exc).__name__}.",
            question=(
                "I could not reach Telegram to confirm the session. Please confirm the account is "
                "signed in, or tell me what the sign-in step reported."
            ),
        )

    return VerificationResult.satisfied(
        method="telegram_is_user_authorized",
        outcome="Telegram reported the session is connected and authorised.",
    )


# ----------------------------------------------------------------------
# Browser / live sessions
# ----------------------------------------------------------------------
async def _verify_live_session(
    db: AsyncSession, task: Task, request: InterventionRequest
) -> VerificationResult:
    """Check the live browser session that is holding this task up.

    Uses the existing gateway client the live pages already read from. If the gateway is not
    reachable this returns "unchecked" rather than assuming the session is fine.
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
        session = await gateway.get_json(f"/sessions/{integration}/{key}")
    except gateway.GatewayUnavailable:
        return VerificationResult.unchecked(
            method="live_session_state",
            outcome="The worker gateway is not reachable, so the session cannot be inspected.",
            question="I cannot inspect the live session right now. Please tell me whether the step you were asked to do is now complete.",
        )

    waiting = session.get("waiting") or None
    if waiting:
        reason = str(waiting.get("reason") or "the session is still waiting for you")
        return VerificationResult.failed(
            method="live_session_state",
            outcome=f"The session is still waiting: {reason}",
            question="That session still reports it is waiting. Please finish the step it is asking for, then tell me to re-check.",
        )
    if session.get("status") in ("stopped", "failed"):
        return VerificationResult.failed(
            method="live_session_state",
            outcome=f"The session is {session.get('status')}.",
            question="That session has ended. Tell me whether to restart it or continue this task another way.",
        )

    return VerificationResult.satisfied(
        method="live_session_state",
        outcome=f"The {integration} session reports no outstanding verification.",
        status=session.get("status"),
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
    register(HUMAN_VERIFICATION, _verify_live_session)
    register(MISSING_AUTHENTICATION, _verify_authentication)
    register(MISSING_CONFIGURATION, _verify_configuration)


register_default_verifiers()