"""Browser Automation Tools exposed to the agent.

Every tool inspects the page AFTER acting and returns evidence. A click is never
reported as a successful submission on its own.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from backend.app.browser.controller import controller
from backend.app.tools.base import BaseTool, SideEffect, ToolResult

logger = logging.getLogger(__name__)


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
        result = await controller.navigate(params["session_key"], params["url"])
        if not result.get("ok"):
            return ToolResult.failure(
                result.get("detail", "Navigation failed"), category=result.get("reason", "NAVIGATION_FAILED"),
                retryable=result.get("reason") == "navigation_failed",
            )

        obs = result["observation"]
        if obs.get("requires_human"):
            # Do not attempt to solve or evade the gate.
            return ToolResult.needs_user(obs.get("human_reason") or "Human interaction required.")

        shot = await controller.screenshot(params["session_key"], "navigate")
        return ToolResult.success(
            data={"observation": obs, "screenshot_path": shot},
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
        page = await controller.get_page(params["session_key"])
        if page is None:
            return ToolResult.failure("No open page for this session.", category="SESSION_MISSING", retryable=True)
        obs = await controller.inspect(page)
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
        page = await controller.get_page(params["session_key"])
        if page is None:
            return ToolResult.failure("No open page for this session.", category="SESSION_MISSING", retryable=True)

        before = await controller.inspect(page)
        if before.requires_human:
            return ToolResult.needs_user(before.human_reason or "Human interaction required.")

        try:
            locator = page.get_by_role("button", name=params["button_text"], exact=False)
            if await locator.count() == 0:
                locator = page.locator(f"text={params['button_text']}")
            if await locator.count() == 0:
                return ToolResult.failure(
                    f"No button matching '{params['button_text']}'. Visible: {before.buttons[:20]}",
                    category="ELEMENT_NOT_FOUND",
                )
            await locator.first.click(timeout=10000)
        except Exception as exc:
            text = str(exc).lower()
            if "captcha" in text:
                return ToolResult.needs_user("CAPTCHA triggered by this action.")
            return ToolResult.failure(f"Click failed: {exc}", category="INTERACTION_FAILED", retryable=True)

        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        after = await controller.inspect(page)
        shot = await controller.screenshot(params["session_key"], "after_click")

        if after.requires_human:
            return ToolResult.needs_user(after.human_reason or "Human interaction required.")

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
                failed.append(f"{name}: {exc}")

        observation = await controller.inspect(page)
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
        path = await controller.screenshot(params["session_key"], params.get("label", "evidence"))
        if not path:
            return ToolResult.failure("Screenshot capture failed.", category="CAPTURE_FAILED", retryable=True)
        return ToolResult.success(
            data={"screenshot_path": path},
            evidence={"authoritative": True, "kind": "screenshot", "uri": path},
        )


def register_browser_tools(registry) -> None:
    registry.register_or_replace_many(
        [
            NavigateTool(),
            InspectPageTool(),
            ClickButtonTool(),
            FillFormTool(),
            CaptureEvidenceTool(),
        ]
    )
