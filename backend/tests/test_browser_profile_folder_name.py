"""The operator can choose the Chrome profile folder name; random only when left empty."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.app.browser import chrome
from backend.app.browser import profiles as P


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(P, "ROOT", root)
    monkeypatch.setattr(P, "_meta_path", lambda: root / "profiles.json")
    monkeypatch.setattr(chrome, "profile_root", lambda: root / "ChromeProfiles")
    chrome.reset_caches()
    return P


def _expected(store, name: str) -> str:
    return chrome.normalize_path(str(store.ROOT / "ChromeProfiles" / name))


def test_a_typed_folder_name_is_used_under_the_profile_root(store):
    profile = store.create_profile({"name": "Wallet One", "user_data_dir": "wallet-1"})
    assert profile.resolved_user_data_dir == _expected(store, "wallet-1")
    assert not chrome.is_personal_user_data_dir(profile.resolved_user_data_dir)
    # The profile id stays internal and independent of the folder the operator chose.
    assert Path(profile.resolved_user_data_dir).name == "wallet-1"
    assert profile.id != "wallet-1"


def test_empty_directory_still_gets_the_automatic_default(store):
    profile = store.create_profile({"name": "Auto", "user_data_dir": ""})
    assert Path(profile.resolved_user_data_dir).name == profile.id
    assert Path(profile.resolved_user_data_dir).parent == Path(_expected(store, "x")).parent


def test_omitting_the_directory_also_gets_the_automatic_default(store):
    profile = store.create_profile({"name": "Auto Two"})
    assert Path(profile.resolved_user_data_dir).name == profile.id


def test_a_full_path_still_works(store, tmp_path):
    target = tmp_path / "my-own-dir"
    profile = store.create_profile({"name": "Full Path", "user_data_dir": str(target)})
    assert profile.resolved_user_data_dir == chrome.normalize_path(str(target))


@pytest.mark.parametrize("bad", ["my profile", "..", ".hidden", "name.", "con", "NUL", "a" * 65, "wal<let", "wal*let"])
def test_invalid_folder_names_are_refused_with_a_clear_reason(store, bad):
    with pytest.raises(P.ProfileError) as excinfo:
        store.create_profile({"name": "Bad Name", "user_data_dir": bad})
    assert "folder name" in str(excinfo.value).lower()


@pytest.mark.parametrize("bad", ["some/relative/dir", "..\\..\\escape", "../escape"])
def test_relative_paths_are_still_refused(store, bad):
    with pytest.raises(P.ProfileError) as excinfo:
        store.create_profile({"name": "Relative", "user_data_dir": bad})
    assert "absolute path" in str(excinfo.value)


def test_two_profiles_cannot_share_one_directory(store):
    store.create_profile({"name": "First", "user_data_dir": "shared"})
    with pytest.raises(P.ProfileError) as excinfo:
        store.create_profile({"name": "Second", "user_data_dir": "shared"})
    assert excinfo.value.status == 409
    assert "already uses" in str(excinfo.value)


def test_directory_names_compare_case_insensitively_on_windows_style_filesystems(store):
    if os.path.normcase("A") == "A":
        pytest.skip("case-sensitive filesystem: Shared and shared are different folders")
    store.create_profile({"name": "First", "user_data_dir": "Shared"})
    with pytest.raises(P.ProfileError):
        store.create_profile({"name": "Second", "user_data_dir": "shared"})


def test_editing_a_profile_can_set_a_folder_name(store):
    profile = store.create_profile({"name": "Editable"})
    updated = store.update_profile(profile.id, {"user_data_dir": "renamed-dir"})
    assert updated.resolved_user_data_dir == _expected(store, "renamed-dir")


def test_saving_a_profile_with_its_own_directory_is_not_a_duplicate(store):
    profile = store.create_profile({"name": "Same", "user_data_dir": "keep-me"})
    again = store.update_profile(profile.id, {"user_data_dir": "keep-me", "description": "x"})
    assert again.resolved_user_data_dir == _expected(store, "keep-me")


def test_editing_into_another_profiles_directory_is_refused(store):
    store.create_profile({"name": "Owner", "user_data_dir": "owned"})
    other = store.create_profile({"name": "Other"})
    with pytest.raises(P.ProfileError) as excinfo:
        store.update_profile(other.id, {"user_data_dir": "owned"})
    assert excinfo.value.status == 409


def test_a_typed_name_never_escapes_into_a_personal_browser_directory(store, monkeypatch):
    """Even if the profile root itself were a personal directory, the personal-dir guard still fires."""
    root = store.ROOT / "ChromeProfiles"
    monkeypatch.setattr(chrome, "PERSONAL_USER_DATA_DIRS", (str(root),))
    with pytest.raises(P.ProfileError) as excinfo:
        store.create_profile({"name": "Escape", "user_data_dir": "inside"})
    assert "real Chrome/Chromium profile" in str(excinfo.value)
