"""The whole agent path, end to end, against a real Chrome profile.

request -> tool selection -> permission check -> registry -> controller -> Chrome ->
observation -> evidence -> activity event.

The only thing stubbed is the fixed LLM's reply (the tool calls it asks for), because the
point of this file is everything AFTER the model has chosen. The browser, the profile
directory, the persistence and the activity log are all real.
"""

from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.app.agent.chat_loop import ChatToolLoop
from backend.app.browser import chrome
from backend.app.browser import profiles as P
from backend.app.browser.controller import controller
from backend.app.live import hub
from backend.app.tools.bootstrap import bootstrap_tools
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry

PAGE = """<!doctype html><html><head><title>Agent Path Site</title></head><body>
<h1>Agent Path</h1>
<button id="go" onclick="document.getElementById('out').textContent='went'">Start here</button>
<div id="out">not yet</div>
</body></html>"""


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def browser_loop():
    import asyncio

    loop = asyncio.new_event_loop()
    global _LOOP
    _LOOP = loop
    try:
        yield loop
    finally:
        loop.run_until_complete(controller.stop())
        loop.close()


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("agent-site")
    (root / "index.html").write_text(PAGE, encoding="utf-8")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(root)))
    import threading

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
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


@pytest.fixture
def allow_loopback(monkeypatch):
    import backend.app.browser.controller as controller_module

    async def _allow(_url):
        return None

    monkeypatch.setattr(controller_module, "destination_block_reason", _allow)
    monkeypatch.setattr(controller_module, "url_block_reason", lambda _url: None)


def run(coro):
    return _LOOP.run_until_complete(coro)


