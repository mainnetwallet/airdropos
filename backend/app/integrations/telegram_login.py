"""Add a Telegram account from the UI: phone -> login code -> (optional) 2FA password.

Secret handling, enforced here:
  * The phone number, login code and 2FA password exist only in memory for the length of one
    request. They are never written to disk, logged, audited, stored in the registry, or
    passed to the LLM. Only the resulting Telethon session file and the profile name are kept.
  * A pending login lives at most PENDING_TTL_SECONDS and is dropped after a successful
    login, a cancel, a fatal error, or too many wrong attempts.
  * Errors are mapped to short fixed messages; raw Telethon text (which can echo the phone
    number) is never returned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from backend.app.core.config import settings
from backend.app.integrations import telegram_accounts as registry

logger = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 600
MAX_PENDING = 3
MAX_ATTEMPTS = 5

try:  # Telethon is optional, exactly like the tools.
    from telethon import TelegramClient  # type: ignore

    TELETHON_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    TelegramClient = None  # type: ignore
    TELETHON_AVAILABLE = False


class LoginError(Exception):
    """A login step failed. ``status`` is the HTTP status the API should answer with."""

    def __init__(self, message: str, status: int = 400, *, field: Optional[str] = None) -> None:
        super().__init__(message)
        self.message, self.status, self.field = message, status, field


@dataclass
class _Pending:
    client: Any
    label: str
    session_path: Path
    phone: str = field(repr=False)
    phone_code_hash: str = field(repr=False)
    make_default: bool = False
    stage: str = "code"
    attempts: int = 0
    expires_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_pending: Dict[str, _Pending] = {}


def _check_ready() -> None:
    if not TELETHON_AVAILABLE:
        raise LoginError("Telethon is not installed on the server (pip install telethon).", 409)
    if not (settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH):
        raise LoginError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first, then restart.", 409)


async def _drop(login_id: str, *, delete_session: bool) -> None:
    item = _pending.pop(login_id, None)
    if item is None:
        return
    try:
        await item.client.disconnect()
    except Exception:
        pass
    if delete_session:
        _delete_session_files(item.session_path)


def _delete_session_files(session_path: Path) -> None:
    for suffix in (".session", ".session-journal"):
        try:
            Path(f"{session_path}{suffix}").unlink()
        except OSError:
            pass


async def _purge_expired() -> None:
    now = time.time()
    for login_id, item in list(_pending.items()):
        if item.expires_at <= now:
            await _drop(login_id, delete_session=True)


def _map_error(exc: BaseException) -> LoginError:
    """Fixed messages only: Telethon's own text can contain the phone number."""
    name = type(exc).__name__
    table = {
        "PhoneNumberInvalidError": ("That phone number is not valid. Use the international format, e.g. +8801XXXXXXXXX.", 400, "phone"),
        "PhoneNumberBannedError": ("Telegram has banned this phone number.", 400, "phone"),
        "PhoneNumberFloodError": ("Too many login attempts for this number. Wait and try later.", 429, "phone"),
        "ApiIdInvalidError": ("TELEGRAM_API_ID / TELEGRAM_API_HASH were rejected by Telegram.", 409, None),
        "PhoneCodeInvalidError": ("That code is wrong.", 400, "code"),
        "PhoneCodeEmptyError": ("Enter the login code.", 400, "code"),
        "PhoneCodeExpiredError": ("That code expired. Start again to get a new one.", 410, "code"),
        "PasswordHashInvalidError": ("That 2FA password is wrong.", 400, "password"),
    }
    if name in table:
        msg, status, fld = table[name]
        return LoginError(msg, status, field=fld)
    if name in ("FloodWaitError", "FloodError"):
        seconds = int(getattr(exc, "seconds", 0) or 0)
        return LoginError(f"Telegram asked to wait{f' {seconds}s' if seconds else ''} before trying again.", 429)
    logger.warning("Telegram login step failed (%s)", name)  # type name only: never the message
    return LoginError("Telegram login failed. Check the details and try again.", 502)


