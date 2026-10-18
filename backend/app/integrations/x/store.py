"""Persistence for X API configurations and X activity history.

Everything here goes through the existing async session factory and models. No
second database, no file store, no separate task engine.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.integrations.x.crypto import CredentialVault, DecryptionFailed, vault_status
from backend.app.models import XActivityRecord, XApiConfiguration
from backend.app.models.x_integration import (
    CONFIG_ERROR,
    CONFIG_STATUSES,
    CONFIG_UNVERIFIED,
)

logger = logging.getLogger(__name__)

# Refresh this far ahead of expiry so a token never dies mid-request.
REFRESH_MARGIN = timedelta(seconds=90)


class StoreError(Exception):
    """A configuration operation that the operator must fix."""

    def __init__(self, message: str, *, status: int = 400, field: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.field = field


# ======================================================================
# Configurations
# ======================================================================
async def list_configurations(db: AsyncSession) -> List[XApiConfiguration]:
    result = await db.execute(
        select(XApiConfiguration).order_by(XApiConfiguration.name.asc())
    )
    return list(result.scalars().all())


async def get_configuration(db: AsyncSession, config_id: str) -> Optional[XApiConfiguration]:
    if not config_id:
        return None
    return await db.get(XApiConfiguration, _as_uuid(config_id))


async def get_by_name(db: AsyncSession, name: str) -> Optional[XApiConfiguration]:
    result = await db.execute(
        select(XApiConfiguration).where(func.lower(XApiConfiguration.name) == str(name).strip().lower())
    )
    return result.scalars().first()


def _as_uuid(value: str) -> Any:
    """Coerce a route parameter to a UUID, or fail with a clean error."""
    import uuid

    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise StoreError("Not a valid configuration id.", status=400) from exc


async def create_configuration(
    db: AsyncSession, payload: Dict[str, Any], *, actor: str = "operator"
) -> XApiConfiguration:
    name = str(payload.get("name") or "").strip()
    if await get_by_name(db, name) is not None:
        raise StoreError(f"A configuration named '{name}' already exists.", field="name")
    if not payload.get("access_token") and not payload.get("client_secret"):
        raise StoreError(
            "Provide either a client secret or an access token; a configuration with neither "
            "can never be used.",
            field="client_secret",
        )

    row = XApiConfiguration(
        name=name,
        description=str(payload.get("description") or "")[:400],
        client_id=str(payload.get("client_id") or "")[:255],
        is_enabled=bool(payload.get("enabled", True)),
        is_default=bool(payload.get("default", False)),
        status=CONFIG_UNVERIFIED,
        status_detail="Not tested yet. Use Test Connection before relying on it.",
        created_by=str(actor or "operator")[:128],
    )
    db.add(row)
    await db.flush()
    # The vault is bound to the row id, so it can only be built after the flush.
    vault = CredentialVault(str(row.id))
    row.client_secret_enc = vault.seal({"client_secret": payload.get("client_secret")}) or None
    row.access_token_enc = vault.seal({"access_token": payload.get("access_token")}) or None
    row.refresh_token_enc = vault.seal({"refresh_token": payload.get("refresh_token")}) or None

    username = str(payload.get("username") or "").lstrip("@").strip()
    if username:
        row.x_username = username[:64]

    if row.is_default:
        await _clear_other_defaults(db, str(row.id))
    await db.commit()
    await db.refresh(row)
    return row


async def update_configuration(
    db: AsyncSession,
    config_id: str,
    changes: Dict[str, Any],
) -> XApiConfiguration:
    row = await get_configuration(db, config_id)
    if row is None:
        raise StoreError("Configuration not found.", status=404)

    if "name" in changes and changes["name"]:
        new_name = str(changes["name"]).strip()
        other = await get_by_name(db, new_name)
        if other is not None and str(other.id) != str(row.id):
            raise StoreError(f"A configuration named '{new_name}' already exists.", field="name")
        row.name = new_name

    if "description" in changes and changes["description"] is not None:
        row.description = str(changes["description"])[:400]
    if "enabled" in changes and changes["enabled"] is not None:
        row.is_enabled = bool(changes["enabled"])
    if "default" in changes and changes["default"] is not None:
        row.is_default = bool(changes["default"])
        if row.is_default:
            await _clear_other_defaults(db, str(row.id))
    if "username" in changes and changes["username"] is not None:
        handle = str(changes["username"] or "").lstrip("@").strip()
        row.x_username = handle[:64] or None

    # A secret is only replaced when a NEW non-empty value is supplied. An empty
    # string in an edit form means "leave what is stored alone", not "erase it":
    # silently wiping a credential because a masked field was submitted empty is
    # a data-loss bug that looks like a successful save.
    secret_changes = {
        key: str(changes[key])
        for key in ("client_secret", "access_token", "refresh_token")
        if str(changes.get(key) or "").strip()
    }
    if secret_changes:
        vault = CredentialVault(str(row.id))
        current = vault.open(row.client_secret_enc) if row.client_secret_enc else {}
        current.update(vault.open(row.access_token_enc) if row.access_token_enc else {})
        current.update(vault.open(row.refresh_token_enc) if row.refresh_token_enc else {})
        current.update(secret_changes)
        row.client_secret_enc = vault.seal({"client_secret": current.get("client_secret")}) or None
        row.access_token_enc = vault.seal({"access_token": current.get("access_token")}) or None
        row.refresh_token_enc = vault.seal({"refresh_token": current.get("refresh_token")}) or None
        # New credentials invalidate what we knew about this account.
        row.status = CONFIG_UNVERIFIED
        row.status_detail = "Credentials changed; not tested yet."

    if "client_id" in changes and changes["client_id"]:
        row.client_id = str(changes["client_id"])[:255]
        row.status = CONFIG_UNVERIFIED
        row.status_detail = "App credentials changed; not tested yet."

    await db.commit()
    await db.refresh(row)
    return row


async def _clear_other_defaults(db: AsyncSession, keep_id: str) -> None:
    result = await db.execute(select(XApiConfiguration).where(XApiConfiguration.is_default.is_(True)))
    for other in result.scalars().all():
        if str(other.id) != str(keep_id):
            other.is_default = False


async def set_default_configuration(db: AsyncSession, config_id: Optional[str]) -> None:
    if config_id:
        row = await get_configuration(db, config_id)
        if row is None:
            raise StoreError("Configuration not found.", status=404)
        await _clear_other_defaults(db, str(row.id))
        row.is_default = True
    else:
        result = await db.execute(
            select(XApiConfiguration).where(XApiConfiguration.is_default.is_(True))
        )
        for row in result.scalars().all():
            row.is_default = False
    await db.commit()


async def delete_configuration(db: AsyncSession, config_id: str) -> None:
    row = await get_configuration(db, config_id)
    if row is None:
        raise StoreError("Configuration not found.", status=404)
    await db.delete(row)
    await db.commit()


async def record_check(
    db: AsyncSession,
    row: XApiConfiguration,
    *,
    status: str,
    detail: str = "",
    rate_limit_remaining: Optional[int] = None,
    rate_limit_reset_epoch: Optional[int] = None,
    user_id: Optional[str] = None,
    username: Optional[str] = None,
    scopes: Optional[Sequence[str]] = None,
) -> None:
    """Persist the outcome of a connection test or a call.

    ``detail`` is written by our own code from a fixed vocabulary of categories.
    It is truncated, and it never carries a response body or a header.

    ``merge`` first, deliberately: the caller may hold a row loaded on a DIFFERENT
    session (the tools run in their own short-lived session, and the row was
    selected on another). Mutating a detached instance and committing is a silent
    no-op in SQLAlchemy 2.0, so without this the recorded status - including a
    rate-limit that is meant to stop the next call - would never reach the row.
    """
    row = await db.merge(row)
    if status not in CONFIG_STATUSES:
        status = CONFIG_ERROR
    row.status = status
    row.status_detail = (detail or "")[:500] or None
    row.last_checked_at = datetime.now(timezone.utc)
    if rate_limit_remaining is not None:
        row.rate_limit_remaining = int(rate_limit_remaining)
    if rate_limit_reset_epoch:
        row.rate_limit_reset_at = datetime.fromtimestamp(rate_limit_reset_epoch, tz=timezone.utc)
    if user_id:
        row.x_user_id = str(user_id)[:32]
    if username:
        row.x_username = str(username).lstrip("@")[:64]
    if scopes is not None:
        row.scopes = sorted({str(s) for s in scopes if str(s).strip()})
    await db.commit()


async def touch_configuration(db: AsyncSession, row: XApiConfiguration) -> None:
    # Merge, for the same reason as `record_check`: a detached row's mutation
    # would otherwise be discarded silently at commit.
    row = await db.merge(row)
    row.last_used_at = datetime.now(timezone.utc)
    await db.commit()


# ======================================================================
# Selection
# ======================================================================
async def select_configuration(
    db: AsyncSession, config_id: Optional[str] = None
) -> Tuple[Optional[XApiConfiguration], str]:
    """Pick the configuration for one operation.

    Returns ``(row, reason)``. ``row`` is ``None`` when nothing eligible exists;
    ``reason`` then says exactly why, because "no configuration" is not an
    actionable message for an operator.
    """
    if config_id:
        row = await get_configuration(db, config_id)
        if row is None:
            return None, f"No X API configuration with id '{config_id}'."
        if not row.is_enabled:
            return None, f"Configuration '{row.name}' is disabled."
        if not row.access_token_enc:
            return None, (
                f"Configuration '{row.name}' has app credentials but no authorized account. "
                "Complete the OAuth step, or paste an access token."
            )
        return row, f"Explicitly selected configuration '{row.name}'."

    result = await db.execute(
        select(XApiConfiguration)
        .where(XApiConfiguration.is_enabled.is_(True))
        .order_by(
            XApiConfiguration.is_default.desc(),
            XApiConfiguration.last_used_at.asc().nullsfirst(),
            XApiConfiguration.name.asc(),
        )
    )
    candidates = [r for r in result.scalars().all() if r.access_token_enc]
    if not candidates:
        enabled_without_token = await db.execute(
            select(XApiConfiguration).where(XApiConfiguration.is_enabled.is_(True))
        )
        if list(enabled_without_token.scalars().all()):
            return None, (
                "No configuration has an authorized X account yet. Add an access token or run "
                "the OAuth authorization on the X.com page."
            )
        return None, "No X API configuration is enabled. Add one on the X.com page."
    chosen = candidates[0]
    how = "the default configuration" if chosen.is_default else "the first authorized configuration"
    return chosen, f"Selected {how}: '{chosen.name}'."


def select_browser_profile(
    explicit: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """Pick the Chrome profile for browser mode.

    An explicit id always wins and is never silently swapped for another
    profile. With no id, the default agent profile is used, because that is the
    profile the operator already logged into.
    """
    from backend.app.browser import profiles as profile_store

    if explicit:
        if not profile_store.valid_profile_id(explicit):
            return None, f"'{explicit}' is not a valid browser profile id."
        if profile_store.get_profile(explicit) is None:
            return None, f"No browser profile with id '{explicit}'."
        return explicit, f"Using the requested browser profile '{explicit}'."

    from backend.app.core.config import settings

    pinned = str(getattr(settings, "X_BROWSER_PROFILE_ID", "") or "").strip()
    if pinned:
        # An operator may keep a dedicated, separately signed-in profile for X
        # instead of using the general default agent profile.
        if profile_store.valid_profile_id(pinned) and profile_store.get_profile(pinned) is not None:
            return pinned, f"Using the X browser profile pinned in settings ({pinned})."
        return None, (
            f"X_BROWSER_PROFILE_ID is set to '{pinned}', which is not an existing browser "
            "profile. Fix it on the X.com page or in the environment."
        )

    default = profile_store.get_default_agent_profile()
    if default is None:
        return None, (
            "No default agent browser profile is selected. Choose one on the X.com page "
            "(or Browser Profiles) so the agent can reuse your existing X login."
        )
    return default.id, f"Using the default agent browser profile '{default.id}' ({default.name})."


# ======================================================================
# Credentials
# ======================================================================
def decrypt_credentials(row: XApiConfiguration) -> Dict[str, str]:
    """Open the stored bundle. Raises :class:`DecryptionFailed` if the key changed."""
    vault = CredentialVault(str(row.id))
    values = vault.open(row.client_secret_enc)
    values.update(vault.open(row.access_token_enc))
    values.update(vault.open(row.refresh_token_enc))
    return values


def token_needs_refresh(row: XApiConfiguration) -> bool:
    if not row.token_expires_at:
        return False
    expires = row.token_expires_at
    if expires.tzinfo is None:  # SQLite hands back naive datetimes
        expires = expires.replace(tzinfo=timezone.utc)
    return expires - REFRESH_MARGIN <= datetime.now(timezone.utc)


async def store_refreshed_tokens(
    db: AsyncSession, row: XApiConfiguration, tokens: Dict[str, Any]
) -> None:
    """Persist a refreshed token through the same encryption path as the import."""
    vault = CredentialVault(str(row.id))
    existing = decrypt_credentials(row)
    access = str(tokens.get("access_token") or "")
    refresh = str(tokens.get("refresh_token") or existing.get("refresh_token") or "")
    if access:
        row.access_token_enc = vault.seal({"access_token": access})
    if refresh:
        row.refresh_token_enc = vault.seal({"refresh_token": refresh})
    expires_at = tokens.get("expires_at")
    if expires_at:
        try:
            row.token_expires_at = datetime.fromisoformat(str(expires_at))
        except ValueError:
            row.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
    scope = str(tokens.get("scope") or "")
    if scope:
        row.scopes = sorted({s for s in scope.split() if s})
    await db.commit()


# ======================================================================
# Public projection
# ======================================================================
def public_configuration(row: XApiConfiguration) -> Dict[str, Any]:
    """Exactly what the browser is allowed to see.

    No secret, and no length-prefixed hint of one that would help an attacker:
    `client_secret_mask` is a fixed bullet mask, never a prefix of the value.
    """
    vault_ready = vault_status()["ready"]
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "client_id_preview": _preview(row.client_id),
        "has_client_secret": bool(row.client_secret_enc),
        "has_access_token": bool(row.access_token_enc),
        "has_refresh_token": bool(row.refresh_token_enc),
        "client_secret_mask": "••••" if row.client_secret_enc else "",
        "access_token_mask": "••••" if row.access_token_enc else "",
        "scopes": list(row.scopes or []),
        "x_user_id": row.x_user_id or "",
        "x_username": row.x_username or "",
        "is_enabled": bool(row.is_enabled),
        "is_default": bool(row.is_default),
        "status": row.status,
        "status_detail": row.status_detail or "",
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "token_expires_at": row.token_expires_at.isoformat() if row.token_expires_at else None,
        "rate_limit_remaining": row.rate_limit_remaining,
        "rate_limit_reset_at": row.rate_limit_reset_at.isoformat() if row.rate_limit_reset_at else None,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "vault_ready": vault_ready,
        "can_write": bool(row.access_token_enc) and bool(row.is_enabled),
    }


def _preview(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    return f"{text[:5]}…{text[-2:]}" if len(text) > 8 else "••"


def public_activity(row: XActivityRecord) -> Dict[str, Any]:
    return {
        "id": str(row.id),
        "config_id": row.config_id or "",
        "config_name": row.config_name,
        "browser_profile_id": row.browser_profile_id or "",
        "method": row.method,
        "operation": row.operation,
        "target": row.target,
        "status": row.status,
        "operation_key": row.operation_key,
        "outcome_uncertain": bool(row.outcome_uncertain),
        "result_summary": row.result_summary or {},
        "error": row.error or "",
        "error_category": row.error_category or "",
        "duration_ms": row.duration_ms,
        "task_id": row.task_id or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ======================================================================
# Activity
# ======================================================================
async def record_activity(
    db: AsyncSession,
    *,
    method: str,
    operation: str,
    status: str,
    target: str = "",
    operation_key: str = "",
    config: Optional[XApiConfiguration] = None,
    browser_profile_id: Optional[str] = None,
    result_summary: Optional[Dict[str, Any]] = None,
    error: str = "",
    error_category: str = "",
    outcome_uncertain: bool = False,
    duration_ms: Optional[int] = None,
    task_id: Optional[str] = None,
) -> XActivityRecord:
    """Append one activity row.

    Every field is passed through :func:`crypto.redact` first, and the whole
    structure is then checked by :func:`crypto.assert_no_secrets`. A caller that
    accidentally puts a token in a summary, an error string or a target must not
    be able to store it — the activity table outlives the credential that produced
    it, so a leak here would outlive the key rotation too.
    """
    from backend.app.integrations.x.crypto import assert_no_secrets, redact, scrub_text

    safe_summary = redact(result_summary or {})
    safe_error = scrub_text(error)
    safe_target = scrub_text(target, limit=512)

    row = XActivityRecord(
        config_id=str(config.id) if config is not None else None,
        config_name=config.name if config is not None else "",
        browser_profile_id=browser_profile_id,
        method=method,
        operation=operation,
        target=safe_target[:512],
        status=status,
        operation_key=str(operation_key)[:160],
        outcome_uncertain=bool(outcome_uncertain),
        result_summary=safe_summary,
        error=safe_error[:600] or None,
        error_category=str(error_category)[:64] or None,
        duration_ms=duration_ms,
        task_id=str(task_id)[:64] if task_id else None,
    )
    try:
        assert_no_secrets(
            {"result_summary": safe_summary, "target": safe_target, "error": safe_error},
            where="x_activity",
        )
    except ValueError as exc:
        # Fail closed: the row is not written at all rather than written with
        # something that should not have been there.
        logger.error("Refusing to record X activity that would store a secret: %s", exc)
        raise StoreError(
            "The activity detail contained something that looks like a credential and was "
            "not recorded. The operation itself is unaffected; please report this.",
            status=500,
        ) from exc

    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_activity(
    db: AsyncSession,
    *,
    limit: int = 50,
    method: str = "",
    status: str = "",
    config_id: str = "",
) -> List[XActivityRecord]:
    stmt = select(XActivityRecord)
    if method:
        stmt = stmt.where(XActivityRecord.method == method)
    if status:
        stmt = stmt.where(XActivityRecord.status == status)
    if config_id:
        stmt = stmt.where(XActivityRecord.config_id == config_id)
    stmt = stmt.order_by(XActivityRecord.created_at.desc()).limit(max(1, min(int(limit or 50), 500)))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def find_recent(
    db: AsyncSession, operation_key: str, *, within_seconds: int = 3600
) -> Optional[XActivityRecord]:
    """The most recent activity for one operation fingerprint.

    This is the duplicate guard. It looks at SUCCESS rows only: an UNCERTAIN row
    means "we do not know", which is handled by verification, not by re-sending.
    """
    if not operation_key:
        return None
    since = datetime.now(timezone.utc) - timedelta(seconds=max(1, int(within_seconds)))
    result = await db.execute(
        select(XActivityRecord)
        .where(
            XActivityRecord.operation_key == operation_key,
            XActivityRecord.status == "SUCCESS",
            XActivityRecord.created_at >= since,
        )
        .order_by(XActivityRecord.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def find_uncertain(
    db: AsyncSession, operation_key: str, *, within_seconds: int = 86_400
) -> Optional[XActivityRecord]:
    """An UNCERTAIN attempt for the same fingerprint, if one is outstanding."""
    if not operation_key:
        return None
    since = datetime.now(timezone.utc) - timedelta(seconds=max(1, int(within_seconds)))
    result = await db.execute(
        select(XActivityRecord)
        .where(
            XActivityRecord.operation_key == operation_key,
            XActivityRecord.outcome_uncertain.is_(True),
            XActivityRecord.created_at >= since,
        )
        .order_by(XActivityRecord.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def resolve_uncertain(
    db: AsyncSession, record_id: str, *, status: str, summary: Optional[Dict[str, Any]] = None
) -> Optional[XActivityRecord]:
    """Close out an UNCERTAIN record once its real outcome is known."""
    import uuid

    try:
        row = await db.get(XActivityRecord, uuid.UUID(str(record_id)))
    except (ValueError, TypeError):
        return None
    if row is None:
        return None
    row.outcome_uncertain = False
    row.status = status
    if summary:
        from backend.app.integrations.x.crypto import redact

        row.result_summary = redact({**(row.result_summary or {}), **summary})
    await db.commit()
    await db.refresh(row)
    return row