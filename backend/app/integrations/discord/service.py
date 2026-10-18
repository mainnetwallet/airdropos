"""The one Discord capability the agent and the UI both go through.

Responsibilities, in order:

1. Resolve the browser profile. Always explicitly - never "the default", never a
   substitution.
2. Check the operation against the task's own wording, so a read-only task cannot
   send.
3. Refuse a duplicate or an uncertain send BEFORE anything is typed.
4. Run it through the browser adapter.
5. Record what happened, including "we do not know whether it happened".

Everything the agent needs comes back in one envelope, so it never has to guess
which path ran, why, and what the evidence was.

The safety properties this module is responsible for
----------------------------------------------------
* **No silent account switching.** A profile id that does not exist is an error, not
  a reason to use a different one. Acting as the wrong person is the one failure
  that cannot be undone.
* **Read-only stays read-only.** Enforced from the task text by ``routing``, so it
  holds even when the model is persuaded by something it read in a channel.
* **No blind retry of a send.** An unconfirmed send is recorded as UNCERTAIN and
  blocks a repeat until someone verifies.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.browser import profiles as profile_store
from backend.app.integrations.discord import browser, routing, store
from backend.app.integrations.discord.errors import (
    CAT_LOGIN_REQUIRED,
    DiscordBrowserError,
    DiscordError,
    DiscordIntentError,
    DiscordLoginRequired,
    DiscordProfileError,
    DiscordUnknownOutcome,
)
from backend.app.integrations.discord.models import (
    BLOCKED,
    FAILED,
    OK,
    SKIPPED_DUPLICATE,
    UNCERTAIN,
    DiscordOutcome,
    text_fingerprint,
)

logger = logging.getLogger(__name__)

#: How long an identical, KNOWN-COMPLETED send is suppressed. Operator-configurable
#: through the ``discord.duplicate_window_hours`` system setting (default 6h).
DEFAULT_DUPLICATE_WINDOW_HOURS = 6


# ======================================================================
# Profile resolution
# ======================================================================
def require_profile(profile_id: Optional[str]) -> str:
    """The profile this operation must use, or a clear failure.

    Deliberately strict. No default, no fallback, no "the profile the agent usually
    uses": if the caller did not name an existing profile, the answer is an error the
    operator can act on.
    """
    raw = str(profile_id or "").strip()
    if not raw:
        raise DiscordProfileError(
            "No Discord browser profile was selected. Every Discord task must name the existing "
            "browser profile of the account it should act as. Pick one on the Discord page.",
            status_code=400,
        )
    normalized = profile_store.normalize_session_key(raw)
    if profile_store.get_profile(normalized) is None:
        raise DiscordProfileError(
            f"There is no browser profile with the id '{normalized}'. "
            "AirdropOS will not substitute a different profile, because that would act as a "
            "different Discord account than the one you selected.",
            status_code=404,
        )
    return normalized


def available_profiles() -> List[Dict[str, Any]]:
    """Existing browser profiles, described for a Discord account selector.

    Everything here is already public: an id, a name, a description and Chrome
    settings. No cookie, no storage, no token - AirdropOS never reads or exposes those.
    """
    out: List[Dict[str, Any]] = []
    for profile in profile_store.list_profiles():
        problems = profile_store.validate_profile_config(profile)
        out.append(
            {
                "id": profile.id,
                "name": profile.name,
                "description": profile.description,
                "launch_mode": profile.launch_mode,
                "browser_type": profile.browser_type,
                "headless": profile.headless,
                "user_data_dir": profile.resolved_user_data_dir,
                "start_url": profile.start_url,
                "is_default_agent": profile.is_default_agent,
                "last_used_at": profile.last_used_at,
                "last_opened_at": profile.last_opened_at,
                "last_error": profile.last_error,
                "can_open": not problems,
                "open_problems": problems,
            }
        )
    return out


# ======================================================================
# Status
# ======================================================================
async def integration_status(db: AsyncSession, profile_id: Optional[str] = None) -> Dict[str, Any]:
    """Everything the Discord page needs, without touching Discord.

    Deliberately does NOT read the login state: that requires a real browser
    operation, and calling it here would turn a page load into a live Chrome probe.
    The page asks for login state separately, and only when the operator asks.
    """
    profiles = available_profiles()
    return {
        "mode": "browser",
        "note": (
            "Discord is driven through your own signed-in Chrome profiles on discord.com. "
            "There is no bot token, no API key and no stored credential: you log in by hand and "
            "AirdropOS reuses that session."
        ),
        "profiles": profiles,
        "profile_count": len(profiles),
        "openable_profile_count": sum(1 for p in profiles if p["can_open"]),
        "selected_profile_id": str(profile_id or ""),
        "catalog": routing.describe_catalog(),
        "policy_note": (
            "A read-only task can never send. A draft task can never send either - it returns "
            "text for you to review. Sending needs write intent, and the result is verified "
            "against the page before it is reported as sent."
        ),
        "anti_spam_note": (
            "Bulk unsolicited messaging, fake engagement and mass invitations are out of scope "
            "and are not implemented. Discord's own rate limits and server rules apply."
        ),
    }


# ======================================================================
# Duplicate and uncertainty guards
# ======================================================================
async def duplicate_window_seconds(db: AsyncSession) -> int:
    """The configured duplicate window, read fresh.

    Read per call because the API and the worker are separate processes: a value
    cached in one is a value the other ignores.
    """
    from backend.app.core.config import settings
    from backend.app.services.preferences import DISCORD_DUPLICATE_WINDOW_HOURS, get_preference

    raw = await get_preference(db, DISCORD_DUPLICATE_WINDOW_HOURS, default="")
    if raw == "":
        try:
            hours = float(getattr(settings, "DISCORD_DUPLICATE_WINDOW_HOURS", DEFAULT_DUPLICATE_WINDOW_HOURS))
        except (TypeError, ValueError):
            hours = DEFAULT_DUPLICATE_WINDOW_HOURS
    else:
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            hours = DEFAULT_DUPLICATE_WINDOW_HOURS
    hours = max(0.0, min(hours, 24 * 30))
    return int(hours * 3600)


async def _resolve_send_target(profile_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Work out which channel a send would actually land in.

    Three cases, in order:

    1. The caller named a channel. It is opened first, so navigation and the send
       happen inside ONE interaction-lock acquisition. That makes the whole send
       atomic against another task on the same profile, which per-call locking alone
       cannot guarantee.
    2. No channel named: observe where the page actually is. Guessing here would make
       the duplicate guard both miss real duplicates and block legitimate ones.
    3. The page is not on a channel at all: refuse. Sending into an unknown channel is
       worse than not sending.
    """
    server_id = str(params.get("server_id") or "")
    channel_id = str(params.get("channel_id") or "")
    channel_name = str(params.get("channel_name") or "")

    if server_id and channel_id:
        try:
            opened = await browser.open_channel(
                profile_id, server_id=server_id, channel_id=channel_id
            )
        except DiscordError as exc:
            return {"__refused__": True, "detail": str(exc)}
        return {**(opened.get("channel") or {}), "opened": True}

    if channel_name:
        try:
            opened = await browser.open_channel(
                profile_id, server_id=server_id, channel_name=channel_name
            )
        except DiscordError as exc:
            return {"__refused__": True, "detail": str(exc)}
        return {**(opened.get("channel") or {}), "opened": True}

    try:
        observed = await browser.channel_of(profile_id)
    except DiscordError as exc:
        return {"__refused__": True, "detail": str(exc)}
    if not observed.get("ok"):
        return {
            "__refused__": True,
            "detail": (
                str(observed.get("detail") or "The profile is not on a Discord channel.")
                + " Open the channel you want to post in, or name it, and try again."
            ),
        }
    return {**(observed.get("channel") or {}), "observed": True}


