"""Google Chrome specifics: discovery, launch arguments, locking, extensions.

Everything here is deterministic operating-system work. No Playwright import at
module scope (so the module stays importable and unit-testable without a browser),
no network access, and no code path that can delete or reset a user profile.

What is genuinely supported, and what is not
--------------------------------------------
* Launching the INSTALLED Google Chrome with a dedicated user-data directory is
  supported and persistent. Chrome itself enforces the directory lock, so a second
  controller cannot silently take a profile that is already open (see
  ``profile_lock``).
* Extensions INSTALLED INTO that directory (through the Chrome Web Store UI) persist
  and are detectable from the profile's own ``Preferences`` file. See
  ``installed_extensions``.
* Unpacked developer extensions passed with ``--load-extension`` are loaded for the
  session only: Chrome does not write them into the profile's installed-extension
  list. That limitation is reported, never hidden.
* Programmatic installation from the Chrome Web Store is NOT automated. Chrome shows
  an "Add to Chrome" confirmation that the user must accept; the browser tools pause
  the session for that click and verify the result afterwards.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

# Chrome's own user-data directories. Pointing a managed profile at one of these would
# silently take over (and, on a later extension install, rewrite) a person's real
# browser profile, so they are refused as a managed profile directory.
PERSONAL_USER_DATA_DIRS: Tuple[str, ...] = (
    r"%LOCALAPPDATA%\Google\Chrome\User Data",
    r"%USERPROFILE%\AppData\Local\Google\Chrome\User Data",
    r"~/Library/Application Support/Google/Chrome",
    "~/.config/google-chrome",
    "~/.config/chromium",
    "~/.config/microsoft-edge",
)

_WINDOWS_CHROME = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)
_MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_LINUX_CHROME = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

_EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")

# Version and discovery are display-only and read from FILE METADATA. They are cached
# because the browser-profile listing is polled every few seconds, and — more
# importantly — nothing here may ever execute the Chrome binary (see
# ``executable_version``).
_VERSION_CACHE: Dict[Tuple[str, Tuple[int, int]], Optional[str]] = {}
_DISCOVERY_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_DISCOVERY_TTL_SECONDS = 30.0
_DISCOVERY_CACHED_AT = 0.0


class ChromeError(Exception):
    """A Chrome launch/attach precondition failed. Safe to show to the operator."""


def expand(path: str) -> Path:
    """Expand environment variables and ``~`` in an operator-supplied path."""
    return Path(os.path.expandvars(os.path.expanduser(str(path or "").strip())))


def normalize_path(raw: str) -> str:
    """Canonical string form of an operator-supplied path (env vars expanded, no trailing sep)."""
    if not str(raw or "").strip():
        return ""
    text = os.path.expandvars(os.path.expanduser(str(raw).strip()))
    try:
        return str(Path(text).resolve(strict=False))
    except OSError:
        return text


def profile_root() -> Path:
    """Directory that holds every managed Chrome profile directory."""
    configured = str(settings.BROWSER_PROFILE_ROOT or "").strip()
    if configured:
        return expand(configured)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "AirdropOS" / "ChromeProfiles"
    return Path.home() / ".airdropos" / "chrome-profiles"


def default_user_data_dir(profile_id: str) -> str:
    """``%LOCALAPPDATA%\\AirdropOS\\ChromeProfiles\\<profile-id>`` (see :func:`profile_root`)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(profile_id or "").strip())[:64]
    return str(profile_root() / (safe or "profile"))


def reset_caches() -> None:
    """Forget the display caches. Used by tests and after an operator changes the setting."""
    _VERSION_CACHE.clear()
    _DISCOVERY_CACHE.clear()
    global _DISCOVERY_CACHED_AT
    _DISCOVERY_CACHED_AT = 0.0


def is_personal_user_data_dir(raw: str) -> bool:
    """True when ``raw`` is (or is inside) a real person's Chrome/Chromium profile root."""
    try:
        target = expand(raw).resolve(strict=False)
    except OSError:
        return True
    for candidate in PERSONAL_USER_DATA_DIRS:
        personal = expand(candidate).resolve(strict=False)
        if target == personal or personal in target.parents:
            return True
    return False


