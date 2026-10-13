"""Browser profile configuration, persistence and extension detection.

Everything here is deterministic configuration work: no browser is launched and no
network request is made. The real-browser behaviour lives in
``test_browser_live_chrome.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.app.browser import chrome
from backend.app.browser import profiles as P


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the profile store at a throwaway directory for each test."""
    root = tmp_path / "browser_sessions"
    root.mkdir()
    monkeypatch.setattr(P, "ROOT", root)
    monkeypatch.setattr(P, "_meta_path", lambda: root / "profiles.json")
    monkeypatch.setattr(chrome, "profile_root", lambda: root / "ChromeProfiles")
    return P


# ------------------------------------------------------------------ creation
def test_new_profile_gets_a_dedicated_chrome_directory(store, tmp_path):
    profile = store.create_profile({"name": "Trading"})
    expected = str(store.ROOT / "ChromeProfiles" / profile.id)
    assert profile.resolved_user_data_dir == expected
    assert Path(expected).is_absolute()
    assert profile.launch_mode == "managed"
    assert profile.browser_type == "chrome"


def test_profile_config_persists_across_a_process_restart(store):
    created = store.create_profile({"name": "Persisted", "locale": "en-GB", "start_url": "https://example.com"})
    # A fresh read is what a restarted process sees.
    assert store.get_profile(created.id) is not None
    again = store.get_profile(created.id)
    assert again.locale == "en-GB"
    assert again.start_url == "https://example.com"


def test_duplicate_names_are_refused(store):
    store.create_profile({"name": "Same"})
    with pytest.raises(P.ProfileError):
        store.create_profile({"name": "same"})


def test_personal_chrome_profile_directory_is_refused(store):
    profile = store.create_profile({"name": "Personal"})
    personal = str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data")
    with pytest.raises(P.ProfileError) as excinfo:
        store.update_profile(profile.id, {"user_data_dir": personal})
    assert "real Chrome/Chromium profile" in str(excinfo.value)


def test_relative_profile_directory_is_refused(store):
    profile = store.create_profile({"name": "Relative"})
    with pytest.raises(P.ProfileError):
        store.update_profile(profile.id, {"user_data_dir": "some/relative/dir"})


def test_nonexistent_executable_is_refused(store):
    profile = store.create_profile({"name": "BadExe"})
    with pytest.raises(P.ProfileError):
        store.update_profile(profile.id, {"executable_path": str(Path("C:/definitely/not/here/chrome.exe"))})


def test_attach_mode_requires_a_port_and_loopback_host(store):
    profile = store.create_profile({"name": "Attach"})
    with pytest.raises(P.ProfileError):
        store.update_profile(profile.id, {"launch_mode": "attach"})
    with pytest.raises(P.ProfileError) as excinfo:
        store.update_profile(
            profile.id, {"launch_mode": "attach", "debugger_host": "10.1.2.3", "debugger_port": 9222}
        )
    assert "127.0.0.1" in str(excinfo.value)


def test_unpacked_extension_directory_must_have_a_manifest(store, tmp_path):
    profile = store.create_profile({"name": "Ext"})
    empty = tmp_path / "no-manifest"
    empty.mkdir()
    with pytest.raises(P.ProfileError):
        store.update_profile(profile.id, {"unpacked_extensions": str(empty)})
    good = tmp_path / "with-manifest"
    good.mkdir()
    (good / "manifest.json").write_text(json.dumps({"manifest_version": 3, "name": "X", "version": "1"}))
    store.update_profile(profile.id, {"unpacked_extensions": str(good)})
    assert store.get_profile(profile.id).extension_dirs() == [str(good)]


# ------------------------------------------------------------------ default agent profile
def test_first_profile_becomes_the_default_agent_profile(store):
    first = store.create_profile({"name": "First"})
    assert store.get_default_agent_profile().id == first.id
    assert store.resolve_session_key(None) == first.id


def test_default_agent_profile_survives_a_restart(store):
    first = store.create_profile({"name": "First"})
    second = store.create_profile({"name": "Second"})
    store.set_default_agent_profile(second.id)
    # Re-read the document the way a restarted process would.
    assert store._read_doc()["default_agent_profile"] == second.id
    assert store.get_default_agent_profile().id == second.id
    assert store.get_profile(first.id).is_default_agent is False
    assert store.get_profile(second.id).is_default_agent is True


