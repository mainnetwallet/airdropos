"""Installed-extension detection must see a real Web Store install."""

import json

from backend.app.browser import chrome

EXT = "dafimlnpihgaoeglopkclnfpfneneolh"


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_secure_preferences_install_is_detected(tmp_path):
    _write(
        tmp_path / "Default" / "Secure Preferences",
        {"extensions": {"settings": {EXT: {"location": 1, "state": 1, "from_webstore": True,
                                           "manifest": {"name": "Psy Wallet", "version": "1.0"}}}}},
    )
    _write(tmp_path / "Default" / "Preferences", {"extensions": {"ui": {}}})
    found = chrome.installed_extensions(str(tmp_path))
    assert [(e.id, e.name) for e in found] == [(EXT, "Psy Wallet")]


def test_unflushed_install_is_seen_in_the_extensions_folder(tmp_path):
    _write(tmp_path / "Default" / "Preferences", {})
    _write(
        tmp_path / "Default" / "Extensions" / EXT / "1.2.3_0" / "manifest.json",
        {"name": "__MSG_appName__", "version": "1.2.3", "default_locale": "en",
         "update_url": "https://clients2.google.com/service/update2/crx"},
    )
    _write(
        tmp_path / "Default" / "Extensions" / EXT / "1.2.3_0" / "_locales" / "en" / "messages.json",
        {"appName": {"message": "Psy Wallet"}},
    )
    found = chrome.installed_extensions(str(tmp_path))
    assert len(found) == 1 and found[0].id == EXT
    assert found[0].name == "Psy Wallet" and found[0].from_webstore


def test_nothing_installed_is_empty(tmp_path):
    _write(tmp_path / "Default" / "Preferences", {"extensions": {"settings": {}}})
    assert chrome.installed_extensions(str(tmp_path)) == []


def test_component_extensions_are_ignored(tmp_path):
    _write(
        tmp_path / "Default" / "Secure Preferences",
        {"extensions": {"settings": {EXT: {"location": 5, "manifest": {"name": "Built in"}}}}},
    )
    assert chrome.installed_extensions(str(tmp_path)) == []
