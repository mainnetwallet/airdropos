"""Open Browser must land on Chrome's New Tab page, not on about:blank.

Playwright's ``launch_persistent_context`` always appends ``about:blank`` to the Chrome
command line (and rejects any URL passed through ``args``), so a managed Chrome used to
open on a blank white tab. The fix reuses that first tab and navigates it to
``chrome://newtab/``; these tests pin the behaviour down without needing a real browser.
The real-Chrome behaviour was verified separately (see the commit message).
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.browser import chrome
from backend.app.browser import controller as controller_module
from backend.app.browser import profiles as P
from backend.app.browser.controller import BrowserController


class FakePage:
    def __init__(self, url="about:blank", goto_error=None):
        self.url = url
        self._closed = False
        self._goto_error = goto_error
        self.goto_calls = []
        self.fronted = 0

    def is_closed(self):
        return self._closed

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        if self._goto_error:
            raise self._goto_error
        self.url = "chrome://new-tab-page/"

    async def bring_to_front(self):
        self.fronted += 1


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        page = FakePage()
        self.pages.append(page)
        return page


def make_controller(pages, mode="persistent", tracked=True):
    ctrl = BrowserController()
    context = FakeContext(pages)
    ctrl._contexts["p1"] = context
    ctrl._modes["p1"] = mode
    if tracked and pages:
        ctrl._pages["p1"] = pages[0]
    return ctrl, context


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- chrome helpers
def test_new_tab_url_is_chromes_own_page_not_about_blank():
    assert chrome.NEW_TAB_URL == "chrome://newtab/"
    assert not chrome.is_blank_page_url(chrome.NEW_TAB_URL)


@pytest.mark.parametrize("url", ["about:blank", "ABOUT:BLANK", "", None, "  about:blank "])
def test_blank_page_urls_are_recognised(url):
    assert chrome.is_blank_page_url(url)


@pytest.mark.parametrize("url", ["chrome://new-tab-page/", "https://example.com/", "chrome://extensions/"])
def test_real_pages_are_not_blank(url):
    assert not chrome.is_blank_page_url(url)


def test_extensions_are_not_disabled_by_playwrights_default_switch():
    assert "--disable-extensions" in chrome.PLAYWRIGHT_IGNORED_DEFAULT_ARGS
    # The managed-profile command line itself never disables extensions.
    assert "--disable-extensions" not in chrome.build_chrome_args(headless=False, extension_dirs=["/x"])


def test_the_agent_allowlist_is_not_widened_by_the_new_tab_page():
    """chrome://newtab is a lifecycle step, not something the agent may navigate to."""
    assert chrome.internal_page_reason(chrome.NEW_TAB_URL) is not None


# ---------------------------------------------------------------- show_new_tab
def test_blank_first_tab_is_reused_and_navigated_to_the_new_tab_page():
    page = FakePage("about:blank")
    ctrl, context = make_controller([page])
    result = run(ctrl.show_new_tab("p1"))
    assert result["ok"] and result["changed"]
    assert page.goto_calls and page.goto_calls[0][0] == chrome.NEW_TAB_URL
    assert page.url == "chrome://new-tab-page/"
    assert context.new_page_calls == 0, "must reuse the existing tab, not open another"
    assert len(context.pages) == 1
    assert page.fronted == 1
    assert ctrl._pages["p1"] is page


def test_untracked_blank_tab_is_adopted_instead_of_creating_a_new_one():
    page = FakePage("about:blank")
    ctrl, context = make_controller([page], tracked=False)
    result = run(ctrl.show_new_tab("p1"))
    assert result["changed"]
    assert context.new_page_calls == 0
    assert ctrl._pages["p1"] is page


def test_a_tab_that_already_shows_something_is_left_alone():
    page = FakePage("https://example.com/")
    ctrl, context = make_controller([page])
    result = run(ctrl.show_new_tab("p1"))
    assert result["ok"] and result["changed"] is False
    assert page.goto_calls == []
    assert context.new_page_calls == 0


def test_other_tabs_are_never_closed_or_navigated():
    first, other = FakePage("about:blank"), FakePage("https://example.com/")
    ctrl, context = make_controller([first, other])
    run(ctrl.show_new_tab("p1"))
    assert other.goto_calls == [] and not other.is_closed()
    assert len(context.pages) == 2


def test_only_managed_persistent_chrome_is_touched():
    for mode in ("attached", "ephemeral"):
        page = FakePage("about:blank")
        ctrl, _ = make_controller([page], mode=mode)
        result = run(ctrl.show_new_tab("p1"))
        assert result["changed"] is False
        assert page.goto_calls == [], f"{mode} sessions must not be navigated"


def test_a_failed_new_tab_navigation_is_reported_but_not_fatal():
    page = FakePage("about:blank", goto_error=RuntimeError("net::ERR_ABORTED"))
    ctrl, context = make_controller([page])
    result = run(ctrl.show_new_tab("p1"))
    assert result["ok"] is False and result["reason"] == "newtab_failed"
    assert context.new_page_calls == 0
    assert not page.is_closed()


def test_missing_session_and_missing_page_are_handled():
    ctrl = BrowserController()
    assert run(ctrl.show_new_tab("nope"))["changed"] is False
    ctrl, _ = make_controller([], tracked=False)
    assert run(ctrl.show_new_tab("p1"))["reason"] == "no_page"


# ---------------------------------------------------------------- launch + open_profile wiring
@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(P, "ROOT", root)
    monkeypatch.setattr(P, "_meta_path", lambda: root / "profiles.json")
    monkeypatch.setattr(chrome, "profile_root", lambda: root / "ChromeProfiles")
    monkeypatch.setattr(controller_module, "USER_DATA_ROOT", root)
    chrome.reset_caches()
    return P


def test_launch_keeps_the_managed_profile_and_does_not_disable_extensions(store):
    profile = store.create_profile({
        "name": "NTP", "user_data_dir": str(store.ROOT / "ntp-profile"), "launch_mode": "managed",
    })
    captured = {}

    class Ctx(FakeContext):
        browser = None

        def set_default_timeout(self, *_): pass
        def set_default_navigation_timeout(self, *_): pass
        async def route(self, *_): pass
        def on(self, *_): pass

    class Chromium:
        async def launch_persistent_context(self, **kwargs):
            captured.update(kwargs)
            return Ctx([FakePage("about:blank")])

    class PW:
        chromium = Chromium()

    ctrl = BrowserController()
    ctrl._playwright = PW()
    run(ctrl._ensure_persistent(profile.id, P.get_profile(profile.id)))
    assert captured["user_data_dir"] == profile.resolved_user_data_dir
    assert not chrome.is_personal_user_data_dir(captured["user_data_dir"])
    assert "--disable-extensions" in captured["ignore_default_args"]
    # No URL is passed through args (Playwright rejects that); the page is fixed afterwards.
    assert not any(not str(a).startswith("-") for a in captured["args"])