def test_default_agent_profile_can_be_cleared(store):
    profile = store.create_profile({"name": "Only"})
    store.set_default_agent_profile(None)
    assert store.get_default_agent_profile() is None
    assert store.resolve_session_key(None) == "default"
    assert store.get_profile(profile.id) is not None  # the profile itself survives


def test_explicit_session_key_always_wins(store):
    default = store.create_profile({"name": "Default"})
    store.create_profile({"name": "Other"})
    assert store.resolve_session_key("some-other-key") == "some-other-key"
    assert store.resolve_session_key(default.id) == default.id
    assert store.resolve_session_key(None) == default.id


def test_stale_default_id_does_not_resolve_to_another_profile(store):
    profile = store.create_profile({"name": "Gone"})
    store.set_default_agent_profile(profile.id)
    store.delete_profile(profile.id)
    assert store.get_default_agent_profile() is None
    assert store.resolve_session_key(None) == "default"


def test_setting_a_missing_default_profile_is_refused(store):
    with pytest.raises(P.ProfileError):
        store.set_default_agent_profile("does-not-exist")


# ------------------------------------------------------------------ deletion safety
def test_deleting_configuration_keeps_the_chrome_profile_data(store, tmp_path):
    directory = tmp_path / "keep-my-data"
    directory.mkdir()
    (directory / "marker.txt").write_text("important", encoding="utf-8")
    profile = store.create_profile({"name": "Keeper", "user_data_dir": str(directory)})
    store.delete_profile(profile.id)
    assert store.get_profile(profile.id) is None
    assert (directory / "marker.txt").read_text(encoding="utf-8") == "important"


def test_deleting_profile_data_requires_the_profile_id_back(store, tmp_path):
    directory = tmp_path / "wipe-me"
    directory.mkdir()
    (directory / "marker.txt").write_text("x", encoding="utf-8")
    profile = store.create_profile({"name": "Wipe", "user_data_dir": str(directory)})
    with pytest.raises(P.ProfileError):
        store.delete_profile_data(profile.id, confirm="something-else")
    assert (directory / "marker.txt").exists()
    result = store.delete_profile_data(profile.id, confirm=profile.id)
    assert result["deleted"] is True
    assert not directory.exists()


def test_attached_profile_data_is_never_deleted_by_airdropos(store, tmp_path):
    directory = tmp_path / "someone-elses-chrome"
    directory.mkdir()
    (directory / "marker.txt").write_text("x", encoding="utf-8")
    profile = store.create_profile({
        "name": "Attached", "launch_mode": "attach", "debugger_host": "127.0.0.1", "debugger_port": 9222,
    })
    with pytest.raises(P.ProfileError) as excinfo:
        store.delete_profile_data(profile.id, confirm=profile.id)
    assert "does not own" in str(excinfo.value)
    assert (directory / "marker.txt").exists()


# ------------------------------------------------------------------ extension detection
def _write_preferences(directory: Path, settings_blob: dict) -> None:
    (directory / "Default").mkdir(parents=True, exist_ok=True)
    (directory / "Default" / "Preferences").write_text(
        json.dumps({"extensions": {"settings": settings_blob}}), encoding="utf-8"
    )


def test_installed_extensions_are_read_from_the_profile_own_state(tmp_path):
    directory = tmp_path / "profile"
    _write_preferences(directory, {
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
            "state": 1,
            "location": 1,
            "from_webstore": True,
            "path": "ldnnhbdfdfaomdkehbekjhiahibekdcf",
            "manifest": {
                "name": "Example Wallet",
                "version": "3.2.1",
                "manifest_version": 3,
                "description": "A test wallet",
                "permissions": ["storage", "unlimitedStorage"],
            },
        },
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": {
            "state": 0, "location": 1, "manifest": {"name": "Disabled One", "version": "1.0"},
        },
        # A component extension must not be reported as user-installed.
        "cccccccccccccccccccccccccccccccc": {"state": 1, "location": 4, "manifest": {"name": "Chrome Web Store"}},
    })
    installed = chrome.installed_extensions(str(directory))
    by_id = {e.id: e for e in installed}
    assert set(by_id) == {
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }
    wallet = by_id["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    assert wallet.name == "Example Wallet"
    assert wallet.version == "3.2.1"
    assert wallet.enabled is True
    assert wallet.from_webstore is True
    assert "storage" in wallet.permissions
    assert by_id["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"].enabled is False


def test_extension_matching_is_case_insensitive_and_by_id(tmp_path):
    directory = tmp_path / "profile"
    _write_preferences(directory, {
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
            "state": 1, "location": 1,
            "manifest": {"name": "MetaMask", "version": "12.0", "description": "A crypto wallet"},
        },
    })
    assert len(chrome.extension_matches(str(directory), "metamask")) == 1
    assert len(chrome.extension_matches(str(directory), "METAMASK")) == 1
    assert chrome.extension_matches(str(directory), "trust wallet") == []


