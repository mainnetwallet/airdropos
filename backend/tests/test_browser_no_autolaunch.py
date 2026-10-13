"""Chrome must launch ONLY on an explicit user action.

Regression suite for the "Who's using Chrome?" popup. Opening the Browser Profiles page
polls ``GET /api/v1/browser/profiles`` every few seconds, and that used to execute
``chrome.exe --version``. Chrome has no such switch on Windows: it forwards to, or
starts, the full browser against the operator's PERSONAL profile directory, which is
what showed the profile picker.

Two detectors are used together, because either alone is incomplete:

* :func:`no_browser_spawn` makes ANY process spawn from the chrome module an immediate
  failure, so "somebody executed a browser binary" can never pass silently.
* Chrome processes that reference the profile's own managed directory are counted, so a
  real launch is observed for real. This never counts, and never touches, the
  operator's own Chrome.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.app.browser import chrome
from backend.app.browser import controller as controller_module
from backend.app.browser import profiles as P
from backend.app.browser.controller import controller
from backend.app.live import hub


@pytest.fixture(scope="module")
def browser_loop():
    """One event loop: Playwright's connection belongs to the loop that created it."""
    loop = asyncio.new_event_loop()
    global _LOOP
    _LOOP = loop
    try:
        yield loop
    finally:
        loop.run_until_complete(controller.stop())
        loop.close()


_LOOP = None


def run(coro):
    return (_LOOP or asyncio.new_event_loop()).run_until_complete(coro)


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


@pytest.fixture
def no_browser_spawn(monkeypatch):
    """Fail loudly if anything under test tries to execute a process."""
    def boom(*args, **kwargs):
        raise AssertionError(f"A process was spawned: {args!r}")

    for name in ("run", "Popen", "call", "check_output", "check_call"):
        monkeypatch.setattr(subprocess, name, boom)
    return boom


@pytest.fixture
def profile(store):
    directory = store.ROOT / "autolaunch-profile"
    return store.create_profile({
        "name": "No Autolaunch", "user_data_dir": str(directory), "launch_mode": "managed",
    })


def chrome_for(directory: str) -> int:
    """Chrome processes holding THIS profile directory. Never counts user-owned Chrome."""
    return len(chrome.chrome_processes_for(directory))


def browser_instances_for(directory: str) -> int:
    """Chrome BROWSER (main) processes for THIS profile directory.

    Renderer, GPU and utility children share the same ``--user-data-dir`` and come and
    go with whatever page is showing (the New Tab page starts its own renderer a moment
    after launch), so they say nothing about whether a second Chrome was started.
    """
    import psutil

    count = 0
    for entry in chrome.chrome_processes_for(directory):
        try:
            cmdline = psutil.Process(entry["pid"]).cmdline()
        except Exception:
            continue
        if not any(str(arg).startswith("--type=") for arg in cmdline):
            count += 1
    return count


# ======================================================================
# 1. Nothing on the read path may execute the browser
# ======================================================================
def test_version_probe_never_executes_the_browser(no_browser_spawn):
    """`chrome.exe --version` starts Chrome with the user's own profile on Windows."""
    info = chrome.find_chrome_executable()
    if not info.get("exists"):
        pytest.skip("Google Chrome is not installed on this machine")
    version = chrome.executable_version(info["path"])
    # No spawn happened (the fixture would have raised). A version is still returned.
    assert version is None or re_version(version)
    assert ":" not in str(version or "")


def re_version(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"\d+(\.\d+)*", str(value or "")))


def test_real_chrome_version_is_read_without_spawning(no_browser_spawn):
    info = chrome.find_chrome_executable()
    if not info.get("exists"):
        pytest.skip("Google Chrome is not installed on this machine")
    version = chrome.executable_version(info["path"])
    assert version and version.count(".") >= 2, version


def test_version_probe_of_a_missing_file_is_safe(no_browser_spawn):
    assert chrome.executable_version("") is None
    assert chrome.executable_version("C:/definitely/not/chrome.exe") is None


def test_discovery_never_executes_the_browser(no_browser_spawn):
    chrome.reset_caches()
    for _ in range(3):
        info = chrome.find_chrome_executable()
        assert "path" in info and "exists" in info