class Guard:
    """The verdict of the pre-send checks. ``None`` means "you may send"."""

    def __init__(self, outcome: Optional[DiscordOutcome] = None) -> None:
        self.outcome = outcome

    @property
    def blocked(self) -> bool:
        return self.outcome is not None


async def guard_send(
    db: AsyncSession,
    *,
    profile_id: str,
    body: str,
    reply_to: str = "",
    channel_id: str = "",
    task_id: str = "",
) -> Guard:
    """Refuse a send that may already have been made.

    Two distinct situations, handled differently:

    * A previous attempt whose outcome is UNKNOWN: do NOT send. Not ever, not for any
      window length, and not because time passed. A retry here is how somebody gets
      the same message twice. The block is lifted only when a human resolves it -
      ``discord_verify_message`` when the message is seen, or the operator's explicit
      resolution through the API.
    * A previous SUCCESS with the same fingerprint: the action is DONE. Report the
      original evidence instead of doing it again. Skipped when the operator has set
      the window to 0, which is the only sanctioned way to repeat on purpose.
    """
    key = store.send_key(
        profile_id, body, reply_to=reply_to, channel_id=channel_id, task_id=task_id
    )

    pending = await store.find_uncertain(db, key)
    if pending is not None:
        return Guard(
            DiscordOutcome(
                ok=False,
                operation="send_message",
                status=UNCERTAIN,
                uncertain=True,
                error_category="DISCORD_OUTCOME_UNKNOWN",
                fingerprint=key,
                data={"activity_id": str(pending.id), "operation_key": key},
                error=(
                    "An earlier attempt at this exact message returned no confirmation, so it may "
                    f"or may not have been sent (activity {pending.id}). It was NOT sent again. "
                    "Run discord_verify_message to check the channel; if it is not there, an "
                    "operator can resolve the attempt so it can be sent once."
                ),
                notes=["Ambiguous writes are never repeated automatically."],
            )
        )

    window = await duplicate_window_seconds(db)
    if window <= 0:
        return Guard()
    previous = await store.find_completed(db, key, within_seconds=window)
    if previous is None:
        return Guard()
    return Guard(
        DiscordOutcome(
            ok=True,
            operation="send_message",
            status=SKIPPED_DUPLICATE,
            fingerprint=key,
            data={
                "already_completed": True,
                "original_activity_id": str(previous.id),
                "completed_at": previous.created_at.isoformat() if previous.created_at else "",
                "duplicate_window_hours": window / 3600,
            },
            evidence_path=previous.evidence_path or "",
            notes=[
                f"This exact message already succeeded at "
                f"{previous.created_at.isoformat() if previous.created_at else 'an earlier time'}. "
                "Nothing was sent again."
            ],
        )
    )