def test_extension_manifest_is_read_from_disk(tmp_path):
    directory = tmp_path / "profile"
    ext = directory / "Default" / "Extensions" / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" / "1.0.0"
    ext.mkdir(parents=True)
    (ext / "manifest.json").write_text(json.dumps({"name": "Test", "version": "1.0.0"}), encoding="utf-8")
    manifest = chrome.extension_manifest(str(directory), "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert manifest["name"] == "Test"
    assert chrome.extension_manifest(str(directory), "not-an-id") == {}


def test_missing_preferences_yields_no_extensions(tmp_path):
    assert chrome.installed_extensions(str(tmp_path / "never-created")) == []


# ------------------------------------------------------------------ internal pages
def test_only_extensions_and_version_internal_pages_are_allowed():
    assert chrome.internal_page_reason("chrome://extensions/") is None
    assert chrome.internal_page_reason("chrome://version/") is None
    assert chrome.internal_page_reason("chrome://settings/") is not None
    assert chrome.internal_page_reason("chrome://net-internals/") is not None
    assert chrome.internal_page_reason("https://example.com") == "not an internal browser page"


def test_internal_pages_can_be_disabled(monkeypatch):
    monkeypatch.setattr(chrome.settings, "BROWSER_ALLOW_INTERNAL_PAGES", False)
    assert "disabled" in (chrome.internal_page_reason("chrome://extensions/") or "")


# ------------------------------------------------------------------ attach endpoint
def test_attach_endpoint_is_loopback_only():
    assert chrome.attach_endpoint("127.0.0.1", 9222) == "http://127.0.0.1:9222"
    assert chrome.attach_endpoint("localhost", 9222) == "http://127.0.0.1:9222"
    with pytest.raises(chrome.ChromeError):
        chrome.attach_endpoint("192.168.1.10", 9222)
    with pytest.raises(chrome.ChromeError):
        chrome.attach_endpoint("0.0.0.0", 9222)
    with pytest.raises(chrome.ChromeError):
        chrome.attach_endpoint("127.0.0.1", 80)


# ------------------------------------------------------------------ validation
def test_validate_profile_config_reports_what_is_wrong(store, monkeypatch):
    profile = store.create_profile({"name": "Validate"})
    assert store.validate_profile_config(profile) == []
    monkeypatch.setattr(
        chrome, "find_chrome_executable",
        lambda: {"path": "", "exists": False, "detail": "not found"},
    )
    problems = store.validate_profile_config(store.get_profile(profile.id))
    assert any("Google Chrome was not found" in p for p in problems)


def test_unpacked_extension_missing_manifest_is_a_configuration_problem(store, tmp_path):
    directory = tmp_path / "gone"
    directory.mkdir()
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    profile = store.create_profile({"name": "Gone", "unpacked_extensions": str(directory)})
    import shutil

    shutil.rmtree(directory)
    problems = store.validate_profile_config(store.get_profile(profile.id))
    assert any("manifest.json" in p for p in problems)


def test_launch_args_load_only_the_named_unpacked_extensions():
    args = chrome.build_chrome_args(headless=False, extension_dirs=["C:/a", "C:/b"])
    assert "--disable-extensions-except=C:\\a,C:\\b" in args or "--disable-extensions-except=C:/a,C:/b" in args
    assert any(a.startswith("--load-extension=") for a in args)
    # The sandbox must stay on: a profile that loads extensions runs under Chrome's own.
    assert "--no-sandbox" not in args
    assert "--headless=new" not in args


def test_headless_uses_the_new_mode_so_extensions_still_work():
    assert "--headless=new" in chrome.build_chrome_args(headless=True)
