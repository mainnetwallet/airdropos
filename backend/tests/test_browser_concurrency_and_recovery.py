"""Browser concurrency and session-recovery regression tests (real Playwright + Chromium).

Every test drives a REAL Chromium launched through ``BrowserController`` with a managed
persistent profile in a temp directory. Pages are local (``set_content``), nothing touches the
network and no operator profile is used. When Playwright's Chromium cannot launch in the
environment the browser tests SKIP (they never silently pass).

Defects these tests pin (each failed before its fix):

* Two tool calls on the same browser session ran at the same time. The per-key lock only guarded
  context/page creation and closing, so their keystrokes interleaved on the shared page
  ("ABABAB...").
* ``open_profile`` / ``reconnect_profile`` could not recover a session whose context had closed
  behind the controller's back (operator closed the window, Chrome crashed, an attached Chrome
  restarted): ``_ensure_context`` kept returning the dead cached context, so the result was
  ``launch_failed: Chrome started but no page is available``.
* Closing a session's only tab by id left ``None`` in the page map, after which ``open_keys()``
  raised and the worker's shutdown skipped ``stop()``.
* ``peek_page`` ignored a surviving tab when the active tab had closed, so the session reconciler
  tore down a session that still had a live tab.

The rest (profile reuse, page adoption, stale-tab and killed-Chrome recovery, concurrent
``get_page``) already worked; those tests are guards so they stay that way.
"""

import asyncio
import subprocess
import socket
import tempfile
import time

import pytest

from backend.app.browser import chrome
from backend.app.browser import profiles as profile_store
from backend.app.browser.controller import BrowserController
from backend.app.core.config import settings
from backend.app.live import hub
from backend.app.live.providers import browser as live_browser
from backend.app.tools import browser_tools as bt
from backend.app.tools.base import ToolResultStatus

KEYLOG_PAGE = (
    "<input id=f autofocus><script>"
    "window.log=[];window.stamps=[];"
    "document.getElementById('f').addEventListener('keydown',function(e){"
    "window.log.push(e.key);window.stamps.push(performance.timeOrigin+performance.now());});"
    "</script>"
)
COOKIE = {"name": "sid", "value": "abc123", "domain": "example.test", "path": "/"}


def _chromium_launches() -> bool:
    async def probe() -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox"])
            await browser.close()

    try:
        asyncio.run(probe())
        return True
    except Exception:
        return False


_CHROMIUM_OK = _chromium_launches()


class Env:
    def __init__(self, controller: BrowserController, root) -> None:
        self.controller = controller
        self.root = root
        self.keys = []

    def profile(self, name: str, **extra) -> str:
        data = {"name": name, "browser_type": "chromium", "headless": True, **extra}
        profile = profile_store.create_profile(data)
        self.keys.append(profile.id)
        return profile.id

    async def page(self, key: str, html: str = KEYLOG_PAGE):
        page = await self.controller.get_page(key)
        assert page is not None, self.controller._last_error
        await page.set_content(html)
        await page.focus("#f")
        return page


def _kill_profile_chrome(user_data_dir: str) -> None:
    import psutil

    for entry in chrome.chrome_processes_for(user_data_dir):
        try:
            psutil.Process(entry["pid"]).kill()
        except Exception:
            pass


@pytest.fixture
async def env(tmp_path, monkeypatch):
    if not _CHROMIUM_OK:
        pytest.skip("Playwright Chromium cannot launch in this environment")
    meta = tmp_path / "meta"
    meta.mkdir()
    monkeypatch.setattr(profile_store, "ROOT", meta)
    monkeypatch.setattr(settings, "BROWSER_PROFILE_ROOT", str(tmp_path / "chrome"))
    monkeypatch.setattr(settings, "BROWSER_PERSISTENT_PROFILES", True)
    monkeypatch.setattr(settings, "BROWSER_ALLOW_ATTACH", True)
    controller = BrowserController()
    monkeypatch.setattr(bt, "controller", controller)
    monkeypatch.setattr(live_browser, "controller", controller)
    holder = Env(controller, tmp_path)
    yield holder
    try:
        await asyncio.wait_for(controller.stop(), 30)
    except Exception:
        pass
    for key in holder.keys:
        profile = profile_store.get_profile(key)
        if profile is not None:
            _kill_profile_chrome(profile.resolved_user_data_dir)
        hub.browser_sessions.records.pop(key, None)


