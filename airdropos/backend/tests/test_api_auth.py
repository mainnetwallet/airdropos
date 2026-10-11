"""Regression tests for REST authentication and authorization.

Every protected route must refuse an unauthenticated caller and refuse a caller
that holds a valid session but lacks the required scope. These tests are the
guard against a future refactor accidentally dropping a `Depends(require_*)`.

A real in-memory database backs the app so the genuine `get_principal` code path
runs end to end: these are not tests that stub authentication away.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect
from starlette.testclient import TestClient
from sqlalchemy import JSON, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.database.session import get_db
from backend.app.main import app
from backend.app.models import Base, User, UserSession
from backend.app.security.auth import (
    Scope,
    generate_token,
    hash_password,
    hash_token,
)

# The models declare PostgreSQL JSONB columns. SQLite has no JSONB type, so the
# type is rendered as plain JSON for these tests. This affects DDL and encoding
# only; the application code paths under test are unchanged.
if not hasattr(JSONB, "compile"):  # pragma: no cover - defensive
    raise RuntimeError("Unexpected SQLAlchemy JSONB definition")

from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003
    """Render PostgreSQL JSONB columns as SQLite JSON."""
    return "JSON"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db_session_factory():
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

    yield factory

    app.dependency_overrides.clear()
    await engine.dispose()


async def _make_user(factory, *, scopes: set[str], active: bool = True) -> tuple[str, str]:
    """Create a user plus a live session. Returns (raw_token, email)."""
    email = f"user-{uuid.uuid4().hex[:8]}@test.local"
    async with factory() as db:
        user = User(
            email=email,
            hashed_password=hash_password("correct-horse-battery"),
            is_active=active,
            is_superuser=False,
        )
        db.add(user)
        await db.flush()

        token = generate_token()
        db.add(
            UserSession(
                user_id=user.id,
                token=hash_token(token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                scopes=sorted(scopes),
            )
        )
        await db.commit()
    return token, email


async def _make_expired_session(factory) -> str:
    email = f"expired-{uuid.uuid4().hex[:8]}@test.local"
    async with factory() as db:
        user = User(
            email=email,
            hashed_password=hash_password("correct-horse-battery"),
            is_active=True,
            is_superuser=False,
        )
        db.add(user)
        await db.flush()
        token = generate_token()
        db.add(
            UserSession(
                user_id=user.id,
                token=hash_token(token),
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
                scopes=[Scope.READ.value],
            )
        )
        await db.commit()
    return token


@pytest.fixture
async def client(db_session_factory):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


def _run(coro):
    """Run a coroutine to completion from synchronous test code.

    The websocket tests are synchronous (Starlette's TestClient cannot be driven
    from inside a running event loop) but still need the async session factory.
    """
    return asyncio.run(coro)


async def _get_session_by_token(factory, raw_token: str):
    async with factory() as db:
        return await db.scalar(
            select(UserSession).where(UserSession.token == hash_token(raw_token))
        )


def client_no_db():
    """A raw ASGI client for WebSocket tests, which do not use dependency injection."""
    return TestClient(app)


# ======================================================================
# Unauthenticated access must be refused
# ======================================================================
PROTECTED_GETS = [
    "/api/v1/readiness",
    "/api/v1/agent/info",
    "/api/v1/overview",
    "/api/v1/tasks",
    "/api/v1/tasks/AIR-2026-000001",
    "/api/v1/goals",
    "/api/v1/wallets",
    "/api/v1/tools",
    "/api/v1/tools/catalog",
    "/api/v1/memory",
    "/api/v1/improvements",
    "/api/v1/diagnostics",
    "/api/v1/campaigns",
    "/api/v1/campaigns/00000000-0000-0000-0000-000000000000",
    "/api/v1/transactions",
    "/api/v1/audit",
    "/api/v1/settings",
    "/api/v1/emergency-stop",
    "/api/v1/health/detail",
]

PROTECTED_POSTS = [
    "/api/v1/tasks",
    "/api/v1/learning/analyze",
    "/api/v1/agent/chat",
    "/api/v1/emergency-stop",
]


@pytest.mark.parametrize("path", PROTECTED_GETS)
@pytest.mark.asyncio
async def test_protected_get_requires_auth(client, db_session_factory, path):
    """No credentials means 401, never 200."""
    response = await client.get(path)
    assert response.status_code == 401, f"{path} returned {response.status_code}"


@pytest.mark.parametrize("path", PROTECTED_POSTS)
@pytest.mark.asyncio
async def test_protected_post_requires_auth(client, db_session_factory, path):
    response = await client.post(path, json={})
    assert response.status_code == 401, f"{path} returned {response.status_code}"


@pytest.mark.asyncio
async def test_bogus_token_is_rejected(client, db_session_factory):
    """An arbitrary string must not authenticate, even with the Bearer scheme."""
    response = await client.get(
        "/api/v1/overview", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_session_is_rejected(client, db_session_factory):
    token = await _make_expired_session(db_session_factory)
    response = await client.get(
        "/api/v1/overview", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


# ======================================================================
# Public endpoints stay reachable
# ======================================================================
@pytest.mark.asyncio
async def test_login_and_root_remain_public(client, db_session_factory):
    """Authentication entry points must work without credentials."""
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/api/v1/health")).status_code == 200
    assert (await client.post("/api/v1/auth/login", json={
        "email": "nobody@test.local", "password": "wrong-password"
    })).status_code == 401


@pytest.mark.asyncio
async def test_public_root_does_not_leak_operational_detail(client, db_session_factory):
    """The banner must not advertise the model, socket path, or stop state."""
    body = (await client.get("/")).json()
    assert "fixed_model" not in body
    assert "websocket" not in body
    assert "emergency_stop" not in body


@pytest.mark.asyncio
async def test_public_health_does_not_leak_internals(client, db_session_factory):
    body = (await client.get("/api/v1/health")).json()
    assert "llm" not in body
    assert "browser" not in body
    assert "telegram" not in body


# ======================================================================
# Scope enforcement: authenticated is not the same as authorized
# ======================================================================
@pytest.mark.asyncio
async def test_read_scope_cannot_reach_admin_route(client, db_session_factory):
    token, _ = await _make_user(db_session_factory, scopes={Scope.READ.value})
    response = await client.get(
        "/api/v1/settings", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200  # read is allowed to list settings

    patch = await client.patch(
        "/api/v1/settings/anything",
        headers={"Authorization": f"Bearer {token}"},
        json={"value": "x"},
    )
    assert patch.status_code == 403, "read-only session must not write settings"


@pytest.mark.asyncio
async def test_read_scope_cannot_engage_emergency_stop(client, db_session_factory):
    token, _ = await _make_user(db_session_factory, scopes={Scope.READ.value})
    response = await client.post(
        "/api/v1/emergency-stop",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "unauthorized attempt"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_read_scope_cannot_manage_tools_or_wallets(client, db_session_factory):
    """Wallet and tool metadata require their own scopes, not generic `read`."""
    token, _ = await _make_user(db_session_factory, scopes={Scope.READ.value})
    headers = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/api/v1/wallets", headers=headers)).status_code == 403
    assert (await client.get("/api/v1/tools", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_audit_scope_is_required_for_audit(client, db_session_factory):
    token, _ = await _make_user(db_session_factory, scopes={Scope.READ.value})
    response = await client.get(
        "/api/v1/audit", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_task_write_scope_allows_task_creation(client, db_session_factory):
    token, _ = await _make_user(
        db_session_factory, scopes={Scope.READ.value, Scope.TASK_WRITE.value}
    )
    response = await client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Regression test task"},
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_inactive_account_cannot_authenticate(client, db_session_factory):
    token, _ = await _make_user(
        db_session_factory, scopes={Scope.READ.value}, active=False
    )
    response = await client.get(
        "/api/v1/overview", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_auth_me_reports_granted_scopes(client, db_session_factory):
    token, email = await _make_user(db_session_factory, scopes={Scope.READ.value})
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == email
    assert body["scopes"] == [Scope.READ.value]


# ======================================================================
# Secret redaction
# ======================================================================
@pytest.mark.asyncio
async def test_secret_settings_are_never_returned(client, db_session_factory):
    """Even an admin cannot read a secret back; settings are write-only."""
    from backend.app.models import SystemSetting

    async with db_session_factory() as db:
        db.add(
            SystemSetting(
                key="llm.api_key",
                value="super-secret-value",
                is_secret=True,
                description="LLM credential",
            )
        )
        await db.commit()

    token, _ = await _make_user(
        db_session_factory, scopes={Scope.READ.value, Scope.SYSTEM_ADMIN.value}
    )
    headers = {"Authorization": f"Bearer {token}"}

    listing = await client.get("/api/v1/settings", headers=headers)
    assert listing.status_code == 200
    assert "super-secret-value" not in listing.text

    single = await client.get("/api/v1/settings/llm.api_key", headers=headers)
    assert single.status_code == 200
    assert single.json()["value"] == "[REDACTED]"


# ======================================================================
# WebSocket authentication
# ======================================================================
# The socket handler builds its own session from the module-level
# `AsyncSessionLocal`, so these tests rebind it to the in-memory database. They
# are synchronous because Starlette's TestClient — the only ASGI client that
# supports websockets — drives the app from a separate portal thread and cannot
# be awaited from inside an existing event loop.
@pytest.fixture
def ws_client(db_session_factory, monkeypatch):
    from backend.app.api.v1.endpoints import ws as ws_module

    monkeypatch.setattr(ws_module, "AsyncSessionLocal", db_session_factory)
    with TestClient(app) as c:
        yield c


def test_websocket_rejects_arbitrary_token(ws_client):
    """A random string must not open the event stream.

    The token query parameter used to be checked only for emptiness, which meant
    any non-empty value connected. It must be a real, unexpired session token.
    """
    for query in ("?token=totally-made-up", "?token=", ""):
        with pytest.raises(WebSocketDisconnect):
            with ws_client.websocket_connect(f"/api/v1/ws{query}") as ws:
                ws.receive_text()


def test_websocket_rejects_expired_session(ws_client, db_session_factory):
    """An expired session must not keep a live stream open."""
    token = _run(_make_expired_session(db_session_factory))
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            ws.receive_text()


def test_websocket_rejects_session_without_read_scope(ws_client, db_session_factory):
    """Authentication alone is not enough; the stream requires the read scope."""
    token, _ = _run(
        _make_user(db_session_factory, scopes={Scope.TASK_WRITE.value})
    )
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            ws.receive_text()


def test_websocket_accepts_valid_session(ws_client, db_session_factory):
    """A live, in-scope session connects and receives the connected event."""
    token, _ = _run(
        _make_user(db_session_factory, scopes={Scope.READ.value})
    )
    with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
        event = ws.receive_json()
        assert event["type"] == "connected"
        assert event["payload"]["authenticated"] is True
        assert Scope.READ.value in event["payload"]["scopes"]

        # The stream stays open and answers heartbeats.
        ws.send_text("ping")
        assert ws.receive_json()["type"] == "pong"


def test_websocket_updates_session_last_used(ws_client, db_session_factory):
    """A successful socket handshake counts as session usage."""
    token, _ = _run(
        _make_user(db_session_factory, scopes={Scope.READ.value})
    )
    with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
        ws.receive_json()

    stored = _run(_get_session_by_token(db_session_factory, token))
    assert stored is not None
    assert stored.last_used_at is not None