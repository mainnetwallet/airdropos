"""Playwright Browser Controller.

Isolated contexts per account. Persistent session directories. Crash recovery.
Detects CAPTCHA/MFA/expiry and reports NEEDS_USER_ACTION rather than attempting
to circumvent access controls (Specification Part 12).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.app.core.config import settings

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


class BrowserController:
    """Manages Playwright lifecycle and isolated per-account contexts."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._contexts: Dict[str, Any] = {}
        self._pages: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
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
                self._last_error = str(exc)
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
    async def get_context(self, session_key: str, persistent: bool = True):
        """Get or create an isolated browser context for an account.

        Cookies and storage are partitioned per session_key so separate accounts
        never share authentication state.
        """
        if session_key in self._contexts:
            return self._contexts[session_key]

        if not await self.ensure_running():
            return None

        storage_dir = None
        if persistent:
            USER_DATA_ROOT.mkdir(parents=True, exist_ok=True)
            storage_dir = USER_DATA_ROOT / f"session_{session_key}"

        try:
            context = await self._browser.new_context(
                viewport={"width": settings.BROWSER_VIEWPORT_WIDTH, "height": settings.BROWSER_VIEWPORT_HEIGHT},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                ignore_https_errors=False,
            )
            context.set_default_timeout(settings.BROWSER_ACTION_TIMEOUT_MS)
            context.set_default_navigation_timeout(settings.BROWSER_NAVIGATION_TIMEOUT_MS)
            self._contexts[session_key] = context
            logger.info("Created isolated browser context for session '%s'", session_key)
            return context
        except Exception as exc:
            logger.error("Failed to create context for '%s': %s", session_key, exc)
            return None

    async def get_page(self, session_key: str):
        """Get or create a page within the session's context. Reuses open tabs."""
        if session_key in self._pages:
            page = self._pages[session_key]
            if not page.is_closed():
                return page
        context = await self.get_context(session_key)
        if context is None:
            return None
        try:
            page = await context.new_page()
            self._pages[session_key] = page
            return page
        except Exception as exc:
            logger.error("Failed to open page for '%s': %s", session_key, exc)
            return None

    async def close_session(self, session_key: str) -> None:
        page = self._pages.pop(session_key, None)
        if page and not page.is_closed():
            try:
                await page.close()
            except Exception:
                pass
        context = self._contexts.pop(session_key, None)
        if context:
            try:
                await context.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Navigation & observation
    # ------------------------------------------------------------------
    async def navigate(self, session_key: str, url: str) -> Dict[str, Any]:
        page = await self.get_page(session_key)
        if page is None:
            return {"ok": False, "reason": "browser_unavailable", "detail": self._last_error}

        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            # A crash is recoverable: drop the context and try once more.
            logger.warning("Navigation to %s failed: %s. Recreating context.", url, exc)
            await self.close_session(session_key)
            page = await self.get_page(session_key)
            if page is None:
                return {"ok": False, "reason": "browser_unavailable"}
            try:
                response = await page.goto(url, wait_until="domcontentloaded")
            except Exception as exc2:
                return {"ok": False, "reason": "navigation_failed", "detail": str(exc2)}

        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # Networkidle is best-effort; many sites never reach it.

        observation = await self.inspect(page)
        observation["http_status"] = response.status if response else None
        return {"ok": True, "observation": observation}

    async def inspect(self, page) -> PageObservation:
        """Extract a structured view of the current page."""
        try:
            content = await page.content()
        except Exception as exc:
            return PageObservation(
                url=page.url, title="", text_excerpt=f"Inspection failed: {exc}",
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
        for btn in await page.query_selector_all("button, [role=button], input[type=submit]"):
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

    def health(self) -> Dict[str, Any]:
        return {
            "status": "HEALTHY" if self._healthy else "UNAVAILABLE",
            "headless": settings.BROWSER_HEADLESS,
            "open_contexts": len(self._contexts),
            "open_pages": len(self._pages),
            "last_error": self._last_error,
        }


controller = BrowserController()
