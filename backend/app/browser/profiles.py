"""Browser profiles: named, isolated, persistent Chrome identities.

A PROFILE is configuration plus a real Chrome user-data directory. It is NOT a
running browser and NOT a task. A running browser session is created from a profile
(key == profile id) and shows up in Live Broadcast.

Storage
  * Metadata: ``<BROWSER_USER_DATA_DIR>/profiles.json`` (no secrets in it). This also
    holds which profile the AGENT uses by default, so that choice survives a restart.
  * Chrome profile data: the profile's own user-data directory (see
    ``chrome.default_user_data_dir``). Chrome writes cookies, storage, installed
    extensions and extension settings there itself; AirdropOS never parses or edits it
    except to READ the installed-extension list.
  * Session state: ``<BROWSER_USER_DATA_DIR>/profiles/<id>/state.enc``. Used only by
    the ephemeral fallback context. Cookies are credentials, so they are encrypted at
    rest (Fernet, key derived from SECRET_KEY) and the files are created owner-only
    (0600).

The API process writes metadata; the worker only reads it and writes session state.

Deleting a profile's CONFIGURATION never deletes its Chrome profile data. Wiping the
data is a separate, explicitly confirmed operation (``delete_profile_data``).
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

from backend.app.browser import chrome
from backend.app.core.config import settings

logger = logging.getLogger(__name__)

ROOT = Path(settings.BROWSER_USER_DATA_DIR)
_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]")
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_lock = threading.Lock()

BROWSER_TYPES = ("chrome", "chromium")
LAUNCH_MODES = ("managed", "attach")


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

    # ---------------------------------------------------------------- Chrome profile
    browser_type: str = "chrome"        # installed Google Chrome, or Playwright Chromium
    launch_mode: str = "managed"       # "managed" = AirdropOS launches it; "attach" = existing Chrome
    executable_path: str = ""          # empty = auto-detect Google Chrome
    user_data_dir: str = ""            # empty = <profile root>/<id>, resolved on create
    headless: bool = False             # managed mode only; extensions need a real window
    debugger_host: str = ""            # attach mode only; must be loopback
    debugger_port: int = 0             # attach mode only
    unpacked_extensions: str = ""      # comma-separated developer extension directories
    is_default_agent: bool = False

    # ---------------------------------------------------------------- live state
    last_used_at: str = ""
    last_opened_at: str = ""
    last_error: str = ""
    last_recovery: str = ""

    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def resolved_user_data_dir(self) -> str:
        return self.user_data_dir or chrome.default_user_data_dir(self.id)

    def resolved_executable(self) -> str:
        """The executable this profile will actually launch, or "" when none is available."""
        if self.executable_path:
            return self.executable_path
        if self.browser_type == "chrome":
            return str(chrome.find_chrome_executable().get("path") or "")
        return ""  # chromium: Playwright's own build

    def extension_dirs(self) -> List[str]:
        return [d.strip() for d in (self.unpacked_extensions or "").split(",") if d.strip()]


class ProfileError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------- metadata
def _meta_path() -> Path:
    return ROOT / "profiles.json"


def _read_doc() -> Dict[str, Any]:
    path = _meta_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Could not read browser profiles: %s", exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_all() -> List[BrowserProfile]:
    raw = _read_doc()
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


def _write_doc(doc: Dict[str, Any]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ROOT), prefix=".profiles-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2))
        os.replace(tmp, _meta_path())
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_all(profiles: List[BrowserProfile]) -> None:
    """Persist the profile list, keeping every other key in the document (the default)."""
    doc = _read_doc()
    doc["profiles"] = [p.as_dict() for p in profiles]
    doc.setdefault("default_agent_profile", "")
    _write_doc(doc)



def _clean_path(raw: Any, label: str, *, must_exist: bool = False, must_be_dir: bool = False) -> str:
    """Canonicalize an operator-supplied path and refuse the obviously wrong ones.

    Paths are resolved on the BACKEND so ``%LOCALAPPDATA%\\...`` and ``~`` work
    regardless of what the frontend can express, and a nonexistent parent is a
    configuration error rather than a surprise at launch time.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    # Expand FIRST: ``%LOCALAPPDATA%\...`` only becomes an absolute path after expansion,
    # so checking the raw text would refuse a perfectly valid Windows path.
    text = os.path.expandvars(os.path.expanduser(text))
    if not re.match(r"^[A-Za-z]:[\\/]|^\\\\|^/", text):
        raise ProfileError(f"{label} must be an absolute path (for example C:\\... or /home/...).")
    resolved = chrome.normalize_path(text)
    target = Path(resolved)
    if must_exist and not target.exists():
        raise ProfileError(f"{label} does not exist: {resolved}", 400)
    if must_be_dir and target.exists() and not target.is_dir():
        raise ProfileError(f"{label} is not a directory: {resolved}", 400)
    if target.exists():
        try:
            resolved = str(target.resolve(strict=False))
        except OSError:
            pass
    return resolved


