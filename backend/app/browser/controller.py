"""Playwright Browser Controller.

Two kinds of browser session, one controller:

* MANAGED PERSISTENT CHROME (the default). The INSTALLED Google Chrome is launched
  against a dedicated user-data directory that belongs to the browser profile. Cookies,
  storage, installed extensions and extension settings therefore survive an AirdropOS
  restart, and the agent reuses the same identity on every later task. Chrome itself
  refuses to open a directory another Chrome owns, which is what makes profile locking
  real rather than advisory.
* EPHEMERAL CONTEXT (the pre-existing behaviour), used when persistent profiles are
  turned off or a profile has no usable Chrome. It has no persistence and no
  extensions; it is never silently swapped in for a managed profile, it is reported.

Contexts are isolated per profile so separate profiles never share authentication
state. Crashes, profile locks and dead tabs are detected and recovered from rather
than cached as stale handles. CAPTCHA/MFA/expiry are reported as NEEDS_USER_ACTION
instead of being circumvented (Specification Part 12).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from backend.app.browser import chrome
from backend.app.browser import profiles as profile_store
from backend.app.browser.interaction import DialogRecorder, DiagnosticsCollector, DownloadRecorder
from backend.app.browser.profiles import normalize_session_key
from backend.app.core.config import settings
from backend.app.live import hub
from backend.app.live.events import redact_text, safe_url

logger = logging.getLogger(__name__)

# Signals that human interaction is genuinely unavoidable.
CAPTCHA_PATTERNS = [
    r"captcha", r"are you a robot", r"verify you are human", r"hcaptcha", r"recaptcha",
    r"cloudflare.{0,20}challenge", r"checking your browser", r"cf-challenge",
]
MFA_PATTERNS = [r"two-factor", r"2-step", r"authenticator", r"enter.{0,20}code.{0,20}sent", r"otp"]
SESSION_EXPIRED_PATTERNS = [r"session.{0,10}expired", r"please.{0,20}log ?in", r"sign in to continue", r"unauthorized"]

USER_DATA_ROOT = Path(settings.BROWSER_USER_DATA_DIR)


@dataclass
class PageObservation:
    """What the agent can see about a page. Content is untrusted data."""

    url: str
    title: str
    text_excerpt: str
    forms: List[Dict[str, Any]] = field(default_factory=list)
    buttons: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    requires_human: bool = False
    human_reason: Optional[str] = None
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    http_status: Optional[int] = None

    # Callers (browser tools) read observations like a mapping.
    def get(self, name: str, default: Any = None) -> Any:
        return getattr(self, name, default)

    def __getitem__(self, name: str) -> Any:
        try:
            return getattr(self, name)
        except AttributeError:
            raise KeyError(name)


_DEAD_MARKERS = ("target closed", "has been closed", "browser has been closed", "crash", "context or browser")
_BLOCKED_HOSTS = ("localhost", "metadata.google.internal", "metadata")
# Chrome's own words for "this profile is already open". Matched so a lock conflict is
# reported as a lock conflict instead of a generic launch failure.
_PROFILE_LOCK_MARKERS = (
    "opening in existing browser session",
    "already in use",
    "profile appears to be in use",
    "the process cannot access the file",
)
# Cloud metadata endpoints that must never be reachable, even with BROWSER_ALLOW_PRIVATE_NETWORK.
_METADATA_IPS = frozenset(
    ipaddress.ip_address(a) for a in ("169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254")
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# IPv6 ranges that can smuggle an IPv4 destination past ``is_global`` (Python reports the
# deprecated IPv4-compatible ::a.b.c.d and site-local fec0::/10 as "global"). They are refused
# outright: nothing legitimate is reached through them, and with BROWSER_ALLOW_PRIVATE_NETWORK
# the 6to4 / Teredo forms would otherwise let a loopback or LAN address through unjudged.
_ALWAYS_BLOCKED_V6 = tuple(
    ipaddress.ip_network(n)
    for n in ("::/96", "fec0::/10", "2002::/16", "2001::/32")
)


def _unwrap_ip(ip):
    """IPv4 hidden inside IPv6 (::ffff:a.b.c.d, NAT64) is judged as the IPv4 it really is."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _ip_block_reason(ip) -> Optional[str]:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is None and ip not in _NAT64:
        if any(ip in net for net in _ALWAYS_BLOCKED_V6):
            return "IPv4-compatible, site-local and tunnelling addresses are not allowed"
    ip = _unwrap_ip(ip)
    if ip in _METADATA_IPS or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return "loopback, link-local and metadata addresses are not allowed"
    # Private LAN, carrier-grade NAT, reserved and any other non-global range.
    if not ip.is_global and not getattr(settings, "BROWSER_ALLOW_PRIVATE_NETWORK", False):
        return "private network addresses are not allowed (BROWSER_ALLOW_PRIVATE_NETWORK)"
    return None


def url_block_reason(url: str) -> Optional[str]:
    """Why a navigation target must be refused, or None when it is allowed (literal checks only).

    Only http(s) pages are opened. Loopback / link-local / cloud-metadata targets are always
    refused (the worker's own loopback hosts the live gateway, and the live view would show
    whatever the browser loads to every viewer). Private ranges need BROWSER_ALLOW_PRIVATE_NETWORK.
    Hostnames are NOT resolved here; see ``destination_block_reason`` for the resolving check.
    """
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except Exception:
        return "unparseable URL"
    if parts.scheme not in ("http", "https") or not host:
        return "only http:// and https:// pages can be opened"
    if host in _BLOCKED_HOSTS or host.endswith(".localhost") or host.endswith(".internal"):
        return "local and internal hosts are not allowed"
    try:
        return _ip_block_reason(ipaddress.ip_address(host))
    except ValueError:
        return None  # a hostname (or a legacy numeric form; the resolving check handles those)


_RESOLVE_TTL_SECONDS = 2.0
_resolve_cache: Dict[str, "tuple[float, Optional[str]]"] = {}


async def destination_block_reason(url: str) -> Optional[str]:
    """Like ``url_block_reason`` but also resolves the hostname and judges EVERY address it maps to.

    Fails closed: if the name cannot be resolved here, the destination cannot be verified, so it is
    refused. This also covers legacy numeric hosts such as ``2130706433`` or ``0x7f.1``, which the
    resolver turns into addresses. Limitation: Chromium resolves the name again itself, so a DNS
    server that answers differently the second time is only narrowed, not eliminated, by checking
    every request right before it is sent. An egress firewall remains the complete answer.
    """
    reason = url_block_reason(url)
    if reason:
        return reason
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return None  # a literal address, already judged above
    except ValueError:
        pass
    now = time.monotonic()
    cached = _resolve_cache.get(host)
    if cached is not None and now - cached[0] < _RESOLVE_TTL_SECONDS:
        return cached[1]
    port = parts.port or (443 if parts.scheme == "https" else 80)
    verdict: Optional[str] = None
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=5
        )
        if not infos:
            verdict = "the hostname could not be verified"
        for info in infos:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
            verdict = _ip_block_reason(ip)
            if verdict:
                verdict = "the hostname resolves to an address that is not allowed"
                break
    except Exception:
        verdict = "the hostname could not be verified"
    if len(_resolve_cache) > 512:
        _resolve_cache.clear()
    _resolve_cache[host] = (now, verdict)
    return verdict


def _safe_error(exc: BaseException, url: str = "") -> str:
    """An error message that is safe to hand to the agent, events and logs: no URL query
    strings, no credentials, no Playwright call logs."""
    text = str(exc).split("Call log:")[0]
    if url:
        text = text.replace(url, safe_url(url))
    text = re.sub(r"https?://\S+", lambda m: safe_url(m.group(0)), text)
    return redact_text(" ".join(text.split()))[:200] or type(exc).__name__


class ProfileLocked(RuntimeError):
    """The Chrome profile directory is already owned by another browser process."""


class BrowserUnavailable(RuntimeError):
    """Chrome could not be launched or reached at all."""