def find_chrome_executable() -> Dict[str, Any]:
    """Locate the Google Chrome executable. Reports honestly when it cannot be found.

    Pure filesystem inspection — it never starts Chrome. Cached briefly because the
    profile listing resolves this once per profile per poll.
    """
    global _DISCOVERY_CACHED_AT
    configured = str(settings.BROWSER_CHROME_EXECUTABLE or "").strip()
    cache_key = (configured, os.name)
    now = time.monotonic()
    cached = _DISCOVERY_CACHE.get(cache_key)
    if cached is not None and now - _DISCOVERY_CACHED_AT < _DISCOVERY_TTL_SECONDS:
        return cached
    result = _find_chrome_executable_uncached(configured)
    _DISCOVERY_CACHE.clear()
    _DISCOVERY_CACHE[cache_key] = result
    _DISCOVERY_CACHED_AT = now
    return result


def _find_chrome_executable_uncached(configured: str) -> Dict[str, Any]:
    tried: List[str] = []
    if configured:
        candidate = expand(configured)
        tried.append(str(candidate))
        if candidate.is_file():
            return {"path": str(candidate), "source": "BROWSER_CHROME_EXECUTABLE", "exists": True}
    if os.name == "nt":
        candidates = [expand(p) for p in _WINDOWS_CHROME]
    elif os.sys.platform == "darwin":  # type: ignore[attr-defined]
        candidates = [Path(_MAC_CHROME)]
    else:
        candidates = [Path(p) for p in _LINUX_CHROME]
    for candidate in candidates:
        tried.append(str(candidate))
        if candidate.is_file():
            return {"path": str(candidate), "source": "auto-detected", "exists": True}
    return {
        "path": "",
        "source": "",
        "exists": False,
        "checked": tried,
        "detail": (
            "Google Chrome was not found in the usual install locations. Set "
            "BROWSER_CHROME_EXECUTABLE, or set the executable path on the browser profile."
        ),
    }


def executable_version(path: str) -> Optional[str]:
    """Version string of a Chrome executable, read WITHOUT executing it.

    Chrome on Windows has no ``--version`` switch: it forwards to, or starts, the full
    browser with the operator's PERSONAL user-data directory. Probing it therefore made
    every read of the browser-profile list pop a Chrome window (and show the first-run
    profile picker) whenever Chrome was not already running. The version is read from
    file metadata instead, which is the same source Windows Explorer shows.

    Display-only: a failure here returns None and must never block anything.
    """
    if not path:
        return None
    key = (str(path), _stat_signature(path))
    cached = _VERSION_CACHE.get(key)
    if cached is not None:
        return cached
    version = _read_version_windows(path) or _read_version_macos(path) or _read_version_linux(path)
    _VERSION_CACHE[key] = version
    # Bound the cache: the listing is polled, the key space is tiny.
    if len(_VERSION_CACHE) > 32:
        _VERSION_CACHE.clear()
    return version


def _stat_signature(path: str) -> Tuple[int, int]:
    """(mtime, size) so a replaced executable is re-read."""
    try:
        stat = os.stat(path)
        return (int(stat.st_mtime), int(stat.st_size))
    except OSError:
        return (0, 0)


