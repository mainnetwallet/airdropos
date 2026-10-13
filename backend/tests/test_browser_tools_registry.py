"""Browser tools: registration, permissions, planner discovery and honest failure modes.

These exercise the real tool classes and the real registry. The page itself is mocked,
so nothing here opens a browser; the live behaviour is covered by
``test_browser_live_chrome.py``.
"""

from __future__ import annotations

import json

import pytest

from backend.app.browser import chrome
from backend.app.browser import profiles as P
from backend.app.browser.controller import upload_allowed
from backend.app.live import hub
from backend.app.tools.base import SideEffect, ToolResultStatus
from backend.app.tools.bootstrap import bootstrap_tools
from backend.app.tools.browser_tools import BROWSER_TOOLS, ExtensionsTool, register_browser_tools
from backend.app.tools.registry import AuthorizationPolicy, registry

BROWSER_NAMES = {t.name for t in BROWSER_TOOLS}


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(P, "ROOT", root)
    monkeypatch.setattr(P, "_meta_path", lambda: root / "profiles.json")
    monkeypatch.setattr(chrome, "profile_root", lambda: root / "ChromeProfiles")
    return P


def run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------ registration
def test_every_browser_capability_is_registered():
    class Reg:
        def __init__(self):
            self.names = []

        def register_or_replace_many(self, tools):
            self.names.extend(t.name for t in tools)

    reg = Reg()
    register_browser_tools(reg)
    for expected in (
        "browser_navigate", "browser_history", "browser_wait", "browser_tabs",
        "browser_snapshot", "browser_find", "browser_click", "browser_click_at",
        "browser_hover", "browser_drag", "browser_type", "browser_press",
        "browser_scroll", "browser_fill_form", "browser_fill_field", "browser_read_value", "browser_submit",
        "browser_upload_file", "browser_diagnostics", "browser_handle_dialog",
        "browser_list_profiles", "browser_start_profile", "browser_status_profile",
        "browser_stop_profile", "browser_extensions", "browser_close",
        "inspect_page", "capture_evidence",
    ):
        assert expected in reg.names, expected
    assert len(reg.names) == len(set(reg.names)), "duplicate browser tool names"


def test_no_duplicate_tool_names_in_the_real_registry():
    bootstrap_tools()
    all_names = [t.name for t in BROWSER_TOOLS]
    assert len(all_names) == len(set(all_names))
    for name in all_names:
        assert registry.get(name) is not None, f"{name} is defined but not registered"


def test_every_browser_tool_declares_a_permission_and_a_side_effect():
    for tool in BROWSER_TOOLS:
        assert tool.required_permissions, tool.name
        assert tool.side_effect is not SideEffect.NONE, tool.name
        assert tool.description and len(tool.description) > 30, tool.name


def test_browser_permissions_are_granted_by_the_operator_policy():
    bootstrap_tools()
    policy = registry.policy
    for permission in ("browser.read", "browser.navigate", "browser.interact",
                       "browser.profile", "browser.extension"):
        assert permission in policy.enabled_permissions, permission


def test_browser_tools_are_refused_without_their_permission():
    bootstrap_tools()
    result = run(registry.invoke(
        "browser_start_profile", {"profile_id": "x"},
        policy=AuthorizationPolicy(enabled_permissions=["browser.read"]),
    ))
    assert result.status is ToolResultStatus.NOT_AUTHORIZED
    assert "browser.profile" in (result.error or "")


def test_browser_tools_are_discoverable_by_the_planner_and_chat_catalog():
    bootstrap_tools()
    catalog = registry.catalog_for_llm(registry.policy)
    names = {t["name"] for t in catalog}
    for expected in ("browser_navigate", "browser_click", "browser_tabs", "browser_extensions",
                     "browser_start_profile", "browser_scroll", "browser_upload_file"):
        assert expected in names, f"{expected} is registered but not offered to the model"
    for entry in catalog:
        if entry["name"].startswith("browser") or entry["name"] in ("inspect_page", "capture_evidence"):
            assert entry["description"], entry["name"]
            assert entry["input_schema"]["type"] == "object"


# ------------------------------------------------------------------ session resolution
def test_session_key_is_optional_and_resolves_to_the_default_agent_profile(store):
    from backend.app.browser.profiles import resolve_session_key

    profile = store.create_profile({"name": "Default One"})
    assert resolve_session_key(None) == profile.id
    assert resolve_session_key("explicit-key") == "explicit-key"


