"""Browser profile REST API: persistence, defaults, lifecycle and data safety.

The API never talks to Chrome. These tests replace the worker gateway with a recording
stub so the API's own behaviour - especially its refusal to claim success - is checked.
Authentication runs end to end against a real in-memory database: nothing here stubs
``get_principal`` away.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from backend.app.browser import chrome
from backend.app.browser import profiles as P
from backend.app.database.session import get_db
from backend.app.live import client as gateway_client
from backend.app.main import app
from backend.app.models import Base, User, UserSession
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003
    """Render PostgreSQL JSONB columns as SQLite JSON (same shim the auth suite uses)."""
    return "JSON"


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(P, "ROOT", root)
    monkeypatch.setattr(P, "_meta_path", lambda: root / "profiles.json")
    monkeypatch.setattr(chrome, "profile_root", lambda: root / "ChromeProfiles")
    return P


class FakeGateway:
    """Stands in for the worker. Records calls; can be told to be unreachable."""

    def __init__(self):
        self.running = False
        self.unreachable = False
        self.calls = []
        self.open_result = None

    async def get_json(self, path, params=None):
        self.calls.append(("GET", path))
        if self.unreachable:
            raise gateway_client.GatewayUnavailable("connection refused")
        if path == "/sessions":
            return {"sessions": [], "degraded": []}
        if path == "/browser/profiles":
            # One call for every profile: the page polls this, so per-profile calls are
            # what this endpoint exists to avoid.
            from backend.app.browser import profiles as P

            return {"statuses": {p.id: self._status(p.id) for p in P.list_profiles()}}
        if path.startswith("/browser/profiles/") and path.endswith("/status"):
            return {"status": self._status(path.split("/")[3])}
        if path.startswith("/browser/profiles/") and path.endswith("/extensions"):
            return {"extensions": {"ok": True, "installed": [], "installed_count": 0}}
        if path.startswith("/sessions/browser/"):
            raise gateway_client.GatewayError(404, "not_found", "no session")
        raise gateway_client.GatewayError(404, "not_found", "no such route")

    async def post_json(self, path, body):
        self.calls.append(("POST", path, body))
        if self.unreachable:
            raise gateway_client.GatewayUnavailable("connection refused")
        if path == "/browser/profiles/open":
            self.running = True
            if self.open_result is not None:
                return self.open_result
            return {"status": self._status(body["profile_id"]), "reused": False}
        if path == "/browser/profiles/reconnect":
            self.running = True
            return {"status": self._status(body["profile_id"])}
        if path == "/browser/profiles/close":
            self.running = False
            return {"closed": body["profile_id"], "was_running": True, "profile_data_kept": True}
        raise gateway_client.GatewayError(404, "not_found", "no such route")

    def _status(self, profile_id):
        return {
            "ok": True, "profile_id": profile_id, "running": self.running,
            "connection": "managed_persistent" if self.running else "not_running",
            "mode": "persistent" if self.running else "none",
            "tabs": [{"tab_id": "tab1", "index": 0, "url": "https://example.com", "active": True,
                      "closed": False, "title": "Example", "raw_scheme": "https"}] if self.running else [],
            "tab_count": 1 if self.running else 0,
            "active_url": "https://example.com" if self.running else "",
            "active_title": "Example" if self.running else "",
            "user_data_dir": "C:/profiles/x",
            "extensions": {"installed": [], "installed_count": 0},
            "last_error": "", "limitations": [],
        }


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(gateway_client, "get_json", fake.get_json)
    monkeypatch.setattr(gateway_client, "post_json", fake.post_json)
    return fake


async def _operator_token(factory) -> str:
    """A real superuser with a real session row: the endpoints see a genuine principal."""
    email = f"op-{uuid.uuid4().hex[:8]}@test.local"
    async with factory() as db:
        user = User(email=email, hashed_password=hash_password("test-operator-password"),
                    is_active=True, is_superuser=True)
        db.add(user)
        await db.flush()
        token = generate_token()
        db.add(
            UserSession(
                user_id=user.id,
                token=hash_token(token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                scopes=[Scope.READ.value, Scope.TASK_WRITE.value],
            )
        )
        await db.commit()
    return token


@pytest.fixture
async def api(store, gateway):
    """The real app, a real in-memory database and a real authenticated operator."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    token = await _operator_token(factory)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            client.store = P
            client.gateway = gateway
            yield client
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


