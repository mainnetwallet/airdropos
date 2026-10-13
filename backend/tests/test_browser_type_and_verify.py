"""browser_type async/sync regression + exact input verification.

Automated tests only: the Chromium here is Playwright's bundled headless build with set_content
pages. They are NOT integration tests against the managed persistent Chrome profile.
"""
import asyncio

import pytest

from backend.app.browser import interaction


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------- no-browser reproduction
class _FakeKeyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    async def type(self, text, delay=0):
        self.typed.append(text)

    async def press(self, key):
        self.pressed.append(key)


class _FakeLocator:
    """Mimics Playwright's async Locator: every I/O method is a coroutine."""

    def __init__(self, input_type):
        self._type = input_type
        self.filled = []

    async def get_attribute(self, name):
        return self._type if name == "type" else None

    async def click(self, timeout=None):
        return None

    async def fill(self, text, timeout=None):
        self.filled.append(text)

    async def evaluate(self, script, *a):
        return {"tag": "input", "type": self._type, "name": "f", "disabled": False, "visible": True}


class _FakePage:
    def __init__(self):
        self.keyboard = _FakeKeyboard()


def _patch_resolve(monkeypatch, locator):
    async def fake_resolve(page, **kw):
        return locator, 1

    monkeypatch.setattr(interaction, "resolve", fake_resolve)


def test_is_sensitive_input_is_awaitable_and_reads_async_attribute():
    # Before the fix this was a plain function that returned a bool, so `await` raised
    # "object bool can't be used in 'await' expression".
    assert run(interaction.is_sensitive_input(_FakeLocator("password"))) is True
    assert run(interaction.is_sensitive_input(_FakeLocator("text"))) is False


def test_type_text_does_not_raise_on_async_locator(monkeypatch):
    loc, page = _FakeLocator("text"), _FakePage()
    _patch_resolve(monkeypatch, loc)
    res = run(interaction.type_text(page, text="hello", strategy="label", value="Email", delay_ms=0))
    assert res["ok"] and res["characters_typed"] == 5
    assert res["sensitive_field"] is False
    assert page.keyboard.typed == ["hello"]
    assert page.keyboard.pressed == []  # no Enter unless submit=True


def test_type_text_flags_password_field_as_sensitive(monkeypatch):
    # The old code swallowed the error and reported False for everything (fail-open).
    loc, page = _FakeLocator("password"), _FakePage()
    _patch_resolve(monkeypatch, loc)
    res = run(interaction.type_text(page, text="s3cret", strategy="label", value="Password", delay_ms=0))
    assert res["sensitive_field"] is True
    assert "s3cret" not in str(res)


# --------------------------------------------------------------------- real headless Chromium
FORM = """
<form id=f onsubmit="window.__submits=(window.__submits||0)+1; return false;">
  <input id=email name=email type=text aria-label="Email">
  <input id=pw name=pw type=password aria-label="Password">
  <textarea id=notes aria-label="Notes"></textarea>
  <select id=sel aria-label="Pick"><option value=a>A</option><option value=b>B</option></select>
  <input id=cb type=checkbox aria-label="Agree">
  <datalist id=dl><option value="alice@example.com"></datalist>
  <input id=ac list=dl aria-label="Auto">
  <button type=submit>Go</button>
</form>
"""


@pytest.fixture
def page():
    from playwright.async_api import async_playwright

    holder = {}

    async def start():
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment without Chromium
            await pw.stop()
            pytest.skip(f"Chromium unavailable: {type(exc).__name__}")
        pg = await browser.new_page()
        await pg.set_content(FORM)
        holder.update(pw=pw, browser=browser)
        return pg

    loop = asyncio.new_event_loop()
    pg = loop.run_until_complete(start())
    pg._loop = loop
    yield pg
    loop.run_until_complete(holder["browser"].close())
    loop.run_until_complete(holder["pw"].stop())
    loop.close()


def _go(page, coro):
    return page._loop.run_until_complete(coro)


def test_real_type_text_types_into_field_without_submitting(page):
    res = _go(page, interaction.type_text(page, text="me@example.com", strategy="label", value="Email", delay_ms=0))
    assert res["ok"] and res["submitted"] is False
    assert _go(page, page.input_value("#email")) == "me@example.com"
    assert _go(page, page.evaluate("window.__submits || 0")) == 0


def test_real_type_text_password_is_flagged_and_not_echoed(page):
    res = _go(page, interaction.type_text(page, text="hunter2", strategy="label", value="Password", delay_ms=0))
    assert res["sensitive_field"] is True
    assert "hunter2" not in str(res)