def _four_parts(ms: int, ls: int) -> str:
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _read_version_windows(path: str) -> Optional[str]:
    """The PE ``VS_FIXEDFILEINFO`` resource, read in-process. No subprocess, no shell.

    The version block is fetched with the Win32 version API and then parsed directly:
    ``VerQueryValueW`` is unreliable here, whereas walking the documented layout
    (header, NUL-terminated ``VS_VERSION_INFO`` key, DWORD alignment, then the fixed
    info struct) is deterministic and cross-checkable via the 0xFEEF04BD signature.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        import struct
        from ctypes import wintypes

        api = ctypes.WinDLL("version", use_last_error=True)  # type: ignore[attr-defined]
        api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        api.GetFileVersionInfoW.restype = wintypes.BOOL

        size = api.GetFileVersionInfoSizeW(str(path), None)
        if not size or size > 4 * 1024 * 1024:  # a version block is never megabytes
            return None
        buffer = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(str(path), 0, size, ctypes.byref(buffer)):
            return None
        raw = bytes(buffer.raw)

        marker = b"V\x00S\x00_\x00V\x00E\x00R\x00S\x00I\x00O\x00N\x00_\x00I\x00N\x00F\x00O\x00"
        index = raw.find(marker)
        if index < 0:
            return None
        value_at = (index + len(marker) + 2 + 3) & ~3  # past the NUL terminator, DWORD aligned
        if value_at + 16 > len(raw):
            return None
        signature, _struct, ms, ls = struct.unpack_from("<IIII", raw, value_at)
        if signature != 0xFEEF04BD:  # VS_FFI_SIGNATURE
            return None
        return _four_parts(ms, ls)
    except Exception:
        return None


def _read_version_macos(path: str) -> Optional[str]:
    """`CFBundleShortVersionString` from the app bundle's Info.plist."""
    try:
        parts = Path(path).parts
        if ".app" not in parts:
            return None
        plist = Path(*parts[: parts.index(".app") + 1]) / "Contents" / "Info.plist"
        if not plist.is_file():
            return None
        text = plist.read_text(encoding="utf-8", errors="replace")
        match = re.search(
            r"<key>CFBundleShortVersionString</key>\s*<string>([^<]+)</string>", text
        )
        return match.group(1).strip() if match else None
    except Exception:
        return None


def _read_version_linux(path: str) -> Optional[str]:
    """The `product_version` file the Debian/Ubuntu Chrome package installs."""
    try:
        if os.name == "nt" or Path(path).suffix.lower() == ".exe":
            return None
        resolved = Path(os.path.realpath(str(path)))
        for marker in (resolved.parent / "product_version",
                       resolved.parent.parent / "etc" / "product_version"):
            if marker.is_file():
                value = marker.read_text(encoding="utf-8", errors="replace").strip()
                if re.fullmatch(r"[\d.]+", value):
                    return value
    except Exception:
        pass
    return None


# Chrome writes these into a user-data directory while it owns the profile. Their
# presence is a strong "somebody else has it open" signal, but Chrome itself is the
# authority: it refuses to open a locked directory, and that refusal is what
# ``launch_persistent_context`` surfaces.
_LOCK_MARKERS = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")


def lock_markers(user_data_dir: str) -> List[str]:
    directory = expand(user_data_dir)
    if not directory.is_dir():
        return []
    return [name for name in _LOCK_MARKERS if (directory / name).exists()]


def _norm_dir(value: str) -> str:
    """Canonical comparison form for a directory path.

    BOTH sides of every comparison must go through this. Normalising only one side (for
    example the flag value but not the target) silently matches nothing on Windows, which
    would quietly disable profile-lock detection.
    """
    return normalize_path(str(value or "")).lower().replace("\\", "/").rstrip("/")


def _command_line_uses_profile(cmdline: List[str], target: str) -> bool:
    """True only when ``--user-data-dir`` in this command line points at ``target``.

    Matching on the flag's VALUE rather than on the path appearing anywhere in the command
    line matters for safety: Chrome child processes sometimes carry a page URL as an
    argument, so a bare substring test could match somebody's unrelated window. A managed
    Chrome is ALWAYS launched with this flag, and Chrome requires it to use a non-default
    directory, so requiring it cannot miss a real one.
    """
    wanted = _norm_dir(target)
    if not wanted:
        return False
    for index, arg in enumerate(cmdline):
        value = None
        if arg.lower().startswith("--user-data-dir="):
            value = arg.split("=", 1)[1]
        elif arg.lower() == "--user-data-dir" and index + 1 < len(cmdline):
            value = cmdline[index + 1]
        if value is None:
            continue
        if _norm_dir(value.strip().strip('"').strip("'")) == wanted:
            return True
    return False