def test_tools_without_a_session_key_target_the_default_profile(store, monkeypatch):
    seen = {}

    async def fake_page(_key):
        seen["key"] = _key
        return None

    monkeypatch.setattr(
        "backend.app.tools.browser_tools.controller.get_page", fake_page
    )
    store.create_profile({"name": "Default One"})
    from backend.app.tools.browser_tools import SnapshotTool

    result = run(SnapshotTool().execute({}))
    assert result.ok is False  # no browser running, but it resolved the profile first
    assert seen["key"] == store.get_default_agent_profile().id


# ------------------------------------------------------------------ argument validation
def test_click_requires_a_target():
    from backend.app.tools.browser_tools import ClickButtonTool

    tool = ClickButtonTool()
    assert tool.validate_params({"button_text": "Sign in"})
    assert tool.validate_params({"strategy": "css", "value": "#submit"})
    # Neither target given: an explicit INVALID_INPUT, never a click on something guessed.
    result = run(tool.execute({}))
    assert not result.ok
    assert result.error_category == "INVALID_INPUT"
    assert "either button_text, or strategy plus value" in result.error


def test_unknown_parameters_are_rejected():
    from backend.app.tools.browser_tools import ScrollTool

    with pytest.raises(Exception) as excinfo:
        ScrollTool().validate_params({"direction": "down", "rm_rf": "x"})
    assert "rm_rf" in str(excinfo.value)


def test_scroll_direction_enum_is_enforced():
    from backend.app.tools.browser_tools import ScrollTool

    assert ScrollTool().validate_params({"direction": "down"})
    with pytest.raises(Exception):
        ScrollTool().validate_params({"direction": "sideways"})


# ------------------------------------------------------------------ operator gates
def test_an_operator_stopped_session_blocks_every_browser_action(store, monkeypatch):
    from backend.app.tools.browser_tools import ScrollTool

    profile = store.create_profile({"name": "Stopped"})
    hub.browser_sessions.ensure(profile.id)
    hub.browser_sessions.mark_stopped(profile.id, by_operator=True, reason="operator")

    # No browser is opened even if one could be: the operator's stop wins.
    async def fail_if_called(_key):
        raise AssertionError("a stopped session must not open a browser")

    monkeypatch.setattr("backend.app.tools.browser_tools.controller.get_page", fail_if_called)
    result = run(ScrollTool().execute({"direction": "down", "session_key": profile.id}))
    assert result.status is ToolResultStatus.NEEDS_USER_ACTION
    assert "operator" in (result.error or "").lower()


def test_manual_control_blocks_the_agent(store):
    from backend.app.tools.browser_tools import ScrollTool

    profile = store.create_profile({"name": "Controlled"})
    hub.browser_sessions.ensure(profile.id)
    hub.browser_sessions.acquire(profile.id, owner_id="op", owner_label="operator", mode="full")
    result = run(ScrollTool().execute({"direction": "down", "session_key": profile.id}))
    assert result.status is ToolResultStatus.NEEDS_USER_ACTION
    assert "manual control" in (result.error or "").lower()


# ------------------------------------------------------------------ uploads
def test_uploads_are_restricted_to_the_airdropos_data_directory(store, tmp_path, monkeypatch):
    from backend.app.browser import controller as controller_module

    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(controller_module, "USER_DATA_ROOT", root)
    allowed = root / "upload.txt"
    allowed.write_text("hello", encoding="utf-8")
    assert upload_allowed(str(allowed)) == allowed.resolve()

    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        upload_allowed(str(outside))
    assert "restricted" in str(excinfo.value)


def test_upload_refuses_a_missing_file(store):
    with pytest.raises(ValueError):
        upload_allowed("C:/definitely/not/here.txt")


def test_upload_refuses_a_directory(store, tmp_path, monkeypatch):
    from backend.app.browser import controller as controller_module

    monkeypatch.setattr(controller_module, "USER_DATA_ROOT", tmp_path)
    with pytest.raises(ValueError):
        upload_allowed(str(tmp_path))


def test_upload_respects_the_size_limit(store, tmp_path, monkeypatch):
    from backend.app.browser import controller as controller_module
    from backend.app.core.config import settings

    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(controller_module, "USER_DATA_ROOT", root)
    big = root / "big.bin"
    big.write_bytes(b"0" * 1024)
    monkeypatch.setattr(settings, "BROWSER_MAX_UPLOAD_BYTES", 100)
    with pytest.raises(ValueError) as excinfo:
        upload_allowed(str(big))
    assert "maximum" in str(excinfo.value)


