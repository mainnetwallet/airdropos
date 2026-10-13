"""Playwright Browser Controller.

Isolated contexts per account. Persistent session directories. Crash recovery.
Detects CAPTCHA/MFA/expiry and reports NEEDS_USER_ACTION rather than attempting
to circumvent access controls (Specification Part 12).
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

from backend.app.browser import profiles as profile_store
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


class BrowserController:
    """Manages Playwright lifecycle and isolated per-account contexts."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._contexts: Dict[str, Any] = {}
        self._pages: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._key_locks: Dict[str, asyncio.Lock] = {}
        self._healthy = False
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> bool:
        """Launch Chromium. Returns False rather than raising so the rest of
        the platform keeps working when browser automation is unavailable."""
        async with self._lock:
            if self._healthy and self._browser is not None:
                return True
            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=settings.BROWSER_HEADLESS,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
                self._healthy = True
                self._last_error = None
                logger.info("Browser automation started (headless=%s)", settings.BROWSER_HEADLESS)
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
        async with self._lock:
            for context in self._contexts.values():
                try:
                    await context.close()
                except Exception:
                    pass
            self._contexts.clear()
            self._pages.clear()
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
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
        """Restart the browser after a crash."""
        if self._healthy:
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

    async def _safe_close(self, obj: Any) -> None:
        if obj is None:
            return
        try:
            await asyncio.wait_for(obj.close(), timeout=10)
        except Exception:
            pass

    async def _mark_browser_dead(self) -> None:
        """The Chromium process is gone: forget everything that pointed into it so the next
        call relaunches cleanly instead of failing forever on stale handles."""
        async with self._lock:
            self._healthy = False
            self._contexts.clear()
            self._pages.clear()
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
        await self._safe_close(self._pages.pop(session_key, None))
        await self._safe_close(self._contexts.pop(session_key, None))

    async def get_context(self, session_key: str, persistent: bool = True):
        """Get or create an isolated browser context for an account.

        Cookies and storage are partitioned per session_key so separate accounts
        never share authentication state.
        """
        session_key = normalize_session_key(session_key)
        async with self._lock_for(session_key):
            return await self._ensure_context(session_key, persistent)

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
        width = (profile.viewport_width if profile and profile.viewport_width else settings.BROWSER_VIEWPORT_WIDTH)
        height = (profile.viewport_height if profile and profile.viewport_height else settings.BROWSER_VIEWPORT_HEIGHT)
        options: Dict[str, Any] = {
            "viewport": {"width": width, "height": height},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "ignore_https_errors": False,
            # Service workers can answer requests without the network layer ever seeing them,
            # which would skip the destination guard below.
            "service_workers": "block",
        }
        if profile and profile.locale:
            options["locale"] = profile.locale
        if profile and profile.timezone_id:
            options["timezone_id"] = profile.timezone_id
        if persistent:
            # Cookies/local storage survive restarts (encrypted at rest, see profiles.py).
            # Only a real profile ever has saved state, and it is only ever loaded into ITS context.
            saved = profile_store.load_state(session_key)
            if saved:
                options["storage_state"] = saved

        context = None
        try:
            context = await self._browser.new_context(**options)
            context.set_default_timeout(settings.BROWSER_ACTION_TIMEOUT_MS)
            context.set_default_navigation_timeout(settings.BROWSER_NAVIGATION_TIMEOUT_MS)
            # Every request (redirect hops, iframes, scripts, fetches) is checked, not just the
            # first URL the agent asked for.
            await context.route("**/*", self._make_route_guard(session_key))
            # context.route() never sees WebSocket handshakes, so a page could otherwise open
            # ws://internal-host/ and bypass the guard above. Fail closed if this Playwright
            # build cannot intercept them (route_web_socket needs Playwright >= 1.48).
            route_ws = getattr(context, "route_web_socket", None)
            if route_ws is None:
                raise RuntimeError("Playwright >= 1.48 is required to guard WebSocket connections")
            await route_ws("**/*", self._make_ws_guard(session_key))
        except Exception as exc:
            logger.error("Failed to create context for '%s': %s", session_key, _safe_error(exc))
            await self._safe_close(context)  # never leave a half-configured context behind
            try:
                if self._browser is not None and not self._browser.is_connected():
                    await self._mark_browser_dead()
            except Exception:
                pass
            return None

        self._contexts[session_key] = context
        hub.browser_sessions.ensure(
            session_key,
            profile_id=profile.id if profile else None,
            profile_name=profile.name if profile else None,
        )
        logger.info("Created isolated browser context for session '%s'", session_key)
        return context

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
        """Get or create a page within the session's context. Reuses open tabs."""
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
                    page = await context.new_page()
                    self._pages[session_key] = page
                    return page
                except Exception as exc:
                    logger.error("Failed to open page for '%s' (attempt %s): %s", session_key, attempt, exc)
                    # A closed/crashed context fails forever and was kept as a stale entry; drop it
                    # so the second attempt (and later calls) start from a fresh one.
                    await self._discard_context(session_key)
            return None

    async def close_session(self, session_key: str) -> None:
        session_key = normalize_session_key(session_key)
        # Serialised with creation, so a context being created right now cannot slip in
        # after this close and be left running with nothing tracking it.
        async with self._lock_for(session_key):
            await self.save_state(session_key)
            await self._safe_close(self._pages.pop(session_key, None))
            await self._safe_close(self._contexts.pop(session_key, None))

    # ------------------------------------------------------------------
    # Navigation & observation
    # ------------------------------------------------------------------
    async def navigate(self, session_key: str, url: str) -> Dict[str, Any]:
        session_key = normalize_session_key(session_key)
        if hub.browser_sessions.is_operator_stopped(session_key):
            return {"ok": False, "reason": "operator_stopped", "detail": "This session was stopped by an operator."}
        blocked = await destination_block_reason(url)
        if blocked:
            # Not a session fault: nothing is marked failed, no browser is opened for it.
            return {"ok": False, "reason": "blocked_url", "detail": blocked}

        page = await self.get_page(session_key)
        if page is None:
            hub.browser_sessions.mark_error(session_key, "Browser unavailable")
            return {"ok": False, "reason": "browser_unavailable", "detail": self._last_error}

        try:
            response = await page.goto(url, wait_until="domcontentloaded")
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
            # The page/context died: drop it and try once more on a fresh one.
            logger.warning("Page for '%s' died during navigation (%s). Recreating context.", session_key, type(exc).__name__)
            await self.close_session(session_key)
            page = await self.get_page(session_key)
            if page is None:
                hub.browser_sessions.mark_error(session_key, "Browser unavailable")
                return {"ok": False, "reason": "browser_unavailable"}
            try:
                response = await page.goto(url, wait_until="domcontentloaded")
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
        if url_block_reason(page.url) and page.url not in ("about:blank", ""):
            try:
                await page.goto("about:blank")
            except Exception:
                pass
            return {"ok": False, "reason": "blocked_url", "detail": "the page redirected to an address that is not allowed"}

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

    async def screenshot(self, session_key: str, label: str = "evidence") -> Optional[str]:
        session_key = normalize_session_key(session_key)
        page = await self.get_page(session_key)
        if page is None:
            return None
        out_dir = USER_DATA_ROOT / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{label}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:
            logger.error("Screenshot failed for '%s': %s", session_key, exc)
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
        """Persist cookies/local storage of a session (best effort)."""
        session_key = normalize_session_key(session_key)
        context = self._contexts.get(session_key)
        if context is None or profile_store.get_profile(session_key) is None:
            return  # state is only ever kept for real profiles
        try:
            state = await context.storage_state()
            await asyncio.get_running_loop().run_in_executor(None, profile_store.save_state, session_key, state)
        except Exception as exc:
            logger.debug("Could not save state for '%s': %s", session_key, exc)

    def health(self) -> Dict[str, Any]:
        return {
            "status": "HEALTHY" if self._healthy else "UNAVAILABLE",
            "headless": settings.BROWSER_HEADLESS,
            "open_contexts": len(self._contexts),
            "open_pages": len(self._pages),
            "last_error": self._last_error,
        }


controller = BrowserController()
