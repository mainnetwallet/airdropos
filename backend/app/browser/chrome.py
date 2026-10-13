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
    """Locate the Google Chrome executable. Reports honestly when it cannot be found."""
    configured = str(settings.BROWSER_CHROME_EXECUTABLE or "").strip()
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
    """Version string of a Chrome executable, or None when it cannot be read."""
    if not path:
        return None
    try:
        import subprocess

        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, operator-supplied binary
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        text = f"{proc.stdout} {proc.stderr}".strip()
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", text)
        return match.group(1) if match else None
    except Exception:
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


def chrome_processes_for(user_data_dir: str) -> List[Dict[str, Any]]:
    """Chrome processes whose command line references this user-data directory."""
    try:
        import psutil  # optional dependency
    except Exception:
        return []
    target = normalize_path(user_data_dir).lower().replace("\\", "/")
    if not target:
        return []
    found: List[Dict[str, Any]] = []
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmdline = proc.info.get("cmdline") or []
            except Exception:
                continue
            if "chrome" not in name and "chromium" not in name:
                continue
            joined = " ".join(cmdline).lower().replace("\\", "/")
            if target in joined:
                found.append({"pid": proc.info.get("pid"), "name": proc.info.get("name")})
                if len(found) >= 8:
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


def installed_extensions(user_data_dir: str) -> List[InstalledExtension]:
    """Extensions INSTALLED into this Chrome profile, read from its own Preferences file.

    This is the profile's real state, not an inference from a page. Chrome writes the
    installed list here; an extension that was only passed with ``--load-extension``
    is absent from it, which is exactly why it is reported as a limitation.
    """
    settings_blob = (read_preferences(user_data_dir).get("extensions") or {}).get("settings") or {}
    out: List[InstalledExtension] = []
    for ext_id, meta in settings_blob.items():
        if not isinstance(meta, dict) or not _EXTENSION_ID_RE.match(str(ext_id)):
            continue
        if _looks_like_component_extension(meta, str(ext_id)):
            continue
        manifest = meta.get("manifest") or {}
        out.append(
            InstalledExtension(
                id=str(ext_id),
                name=str(manifest.get("name") or ""),
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