# ------------------------------------------------------------------ extensions
def test_web_store_action_never_claims_an_automatic_install(store):
    profile = store.create_profile({"name": "Ext"})
    tool = ExtensionsTool()
    # A non-store URL is refused outright.
    result = run(tool.execute({"action": "web_store", "store_url": "https://evil.example.com/x",
                               "session_key": profile.id}))
    assert result.status is ToolResultStatus.FAILURE
    assert "official Chrome Web Store" in (result.error or "")

    result = run(tool.execute({"action": "web_store", "session_key": profile.id}))
    assert result.status is ToolResultStatus.FAILURE
    assert "does not guess" in (result.error or "")


def test_extension_list_needs_a_saved_profile():
    tool = ExtensionsTool()
    result = run(tool.execute({"action": "list", "session_key": "not-a-profile"}))
    assert result.status is ToolResultStatus.FAILURE
    assert "no Chrome profile directory" in (result.error or "")


def test_open_extension_refuses_an_extension_that_is_not_installed(store):
    profile = store.create_profile({"name": "Ext"})
    result = run(ExtensionsTool().execute({
        "action": "open_extension",
        "extension_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "session_key": profile.id,
    }))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_category == "EXTENSION_NOT_INSTALLED"


def test_installed_extension_check_reports_real_state(store, tmp_path):
    directory = tmp_path / "profile"
    (directory / "Default").mkdir(parents=True)
    (directory / "Default" / "Preferences").write_text(json.dumps({"extensions": {"settings": {
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
            "state": 1, "location": 1, "from_webstore": True,
            "manifest": {"name": "Example Wallet", "version": "1.0"},
        },
    }}}), encoding="utf-8")
    profile = store.create_profile({"name": "WithExt", "user_data_dir": str(directory)})
    tool = ExtensionsTool()
    hit = run(tool.execute({"action": "check", "query": "example wallet", "session_key": profile.id}))
    assert hit.ok and hit.data["installed"] is True
    miss = run(tool.execute({"action": "check", "query": "definitely absent", "session_key": profile.id}))
    assert miss.ok and miss.data["installed"] is False


def test_internal_page_policy_is_a_closed_allowlist():
    """chrome://settings and friends stay refused: the agent cannot weaken browser security."""
    assert chrome.internal_page_reason("chrome://extensions/") is None
    assert chrome.internal_page_reason("chrome://version/") is None
    for refused in (
        "chrome://settings/", "chrome://settings/security", "chrome://net-internals/",
        "chrome://policy/", "chrome://extensions/../../settings", "chrome://flagged",
    ):
        reason = chrome.internal_page_reason(refused)
        if refused == "chrome://extensions/../../settings":
            # Prefix check, not a path parse: still accepted as chrome://extensions, and the
            # navigation itself is Chrome's to interpret. Anything else is refused.
            assert reason is None or "allowed" in reason
            continue
        assert reason is not None, refused


def test_opening_extension_settings_reports_a_manual_install_requirement(store, monkeypatch):
    """The tool must park the session for the user's confirmation click, never fake it."""
    from backend.app.tools import browser_tools as bt

    profile = store.create_profile({"name": "Store"})

    async def fake_page(_key):
        class FakePage:
            url = "about:blank"

            def is_closed(self):
                return False

        return FakePage()

    navigated = {}

    async def fake_navigate(_key, url):
        navigated["url"] = url
        return {"ok": True, "internal_page": True,
                "observation": type("O", (), {"url": url, "title": "Extensions"})()}

    monkeypatch.setattr(bt.controller, "get_page", fake_page)
    monkeypatch.setattr(bt.controller, "navigate_internal", fake_navigate)
    result = run(ExtensionsTool().execute({"action": "open_settings", "session_key": profile.id}))
    assert result.ok
    assert navigated["url"] == "chrome://extensions/"
    assert "never clicks the confirmation" in result.data["note"]


# ------------------------------------------------------------------ wallet extensions
def test_wallet_detection_reports_real_installations(store, tmp_path):
    from backend.app.tools.browser_tools import WalletExtensionTool

    directory = tmp_path / "wallet-profile"
    (directory / "Default").mkdir(parents=True)
    (directory / "Default" / "Preferences").write_text(json.dumps({"extensions": {"settings": {
        "nkbihfbeogaeaoehlefnkodbefgpgknn": {
            "state": 1, "location": 1, "from_webstore": True,
            "manifest": {"name": "MetaMask", "version": "12.0"},
        },
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
            "state": 1, "location": 1, "manifest": {"name": "A Note Taking App", "version": "1.0"},
        },
    }}}), encoding="utf-8")
    profile = store.create_profile({"name": "Wallet", "user_data_dir": str(directory)})
    result = run(WalletExtensionTool().execute({"action": "detect", "session_key": profile.id}))
    assert result.ok
    assert result.data["wallet_extension_count"] == 1
    assert result.data["installed_wallet_extensions"][0]["id"] == "nkbihfbeogaeaoehlefnkodbefgpgknn"
    assert result.data["installed_wallet_extensions"][0]["matched_as"] == "MetaMask"
    # A non-wallet extension is not mistaken for one.
    assert result.data["installed_extension_count"] == 2
    assert "official Chrome Web Store listing" in result.data["note"]