# ------------------------------------------------------------------ routes
async def test_browser_profiles_route_exists_and_returns_real_state(api, gateway):
    response = await api.get("/api/v1/browser/profiles")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profiles"] == []
    assert body["worker_reachable"] is True
    assert "chrome" in body and "profile_root" in body
    assert "persistent_profiles_enabled" in body


async def test_the_listing_asks_the_worker_once_not_once_per_profile(api, gateway):
    """The page polls this every few seconds; N worker round trips per poll is a defect."""
    for name in ("One", "Two", "Three", "Four"):
        await api.post("/api/v1/browser/profiles", json={"name": name})
    gateway.calls.clear()
    response = await api.get("/api/v1/browser/profiles")
    assert response.status_code == 200
    assert len(response.json()["profiles"]) == 4
    worker_calls = [c for c in gateway.calls if c[1].startswith("/browser/profiles")]
    assert len(worker_calls) == 1, worker_calls


async def test_create_profile_persists_its_configuration(api, store):
    response = await api.post("/api/v1/browser/profiles", json={"name": "API Profile"})
    assert response.status_code == 201, response.text
    profile = response.json()
    assert profile["name"] == "API Profile"
    assert profile["launch_mode"] == "managed"
    assert profile["resolved_user_data_dir"].endswith(profile["id"])
    assert store.get_profile(profile["id"]) is not None
    # The first profile becomes the agent default, and that choice is persisted.
    assert store.default_agent_profile_id() == profile["id"]
    assert profile["is_default_agent"] is True


async def test_create_profile_refuses_a_personal_chrome_directory(api):
    import os
    from pathlib import Path

    personal = str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data")
    response = await api.post("/api/v1/browser/profiles", json={"name": "Bad", "user_data_dir": personal})
    assert response.status_code == 400
    assert "real Chrome/Chromium profile" in response.json()["detail"]["error"]