def _clean_user_data_dir(raw: Any, profile_id: str = "") -> str:
    """Validate a managed Chrome profile directory.

    Refuses a real person's Chrome/Chromium profile root: launching Chrome against it
    would put the agent inside the operator's own logged-in browser, and a later
    extension install would write into it.
    """
    text = str(raw or "").strip()
    if not text:
        return chrome.default_user_data_dir(profile_id) if profile_id else ""
    resolved = _clean_path(text, "Chrome profile directory", must_be_dir=True)
    if chrome.is_personal_user_data_dir(resolved):
        raise ProfileError(
            "That directory is a real Chrome/Chromium profile (a person's own browser data). "
            "AirdropOS will not launch a managed profile there. Choose a dedicated directory such "
            "as the default one under the AirdropOS profile root."
        )
    return resolved


def _clean_settings(data: Dict[str, Any], profile_id: str = "") -> Dict[str, Any]:
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

    # ---------------------------------------------------------------- Chrome config
    if "browser_type" in data:
        browser_type = str(data["browser_type"] or "chrome").strip().lower()
        if browser_type not in BROWSER_TYPES:
            raise ProfileError(f"Browser type must be one of {', '.join(BROWSER_TYPES)}.")
        out["browser_type"] = browser_type
    if "launch_mode" in data:
        mode = str(data["launch_mode"] or "managed").strip().lower()
        if mode not in LAUNCH_MODES:
            raise ProfileError(f"Launch mode must be one of {', '.join(LAUNCH_MODES)}.")
        out["launch_mode"] = mode
    if "executable_path" in data:
        out["executable_path"] = _clean_path(
            data["executable_path"], "Chrome executable", must_exist=True, must_be_dir=False
        )
    if "user_data_dir" in data:
        out["user_data_dir"] = _clean_user_data_dir(data["user_data_dir"], profile_id)
    if "headless" in data:
        if data["headless"] is None:
            out["headless"] = False
        else:
            out["headless"] = bool(data["headless"])
    if "debugger_host" in data or "debugger_port" in data:
        host = str(data.get("debugger_host") or "").strip()
        port = int(data.get("debugger_port") or 0)
        if host or port:
            if not settings.BROWSER_ALLOW_ATTACH:
                raise ProfileError(
                    "Connecting to an existing Chrome is disabled (BROWSER_ALLOW_ATTACH=false)."
                )
            if not host:
                host = settings.BROWSER_ATTACH_HOST or "127.0.0.1"
            if port <= 0:
                raise ProfileError(
                    "A DevTools port is required to connect to an existing Chrome. Start Chrome with "
                    "--remote-debugging-port=<port> and enter that port here."
                )
            try:
                chrome.attach_endpoint(host, port)  # validates loopback + range
            except chrome.ChromeError as exc:
                raise ProfileError(str(exc)) from None
        out["debugger_host"] = host
        out["debugger_port"] = port
    if "unpacked_extensions" in data:
        raw_dirs = data["unpacked_extensions"]
        if isinstance(raw_dirs, list):
            raw_dirs = ",".join(str(d) for d in raw_dirs)
        dirs: List[str] = []
        for item in str(raw_dirs or "").split(","):
            item = item.strip()
            if not item:
                continue
            resolved = _clean_path(item, "Unpacked extension directory", must_exist=True, must_be_dir=True)
            manifest = Path(resolved) / "manifest.json"
            if not manifest.is_file():
                raise ProfileError(f"No manifest.json in the unpacked extension directory: {resolved}")
            dirs.append(resolved)
            if len(dirs) > 10:
                raise ProfileError("At most 10 unpacked extension directories per profile.")
        out["unpacked_extensions"] = ",".join(dirs)
    if "is_default_agent" in data:
        out["is_default_agent"] = bool(data["is_default_agent"])
    return out


def _validate_attach_settings(profile: BrowserProfile) -> None:
    """Existing-Chrome connection settings, checked on the MERGED profile.

    Field-by-field validation cannot catch "launch_mode became attach but no port was
    ever given", because that only becomes true once both fields are on the same row.
    """
    if profile.launch_mode != "attach":
        return
    if not settings.BROWSER_ALLOW_ATTACH:
        raise ProfileError(
            "Connecting to an existing Chrome is disabled (BROWSER_ALLOW_ATTACH=false)."
        )
    host = profile.debugger_host or settings.BROWSER_ATTACH_HOST or "127.0.0.1"
    if not profile.debugger_port:
        raise ProfileError(
            "A DevTools port is required to connect to an existing Chrome. Start Chrome with "
            "--remote-debugging-port=<port> and set that port on the profile."
        )
    try:
        chrome.attach_endpoint(host, profile.debugger_port)  # validates loopback + range
    except chrome.ChromeError as exc:
        raise ProfileError(str(exc)) from None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_profiles() -> List[BrowserProfile]:
    return _read_all()