def chrome_processes_for(user_data_dir: str) -> List[Dict[str, Any]]:
    """Chrome processes whose ``--user-data-dir`` is exactly this directory.

    Used for profile-lock detection and, when AirdropOS stops a managed profile, for
    terminating only the processes it started. A user's own Chrome, running a different
    profile, is never matched and never touched.
    """
    try:
        import psutil  # optional dependency
    except Exception:
        return []
    target = _norm_dir(user_data_dir)
    if not target:
        return []
    found: List[Dict[str, Any]] = []
    try:
        # ad_value=None: a process we may not inspect (another user's, or a protected one)
        # becomes None instead of raising, so the scan simply skips it.
        for proc in psutil.process_iter(["pid", "name", "cmdline"], ad_value=None):
            try:
                name = (proc.info.get("name") or "").lower()
                cmdline = proc.info.get("cmdline") or []
            except Exception:
                continue
            if "chrome" not in name and "chromium" not in name:
                continue
            if _command_line_uses_profile(cmdline, target):
                found.append({"pid": proc.info.get("pid"), "name": proc.info.get("name")})
                if len(found) >= 16:
                    break
    except Exception:
        return found
    return found


def profile_lock(user_data_dir: str) -> Dict[str, Any]:
    """Whether another Chrome appears to own this profile directory right now."""
    target = normalize_path(user_data_dir)
    if not target:
        return {"locked": False, "reason": "", "processes": []}
    processes = chrome_processes_for(target)
    markers = lock_markers(target)
    if processes:
        return {
            "locked": True,
            "reason": (
                f"Chrome process(es) {[p.get('pid') for p in processes]} already have this profile open."
            ),
            "processes": processes,
            "markers": markers,
        }
    if markers:
        return {
            "locked": True,
            "reason": f"The profile directory still holds Chrome's lock file(s): {', '.join(markers)}.",
            "processes": [],
            "markers": markers,
        }
    return {"locked": False, "reason": "", "processes": [], "markers": markers}


def ensure_profile_dir(user_data_dir: str) -> str:
    """Create the managed profile directory (owner-only) and return its canonical path."""
    directory = expand(user_data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass  # Windows and some network filesystems have no POSIX mode
    return normalize_path(str(directory))


# Flags that make a managed Chrome behave like a dedicated automation browser rather
# than hijacking the operator's real session. `--no-first-run`/`--no-default-browser-check`
# are what stop the first-run wizard and the "set Chrome as default" prompt from ever
# appearing during an automated launch.
BASE_CHROME_ARGS: Tuple[str, ...] = (
    "--no-first-run",
    "--no-default-browser-check",
    "--no-service-autorun",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-client-side-phishing-detection",
    "--disable-sync",
    "--disable-features=Translate,OptimizationHints,MediaRouter",
    "--password-store=basic",
    "--use-mock-keychain",
)


# Chrome's own New Tab page. Playwright's launch_persistent_context() ALWAYS appends
# "about:blank" to the command line (and rejects any URL passed through ``args``), so a
# managed Chrome starts on a blank white tab. This is the supported way to show the
# normal New Tab experience instead: reuse that first tab and navigate it here.
NEW_TAB_URL = "chrome://newtab/"
BLANK_PAGE_URLS: Tuple[str, ...] = ("", "about:blank")

# Default switches Playwright adds that must NOT reach a managed, extension-capable
# Chrome. ``--disable-extensions`` switches off every installed extension (MetaMask and
# the like) no matter what ``--load-extension`` / ``--disable-extensions-except`` say.
PLAYWRIGHT_IGNORED_DEFAULT_ARGS: Tuple[str, ...] = ("--disable-extensions",)


def is_blank_page_url(url: Optional[str]) -> bool:
    """True for the empty tab Chrome opens on a fresh launch (about:blank or no URL yet)."""
    return str(url or "").strip().lower() in BLANK_PAGE_URLS


def build_chrome_args(
    *,
    headless: bool,
    extension_dirs: Optional[List[str]] = None,
    extra: Optional[List[str]] = None,
    enable_automation_flag: bool = False,
) -> List[str]:
    """Chrome command line for a managed profile.

    ``--no-sandbox`` is NOT added: a persistent profile that loads extensions must run
    with Chrome's own sandbox, and the existing Playwright-managed Chromium launch
    keeps its own flags. ``--enable-automation`` is likewise opt-in because it makes
    every page announce that it is automated, which some sites treat as a bot.
    """
    args = list(BASE_CHROME_ARGS)
    if extra:
        args.extend(str(a) for a in extra if a)
    if enable_automation_flag:
        args.append("--enable-automation")
    dirs = [str(d) for d in (extension_dirs or []) if d]
    if dirs:
        # Load ONLY these extensions. Without --disable-extensions-except Chrome would
        # also load whatever else happens to be unpacked in the profile.
        args.append("--disable-extensions-except=" + ",".join(dirs))
        args.append("--load-extension=" + ",".join(dirs))
    if headless:
        # The new headless mode is the one that supports extensions. `--headless=old`
        # would silently give a profile with no working extensions at all.
        args.append("--headless=new")
    return args


@dataclass
class InstalledExtension:
    id: str
    name: str = ""
    version: str = ""
    enabled: bool = True
    from_webstore: bool = False
    description: str = ""
    homepage: str = ""
    manifest_version: int = 0
    permissions: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "enabled": self.enabled,
            "from_webstore": self.from_webstore,
            "description": self.description[:200],
            "homepage": self.homepage,
            "manifest_version": self.manifest_version,
            "permissions": self.permissions[:40],
        }


