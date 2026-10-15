"""The Wallets-page default settles 'which wallet', and the Telegram account supplies the username."""

from __future__ import annotations

import pytest

from backend.app.database.session import session_scope
from backend.app.models.wallets import Wallet
from backend.app.tools.wallet_tools import GetWalletAddressTool, resolve_wallet
from backend.app.wallets.service import WalletError, WalletService

A = "0x" + "1" * 40
B = "0x" + "2" * 40


async def _two_wallets(a_networks=("BSC",), b_networks=("BSC",)):
    async with session_scope() as db:
        a = Wallet(label="a", address=A, supported_networks=list(a_networks))
        b = Wallet(label="b", address=B, supported_networks=list(b_networks))
        db.add_all([a, b])
        await db.commit()
        return str(a.id), str(b.id)


async def test_without_a_default_two_wallets_are_ambiguous():
    await _two_wallets()
    async with session_scope() as db:
        row, reason = await resolve_wallet(db, network="BSC")
    assert row is None and "2 wallets match" in reason


async def test_the_default_wallet_is_used_when_none_is_named():
    _, b_id = await _two_wallets()
    async with session_scope() as db:
        service = WalletService(db)
        await service.set_default(await service.get(__import__("uuid").UUID(b_id)))
        await db.commit()
    async with session_scope() as db:
        row, reason = await resolve_wallet(db, network="BSC")
    assert reason is None and row.address == B and row.is_default is True


async def test_only_one_wallet_can_be_the_default():
    import uuid

    a_id, b_id = await _two_wallets()
    async with session_scope() as db:
        service = WalletService(db)
        await service.set_default(await service.get(uuid.UUID(a_id)))
        await service.set_default(await service.get(uuid.UUID(b_id)))
        await db.commit()
        a = await service.get(uuid.UUID(a_id))
        b = await service.get(uuid.UUID(b_id))
    assert (a.is_default, b.is_default) == (False, True)


async def test_an_inactive_wallet_cannot_be_default_and_deactivating_clears_it():
    import uuid

    a_id, _ = await _two_wallets()
    async with session_scope() as db:
        service = WalletService(db)
        row = await service.get(uuid.UUID(a_id))
        await service.set_default(row)
        await service.set_active(row, False)
        assert row.is_default is False
        with pytest.raises(WalletError):
            await service.set_default(row)


async def test_a_default_that_does_not_fit_the_network_is_not_forced():
    a_id, _ = await _two_wallets(a_networks=("ETH",), b_networks=("BSC",))
    import uuid

    async with session_scope() as db:
        service = WalletService(db)
        await service.set_default(await service.get(uuid.UUID(a_id)))
        await db.commit()
    async with session_scope() as db:
        row, _ = await resolve_wallet(db, network="BSC")
    assert row.address == B  # the only BSC wallet, not the ETH-only default


async def test_get_wallet_address_tool_returns_the_default():
    import uuid

    _, b_id = await _two_wallets()
    async with session_scope() as db:
        service = WalletService(db)
        await service.set_default(await service.get(uuid.UUID(b_id)))
        await db.commit()
    result = await GetWalletAddressTool().execute({"network": "BSC"})
    assert result.ok and result.data["address"] == B and result.data["is_default"] is True


# --- Telegram account ---------------------------------------------------------------------
class _Me:
    def __init__(self, username):
        self.username, self.first_name, self.last_name, self.id = username, "Mx", "W", 42


async def _run_with_me(monkeypatch, me):
    from backend.app.tools import telegram_user_tools as t

    class _Client:
        async def get_me(self):
            return me

    async def fake_run(action, **_):
        return await action(_Client())

    monkeypatch.setattr(t._session, "run", fake_run)
    return await t.TelegramGetMyAccountTool().execute({})


async def test_telegram_account_tool_returns_the_logged_in_username(monkeypatch):
    result = await _run_with_me(monkeypatch, _Me("mainw"))
    assert result.ok
    assert result.data["handle"] == "@mainw" and "phone" not in str(result.data).lower()


async def test_telegram_account_without_username_asks_the_operator(monkeypatch):
    result = await _run_with_me(monkeypatch, _Me(None))
    assert not result.ok and result.error_category == "HUMAN_INTERVENTION_REQUIRED"
