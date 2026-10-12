"""Browser profiles: named, isolated, persistent browser identities.

A PROFILE is configuration plus saved session state (cookies / local storage).
It is NOT a running browser and NOT a task. A running browser session is created
from a profile (key == profile id) and shows up in Live Broadcast.

Storage
  * Metadata: ``<BROWSER_USER_DATA_DIR>/profiles.json`` (no secrets in it).
  * Session state: ``<BROWSER_USER_DATA_DIR>/profiles/<id>/state.enc``. Cookies are
    credentials, so they are encrypted at rest (Fernet, key derived from SECRET_KEY)
    and the files are created owner-only (0600).

The API process writes metadata; the worker only reads it and writes session state.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

ROOT = Path(settings.BROWSER_USER_DATA_DIR)
_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]")
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_lock = threading.Lock()


def normalize_session_key(key: str) -> str:
    """Make any agent/profile-supplied key safe to use as an id and as a path part.

    Keys that are already safe are returned unchanged (profile ids always are). A key that
    needed rewriting gets a short hash of the ORIGINAL appended, so two different raw keys
    ("a b" and "a_b") can never collapse into the same browser context / state directory.
    """
    raw = (key or "").strip()
    if not raw:
        return "default"
    cleaned = _KEY_RE.sub("_", raw).strip(".")
    if cleaned == raw and len(raw) <= 64:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{(cleaned or 'session')[:55]}-{digest}"


def valid_profile_id(value: str) -> bool:
    return bool(_ID_RE.match(value or "")) and not value.startswith(".")


@dataclass
class BrowserProfile:
    id: str
    name: str
    description: str = ""
    viewport_width: int = 0   # 0 = use the global default
    viewport_height: int = 0
    locale: str = ""
    timezone_id: str = ""
    start_url: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProfileError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------- metadata
def _meta_path() -> Path:
    return ROOT / "profiles.json"


def _read_all() -> List[BrowserProfile]:
    path = _meta_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Could not read browser profiles: %s", exc)
        return []
    known = BrowserProfile.__dataclass_fields__.keys()
    out: List[BrowserProfile] = []
    for item in raw.get("profiles", []):
        if isinstance(item, dict) and valid_profile_id(str(item.get("id", ""))):
            out.append(BrowserProfile(**{k: v for k, v in item.items() if k in known}))
    return out


def _private_dir(path: Path) -> None:
    """Create a directory owner-only, together with its parents BELOW ROOT (ROOT itself is left as is)."""
    path.mkdir(parents=True, exist_ok=True)
    for d in (path, *[q for q in path.parents if ROOT in q.parents]):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def _write_all(profiles: List[BrowserProfile]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"profiles": [p.as_dict() for p in profiles]}, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(ROOT), prefix=".profiles-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, _meta_path())
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _clean_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "name" in data:
        name = str(data["name"] or "").strip()
        if not name or len(name) > 60:
            raise ProfileError("Name is required (max 60 characters).")
        out["name"] = name
    if "description" in data:
        out["description"] = str(data["description"] or "").strip()[:200]
    for dim, lo, hi in (("viewport_width", 320, 3840), ("viewport_height", 320, 2160)):
        if dim in data:
            value = int(data[dim] or 0)
            if value and not lo <= value <= hi:
                raise ProfileError(f"{dim} must be 0 (default) or between {lo} and {hi}.")
            out[dim] = value
    if "locale" in data:
        loc = str(data["locale"] or "").strip()
        if loc and not re.match(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$", loc):
            raise ProfileError("Locale looks invalid (example: en-US).")
        out["locale"] = loc
    if "timezone_id" in data:
        tz = str(data["timezone_id"] or "").strip()
        if tz and not re.match(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$", tz):
            raise ProfileError("Timezone looks invalid (example: Europe/Berlin).")
        out["timezone_id"] = tz
    if "start_url" in data:
        url = str(data["start_url"] or "").strip()
        if url and not re.match(r"^https?://[^\s]+$", url, re.I):
            raise ProfileError("Start URL must begin with http:// or https://")
        out["start_url"] = url[:2048]
    return out


def list_profiles() -> List[BrowserProfile]:
    return _read_all()


def get_profile(profile_id: str) -> Optional[BrowserProfile]:
    return next((p for p in _read_all() if p.id == profile_id), None)


def create_profile(data: Dict[str, Any]) -> BrowserProfile:
    cleaned = _clean_settings(data)
    if "name" not in cleaned:
        raise ProfileError("Name is required.")
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned["name"].lower()).strip("-")[:30] or "profile"
    with _lock:
        profiles = _read_all()
        if any(p.name.lower() == cleaned["name"].lower() for p in profiles):
            raise ProfileError("A profile with this name already exists.", 409)
        profile = BrowserProfile(id=f"{slug}-{secrets.token_hex(2)}", **cleaned)
        profiles.append(profile)
        _write_all(profiles)
    return profile


def update_profile(profile_id: str, data: Dict[str, Any]) -> BrowserProfile:
    cleaned = _clean_settings(data)
    with _lock:
        profiles = _read_all()
        target = next((p for p in profiles if p.id == profile_id), None)
        if target is None:
            raise ProfileError("Profile not found.", 404)
        if "name" in cleaned and any(
            p.id != profile_id and p.name.lower() == cleaned["name"].lower() for p in profiles
        ):
            raise ProfileError("A profile with this name already exists.", 409)
        for k, v in cleaned.items():
            setattr(target, k, v)
        target.updated_at = datetime.now(timezone.utc).isoformat()
        _write_all(profiles)
        return target


def delete_profile(profile_id: str) -> None:
    with _lock:
        profiles = _read_all()
        if not any(p.id == profile_id for p in profiles):
            raise ProfileError("Profile not found.", 404)
        _write_all([p for p in profiles if p.id != profile_id])
        clear_state(profile_id)


# ---------------------------------------------------------------------- saved state
def _state_dir(key: str) -> Path:
    return ROOT / "profiles" / normalize_session_key(key)


_DEFAULT_SECRET_KEY = "0123456789abcdef" * 4  # the publicly known development default in config.py


def _encryption_unsafe() -> bool:
    """Cookies are credentials: never encrypt them with a key anyone can read in the repository."""
    return settings.ENVIRONMENT == "production" and settings.SECRET_KEY == _DEFAULT_SECRET_KEY


def _fernet():
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(f"{settings.SECRET_KEY}|browser-state-v1".encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def has_state(key: str) -> bool:
    return (_state_dir(key) / "state.enc").exists()


def save_state(key: str, state: Dict[str, Any]) -> bool:
    """Encrypt and store session state. Only real profiles are persisted.

    Returns False (and writes nothing) when the profile does not exist any more, so a
    session closing after its profile was deleted cannot leave orphaned cookies behind,
    and ad-hoc agent session keys never create credential files that nothing can clear.
    """
    key = normalize_session_key(key)
    if _encryption_unsafe():
        logger.error("Browser session state NOT saved: SECRET_KEY is still the default in production.")
        return False
    with _lock:
        if get_profile(key) is None:
            return False
        directory = _state_dir(key)
        _private_dir(directory)
        # The profile id travels INSIDE the ciphertext so a copied/renamed state.enc cannot be
        # loaded into a different profile.
        payload = {"v": 1, "profile": key, "state": state}
        token = _fernet().encrypt(json.dumps(payload).encode("utf-8"))
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(token)
            os.chmod(tmp, 0o600)
            os.replace(tmp, directory / "state.enc")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return True


def load_state(key: str) -> Optional[Dict[str, Any]]:
    key = normalize_session_key(key)
    if _encryption_unsafe() or get_profile(key) is None:
        return None
    path = _state_dir(key) / "state.enc"
    if not path.exists():
        return None
    try:
        data = json.loads(_fernet().decrypt(path.read_bytes()).decode("utf-8"))
    except Exception as exc:  # wrong key, corruption: start clean rather than crash
        logger.warning("Saved session state for '%s' could not be read (%s); starting clean.", key, type(exc).__name__)
        return None
    if not isinstance(data, dict):
        return None
    if "profile" in data:
        if data.get("profile") != key or not isinstance(data.get("state"), dict):
            logger.warning("Saved session state for '%s' belongs to another profile; ignoring it.", key)
            return None
        return data["state"]
    return data if "cookies" in data or "origins" in data else None


def clear_state(key: str) -> None:
    shutil.rmtree(_state_dir(key), ignore_errors=True)