async def type_into(key: str, text: str, delay_ms: int = 30):
    return await bt.TypeTool().execute({"text": text, "delay_ms": delay_ms, "session_key": key})


async def keylog(page) -> str:
    return await page.evaluate("window.log.join('')")


async def _drain(page, quiet_s: float = 0.5, limit_s: float = 30.0) -> None:
    """Wait until the page has stopped receiving keystrokes."""
    deadline = time.time() + limit_s
    last = -1
    stable_since = time.time()
    while time.time() < deadline:
        count = await page.evaluate("window.log.length")
        if count != last:
            last, stable_since = count, time.time()
        elif time.time() - stable_since >= quiet_s:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the page kept receiving keystrokes")


# ======================================================================
# Concurrent browser actions
# ======================================================================
def test_interaction_lock_is_per_session_and_normalised():
    controller = BrowserController()
    a = controller.interaction_lock("acct-a")
    assert controller.interaction_lock("acct-a") is a
    assert controller.interaction_lock("acct-b") is not a
    # Same normalisation as every other per-session structure: an unsafe key cannot dodge the lock.
    assert controller.interaction_lock("a b") is controller.interaction_lock("a b")
    # A separate lock from the lifecycle one: nesting the two in the documented order must not clash.
    assert a is not controller._lock_for("acct-a")


async def test_stop_resets_interaction_locks():
    controller = BrowserController()
    first = controller.interaction_lock("acct-a")
    await controller.stop()
    # asyncio locks bind to a loop; a restart must hand out fresh ones like the lifecycle locks.
    assert controller.interaction_lock("acct-a") is not first


async def test_same_session_typing_is_serialised(env):
    key = env.profile("race")
    page = await env.page(key)
    first, second = await asyncio.wait_for(
        asyncio.gather(type_into(key, "A" * 10, 40), type_into(key, "B" * 10, 40)), 60
    )
    assert first.status is ToolResultStatus.SUCCESS and second.status is ToolResultStatus.SUCCESS
    assert await keylog(page) in ("A" * 10 + "B" * 10, "B" * 10 + "A" * 10)


async def test_different_sessions_still_run_in_parallel(env):
    key_a, key_b = env.profile("par-a"), env.profile("par-b")
    page_a, page_b = await env.page(key_a), await env.page(key_b)
    await asyncio.wait_for(asyncio.gather(type_into(key_a, "A" * 12, 40), type_into(key_b, "B" * 12, 40)), 60)
    a = await page_a.evaluate("window.stamps")
    b = await page_b.evaluate("window.stamps")
    assert await keylog(page_a) == "A" * 12 and await keylog(page_b) == "B" * 12
    # The two typing runs overlapped in wall-clock time: one session's lock never blocks another.
    assert a[0] < b[-1] and b[0] < a[-1]


async def test_lock_is_released_after_failure_cancellation_and_queued_cancel(env):
    key = env.profile("release")
    page = await env.page(key)

    # A failing call (element never appears) releases the lock.
    failed = await bt.TypeTool().execute(
        {"text": "x", "strategy": "css", "value": "#nope", "timeout_ms": 300, "session_key": key}
    )
    assert failed.status is not ToolResultStatus.SUCCESS
    assert not env.controller.interaction_lock(key).locked()

    # A call cut off by a timeout (what the registry does) releases it too.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(type_into(key, "T" * 40, 100), 0.5)
    assert not env.controller.interaction_lock(key).locked()

    # Cancelling the Python side does not stop Playwright's server-side typing loop, so the cut-off
    # call above keeps typing to its end. Let that drain; it is Playwright behaviour the lock
    # cannot change, and it is not what this test is about.
    await _drain(page)
    await page.evaluate("window.log.length = 0")

    # A waiter cancelled while queued neither types nor wedges the lock.
    holder = asyncio.create_task(type_into(key, "A" * 12, 40))
    await asyncio.sleep(0.15)
    queued = asyncio.create_task(type_into(key, "B" * 5, 40))
    await asyncio.sleep(0.1)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert (await asyncio.wait_for(holder, 30)).status is ToolResultStatus.SUCCESS
    after = await asyncio.wait_for(type_into(key, "C" * 3, 20), 30)
    assert after.status is ToolResultStatus.SUCCESS
    assert await keylog(page) == "A" * 12 + "C" * 3