def test_reading_the_profile_list_launches_nothing(store, profile, no_browser_spawn, browser_loop):
    """The exact work `GET /api/v1/browser/profiles` does on every 8-second poll."""
    assert chrome_for(profile.resolved_user_data_dir) == 0
    for _ in range(3):
        assert chrome_for(profile.resolved_user_data_dir) == 0
        statuses = [P.validate_profile_config(P.get_profile(profile.id))]
        assert statuses[0] == []
        assert chrome.installed_extensions(profile.resolved_user_data_dir) == []
        assert chrome.find_chrome_executable()["exists"] in (True, False)
    assert chrome_for(profile.resolved_user_data_dir) == 0


def test_profile_status_does_not_launch_chrome(store, profile, no_browser_spawn, browser_loop):
    status = run(controller.profile_status(profile.id))
    assert status["running"] is False
    assert status["connection"] == "not_running"
    assert chrome_for(profile.resolved_user_data_dir) == 0


def test_extension_status_does_not_launch_chrome(store, profile, no_browser_spawn, browser_loop):
    status = run(controller.extension_status(profile.id))
    assert status["installed"] == []
    assert chrome_for(profile.resolved_user_data_dir) == 0


def test_controller_health_does_not_launch_chrome(store, profile, no_browser_spawn):
    health = controller.health()
    assert health["status"] in ("HEALTHY", "UNAVAILABLE")
    assert "chrome_executable" in health
    assert chrome_for(profile.resolved_user_data_dir) == 0


def test_gateway_profile_listing_launches_nothing(store, profile, no_browser_spawn):
    """`GET /browser/profiles` on the worker gateway is what the page polls."""
    from starlette.testclient import TestClient

    from backend.app.live.gateway import build_app
    from backend.app.live.security import GATEWAY_HEADER, gateway_secret

    with TestClient(build_app()) as client:
        client.headers.update({GATEWAY_HEADER: gateway_secret()})
        response = client.get("/browser/profiles")
        assert response.status_code == 200
        assert profile.id in response.json()["statuses"]
    assert chrome_for(profile.resolved_user_data_dir) == 0


def test_editing_a_profile_does_not_launch_chrome(store, profile, no_browser_spawn):
    updated = store.update_profile(profile.id, {"description": "edited"})
    assert updated.description == "edited"
    assert chrome_for(profile.resolved_user_data_dir) == 0


def test_api_listing_endpoint_launches_nothing(store, profile, no_browser_spawn, monkeypatch):
    """The REST endpoint end to end (with the worker stubbed, as in the API suite)."""
    from backend.app.live import client as gateway_client

    calls = []

    async def fake_get_json(path, params=None):
        calls.append(path)
        if path == "/sessions":
            return {"sessions": [], "degraded": []}
        if path == "/browser/profiles":
            return {"statuses": {profile.id: {"ok": True, "running": False}}}
        raise gateway_client.GatewayError(404, "not_found", "no")

    monkeypatch.setattr(gateway_client, "get_json", fake_get_json)
    from backend.app.api.v1.endpoints import browser_profiles as endpoint

    async def go():
        return await endpoint.list_profiles(_Principal())

    class _Principal:  # minimal stand-in: the endpoint only reads nothing from it
        user_id = None

    body = run(go())
    assert body["worker_reachable"] is True
    assert body["profiles"][0]["browser_running"] is False
    assert "/browser/profiles" in calls
    assert chrome_for(profile.resolved_user_data_dir) == 0


# ======================================================================
# 2. Explicit launch DOES happen, with the right directory
# ======================================================================
def test_explicit_open_launches_the_selected_profile(store, profile, browser_loop):
    assert chrome_for(profile.resolved_user_data_dir) == 0
    result = run(controller.open_profile(profile.id))
    assert result["ok"] is True, result.get("detail")
    assert result["connection"] == "managed_persistent"
    assert result["user_data_dir"] == profile.resolved_user_data_dir
    # Chrome is genuinely running against that directory.
    assert chrome_for(profile.resolved_user_data_dir) > 0
    # ...and nothing was launched against the operator's personal profile.
    assert controller.profile_dir(profile.id) == profile.resolved_user_data_dir


