"""The single X.com capability the agent and the UI both go through.

Responsibilities, in this order:

1. Resolve the account/profile a task targets.
2. Decide the method (:mod:`backend.app.integrations.x.routing`).
3. Refuse a duplicate write BEFORE sending anything.
4. Execute through the API client or the browser adapter.
5. Record what happened, including "we do not know whether it happened".
6. Verify when the outcome is uncertain, instead of retrying.

Everything the agent needs is returned in one envelope so it never has to guess
which method ran, why, and what the evidence was.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.integrations.x import browser_adapter, routing
from backend.app.integrations.x.client import (
    AUTHORIZATION_SCOPES,
    XApiClient,
    XApiResult,
    build_authorize_url,
    generate_pkce,
    x_api_enabled,
)
from backend.app.integrations.x.crypto import DecryptionFailed, VaultUnavailable, vault_status
from backend.app.integrations.x.routing import Operation
from backend.app.integrations.x.store import (
    StoreError,
    decrypt_credentials,
    find_recent,
    find_uncertain,
    record_activity,
    record_check,
    resolve_uncertain,
    select_browser_profile,
    select_configuration,
    store_refreshed_tokens,
    touch_configuration,
    token_needs_refresh,
)
from backend.app.models import XApiConfiguration
from backend.app.models.x_integration import (
    CONFIG_AUTH_REQUIRED,
    CONFIG_ERROR,
    CONFIG_INVALID,
    CONFIG_OK,
    CONFIG_RATE_LIMITED,
    CONFIG_UNVERIFIED,
)

logger = logging.getLogger(__name__)

# Activity statuses.
OK = "SUCCESS"
FAILED = "FAILED"
UNCERTAIN = "UNCERTAIN"
BLOCKED = "NEEDS_USER_ACTION"
SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"

# How long the same fingerprint is treated as "already done".
#
# Operator-configurable (system setting `x.duplicate_window_hours`, default 6).
# It has to be: without a knob, an operator who legitimately wants to publish the
# same announcement twice has no recourse at all, because the one escape hatch
# (`allow_duplicate`) is deliberately unreachable from any tool schema.
#
#   0   -> suppress only the UNCERTAIN guard: never resend a write whose outcome
#          is unknown, but allow a deliberate repeat of one that is known done.
#   >0  -> also refuse a known-duplicate within the window.
DEFAULT_DUPLICATE_WINDOW_HOURS = 6

# The X API requires `offline.access` at authorization time for a refresh token
# to ever be issued.
REQUIRED_SCOPES = tuple(s for s in AUTHORIZATION_SCOPES)


@dataclass
class XOutcome:
    """One X operation, whichever method ran it."""

    ok: bool
    operation: str
    method: str
    status: str
    data: Dict[str, Any] = field(default_factory=dict)
    route: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_category: str = ""
    uncertain: bool = False
    needs_user: bool = False
    blocked_reason: str = ""
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": self.operation,
            "method": self.method,
            "status": self.status,
            "data": self.data,
            "route": self.route,
            "error": self.error,
            "error_category": self.error_category,
            "outcome_uncertain": self.uncertain,
            "needs_user_action": self.needs_user,
            "blocked_reason": self.blocked_reason,
            "notes": list(self.notes),
        }


class XError(Exception):
    """An X operation could not be carried out. `outcome` carries the detail."""

    def __init__(self, message: str, *, category: str = "X_ERROR", status_code: int = 400) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code


# ======================================================================
# Configuration / status
# ======================================================================
async def configuration_status(
    db: AsyncSession, *, config_id: Optional[str] = None
) -> Dict[str, Any]:
    """Everything the overview needs, without any secret."""
    from backend.app.integrations.x.store import list_configurations, public_configuration

    profile_id, profile_reason = select_browser_profile()
    rows = await list_configurations(db)
    enabled = [r for r in rows if r.is_enabled]
    authorized = [r for r in enabled if r.access_token_enc]
    return {
        "api_enabled": x_api_enabled(),
        "vault": vault_status(),
        "configurations": [public_configuration(r) for r in rows],
        "configuration_count": len(rows),
        "enabled_count": len(enabled),
        "authorized_count": len(authorized),
        "verified_count": sum(1 for r in authorized if r.status == CONFIG_OK),
        "browser_profile_id": profile_id or "",
        "browser_profile_reason": profile_reason,
        "required_scopes": list(REQUIRED_SCOPES),
        "catalog": routing.describe_catalog(),
        "note": (
            "A write through the API needs an X developer app approved for write access on a "
            "paid plan. Without one X answers 403 and the agent uses the signed-in browser "
            "profile instead."
        ),
    }


async def test_connection(db: AsyncSession, config_id: str) -> Dict[str, Any]:
    """Prove a configuration works, and record exactly what worked.

    ``GET /2/users/me`` is the honest test: it uses only the token itself, tells
    us WHICH account it belongs to, and cannot be faked by a valid client id with
    no user authorization.
    """
    from backend.app.integrations.x.store import get_configuration

    row = await get_configuration(db, config_id)
    if row is None:
        raise StoreError("Configuration not found.", status=404)
    if not row.is_enabled:
        raise StoreError("This configuration is disabled. Enable it before testing.", status=409)
    _check_vault_or_fail(row)

    try:
        client = await _client_for(db, row, allow_refresh=True)
    except DecryptionFailed as exc:
        await record_check(db, row, status=CONFIG_INVALID, detail=str(exc)[:400])
        raise StoreError(str(exc), status=409) from exc
    if client is None:
        raise StoreError(
            "This configuration has no access token, so there is nothing to test. Complete "
            "the OAuth authorization first.",
            status=409,
        )

    result = await client.whoami()
    if result.ok:
        user = (result.data or {}).get("user") or {}
        await record_check(
            db, row, status=CONFIG_OK,
            detail=f"Authorized as @{user.get('username', '?')} (id {user.get('id', '?')}).",
            rate_limit_remaining=result.rate_limit_remaining,
            rate_limit_reset_epoch=result.rate_limit_reset_epoch,
            user_id=str(user.get("id") or ""),
            username=str(user.get("username") or ""),
            scopes=client_scopes(row),
        )
        return {
            "ok": True,
            "status": CONFIG_OK,
            "user": {
                "id": str(user.get("id") or ""),
                "username": str(user.get("username") or ""),
                "name": str(user.get("name") or ""),
            },
            "rate_limit_remaining": result.rate_limit_remaining,
        }

    status = _config_status_for(result)
    await record_check(
        db, row, status=status, detail=result.error[:400],
        rate_limit_remaining=result.rate_limit_remaining,
        rate_limit_reset_epoch=result.rate_limit_reset_epoch,
    )
    return {
        "ok": False,
        "status": status,
        "error": result.error,
        "error_category": result.error_category,
        "rate_limit_remaining": result.rate_limit_remaining,
        "retry_after_seconds": result.retry_after_seconds,
    }


def _config_status_for(result: XApiResult) -> str:
    return {
        "X_AUTH_FAILED": CONFIG_AUTH_REQUIRED,
        "X_PERMISSION_DENIED": CONFIG_INVALID,
        "X_RATE_LIMITED": CONFIG_RATE_LIMITED,
    }.get(result.error_category, CONFIG_ERROR)


def client_scopes(row: XApiConfiguration) -> List[str]:
    return [str(s) for s in (row.scopes or [])]


def _check_vault_or_fail(row: XApiConfiguration) -> None:
    """Refuse to touch a credential the vault cannot open.

    Turns an unsafe-key condition into a clean, actionable 409 rather than a 500:
    "your vault key is unsafe in production" is something an operator can act on
    from that message.
    """
    try:
        decrypt_credentials(row)
    except DecryptionFailed:
        raise
    except VaultUnavailable as exc:
        raise StoreError(str(exc), status=409) from exc


async def _bind_to_expected_account(
    db: AsyncSession, row: XApiConfiguration, access_token: str
) -> Optional[Dict[str, Any]]:
    """Refuse a token that authorizes a DIFFERENT account than expected.

    Returns ``None`` when binding succeeded (or was not required), or a completed
    response describing the refusal.

    An X authorization code can be exchanged for any account the consenting user
    chose. Storing that token without checking would let a configuration act as
    an account the operator never named. So when the row declares a handle, the
    identity is resolved and compared BEFORE the tokens are stored.
    """
    expected = str(row.x_username or "").lstrip("@").strip()
    if not expected:
        # Nothing was declared, so there is nothing to contradict. The account is
        # recorded on the first Test Connection and visible on the page.
        return None

    try:
        secrets_for_row = decrypt_credentials(row)
    except DecryptionFailed:
        secrets_for_row = {}
    probe = XApiClient(
        access_token=access_token,
        client_id=row.client_id,
        client_secret=secrets_for_row.get("client_secret", ""),
        scopes=list(AUTHORIZATION_SCOPES),
    )
    result = await probe.whoami()
    if not result.ok:
        # Could not verify the identity. Refuse rather than store blind: the
        # operator can retry, and an unverified binding is exactly the hole this
        # is here to close.
        await record_check(
            db, row, status=CONFIG_AUTH_REQUIRED,
            detail=f"Authorized, but the account could not be verified: {result.error}"[:400],
        )
        raise StoreError(
            "X issued a token but the account it belongs to could not be verified, so it was NOT "
            f"stored. ({result.error})",
            status=409,
        )

    actual = str(((result.data or {}).get("user") or {}).get("username") or "").lstrip("@")
    if actual.lower() != expected.lower():
        await record_check(
            db, row, status=CONFIG_AUTH_REQUIRED,
            detail=(
                f"Refused a token for @{actual}; this configuration expects @{expected}."
            )[:400],
        )
        raise StoreError(
            f"The authorization code belongs to @{actual}, but this configuration is set up for "
            f"@{expected}. The token was NOT stored. Either authorize @{expected} instead, or "
            "change the expected handle on this configuration to the account you intend to use.",
            status=409,
            field="username",
        )
    return None


# ======================================================================
# OAuth helpers
# ======================================================================
async def begin_authorization(db: AsyncSession, config_id: str) -> Dict[str, Any]:
    """Produce the authorization URL the operator opens to grant access.

    The PKCE verifier is NOT stored here. It is held in memory with a short TTL
    so it can complete the code exchange, and never persisted: a verifier on
    disk next to the code would defeat the point of PKCE.
    """
    from backend.app.integrations.x.store import get_configuration

    row = await get_configuration(db, config_id)
    if row is None:
        raise StoreError("Configuration not found.", status=404)
    if not row.client_id:
        raise StoreError("This configuration has no client id, so authorization cannot start.", status=409)

    pkce = generate_pkce()
    # `state` is the CSRF binding for the code exchange, so it must be
    # UNGUESSABLE. It was previously a hash of the row id and the wall clock,
    # both of which the caller can observe: `GET /x/configurations` returns the
    # id, and the response to this very call carries the timestamp. A random token
    # is the only thing that makes the check mean anything.
    state = secrets.token_urlsafe(32)
    url = build_authorize_url(
        client_id=row.client_id,
        redirect_uri=settings_redirect_uri(),
        code_challenge=pkce["code_challenge"],
        state=state,
        scopes=AUTHORIZATION_SCOPES,
    )
    pending = _remember_pending(str(row.id), pkce["code_verifier"], state)
    return {
        "authorization_url": url,
        "state": state,
        "redirect_uri": settings_redirect_uri(),
        "scopes": list(AUTHORIZATION_SCOPES),
        "expires_in_seconds": pending,
        "note": (
            "Open the URL, sign in to the X account you want AirdropOS to act as, and approve "
            "the listed scopes. X redirects back with a `code`; paste that code below. "
            "Without `offline.access` no refresh token is issued and you would have to "
            "re-authorize whenever the access token expires."
        ),
    }


def settings_redirect_uri() -> str:
    from backend.app.core.config import settings

    return str(getattr(settings, "X_OAUTH_REDIRECT_URI", "") or "")


_PENDING: Dict[str, Dict[str, Any]] = {}
PENDING_TTL_SECONDS = 900


def _remember_pending(config_id: str, verifier: str, state: str) -> int:
    now = time.time()
    for key, value in list(_PENDING.items()):
        if now - value["created"] > PENDING_TTL_SECONDS:
            _PENDING.pop(key, None)
    _PENDING[config_id] = {"verifier": verifier, "state": state, "created": now}
    return PENDING_TTL_SECONDS


def _take_pending(config_id: str, state: str) -> Optional[Dict[str, Any]]:
    record = _PENDING.get(config_id)
    if not record:
        return None
    if time.time() - record["created"] > PENDING_TTL_SECONDS:
        _PENDING.pop(config_id, None)
        return None
    # Constant-time: this is a secret the caller will present back to us.
    if not state or not hmac.compare_digest(record["state"], state):
        return None
    _PENDING.pop(config_id, None)
    return record


async def complete_authorization(db: AsyncSession, config_id: str, code: str, state: str) -> Dict[str, Any]:
    """Exchange the authorization code for tokens and store them encrypted."""
    import httpx

    from backend.app.integrations.x.client import TOKEN_URL, _basic_auth_header
    from backend.app.integrations.x.store import get_configuration

    row = await get_configuration(db, config_id)
    if row is None:
        raise StoreError("Configuration not found.", status=404)
    pending = _take_pending(str(row.id), state)
    if pending is None:
        raise StoreError(
            "This authorization request has expired or the `state` does not match. Start the "
            "authorization again.",
            status=400,
        )
    if not row.client_id or not row.client_secret_enc:
        raise StoreError(
            "The client secret is required to exchange the authorization code.", status=409
        )
    try:
        stored = decrypt_credentials(row)
    except DecryptionFailed as exc:
        raise StoreError(str(exc), status=409) from exc

    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            response = await http.post(
                TOKEN_URL,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": _basic_auth_header(row.client_id, stored.get("client_secret", "")),
                },
                data={
                    "grant_type": "authorization_code",
                    "code": str(code),
                    "redirect_uri": settings_redirect_uri(),
                    "code_verifier": pending["verifier"],
                    "client_id": row.client_id,
                },
            )
    except httpx.HTTPError as exc:
        raise StoreError(f"Could not reach the X token endpoint ({type(exc).__name__}).", status=502) from exc

    if response.status_code >= 400:
        await record_check(
            db, row, status=CONFIG_AUTH_REQUIRED,
            detail=f"X refused the authorization code exchange ({response.status_code}).",
        )
        raise StoreError(
            "X refused the authorization code. It is single-use and short-lived: start the "
            "authorization again and paste the new code.",
            status=400,
        )

    try:
        body = response.json()
    except Exception as exc:
        raise StoreError("The X token endpoint returned a non-JSON response.", status=502) from exc

    tokens = {
        "access_token": str(body.get("access_token", "")),
        "refresh_token": str(body.get("refresh_token", "")),
        "scope": str(body.get("scope", "")),
        "expires_at": None,
    }
    if isinstance(body.get("expires_in"), (int, float)):
        from datetime import datetime, timedelta, timezone

        tokens["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
        ).isoformat()
    if not tokens["access_token"]:
        raise StoreError("X returned no access token.", status=502)

    # ACCOUNT BINDING. The exchange succeeded, but the token now belongs to
    # WHICHEVER account X authorized - which is not necessarily the one the
    # operator intended. If a handle was declared, prove the identity matches
    # before these tokens are stored, rather than discovering the mismatch later
    # as a post published from the wrong account.
    bound = await _bind_to_expected_account(db, row, tokens["access_token"])
    if bound is not None:
        return bound

    await store_refreshed_tokens(db, row, tokens)
    await record_check(db, row, status=CONFIG_UNVERIFIED, detail="Authorized. Run Test Connection to confirm the account.")
    return {
        "ok": True,
        "has_access_token": True,
        "has_refresh_token": bool(tokens["refresh_token"]),
        "expires_at": tokens["expires_at"],
        "scopes": tokens["scope"].split() if tokens["scope"] else [],
        "note": (
            "Tokens stored encrypted. No token value is returned here or shown in the UI."
            if tokens["refresh_token"]
            else "No refresh token was issued (offline.access was not granted). The access token "
                 "will need re-authorizing when it expires."
        ),
    }


# ======================================================================
# Client construction
# ======================================================================
async def _client_for(
    db: AsyncSession, row: XApiConfiguration, *, allow_refresh: bool = False
) -> Optional[XApiClient]:
    """Build a client, refreshing the token first if it is about to expire.

    Returns ``None`` when the configuration has no access token at all: that is
    a normal, reportable state, not an error.
    """
    try:
        secrets = decrypt_credentials(row)
    except DecryptionFailed as exc:
        logger.warning("X credentials could not be opened for %s: %s", row.name, type(exc).__name__)
        raise StoreError(str(exc), status=409) from exc

    access = secrets.get("access_token", "")
    if not access:
        return None

    if allow_refresh and token_needs_refresh(row):
        client = XApiClient(
            access_token=access,
            client_id=row.client_id,
            client_secret=secrets.get("client_secret", ""),
            refresh_token=secrets.get("refresh_token", ""),
            scopes=client_scopes(row),
        )
        refreshed = await client.refresh_access_token()
        if refreshed.ok:
            await store_refreshed_tokens(db, row, refreshed.data)
            access = str(refreshed.data.get("access_token") or access)
        else:
            # A failed refresh is not fatal: the old token may still be valid.
            logger.info("X token refresh failed for %s: %s", row.name, refreshed.error_category)

    return XApiClient(
        access_token=access,
        client_id=row.client_id,
        client_secret=secrets.get("client_secret", ""),
        refresh_token=secrets.get("refresh_token", ""),
        scopes=client_scopes(row),
    )


# ======================================================================
# Routing inputs
# ======================================================================
async def duplicate_window_seconds(db: AsyncSession) -> int:
    """The configured duplicate window, in seconds.

    Read fresh on every call: the API and the worker are separate processes, and
    a value cached in one of them is a value the other ignores. An unparseable or
    negative setting falls back to the default rather than silently disabling
    the guard.
    """
    from backend.app.core.config import settings
    from backend.app.services.preferences import X_DUPLICATE_WINDOW_HOURS, get_preference

    raw = await get_preference(db, X_DUPLICATE_WINDOW_HOURS, default="")
    if raw == "":
        try:
            hours = float(getattr(settings, "X_DUPLICATE_WINDOW_HOURS", DEFAULT_DUPLICATE_WINDOW_HOURS))
        except (TypeError, ValueError):
            hours = DEFAULT_DUPLICATE_WINDOW_HOURS
    else:
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            hours = DEFAULT_DUPLICATE_WINDOW_HOURS
    hours = max(0.0, min(hours, 24 * 30))
    return int(hours * 3600)


async def effective_preference(db: AsyncSession) -> str:
    """The operator's stored routing preference, falling back to the environment.

    Read fresh on every call: the API and the worker are separate processes, and
    a value cached in one of them is a value the other ignores.
    """
    from backend.app.core.config import settings
    from backend.app.services.preferences import X_EXECUTION_PREFERENCE, get_preference

    stored = await get_preference(db, X_EXECUTION_PREFERENCE, default="")
    if stored in routing.PREFERENCES:
        return stored
    value = str(getattr(settings, "X_EXECUTION_PREFERENCE", routing.MODE_AUTO) or routing.MODE_AUTO).lower()
    return value if value in routing.PREFERENCES else routing.MODE_AUTO
async def _api_readiness(db: AsyncSession, config_id: Optional[str]) -> Dict[str, Any]:
    """Can the API half run right now, and if not, exactly why not."""
    if not x_api_enabled():
        return {
            "available": False,
            "reason": "The X API is switched off for this deployment (X_API_ENABLED=false).",
            "config": None,
        }
    row, reason = await select_configuration(db, config_id)
    if row is None:
        return {"available": False, "reason": reason, "config": None}
    if row.status == CONFIG_AUTH_REQUIRED:
        return {
            "available": False,
            "reason": (
                f"Configuration '{row.name}' was rejected by X and needs to be authorized again."
            ),
            "config": row,
        }
    if row.status == CONFIG_RATE_LIMITED:
        # Persisted from the `x-rate-limit-reset` X actually sent. The guard is
        # deliberately NOT conditional on the status still being set on every
        # 429: X omits `x-rate-limit-remaining` on some responses, and a brake
        # that silently stops braking is worse than no brake at all.
        reset_at = row.rate_limit_reset_at
        if reset_at is not None:
            if reset_at.tzinfo is None:  # SQLite hands back naive datetimes
                reset_at = reset_at.replace(tzinfo=timezone.utc)
            if reset_at > datetime.now(timezone.utc):
                return {
                    "available": False,
                    "reason": (
                        f"Configuration '{row.name}' is rate limited by X until "
                        f"{reset_at.isoformat()}. Not calling again before then."
                    ),
                    "config": row,
                }
            # The window has rolled over; let the next real call decide.
            await record_check(
                db, row, status=CONFIG_UNVERIFIED,
                detail="The previous rate-limit window has passed; not tested yet.",
            )
    return {"available": True, "reason": reason, "config": row}


async def _resolve_targets(
    db: AsyncSession,
    *,
    config_id: Optional[str],
    browser_profile_id: Optional[str],
    browser_permitted: bool = True,
) -> Dict[str, Any]:
    api = await _api_readiness(db, config_id)
    profile_id, profile_reason = select_browser_profile(browser_profile_id)
    if not browser_permitted and profile_id:
        # The operator has not granted browser interaction on this deployment.
        profile_id, profile_reason = None, (
            "Browser interaction is not permitted by the operator's current authorization "
            "policy, so browser mode is unavailable."
        )
    return {
        "api_available": bool(api["available"]),
        "api_unavailable_reason": "" if api["available"] else str(api["reason"]),
        "api_reason": str(api["reason"]),
        "config": api["config"],
        "browser_available": bool(profile_id),
        "browser_unavailable_reason": "" if profile_id else str(profile_reason),
        "browser_profile_id": profile_id or "",
    }


# ======================================================================
# Operation fingerprints (duplicate prevention)
# ======================================================================
def operation_key(operation: str, account: str, target: str) -> str:
    """A stable fingerprint for "this exact write, by this exact account".

    The post TEXT is hashed rather than stored, so the fingerprint cannot be
    reversed into the operator's words, and two identical posts an hour apart
    still collide - which is exactly the duplicate we are trying to stop.
    """
    digest = hashlib.sha256(str(target).strip().encode("utf-8")).hexdigest()[:32]
    return f"{operation}:{account}:{digest}"[:160]


def post_target(text: str, reply_to: Optional[str] = None) -> str:
    body = hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()[:32]
    return f"text:{body}" + (f"|reply:{reply_to}" if reply_to else "")


def handle_target(handle: str) -> str:
    return f"@{str(handle).lstrip('@').strip()}"


def parse_post_id(value: Any) -> str:
    """Accept a bare snowflake id or any x.com/twitter.com status URL."""
    raw = str(value or "").strip()
    if not raw:
        raise XError("A post id or URL is required.", category="INVALID_INPUT", status_code=400)
    if re.fullmatch(r"[0-9]{5,25}", raw):
        return raw
    match = re.search(r"/status(?:es)?/(\d{5,25})", raw)
    if match:
        return match.group(1)
    raise XError(
        f"'{raw[:80]}' is not a post id or an x.com status URL.", category="INVALID_INPUT", status_code=400
    )


def parse_handle(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise XError("A username is required.", category="INVALID_INPUT", status_code=400)
    handle = raw.split("/")[-1].split("?")[0].lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        raise XError(
            f"'{raw[:60]}' is not a valid X username.", category="INVALID_INPUT", status_code=400
        )
    return handle


# ======================================================================
# The unified executor
# ======================================================================
async def execute(
    db: AsyncSession,
    operation: str,
    *,
    method: str = routing.AUTO,
    config_id: Optional[str] = None,
    browser_profile_id: Optional[str] = None,
    task_text: str = "",
    task_id: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    allow_duplicate: bool = False,
    browser_permitted: bool = True,
    preference: str = "",
) -> XOutcome:
    """Run one X operation by the best available method.

    This is the only entry point the tools and the API use, so routing,
    duplicate prevention, activity recording and verification cannot diverge
    between the two.

    ``preference`` empty means "read the operator's stored preference", which is
    what every agent call does: the setting lives in the database because the
    API and the worker are separate processes and must route identically.

    ``browser_permitted`` is the caller's live view of the operator's browser
    permissions. Passing it in (rather than reading the policy here) keeps this
    module free of a registry dependency, and means revoking ``browser.interact``
    stops browser-backed X work on the next call rather than at the next review.
    """
    params = dict(params or {})
    if not preference:
        preference = await effective_preference(db)
    op: Optional[Operation] = routing.OPERATIONS.get(operation)
    if op is None:
        raise XError(
            f"'{operation}' is not an X operation this integration implements.",
            category="UNSUPPORTED_OPERATION",
            status_code=400,
        )

    targets = await _resolve_targets(
        db, config_id=config_id, browser_profile_id=browser_profile_id,
        browser_permitted=browser_permitted,
    )
    decision = routing.choose_method(
        operation,
        preference=preference,
        requested=method,
        api_available=bool(op.api and targets["api_available"]),
        api_unavailable_reason=str(targets["api_unavailable_reason"]),
        browser_available=bool(op.browser and targets["browser_available"]),
        browser_unavailable_reason=str(targets["browser_unavailable_reason"]),
        task_text=task_text,
    )
    if not decision.ok:
        return XOutcome(
            ok=False, operation=operation, method="", status=BLOCKED,
            blocked_reason=decision.blocked_reason, route=decision.as_dict(),
            notes=list(decision.notes),
        )

    target_label, fingerprint_target = _target_for(operation, params)
    account = _account_label(targets)

    if op.is_write:
        key = operation_key(operation, account, fingerprint_target)
        guard = await _guard_write(
            db, key=key, allow_duplicate=allow_duplicate, operation=operation
        )
        if guard is not None:
            return guard
    else:
        key = ""

    config = targets["config"] if decision.method == routing.API else None
    profile = targets["browser_profile_id"] if decision.method == routing.BROWSER else None

    try:
        if decision.method == routing.API:
            outcome = await _run_api(db, operation, config, params)
        else:
            outcome = await _run_browser(operation, params, profile)
    except browser_adapter.BrowserGate as exc:
        outcome = XOutcome(
            ok=False, operation=operation, method=routing.BROWSER, status=BLOCKED,
            error=str(exc), error_category="HUMAN_INTERVENTION_REQUIRED", needs_user=True,
            route=decision.as_dict(), notes=list(decision.notes),
        )
        await _record(db, operation, routing.BROWSER, BLOCKED, profile, target_label, key,
                      outcome, task_id, None)
        return outcome
    except browser_adapter.BrowserXError as exc:
        outcome = XOutcome(
            ok=False, operation=operation, method=routing.BROWSER, status=UNCERTAIN if op.is_write else FAILED,
            error=str(exc), error_category="X_BROWSER_FAILED",
            # A browser failure mid-write is exactly the ambiguous case: the click
            # may have landed before the page failed to confirm it.
            uncertain=bool(op.is_write), route=decision.as_dict(), notes=list(decision.notes),
        )
        await _record(db, operation, routing.BROWSER, outcome.status, profile, target_label, key,
                      outcome, task_id, None)
        return outcome
    except (XError, StoreError) as exc:
        category = getattr(exc, "category", "X_ERROR")
        outcome = XOutcome(
            ok=False, operation=operation, method=decision.method, status=FAILED,
            error=str(exc), error_category=category, route=decision.as_dict(),
            notes=list(decision.notes),
        )
        await _record(db, operation, decision.method, FAILED, profile, target_label, key,
                      outcome, task_id, config)
        return outcome

    outcome.route = decision.as_dict()
    if config is not None and outcome.ok:
        try:
            await touch_configuration(db, config)
        except Exception:  # bookkeeping must never fail the operation
            logger.info("Could not record last-used for X configuration %s", config.name)

    await _record(db, operation, decision.method, outcome.status, profile, target_label, key,
                  outcome, task_id, config)
    return outcome


def _account_label(targets: Dict[str, Any]) -> str:
    """Which ACCOUNT a write is attributed to, for the duplicate fingerprint.

    Deliberately method-independent and name-independent. It used to include the
    method and the configuration NAME, which meant the same post text fingerprinted
    differently through the API and through the browser - so the browser fallback
    silently defeated the duplicate guard, and renaming a configuration quietly
    reset the window. What identifies the account is who it is, not how it is
    reached.
    """
    config: Optional[XApiConfiguration] = targets.get("config")
    if config is not None and (config.x_user_id or config.x_username):
        # The id X itself resolved: stable across renames and across methods.
        return f"x:{config.x_user_id or config.x_username}"
    profile_id = str(targets.get("browser_profile_id") or "")
    if profile_id:
        return f"profile:{profile_id}"
    return "unknown-account"


def _target_for(operation: str, params: Dict[str, Any]) -> tuple[str, str]:
    """(human label, fingerprint input). The label never contains the post body."""
    if operation in ("create_post", "create_reply"):
        digest = hashlib.sha256(str(params.get("text") or "").strip().encode()).hexdigest()[:12]
        label = f"text:{digest}" + (f" reply->{params.get('reply_to')}" if params.get("reply_to") else "")
        return label, post_target(str(params.get("text") or ""), params.get("reply_to"))
    if operation in ("read_post", "like", "unlike", "verify_action"):
        value = params.get("post_id") or params.get("post") or ""
        try:
            post_id = parse_post_id(value)
        except XError:
            post_id = str(value)[:60]
        return f"post:{post_id}", post_id
    if operation in ("follow", "unfollow"):
        value = params.get("username") or params.get("user") or ""
        try:
            return handle_target(parse_handle(value)), handle_target(parse_handle(value))
        except XError:
            return str(value)[:60], str(value)
    if operation == "read_profile":
        value = params.get("username") or params.get("user") or ""
        return str(value)[:60], str(value)
    return str(params.get("target") or "")[:60], str(params.get("target") or "")


async def _guard_write(
    db: AsyncSession, *, key: str, allow_duplicate: bool, operation: str
) -> Optional[XOutcome]:
    """Refuse to send a write that has already been sent.

    Two distinct situations:

    * A previous SUCCESS with the same fingerprint: the action is DONE. Return
      that record's evidence instead of sending it again. Skipped entirely when
      the operator has set the duplicate window to 0, which is the only sanctioned
      way to publish the same text twice.
    * A previous UNCERTAIN with the same fingerprint: we do not know. Do NOT
      send. Ask for verification; a retry here is how duplicate posts happen.
      This branch is NOT affected by the window - an ambiguous write is never
      safe to repeat, however long ago it was.
    """
    if allow_duplicate or not key:
        return None
    pending = await find_uncertain(db, key)
    if pending is not None:
        return XOutcome(
            ok=False,
            operation=operation,
            method=pending.method,
            status=UNCERTAIN,
            uncertain=True,
            blocked_reason=(
                f"An earlier attempt at this exact {operation} returned no answer, so it may or "
                f"may not have been applied (activity {pending.id}). It was NOT repeated. Run "
                "x_verify_action to check the current state first."
            ),
            data={"activity_id": str(pending.id)},
            route={"method": pending.method, "duplicate_guard": "uncertain_previous"},
        )
    window = await duplicate_window_seconds(db)
    if window <= 0:
        return None
    previous = await find_recent(db, key, within_seconds=window)
    if previous is not None:
        return XOutcome(
            ok=True,
            operation=operation,
            method=previous.method,
            status=SKIPPED_DUPLICATE,
            data={
                "already_completed": True,
                "original_activity_id": str(previous.id),
                "original_post_id": str((previous.result_summary or {}).get("post_id") or ""),
                "completed_at": previous.created_at.isoformat() if previous.created_at else "",
                "duplicate_window_hours": window / 3600,
                "note": (
                    f"This exact {operation} already succeeded through {previous.method} at "
                    f"{previous.created_at.isoformat() if previous.created_at else 'an earlier time'}. "
                    "Nothing was sent again. Publishing it again on purpose is possible by setting "
                    "x.duplicate_window_hours to 0."
                ),
            },
            route={"method": previous.method, "duplicate_guard": "already_completed"},
        )
    return None


async def _run_api(
    db: AsyncSession,
    operation: str,
    config: Optional[XApiConfiguration],
    params: Dict[str, Any],
) -> XOutcome:
    if config is None:  # pragma: no cover - routing only picks api when a config exists
        return XOutcome(
            ok=False, operation=operation, method=routing.API, status=FAILED,
            error="No X API configuration was selected.", error_category="X_NO_CONFIGURATION",
        )
    try:
        client = await _client_for(db, config, allow_refresh=True)
    except StoreError as exc:
        return XOutcome(
            ok=False, operation=operation, method=routing.API, status=FAILED,
            error=str(exc), error_category="X_CREDENTIAL_UNREADABLE",
        )
    if client is None:
        return XOutcome(
            ok=False, operation=operation, method=routing.API, status=FAILED,
            error="This configuration has no authorized X account.", error_category="X_AUTH_FAILED",
        )

    result = await _dispatch_api(client, operation, params, own_user_id=str(config.x_user_id or ""))
    outcome = _outcome_from_result(operation, routing.API, result)
    if outcome.ok and str(config.x_user_id or "") == "" and outcome.data.get("acting_account"):
        # A successful call resolved the acting account. Persist it so the next
        # write does not need a second /2/users/me round trip.
        try:
            username = str(outcome.data["acting_account"]).lstrip("@")
            await record_check(
                db, config, status=config.status or CONFIG_UNVERIFIED,
                detail=f"Last used as @{username}.", username=username,
            )
        except Exception:
            logger.info("Could not record the acting account for X configuration %s", config.name)
    if result.error_category == "X_RATE_LIMITED":
        # Recorded regardless of whether X included a remaining-count header: a
        # 429 is a 429, and the reset time is what stops the NEXT task from
        # walking straight back into it. (The old code required
        # `rate_limit_remaining is not None`, which X omits on some 429s, so the
        # brake never engaged.)
        reset_epoch = result.rate_limit_reset_epoch
        if not reset_epoch and result.retry_after_seconds:
            reset_epoch = int(time.time()) + int(result.retry_after_seconds)
        await record_check(
            db, config, status=CONFIG_RATE_LIMITED,
            detail=f"Rate limited by X: {result.error}"[:400],
            rate_limit_remaining=result.rate_limit_remaining,
            rate_limit_reset_epoch=reset_epoch,
        )
    elif not result.ok and result.error_category in ("X_AUTH_FAILED", "X_PERMISSION_DENIED"):
        await record_check(db, config, status=_config_status_for(result), detail=result.error[:400])
    return outcome


async def _dispatch_api(
    client: XApiClient, operation: str, params: Dict[str, Any], own_user_id: str = ""
) -> XApiResult:
    """Map an operation to its endpoint, validating arguments first."""
    allowed, reason = client.can(operation)
    if not allowed:
        return XApiResult(ok=False, operation=operation, error=reason, error_category="X_AUTH_FAILED")

    if operation == "read_profile":
        return await client.read_user_by_username(parse_handle(params.get("username")))
    if operation == "read_post":
        return await client.read_post(parse_post_id(params.get("post_id")))

    if operation in ("create_post", "create_reply"):
        text = str(params.get("text") or "").strip()
        if not text:
            return XApiResult(
                ok=False, operation=operation, error="The post text is empty.",
                error_category="X_INVALID_REQUEST",
            )
        if len(text) > 25_000:
            return XApiResult(
                ok=False, operation=operation,
                error="The post text is longer than X allows (25,000 characters).",
                error_category="X_INVALID_REQUEST",
            )
        reply_to = params.get("reply_to") if operation == "create_reply" else None
        if operation == "create_reply" and not reply_to:
            return XApiResult(
                ok=False, operation=operation,
                error="A reply needs the post it replies to.", error_category="X_INVALID_REQUEST",
            )
        if reply_to:
            reply_to = parse_post_id(reply_to)
        return await client.create_post(text, reply_to=reply_to)

    # Every remaining endpoint is addressed by the AUTHENTICATED user's id, which
    # X requires in the path. It is known from a previous successful test; if it
    # is not, say so rather than guessing an id.
    if not own_user_id:
        return await _resolve_own_id(client, operation, params)

    if operation in ("like", "unlike"):
        post_id = parse_post_id(params.get("post_id"))
        return (
            await client.like_post(own_user_id, post_id)
            if operation == "like"
            else await client.unlike_post(own_user_id, post_id)
        )
    if operation in ("follow", "unfollow"):
        handle = parse_handle(params.get("username"))
        lookup = await client.read_user_by_username(handle)
        if not lookup.ok:
            return lookup
        target = str(((lookup.data or {}).get("user") or {}).get("id") or "")
        if not target:
            return XApiResult(
                ok=False, operation=operation, error=f"@{handle} resolved to no id.",
                error_category="X_NOT_FOUND",
            )
        return (
            await client.follow_user(own_user_id, target)
            if operation == "follow"
            else await client.unfollow_user(own_user_id, target)
        )
    if operation == "verify_action":
        return await _verify_api(client, params, own_user_id)

    return XApiResult(
        ok=False, operation=operation,
        error=f"'{operation}' has no API implementation.", error_category="X_OPERATION_UNSUPPORTED",
    )


async def _resolve_own_id(
    client: XApiClient, operation: str, params: Dict[str, Any]
) -> XApiResult:
    """Resolve the authenticated account's id with one ``/2/users/me`` call.

    Needed because X addresses writes by the acting user's id. The result is not
    persisted here: the service stores it on the configuration after a Test
    Connection, so this is the fallback for a configuration that has not been
    tested since it was created.
    """
    me = await client.whoami()
    if not me.ok:
        return me
    user = (me.data or {}).get("user") or {}
    me_id = str(user.get("id") or "")
    username = str(user.get("username") or "")
    if not me_id:
        return XApiResult(
            ok=False, operation=operation,
            error="X did not return this account's id.", error_category="X_IDENTITY_UNKNOWN",
        )
    # Now that the id is known, perform the operation the caller actually asked
    # for. One extra round trip is the honest price for not persisting an id we
    # were not given.
    resolved = await _dispatch_api(client, operation, params, own_user_id=me_id)
    if resolved.ok and username:
        resolved.data = {**(resolved.data or {}), "acting_account": f"@{username}"}
    return resolved


async def _verify_api(
    client: XApiClient, params: Dict[str, Any], own_user_id: str = ""
) -> XApiResult:
    """Check whether a previously-uncertain write actually landed."""
    what = str(params.get("check") or "").strip().lower()
    if what not in ("post", "like", "follow"):
        return XApiResult(
            ok=False, operation="verify_action",
            error="Say what to check: 'post', 'like' or 'follow'.",
            error_category="X_INVALID_REQUEST",
        )
    me_id = own_user_id
    if not me_id:
        me = await client.whoami()
        if not me.ok:
            return me
        me_id = str(((me.data or {}).get("user") or {}).get("id") or "")
    if not me_id:
        return XApiResult(
            ok=False, operation="verify_action", error="Could not resolve this account's X id.",
            error_category="X_IDENTITY_UNKNOWN",
        )
    if what == "post":
        post_id = parse_post_id(params.get("post_id"))
        found = await client.read_post(post_id)
        if found.ok:
            return XApiResult(
                ok=True, operation="verify_action",
                data={"check": "post", "post_id": post_id, "exists": True, "post": found.data},
            )
        if found.error_category == "X_NOT_FOUND":
            return XApiResult(
                ok=True, operation="verify_action",
                data={"check": "post", "post_id": post_id, "exists": False},
            )
        return found
    if what == "like":
        return await client.liked_posts(me_id, parse_post_id(params.get("post_id")))
    lookup = await client.read_user_by_username(parse_handle(params.get("username")))
    if not lookup.ok:
        return lookup
    target = str(((lookup.data or {}).get("user") or {}).get("id") or "")
    return await client.is_following(me_id, target)


def _outcome_from_result(operation: str, method: str, result: XApiResult) -> XOutcome:
    status = OK if result.ok else (UNCERTAIN if result.uncertain else FAILED)
    return XOutcome(
        ok=result.ok,
        operation=operation,
        method=method,
        status=status,
        data=dict(result.data or {}),
        error="" if result.ok else result.error,
        error_category="" if result.ok else result.error_category,
        uncertain=result.uncertain,
        notes=(
            [f"X rate limit: {result.rate_limit_remaining} requests left in this window."]
            if result.ok and result.rate_limit_remaining is not None
            else []
        )
        + (
            [f"Wait about {result.retry_after_seconds}s before the next X call."]
            if result.error_category == "X_RATE_LIMITED" and result.retry_after_seconds
            else []
        ),
    )


async def _run_browser(
    operation: str,
    params: Dict[str, Any],
    profile_id: Optional[str],
) -> XOutcome:
    if not profile_id:
        return XOutcome(
            ok=False, operation=operation, method=routing.BROWSER, status=FAILED,
            error="No browser profile is selected for X work.", error_category="X_NO_PROFILE",
        )
    try:
        if operation == "read_profile":
            data = await browser_adapter.read_profile(profile_id, parse_handle(params.get("username")))
        elif operation == "read_post":
            data = await browser_adapter.read_post(profile_id, parse_post_id(params.get("post_id")))
        elif operation == "create_post":
            data = await browser_adapter.create_post(profile_id, str(params.get("text") or ""))
        elif operation == "create_reply":
            data = await browser_adapter.create_post(
                profile_id, str(params.get("text") or ""),
                reply_to=parse_post_id(params.get("reply_to")),
            )
        elif operation == "like":
            data = await browser_adapter.like_post(profile_id, parse_post_id(params.get("post_id")))
        elif operation == "unlike":
            data = await browser_adapter.unlike_post(profile_id, parse_post_id(params.get("post_id")))
        elif operation == "follow":
            data = await browser_adapter.follow_user(profile_id, parse_handle(params.get("username")))
        elif operation == "unfollow":
            data = await browser_adapter.unfollow_user(profile_id, parse_handle(params.get("username")))
        elif operation == "verify_action":
            data = await _verify_browser(profile_id, params)
        else:
            return XOutcome(
                ok=False, operation=operation, method=routing.BROWSER, status=FAILED,
                error=f"'{operation}' has no browser implementation.",
                error_category="X_OPERATION_UNSUPPORTED",
            )
    except XError:
        raise
    except Exception as exc:  # argument problems surface as clean errors, not 500s
        raise XError(str(exc), category="INVALID_INPUT", status_code=400) from exc

    return XOutcome(
        ok=True, operation=operation, method=routing.BROWSER, status=OK, data=data,
        notes=[
            "Done through the signed-in browser profile. The page's own state is the evidence; "
            "there is no API record to cross-check."
        ],
    )


async def _verify_browser(profile_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Browser verification: read the page and report what it shows now."""
    what = str(params.get("check") or "").strip().lower()
    if what == "post":
        data = await browser_adapter.read_post(profile_id, parse_post_id(params.get("post_id")))
        return {"check": "post", "exists": True, "url": data.get("url"), "title": data.get("page_title")}
    if what == "like":
        page_state = await browser_adapter.like_state_for(profile_id, parse_post_id(params.get("post_id")))
        return {"check": "like", "liked": bool(page_state.get("liked")), "state": page_state}
    if what == "follow":
        page_state = await browser_adapter.follow_state_for(profile_id, parse_handle(params.get("username")))
        return {"check": "follow", "following": bool(page_state.get("following")), "state": page_state}
    raise XError("Say what to check: 'post', 'like' or 'follow'.", category="INVALID_INPUT", status_code=400)


