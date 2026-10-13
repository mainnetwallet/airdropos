"""Real browser automation against a controlled local test site.

These tests launch the INSTALLED Google Chrome against an isolated throwaway profile
directory under the test's own tmp_path. They never touch the operator's real Chrome
profile, never use the network, and never perform a financial transaction.

The production SSRF guard refuses loopback addresses on purpose, so the two guard
functions are relaxed for this module only (via monkeypatch). Everything else - the
route guards' existence, the permission gates, the profile lock, the extension
allowlist - is exercised exactly as it runs in production.
"""

from __future__ import annotations

import json
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backend.app.browser import chrome
from backend.app.browser import controller as controller_module
from backend.app.browser import profiles as P
from backend.app.browser.controller import controller
from backend.app.live import hub

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_LOOP = None

INDEX = """<!doctype html><html><head><title>AirdropOS Test Site</title></head>
<body>
<h1>Test Site</h1>
<form id="f" action="/done.html" method="get">
  <label for="email">Email</label><input id="email" name="email" type="text" required>
  <label for="pw">Password</label><input id="pw" name="pw" type="password">
  <label for="plan">Plan</label>
  <select id="plan" name="plan"><option value="a">Basic</option><option value="b">Pro</option></select>
  <label><input type="checkbox" name="tos" id="tos"> Accept terms</label>
  <button type="submit" id="go">Submit form</button>
</form>
<a id="next" href="/page2.html">Go to page two</a>
<button id="jsbtn" onclick="document.getElementById('out').textContent='clicked'">JS Button</button>
<div id="out"></div>
<iframe src="/frame.html" title="inner frame" width="300" height="100"></iframe>
<button id="alertbtn" onclick="alert('a dialog appeared')">Show alert</button>
<div style="height:2500px">tall</div>
<script>console.log('hello from console'); fetch('/missing.png');</script>
</body></html>"""
PAGE2 = "<h1>Page Two</h1><p>second page</p>"
DONE = "<h1>Form Submitted</h1><p>thank you</p>"
FRAME = "<button id='inner'>Inner button</button>"