@dataclass
class SessionRecorders:
    """Per-session event capture. Redacted and bounded; nothing here holds a secret."""

    diagnostics: DiagnosticsCollector = field(default_factory=DiagnosticsCollector)
    dialogs: DialogRecorder = field(default_factory=DialogRecorder)
    downloads: DownloadRecorder = field(default_factory=lambda: DownloadRecorder(download_dir()))


def download_dir() -> Path:
    """Where browser downloads are saved. Always inside AirdropOS's own data directory."""
    configured = str(settings.BROWSER_DOWNLOAD_DIR or "").strip()
    directory = Path(configured) if configured else USER_DATA_ROOT / "downloads"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Could not create the browser download directory %s", directory)
    return directory


def upload_allowed(path: str) -> Path:
    """Resolve a file the agent is allowed to upload, refusing anything outside data/.

    Uploads are the one direction where a mistake can read the operator's disk, so the
    path must exist, be a real file, and sit inside a permitted root. AirdropOS's own
    data directory is always permitted; everything else must be opted in.
    """
    candidate = chrome.expand(path)
    if not candidate.is_absolute():
        raise ValueError("Upload paths must be absolute.")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise ValueError(f"No such file to upload: {path}") from None
    if not resolved.is_file():
        raise ValueError(f"Not a file: {path}")
    size = resolved.stat().st_size
    if size > settings.BROWSER_MAX_UPLOAD_BYTES:
        raise ValueError(
            f"That file is {size} bytes; the permitted maximum is {settings.BROWSER_MAX_UPLOAD_BYTES} bytes."
        )
    roots = [USER_DATA_ROOT.resolve(strict=False), download_dir().resolve(strict=False)]
    if not any(root == resolved or root in resolved.parents for root in roots):
        raise ValueError(
            "Uploads are restricted to files inside the AirdropOS data directory "
            f"({USER_DATA_ROOT}). Copy the file there first."
        )
    return resolved