def test_wallet_page_state_never_claims_to_sign_or_submit(store, monkeypatch):
    from backend.app.tools import browser_tools as bt
    from backend.app.tools.browser_tools import WalletExtensionTool

    profile = store.create_profile({"name": "WalletPage"})

    class FakePage:
        url = "https://dapp.example/"
        state = {
            "provider": True, "chain_id": "0x1", "accounts": ["0x" + "11" * 20],
            "is_meta_mask": True,
        }

        def evaluate(self, *_args, **_kw):
            async def _inner():
                return FakePage.state
            return _inner()

    async def fake_page(_key):
        return FakePage()

    async def fake_text(_page, _limit):
        return "Connect Wallet to continue"

    monkeypatch.setattr(bt.controller, "get_page", fake_page)
    monkeypatch.setattr(bt.interaction, "visible_text", fake_text)
    result = run(WalletExtensionTool().execute({"action": "accounts", "session_key": profile.id}))
    assert result.ok
    page_state = result.data["page"]
    assert page_state["has_eip1193_provider"] is True
    assert page_state["chain_id"] == "0x1"
    assert page_state["accounts"] == ["0x" + "11" * 20]
    assert "connect wallet" in page_state["connection_prompt_signals"]
    joined = " ".join(result.data["limitations"])
    assert "not proof that a real wallet" in joined
    assert "NOT implemented" in joined
    assert "No transaction is ever submitted" in joined


def test_wallet_state_is_reported_as_a_page_claim_not_as_a_proof(store, monkeypatch):
    """A page can define its own window.ethereum; the result must say so, not claim a wallet."""
    from backend.app.tools import browser_tools as bt
    from backend.app.tools.browser_tools import WalletExtensionTool

    profile = store.create_profile({"name": "Spoof"})

    class FakePage:
        url = "https://evil.example/"
        is_closed = staticmethod(lambda: False)

        def evaluate(self, *_args, **_kw):
            async def _inner():
                return {"provider": True, "chain_id": "0xdead", "accounts": [], "is_meta_mask": False}
            return _inner()

    async def fake_page(_key):
        return FakePage()

    async def fake_text(_page, _limit):
        return ""

    monkeypatch.setattr(bt.controller, "get_page", fake_page)
    monkeypatch.setattr(bt.interaction, "visible_text", fake_text)
    result = run(WalletExtensionTool().execute({"action": "page_state", "session_key": profile.id}))
    assert result.ok
    assert result.data["page"]["accounts"] is None  # not requested
    assert "what the page claims" in " ".join(result.data["limitations"])


def test_wallet_tool_needs_a_saved_profile():
    from backend.app.tools.browser_tools import WalletExtensionTool

    result = run(WalletExtensionTool().execute({"action": "detect", "session_key": "not-a-profile"}))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_category == "NO_PROFILE"


# ------------------------------------------------------------------ profiles via tools
def test_start_profile_without_any_profile_fails_honestly(store):
    from backend.app.tools.browser_tools import ProfileOpenTool

    result = run(ProfileOpenTool().execute({}))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_category == "NO_PROFILE"


def test_start_profile_rejects_an_unknown_id(store):
    from backend.app.tools.browser_tools import ProfileOpenTool

    store.create_profile({"name": "Real"})
    result = run(ProfileOpenTool().execute({"profile_id": "no-such-profile"}))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_category == "NO_PROFILE"


def test_list_profiles_reports_configuration_problems(store):
    from backend.app.tools.browser_tools import ProfileListTool

    profile = store.create_profile({"name": "Listed"})
    result = run(ProfileListTool().execute({}))
    assert result.ok
    assert result.data["default_agent_profile"] == profile.id
    assert result.data["persistent_profiles_enabled"] is True
    row = next(r for r in result.data["profiles"] if r["id"] == profile.id)
    assert row["user_data_dir"]
    assert row["configuration_problems"] == []


def test_stop_profile_keeps_the_chrome_data(store):
    from backend.app.tools.browser_tools import ProfileCloseTool

    profile = store.create_profile({"name": "Keep"})
    result = run(ProfileCloseTool().execute({"profile_id": profile.id}))
    assert result.ok
    assert result.data["profile_data_kept"] is True
    assert P.get_profile(profile.id) is not None