async def start(label: str, phone: str, make_default: bool = False) -> Dict[str, Any]:
    """Validate, create a fresh session file and ask Telegram to send the login code."""
    _check_ready()
    await _purge_expired()
    label = (label or "").strip()
    if not registry.valid_label(label):
        raise LoginError("Label must be 1-40 characters: letters, digits, space, _ . -", field="label")
    phone = "".join(ch for ch in (phone or "") if ch.isdigit() or ch == "+").strip()
    if len(phone.lstrip("+")) < 6 or len(phone) > 20:
        raise LoginError("Enter the phone number with the country code, e.g. +8801XXXXXXXXX.", field="phone")
    slug = registry.slugify(label)
    for acct in registry.list_accounts():
        if acct["label"].lower() == label.lower() or registry.slugify(acct["label"]) == slug:
            raise LoginError(f"An account labelled '{acct['label']}' already exists. Remove it first or pick another label.", 409, field="label")
    if any(p.label.lower() == label.lower() or registry.slugify(p.label) == slug for p in _pending.values()):
        raise LoginError("A login for this label is already in progress.", 409, field="label")
    if len(_pending) >= MAX_PENDING:
        raise LoginError("Too many logins in progress. Finish or cancel one first.", 429)

    session_path = registry.accounts_dir() / slug
    session_path.parent.mkdir(parents=True, exist_ok=True)
    _delete_session_files(session_path)  # a leftover from an abandoned attempt
    client = TelegramClient(str(session_path), int(settings.TELEGRAM_API_ID), str(settings.TELEGRAM_API_HASH))
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except Exception as exc:
        try:
            await client.disconnect()
        except Exception:
            pass
        _delete_session_files(session_path)
        raise _map_error(exc) from None
    login_id = secrets.token_urlsafe(16)
    _pending[login_id] = _Pending(
        client=client, label=label, session_path=session_path, phone=phone,
        phone_code_hash=sent.phone_code_hash, make_default=make_default,
        expires_at=time.time() + PENDING_TTL_SECONDS,
    )
    return {"login_id": login_id, "stage": "code", "expires_in": PENDING_TTL_SECONDS}


def _get(login_id: str) -> _Pending:
    item = _pending.get(login_id)
    if item is None or item.expires_at <= time.time():
        raise LoginError("This login expired or was cancelled. Start again.", 410)
    return item


async def _finish(login_id: str, item: _Pending) -> Dict[str, Any]:
    me = await item.client.get_me()
    try:
        await item.client.disconnect()
    except Exception:
        pass
    _pending.pop(login_id, None)
    for candidate in item.session_path.parent.glob(item.session_path.name + ".session*"):
        try:
            os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    entry = registry.add_account(
        item.label, me, os.path.relpath(item.session_path, registry.ROOT), make_default=item.make_default
    )
    return {"stage": "done", "label": entry["label"]}


async def _step(login_id: str, expected: str, call: Any) -> Dict[str, Any]:
    await _purge_expired()
    item = _get(login_id)
    async with item.lock:
        if item.stage != expected:
            raise LoginError("That step is not expected right now.", 409)
        item.attempts += 1
        if item.attempts > MAX_ATTEMPTS:
            await _drop(login_id, delete_session=True)
            raise LoginError("Too many wrong attempts. Start again.", 429)
        try:
            return await call(item)
        except LoginError:
            raise
        except Exception as exc:
            err = _map_error(exc)
            if err.status in (409, 410, 429, 502):  # not something a retry of this step can fix
                await _drop(login_id, delete_session=True)
            raise err from None


async def submit_code(login_id: str, code: str) -> Dict[str, Any]:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not code:
        raise LoginError("Enter the login code.", field="code")

    async def call(item: _Pending) -> Dict[str, Any]:
        try:
            await item.client.sign_in(phone=item.phone, code=code, phone_code_hash=item.phone_code_hash)
        except Exception as exc:
            if type(exc).__name__ == "SessionPasswordNeededError":
                item.stage, item.attempts = "password", 0
                return {"stage": "password"}
            raise
        return await _finish(login_id, item)

    return await _step(login_id, "code", call)


async def submit_password(login_id: str, password: str) -> Dict[str, Any]:
    if not password:
        raise LoginError("Enter the 2FA password.", field="password")

    async def call(item: _Pending) -> Dict[str, Any]:
        await item.client.sign_in(password=password)
        return await _finish(login_id, item)

    return await _step(login_id, "password", call)


async def cancel(login_id: str) -> None:
    await _drop(login_id, delete_session=True)


async def revoke_and_delete(session_path: str) -> bool:
    """Log the session out on Telegram's side (best effort), then delete its files.

    Returns whether Telegram confirmed the logout. The files are deleted either way.
    """
    revoked = False
    path = registry.resolve_path(session_path)
    if TELETHON_AVAILABLE and settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH and \
            Path(f"{path}.session").exists():
        client = TelegramClient(str(path), int(settings.TELEGRAM_API_ID), str(settings.TELEGRAM_API_HASH))
        try:
            await asyncio.wait_for(client.connect(), timeout=15)
            if await client.is_user_authorized():
                revoked = bool(await asyncio.wait_for(client.log_out(), timeout=15))
        except Exception as exc:
            logger.warning("Telegram logout failed (%s)", type(exc).__name__)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    _delete_session_files(path)
    return revoked
