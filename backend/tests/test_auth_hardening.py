"""Regression tests for the authentication / authorization hardening pass.

Covers: revoked sessions, empty session scopes, REST/WebSocket parity, token
transport (no query-string tokens), password policy, and fail-closed
authorization-policy checks (networks, wallet ids, authorized addresses).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from backend.app.models import User, UserSession
from backend.app.security.auth import (
    PBKDF2_MIN_ROUNDS,
    PBKDF2_ROUNDS,
    MIN_PASSWORD_LENGTH,
    Scope,
    generate_token,
    hash_password,
    hash_token,
    is_valid_password_hash,
    verify_password,
)

# Reuse the real-database fixtures and helpers from the REST auth suite.
from backend.tests.test_api_auth import (  # noqa: F401
    _get_session_by_token,
    _make_user,
    _run,
    client,
    db_session_factory,
    ws_client,
)

AUTH = lambda token: {"Authorization": f"Bearer {token}"}  # noqa: E731


async def _make_superuser_session(factory, *, scopes, revoked=False, expired=False):
    """A superuser whose *session* carries `scopes` (possibly empty / None)."""
    async with factory() as db:
        user = User(
            email=f"su-{uuid.uuid4().hex[:8]}@test.local",
            hashed_password=hash_password("correct-horse-battery"),
            is_active=True,
            is_superuser=True,
        )
        db.add(user)
        await db.flush()
        token = generate_token()
        delta = timedelta(hours=-1) if expired else timedelta(hours=1)
        db.add(
            UserSession(
                user_id=user.id,
                token=hash_token(token),
                expires_at=datetime.now(timezone.utc) + delta,
                scopes=scopes,
                revoked_at=datetime.now(timezone.utc) if revoked else None,
            )
        )
        await db.commit()
    return token


# ======================================================================
# 1. Revoked sessions
# ======================================================================
@pytest.mark.asyncio
async def test_logout_revokes_token_for_rest(client, db_session_factory):
    token, _ = await _make_user(db_session_factory, scopes={Scope.READ.value})
    assert (await client.get("/api/v1/auth/me", headers=AUTH(token))).status_code == 200

    assert (await client.post("/api/v1/auth/logout", headers=AUTH(token))).status_code == 200

    for path in ("/api/v1/auth/me", "/api/v1/overview"):
        assert (await client.get(path, headers=AUTH(token))).status_code == 401, path


@pytest.mark.asyncio
async def test_logout_twice_is_rejected_not_silently_accepted(client, db_session_factory):
    token, _ = await _make_user(db_session_factory, scopes={Scope.READ.value})
    await client.post("/api/v1/auth/logout", headers=AUTH(token))
    assert (await client.post("/api/v1/auth/logout", headers=AUTH(token))).status_code == 401


# ======================================================================
# 2. Expired / invalid sessions and empty scopes
# ======================================================================
@pytest.mark.asyncio
async def test_empty_session_scopes_grant_nothing_even_for_superuser(client, db_session_factory):
    token = await _make_superuser_session(db_session_factory, scopes=[])
    me = await client.get("/api/v1/auth/me", headers=AUTH(token))
    assert me.status_code == 200
    assert me.json()["scopes"] == []

    for path in ("/api/v1/overview", "/api/v1/settings", "/api/v1/audit", "/api/v1/wallets"):
        assert (await client.get(path, headers=AUTH(token))).status_code == 403, path


@pytest.mark.asyncio
async def test_null_session_scopes_grant_nothing(client, db_session_factory):
    token = await _make_superuser_session(db_session_factory, scopes=None)
    me = await client.get("/api/v1/auth/me", headers=AUTH(token))
    assert me.json()["scopes"] == []
    assert (await client.get("/api/v1/settings", headers=AUTH(token))).status_code == 403


@pytest.mark.asyncio
async def test_unknown_scope_strings_grant_nothing(client, db_session_factory):
    token = await _make_superuser_session(db_session_factory, scopes=["root", "*", "admin"])
    assert (await client.get("/api/v1/auth/me", headers=AUTH(token))).json()["scopes"] == []


@pytest.mark.asyncio
async def test_expired_superuser_session_rejected(client, db_session_factory):
    token = await _make_superuser_session(
        db_session_factory, scopes=[Scope.READ.value], expired=True
    )
    assert (await client.get("/api/v1/overview", headers=AUTH(token))).status_code == 401


@pytest.mark.asyncio
async def test_revoked_session_with_full_scopes_rejected(client, db_session_factory):
    token = await _make_superuser_session(
        db_session_factory, scopes=[s.value for s in Scope], revoked=True
    )
    assert (await client.get("/api/v1/settings", headers=AUTH(token))).status_code == 401


# ======================================================================
# 4. Token transport: no query-string tokens
# ======================================================================
@pytest.mark.asyncio
async def test_rest_ignores_query_string_token(client, db_session_factory):
    token, _ = await _make_user(db_session_factory, scopes={Scope.READ.value})
    response = await client.get(f"/api/v1/auth/me?token={token}")
    assert response.status_code == 401


def test_websocket_rejects_query_string_token(ws_client, db_session_factory):
    token, _ = _run(_make_user(db_session_factory, scopes={Scope.READ.value}))
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            ws.receive_text()


# ======================================================================
# 3. REST / WebSocket parity
# ======================================================================
def _ws(ws_client, token):
    return ws_client.websocket_connect("/api/v1/ws", subprotocols=["bearer", token])


def test_websocket_accepts_subprotocol_token(ws_client, db_session_factory):
    token, _ = _run(_make_user(db_session_factory, scopes={Scope.READ.value}))
    with _ws(ws_client, token) as ws:
        event = ws.receive_json()
        assert event["type"] == "connected"
        assert event["payload"]["authenticated"] is True
        assert ws.accepted_subprotocol == "bearer"  # the token itself is never echoed


def test_websocket_accepts_authorization_header(ws_client, db_session_factory):
    token, _ = _run(_make_user(db_session_factory, scopes={Scope.READ.value}))
    with ws_client.websocket_connect("/api/v1/ws", headers=AUTH(token)) as ws:
        assert ws.receive_json()["type"] == "connected"


def test_websocket_rejects_revoked_session(ws_client, db_session_factory):
    token = _run(
        _make_superuser_session(db_session_factory, scopes=[Scope.READ.value], revoked=True)
    )
    with pytest.raises(WebSocketDisconnect):
        with _ws(ws_client, token) as ws:
            ws.receive_text()


def test_websocket_empty_scopes_grant_nothing(ws_client, db_session_factory):
    """A superuser session with empty scopes must not get the stream (REST gives 403)."""
    token = _run(_make_superuser_session(db_session_factory, scopes=[]))
    with pytest.raises(WebSocketDisconnect):
        with _ws(ws_client, token) as ws:
            ws.receive_text()


def test_websocket_closes_when_session_revoked_without_ping(
    ws_client, db_session_factory, monkeypatch
):
    """A revoked session must lose the stream even if the client never pings."""
    from backend.app.api.v1.endpoints import ws as ws_module

    monkeypatch.setattr(ws_module, "WS_REVALIDATE_SECONDS", 0.2)
    token, _ = _run(_make_user(db_session_factory, scopes={Scope.READ.value}))

    async def _revoke():
        async with db_session_factory() as db:
            row = await db.scalar(
                select(UserSession).where(UserSession.token == hash_token(token))
            )
            row.revoked_at = datetime.now(timezone.utc)
            await db.commit()

    with _ws(ws_client, token) as ws:
        assert ws.receive_json()["type"] == "connected"
        _run(_revoke())
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()  # server closes; no ping was ever sent


def test_websocket_refused_in_production_when_auth_disabled(ws_client, monkeypatch):
    """REST answers 503 in this state; the socket must not stream either."""
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect("/api/v1/ws", subprotocols=["bearer", "anything"]) as ws:
            ws.receive_text()


# ======================================================================
# 5. Password validation and hashing
# ======================================================================
def test_short_passwords_are_rejected():
    for pw in ("", "abcd", "x" * (MIN_PASSWORD_LENGTH - 1)):
        with pytest.raises(ValueError):
            hash_password(pw)


def test_overlong_password_is_rejected():
    with pytest.raises(ValueError):
        hash_password("x" * 257)  # login accepts at most 256 characters


def test_hash_uses_current_work_factor_and_round_trips():
    assert PBKDF2_ROUNDS >= 600_000
    stored = hash_password("correct-horse-battery")
    assert stored.split("$")[1] == str(PBKDF2_ROUNDS)
    assert verify_password("correct-horse-battery", stored)
    assert not verify_password("correct-horse-batterz", stored)


def test_verify_rejects_weak_or_malformed_stored_hashes():
    salt = "ab" * 16
    for stored in (
        f"pbkdf2$1${salt}${'00' * 32}",                       # trivially cheap
        f"pbkdf2$0${salt}${'00' * 32}",
        f"pbkdf2$-5${salt}${'00' * 32}",
        f"pbkdf2$999999999999${salt}${'00' * 32}",             # CPU-exhaustion attempt
        f"pbkdf2$600000$ab${'00' * 32}",                       # salt too short
        "pbkdf2$600000$zz$zz",
        "plaintext",
        "",
    ):
        assert verify_password("anything-long-enough", stored) is False, stored


def test_legacy_390k_hashes_still_verify():
    import hashlib

    salt = bytes.fromhex("cd" * 16)
    digest = hashlib.pbkdf2_hmac("sha256", b"correct-horse-battery", salt, 390_000)
    legacy = f"pbkdf2$390000${salt.hex()}${digest.hex()}"
    assert 390_000 >= PBKDF2_MIN_ROUNDS
    assert verify_password("correct-horse-battery", legacy)


def test_password_hash_format_validator():
    assert is_valid_password_hash(hash_password("correct-horse-battery"))
    assert not is_valid_password_hash("correct-horse-battery")
    assert not is_valid_password_hash("pbkdf2$1$aa$bb")
    assert not is_valid_password_hash(None)


@pytest.mark.asyncio
async def test_bootstrap_refuses_malformed_operator_hash(client, db_session_factory, monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "OPERATOR_PASSWORD_HASH", "my-plaintext-password")
    response = await client.post("/api/v1/auth/bootstrap")
    assert response.status_code == 503
    async with db_session_factory() as db:
        assert await db.scalar(select(User).limit(1)) is None


# ======================================================================
# 6. Authorization policy must fail closed
# ======================================================================
from backend.app.tools.base import BaseTool, SideEffect, ToolResult  # noqa: E402
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry  # noqa: E402


class _NetTool(BaseTool):
    name = "net_probe"
    description = "probe"
    version = "1"
    side_effect = SideEffect.NONE
    required_permissions = ["test.read"]
    input_schema = {
        "type": "object",
        "properties": {"network": {"type": "string"}},
        "required": ["network"],
    }
    calls = 0

    async def execute(self, params):
        type(self).calls += 1
        return ToolResult.success(data={"network": params["network"]})


class _WalletTool(_NetTool):
    name = "wallet_probe"
    input_schema = {
        "type": "object",
        "properties": {"wallet_id": {"type": "string"}},
        "required": ["wallet_id"],
    }

    async def execute(self, params):
        type(self).calls += 1
        return ToolResult.success(data={})


def _registry(policy):
    reg = ToolRegistry()
    reg.register(_NetTool())
    reg.register(_WalletTool())
    reg.policy = policy
    return reg


def test_explicit_empty_network_allowlist_does_not_fall_back_to_all():
    assert AuthorizationPolicy(allowed_networks=[]).allowed_networks == set()
    assert AuthorizationPolicy(allowed_networks=None).allowed_networks  # default stays usable


def test_permits_network_denies_blank_and_non_string():
    policy = AuthorizationPolicy(allowed_networks=["sepolia"])
    assert policy.permits_network("sepolia")[0]
    assert policy.permits_network("SEPOLIA")[0]
    for bad in ("", "  ", None, 5, "ethereum"):
        assert policy.permits_network(bad)[0] is False, bad


@pytest.mark.asyncio
async def test_registry_enforces_network_allowlist():
    _NetTool.calls = 0
    reg = _registry(AuthorizationPolicy(enabled_permissions=["test.read"], allowed_networks=["sepolia"]))
    ok = await reg.invoke("net_probe", {"network": "sepolia"})
    assert ok.ok
    denied = await reg.invoke("net_probe", {"network": "ethereum"})
    assert denied.status.value == "not_authorized"
    assert _NetTool.calls == 1  # the denied call never executed


@pytest.mark.asyncio
async def test_registry_with_empty_allowlist_denies_every_network():
    _NetTool.calls = 0
    reg = _registry(AuthorizationPolicy(enabled_permissions=["test.read"], allowed_networks=[]))
    result = await reg.invoke("net_probe", {"network": "sepolia"})
    assert result.status.value == "not_authorized"
    assert _NetTool.calls == 0


@pytest.mark.asyncio
async def test_registry_enforces_wallet_id_allowlist_and_denies_by_default():
    _WalletTool.calls = 0
    allowed = str(uuid.uuid4())
    reg = _registry(AuthorizationPolicy(enabled_permissions=["test.read"], allowed_wallet_ids=[allowed]))
    assert (await reg.invoke("wallet_probe", {"wallet_id": allowed})).ok
    other = await reg.invoke("wallet_probe", {"wallet_id": str(uuid.uuid4())})
    assert other.status.value == "not_authorized"

    reg_default = _registry(AuthorizationPolicy(enabled_permissions=["test.read"]))
    assert (await reg_default.invoke("wallet_probe", {"wallet_id": allowed})).status.value == "not_authorized"
    assert _WalletTool.calls == 1


def test_policy_permits_wallet():
    policy = AuthorizationPolicy(allowed_wallet_ids=["w-1"])
    assert policy.permits_wallet("w-1")[0]
    assert not policy.permits_wallet("w-2")[0]
    assert not policy.permits_wallet("")[0]
    assert not AuthorizationPolicy().permits_wallet("w-1")[0]


def test_planner_blocks_transact_step_without_network():
    from backend.app.planning.planner import (
        ExecutionPlanSpec, PlanConstraints, PlanStep, Planner, StepKind,
    )

    def step(kind, params):
        return PlanStep(index=0, description="d", kind=kind, parameters=params)

    constraints = PlanConstraints(allow_transactions=True, allowed_networks={"sepolia"})
    steps = [
        step(StepKind.TRANSACT, {}),                             # no network at all
        step(StepKind.TRANSACT, {"network": ""}),
        step(StepKind.TRANSACT, {"network": "ethereum"}),        # not allowed
        step(StepKind.TRANSACT, {"network": "sepolia"}),         # allowed
        step(StepKind.RESEARCH, {}),                             # network-free step is fine
    ]
    spec = ExecutionPlanSpec(objective="o", steps=steps, rationale="r")
    Planner.enforce_constraints(Planner.__new__(Planner), spec, constraints)
    assert [s.status == "skipped" for s in spec.steps] == [True, True, True, False, False]


def test_vault_signing_fails_closed_without_authorized_addresses():
    from backend.app.wallets.vault import KeyVault, SigningDenied, SigningRequest

    vault = KeyVault("0" * 64)
    addr = "0x" + "11" * 20
    request = SigningRequest(wallet_address=addr, chain_id=1, message="hi")

    for authorized in (None, set(), frozenset()):
        with pytest.raises(SigningDenied):
            vault.validate_request(request, authorized)
    with pytest.raises(SigningDenied):
        vault.validate_request(request, {"0x" + "22" * 20})
    vault.validate_request(request, {addr.upper().replace("0X", "0x")})  # case-insensitive match