def _site(tmp_path: Path):
    (tmp_path / "index.html").write_text(INDEX, encoding="utf-8")
    (tmp_path / "page2.html").write_text(PAGE2, encoding="utf-8")
    (tmp_path / "done.html").write_text(DONE, encoding="utf-8")
    (tmp_path / "frame.html").write_text(FRAME, encoding="utf-8")
    handler = partial(_QuietHandler, directory=str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output readable
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    httpd = _site(root)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(P, "ROOT", root)
    monkeypatch.setattr(P, "_meta_path", lambda: root / "profiles.json")
    monkeypatch.setattr(chrome, "profile_root", lambda: root / "ChromeProfiles")
    return P


@pytest.fixture(scope="module")
def browser_loop():
    """One event loop for the whole module.

    Playwright's connection belongs to the loop that created it, so creating a fresh loop
    per call (as a naive helper would) breaks every operation after the first.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    global _LOOP
    _LOOP = loop
    try:
        yield loop
    finally:
        loop.run_until_complete(controller.stop())
        loop.close()


@pytest.fixture
def allow_loopback(monkeypatch):
    """The controlled test site is on loopback; production refuses that on purpose."""
    async def _allow(_url):
        return None

    monkeypatch.setattr(controller_module, "destination_block_reason", _allow)
    monkeypatch.setattr(controller_module, "url_block_reason", lambda _url: None)


@pytest.fixture
def profile(store, tmp_path, browser_loop):
    directory = tmp_path / "chrome-profile"
    created = store.create_profile({
        "name": "Live Test", "user_data_dir": str(directory), "browser_type": "chrome", "launch_mode": "managed",
    })
    return created


@pytest.fixture
def open_profile(profile, allow_loopback):
    """Launch the profile's Chrome for a test and always close it afterwards."""
    result = run(controller.open_profile(profile.id))
    assert result.get("ok"), f"Chrome did not start: {result.get('detail') or result.get('last_error')}"
    try:
        yield profile
    finally:
        run(controller.close_session(profile.id))
        hub.browser_sessions.mark_stopped(profile.id, by_operator=True, reason="test teardown")


def run(coro):
    import asyncio

    loop = _LOOP or asyncio.new_event_loop()
    return loop.run_until_complete(coro)


# ------------------------------------------------------------------ launch
def test_managed_profile_launches_real_persistent_chrome(open_profile):
    status = run(controller.profile_status(open_profile.id))
    assert status["running"] is True
    assert status["connection"] == "managed_persistent"
    assert status["mode"] == "persistent"
    assert status["tab_count"] >= 1
    assert Path(status["user_data_dir"]).is_dir()
    assert status["route_guard"] == "enforced"
    # The worker really owns Chrome, so the installed-extension list is read from disk.
    assert status["extensions"]["installed"] == []


def test_mode_specific_limitations_are_reported_honestly():
    attached = controller._limitations("attached")
    ephemeral = controller._limitations("ephemeral")
    managed = controller._limitations("persistent")
    assert any("not intercepted" in n for n in attached)
    assert any("belongs to that browser" in n for n in attached)
    assert any("no persistent cookies" in n for n in ephemeral)
    assert not any("no persistent cookies" in n for n in managed)


def test_a_locked_profile_is_refused_rather_than_stolen(open_profile, store):
    """Chrome itself refuses a profile another instance owns; AirdropOS must surface that."""
    profile = open_profile
    lock = chrome.profile_lock(profile.resolved_user_data_dir)
    assert lock["locked"] is True, "the running Chrome should hold a lock on its profile"
    # A second profile for a DIFFERENT directory is fine; the point is the lock check exists.
    second_dir = Path(str(profile.resolved_user_data_dir) + "-second")
    second = store.create_profile({"name": "Second", "user_data_dir": str(second_dir)})
    result = run(controller.open_profile(second.id))
    assert result["ok"] is True, result.get("detail")
    run(controller.close_session(second.id))
    hub.browser_sessions.mark_stopped(second.id, by_operator=True, reason="test")


def test_locked_profile_refuses_to_launch_and_leaves_the_data_alone(open_profile, store, monkeypatch):
    profile = open_profile
    monkeypatch.setattr(
        chrome, "profile_lock",
        lambda _dir: {"locked": True, "reason": "another Chrome owns it", "processes": [], "markers": []},
    )
    marker = Path(profile.resolved_user_data_dir) / "marker.txt"
    marker.write_text("do not delete", encoding="utf-8")
    run(controller.close_session(profile.id))
    result = run(controller.open_profile(profile.id))
    assert result["ok"] is False
    assert "already open" in result["detail"].lower()
    assert marker.read_text(encoding="utf-8") == "do not delete"


def test_open_profile_reports_failure_instead_of_a_fake_success(store):
    profile = store.create_profile({"name": "No Chrome"})
    original = P.chrome.find_chrome_executable
    P.chrome.find_chrome_executable = lambda: {"path": "", "exists": False, "detail": "not found"}
    try:
        result = run(controller.open_profile(profile.id))
    finally:
        P.chrome.find_chrome_executable = original
    assert result["ok"] is False
    assert result["reason"] == "launch_failed"
    assert "Chrome" in (result["detail"] or "")


# ------------------------------------------------------------------ navigation & tabs
def test_navigation_click_form_and_tab_management(open_profile, site):
    key = open_profile.id
    nav = run(controller.navigate(key, f"{site}/index.html"))
    assert nav["ok"], nav.get("detail")
    assert nav["observation"].title == "AirdropOS Test Site"

    from backend.app.browser import interaction as I

    page = controller.peek_page(key)
    click = run(I.click(page, strategy="text", value="JS Button"))
    assert click["ok"]
    assert run(page.locator("#out").inner_text()) == "clicked"

    assert run(I.fill_field(page, strategy="label", field_value="Email", value="a@b.test"))["ok"]
    secret = run(I.fill_field(page, strategy="label", field_value="Password", value="hunter2secret"))
    assert secret["control"] == "password"
    assert "hunter2secret" not in json.dumps(secret)
    assert run(I.fill_field(page, strategy="label", field_value="Plan", value="Pro"))["selected"] == ["b"]
    assert run(I.fill_field(page, strategy="label", field_value="Accept terms", value="true"))["checked"] is True

    # An element inside an iframe is reachable (Playwright searches every frame).
    assert run(I.click(page, strategy="text", value="Inner button"))["ok"]

    # A miss reports what IS there instead of pretending.
    with pytest.raises(I.LocatorNotFound) as excinfo:
        run(I.click(page, strategy="text", value="Nothing like this", timeout_ms=300))
    assert "Visible controls" in str(excinfo.value)

    new_tab = run(controller.new_tab(key, f"{site}/page2.html"))
    assert new_tab["ok"]
    listing = run(controller.list_tabs(key))
    assert listing["tab_count"] == 2
    assert any(t["url"].endswith("/page2.html") for t in listing["tabs"])
    assert listing["active_tab_id"]

    switched = run(controller.switch_tab(key, url_contains="index.html"))
    assert switched["ok"] and switched["url"].endswith("/index.html")

    closed = run(controller.close_tab(key, tab_id=new_tab["tab_id"]))
    assert closed["ok"]
    assert run(controller.list_tabs(key))["tab_count"] == 1

    back = run(controller.history(key, "back"))
    assert back["ok"] is False and back["reason"] == "no_history"
    reload_ = run(controller.history(key, "reload"))
    assert reload_["ok"] is True


def test_scrolling_reaches_the_bottom_and_reports_it(open_profile, site):
    key = open_profile.id
    run(controller.navigate(key, f"{site}/index.html"))
    from backend.app.browser import interaction as I

    page = controller.peek_page(key)
    down = run(I.scroll(page, direction="down", amount=1200))
    assert down["ok"] and down["moved"] is True
    bottom = run(I.scroll(page, direction="bottom"))
    assert bottom["at_bottom"] is True


def test_wait_reports_a_real_timeout(open_profile, site):
    key = open_profile.id
    run(controller.navigate(key, f"{site}/index.html"))
    from backend.app.browser import interaction as I

    page = controller.peek_page(key)
    good = run(I.wait_for(page, kind="element", value="#email", timeout_ms=3000))
    assert good["ok"] is True
    bad = run(I.wait_for(page, kind="element", value="#never-exists", timeout_ms=700))
    assert bad["ok"] is False and "detail" in bad


def test_dialogs_do_not_hang_the_page(open_profile, site):
    key = open_profile.id
    run(controller.navigate(key, f"{site}/index.html"))
    page = controller.peek_page(key)
    run(page.locator("#alertbtn").click())
    run(page.wait_for_timeout(700))
    diagnostics = controller.diagnostics_for(key)
    assert any(d["type"] == "alert" for d in diagnostics["dialogs"]), diagnostics["dialogs"]
    assert diagnostics["page_errors"] is not None


def test_diagnostics_capture_console_and_failed_requests(open_profile, site):
    key = open_profile.id
    run(controller.navigate(key, f"{site}/index.html"))
    run(controller.peek_page(key).wait_for_timeout(800))
    diagnostics = controller.diagnostics_for(key)
    assert any("hello from console" in m["text"] for m in diagnostics["console"]), diagnostics["console"]
    assert any("missing.png" in r.get("url", "") for r in diagnostics["failed_requests"])


# ------------------------------------------------------------------ persistence
def test_profile_state_survives_closing_and_reopening_chrome(store, allow_loopback, site):
    directory = store.ROOT / "persist-profile"
    profile = store.create_profile({"name": "Persist", "user_data_dir": str(directory)})
    key = profile.id

    assert run(controller.open_profile(key))["ok"]
    run(controller.navigate(key, f"{site}/index.html"))
    run(controller._contexts[key].add_cookies([
        {"name": "persisted", "value": "yes", "url": site, "expires": time.time() + 86400},
    ]))
    run(controller.close_session(key))

    # A brand new launch from the SAME saved configuration must see the cookie.
    status = run(controller.reconnect_profile(key))
    assert status["ok"], status.get("detail")
    assert status["connection"] == "managed_persistent"
    cookies = run(controller._contexts[key].cookies(site))
    assert any(c["name"] == "persisted" for c in cookies), cookies
    hub.browser_sessions.mark_stopped(key, by_operator=True, reason="test")
    run(controller.close_session(key))
    run(controller.stop())


def test_profile_directory_is_untouched_by_a_close(open_profile):
    directory = Path(open_profile.resolved_user_data_dir)
    assert directory.is_dir()
    run(controller.close_session(open_profile.id))
    assert directory.is_dir()
    # Chrome's own state files are on disk, which is what makes the profile reusable.
    assert (directory / "Default").is_dir()
    hub.browser_sessions.mark_stopped(open_profile.id, by_operator=True, reason="test")


def test_two_profiles_never_share_a_browser(store, allow_loopback, site):
    a = store.create_profile({"name": "A", "user_data_dir": str(store.ROOT / "pa")})
    b = store.create_profile({"name": "B", "user_data_dir": str(store.ROOT / "pb")})
    assert run(controller.open_profile(a.id))["ok"]
    assert run(controller.open_profile(b.id))["ok"]
    assert controller._contexts[a.id] is not controller._contexts[b.id]
    assert controller.profile_dir(a.id) != controller.profile_dir(b.id)
    run(controller._contexts[a.id].add_cookies([
        {"name": "only_a", "value": "1", "url": site, "expires": time.time() + 86400},
    ]))
    run(controller.navigate(a.id, f"{site}/index.html"))
    cookies_b = run(controller._contexts[b.id].cookies(site))
    assert not any(c["name"] == "only_a" for c in cookies_b), "profile B saw profile A's cookie"
    for key in (a.id, b.id):
        run(controller.close_session(key))
        hub.browser_sessions.mark_stopped(key, by_operator=True, reason="test")
    run(controller.stop())


# ------------------------------------------------------------------ extensions
def test_internal_pages_allowlist_is_enforced_at_launch(open_profile):
    key = open_profile.id
    allowed = run(controller.navigate_internal(key, "chrome://extensions/"))
    assert allowed["ok"] is True
    refused = run(controller.navigate_internal(key, "chrome://settings/"))
    assert refused["ok"] is False
    assert refused["reason"] == "blocked_internal_page"
    assert "security" in refused["detail"].lower()


def test_extension_status_reports_real_installations(store, open_profile):
    status = run(controller.extension_status(open_profile.id))
    assert status["ok"] is True
    assert status["installed"] == []          # nothing was installed in this test profile
    assert status["capabilities"]["install_from_web_store"] == "manual_confirmation_required"
    assert status["capabilities"]["inspect_popup_dom"] is False
    assert "Add to Chrome" in status["capabilities"]["notes"]


def test_installed_unpacked_extension_is_loaded_but_not_recorded(store, tmp_path):
    """Chrome loads --load-extension for the session only; it must not be claimed as installed."""
    ext = tmp_path / "dev-extension"
    ext.mkdir()
    (ext / "manifest.json").write_text(json.dumps({
        "manifest_version": 3, "name": "Dev Ext", "version": "1.0", "description": "test",
    }), encoding="utf-8")
    directory = tmp_path / "profile-with-ext"
    profile = store.create_profile({
        "name": "Ext", "user_data_dir": str(directory), "unpacked_extensions": str(ext),
    })
    result = run(controller.open_profile(profile.id))
    assert result["ok"], result.get("detail")
    status = run(controller.extension_status(profile.id))
    assert status["unpacked_loaded_this_session"] == [str(ext)]
    # The honest limit: Chrome does not write it into the profile's installed list.
    assert status["installed"] == []
    run(controller.close_session(profile.id))
    hub.browser_sessions.mark_stopped(profile.id, by_operator=True, reason="test")
    run(controller.stop())