# ======================================================================
# The executor
# ======================================================================
async def execute(
    db: AsyncSession,
    operation: str,
    *,
    browser_profile_id: Optional[str] = None,
    task_text: str = "",
    intent: Optional[str] = None,
    task_id: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    allow_duplicate: bool = False,
) -> DiscordOutcome:
    """Run one Discord operation against one explicitly named browser profile.

    This is the only entry point the tools and the API use, so profile resolution,
    intent enforcement, duplicate prevention and recording cannot drift between the
    chat console, the task worker and the Discord page.
    """
    outcome = await _execute(
        db,
        operation,
        browser_profile_id=browser_profile_id,
        task_text=task_text,
        intent=intent,
        task_id=task_id,
        params=params,
        allow_duplicate=allow_duplicate,
    )
    # Stamp the account onto every outcome that resolved a profile, including the
    # failures. A parked login request has to name the exact profile the operator
    # must open, so this must not depend on which branch produced the result.
    resolved = str(browser_profile_id or "").strip()
    if resolved:
        from backend.app.browser import profiles as store

        outcome.browser_profile_id = store.normalize_session_key(resolved)
    return outcome


async def _execute(
    db: AsyncSession,
    operation: str,
    *,
    browser_profile_id: Optional[str] = None,
    task_text: str = "",
    intent: Optional[str] = None,
    task_id: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    allow_duplicate: bool = False,
) -> DiscordOutcome:
    params = dict(params or {})
    op = routing.OPERATIONS.get(operation)
    if op is None:
        return DiscordOutcome(
            ok=False,
            operation=operation,
            status=FAILED,
            error=(
                f"'{operation}' is not a Discord operation this integration implements. "
                f"Supported: {', '.join(sorted(routing.OPERATIONS))}."
            ),
            error_category="DISCORD_UNSUPPORTED_OPERATION",
        )

    started = time.monotonic()

    # 1. The account. Strict on purpose.
    try:
        profile_id = require_profile(browser_profile_id)
    except DiscordProfileError as exc:
        return DiscordOutcome(
            ok=False, operation=operation, status=FAILED, error=str(exc), error_category=exc.category
        )

    # 2. The boundary between reading and writing, from the operator's own words.
    try:
        reading = routing.enforce_intent(operation, task_text=task_text, intent=intent)
    except DiscordIntentError as exc:
        await _record(
            db,
            operation=operation,
            profile_id=profile_id,
            intent=reading_intent(op),
            status=FAILED,
            error=str(exc),
            error_category=exc.category,
            task_id=task_id,
            elapsed=started,
        )
        return DiscordOutcome(
            ok=False, operation=operation, status=FAILED, error=str(exc), error_category=exc.category
        )

    level = reading.level if reading else (op.intent if op.intent != routing.INTENT_UNKNOWN else routing.INTENT_READ)

    # 3. Duplicate / uncertainty guard, before anything is typed.
    #
    # The channel is OBSERVED from the page rather than taken from the caller's
    # arguments. That matters twice: it is what the message would actually land in,
    # and it closes the window where another task on the same profile navigates
    # between this guard and the send.
    fingerprint = ""
    observed_channel: Dict[str, Any] = {}
    if op.is_write and operation in ("send_message", "reply_to_message"):
        body = str(params.get("text") or "")
        reply_to = str(params.get("reply_to_message_id") or "")
        target_channel = await _resolve_send_target(profile_id, params)
        observed_channel = target_channel
        if target_channel.get("__refused__"):
            return DiscordOutcome(
                ok=False,
                operation=operation,
                status=FAILED,
                error=str(target_channel.get("detail") or "The target channel could not be resolved."),
                error_category="DISCORD_CHANNEL_UNRESOLVED",
            )
        channel_id = str(target_channel.get("channel_id") or "")
        if allow_duplicate:
            fingerprint = ""
        else:
            verdict = await guard_send(
                db,
                profile_id=profile_id,
                body=body,
                reply_to=reply_to,
                channel_id=channel_id,
                task_id=task_id or "",
            )
            if verdict.blocked:
                assert verdict.outcome is not None
                verdict.outcome.operation = operation
                return verdict.outcome
            fingerprint = store.send_key(
                profile_id, body, reply_to=reply_to, channel_id=channel_id, task_id=task_id or ""
            )
        # Carried into the send so it can refuse if the page moved in the meantime.
        params = {**params, "_guard_channel_id": channel_id}

    if op.consequential and not params.get("confirmed"):
        message = (
            f"'{op.label}' is irreversible and needs explicit authorization. Nothing was done."
        )
        return DiscordOutcome(
            ok=False,
            operation=operation,
            status=BLOCKED,
            error=message,
            error_category="DISCORD_CONFIRMATION_REQUIRED",
        )

    # 4. Run it.
    try:
        data = await _dispatch(
            db, browser_profile_id=profile_id, operation=operation, params=params,
            profile_id=profile_id, task_id=task_id,
        )
    except DiscordLoginRequired as exc:
        await _record(
            db,
            operation=operation,
            profile_id=profile_id,
            intent=level,
            status=BLOCKED,
            needs_user=True,
            error=str(exc),
            error_category=CAT_LOGIN_REQUIRED,
            task_id=task_id,
            elapsed=started,
            fingerprint=fingerprint,
        )
        return DiscordOutcome(
            ok=False,
            operation=operation,
            status=BLOCKED,
            needs_user=True,
            error=str(exc),
            error_category=CAT_LOGIN_REQUIRED,
            fingerprint=fingerprint,
            data={"indicator": exc.indicator},
            notes=[
                "Discord is showing its sign-in form. Sign in manually in this browser profile; "
                "AirdropOS will not type your password or pass MFA."
            ],
        )
    except DiscordUnknownOutcome as exc:
        await _record(
            db,
            operation=operation,
            profile_id=profile_id,
            intent=level,
            status=UNCERTAIN,
            uncertain=True,
            error=str(exc),
            error_category=exc.category,
            task_id=task_id,
            elapsed=started,
            fingerprint=fingerprint,
            summary=dict(exc.detail),
        )
        return DiscordOutcome(
            ok=False,
            operation=operation,
            status=UNCERTAIN,
            uncertain=True,
            error=str(exc),
            error_category=exc.category,
            fingerprint=fingerprint,
            data=dict(exc.detail),
            notes=[
                "This send was NOT repeated. Verify the channel with discord_verify_message "
                "before deciding whether it landed."
            ],
        )
    except DiscordError as exc:
        await _record(
            db,
            operation=operation,
            profile_id=profile_id,
            intent=level,
            status=FAILED,
            error=str(exc),
            error_category=exc.category,
            task_id=task_id,
            elapsed=started,
            fingerprint=fingerprint,
        )
        return DiscordOutcome(
            ok=False, operation=operation, status=FAILED, error=str(exc), error_category=exc.category
        )
    except Exception as exc:  # argument problems surface as clean errors, not 500s
        logger.exception("Discord operation %s failed unexpectedly", operation)
        detail = f"{type(exc).__name__}: the Discord operation could not be completed."
        await _record(
            db,
            operation=operation,
            profile_id=profile_id,
            intent=level,
            status=FAILED,
            error=detail,
            error_category="DISCORD_UNEXPECTED",
            task_id=task_id,
            elapsed=started,
        )
        return DiscordOutcome(
            ok=False,
            operation=operation,
            status=FAILED,
            error=detail,
            error_category="DISCORD_UNEXPECTED",
        )

    channel = _channel_from(data)
    await _record(
        db,
        operation=operation,
        profile_id=profile_id,
        intent=level,
        status=OK,
        task_id=task_id,
        elapsed=started,
        fingerprint=fingerprint,
        summary=_summary_from(data),
        evidence_path=str(data.get("screenshot_path") or ""),
        target=_target_label(operation, params, channel),
    )
    return DiscordOutcome(
        ok=True,
        operation=operation,
        status=OK,
        data=data,
        channel=channel,
        evidence_path=str(data.get("screenshot_path") or ""),
        fingerprint=fingerprint,
        notes=[
            "Done through the selected signed-in browser profile. The page's own state after "
            "the action is the evidence; there is no API record to cross-check."
        ],
    )