def get_profile(profile_id: str) -> Optional[BrowserProfile]:
    return next((p for p in _read_all() if p.id == profile_id), None)


def default_agent_profile_id() -> str:
    """Id of the profile the agent uses when a task does not name one."""
    return str(_read_doc().get("default_agent_profile") or "")


def get_default_agent_profile() -> Optional[BrowserProfile]:
    """The agent's default profile, or None.

    The stored id wins; a profile still flagged ``is_default_agent`` is honoured too so
    a selection made before this field existed is not silently lost. The result always
    belongs to a profile that actually exists -- a stale id never resolves to a
    substitute.
    """
    profiles = _read_all()
    stored = default_agent_profile_id()
    found = next((p for p in profiles if p.id == stored), None)
    if found is not None:
        return found
    flagged = [p for p in profiles if p.is_default_agent]
    return flagged[0] if flagged else None


def resolve_session_key(supplied: Optional[str]) -> str:
    """Which browser profile a browser call targets.

    An explicitly supplied key always wins. With no key the agent uses its default
    profile, and only if there is none does it fall back to an ad-hoc, non-persistent
    session. It never silently substitutes a different saved profile.
    """
    raw = str(supplied or "").strip()
    if raw:
        return normalize_session_key(raw)
    default = get_default_agent_profile()
    if default is not None:
        return default.id
    return "default"


def set_default_agent_profile(profile_id: Optional[str]) -> Optional[BrowserProfile]:
    """Make one profile the agent's default (or clear it with None)."""
    with _lock:
        profiles = _read_all()
        if profile_id in (None, ""):
            doc = _read_doc()
            doc["profiles"] = [p.as_dict() for p in profiles]
            doc["default_agent_profile"] = ""
            _write_doc(doc)
            for p in profiles:
                if p.is_default_agent:
                    p.is_default_agent = False
            _write_all(profiles)
            return None
        if not valid_profile_id(str(profile_id)) or get_profile(str(profile_id)) is None:
            raise ProfileError("Profile not found.", 404)
        target = str(profile_id)
        for p in profiles:
            p.is_default_agent = p.id == target
            if p.is_default_agent:
                p.updated_at = _now()
        doc = _read_doc()
        doc["profiles"] = [p.as_dict() for p in profiles]
        doc["default_agent_profile"] = target
        _write_doc(doc)
        return next(p for p in profiles if p.id == target)


def record_use(profile_id: str, *, opened: bool = False, error: str = "", recovery: str = "") -> None:
    """Persist last-used / last-error / recovery information for the profile list.

    Best effort: a failure here must never break a browser operation, so the read and
    the write are each guarded and the previous values are kept on error.
    """
    try:
        with _lock:
            profiles = _read_all()
            target = next((p for p in profiles if p.id == profile_id), None)
            if target is None:
                return
            target.last_used_at = _now()
            if opened:
                target.last_opened_at = _now()
            if error:
                target.last_error = str(error)[:300]
            if recovery:
                target.last_recovery = str(recovery)[:300]
            _write_all(profiles)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record profile use for '%s': %s", profile_id, exc)


def create_profile(data: Dict[str, Any]) -> BrowserProfile:
    cleaned = _clean_settings(data)
    if "name" not in cleaned:
        raise ProfileError("Name is required.")
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned["name"].lower()).strip("-")[:30] or "profile"
    with _lock:
        profiles = _read_all()
        if any(p.name.lower() == cleaned["name"].lower() for p in profiles):
            raise ProfileError("A profile with this name already exists.", 409)
        profile_id = f"{slug}-{secrets.token_hex(2)}"
        # The default Chrome profile directory is only knowable once the id exists.
        if "user_data_dir" not in cleaned:
            cleaned["user_data_dir"] = chrome.default_user_data_dir(profile_id)
        profile = BrowserProfile(id=profile_id, **cleaned)
        _validate_attach_settings(profile)
        profiles.append(profile)
        _write_all(profiles)
        # First profile created becomes the agent's default, so a fresh install works
        # without a second manual step, and the choice is persisted like any other.
        if not get_default_agent_profile() and profile.launch_mode == "managed":
            doc = _read_doc()
            doc["profiles"] = [p.as_dict() for p in profiles]
            doc["default_agent_profile"] = profile.id
            profile.is_default_agent = True
            _write_doc(doc)
            _write_all(profiles)
    return profile