async def _record(
    db: AsyncSession,
    operation: str,
    method: str,
    status: str,
    profile: Optional[str],
    target_label: str,
    key: str,
    outcome: XOutcome,
    task_id: Optional[str],
    config: Optional[XApiConfiguration] = None,
) -> None:
    """Append the activity row. A failure to record must not change the result."""
    try:
        await record_activity(
            db,
            method=method,
            operation=operation,
            status=status,
            target=target_label,
            operation_key=key,
            config=config,
            browser_profile_id=profile,
            result_summary={
                k: v for k, v in (outcome.data or {}).items()
                if k in ("post_id", "user", "exists", "liked", "following", "already_completed", "url")
            },
            error=outcome.error,
            error_category=outcome.error_category,
            outcome_uncertain=outcome.uncertain,
            task_id=task_id,
        )
    except Exception as exc:
        logger.warning("Could not record X activity: %s", type(exc).__name__)


# ======================================================================
# Uncertain-outcome resolution
# ======================================================================
async def resolve_activity(db: AsyncSession, activity_id: str) -> Dict[str, Any]:
    """Close an UNCERTAIN record once the real outcome is known."""
    row = await resolve_uncertain(db, activity_id, status=OK, summary={"verified": True})
    if row is None:
        raise StoreError("Activity record not found.", status=404)
    return {"id": str(row.id), "status": row.status, "outcome_uncertain": row.outcome_uncertain}