"""Regression tests: research tools must not reach loopback / metadata / private hosts."""

from __future__ import annotations

import httpx
import pytest

from backend.app.integrations.discord import DiscordBot
from backend.app.integrations.twitter import TwitterBot
from backend.app.tools import web_tools
from backend.app.tools.base import ToolResultStatus
from backend.app.tools.web_tools import DocumentationReaderTool, SiteSearchTool, WebFetchTool

BLOCKED = [
    "http://127.0.0.1:8000/api/v1/settings",
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost/",
    "http://[::1]/",
    "http://postgres.internal/",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BLOCKED)
@pytest.mark.parametrize("tool_cls", [WebFetchTool, SiteSearchTool, DocumentationReaderTool])
async def test_literal_internal_targets_refused(tool_cls, url):
    result = await tool_cls().execute({"url": url})
    assert result.status != ToolResultStatus.SUCCESS
    assert "not allowed" in (result.error or "").lower() or "DESTINATION_BLOCKED" in str(result)


@pytest.mark.asyncio
async def test_redirect_to_internal_address_is_refused(monkeypatch):
    """A public page that 302s to cloud metadata must be stopped at the second hop."""

    async def fake_block(url: str):
        return "local and internal hosts are not allowed" if "169.254" in url else None

    import backend.app.browser.controller as controller

    monkeypatch.setattr(controller, "destination_block_reason", fake_block)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.org":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
        raise AssertionError("internal host was contacted")

    real = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(web_tools.httpx, "AsyncClient", patched)
    result = await WebFetchTool().execute({"url": "http://example.org/"})
    assert result.status != ToolResultStatus.SUCCESS


@pytest.mark.asyncio
async def test_public_page_still_fetches(monkeypatch):
    async def fake_block(url: str):
        return None

    import backend.app.browser.controller as controller

    monkeypatch.setattr(controller, "destination_block_reason", fake_block)
    real = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = httpx.MockTransport(
            lambda req: httpx.Response(200, text="<html><body>airdrop snapshot</body></html>")
        )
        return real(*a, **kw)

    monkeypatch.setattr(web_tools.httpx, "AsyncClient", patched)
    result = await WebFetchTool().execute({"url": "http://example.org/"})
    assert result.status == ToolResultStatus.SUCCESS


@pytest.mark.asyncio
async def test_discord_and_x_do_not_claim_to_run(monkeypatch):
    for bot, starter in ((DiscordBot(), "start_polling"), (TwitterBot(), "start_polling")):
        monkeypatch.setattr(type(bot), "is_configured", property(lambda self: True))
        assert await getattr(bot, starter)() is False
        assert bot.health()["status"] == "NOT_IMPLEMENTED"