def reading_intent(op: routing.Operation) -> str:
    return op.intent if op.intent != routing.INTENT_UNKNOWN else routing.INTENT_READ


async def _dispatch(
    db: AsyncSession,
    *,
    browser_profile_id: str,
    operation: str,
    params: Dict[str, Any],
    profile_id: str = "",
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Map an operation to its browser call.

    ``db`` is threaded through for the one operation that records its own evidence:
    a verification that FINDS a message is what closes an uncertain send, so it has
    to leave a row behind rather than only answering the caller.
    """
    if operation == "open_app":
        return await browser.open_app(browser_profile_id, str(params.get("path") or ""))
    if operation == "list_servers":
        return await browser.list_servers(browser_profile_id)
    if operation == "open_channel":
        return await browser.open_channel(
            browser_profile_id,
            server_id=str(params.get("server_id") or ""),
            channel_id=str(params.get("channel_id") or ""),
            channel_name=str(params.get("channel_name") or ""),
        )
    if operation == "read_channel":
        return await browser.read_messages(
            browser_profile_id,
            limit=_int(params.get("limit"), 30, 1, 100),
            older_pages=_int(params.get("older_pages"), 0, 0, 20),
        )
    if operation == "search_messages":
        return await browser.search_messages(
            browser_profile_id,
            str(params.get("query") or ""),
            limit=_int(params.get("limit"), 25, 1, 60),
        )
    if operation == "find_unanswered_questions":
        return await browser.find_unanswered_questions(
            browser_profile_id,
            limit=_int(params.get("limit"), 30, 1, 100),
            older_pages=_int(params.get("older_pages"), 1, 0, 10),
        )
    if operation == "send_message":
        return await browser.send_message(
            browser_profile_id,
            str(params.get("text") or ""),
            expected_channel_id=str(params.get("_guard_channel_id") or ""),
        )
    if operation == "reply_to_message":
        return await browser.send_message(
            browser_profile_id,
            str(params.get("text") or ""),
            reply_to_message_id=str(params.get("reply_to_message_id") or ""),
            expected_channel_id=str(params.get("_guard_channel_id") or ""),
        )
    if operation == "join_server":
        return await browser.join_server(browser_profile_id, str(params.get("invite") or ""))
    if operation == "leave_server":
        return await browser.leave_server(
            browser_profile_id,
            str(params.get("server_name") or ""),
            confirmed=bool(params.get("confirmed")),
        )
    if operation == "verify_message":
        result = await browser.verify_message_present(
            browser_profile_id,
            str(params.get("text") or ""),
            limit=_int(params.get("limit"), 50, 1, 100),
        )
        # A verify that FINDS the message is the evidence that closes an uncertain
        # send, so record it as one. The duplicate guard then treats it as an ordinary
        # completed send and stops blocking - which is correct: the message is known
        # to exist, and repeating it is what we were protecting against.
        #
        # A verify that does NOT find it deliberately resolves nothing. "Not in the
        # last N messages" is not proof of absence, and turning that into permission
        # to resend is exactly the duplicate this guard exists to prevent. The way out
        # is the operator's explicit resolution, not a timeout.
        if result.get("found"):
            # Mark it so the single activity row this operation already writes
            # carries the confirmation. NOT a second row: one operation, one record.
            result["_resolves_uncertain"] = True
            result["_confirmed_excerpt"] = state_excerpt(result)
        return result
    raise DiscordBrowserError(f"'{operation}' has no browser implementation.")


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _channel_from(data: Optional[Dict[str, Any]]):
    from backend.app.integrations.discord.models import ChannelRef

    raw = (data or {}).get("channel")
    if not isinstance(raw, dict) or not raw:
        return None
    return ChannelRef(
        server_id=str(raw.get("server_id") or ""),
        server_name=str(raw.get("server_name") or ""),
        channel_id=str(raw.get("channel_id") or ""),
        channel_name=str(raw.get("channel_name") or ""),
    )


def _summary_from(data: Dict[str, Any]) -> Dict[str, Any]:
    """The persistable slice of a result: identifiers and a bounded excerpt.

    Message bodies are deliberately reduced to a fingerprint plus a short excerpt by
    ``MessageRecord.summary`` upstream; this only collects what the adapter returned.
    """
    summary: Dict[str, Any] = {}
    channel = data.get("channel")
    if isinstance(channel, dict):
        summary["channel"] = {k: str(v)[:120] for k, v in channel.items()}
    evidence = data.get("evidence")
    if isinstance(evidence, dict):
        message = evidence.get("message")
        if isinstance(message, dict):
            summary["message_id"] = message.get("message_id", "")
            summary["excerpt"] = message.get("excerpt", "")
            summary["permalink"] = message.get("permalink", "")
    for key in ("message_id", "permalink", "excerpt", "result_count", "message_count", "query", "found"):
        if key in data and key not in summary:
            value = data[key]
            summary[key] = str(value)[:300] if not isinstance(value, (dict, list)) else ""
    # A positive verification is the evidence that closes an uncertain send. Carried
    # on the normal activity row so the history shows why that block was lifted.
    if data.get("_resolves_uncertain"):
        summary["resolves_uncertain"] = True
        summary["confirmed_excerpt"] = str(data.get("_confirmed_excerpt") or "")[:240]
    return summary


def state_excerpt(result: Dict[str, Any]) -> str:
    """A bounded excerpt of a verified message, for the activity record."""
    message = result.get("message") if isinstance(result.get("message"), dict) else {}
    return str(message.get("content") or "")[:240]


async def resolve_uncertain_send(
    db: AsyncSession,
    *,
    profile_id: str,
    body: str,
    resolution: str,
    note: str = "",
    operation_key: str = "",
) -> Dict[str, Any]:
    """An operator's explicit decision about an uncertain send.

    The ONLY way an uncertain attempt stops blocking that message. It is deliberately
    not reachable from the agent: a model that could resolve its own uncertainty
    would resolve it in the direction that lets it act, which defeats the mechanism.

    ``sent`` marks it delivered. ``not_sent`` abandons the attempt so the message can
    be sent once. Either way the decision is written into the history.
    """
    if resolution not in ("sent", "not_sent"):
        raise DiscordError(
            "resolution must be 'sent' or 'not_sent'.", category="INVALID_INPUT", status_code=400
        )

    row = None
    if operation_key:
        row = await store.resolve_uncertain(db, operation_key, resolution=resolution, note=note)
    if row is None:
        # No exact key (the UI may not have kept it, and the key is channel- and
        # task-scoped). Fall back to the message FINGERPRINT across this profile's
        # unconfirmed rows, newest first, so an operator resolves the same message
        # text they were shown regardless of which channel it was attempted in.
        needle = text_fingerprint(body)
        candidates = await store.list_activity(
            db, limit=200, browser_profile_id=profile_id
        )
        for candidate in candidates:
            if not candidate.outcome_uncertain:
                continue
            if candidate.operation_key.endswith(needle):
                row = await store.resolve_uncertain(
                    db, candidate.operation_key, resolution=resolution, note=note
                )
                if row is not None:
                    break
    if row is None:
        raise DiscordError(
            "There is no unconfirmed Discord send matching that message for this profile.",
            category="NOT_FOUND",
            status_code=404,
        )
    return {
        "resolved": True,
        "activity_id": str(row.id),
        "resolution": resolution,
        "operation_key": row.operation_key,
        "status": row.status,
        "note": (
            "Recorded as delivered. This message will not be sent again."
            if resolution == "sent"
            else "Recorded as never delivered. The message may be sent once."
        ),
    }


def _target_label(operation: str, params: Dict[str, Any], channel) -> str:
    """A short, non-secret label for the activity row."""
    channel_label = channel.label if channel else ""
    if operation in ("send_message", "reply_to_message", "verify_message"):
        digest = text_fingerprint(str(params.get("text") or ""))[:24]
        suffix = f" reply->{params.get('reply_to_message_id')}" if params.get("reply_to_message_id") else ""
        return f"text:{digest}{suffix} in {channel_label or 'current channel'}"[:512]
    if operation == "search_messages":
        return f"query:{str(params.get('query') or '')[:100]}"
    if operation == "join_server":
        return f"invite:{str(params.get('invite') or '')[-8:]}"
    if operation == "leave_server":
        return f"server:{str(params.get('server_name') or '')[:100]}"
    if channel_label:
        return channel_label[:512]
    return f"profile:{params.get('browser_profile_id') or ''}"[:512]


async def _record(
    db: AsyncSession,
    *,
    operation: str,
    profile_id: str,
    intent: str,
    status: str,
    task_id: Optional[str] = None,
    error: str = "",
    error_category: str = "",
    uncertain: bool = False,
    needs_user: bool = False,
    elapsed: Optional[float] = None,
    fingerprint: str = "",
    summary: Optional[Dict[str, Any]] = None,
    evidence_path: str = "",
    target: str = "",
) -> None:
    """Write the activity row. Never allowed to fail the operation."""
    try:
        await store.record_activity(
            db,
            browser_profile_id=profile_id,
            operation=operation,
            intent=intent,
            status=BLOCKED if needs_user else status,
            target=target or f"profile:{profile_id}",
            operation_key=fingerprint,
            outcome_uncertain=uncertain,
            result_summary=dict(summary or {}),
            evidence_path=evidence_path,
            error=error,
            error_category=error_category,
            duration_ms=int((time.monotonic() - elapsed) * 1000) if elapsed else None,
            task_id=task_id,
        )
        await db.commit()
    except Exception as exc:
        # A history row is bookkeeping. Losing it must not fail the user's task.
        logger.info("Could not record Discord activity for %s: %s", operation, type(exc).__name__)
        try:
            await db.rollback()
        except Exception:
            pass