def test_fill_then_read_exact_match_and_no_submit(page):
    _go(page, interaction.fill_field(page, strategy="label", value="exact value 123"
                                     , field_value="Email"))
    got = _go(page, interaction.verify_input_value(page, strategy="label", value="Email",
                                                   expected="exact value 123"))
    assert got["status"] == "VERIFIED" and got["value"] == "exact value 123"
    assert _go(page, page.evaluate("window.__submits || 0")) == 0


def test_read_is_exact_not_substring_or_trimmed(page):
    _go(page, interaction.fill_field(page, strategy="label", value="abc ", field_value="Email"))
    assert _go(page, interaction.verify_input_value(page, strategy="label", value="Email", expected="abc"))["status"] == "MISMATCH"
    assert _go(page, interaction.verify_input_value(page, strategy="label", value="Email", expected="abc "))["status"] == "VERIFIED"


def test_mismatch_reports_actual_value(page):
    _go(page, interaction.fill_field(page, strategy="label", value="one", field_value="Email"))
    res = _go(page, interaction.verify_input_value(page, strategy="label", value="Email", expected="two"))
    assert res["status"] == "MISMATCH" and res["value"] == "one"


def test_datalist_suggestion_is_not_counted_as_value(page):
    _go(page, interaction.fill_field(page, strategy="label", value="ali", field_value="Auto"))
    res = _go(page, interaction.verify_input_value(page, strategy="label", value="Auto",
                                                   expected="alice@example.com"))
    assert res["status"] == "MISMATCH" and res["value"] == "ali"


def test_password_field_is_unverified_and_value_never_returned(page):
    _go(page, interaction.fill_field(page, strategy="label", value="hunter2", field_value="Password"))
    res = _go(page, interaction.verify_input_value(page, strategy="label", value="Password", expected="hunter2"))
    assert res["status"] == "UNVERIFIED"
    assert "hunter2" not in str(res)


def test_unreadable_target_is_unverified_not_success(page):
    res = _go(page, interaction.verify_input_value(page, strategy="label", value="No such field",
                                                   expected="x", timeout_ms=300))
    assert res["status"] == "UNVERIFIED"
    assert res.get("ok") is False


def test_non_input_element_is_unverified(page):
    res = _go(page, interaction.verify_input_value(page, strategy="text", value="Go", expected="Go", timeout_ms=300))
    assert res["status"] == "UNVERIFIED"


# --------------------------------------------------------------------- tool layer
def _tool_env(monkeypatch, page):
    from backend.app.tools import browser_tools as bt

    async def fake_get_page(key):
        return page

    async def fake_inspect(p):
        class Obs:
            url = "about:blank"
            __dict__ = {"url": "about:blank"}
        return Obs()

    monkeypatch.setattr(bt.controller, "get_page", fake_get_page)
    monkeypatch.setattr(bt.controller, "inspect", fake_inspect)
    return bt


def test_browser_type_tool_succeeds_end_to_end(page, monkeypatch):
    bt = _tool_env(monkeypatch, page)
    res = _go(page, bt.TypeTool().execute({"text": "tool typed", "strategy": "label", "value": "Notes",
                                           "session_key": "type_tool_test"}))
    assert res.ok, res.error
    assert _go(page, page.input_value("#notes")) == "tool typed"
    assert _go(page, page.evaluate("window.__submits || 0")) == 0


def test_read_value_tool_verified_mismatch_unverified(page, monkeypatch):
    bt = _tool_env(monkeypatch, page)
    tool = bt.ReadValueTool()
    _go(page, page.fill("#email", "a@b.co"))
    ok = _go(page, tool.execute({"strategy": "label", "target": "Email", "expected": "a@b.co",
                                 "session_key": "rv_test"}))
    assert ok.ok and ok.data["status"] == "VERIFIED"
    bad = _go(page, tool.execute({"strategy": "label", "target": "Email", "expected": "x@y.z",
                                  "session_key": "rv_test"}))
    assert bad.ok and bad.data["status"] == "MISMATCH" and bad.data["value"] == "a@b.co"
    pw = _go(page, tool.execute({"strategy": "label", "target": "Password", "expected": "x",
                                 "session_key": "rv_test", "timeout_ms": 500}))
    assert pw.data["status"] == "UNVERIFIED"
    missing = _go(page, tool.execute({"strategy": "label", "target": "Nope", "expected": "x",
                                      "session_key": "rv_test", "timeout_ms": 300}))
    assert missing.data["status"] == "UNVERIFIED"


def test_read_value_tool_respects_operator_gate(page, monkeypatch):
    from backend.app.live import hub

    bt = _tool_env(monkeypatch, page)
    hub.browser_sessions.ensure("rv_gate")
    monkeypatch.setattr(hub.browser_sessions, "gate", lambda k: "Operator stopped this session")
    res = _go(page, bt.ReadValueTool().execute({"strategy": "label", "target": "Email", "session_key": "rv_gate"}))
    assert not res.ok