class BrowserController:
    """Manages Playwright lifecycle and isolated per-profile contexts."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._contexts: Dict[str, Any] = {}
        self._pages: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._key_locks: Dict[str, asyncio.Lock] = {}
        self._healthy = False
        self._last_error: Optional[str] = None
        # session_key -> "persistent" | "ephemeral" | "attached"
        self._modes: Dict[str, str] = {}
        # session_key -> Chrome user-data directory in use (persistent mode only)
        self._profile_dirs: Dict[str, str] = {}
        # session_key -> Browser reached over CDP (attach mode only)
        self._attached: Dict[str, Any] = {}
        # session_key -> capture recorders
        self._recorders: Dict[str, SessionRecorders] = {}
        # id(page) -> stable tab identifier
        self._tab_ids: Dict[int, str] = {}
        self._tab_seq = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> bool:
        """Start Playwright. Returns False rather than raising so the rest of
        the platform keeps working when browser automation is unavailable."""
        async with self._lock:
            if self._healthy and self._playwright is not None:
                return True
            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._healthy = True
                self._last_error = None
                logger.info(
                    "Playwright started (managed profiles: %s, headless default: %s)",
                    settings.BROWSER_PERSISTENT_PROFILES, settings.BROWSER_HEADLESS,
                )
                return True
            except Exception as exc:
                self._healthy = False
                self._last_error = _safe_error(exc)
                logger.error("Browser automation failed to start: %s", exc)
                if self._playwright:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None
                return False

    async def stop(self) -> None:
        """Close every context and stop Playwright. Chrome state is left on disk."""
        async with self._lock:
            for context in self._contexts.values():
                try:
                    await context.close()
                except Exception:
                    pass
            self._contexts.clear()
            self._pages.clear()
            self._modes.clear()
            self._profile_dirs.clear()
            self._recorders.clear()
            self._tab_ids.clear()
            # asyncio locks bind to the loop that first awaits them. Keeping them across a
            # restart would make the next (possibly different) loop refuse every session.
            self._key_locks.clear()
            for browser in list(self._attached.values()):
                try:
                    # Disconnect, do NOT close: this is somebody's own Chrome.
                    await browser.close()
                except Exception:
                    pass
            self._attached.clear()
            self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            self._healthy = False
            logger.info("Browser automation stopped")

    async def ensure_running(self) -> bool:
        """Restart the browser layer after a crash."""
        if self._healthy and self._playwright is not None:
            return True
        logger.warning("Browser not healthy (%s); attempting restart", self._last_error)
        return await self.start()

    # ------------------------------------------------------------------
    # Contexts
    # ------------------------------------------------------------------
    def _lock_for(self, session_key: str) -> asyncio.Lock:
        lock = self._key_locks.get(session_key)
        if lock is None:
            lock = self._key_locks[session_key] = asyncio.Lock()
        return lock

    def is_busy(self, session_key: str) -> bool:
        """True while a context/page for this key is being created or closed."""
        lock = self._key_locks.get(normalize_session_key(session_key))
        return bool(lock and lock.locked())

    def context_keys(self) -> List[str]:
        return list(self._contexts.keys())

    def session_mode(self, session_key: str) -> str:
        """How this session's browser was obtained: persistent, ephemeral or attached."""
        return self._modes.get(normalize_session_key(session_key), "none")

    def profile_dir(self, session_key: str) -> str:
        return self._profile_dirs.get(normalize_session_key(session_key), "")

    async def _safe_close(self, obj: Any) -> None:
        if obj is None:
            return
        try:
            await asyncio.wait_for(obj.close(), timeout=10)
        except Exception:
            pass

    async def _mark_browser_dead(self) -> None:
        """The browser process is gone: forget everything that pointed into it so the next
        call relaunches cleanly instead of failing forever on stale handles."""
        async with self._lock:
            self._healthy = False
            self._contexts.clear()
            self._pages.clear()
            self._modes.clear()
            self._profile_dirs.clear()
            self._recorders.clear()
            browser, playwright = self._browser, self._playwright
            self._browser = None
            self._playwright = None
        await self._safe_close(browser)
        if playwright is not None:
            try:
                await asyncio.wait_for(playwright.stop(), timeout=10)
            except Exception:
                pass
        logger.warning("Browser process is gone; it will be relaunched on the next request")

    async def _discard_context(self, session_key: str) -> None:
        """Drop a (possibly broken) context and its page WITHOUT saving state. Caller holds the key lock."""
        recorders = self._recorders.pop(session_key, None)
        if recorders is not None:
            for page in list(self._pages.values()) + []:
                recorders.diagnostics.forget(page)
                recorders.dialogs.forget(page)
                recorders.downloads.forget(page)
        await self._safe_close(self._pages.pop(session_key, None))
        await self._safe_close(self._contexts.pop(session_key, None))
        self._modes.pop(session_key, None)
        self._profile_dirs.pop(session_key, None)

    async def get_context(self, session_key: str, persistent: bool = True):
        """Get or create an isolated browser context for an account.

        Cookies and storage are partitioned per session_key so separate accounts
        never share authentication state.
        """
        session_key = normalize_session_key(session_key)
        async with self._lock_for(session_key):
            return await self._ensure_context(session_key, persistent)

    def _context_options(self, profile) -> Dict[str, Any]:
        width = (profile.viewport_width if profile and profile.viewport_width else settings.BROWSER_VIEWPORT_WIDTH)
        height = (profile.viewport_height if profile and profile.viewport_height else settings.BROWSER_VIEWPORT_HEIGHT)
        options: Dict[str, Any] = {
            "viewport": {"width": width, "height": height},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "ignore_https_errors": False,
            # Service workers can answer requests without the network layer ever seeing them,
            # which would skip the destination guard below.
            "service_workers": "block",
            "accept_downloads": True,
        }
        if profile and profile.locale:
            options["locale"] = profile.locale
        if profile and profile.timezone_id:
            options["timezone_id"] = profile.timezone_id
        return options

    async def _ensure_context(self, session_key: str, persistent: bool = True):
        """Create the context if missing. The caller holds this key's lock, so two callers can
        never both create one (the loser used to be overwritten and leaked)."""
        if session_key in self._contexts:
            return self._contexts[session_key]

        if hub.browser_sessions.is_operator_stopped(session_key):
            # An operator stop must not be undone by an agent call that was already in flight.
            logger.info("Not opening a context for operator-stopped session '%s'", session_key)
            return None

        if not await self.ensure_running():
            return None

        profile = profile_store.get_profile(session_key)
        if profile is not None and profile.launch_mode == "attach":
            return await self._ensure_attached(session_key, profile)
        if profile is not None and persistent and settings.BROWSER_PERSISTENT_PROFILES:
            return await self._ensure_persistent(session_key, profile)
        return await self._ensure_ephemeral(session_key, profile, persistent)

    # ------------------------------------------------------------------ persistent Chrome
    async def _ensure_persistent(self, session_key: str, profile):
        """Launch the INSTALLED Chrome against this profile's own user-data directory."""
        problems = profile_store.validate_profile_config(profile)
        if problems:
            self._last_error = "; ".join(problems)
            profile_store.record_use(session_key, error=self._last_error)
            logger.error("Profile '%s' is not launchable: %s", session_key, self._last_error)
            return None

        user_data_dir = profile.resolved_user_data_dir

        # Defence in depth, checked FIRST and before anything is even probed. Chrome
        # silently falls back to the OPERATOR'S OWN profile directory when
        # --user-data-dir is missing or points somewhere unusable, which is how a stray
        # Chrome window can appear with the wrong (or somebody else's) profile. Refuse
        # rather than risk it, before any lock scan touches the filesystem.
        if not user_data_dir or chrome.is_personal_user_data_dir(user_data_dir):
            detail = (
                "Refusing to launch Chrome against a real personal browser profile directory. "
                "AirdropOS only opens its own managed profile directories."
            )
            self._last_error = detail
            profile_store.record_use(session_key, error=detail)
            logger.error("Refusing to launch '%s': %s", session_key, detail)
            return None

        lock = chrome.profile_lock(user_data_dir)
        if lock["locked"]:
            detail = (
                f"This Chrome profile is already open in another process: {lock['reason']} "
                "Close that window (or reconnect to it) before starting a session here."
            )
            self._last_error = detail
            profile_store.record_use(session_key, error=detail)
            hub.browser_sessions.mark_error(session_key, "Chrome profile is already in use")
            logger.warning("Refusing to launch locked profile '%s': %s", session_key, lock["reason"])
            return None
        try:
            user_data_dir = chrome.ensure_profile_dir(user_data_dir)
        except OSError as exc:
            self._last_error = f"The Chrome profile directory could not be created: {exc}"
            profile_store.record_use(session_key, error=self._last_error)
            return None

        options = self._context_options(profile)
        executable = profile.resolved_executable() if profile.browser_type == "chrome" else ""
        if profile.browser_type == "chromium":
            options.pop("user_agent", None)  # Chromium reports its own build
        # Headless is honoured as asked: the NEW headless mode (--headless=new) loads
        # extensions too, and nothing is drawn on the operator's PC. The session is
        # watched and clicked through Live Broadcast (screenshot frames + input).
        headless = bool(profile.headless)

        args = chrome.build_chrome_args(
            headless=headless,
            extension_dirs=profile.extension_dirs(),
            extra=["--no-sandbox"] if settings.ENVIRONMENT != "production" else None,
        )
        if profile.locale:
            args.append(f"--lang={profile.locale}")
        # The timezone is applied by Playwright as a context override, not a Chrome flag:
        # Chrome derives it from the host OS, and overriding it at context level keeps the
        # ephemeral fallback and the persistent launch behaving identically.

        context = None
        try:
            context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                executable_path=executable or None,
                headless=headless,
                args=args,
                # Playwright's default --disable-extensions would keep every installed
                # extension (MetaMask etc.) from loading in this profile.
                ignore_default_args=list(chrome.PLAYWRIGHT_IGNORED_DEFAULT_ARGS),
                **options,
            )
            self._browser = context.browser
        except Exception as exc:
            text = str(exc).lower()
            if any(marker in text for marker in _PROFILE_LOCK_MARKERS):
                detail = (
                    "Chrome refused to open this profile because another Chrome process already has it. "
                    "Close that window, or use Reconnect if you want to work with the running one."
                )
                self._last_error = detail
                profile_store.record_use(session_key, error=detail, recovery="Launch refused; profile left untouched")
                hub.browser_sessions.mark_error(session_key, "Chrome profile is already in use")
                logger.warning("Chrome refused locked profile '%s': %s", session_key, _safe_error(exc))
                return None
            detail = (
                "Google Chrome could not be started for this profile. "
                + (f"Executable: {executable}. " if executable else "No Google Chrome executable was found. ")
                + _safe_error(exc)
            )
            self._last_error = detail
            profile_store.record_use(session_key, error=detail)
            logger.error("Chrome launch failed for '%s': %s", session_key, _safe_error(exc))
            return None

        if not await self._finish_context(session_key, context, profile, route_guard=True):
            await self._safe_close(context)
            return None
        self._modes[session_key] = "persistent"
        self._profile_dirs[session_key] = user_data_dir
        self._last_error = None
        profile_store.record_use(session_key, opened=True, recovery="")
        hub.browser_sessions.ensure(
            session_key, profile_id=profile.id, profile_name=profile.name
        )
        logger.info(
            "Launched persistent Chrome for profile '%s' (dir=%s, extensions=%s)",
            session_key, user_data_dir, profile.extension_dirs() or "installed-only",
        )
        return context

    # ------------------------------------------------------------------ existing Chrome
    async def _ensure_attached(self, session_key: str, profile):
        """Attach to an already-running Chrome over a loopback DevTools port.

        Only an endpoint the operator explicitly configured is used, it must be loopback,
        and Playwright does not intercept requests in the default context of a CDP browser
        -- so the destination guard is limited to navigations and that limitation is
        reported rather than hidden.
        """
        if not settings.BROWSER_ALLOW_ATTACH:
            self._last_error = "Connecting to an existing Chrome is disabled (BROWSER_ALLOW_ATTACH=false)."
            return None
        if not profile.debugger_port:
            self._last_error = (
                "This profile has no DevTools port configured. Start Chrome with "
                "--remote-debugging-port=<port> and set that port on the profile."
            )
            return None
        try:
            endpoint = chrome.attach_endpoint(profile.debugger_host, profile.debugger_port)
        except chrome.ChromeError as exc:
            self._last_error = str(exc)
            return None

        try:
            existing = self._attached.get(session_key)
            if existing is not None:
                try:
                    if existing.is_connected() and existing.contexts:
                        return self._contexts.get(session_key) or existing.contexts[0]
                except Exception:
                    self._attached.pop(session_key, None)
            browser = await self._playwright.chromium.connect_over_cdp(endpoint, timeout=20000)
            self._attached[session_key] = browser
        except Exception as exc:
            detail = (
                f"Could not connect to the existing Chrome at {endpoint}. It must already be running with "
                f"--remote-debugging-port={profile.debugger_port}. {_safe_error(exc)}"
            )
            self._last_error = detail
            profile_store.record_use(session_key, error=detail)
            logger.warning("CDP attach failed for '%s': %s", session_key, _safe_error(exc))
            return None
        if not browser.contexts:
            self._last_error = "The connected Chrome exposed no browser context."
            return None
        context = browser.contexts[0]
        ok = await self._finish_context(session_key, context, profile, route_guard=False)
        if not ok:
            return None
        self._modes[session_key] = "attached"
        self._last_error = None
        profile_store.record_use(session_key, opened=True, recovery="")
        logger.info("Attached to the existing Chrome at %s for profile '%s'", endpoint, session_key)
        return context

    # ------------------------------------------------------------------ ephemeral
    async def _ensure_ephemeral(self, session_key: str, profile, persistent: bool = True):
        """The pre-existing behaviour: a throwaway Chromium context with no persistence.

        Used only when persistent profiles are off or the profile has no usable Chrome.
        The mode is recorded so nothing downstream can mistake it for a managed profile.
        """
        context = None
        try:
            if not await self._launch_shared_chromium():
                return None
            options = self._context_options(profile)
            if profile is None or profile.browser_type == "chromium":
                options.pop("user_agent", None)
            if persistent and profile is not None:
                # Cookies/local storage survive restarts (encrypted at rest, see profiles.py).
                # Only a real profile ever has saved state, and it is only ever loaded into ITS context.
                saved = profile_store.load_state(session_key)
                if saved:
                    options["storage_state"] = saved
            context = await self._browser.new_context(**options)
        except Exception as exc:
            self._last_error = _safe_error(exc)
            logger.error("Failed to create an ephemeral context for '%s': %s", session_key, _safe_error(exc))
            return None
        if not await self._finish_context(session_key, context, profile, route_guard=True):
            await self._safe_close(context)
            return None
        self._modes[session_key] = "ephemeral"
        self._last_error = None
        logger.info("Created an ephemeral browser context for session '%s' (no persistence)", session_key)
        return context

    async def _launch_shared_chromium(self) -> bool:
        """One Playwright-managed Chromium shared by every ephemeral context."""
        async with self._lock:
            if self._browser is not None:
                try:
                    if self._browser.is_connected():
                        return True
                except Exception:
                    self._browser = None
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=settings.BROWSER_HEADLESS,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
                return True
            except Exception as exc:
                self._healthy = False
                self._last_error = _safe_error(exc)
                logger.error("Could not launch the Playwright Chromium: %s", self._last_error)
                return False

    async def _finish_context(self, session_key: str, context, profile, *, route_guard: bool) -> bool:
        """Apply timeouts, the destination guard and the capture recorders to a new context."""
        try:
            context.set_default_timeout(settings.BROWSER_ACTION_TIMEOUT_MS)
            context.set_default_navigation_timeout(settings.BROWSER_NAVIGATION_TIMEOUT_MS)
        except Exception:
            pass
        if route_guard:
            # Every request (redirect hops, iframes, scripts, fetches) is checked, not just the
            # first URL the agent asked for.
            try:
                await context.route("**/*", self._make_route_guard(session_key))
            except Exception as exc:
                logger.error("Could not install the route guard for '%s': %s", session_key, _safe_error(exc))
                return False
            # context.route() never sees WebSocket handshakes, so a page could otherwise open
            # ws://internal-host/ and bypass the guard above. Fail closed if this Playwright
            # build cannot intercept them (route_web_socket needs Playwright >= 1.48).
            route_ws = getattr(context, "route_web_socket", None)
            if route_ws is None:
                logger.error(
                    "Playwright >= 1.48 is required to guard WebSocket connections; refusing to run unguarded."
                )
                return False
            try:
                await route_ws("**/*", self._make_ws_guard(session_key))
            except Exception as exc:
                logger.error("Could not install the WebSocket guard for '%s': %s", session_key, _safe_error(exc))
                return False
        recorders = SessionRecorders()
        try:
            for existing in list(context.pages):
                recorders.diagnostics.attach(existing)
                recorders.dialogs.attach(existing)
                recorders.downloads.attach(existing)
            context.on("page", self._on_new_page(session_key, recorders))
        except Exception as exc:
            logger.debug("Could not attach page recorders for '%s': %s", session_key, type(exc).__name__)
        self._contexts[session_key] = context
        self._recorders[session_key] = recorders
        # A freshly launched Chrome already has a tab. Adopt it so the session is usable
        # immediately instead of only after the first get_page() call.
        if session_key not in self._pages:
            try:
                existing = [p for p in context.pages if not p.is_closed()]
            except Exception:
                existing = []
            if existing:
                self._pages[session_key] = existing[0]
                self._tab_id(existing[0])
        if profile is not None:
            hub.browser_sessions.ensure(
                session_key, profile_id=profile.id, profile_name=profile.name
            )
        return True

    def _on_new_page(self, session_key: str, recorders: SessionRecorders):
        def handler(page) -> None:
            try:
                recorders.diagnostics.attach(page)
                recorders.dialogs.attach(page)
                recorders.downloads.attach(page)
                self._tab_id(page)
            except Exception:
                pass
        return handler

    def recorders(self, session_key: str) -> Optional[SessionRecorders]:
        return self._recorders.get(normalize_session_key(session_key))

    # ------------------------------------------------------------------ route guards
    def _make_route_guard(self, session_key: str):
        async def guard(route) -> None:
            request = route.request
            url = request.url
            scheme = urlsplit(url).scheme.lower()
            if scheme in ("data", "blob", "about"):
                await self._route_continue(route)  # no network destination
                return
            try:
                reason = await destination_block_reason(url)
            except Exception:
                reason = "the destination could not be verified"
            if reason is None:
                await self._route_continue(route)
                return
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass  # the page went away while we were deciding
            try:
                is_nav = bool(request.is_navigation_request())
            except Exception:
                is_nav = False
            if is_nav:  # one event per blocked page load; blocked subresources would flood the log
                hub.events.emit(
                    integration="browser",
                    session_id=f"browser:{session_key}",
                    type="browser.blocked",
                    status="blocked",
                    message=f"Blocked a request to {safe_url(url)}: {reason}",
                )
            logger.warning("Blocked browser request for '%s' to %s: %s", session_key, safe_url(url), reason)

        return guard

    def _make_ws_guard(self, session_key: str):
        async def guard(ws) -> None:
            url = str(getattr(ws, "url", "") or "")
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            if scheme not in ("ws", "wss"):
                reason: Optional[str] = "only ws:// and wss:// WebSockets are allowed"
            else:
                # The same destination policy as HTTP: ws -> http, wss -> https.
                http_url = ("https" if scheme == "wss" else "http") + url[len(scheme):]
                try:
                    reason = await destination_block_reason(http_url)
                except Exception:
                    reason = "the destination could not be verified"
            if reason is None:
                try:
                    ws.connect_to_server()  # allowed: pass the connection through unchanged
                except Exception:
                    pass
                return
            try:
                await ws.close(code=1008, reason="blocked by policy")
            except Exception:
                pass  # the page went away while we were deciding
            logger.warning("Blocked WebSocket for '%s' to %s: %s", session_key, safe_url(url), reason)

        return guard

    @staticmethod
    async def _route_continue(route) -> None:
        try:
            await route.continue_()
        except Exception:
            pass  # request already cancelled (page closed or navigated away)

    async def get_page(self, session_key: str):
        """Get or create a page within the session's context. Reuses open tabs.

        For a persistent Chrome profile the context always has at least one tab, so the
        existing one is adopted rather than a blank second tab being created.
        """
        session_key = normalize_session_key(session_key)
        async with self._lock_for(session_key):
            page = self._pages.get(session_key)
            if page is not None and not page.is_closed():
                return page
            self._pages.pop(session_key, None)
            for attempt in (1, 2):
                context = await self._ensure_context(session_key)
                if context is None:
                    return None
                try:
                    page = await self._new_or_adopt_page(context)
                    if page is None:
                        continue
                    self._pages[session_key] = page
                    return page
                except Exception as exc:
                    logger.error("Failed to open page for '%s' (attempt %s): %s", session_key, attempt, _safe_error(exc))
                    # A closed/crashed context fails forever and was kept as a stale entry; drop it
                    # so the second attempt (and later calls) start from a fresh one.
                    await self._discard_context(session_key)
            return None

    async def show_new_tab(self, session_key: str) -> Dict[str, Any]:
        """Turn the blank first tab of a freshly opened managed Chrome into the New Tab page.

        Playwright starts a persistent Chrome on ``about:blank``. This reuses that one
        tab (it never opens another and never closes anything) and navigates it to
        Chrome's own New Tab page. A tab that already shows something is left alone, so
        calling this on a reused or already-navigated session is a no-op. A failure is
        reported but never fatal: the profile is still open and usable.

        Not exposed to the agent: ``chrome://newtab`` is not on the agent's allowlist.
        """
        session_key = normalize_session_key(session_key)
        if self._modes.get(session_key) != "persistent":
            return {"ok": True, "changed": False, "reason": "not_managed_chrome"}
        context = self._contexts.get(session_key)
        if context is None:
            return {"ok": False, "changed": False, "reason": "session_missing"}
        page = self._pages.get(session_key)
        try:
            if page is None or page.is_closed():
                page = next((p for p in context.pages if not p.is_closed()), None)
            if page is None:
                return {"ok": False, "changed": False, "reason": "no_page"}
            if not chrome.is_blank_page_url(page.url):
                return {"ok": True, "changed": False, "url": safe_url(page.url)}
        except Exception as exc:
            return {"ok": False, "changed": False, "reason": "page_unavailable", "detail": _safe_error(exc)}
        try:
            # "commit": the New Tab page redirects internally (and on a Google profile
            # loads its own content), so waiting for domcontentloaded can time out.
            await page.goto(chrome.NEW_TAB_URL, wait_until="commit", timeout=settings.BROWSER_NAVIGATION_TIMEOUT_MS)
        except Exception as exc:
            logger.warning("Could not show the New Tab page for '%s': %s", session_key, type(exc).__name__)
            return {"ok": False, "changed": False, "reason": "newtab_failed", "detail": _safe_error(exc)}
        self._pages[session_key] = page
        try:
            await page.bring_to_front()
        except Exception:
            pass
        return {"ok": True, "changed": True, "url": safe_url(page.url)}

    async def _new_or_adopt_page(self, context):
        """Reuse the browser's existing first tab, or open one. Returns None if neither works."""
        try:
            existing = [p for p in context.pages if not p.is_closed()]
        except Exception:
            existing = []
        if existing:
            return existing[0]
        return await context.new_page()

    async def close_session(self, session_key: str) -> None:
        """Stop this session's browser. Chrome's own profile data is never touched."""
        session_key = normalize_session_key(session_key)
        # Serialised with creation, so a context being created right now cannot slip in
        # after this close and be left running with nothing tracking it.
        async with self._lock_for(session_key):
            await self.save_state(session_key)
            context = self._contexts.get(session_key)
            mode = self._modes.get(session_key)
            self._pages.pop(session_key, None)
            self._contexts.pop(session_key, None)
            self._modes.pop(session_key, None)
            self._recorders.pop(session_key, None)
            if mode == "attached":
                # Disconnect from somebody's own Chrome. Closing it would take over their browser.
                browser = self._attached.pop(session_key, None)
                await self._safe_close(browser)
            else:
                user_data_dir = self._profile_dirs.get(session_key, "")
                await self._release_chrome(context, user_data_dir if mode == "persistent" else "")
            self._profile_dirs.pop(session_key, None)

    async def _release_chrome(self, context, user_data_dir: str) -> None:
        """Close a managed Chrome and make sure it is really gone from the profile.

        An INSTALLED Chrome keeps running after its last window closes (background mode),
        and a lingering process keeps the profile directory locked. That would make the
        very next task fail with a lock conflict, so the process tree AirdropOS itself
        started for THIS profile is waited for and then terminated. Only processes whose
        command line names this exact managed directory are touched.

        The context is closed, not ``context.browser``: for a persistent context Chrome and
        its context are one unit and closing the browser separately tears down the shared
        Playwright connection with it.
        """
        await self._safe_close(context)
        if not user_data_dir:
            return
        if await self._wait_for_chrome_exit(user_data_dir, timeout=5.0):
            return
        logger.warning("Terminating the Chrome processes left holding profile '%s'", user_data_dir)
        self._terminate_chrome(user_data_dir)
        await self._wait_for_chrome_exit(user_data_dir, timeout=10.0, kill=True)

    async def _wait_for_chrome_exit(self, user_data_dir: str, timeout: float = 5.0,
                                    kill: bool = False) -> bool:
        """Poll until no Chrome process still holds this profile. True when it is clear."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = chrome.chrome_processes_for(user_data_dir)
            if not remaining:
                return True
            if time.monotonic() >= deadline:
                if not kill:
                    return False
                for entry in remaining:
                    self._terminate_pid(entry.get("pid"))
                deadline = time.monotonic() + 3.0
                kill = False  # one kill round only; never loop forever
            try:
                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise

    @staticmethod
    def _terminate_chrome(user_data_dir: str) -> None:
        for entry in chrome.chrome_processes_for(user_data_dir):
            BrowserController._terminate_pid(entry.get("pid"))

    @staticmethod
    def _terminate_pid(pid: Optional[int]) -> None:
        if not pid:
            return
        try:
            import psutil
        except Exception:
            return  # psutil is optional; without it we simply wait longer next time
        try:
            process = psutil.Process(int(pid))
            process.terminate()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Tabs
    # ------------------------------------------------------------------
    def _tab_id(self, page) -> str:
        """A stable identifier for a tab, kept for as long as the page object exists."""
        key = id(page)
        tab_id = self._tab_ids.get(key)
        if tab_id is None:
            self._tab_seq += 1
            tab_id = f"tab{self._tab_seq}"
            self._tab_ids[key] = tab_id
        return tab_id

    async def list_tabs(self, session_key: str) -> Dict[str, Any]:
        """Every open tab of this session, with stable ids and which one is active."""
        session_key = normalize_session_key(session_key)
        context = self._contexts.get(session_key)
        if context is None:
            return {"ok": False, "reason": "session_missing", "tabs": [], "active_tab_id": None}
        active = self._pages.get(session_key)
        tabs: List[Dict[str, Any]] = []
        try:
            pages = list(context.pages)
        except Exception as exc:
            return {"ok": False, "reason": "browser_unavailable", "detail": _safe_error(exc), "tabs": []}
        for index, page in enumerate(pages):
            closed = False
            try:
                closed = page.is_closed()
            except Exception:
                closed = True
            tabs.append({
                "tab_id": self._tab_id(page),
                "index": index,
                "url": safe_url(getattr(page, "url", "")),
                "raw_scheme": (getattr(page, "url", "") or "").split(":", 1)[0][:20],
                "active": page is active,
                "closed": closed,
                "title": "" if closed else await self._page_title_safe(page),
            })
        active_id = None
        if active is not None:
            try:
                if not active.is_closed():
                    active_id = self._tab_id(active)
            except Exception:
                active_id = None
        return {
            "ok": True,
            "tabs": tabs,
            "active_tab_id": active_id,
            "tab_count": len(tabs),
            "max_tabs": settings.BROWSER_MAX_TABS,
        }

    async def _page_title_safe(self, page) -> str:
        try:
            title = await page.title()
        except Exception:
            return ""
        return redact_text(title)[:160]

    async def _pick_page(
        self,
        session_key: str,
        *,
        index: Optional[int] = None,
        tab_id: Optional[str] = None,
        url_contains: str = "",
        offset: int = 0,
    ):
        """Find the tab a call refers to, or (None, available) so the caller can say what exists."""
        context = self._contexts.get(session_key)
        if context is None:
            return None, []
        try:
            pages = [p for p in context.pages if not p.is_closed()]
        except Exception:
            pages = []
        if tab_id:
            for page in pages:
                if self._tab_id(page) == tab_id:
                    return page, pages
            return None, pages
        if url_contains:
            needle = str(url_contains).lower()
            matches = [p for p in pages if needle in (getattr(p, "url", "") or "").lower()]
            if matches:
                clamped = max(0, min(offset, len(matches) - 1))
                return matches[clamped], pages
            return None, pages
        if index is not None:
            try:
                position = int(index)
            except (TypeError, ValueError):
                return None, pages
            if -len(pages) <= position < len(pages):
                return pages[position], pages
            return None, pages
        return None, pages

    async def new_tab(self, session_key: str, url: str = "") -> Dict[str, Any]:
        """Open a new tab and make it the active one."""
        session_key = normalize_session_key(session_key)
        context = self._contexts.get(session_key)
        if context is None:
            return {"ok": False, "reason": "session_missing", "detail": "No browser is open for this session."}
        try:
            open_tabs = len([p for p in context.pages if not p.is_closed()])
        except Exception:
            open_tabs = 0
        if open_tabs >= settings.BROWSER_MAX_TABS:
            return {
                "ok": False,
                "reason": "tab_limit",
                "detail": f"This session already has {open_tabs} tabs (limit {settings.BROWSER_MAX_TABS}).",
            }
        try:
            page = await context.new_page()
        except Exception as exc:
            return {"ok": False, "reason": "tab_failed", "detail": _safe_error(exc)}
        self._tab_id(page)
        self._pages[session_key] = page
        if url:
            result = await self.navigate(session_key, url)
            if not result.get("ok"):
                return {"ok": False, "reason": result.get("reason", "navigation_failed"),
                        "detail": result.get("detail", ""), "tab_opened": True}
        try:
            await page.bring_to_front()
        except Exception:
            pass
        await self._tabs_touch(session_key, page)
        return {"ok": True, "tab_id": self._tab_id(page), "url": safe_url(page.url),
                "title": await self._page_title_safe(page)}

    async def switch_tab(
        self,
        session_key: str,
        *,
        index: Optional[int] = None,
        tab_id: Optional[str] = None,
        url_contains: str = "",
        offset: int = 0,
        step: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Make another tab active: by id, position, URL, or next/previous step."""
        session_key = normalize_session_key(session_key)
        if step is not None:
            listing = await self.list_tabs(session_key)
            live = [t for t in listing.get("tabs", []) if not t["closed"]]
            if not live:
                return {"ok": False, "reason": "no_tabs", "detail": "This session has no open tabs."}
            current = next((i for i, t in enumerate(live) if t.get("active")), 0)
            position = (current + int(step)) % len(live)
            index = live[position]["index"]
        page, pages = await self._pick_page(
            session_key, index=index, tab_id=tab_id, url_contains=url_contains, offset=offset
        )
        if page is None:
            listing = await self.list_tabs(session_key)
            available = ", ".join(
                f"{t['tab_id']}({t['index']}) {t['url'][:60]}" for t in listing.get("tabs", []) if not t["closed"]
            ) or "none"
            return {
                "ok": False,
                "reason": "tab_not_found",
                "detail": f"No matching tab. Open tabs: {available}.",
            }
        self._pages[session_key] = page
        try:
            await page.bring_to_front()
        except Exception:
            pass
        await self._tabs_touch(session_key, page)
        return {"ok": True, "tab_id": self._tab_id(page), "url": safe_url(page.url),
                "title": await self._page_title_safe(page), "tab_count": len(pages)}

    async def close_tab(
        self,
        session_key: str,
        *,
        index: Optional[int] = None,
        tab_id: Optional[str] = None,
        url_contains: str = "",
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Close a tab and hand the session to another one that is still open."""
        session_key = normalize_session_key(session_key)
        context = self._contexts.get(session_key)
        if context is None:
            return {"ok": False, "reason": "session_missing"}
        listing = await self.list_tabs(session_key)
        live = [t for t in listing.get("tabs", []) if not t["closed"]]
        if len(live) <= 1 and not index and not tab_id and not url_contains:
            return {
                "ok": False,
                "reason": "last_tab",
                "detail": "This is the session's only tab. Close the whole browser session instead.",
            }
        page, _pages = await self._pick_page(
            session_key, index=index, tab_id=tab_id, url_contains=url_contains, offset=offset
        )
        if page is None:
            return {"ok": False, "reason": "tab_not_found", "detail": "No matching open tab."}
        closing_id = self._tab_id(page)
        was_active = self._pages.get(session_key) is page
        remaining: List[Any] = []
        await self._safe_close(page)
        self._tab_ids.pop(id(page), None)
        if was_active:
            try:
                remaining = [p for p in context.pages if not p.is_closed()]
            except Exception:
                remaining = []
            self._pages[session_key] = remaining[0] if remaining else None
            if remaining:
                try:
                    await remaining[0].bring_to_front()
                except Exception:
                    pass
        await self._tabs_touch(session_key, self._pages.get(session_key))
        return {"ok": True, "closed_tab_id": closing_id, "remaining_tabs": len(remaining) if was_active else None}

    async def _tabs_touch(self, session_key: str, page) -> None:
        if page is None:
            return
        try:
            hub.browser_sessions.touch(session_key, url=page.url, title=await self._page_title_safe(page))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Navigation & observation
    # ------------------------------------------------------------------
    async def navigate(self, session_key: str, url: str) -> Dict[str, Any]:
        return await self._navigate(session_key, url, internal=False)

    async def navigate_internal(self, session_key: str, url: str) -> Dict[str, Any]:
        """Open a browser-internal page (chrome://extensions, chrome://version).

        Refused unless it is on the allowlist in ``chrome.internal_page_reason``. These
        pages carry no network destination, so the SSRF guard is not involved; the
        allowlist is the whole control, and it is deliberately tiny.
        """
        reason = chrome.internal_page_reason(url)
        if reason:
            return {"ok": False, "reason": "blocked_internal_page", "detail": reason}
        return await self._navigate(session_key, url, internal=True)

    async def _navigate(self, session_key: str, url: str, *, internal: bool = False) -> Dict[str, Any]:
        session_key = normalize_session_key(session_key)
        if hub.browser_sessions.is_operator_stopped(session_key):
            return {"ok": False, "reason": "operator_stopped", "detail": "This session was stopped by an operator."}
        if internal:
            blocked = None
        else:
            blocked = await destination_block_reason(url)
            if blocked:
                # Not a session fault: nothing is marked failed, no browser is opened for it.
                return {"ok": False, "reason": "blocked_url", "detail": blocked}

        page = await self.get_page(session_key)
        if page is None:
            hub.browser_sessions.mark_error(session_key, "Browser unavailable")
            return {"ok": False, "reason": "browser_unavailable", "detail": self._last_error}

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=settings.BROWSER_NAVIGATION_TIMEOUT_MS)
        except Exception as exc:
            text = str(exc).lower()
            if "err_blocked_by_client" in text:
                # Our own destination guard refused a redirect hop or the page itself: not retryable,
                # not a session fault.
                return {"ok": False, "reason": "blocked_url", "detail": "the page or a redirect led to an address that is not allowed"}
            if not (page.is_closed() or any(marker in text for marker in _DEAD_MARKERS)):
                # An ordinary failure (DNS error, timeout, refused connection). The browser and its
                # login state are fine, so keep them instead of tearing the whole context down.
                logger.warning("Navigation to %s failed: %s", safe_url(url), type(exc).__name__)
                hub.browser_sessions.mark_error(session_key, "Navigation failed")
                return {"ok": False, "reason": "navigation_failed", "detail": _safe_error(exc, url)}
            # The page/context died: drop it and try once more on a fresh one. The Chrome profile
            # itself is untouched: it is on disk and Chrome reloads it.
            logger.warning("Page for '%s' died during navigation (%s). Recreating context.", session_key, type(exc).__name__)
            await self.close_session(session_key)
            page = await self.get_page(session_key)
            if page is None:
                hub.browser_sessions.mark_error(session_key, "Browser unavailable")
                return {"ok": False, "reason": "browser_unavailable", "detail": self._last_error}
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=settings.BROWSER_NAVIGATION_TIMEOUT_MS)
            except Exception as exc2:
                hub.browser_sessions.mark_error(session_key, "Navigation failed")
                return {"ok": False, "reason": "navigation_failed", "detail": _safe_error(exc2, url)}

        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # Networkidle is best-effort; many sites never reach it.

        # A late agent call must not report or persist anything after an operator stopped the session.
        if hub.browser_sessions.is_operator_stopped(session_key):
            return {"ok": False, "reason": "operator_stopped", "detail": "This session was stopped by an operator."}
        # Defence in depth: whatever the page ended up on (after redirects) is judged again.
        if not internal and url_block_reason(page.url) and page.url not in ("about:blank", ""):
            try:
                await page.goto("about:blank")
            except Exception:
                pass
            return {"ok": False, "reason": "blocked_url", "detail": "the page redirected to an address that is not allowed"}

        if internal:
            # A chrome:// page has no useful body text and a shadow-DOM UI; report what it is.
            return {
                "ok": True,
                "observation": PageObservation(
                    url=page.url,
                    title=await self._page_title_safe(page),
                    text_excerpt="",
                    requires_human=False,
                ),
                "internal_page": True,
            }

        observation = await self.inspect(page)
        observation.http_status = response.status if response else None
        hub.browser_sessions.touch(session_key, url=page.url, title=observation.title)
        hub.events.emit(
            integration="browser",
            session_id=f"browser:{session_key}",
            type="browser.navigated",
            status="ok",
            message=f"Navigated to {safe_url(page.url)}",
        )
        await self.save_state(session_key)
        return {"ok": True, "observation": observation}

    async def history(self, session_key: str, action: str) -> Dict[str, Any]:
        """Go back, forward or reload. Reports the URL actually landed on.

        ``back``/``forward`` return ``ok: False`` with reason ``no_history`` at the end of
        the stack instead of pretending the page did not move.
        """
        session_key = normalize_session_key(session_key)
        action = str(action or "reload").strip().lower()
        if action not in ("back", "forward", "reload"):
            return {"ok": False, "reason": "invalid_action", "detail": "action must be back, forward or reload."}
        page = await self.get_page(session_key)
        if page is None:
            return {"ok": False, "reason": "browser_unavailable", "detail": self._last_error}
        url_before = safe_url(page.url)
        try:
            if action == "back":
                response = await page.go_back(wait_until="domcontentloaded",
                                              timeout=settings.BROWSER_NAVIGATION_TIMEOUT_MS)
            elif action == "forward":
                response = await page.go_forward(wait_until="domcontentloaded",
                                                timeout=settings.BROWSER_NAVIGATION_TIMEOUT_MS)
            else:
                response = await page.reload(wait_until="domcontentloaded",
                                             timeout=settings.BROWSER_NAVIGATION_TIMEOUT_MS)
        except Exception as exc:
            return {"ok": False, "reason": "navigation_failed", "detail": _safe_error(exc)}
        if action in ("back", "forward") and response is None:
            return {
                "ok": False,
                "reason": "no_history",
                "detail": f"There is nothing to go {'back' if action == 'back' else 'forward'} to; still on {url_before}.",
                "url": url_before,
            }
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        title = await self._page_title_safe(page)
        hub.browser_sessions.touch(session_key, url=page.url, title=title)
        hub.events.emit(
            integration="browser",
            session_id=f"browser:{session_key}",
            type=f"browser.{action}",
            status="ok",
            message=f"{action.capitalize()} -> {safe_url(page.url)}",
        )
        return {
            "ok": True,
            "action": action,
            "url_before": url_before,
            "url": safe_url(page.url),
            "title": title,
            "redirected": safe_url(page.url) != url_before,
        }


    async def inspect(self, page) -> PageObservation:
        """Extract a structured view of the current page."""
        try:
            content = await page.content()
        except Exception as exc:
            return PageObservation(
                url=page.url, title="", text_excerpt=f"Inspection failed: {_safe_error(exc)}",
                requires_human=True, human_reason="Page could not be read",
            )

        text = await page.inner_text("body") if await page.query_selector("body") else ""

        forms = []
        for form in await page.query_selector_all("form"):
            inputs = await form.query_selector_all("input, select, textarea")
            fields = []
            for field_el in inputs:
                try:
                    fields.append(
                        {
                            "name": await field_el.get_attribute("name"),
                            "type": await field_el.get_attribute("type"),
                            "placeholder": await field_el.get_attribute("placeholder"),
                            "required": (await field_el.get_attribute("required")) is not None,
                        }
                    )
                except Exception:
                    continue
            forms.append({"action": await form.get_attribute("action"), "fields": fields})

        buttons = []
        for btn in await page.query_selector_all("button, [role=button], [role=link], [role=menuitem], [role=tab], input[type=submit], input[type=button]"):
            label = (await btn.inner_text()) or (await btn.get_attribute("value")) or ""
            if label.strip():
                buttons.append(label.strip()[:80])

        links = []
        for link in await page.query_selector_all("a[href]"):
            href = await link.get_attribute("href")
            if href:
                links.append(href[:500])

        combined = f"{await page.title()} {text[:3000]}".lower()
        requires_human, reason = self._detect_human_gate(combined)

        return PageObservation(
            url=page.url,
            title=await page.title(),
            text_excerpt=text[:6000],
            forms=forms,
            buttons=buttons[:50],
            links=links[:100],
            requires_human=requires_human,
            human_reason=reason,
        )

    @staticmethod
    def _detect_human_gate(text: str) -> tuple[bool, Optional[str]]:
        """Never attempt to bypass these; report them instead."""
        for pattern in CAPTCHA_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True, "CAPTCHA or anti-bot challenge detected. Manual completion required."
        for pattern in MFA_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True, "Multi-factor authentication required. Manual completion required."
        for pattern in SESSION_EXPIRED_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True, "Session expired or login required."
        return False, None

    async def screenshot(self, session_key: str, label: str = "evidence", *, full_page: bool = True) -> Optional[str]:
        session_key = normalize_session_key(session_key)
        page = await self.get_page(session_key)
        if page is None:
            return None
        out_dir = USER_DATA_ROOT / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Filename only: a label such as "../../x" must not be able to leave the screenshots
        # directory. The caller keeps the raw label for its own records.
        safe_label = re.sub(r"[^A-Za-z0-9._-]", "_", label or "evidence")[:100].lstrip(".") or "evidence"
        path = out_dir / f"{safe_label}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.png"
        try:
            await page.screenshot(path=str(path), full_page=full_page, timeout=20000)
            return str(path)
        except Exception as exc:
            logger.error("Screenshot failed for '%s': %s", session_key, _safe_error(exc))
            return None

    # ------------------------------------------------------------------
    # Live view support (read-only; never creates a page)
    # ------------------------------------------------------------------
    def peek_page(self, session_key: str):
        """The existing open page of a session, or None. Never opens anything."""
        page = self._pages.get(normalize_session_key(session_key))
        if page is not None and not page.is_closed():
            return page
        return None

    def open_keys(self) -> List[str]:
        return [k for k, p in self._pages.items() if not p.is_closed()]

    def viewport(self, session_key: str) -> Optional[Dict[str, int]]:
        page = self.peek_page(session_key)
        if page is None:
            return None
        vp = page.viewport_size or {
            "width": settings.BROWSER_VIEWPORT_WIDTH,
            "height": settings.BROWSER_VIEWPORT_HEIGHT,
        }
        return {"width": int(vp["width"]), "height": int(vp["height"])}

    async def capture_jpeg(self, session_key: str, quality: int) -> Optional[Dict[str, Any]]:
        """One JPEG of the CURRENT viewport in CSS pixels (so click coordinates map 1:1)."""
        page = self.peek_page(session_key)
        if page is None:
            return None
        vp = self.viewport(session_key)
        try:
            data = await page.screenshot(type="jpeg", quality=quality, scale="css", timeout=6000)
        except Exception as exc:
            logger.debug("Frame capture failed for '%s': %s", session_key, exc)
            return None
        return {"data": data, "width": vp["width"], "height": vp["height"]}

    async def save_state(self, session_key: str) -> None:
        """Persist cookies/local storage of a session (best effort).

        Only the EPHEMERAL fallback context needs this. A persistent Chrome profile keeps
        its own cookies on disk, so Chrome has already saved them by the time this runs
        and copying them into ``state.enc`` would duplicate credentials for no benefit.
        """
        session_key = normalize_session_key(session_key)
        context = self._contexts.get(session_key)
        if context is None or self.session_mode(session_key) == "persistent":
            return
        if profile_store.get_profile(session_key) is None:
            return  # state is only ever kept for real profiles
        try:
            state = await context.storage_state()
            await asyncio.get_running_loop().run_in_executor(None, profile_store.save_state, session_key, state)
        except Exception as exc:
            logger.debug("Could not save state for '%s': %s", session_key, exc)

    # ------------------------------------------------------------------
    # Profile lifecycle (used by the API's "Open Browser" / "Stop" / "Reconnect")
    # ------------------------------------------------------------------
    async def open_profile(self, profile_id: str) -> Dict[str, Any]:
        """Start (or reuse) the browser for a profile and report what actually happened.

        Never substitutes another profile: if this profile cannot be opened the result
        says so and nothing else is started.
        """
        profile = profile_store.get_profile(profile_id)
        if profile is None:
            return {"ok": False, "reason": "no_profile", "detail": "That browser profile does not exist."}
        session_key = profile.id
        if hub.browser_sessions.is_operator_stopped(session_key):
            # Only the operator may undo their own stop, and that is what the
            # BrowserProvider's create() path is for.
            return {
                "ok": False,
                "reason": "operator_stopped",
                "detail": "This session was stopped by an operator. Use Reconnect to start it again.",
            }
        async with self._lock_for(session_key):
            if session_key in self._contexts and self.peek_page(session_key) is not None:
                status = await self.profile_status(profile_id)
                status["reused"] = True
                return {"ok": True, **status}
            context = await self._ensure_context(session_key)
            if context is not None and not profile.start_url:
                # A profile with a start URL is navigated there by the caller instead.
                await self.show_new_tab(session_key)
        if context is None:
            return {
                "ok": False,
                "reason": "launch_failed",
                "detail": self._last_error or "The browser could not be started for this profile.",
            }
        status = await self.profile_status(profile_id)
        if not status.get("running"):
            return {"ok": False, "reason": "launch_failed",
                    "detail": status.get("last_error") or "Chrome started but no page is available."}
        return {"ok": True, **status}

    async def reconnect_profile(self, profile_id: str) -> Dict[str, Any]:
        """Re-establish a connection to a profile's browser.

        If this controller still owns it, that is a health check. If not, it starts it
        again from the same saved configuration (attach mode reconnects to the running
        Chrome). The profile is never swapped for another one.
        """
        profile = profile_store.get_profile(profile_id)
        if profile is None:
            return {"ok": False, "reason": "no_profile", "detail": "That browser profile does not exist."}
        hub.browser_sessions.restart(profile.id)  # an operator action may undo an operator stop
        context = self._contexts.get(profile.id)
        if context is not None:
            try:
                healthy = bool(context.pages)
            except Exception:
                healthy = False
            if healthy:
                status = await self.profile_status(profile_id)
                profile_store.record_use(profile_id, recovery="Reconnected to the running browser")
                return {"ok": True, "reconnected_to": "running_session", **status}
        result = await self.open_profile(profile_id)
        if result.get("ok"):
            profile_store.record_use(profile_id, recovery="Restarted from the saved profile configuration")
        return result

    async def profile_status(self, profile_id: str) -> Dict[str, Any]:
        """The real state of a profile's browser: mode, tabs, extensions, errors."""
        profile = profile_store.get_profile(profile_id)
        if profile is None:
            return {"ok": False, "reason": "no_profile", "running": False}
        session_key = profile.id
        mode = self.session_mode(session_key)
        page = self.peek_page(session_key)
        tabs: List[Dict[str, Any]] = []
        if mode != "none":
            try:
                listing = await self.list_tabs(session_key)
                tabs = listing.get("tabs", [])
            except Exception:
                tabs = []
        record = hub.browser_sessions.get(session_key)
        extensions = await self.extension_status(profile_id)
        out: Dict[str, Any] = {
            "ok": True,
            "profile_id": profile_id,
            "profile_name": profile.name,
            "launch_mode": profile.launch_mode,
            "browser_type": profile.browser_type,
            "mode": mode,
            "running": page is not None,
            "connection": (
                "attached" if mode == "attached"
                else "managed_persistent" if mode == "persistent"
                else "ephemeral" if mode == "ephemeral"
                else "not_running"
            ),
            "user_data_dir": self.profile_dir(session_key) or profile.resolved_user_data_dir,
            "executable": profile.resolved_executable(),
            "tabs": tabs,
            "tab_count": len([t for t in tabs if not t.get("closed")]),
            "active_url": safe_url(page.url) if page is not None else "",
            "active_title": await self._page_title_safe(page) if page is not None else "",
            "session_status": record.status.value if record is not None else None,
            "last_used_at": profile.last_used_at,
            "last_error": self._last_error if mode == "none" or not page else profile.last_error,
            "profile_last_error": profile.last_error,
            "last_recovery": profile.last_recovery,
            "extensions": extensions,
            "route_guard": "unavailable_on_existing_chrome" if mode == "attached" else "enforced",
            "limitations": self._limitations(mode),
        }
        if profile.launch_mode == "attach":
            out["attach"] = {
                "host": profile.debugger_host or settings.BROWSER_ATTACH_HOST,
                "port": profile.debugger_port,
                "note": (
                    "An attached Chrome belongs to the operator. AirdropOS can read and drive its tabs, "
                    "but cannot intercept its network requests, and disconnecting leaves the browser open."
                ),
            }
        return out

    @staticmethod
    def _limitations(mode: str) -> List[str]:
        notes: List[str] = []
        if mode == "attached":
            notes.append(
                "Connected to an existing Chrome: sub-resource network requests are not intercepted, "
                "and installed-extension state belongs to that browser's own profile."
            )
        elif mode == "ephemeral":
            notes.append(
                "This session is an ephemeral browser context: no persistent cookies, no installed "
                "extensions, nothing carried over to the next task."
            )
        if mode in ("none", "attached"):
            notes.append(
                "Chrome extension popups that Chrome renders in its own window are not part of the page "
                "DOM; they can only be reached through the extension's own pages."
            )
        return notes

    async def extension_status(self, profile_id: str) -> Dict[str, Any]:
        """Which extensions this profile really has, and what can be done with them."""
        profile = profile_store.get_profile(profile_id)
        if profile is None:
            return {"ok": False, "extensions": [], "capabilities": {}}
        directory = self.profile_dir(profile_id) or profile.resolved_user_data_dir
        try:
            installed = chrome.installed_extensions(directory)
        except Exception as exc:
            logger.debug("Could not read installed extensions for '%s': %s", profile_id, exc)
            installed = []
        unpacked = [str(d) for d in profile.extension_dirs()]
        capabilities = {
            "detect_installed": True,
            "open_extension_page": True,
            "open_extension_settings": True,
            "install_from_web_store": "manual_confirmation_required",
            "inspect_popup_dom": False,
            "notes": (
                "Chrome requires a person to accept the 'Add to Chrome' confirmation for a Web Store "
                "install. AirdropOS opens the listing and pauses for that click; it does not bypass it. "
                "Unpacked developer extensions loaded with --load-extension are session-scoped: Chrome "
                "does not record them as installed, so they will not appear after a restart."
            ),
        }
        return {
            "ok": True,
            "profile_id": profile_id,
            "profile_dir": directory,
            "installed": [e.as_dict() for e in installed],
            "installed_count": len(installed),
            "unpacked_loaded_this_session": unpacked,
            "capabilities": capabilities,
            "read_at": datetime.now(timezone.utc).isoformat(),
        }

    def diagnostics_for(self, session_key: str) -> Dict[str, Any]:
        """Console output, page errors, failed requests and dialogs for one session."""
        session_key = normalize_session_key(session_key)
        recorders = self._recorders.get(session_key)
        if recorders is None:
            return {
                "console": [], "page_errors": [], "failed_requests": [],
                "dialogs": [], "downloads": [],
                "note": "No capture is active for this session; it may not be running yet.",
            }
        return {
            **recorders.diagnostics.report(),
            "dialogs": list(recorders.dialogs.seen),
            "downloads": recorders.downloads.report(),
        }

    def health(self) -> Dict[str, Any]:
        return {
            "status": "HEALTHY" if self._healthy else "UNAVAILABLE",
            "headless": settings.BROWSER_HEADLESS,
            "persistent_profiles": settings.BROWSER_PERSISTENT_PROFILES,
            "chrome_executable": str(chrome.find_chrome_executable().get("path") or ""),
            "profile_root": str(chrome.profile_root()),
            "open_contexts": len(self._contexts),
            "open_pages": len(self._pages),
            "modes": sorted(set(self._modes.values())),
            "last_error": self._last_error,
        }


controller = BrowserController()