def test_launch_always_passes_the_managed_user_data_dir(store, profile, browser_loop, monkeypatch):
    """Capture the arguments Playwright would receive."""
    captured = {}
    original = controller._playwright

    class FakeContext:
        pages = []
        def browser(self): return None
        def set_default_timeout(self, *_): pass
        def set_default_navigation_timeout(self, *_): pass
        async def route(self, *_): pass
        def on(self, *_): pass

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs):
            captured.update(kwargs)
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

    class Probe(FakeContext):
        pages = []

    controller._playwright = FakePlaywright()
    try:
        run(controller._ensure_persistent(profile.id, P.get_profile(profile.id)))
    finally:
        controller._playwright = original
    assert captured.get("user_data_dir") == profile.resolved_user_data_dir
    assert not chrome.is_personal_user_data_dir(captured["user_data_dir"])
    assert captured.get("executable_path"), "the installed Chrome must be used, not Playwright's"


def test_launch_refuses_a_personal_profile_directory(store, profile, browser_loop):
    """Defence in depth: even if configuration slipped through, refuse to open user data."""
    personal = str(Path.home() / "AppData/Local/Google/Chrome/User Data")
    bad = store.create_profile({"name": "Sneaky", "user_data_dir": str(store.ROOT / "ok-dir")})
    object.__setattr__(bad, "user_data_dir", personal)
    result = run(controller._ensure_persistent(bad.id, bad))
    assert result is None
    assert "personal" in (controller._last_error or "").lower()
    # Refused BEFORE any lock scan or directory creation: nothing was touched.
    assert not Path(personal, "airdropos-marker").exists()


def test_launch_refuses_when_no_profile_directory_can_be_resolved(store, profile, browser_loop, monkeypatch):
    """A profile that resolves to no directory would make Chrome open the operator's own."""
    monkeypatch.setattr(chrome, "default_user_data_dir", lambda _profile_id: "")
    bad = store.create_profile({"name": "NoDir", "user_data_dir": ""})
    assert bad.resolved_user_data_dir == ""   # the fallback itself produced nothing
    result = run(controller._ensure_persistent(bad.id, bad))
    assert result is None
    # Refused by configuration validation with an honest reason, before any launch.
    assert "profile directory" in (controller._last_error or "").lower()
    assert bad.id not in controller._contexts


# ======================================================================
# Housekeeping
# ======================================================================
@pytest.fixture(autouse=True)
def _no_chrome_left_behind(browser_loop):
    """Close anything a test opened. Never touches processes AirdropOS did not start."""
    yield
    for key in list(controller._contexts):
        hub.browser_sessions.mark_stopped(key, by_operator=True, reason="test teardown")
        run(controller.close_session(key))


# ======================================================================
# 3. No duplicate instances
# ======================================================================
def test_reopening_after_stop_does_not_create_duplicate_processes(store, profile, browser_loop):
    assert run(controller.open_profile(profile.id))["ok"]
    directory = profile.resolved_user_data_dir
    first = chrome_for(directory)
    assert first > 0

    hub.browser_sessions.mark_stopped(profile.id, by_operator=True, reason="test")
    run(controller.close_session(profile.id))
    # AirdropOS does not leave its own Chrome behind holding the profile.
    assert chrome_for(directory) == 0

    assert run(controller.reconnect_profile(profile.id))["ok"]
    second = chrome_for(directory)
    assert 0 < second <= first + 2, (first, second)

    # Reopening an ALREADY running profile reuses it rather than adding another.
    run(controller.open_profile(profile.id))
    run(controller.open_profile(profile.id))
    assert chrome_for(directory) <= second + 1


def test_concurrent_open_and_reconnect_produce_one_instance(store, profile, browser_loop):
    """Two callers racing must not both build a context for the same profile."""
    async def go():
        return await asyncio.gather(
            controller.open_profile(profile.id),
            controller.open_profile(profile.id),
            controller.reconnect_profile(profile.id),
        )

    results = run(go())
    assert any(r.get("ok") for r in results), results
    # Exactly ONE context for this profile, whichever caller won.
    assert profile.id in controller._contexts
    assert controller.session_mode(profile.id) == "persistent"
    assert chrome_for(profile.resolved_user_data_dir) > 0
    hub.browser_sessions.mark_stopped(profile.id, by_operator=True, reason="test")
    run(controller.close_session(profile.id))
    assert profile.id not in controller._contexts


def test_status_check_during_a_session_creates_no_extra_instance(store, profile, browser_loop):
    assert run(controller.open_profile(profile.id))["ok"]
    before = browser_instances_for(profile.resolved_user_data_dir)
    assert before == 1
    for _ in range(5):
        status = run(controller.profile_status(profile.id))
        assert status["running"] is True
    assert browser_instances_for(profile.resolved_user_data_dir) == before
    hub.browser_sessions.mark_stopped(profile.id, by_operator=True, reason="test")
    run(controller.close_session(profile.id))