def _preferences_path(user_data_dir: str) -> Path:
    return expand(user_data_dir) / "Default" / "Preferences"


def read_preferences(user_data_dir: str) -> Dict[str, Any]:
    """The Chrome profile's own Preferences JSON, or {} when it is absent/corrupt."""
    path = _preferences_path(user_data_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("Unreadable Chrome preferences at %s: %s", path, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _looks_like_component_extension(meta: Dict[str, Any], ext_id: str) -> bool:
    location = meta.get("location")
    if location is not None and int(location) != 1:
        # location 1 == the profile's own Extensions directory (a real install);
        # 4/5/6 are policy/component/system extensions that are not user-installed.
        return True
    path = str(meta.get("path") or "")
    if path and "component" in path.lower():
        return True
    # Chrome's own built-in extensions carry a component id pattern rather than 32 letters.
    return bool(ext_id) and not _EXTENSION_ID_RE.match(ext_id)


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("Unreadable Chrome file %s: %s", path, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _extension_settings(user_data_dir: str) -> Dict[str, Any]:
    """Install records for the profile's extensions, from every file Chrome keeps them in.

    Current Chrome records user-installed extensions in ``Secure Preferences``, not in
    ``Preferences``; reading only the latter reports a correct Web Store install as absent.
    Both are merged, ``Secure Preferences`` winning because it is the one Chrome protects.
    """
    base = expand(user_data_dir) / "Default"
    merged: Dict[str, Any] = {}
    for name in ("Preferences", "Secure Preferences"):
        blob = (_read_json_file(base / name).get("extensions") or {}).get("settings") or {}
        if isinstance(blob, dict):
            merged.update({k: v for k, v in blob.items() if isinstance(v, dict)})
    return merged


def _localized(manifest: Dict[str, Any], value: str, extension_dir: Path) -> str:
    """Resolve a ``__MSG_key__`` manifest string from the extension's own locale file."""
    match = re.fullmatch(r"__MSG_(.+?)__", value or "")
    if not match:
        return value
    key = match.group(1)
    locales = [str(manifest.get("default_locale") or ""), "en", "en_US"]
    for locale in locales:
        if not locale:
            continue
        data = _read_json_file(extension_dir / "_locales" / locale / "messages.json")
        # Chrome treats message keys case-insensitively.
        for k, v in data.items():
            if str(k).lower() == key.lower() and isinstance(v, dict) and v.get("message"):
                return str(v["message"])
    return value


def _extensions_on_disk(user_data_dir: str) -> Dict[str, Dict[str, Any]]:
    """Extensions unpacked under ``Default/Extensions/<id>/<version>`` with their manifest.

    Written the moment an install completes, so it sees an extension even before Chrome has
    flushed its preference files. Chrome only moves a package here after a successful install.
    """
    root = expand(user_data_dir) / "Default" / "Extensions"
    found: Dict[str, Dict[str, Any]] = {}
    if not root.is_dir():
        return found
    for ext_dir in root.iterdir():
        if not ext_dir.is_dir() or not _EXTENSION_ID_RE.match(ext_dir.name):
            continue
        versions = sorted((p for p in ext_dir.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
        for version_dir in versions:
            manifest = _read_json_file(version_dir / "manifest.json")
            if manifest:
                manifest = dict(manifest)
                manifest["name"] = _localized(manifest, str(manifest.get("name") or ""), version_dir)
                manifest["description"] = _localized(
                    manifest, str(manifest.get("description") or ""), version_dir
                )
                manifest.setdefault("version", version_dir.name)
                found[ext_dir.name] = manifest
                break
    return found


def installed_extensions(user_data_dir: str) -> List[InstalledExtension]:
    """Extensions INSTALLED into this Chrome profile, from the profile's own files.

    This is the profile's real state, not an inference from a page: Chrome's install records
    (``Secure Preferences`` and ``Preferences``) plus the unpacked ``Extensions`` folder. An
    extension that was only passed with ``--load-extension`` is absent from all of them, which
    is exactly why it is reported as a limitation.
    """
    settings_blob = _extension_settings(user_data_dir)
    on_disk = _extensions_on_disk(user_data_dir)
    out: List[InstalledExtension] = []
    seen = set()

    for ext_id, meta in settings_blob.items():
        if not _EXTENSION_ID_RE.match(str(ext_id)):
            continue
        if _looks_like_component_extension(meta, str(ext_id)):
            continue
        manifest = meta.get("manifest") or on_disk.get(str(ext_id)) or {}
        seen.add(str(ext_id))
        out.append(
            InstalledExtension(
                id=str(ext_id),
                name=str(manifest.get("name") or on_disk.get(str(ext_id), {}).get("name") or ""),
                version=str(manifest.get("version") or ""),
                # Chrome's extension state: 1 = enabled, 0 = disabled, 2 = blacklisted.
                enabled=int(meta.get("state", 1)) == 1,
                from_webstore=bool(meta.get("from_webstore")),
                description=str(manifest.get("description") or ""),
                homepage=str(manifest.get("homepage_url") or ""),
                manifest_version=int(manifest.get("manifest_version") or 0),
                permissions=[str(p) for p in (manifest.get("permissions") or []) if isinstance(p, str)],
            )
        )

    # Unpacked on disk but not (yet) in an install record: a completed install Chrome has not
    # flushed to its preference files.
    for ext_id, manifest in on_disk.items():
        if ext_id in seen:
            continue
        out.append(
            InstalledExtension(
                id=ext_id,
                name=str(manifest.get("name") or ""),
                version=str(manifest.get("version") or ""),
                enabled=True,
                from_webstore="google.com" in str(manifest.get("update_url") or ""),
                description=str(manifest.get("description") or ""),
                homepage=str(manifest.get("homepage_url") or ""),
                manifest_version=int(manifest.get("manifest_version") or 0),
                permissions=[str(p) for p in (manifest.get("permissions") or []) if isinstance(p, str)],
            )
        )
    out.sort(key=lambda e: e.name.lower() or e.id)
    return out


def extension_manifest(user_data_dir: str, extension_id: str) -> Dict[str, Any]:
    """The manifest of an installed extension, read from disk (``{}`` when absent)."""
    ext_id = str(extension_id or "").strip().lower()
    if not _EXTENSION_ID_RE.match(ext_id):
        return {}
    base = expand(user_data_dir) / "Default" / "Extensions" / ext_id
    if not base.is_dir():
        return {}
    versions = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    for version_dir in versions:
        manifest = version_dir / "manifest.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if isinstance(data, dict):
                data.setdefault("version", version_dir.name)
                return data
    return {}


def extension_matches(user_data_dir: str, query: str) -> List[InstalledExtension]:
    """Installed extensions whose name, id or description matches ``query``."""
    needle = str(query or "").strip().lower()
    if not needle:
        return installed_extensions(user_data_dir)
    return [
        e
        for e in installed_extensions(user_data_dir)
        if needle in e.id.lower()
        or needle in e.name.lower()
        or needle in e.description.lower()
    ]


def profile_data_size(user_data_dir: str) -> int:
    """Total bytes a managed profile directory occupies (best effort, bounded)."""
    directory = expand(user_data_dir)
    if not directory.is_dir():
        return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(directory):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
                if total > 4 * 1024 * 1024 * 1024:  # stop counting after 4 GiB
                    return total
    except OSError:
        return total
    return total


# ------------------------------------------------------------------ attach mode
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def attach_endpoint(host: str, port: int) -> str:
    """CDP endpoint for an existing Chrome, restricted to loopback.

    A non-loopback host is refused here rather than at launch: a DevTools endpoint is
    an unauthenticated remote-control port and must never be reachable off-box.
    """
    clean = str(host or "127.0.0.1").strip().lower()
    if clean not in LOOPBACK_HOSTS:
        raise ChromeError(
            "An existing-Chrome connection is only supported on 127.0.0.1. "
            "Chrome's remote debugging port is unauthenticated and must never be "
            "exposed beyond this machine."
        )
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        raise ChromeError("The DevTools port must be a number.") from None
    if not 1024 <= port_number <= 65535:
        raise ChromeError("The DevTools port must be between 1024 and 65535.")
    return f"http://127.0.0.1:{port_number}"


# ------------------------------------------------------------------ chrome:// pages
# A deliberately tiny allowlist. `chrome://extensions` is the only internal page the
# agent needs (it is how an installed extension is verified and how settings are
# opened for the user). `chrome://version` is read-only. Everything else -- and
# especially anything that could weaken browser security -- is refused.
ALLOWED_INTERNAL_PREFIXES: Tuple[str, ...] = (
    "chrome://extensions/",
    "chrome://extensions",
    "chrome://version/",
    "chrome://version",
)
EXTENSIONS_PAGE = "chrome://extensions/"
VERSION_PAGE = "chrome://version/"


def internal_page_reason(url: str) -> Optional[str]:
    """Why an internal (chrome://) target must be refused, or None when allowed."""
    raw = str(url or "").strip()
    if not raw.lower().startswith(("chrome://", "chrome-extension://", "devtools://")):
        return "not an internal browser page"
    if not settings.BROWSER_ALLOW_INTERNAL_PAGES:
        return "internal browser pages are disabled (BROWSER_ALLOW_INTERNAL_PAGES)"
    lowered = raw.lower()
    if lowered.startswith(("chrome://extensions", "chrome://version")):
        return None
    if lowered.startswith("chrome-extension://"):
        # Only pages inside an extension that is actually installed in this profile.
        return None if extension_id_from_url(raw) else "unknown extension id"
    return (
        "only chrome://extensions and chrome://version are allowed; "
        "browser security settings cannot be changed by the agent"
    )


def extension_id_from_url(url: str) -> Optional[str]:
    """The extension id in a ``chrome-extension://<id>/...`` URL, if there is one."""
    match = re.match(r"chrome-extension://([a-p]{32})/", str(url or "").strip(), re.IGNORECASE)
    return match.group(1).lower() if match else None


__all__ = [
    "ChromeError",
    "InstalledExtension",
    "EXTENSIONS_PAGE",
    "VERSION_PAGE",
    "attach_endpoint",
    "build_chrome_args",
    "chrome_processes_for",
    "default_user_data_dir",
    "ensure_profile_dir",
    "executable_version",
    "extension_id_from_url",
    "extension_manifest",
    "extension_matches",
    "find_chrome_executable",
    "installed_extensions",
    "internal_page_reason",
    "is_personal_user_data_dir",
    "normalize_path",
    "profile_data_size",
    "profile_lock",
    "profile_root",
]
