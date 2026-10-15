"""Add / set-default / remove Telegram accounts through the API (fake Telethon, no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import List

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.core.config import settings
from backend.app.database.session import Base, get_db
from backend.app.integrations import telegram_accounts as reg
from backend.app.integrations import telegram_login as login
from backend.app.main import app
from backend.app.models.users import User, UserSession
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token

PHONE = "+8801712345678"
SECRET_CODE = "48213"
SECRET_PASSWORD = "hunter2-2fa"


class PhoneCodeInvalidError(Exception):
    pass


class SessionPasswordNeededError(Exception):
    pass


class PasswordHashInvalidError(Exception):
    pass


class FakeClient:
    """Stands in for Telethon. The account with 2FA is the one whose phone ends in 99."""

    instances: List["FakeClient"] = []

    def __init__(self, path, api_id, api_hash):
        self.path, self.connected, self.logged_in, self.logged_out = path, False, False, False
        self.phone = None
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def send_code_request(self, phone):
        self.phone = phone
        open(f"{self.path}.session", "wb").write(b"s")
        return SimpleNamespace(phone_code_hash="hash123")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is not None:
            if password != SECRET_PASSWORD:
                raise PasswordHashInvalidError("bad password for " + self.phone)
            self.logged_in = True
            return
        if code != SECRET_CODE:
            raise PhoneCodeInvalidError("bad code for " + phone)
        if phone.endswith("99"):
            raise SessionPasswordNeededError("2fa")
        self.logged_in = True

    async def get_me(self):
        return SimpleNamespace(id=77, first_name="Ada", last_name="L", username="ada", phone=self.phone)

    async def is_user_authorized(self):
        return True

    async def log_out(self):
        self.logged_out = True
        return True


@pytest.fixture(autouse=True)
def _fake_telegram(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_SESSION_PATH", str(tmp_path / "tg" / "agent"))
    monkeypatch.setattr(settings, "TELEGRAM_API_ID", 1)
    monkeypatch.setattr(settings, "TELEGRAM_API_HASH", "h")
    monkeypatch.setattr(login, "TelegramClient", FakeClient)
    monkeypatch.setattr(login, "TELETHON_AVAILABLE", True)
    FakeClient.instances.clear()
    login._pending.clear()
    from backend.app.tools import telegram_user_tools as tools

    tools._extra_sessions.clear()
    yield
    login._pending.clear()


@pytest_asyncio.fixture
async def api():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    made = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def override():
        async with made() as session:
            yield session

    app.dependency_overrides[get_db] = override

    async def client(scopes):
        token = generate_token()
        async with made() as db:
            user = User(email=f"{token[:6]}@t.local", hashed_password=hash_password("correct-horse-battery"),
                        is_active=True, is_superuser=True)
            db.add(user)
            await db.flush()
            db.add(UserSession(user_id=user.id, token=hash_token(token),
                               expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                               scopes=sorted(s.value for s in scopes)))
            await db.commit()
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"})

    yield client
    app.dependency_overrides.clear()
    await engine.dispose()


async def _add(c, label="main", phone=PHONE, **extra):
    r = await c.post("/api/v1/telegram/accounts/login/start", json={"label": label, "phone": phone, **extra})
    assert r.status_code == 200, r.text
    return r.json()["login_id"]


async def test_add_an_account_with_phone_then_code(api):
    async with await api(list(Scope)) as c:
        lid = await _add(c, "main")
        r = await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": lid, "code": SECRET_CODE})
        assert r.json() == {"stage": "done", "label": "main"}
        listed = (await c.get("/api/v1/telegram/accounts")).json()["accounts"]
    assert [(a["label"], a["profile_name"], a["is_default"], a["logged_in"]) for a in listed] == [
        ("main", "Ada L", True, True)
    ]
    assert not login._pending  # nothing left in memory


async def test_two_factor_account_needs_the_password_step(api):
    async with await api(list(Scope)) as c:
        lid = await _add(c, "vault", phone="+8801700000099")
        r = await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": lid, "code": SECRET_CODE})
        assert r.json() == {"stage": "password"}
        bad = await c.post("/api/v1/telegram/accounts/login/password", json={"login_id": lid, "password": "nope"})
        assert bad.status_code == 400 and bad.json()["detail"]["field"] == "password"
        ok = await c.post("/api/v1/telegram/accounts/login/password",
                          json={"login_id": lid, "password": SECRET_PASSWORD})
        assert ok.json()["stage"] == "done"
        assert [a["label"] for a in (await c.get("/api/v1/telegram/accounts")).json()["accounts"]] == ["vault"]


async def test_wrong_code_can_be_retried_but_attempts_are_capped(api):
    async with await api(list(Scope)) as c:
        lid = await _add(c)
        for _ in range(login.MAX_ATTEMPTS):
            r = await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": lid, "code": "11111"})
            assert r.status_code == 400 and r.json()["detail"]["field"] == "code"
        r = await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": lid, "code": SECRET_CODE})
        assert r.status_code == 429
        assert not login._pending and not list(reg.accounts_dir().glob("*.session"))


async def test_secrets_are_never_echoed_stored_or_audited(api, caplog):
    async with await api(list(Scope)) as c:
        lid = await _add(c, "vault", phone="+8801700000099")
        bad = await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": lid, "code": "99999"})
        await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": lid, "code": SECRET_CODE})
        await c.post("/api/v1/telegram/accounts/login/password", json={"login_id": lid, "password": SECRET_PASSWORD})
        wrong = await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": "x", "code": SECRET_CODE})
    for text in (bad.text, wrong.text, caplog.text, reg.registry_file().read_text()):
        for secret in (PHONE, "8801700000099", SECRET_CODE, "99999", SECRET_PASSWORD):
            assert secret not in text


async def test_duplicate_or_invalid_labels_and_phone_are_refused(api):
    async with await api(list(Scope)) as c:
        lid = await _add(c, "main")
        await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": lid, "code": SECRET_CODE})
        for body in ({"label": "MAIN", "phone": PHONE}, {"label": "../x", "phone": PHONE},
                     {"label": "ok", "phone": "12"}):
            r = await c.post("/api/v1/telegram/accounts/login/start", json=body)
            assert r.status_code in (400, 409)


async def test_cancel_drops_the_pending_login_and_its_session_file(api):
    async with await api(list(Scope)) as c:
        lid = await _add(c, "temp")
        await c.post("/api/v1/telegram/accounts/login/cancel", json={"login_id": lid})
        r = await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": lid, "code": SECRET_CODE})
    assert r.status_code == 410 and not list(reg.accounts_dir().glob("*.session"))


async def test_set_default_and_remove_with_revoke(api):
    async with await api(list(Scope)) as c:
        for label in ("a", "b"):
            lid = await _add(c, label)
            await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": lid, "code": SECRET_CODE})
        assert (await c.post("/api/v1/telegram/accounts/b/default")).status_code == 200
        accounts = (await c.get("/api/v1/telegram/accounts")).json()["accounts"]
        assert [(x["label"], x["is_default"]) for x in accounts] == [("b", True), ("a", False)]

        r = await c.delete("/api/v1/telegram/accounts/b")
        assert r.json() == {"removed": "b", "revoked_on_telegram": True}
        assert any(i.logged_out for i in FakeClient.instances)
        accounts = (await c.get("/api/v1/telegram/accounts")).json()["accounts"]
        assert [(x["label"], x["is_default"]) for x in accounts] == [("a", True)]  # default moved on
        assert not (reg.accounts_dir() / "b.session").exists()
        assert (await c.delete("/api/v1/telegram/accounts/ghost")).status_code == 404


async def test_remove_without_revoke_only_deletes_locally(api):
    async with await api(list(Scope)) as c:
        lid = await _add(c, "a")
        await c.post("/api/v1/telegram/accounts/login/code", json={"login_id": lid, "code": SECRET_CODE})
        before = len(FakeClient.instances)
        r = await c.delete("/api/v1/telegram/accounts/a?revoke=false")
    assert r.json()["revoked_on_telegram"] is False and len(FakeClient.instances) == before
    assert not (reg.accounts_dir() / "a.session").exists()


async def test_the_old_single_session_can_be_removed(api):
    legacy = reg.resolve_path(settings.TELEGRAM_SESSION_PATH)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    (legacy.parent / "agent.session").write_bytes(b"x")
    async with await api(list(Scope)) as c:
        assert (await c.get("/api/v1/telegram/accounts")).json()["accounts"][0]["label"] == "default"
        assert (await c.post("/api/v1/telegram/accounts/default/default")).status_code == 409
        assert (await c.delete("/api/v1/telegram/accounts/default")).status_code == 200
        assert (await c.get("/api/v1/telegram/accounts")).json()["accounts"] == []


async def test_changing_accounts_needs_the_system_admin_scope(api):
    async with await api([Scope.READ]) as c:
        assert (await c.get("/api/v1/telegram/accounts")).status_code == 200
        for method, url, body in (
            ("post", "/api/v1/telegram/accounts/login/start", {"label": "x", "phone": PHONE}),
            ("post", "/api/v1/telegram/accounts/main/default", None),
            ("delete", "/api/v1/telegram/accounts/main", None),
        ):
            r = await getattr(c, method)(url, **({"json": body} if body else {}))
            assert r.status_code == 403, url


async def test_login_is_refused_until_api_credentials_are_configured(api, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_API_HASH", None)
    async with await api(list(Scope)) as c:
        r = await c.post("/api/v1/telegram/accounts/login/start", json={"label": "x", "phone": PHONE})
    assert r.status_code == 409
