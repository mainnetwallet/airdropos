"""browser_close exists and works; browser_click finds links/text and fails honestly."""
import asyncio

from backend.app.live import hub
from backend.app.tools import browser_tools as bt
from backend.app.tools.browser_tools import CloseSessionTool, _find_clickable, register_browser_tools


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_browser_close_is_registered():
    class Reg:
        def register_or_replace_many(self, tools):
            self.names = [t.name for t in tools]

    reg = Reg()
    register_browser_tools(reg)
    assert "browser_close" in reg.names


def test_close_missing_session_fails_honestly():
    res = run(CloseSessionTool().execute({"session_key": "does_not_exist_zz"}))
    assert not res.ok
    assert "No open browser session" in str(res.__dict__)


def test_close_open_session_closes_it(monkeypatch):
    closed = []

    async def fake_close(key):
        closed.append(key)

    monkeypatch.setattr(bt.controller, "close_session", fake_close)
    key = "close_test_session"
    hub.browser_sessions.ensure(key)
    res = run(CloseSessionTool().execute({"session_key": key}))
    assert closed == [key]
    assert hub.browser_sessions.get(key).stopped
    assert res.ok and res.data["closed"] == key


def test_find_clickable_finds_link_and_div_text():
    from playwright.async_api import async_playwright

    async def go():
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch()
            except Exception:
                return "skip"
            page = await browser.new_page()
            await page.set_content(
                "<a href='#x'>Continue with Google</a><div onclick='1'>Sign up now</div>"
            )
            a = await _find_clickable(page, "Google", wait_ms=500)
            b = await _find_clickable(page, "Sign up", wait_ms=500)
            c = await _find_clickable(page, "nothing here", wait_ms=500)
            await browser.close()
            return a is not None, b is not None, c is None

    out = run(go())
    if out != "skip":
        assert out == (True, True, True)
