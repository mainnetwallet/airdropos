"""Several Telegram accounts: login labels, the account registry, and per-account sessions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.core.config import settings
from backend.app.integrations import telegram_accounts as reg
from backend.app.tools import telegram_user_tools as t


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_SESSION_PATH", str(tmp_path / "tg" / "agent"))
    t._extra_sessions.clear()
    yield
    t._extra_sessions.clear()


def _me(first, last=None, username=None, uid=1):
    return SimpleNamespace(id=uid, first_name=first, last_name=last, username=username)


def _login(label, me, **kw):
    path = reg.accounts_dir() / reg.slugify(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / f"{path.name}.session").write_bytes(b"x")
    return reg.add_account(label, me, str(path), **kw)


def test_two_accounts_are_listed_with_profile_names_and_the_first_is_default():
    _login("main", _me("Ada", "Lovelace", "ada", 11))
    _login("farm 2", _me("Bob", None, None, 22))
    accounts = reg.list_accounts()
    assert [a["label"] for a in accounts] == ["main", "farm 2"]
    assert accounts[0]["is_default"] and not accounts[1]["is_default"]
    assert accounts[0]["profile_name"] == "Ada Lovelace"
    assert accounts[1]["profile_name"] == "Bob"
    assert all(a["session_ready"] for a in accounts)


def test_registry_never_stores_a_phone_number():
    me = _me("Ada", "L", "ada")
    me.phone = "+8801700000000"
    _login("main", me)
    assert "phone" not in reg.registry_file().read_text()


def test_label_is_validated_and_logging_in_again_updates_in_place():
    with pytest.raises(ValueError):
        reg.add_account("../evil", _me("x"), "p")
    _login("main", _me("Old", None, "old", 5))
    _login("main", _me("New", None, "new", 5))
    accounts = reg.list_accounts()
    assert len(accounts) == 1 and accounts[0]["profile_name"] == "New" and accounts[0]["is_default"]


def test_set_default_and_remove():
    _login("a", _me("A"))
    _login("b", _me("B"))
    assert reg.set_default("b")
    assert reg.get_account()["label"] == "b"
    assert reg.remove_account("b")
    assert reg.get_account()["label"] == "a" and reg.get_account()["is_default"]
    assert not reg.remove_account("nope")


def test_old_single_session_still_works_as_the_default_account():
    legacy = reg.resolve_path(settings.TELEGRAM_SESSION_PATH)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    (legacy.parent / "agent.session").write_bytes(b"x")
    accounts = reg.list_accounts()
    assert [a["label"] for a in accounts] == ["default"] and accounts[0]["session_ready"]
    assert t.session_for(None) is t._session


def test_with_nothing_registered_the_default_is_the_legacy_session():
    assert t.session_for(None) is t._session


def test_session_for_routes_each_label_to_its_own_session():
    _login("main", _me("Ada"))
    _login("farm", _me("Bob"))
    main, farm = t.session_for(None), t.session_for("FARM")
    assert main is t.session_for("main") and main is not farm
    assert (main.label, farm.label) == ("main", "farm")
    assert main.SESSION_ID != farm.SESSION_ID
    assert main._lock is not farm._lock  # accounts never block each other
    assert t.session_for("farm") is farm  # same object on the next call


def test_unknown_label_lists_the_logged_in_accounts():
    _login("main", _me("Ada"))
    with pytest.raises(t.UnknownAccount) as exc:
        t.session_for("ghost")
    assert "main" in str(exc.value)


async def test_a_tool_called_with_an_unknown_account_fails_cleanly():
    _login("main", _me("Ada"))
    result = await t.TelegramGetMyAccountTool().execute({"account": "ghost"})
    assert result.error_category == "INVALID_INPUT" and "main" in result.error


async def test_a_tool_uses_the_session_of_the_named_account(monkeypatch):
    _login("main", _me("Ada"))
    _login("farm", _me("Bob"))
    seen = []

    async def fake_run(self, fn, *, interactive, label="x"):
        seen.append(self.label)
        return t.ToolResult.success(data={})

    monkeypatch.setattr(t._TelegramUserSession, "run", fake_run)
    await t.TelegramListDialogsTool().execute({"account": "farm"})
    await t.TelegramListDialogsTool().execute({})
    assert seen == ["farm", "main"]


def test_registered_tools_all_accept_the_account_property():
    class _Reg:
        def register_or_replace_many(self, tools):
            self.tools = tools

    r = _Reg()
    old = (settings.TELEGRAM_USER_ENABLED, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
    settings.TELEGRAM_USER_ENABLED, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH = True, 1, "h"
    try:
        if not t.TELETHON_AVAILABLE:
            pytest.skip("telethon not installed")
        assert t.register_telegram_user_tools(r)
    finally:
        settings.TELEGRAM_USER_ENABLED, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH = old
    assert r.tools and all("account" in tool.input_schema["properties"] for tool in r.tools)