async def test_close_session_is_not_blocked_by_an_inflight_interaction(env):
    """Lifecycle lock and interaction lock must never wait on each other."""
    key = env.profile("deadlock")
    await env.page(key)
    inflight = asyncio.create_task(type_into(key, "Z" * 40, 100))
    await asyncio.sleep(0.5)
    await asyncio.wait_for(env.controller.close_session(key), 40)
    result = await asyncio.wait_for(inflight, 40)
    assert result.status is not ToolResultStatus.SUCCESS  # its page was closed under it
    assert not env.controller.interaction_lock(key).locked()
    # And the session is usable again afterwards.
    page = await env.page(key)
    done = await asyncio.wait_for(type_into(key, "ok", 10), 30)
    assert done.status is ToolResultStatus.SUCCESS and await keylog(page) == "ok"


def test_page_tools_are_serialised_and_lifecycle_tools_are_not():
    serialised = {t.name for t in bt.BROWSER_TOOLS if getattr(type(t).execute, "_serialises_per_session", False)}
    expected = {
        "browser_navigate", "browser_history", "browser_tabs", "inspect_page", "browser_snapshot",
        "browser_find", "browser_click", "browser_click_at", "browser_hover", "browser_drag",
        "browser_type", "browser_press", "browser_scroll", "browser_fill_form", "browser_fill_field",
        "browser_read_value", "browser_submit", "browser_upload_file", "capture_evidence",
        "browser_extensions", "browser_wallet",
    }
    names = {t.name for t in bt.BROWSER_TOOLS}
    assert expected <= names, f"test is stale, unknown tools: {expected - names}"
    # Lifecycle tools take the controller's lifecycle lock (and must stay able to close a busy
    # session); wait/diagnostics/dialog listing must not hold a session while idle.
    assert serialised == expected, (serialised ^ expected)


# ======================================================================
# Session recovery
# ======================================================================
async def test_persistent_profile_is_reused_and_first_tab_adopted(env):
    key = env.profile("reuse")
    page = await env.controller.get_page(key)
    await page.context.add_cookies([{**COOKIE, "expires": time.time() + 86400}])
    directory = env.controller.profile_dir(key)
    await env.controller.close_session(key)
    assert env.controller.peek_page(key) is None

    reopened = await env.controller.get_page(key)
    assert env.controller.session_mode(key) == "persistent"
    assert env.controller.profile_dir(key) == directory
    assert [c["value"] for c in await reopened.context.cookies() if c["name"] == "sid"] == ["abc123"]
    assert len(reopened.context.pages) == 1  # adopted Chrome's own tab, no stray blank second one


async def test_get_page_adopts_a_surviving_tab_when_the_active_one_closed(env):
    key = env.profile("stale-get")
    await env.controller.get_page(key)
    await env.controller.new_tab(key)
    active = env.controller._pages[key]
    await active.close()
    page = await env.controller.get_page(key)
    assert not page.is_closed() and len(page.context.pages) == 1


async def test_peek_page_adopts_a_surviving_tab_without_opening_anything(env):
    key = env.profile("stale-peek")
    await env.controller.get_page(key)
    await env.controller.new_tab(key)
    first, second = env.controller._contexts[key].pages
    env.controller._pages[key] = first
    await first.close()
    survivor = env.controller.peek_page(key)
    assert survivor is second
    assert env.controller._pages[key] is second
    assert len(env.controller._contexts[key].pages) == 1  # peeking never opens a tab


async def test_reconcile_keeps_a_session_that_still_has_a_live_tab(env):
    key = env.profile("reconcile")
    await env.controller.get_page(key)
    await env.controller.new_tab(key)
    first, _second = env.controller._contexts[key].pages
    env.controller._pages[key] = first
    await first.close()  # the operator closed the tab the agent was on; another tab is still open
    record = hub.browser_sessions.records[key]
    record.created_mono -= 60  # past the reconciler's start-up grace period
    await live_browser.BrowserProvider().reconcile()
    assert not record.stopped
    assert key in env.controller._contexts and env.controller.peek_page(key) is not None