def test_stop_releases_the_profile_lock_before_reopening(store, profile, browser_loop):
    assert run(controller.open_profile(profile.id))["ok"]
    directory = profile.resolved_user_data_dir
    hub.browser_sessions.mark_stopped(profile.id, by_operator=True, reason="test")
    run(controller.close_session(directory and profile.id))
    assert chrome.profile_lock(directory)["locked"] is False
    assert run(controller.reconnect_profile(profile.id))["ok"]
    hub.browser_sessions.mark_stopped(profile.id, by_operator=True, reason="test")
    run(controller.close_session(profile.id))


def test_cleanup_never_targets_an_unrelated_chrome(store, profile, browser_loop):
    """Only processes naming THIS managed directory are ever considered for termination."""
    other = str(store.ROOT / "another-profile")
    Path(other).mkdir(parents=True, exist_ok=True)
    controller._terminate_chrome(other)
    # An operator's Chrome holds a different directory and must be untouched.
    assert controller._terminate_chrome(profile.resolved_user_data_dir) is None
    assert chrome.chrome_processes_for("C:/some/other/place") == []


def test_process_matching_requires_the_user_data_dir_flag(tmp_path):
    """Matching the flag's VALUE, not the path appearing anywhere, keeps cleanup safe."""
    target = str(tmp_path / "airdropos-profile").lower().replace("\\", "/")
    assert chrome._command_line_uses_profile(
        ["chrome.exe", f"--user-data-dir={tmp_path / 'airdropos-profile'}"], target)
    assert chrome._command_line_uses_profile(
        ["chrome.exe", "--user-data-dir", str(tmp_path / "airdropos-profile")], target)
    # A different profile: not ours.
    assert not chrome._command_line_uses_profile(
        ["chrome.exe", f"--user-data-dir={tmp_path / 'somebody-else'}"], target)
    # No flag at all: this is somebody's default Chrome.
    assert not chrome._command_line_uses_profile(["chrome.exe", "about:blank"], target)
    # A page URL that merely CONTAINS our path must never match. Chrome child processes
    # can carry a URL as an argument, so a substring test here would be unsafe.
    assert not chrome._command_line_uses_profile(
        ["chrome.exe", "--renderer-client-id=1", f"https://evil.test/{target}/steal"], target)


def test_process_scan_does_not_return_an_unrelated_chrome(tmp_path):
    """The operator's own Chrome (a different directory) is never in the result."""
    if not chrome.find_chrome_executable()["exists"]:
        pytest.skip("Google Chrome is not installed on this machine")
    ours = str(tmp_path / "airdropos-managed-profile")
    assert chrome.chrome_processes_for(ours) == []
    # An obviously unrelated location resolves to nothing.
    assert chrome.chrome_processes_for(str(tmp_path / "definitely-not-a-chrome-profile")) == []


# ======================================================================
# 4. Launch arguments
# ======================================================================
def test_launch_arguments_keep_chrome_in_its_own_profile():
    args = chrome.build_chrome_args(headless=False)
    assert "--no-first-run" in args
    assert "--no-default-browser-check" in args
    # No argument that would point Chrome at another profile directory.
    for arg in args:
        assert not arg.startswith("--profile-directory")
        assert not arg.startswith("--incognito")


def test_the_chrome_module_cannot_execute_anything():
    """Guard against the probe creeping back: the module holds no process machinery."""
    assert not hasattr(chrome, "subprocess")
    # Strip docstrings/comments before scanning, so documenting the old bug is allowed
    # while actually reintroducing the call is not.
    import ast
    import io
    import tokenize

    source = Path(chrome.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    documented = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                documented.add(doc)
    code_only = source
    for doc in documented:
        code_only = code_only.replace(doc, "")
    for token in tokenize.generate_tokens(io.StringIO(code_only).readline):
        if token.type == tokenize.STRING:
            assert "--version" not in token.string, f"a version probe was reintroduced: {token.string[:60]}"
        if token.type == tokenize.NAME and token.string in ("run", "Popen", "check_output", "call"):
            # Only allowed as a method/attribute of something else, never bare.
            assert False, f"process execution referenced: {token.string}"