def update_profile(profile_id: str, data: Dict[str, Any]) -> BrowserProfile:
    cleaned = _clean_settings(data, profile_id=profile_id)
    with _lock:
        profiles = _read_all()
        target = next((p for p in profiles if p.id == profile_id), None)
        if target is None:
            raise ProfileError("Profile not found.", 404)
        if "name" in cleaned and any(
            p.id != profile_id and p.name.lower() == cleaned["name"].lower() for p in profiles
        ):
            raise ProfileError("A profile with this name already exists.", 409)
        previous_dir = target.resolved_user_data_dir
        for k, v in cleaned.items():
            setattr(target, k, v)
        _validate_attach_settings(target)
        target.updated_at = _now()
        if target.resolved_user_data_dir != previous_dir and target.launch_mode == "managed":
            logger.info(
                "Profile '%s' now uses a different Chrome directory; previously used state was left "
                "untouched at %s", profile_id, previous_dir,
            )
        _write_all(profiles)
        return target


def delete_profile(profile_id: str) -> None:
    """Remove the profile CONFIGURATION only.

    The Chrome profile directory, and therefore every cookie, extension and login the
    user accumulated in it, is deliberately left on disk. Wiping that data is a separate
    operation that requires an explicit confirmation (``delete_profile_data``).
    """
    with _lock:
        profiles = _read_all()
        target = next((p for p in profiles if p.id == profile_id), None)
        if target is None:
            raise ProfileError("Profile not found.", 404)
        _write_all([p for p in profiles if p.id != profile_id])
        if default_agent_profile_id() == profile_id:
            doc = _read_doc()
            doc["default_agent_profile"] = ""
            _write_doc(doc)
        clear_state(profile_id)


def delete_profile_data(profile_id: str, *, confirm: str) -> Dict[str, Any]:
    """Delete the Chrome profile DATA of a profile. Requires an exact confirmation token.

    ``confirm`` must equal the profile id. Refusing the real person's browser data
    directory is unconditional, so a mistake cannot wipe a personal Chrome profile even
    with the right token.
    """
    profile = get_profile(profile_id)
    if profile is None:
        raise ProfileError("Profile not found.", 404)
    if str(confirm) != profile_id:
        raise ProfileError(
            "Deleting the Chrome profile data is irreversible. Re-send the profile id to confirm."
        )
    target = profile.resolved_user_data_dir
    if not target:
        raise ProfileError("This profile has no Chrome profile directory.", 409)
    if chrome.is_personal_user_data_dir(target):
        raise ProfileError(
            "Refusing to delete that directory: it is a real Chrome/Chromium profile, not one AirdropOS manages."
        )
    if profile.launch_mode == "attach":
        raise ProfileError(
            "This profile connects to an existing Chrome, so AirdropOS does not own its data directory and "
            "will not delete it. Remove the Chrome data yourself if that is what you want.",
            409,
        )
    directory = Path(target)
    if not directory.is_dir():
        return {"deleted": False, "path": target, "detail": "The directory does not exist; nothing was removed."}
    if directory == Path(settings.BROWSER_USER_DATA_DIR).resolve(strict=False):
        raise ProfileError("Refusing to delete the AirdropOS browser data root.", 409)
    size = chrome.profile_data_size(target)
    shutil.rmtree(directory, ignore_errors=True)
    return {"deleted": True, "path": target, "bytes_freed": size}


def validate_profile_config(profile: BrowserProfile) -> List[str]:
    """Every reason this profile could not be launched right now (empty = looks launchable)."""
    problems: List[str] = []
    if profile.launch_mode == "attach":
        if not settings.BROWSER_ALLOW_ATTACH:
            problems.append("Connecting to an existing Chrome is disabled (BROWSER_ALLOW_ATTACH=false).")
        if not profile.debugger_port:
            problems.append("No DevTools port is configured for the existing-Chrome connection.")
        return problems
    if not settings.BROWSER_PERSISTENT_PROFILES:
        problems.append(
            "Persistent Chrome profiles are disabled (BROWSER_PERSISTENT_PROFILES=false); "
            "this profile would fall back to a temporary browser with no persistent state."
        )
    executable = profile.resolved_executable()
    if profile.browser_type == "chrome" and not executable:
        problems.append(
            "Google Chrome was not found. Set BROWSER_CHROME_EXECUTABLE or the profile's executable path."
        )
    if executable and not Path(executable).is_file():
        problems.append(f"The Chrome executable does not exist: {executable}")
    directory = profile.resolved_user_data_dir
    if not directory:
        problems.append("No Chrome profile directory is configured.")
    elif Path(directory).exists() and not Path(directory).is_dir():
        problems.append(f"The Chrome profile directory is not a directory: {directory}")
    for ext_dir in profile.extension_dirs():
        if not (Path(ext_dir) / "manifest.json").is_file():
            problems.append(f"Unpacked extension directory has no manifest.json: {ext_dir}")
    return problems



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