async def test_closing_the_only_tab_does_not_break_open_keys(env):
    key = env.profile("lasttab")
    page = await env.controller.get_page(key)
    result = await env.controller.close_tab(key, tab_id=env.controller._tab_id(page))
    assert result["ok"] is True
    assert env.controller.open_keys() == []  # used by the worker's shutdown before stop()
    assert key not in env.controller._pages
    again = await env.controller.get_page(key)  # and the session still recovers a page
    assert again is not None and not again.is_closed()


async def test_get_page_recovers_when_every_tab_is_closed(env):
    key = env.profile("alltabs")
    page = await env.controller.get_page(key)
    await page.context.add_cookies([{**COOKIE, "expires": time.time() + 86400}])
    await page.close()
    recovered = await asyncio.wait_for(env.controller.get_page(key), 60)
    assert recovered is not None and not recovered.is_closed()
    assert [c["value"] for c in await recovered.context.cookies() if c["name"] == "sid"] == ["abc123"]


async def test_get_page_recovers_after_the_context_was_closed_behind_its_back(env):
    key = env.profile("ctxclosed")
    page = await env.controller.get_page(key)
    await page.context.add_cookies([{**COOKIE, "expires": time.time() + 86400}])
    await page.context.close()
    recovered = await asyncio.wait_for(env.controller.get_page(key), 60)
    assert recovered is not None and not recovered.is_closed()
    assert [c["value"] for c in await recovered.context.cookies() if c["name"] == "sid"] == ["abc123"]


async def test_get_page_recovers_after_chrome_is_killed(env):
    key = env.profile("killed")
    page = await env.controller.get_page(key)
    await page.context.add_cookies([{**COOKIE, "expires": time.time() + 86400}])
    _kill_profile_chrome(env.controller.profile_dir(key))
    await asyncio.sleep(1)
    recovered = await asyncio.wait_for(env.controller.get_page(key), 60)
    assert recovered is not None and not recovered.is_closed()
    assert env.controller.session_mode(key) == "persistent"


async def test_concurrent_get_page_creates_one_context(env):
    key = env.profile("single")
    pages = await asyncio.wait_for(asyncio.gather(*[env.controller.get_page(key) for _ in range(6)]), 60)
    assert len({id(p) for p in pages}) == 1
    assert len(env.controller._contexts) == 1 and len(pages[0].context.pages) == 1


@pytest.mark.parametrize("entry", ["reconnect_profile", "open_profile"])
async def test_profile_can_be_reconnected_after_its_context_closed(env, entry):
    key = env.profile(f"reconnect-{entry}".replace("_", "-"))
    page = await env.controller.get_page(key)
    healthy = await env.controller.reconnect_profile(key)
    assert healthy["ok"] and healthy["reconnected_to"] == "running_session"

    await page.context.close()  # the window was closed / Chrome went away
    result = await asyncio.wait_for(getattr(env.controller, entry)(key), 60)
    assert result["ok"], result
    assert result.get("running") is True
    live = env.controller.peek_page(key)
    assert live is not None and not live.is_closed()


async def test_attached_chrome_can_be_reconnected_after_it_restarts(env):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        binary = p.chromium.executable_path
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    data_dir = tempfile.mkdtemp(prefix="airdropos-remote-")
    procs = []

    def start() -> None:
        procs.append(subprocess.Popen(
            [binary, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={data_dir}",
             "--no-sandbox", "--no-first-run", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))

    async def wait_port() -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                _r, w = await asyncio.open_connection("127.0.0.1", port)
                w.close()
                return
            except OSError:
                await asyncio.sleep(0.2)
        raise AssertionError("remote Chrome never opened its DevTools port")

    try:
        start()
        await wait_port()
        key = env.profile("attach", launch_mode="attach", debugger_host="127.0.0.1", debugger_port=port)
        opened = await env.controller.open_profile(key)
        assert opened["ok"], opened

        procs[-1].kill()
        procs[-1].wait()
        await asyncio.sleep(1)
        start()  # the operator restarted their Chrome on the same DevTools port
        await wait_port()

        result = await asyncio.wait_for(env.controller.reconnect_profile(key), 60)
        assert result["ok"], result
        live = env.controller.peek_page(key)
        assert live is not None and not live.is_closed()
    finally:
        for proc in procs:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass
        pass
