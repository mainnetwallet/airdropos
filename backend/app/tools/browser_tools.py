"""Browser Automation Tools exposed to the agent.

Every tool inspects the page AFTER acting and returns evidence. A click is never
reported as a successful submission on its own.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from backend.app.browser.controller import _safe_error, controller
from backend.app.browser.profiles import normalize_session_key
from backend.app.live import hub
from backend.app.tools.base import BaseTool, SideEffect, ToolResult

logger = logging.getLogger(__name__)


def _gate(params: Dict[str, Any]):
    """Refuse to act while an operator paused/stopped the session, holds manual
    control, or the session is parked waiting for human verification."""
    reason = hub.browser_sessions.gate(normalize_session_key(params["session_key"]))
    return ToolResult.needs_user(reason) if reason else None


def _needs_human(params: Dict[str, Any], reason: str) -> ToolResult:
    """Park the session as 'waiting for user' (keeps the page, cookies and profile as they are)."""
    hub.browser_sessions.mark_waiting(normalize_session_key(params["session_key"]), reason)
    return ToolResult.needs_user(reason)


def _event(params: Dict[str, Any], type_: str, message: str, status: str = "ok") -> None:
    key = normalize_session_key(params["session_key"])
    hub.browser_sessions.touch(key)
    hub.events.emit(integration="browser", session_id=f"browser:{key}", type=type_, message=message, status=status)

_CLICK_ROLES = ("button", "link", "menuitem", "tab", "option")


async def _find_clickable(page, text: str, wait_ms: int = 6000):
    """Locate something clickable by its visible text.

    Tries buttons, links, menu items, tabs, plain text, aria-label/title and `value`, in the main
    page and in every iframe. Single-page apps (modals, login dialogs) often render a moment after
    navigation, so it polls briefly instead of failing on the first empty look.
    """
    import time

    deadline = time.monotonic() + wait_ms / 1000.0
    while True:
        for frame in [page.main_frame] + [f for f in page.frames if f != page.main_frame]:
            candidates = []
            for role in _CLICK_ROLES:
                candidates.append(frame.get_by_role(role, name=text, exact=False))
            candidates.append(frame.get_by_text(text, exact=False))
            candidates.append(frame.locator(f'[aria-label*="{text}" i], [title*="{text}" i], input[value*="{text}" i]'))
            for loc in candidates:
                try:
                    if await loc.count() > 0:
                        return loc.first
                except Exception:
                    continue
        if time.monotonic() >= deadline:
            return None
        try:
            await page.wait_for_timeout(500)
        except Exception:
            return None


class NavigateTool(BaseTool):
    name = "browser_navigate"
    version = "1.0.0"
    description = "Open a URL in an isolated browser session and return the page's visible content, forms, and buttons."
    required_permissions = ["browser.navigate"]
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 60
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "maxLength": 2048},
            "session_key": {"type": "string", "description": "Isolated session/account identifier", "maxLength": 64},
        },
        "required": ["url", "session_key"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        blocked = _gate(params)
        if blocked:
            return blocked
        result = await controller.navigate(params["session_key"], params["url"])
        if not result.get("ok"):
            return ToolResult.failure(
                result.get("detail", "Navigation failed"), category=result.get("reason", "NAVIGATION_FAILED"),
                retryable=result.get("reason") == "navigation_failed",
            )

        obs = result["observation"]
        if obs.get("requires_human"):
            # Do not attempt to solve or evade the gate.
            return _needs_human(params, obs.get("human_reason") or "Human interaction required.")

        shot = await controller.screenshot(params["session_key"], "navigate")
        return ToolResult.success(
            data={"observation": obs.__dict__, "screenshot_path": shot},
            evidence={"authoritative": True, "kind": "page_observation", "url": obs.get("url")},
        )


class InspectPageTool(BaseTool):
    name = "inspect_page"
    version = "1.0.0"
    description = "Read the current page state in an open session without navigating."
    required_permissions = ["browser.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    input_schema = {
        "type": "object",
        "properties": {
            "session_key": {"type": "string", "maxLength": 64},
        },
        "required": ["session_key"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        blocked = _gate(params)
        if blocked:
            return blocked
        page = await controller.get_page(params["session_key"])
        if page is None:
            return ToolResult.failure("No open page for this session.", category="SESSION_MISSING", retryable=True)
        obs = await controller.inspect(page)
        hub.browser_sessions.touch(normalize_session_key(params["session_key"]), url=page.url, title=obs.title)
        return ToolResult.success(data={"observation": obs.__dict__})


class ClickButtonTool(BaseTool):
    name = "browser_click"
    version = "1.0.0"
    description = "Click a button by its visible text, then re-inspect the page and return what actually changed. Returns evidence, not assumptions."
    required_permissions = ["browser.interact"]
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 45
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "session_key": {"type": "string", "maxLength": 64},
            "button_text": {"type": "string", "maxLength": 200},
            "expect_text": {
                "type": "string",
                "maxLength": 200,
                "description": "Text expected after a successful action. If absent from the resulting page, the action is reported as UNVERIFIED.",
            },
        },
        "required": ["session_key", "button_text"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        blocked = _gate(params)
        if blocked:
            return blocked
        page = await controller.get_page(params["session_key"])
        if page is None:
            return ToolResult.failure("No open page for this session.", category="SESSION_MISSING", retryable=True)

        before = await controller.inspect(page)
        if before.requires_human:
            return _needs_human(params, before.human_reason or "Human interaction required.")

        try:
            target = await _find_clickable(page, params["button_text"])
            if target is None:
                seen = await controller.inspect(page)
                return ToolResult.failure(
                    f"Nothing clickable matching '{params['button_text']}' was found (checked buttons, links, "
                    f"tabs, text and iframes, waited ~6s). Buttons: {seen.buttons[:20]}. "
                    f"Page: {seen.url}. Use inspect_page or capture_evidence, then retry with the exact visible text. "
                    "Do NOT report this click as done.",
                    category="ELEMENT_NOT_FOUND",
                )
            await target.click(timeout=10000)
        except Exception as exc:
            text = str(exc).lower()
            if "captcha" in text:
                return _needs_human(params, "CAPTCHA triggered by this action.")
            _event(params, "browser.click_failed", f'Click on "{params["button_text"][:60]}" failed', "error")
            return ToolResult.failure(f"Click failed: {_safe_error(exc)}", category="INTERACTION_FAILED", retryable=True)

        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        if hub.browser_sessions.is_operator_stopped(normalize_session_key(params["session_key"])):
            # The operator stopped the session while the click was in flight: report nothing as done.
            return ToolResult.needs_user("This session was stopped by an operator while the click was running.")
        after = await controller.inspect(page)
        shot = await controller.screenshot(params["session_key"], "after_click")

        _event(params, "browser.clicked", f'Clicked "{params["button_text"][:60]}" button')
        hub.browser_sessions.touch(normalize_session_key(params["session_key"]), url=after.url, title=after.title)
        if after.requires_human:
            return _needs_human(params, after.human_reason or "Human interaction required.")

        expect = params.get("expect_text")
        verified = False
        if expect:
            verified = expect.lower() in f"{after.title} {after.text_excerpt}".lower()

        # Deliberately NOT claiming success just because the click did not error.
        data = {
            "clicked": params["button_text"],
            "url_before": before.url,
            "url_after": after.url,
            "page_changed": before.url != after.url or before.text_excerpt != after.text_excerpt,
            "expected_text_seen": verified if expect else None,
            "observation_after": after.__dict__,
            "screenshot_path": shot,
            "note": (
                "The click executed. This is NOT proof of a successful submission. "
                "Confirmation requires an authoritative server response or receipt."
            ),
        }
        return ToolResult.success(
            data=data,
            evidence={"authoritative": verified, "kind": "post_action_observation", "url": after.url},
        )


class FillFormTool(BaseTool):
    name = "browser_fill_form"
    version = "1.0.0"
    description = "Fill form fields by name in an open session. Does NOT submit; the caller must click the submit control separately and verify."
    required_permissions = ["browser.interact"]
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 45
    input_schema = {
        "type": "object",
        "properties": {
            "session_key": {"type": "string", "maxLength": 64},
            "fields": {
                "type": "object",
                "description": "Map of field name to value",
            },
        },
        "required": ["session_key", "fields"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        blocked = _gate(params)
        if blocked:
            return blocked
        page = await controller.get_page(params["session_key"])
        if page is None:
            return ToolResult.failure("No open page for this session.", category="SESSION_MISSING", retryable=True)

        filled, missing, failed = [], [], []
        for name, value in (params.get("fields") or {}).items():
            selector = f'[name="{name}"]'
            try:
                element = page.locator(selector).first
                if await element.count() == 0:
                    missing.append(name)
                    continue
                tag = await element.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    await element.select_option(str(value))
                elif tag in ("input", "textarea"):
                    input_type = (await element.get_attribute("type") or "text").lower()
                    if input_type in ("checkbox", "radio"):
                        await element.check() if value else await element.uncheck()
                    else:
                        await element.fill(str(value))
                else:
                    failed.append(name)
                    continue
                filled.append(name)
            except Exception as exc:
                # The message may echo the value being typed (a password, a token): scrub it.
                detail = _safe_error(exc)
                if len(str(value)) >= 3:
                    detail = detail.replace(str(value), "***")
                failed.append(f"{name}: {detail}")

        if hub.browser_sessions.is_operator_stopped(normalize_session_key(params["session_key"])):
            return ToolResult.needs_user("This session was stopped by an operator while the form was being filled.")
        observation = await controller.inspect(page)
        # Field NAMES only: values may be secrets and never go to the activity log.
        _event(params, "browser.form_filled", f"Filled {len(filled)} form field(s)")
        return ToolResult.success(
            data={
                "filled": filled,
                "missing_fields": missing,
                "failed_fields": failed,
                "observation": observation.__dict__,
                "note": "Fields were populated. The form was NOT submitted. Verify before assuming success.",
            }
        )


class CaptureEvidenceTool(BaseTool):
    name = "capture_evidence"
    version = "1.0.0"
    description = "Capture a full-page screenshot of an open session as durable task evidence."
    required_permissions = ["browser.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    input_schema = {
        "type": "object",
        "properties": {
            "session_key": {"type": "string", "maxLength": 64},
            "label": {"type": "string", "maxLength": 100},
        },
        "required": ["session_key"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        blocked = _gate(params)
        if blocked:
            return blocked
        path = await controller.screenshot(params["session_key"], params.get("label", "evidence"))
        if not path:
            return ToolResult.failure("Screenshot capture failed.", category="CAPTURE_FAILED", retryable=True)
        _event(params, "browser.screenshot", "Screenshot captured")
        return ToolResult.success(
            data={"screenshot_path": path},
            evidence={"authoritative": True, "kind": "screenshot", "uri": path},
        )


class CloseSessionTool(BaseTool):
    name = "browser_close"
    version = "1.0.0"
    description = (
        "Close an open browser session (page, context and live view). Saves its cookies/login state first. "
        "Use when the operator asks to stop/close the browser or the work in it is finished."
    )
    required_permissions = ["browser.interact"]
    side_effect = SideEffect.WEB_INTERACTION
    timeout_seconds = 30
    is_idempotent = True
    input_schema = {
        "type": "object",
        "properties": {"session_key": {"type": "string", "maxLength": 64}},
        "required": ["session_key"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        key = normalize_session_key(params["session_key"])
        reg = hub.browser_sessions
        rec = reg.get(key)
        open_keys = [k for k, r in reg.records.items() if not r.stopped]
        if rec is None or rec.stopped:
            return ToolResult.failure(
                f"No open browser session named '{key}'. Open sessions: {open_keys or 'none'}.",
                category="SESSION_MISSING",
            )
        # An operator driving the session by hand is not interrupted. Paused / waiting-for-human
        # sessions CAN be closed (the operator asked), so the normal action gate is not used here.
        reg._expire_lock(rec)
        if rec.control is not None:
            return ToolResult.needs_user("An operator has manual control of this browser session, so the agent will not close it.")
        reg.mark_stopped(key, by_operator=False, reason="Closed by agent on request")
        await controller.close_session(key)
        return ToolResult.success(
            data={"closed": key, "open_sessions": [k for k, r in reg.records.items() if not r.stopped]},
            evidence={"authoritative": True, "kind": "session_closed", "session_key": key},
        )


def register_browser_tools(registry) -> None:
    registry.register_or_replace_many(
        [
            NavigateTool(),
            InspectPageTool(),
            ClickButtonTool(),
            FillFormTool(),
            CaptureEvidenceTool(),
            CloseSessionTool(),
        ]
    )