class ScriptedLLM:
    """Stands in for the one fixed model: emits the tool calls the test wants, in order."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    async def complete(self, messages, json_mode=False):
        import json as _json

        self.calls.append(messages[-1].content if messages else "")
        if self.rounds:
            payload = self.rounds.pop(0)
        else:
            payload = {"reply": "Done.", "tool_calls": []}
        return type("Resp", (), {
            "content": _json.dumps(payload),
            "model": "scripted",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        })()

    @staticmethod
    def parse_json(text):
        import json as _json

        return _json.loads(text)


@pytest.fixture
def scripted_llm(monkeypatch):
    def _install(rounds):
        fake = ScriptedLLM(rounds)
        monkeypatch.setattr("backend.app.agent.chat_loop.llm", fake)
        return fake

    return _install


def test_agent_opens_the_default_profile_and_reaches_the_page(store, allow_loopback, site, browser_loop,
                                                             scripted_llm):
    """The agent's browser task lands in the operator's DEFAULT profile and drives real Chrome."""
    profile = store.create_profile({"name": "Agent Default", "user_data_dir": str(store.ROOT / "agent")})
    bootstrap_tools()
    registry = ToolRegistry()
    from backend.app.tools import airdrop_tools, blockchain_tools, browser_tools, diagnostic_tools
    from backend.app.tools import notification_tools, task_tools, wallet_tools, web_tools

    for register in (
        web_tools.register_web_tools, blockchain_tools.register_blockchain_tools,
        browser_tools.register_browser_tools, notification_tools.register_notification_tools,
        wallet_tools.register_wallet_tools, task_tools.register_task_tools,
        diagnostic_tools.register_diagnostic_tools, airdrop_tools.register_airdrop_tools,
    ):
        register(registry)
    policy = AuthorizationPolicy(
        enabled_permissions=[
            "browser.read", "browser.navigate", "browser.interact", "browser.profile", "browser.extension",
        ],
        allow_web_interaction=True,
    )
    registry.policy = policy

    scripted_llm([
        {"reply": "Opening the browser.", "tool_calls": [
            {"tool": "browser_start_profile", "arguments": {"profile_id": profile.id}}]},
        {"reply": "Opening the page.", "tool_calls": [
            {"tool": "browser_navigate", "arguments": {"url": f"{site}/index.html"}}]},
        {"reply": "Clicking.", "tool_calls": [
            {"tool": "browser_click", "arguments": {"button_text": "Start here",
                                                     "expect_text": "went"}}]},
        {"reply": "Done.", "tool_calls": []},
    ])

    loop_runner = ChatToolLoop(registry=registry, policy=policy)
    result = run(loop_runner.run(db=None, message="open my browser and start the task",
                                 history=[], create_task=None))

    names = [c.tool for c in result.tool_calls]
    assert names == ["browser_start_profile", "browser_navigate", "browser_click"], names
    assert all(c.status == "success" for c in result.tool_calls), [c.error for c in result.tool_calls]

    started = result.tool_calls[0]
    assert started.data["running"] is True
    assert started.data["connection"] == "managed_persistent"
    assert started.data["user_data_dir"] == profile.resolved_user_data_dir

    navigated = result.tool_calls[1]
    assert navigated.data["observation"]["title"] == "Agent Path Site"
    # session_key was never supplied, so it resolved to the default agent profile.
    assert navigated.data["session_key"] == profile.id

    clicked = result.tool_calls[2]
    assert clicked.data["expected_text_seen"] is True
    assert clicked.evidence["authoritative"] is True
    assert clicked.evidence["kind"] == "post_action_observation"

    # The operator's activity log actually recorded what happened.
    events = [e for e in hub.events.recent(limit=200) if e["session_id"] == f"browser:{profile.id}"]
    kinds = {e["type"] for e in events}
    assert "browser.profile_opened" in kinds
    assert "browser.navigated" in kinds
    assert "browser.clicked" in kinds
    assert "browser.form_filled" not in kinds  # nothing was logged that did not happen

    run(controller.close_session(profile.id))
    hub.browser_sessions.mark_stopped(profile.id, by_operator=True, reason="test")
    run(controller.stop())


def test_agent_is_refused_when_the_permission_is_withheld(store, allow_loopback, browser_loop, scripted_llm):
    """A policy without browser.profile must block the tool, with the exact missing permission."""
    bootstrap_tools()
    registry = ToolRegistry()
    from backend.app.tools.browser_tools import register_browser_tools

    register_browser_tools(registry)
    policy = AuthorizationPolicy(enabled_permissions=["browser.read"], allow_web_interaction=True)
    registry.policy = policy
    scripted_llm([
        {"reply": "Opening.", "tool_calls": [
            {"tool": "browser_start_profile", "arguments": {"profile_id": "whatever"}}]},
        {"reply": "I cannot.", "tool_calls": []},
    ])
    loop_runner = ChatToolLoop(registry=registry, policy=policy)
    result = run(loop_runner.run(db=None, message="open chrome", history=[], create_task=None))
    call = result.tool_calls[0]
    assert call.status == "not_authorized"
    assert "browser.profile" in (call.error or "")
    assert any("browser.profile" in b for b in result.blockers)


def test_a_failing_click_is_reported_as_a_failure_not_a_success(store, allow_loopback, site,
                                                               browser_loop, scripted_llm):
    bootstrap_tools()
    registry = ToolRegistry()
    from backend.app.tools.browser_tools import register_browser_tools

    register_browser_tools(registry)
    policy = AuthorizationPolicy(
        enabled_permissions=["browser.read", "browser.navigate", "browser.interact", "browser.profile"],
        allow_web_interaction=True,
    )
    registry.policy = policy
    profile = store.create_profile({"name": "Honest", "user_data_dir": str(store.ROOT / "honest")})
    scripted_llm([
        {"reply": "Starting.", "tool_calls": [
            {"tool": "browser_start_profile", "arguments": {"profile_id": profile.id}}]},
        {"reply": "Navigating.", "tool_calls": [
            {"tool": "browser_navigate", "arguments": {"url": f"{site}/index.html"}}]},
        {"reply": "Clicking a button that is not there.", "tool_calls": [
            {"tool": "browser_click", "arguments": {"button_text": "No Such Button",
                                                     "expect_text": "went"}}]},
        {"reply": "Done.", "tool_calls": []},
    ])
    loop_runner = ChatToolLoop(registry=registry, policy=policy)
    result = run(loop_runner.run(db=None, message="click it", history=[], create_task=None))
    click = result.tool_calls[-1]
    assert click.status == "failure"
    assert click.error_category == "ELEMENT_NOT_FOUND"
    assert "Do NOT report this click as done" in (click.error or "")
    assert click.evidence is None
    assert any("ELEMENT_NOT_FOUND" in b or "Nothing clickable" in b for b in result.blockers)

    run(controller.close_session(profile.id))
    hub.browser_sessions.mark_stopped(profile.id, by_operator=True, reason="test")
    run(controller.stop())


def test_gateway_exposes_the_profile_lifecycle(store, allow_loopback):
    """The worker gateway routes the API calls to the process that owns Chrome.

    No ``browser_loop`` here on purpose: Starlette's TestClient runs the gateway on its own
    event loop, which is exactly the shape of the real worker. ``controller.stop()`` clears
    the loop-bound locks so the controller can be started on that loop.
    """
    from starlette.testclient import TestClient

    from backend.app.live.gateway import build_app
    from backend.app.live.security import GATEWAY_HEADER, gateway_secret

    profile = store.create_profile({"name": "Gateway", "user_data_dir": str(store.ROOT / "gw")})
    # ONE context-managed client: Starlette's TestClient runs each request on its own event
    # loop otherwise, and Playwright's connection cannot survive that.
    with TestClient(build_app()) as client:
        client.headers.update({GATEWAY_HEADER: gateway_secret()})

        status = client.get(f"/browser/profiles/{profile.id}/status")
        assert status.status_code == 200
        assert status.json()["status"]["running"] is False

        opened = client.post("/browser/profiles/open", json={"profile_id": profile.id})
        assert opened.status_code == 200, opened.text
        assert opened.json()["status"]["running"] is True
        assert opened.json()["status"]["connection"] == "managed_persistent"

        extensions = client.get(f"/browser/profiles/{profile.id}/extensions")
        assert extensions.status_code == 200
        assert extensions.json()["extensions"]["installed"] == []

        closed = client.post("/browser/profiles/close", json={"profile_id": profile.id})
        assert closed.status_code == 200
        assert closed.json()["profile_data_kept"] is True

        # Reopening the same profile straight after closing it must work: the browser has to
        # be really gone from the directory, not merely closed.
        reopened = client.post("/browser/profiles/open", json={"profile_id": profile.id})
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["status"]["running"] is True
        assert client.post("/browser/profiles/close", json={"profile_id": profile.id}).status_code == 200

        missing = client.post("/browser/profiles/open", json={"profile_id": "no-such-profile"})
        assert missing.status_code == 409
        assert missing.json()["detail"]["code"] == "no_profile"


def test_gateway_refuses_profile_lifecycle_without_the_shared_secret(store):
    from starlette.testclient import TestClient

    from backend.app.live.gateway import build_app

    profile = store.create_profile({"name": "NoSecret"})
    client = TestClient(build_app())
    assert client.post("/browser/profiles/open", json={"profile_id": profile.id}).status_code == 401
    assert client.get(f"/browser/profiles/{profile.id}/status").status_code == 401