async def test_open_profile_reports_the_workers_real_answer(api, gateway):
    created = (await api.post("/api/v1/browser/profiles", json={"name": "Openable"})).json()
    response = await api.post(f"/api/v1/browser/profiles/{created['id']}/open", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile"]["browser_running"] is True
    assert body["profile"]["connection"] == "managed_persistent"
    assert body["reused"] is False


async def test_open_profile_refuses_when_the_worker_says_it_did_not_start(api, gateway):
    created = (await api.post("/api/v1/browser/profiles", json={"name": "Broken"})).json()
    gateway.open_result = {"status": {"running": False, "last_error": "Chrome refused to start",
                                      "connection": "not_running"}}
    response = await api.post(f"/api/v1/browser/profiles/{created['id']}/open", json={})
    assert response.status_code == 409
    assert "Chrome refused to start" in response.json()["detail"]["error"]


async def test_open_profile_503_when_the_worker_is_unreachable(api, gateway):
    created = (await api.post("/api/v1/browser/profiles", json={"name": "Down"})).json()
    gateway.unreachable = True
    response = await api.post(f"/api/v1/browser/profiles/{created['id']}/open", json={})
    assert response.status_code == 503
    assert "no browser was started" in response.json()["detail"]["error"]


async def test_close_profile_reports_that_the_data_was_kept(api, gateway):
    created = (await api.post("/api/v1/browser/profiles", json={"name": "Closable"})).json()
    await api.post(f"/api/v1/browser/profiles/{created['id']}/open", json={})
    response = await api.post(f"/api/v1/browser/profiles/{created['id']}/close")
    assert response.status_code == 200
    assert response.json()["profile_data_kept"] is True


async def test_reconnect_profile(api, gateway):
    created = (await api.post("/api/v1/browser/profiles", json={"name": "Reconnectable"})).json()
    response = await api.post(f"/api/v1/browser/profiles/{created['id']}/reconnect")
    assert response.status_code == 200
    assert response.json()["profile"]["browser_running"] is True


async def test_set_and_clear_the_default_agent_profile(api, store):
    first = (await api.post("/api/v1/browser/profiles", json={"name": "One"})).json()
    second = (await api.post("/api/v1/browser/profiles", json={"name": "Two"})).json()
    response = await api.post("/api/v1/browser/profiles/default", json={"profile_id": second["id"]})
    assert response.status_code == 200
    assert response.json()["default_agent_profile"] == second["id"]
    assert store.get_default_agent_profile().id == second["id"]
    assert store.get_profile(first["id"]).is_default_agent is False

    listing = (await api.get("/api/v1/browser/profiles")).json()
    assert listing["default_agent_profile"] == second["id"]
    assert [p["id"] for p in listing["profiles"] if p["is_default_agent"]] == [second["id"]]

    cleared = await api.post("/api/v1/browser/profiles/default", json={"profile_id": ""})
    assert cleared.json()["default_agent_profile"] is None
    assert store.get_default_agent_profile() is None


async def test_extensions_endpoint_returns_the_workers_list(api, gateway):
    created = (await api.post("/api/v1/browser/profiles", json={"name": "Ext"})).json()
    response = await api.get(f"/api/v1/browser/profiles/{created['id']}/extensions")
    assert response.status_code == 200
    assert response.json()["installed"] == []


async def test_extensions_endpoint_says_unknown_when_the_worker_is_down(api, gateway):
    created = (await api.post("/api/v1/browser/profiles", json={"name": "ExtDown"})).json()
    gateway.unreachable = True
    response = await api.get(f"/api/v1/browser/profiles/{created['id']}/extensions")
    assert response.status_code == 200
    assert "unknown" in response.json()["note"]


# ------------------------------------------------------------------ deletion safety
async def test_delete_configuration_keeps_the_chrome_data(api, store, tmp_path):
    directory = tmp_path / "keep"
    directory.mkdir()
    (directory / "marker.txt").write_text("keep me", encoding="utf-8")
    created = (await api.post("/api/v1/browser/profiles", json={
        "name": "Keeper", "user_data_dir": str(directory),
    })).json()
    response = await api.delete(f"/api/v1/browser/profiles/{created['id']}")
    assert response.status_code == 200
    assert (directory / "marker.txt").read_text(encoding="utf-8") == "keep me"
    assert response.json()["chrome_data_kept_at"] == str(directory)


async def test_delete_data_requires_the_profile_id_back(api, store, tmp_path):
    directory = tmp_path / "wipe"
    directory.mkdir()
    (directory / "marker.txt").write_text("x", encoding="utf-8")
    created = (await api.post("/api/v1/browser/profiles", json={
        "name": "Wipe", "user_data_dir": str(directory),
    })).json()
    wrong = await api.request("DELETE", f"/api/v1/browser/profiles/{created['id']}/data",
                              json={"confirm": "nope"})
    assert wrong.status_code == 400
    assert (directory / "marker.txt").exists()
    right = await api.request("DELETE", f"/api/v1/browser/profiles/{created['id']}/data",
                              json={"confirm": created["id"]})
    assert right.status_code == 200
    assert not directory.exists()


async def test_delete_data_fails_closed_when_the_worker_is_unreachable(api, gateway, store, tmp_path):
    directory = tmp_path / "wipe2"
    directory.mkdir()
    created = (await api.post("/api/v1/browser/profiles", json={
        "name": "Wipe2", "user_data_dir": str(directory),
    })).json()
    gateway.unreachable = True
    response = await api.request("DELETE", f"/api/v1/browser/profiles/{created['id']}/data",
                                 json={"confirm": created["id"]})
    assert response.status_code == 503
    assert directory.exists()


# ------------------------------------------------------------------ auth
async def test_profile_endpoints_require_authentication(store, gateway):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as anon:
            for method, path in (
                ("GET", "/api/v1/browser/profiles"),
                ("POST", "/api/v1/browser/profiles"),
                ("POST", "/api/v1/browser/profiles/default"),
            ):
                response = await anon.request(method, path, json={})
                assert response.status_code in (401, 403), (method, path, response.status_code)
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
