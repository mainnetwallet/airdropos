"""Tests for the Wallet Management API and service.

Two properties matter more than the CRUD itself:

1. **No secret ever leaves.** The private key is accepted once, encrypted with
   the AES-256-GCM vault, and from then on only capability flags (`has_key_material`,
   `can_sign`) are exposed. It must not appear in a response body, an error
   message, or an audit record.
2. **Write is a separate scope from read.** `wallet_read` lets an observer see
   addresses; `wallet_write` is required to import, rename, lock, deactivate, or
   delete. Importing key material with only `wallet_read` would be a privilege
   escalation, so that combination is asserted explicitly.

The tests run against a real in-memory database and the real
`get_principal` / `require_scopes` path, so a missing `Depends(require_wallet_write)`
shows up as a 200 rather than being masked by a stub.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from backend.app.database.session import get_db
from backend.app.main import app
from backend.app.models import AuditLog, Base, User, UserSession, Wallet
from backend.app.security.auth import Scope, generate_token, hash_password, hash_token

# A valid, deterministic secp256k1 key used only as test input. It is generated
# for this test file and funds nothing on any network.
TEST_PRIVATE_KEY = "0x" + "11" * 32
# Well-known test mnemonic (all words valid BIP-39, checksum not enforced by the
# vault's address derivation path in these tests).
TEST_MNEMONIC = (
    "test test test test test test test test test test test junk"
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003
    """Render PostgreSQL JSONB columns as SQLite JSON for the test database."""
    return "JSON"


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


async def _make_user(factory, *, scopes: set[str]) -> str:
    email = f"wallet-{uuid.uuid4().hex[:8]}@test.local"
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
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                scopes=sorted(scopes),
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


async def _auth(factory, *scopes: str) -> dict:
    token = await _make_user(factory, scopes=set(scopes))
    return {"Authorization": f"Bearer {token}"}


READ_WRITE = (Scope.WALLET_READ.value, Scope.WALLET_WRITE.value)


async def _import(client, factory, **overrides):
    headers = await _auth(factory, *READ_WRITE)
    payload = {"label": "Primary", "secret": TEST_PRIVATE_KEY}
    payload.update(overrides)
    return await client.post("/api/v1/wallets/import", json=payload, headers=headers)


# ======================================================================
# Authorization
# ======================================================================
@pytest.mark.asyncio
async def test_wallet_endpoints_require_authentication(client, db_session_factory):
    """No token means 401 on every wallet route, read and write alike."""
    read_calls = [
        ("GET", "/api/v1/wallets", None),
        ("GET", "/api/v1/wallets/00000000-0000-0000-0000-000000000000", None),
    ]
    write_calls = [
        ("POST", "/api/v1/wallets/import", {"label": "x", "secret": TEST_PRIVATE_KEY}),
        ("POST", "/api/v1/wallets/watch", {"label": "x", "address": "0x" + "aa" * 20}),
        ("PATCH", "/api/v1/wallets/00000000-0000-0000-0000-000000000000", {"label": "y"}),
        ("POST", "/api/v1/wallets/00000000-0000-0000-0000-000000000000/lock", None),
        ("POST", "/api/v1/wallets/00000000-0000-0000-0000-000000000000/deactivate", None),
        ("DELETE", "/api/v1/wallets/00000000-0000-0000-0000-000000000000", None),
    ]
    for method, path, body in read_calls + write_calls:
        response = await client.request(method, path, json=body)
        assert response.status_code == 401, f"{method} {path} returned {response.status_code}"


@pytest.mark.asyncio
async def test_wallet_read_scope_cannot_list(client, db_session_factory):
    """Generic `read` is not wallet access; `wallet_read` is required."""
    headers = await _auth(db_session_factory, Scope.READ.value)
    assert (await client.get("/api/v1/wallets", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_wallet_read_scope_alone_cannot_import_key_material(client, db_session_factory):
    """The critical privilege boundary: viewing addresses must not allow importing keys."""
    headers = await _auth(db_session_factory, Scope.WALLET_READ.value)
    response = await client.post(
        "/api/v1/wallets/import",
        json={"label": "Escalation", "secret": TEST_PRIVATE_KEY},
        headers=headers,
    )
    assert response.status_code == 403

    async with db_session_factory() as db:
        rows = list((await db.execute(select(Wallet))).scalars().all())
    assert rows == [], "a refused import must not have created a wallet"


@pytest.mark.asyncio
async def test_wallet_read_scope_alone_cannot_mutate(client, db_session_factory):
    """Lock, deactivate, rename, and delete all require `wallet_write`."""
    created = await _import(client, db_session_factory, label="Boundaries")
    assert created.status_code == 201, created.text
    wallet_id = created.json()["id"]

    headers = await _auth(db_session_factory, Scope.WALLET_READ.value)
    cases = [
        ("PATCH", f"/api/v1/wallets/{wallet_id}", {"label": "renamed"}),
        ("POST", f"/api/v1/wallets/{wallet_id}/lock", None),
        ("POST", f"/api/v1/wallets/{wallet_id}/deactivate", None),
        ("DELETE", f"/api/v1/wallets/{wallet_id}", None),
    ]
    for method, path, body in cases:
        response = await client.request(method, path, json=body, headers=headers)
        assert response.status_code == 403, f"{method} {path} returned {response.status_code}"

    # The wallet is untouched by the refused mutations.
    rw = await _auth(db_session_factory, *READ_WRITE)
    after = await client.get(f"/api/v1/wallets/{wallet_id}", headers=rw)
    assert after.json()["label"] == "Boundaries"
    assert after.json()["is_locked"] is False
    assert after.json()["is_active"] is True


# ======================================================================
# Secret handling
# ======================================================================
@pytest.mark.asyncio
async def test_import_never_returns_key_material(client, db_session_factory):
    response = await _import(client, db_session_factory)
    assert response.status_code == 201, response.text

    assert TEST_PRIVATE_KEY not in response.text
    body = response.json()
    assert "secret" not in body
    assert "private_key" not in body
    assert "encrypted_secret" not in body
    assert body["has_key_material"] is True
    assert body["can_sign"] is True
    assert body["address"].startswith("0x")


@pytest.mark.asyncio
async def test_no_wallet_endpoint_ever_returns_key_material(client, db_session_factory):
    """Walk every read path and assert the ciphertext never appears."""
    created = await _import(client, db_session_factory, label="Leak check")
    assert created.status_code == 201, created.text
    wallet_id = created.json()["id"]
    headers = await _auth(db_session_factory, *READ_WRITE)

    responses = [
        await client.get("/api/v1/wallets", headers=headers),
        await client.get(f"/api/v1/wallets/{wallet_id}", headers=headers),
    ]
    for response in responses:
        assert response.status_code == 200
        assert TEST_PRIVATE_KEY not in response.text

    async with db_session_factory() as db:
        row = await db.get(Wallet, uuid.UUID(wallet_id))
        ciphertext = row.encrypted_secret

    assert ciphertext and ciphertext != TEST_PRIVATE_KEY
    for response in responses:
        assert ciphertext not in response.text, "encrypted secret must not be serialized"


@pytest.mark.asyncio
async def test_audit_record_contains_no_secret(client, db_session_factory):
    """The audit trail records the address, never the key."""
    created = await _import(client, db_session_factory, label="Audited")
    assert created.status_code == 201
    wallet_id = created.json()["id"]

    async with db_session_factory() as db:
        entries = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == wallet_id,
                        AuditLog.action == "WALLET_IMPORTED",
                    )
                )
            ).scalars().all()
        )
    assert entries, "importing a wallet must write an audit record"
    record = entries[0]
    assert record.after_state["address"] == created.json()["address"]
    serialized = str(record.after_state) + str(record.before_state)
    assert TEST_PRIVATE_KEY not in serialized
    assert TEST_MNEMONIC not in serialized


@pytest.mark.asyncio
async def test_malformed_secret_does_not_echo_the_input(client, db_session_factory):
    """A validation failure must not repeat the submitted material."""
    headers = await _auth(db_session_factory, *READ_WRITE)
    response = await client.post(
        "/api/v1/wallets/import",
        json={"label": "Bad", "secret": "0xdeadbeef-not-a-real-key-secret-value"},
        headers=headers,
    )
    assert response.status_code == 400, response.text
    assert "not-a-real-key-secret-value" not in response.text


# ======================================================================
# CRUD behaviour
# ======================================================================
@pytest.mark.asyncio
async def test_watch_address_stores_no_key_material(client, db_session_factory):
    headers = await _auth(db_session_factory, *READ_WRITE)
    address = "0x" + "AB" * 20
    response = await client.post(
        "/api/v1/wallets/watch",
        json={"label": "Watched", "address": address},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["address"] == address.lower()
    assert body["has_key_material"] is False
    assert body["can_sign"] is False

    async with db_session_factory() as db:
        row = await db.get(Wallet, uuid.UUID(body["id"]))
        assert row.encrypted_secret is None


@pytest.mark.asyncio
async def test_watch_address_rejects_malformed_address(client, db_session_factory):
    headers = await _auth(db_session_factory, *READ_WRITE)
    for bad in ("0x123", "not-an-address", "0x" + "zz" * 20):
        response = await client.post(
            "/api/v1/wallets/watch",
            json={"label": "Bad", "address": bad},
            headers=headers,
        )
        assert response.status_code in (400, 422), f"{bad} returned {response.status_code}"


@pytest.mark.asyncio
async def test_duplicate_address_is_rejected(client, db_session_factory):
    first = await _import(client, db_session_factory, label="First")
    assert first.status_code == 201, first.text

    headers = await _auth(db_session_factory, *READ_WRITE)
    second = await client.post(
        "/api/v1/wallets/import",
        json={"label": "Second", "secret": TEST_PRIVATE_KEY},
        headers=headers,
    )
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_unknown_network_is_rejected(client, db_session_factory):
    headers = await _auth(db_session_factory, *READ_WRITE)
    response = await client.post(
        "/api/v1/wallets/import",
        json={
            "label": "Wrong net",
            "secret": TEST_PRIVATE_KEY,
            "supported_networks": ["not-a-network"],
        },
        headers=headers,
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_lock_and_unlock_toggle_signing(client, db_session_factory):
    created = await _import(client, db_session_factory, label="Lockable")
    wallet_id = created.json()["id"]
    headers = await _auth(db_session_factory, *READ_WRITE)

    locked = await client.post(f"/api/v1/wallets/{wallet_id}/lock", headers=headers)
    assert locked.status_code == 200, locked.text
    assert locked.json()["is_locked"] is True
    assert locked.json()["can_sign"] is False
    # Locking must not destroy the key; unlocking must restore the capability.
    assert locked.json()["has_key_material"] is True

    unlocked = await client.post(
        f"/api/v1/wallets/{wallet_id}/lock?locked=false", headers=headers
    )
    assert unlocked.status_code == 200
    assert unlocked.json()["is_locked"] is False
    assert unlocked.json()["can_sign"] is True


@pytest.mark.asyncio
async def test_deactivate_blocks_signing(client, db_session_factory):
    created = await _import(client, db_session_factory, label="Deactivatable")
    wallet_id = created.json()["id"]
    headers = await _auth(db_session_factory, *READ_WRITE)

    response = await client.post(f"/api/v1/wallets/{wallet_id}/deactivate", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False
    assert response.json()["can_sign"] is False

    # Inactive wallets disappear from the default listing.
    listed = await client.get("/api/v1/wallets?is_active=true", headers=headers)
    assert all(w["id"] != wallet_id for w in listed.json()["wallets"])

    # Reactivate brings it back, preserving history.
    reactivated = await client.post(
        f"/api/v1/wallets/{wallet_id}/deactivate?active=true", headers=headers
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True


@pytest.mark.asyncio
async def test_update_changes_metadata_only(client, db_session_factory):
    created = await _import(client, db_session_factory, label="Original")
    wallet_id = created.json()["id"]
    headers = await _auth(db_session_factory, *READ_WRITE)

    response = await client.patch(
        f"/api/v1/wallets/{wallet_id}",
        json={"label": "Renamed", "notes": "cold storage"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["label"] == "Renamed"
    assert body["notes"] == "cold storage"
    # Address and key material are not editable through metadata updates.
    assert body["address"] == created.json()["address"]
    assert body["has_key_material"] is True


@pytest.mark.asyncio
async def test_delete_removes_the_wallet_and_its_key(client, db_session_factory):
    created = await _import(client, db_session_factory, label="Disposable")
    wallet_id = created.json()["id"]
    headers = await _auth(db_session_factory, *READ_WRITE)

    assert (await client.delete(f"/api/v1/wallets/{wallet_id}", headers=headers)).status_code == 204
    assert (await client.get(f"/api/v1/wallets/{wallet_id}", headers=headers)).status_code == 404

    async with db_session_factory() as db:
        assert await db.get(Wallet, uuid.UUID(wallet_id)) is None


@pytest.mark.asyncio
async def test_malformed_wallet_id_is_a_client_error(client, db_session_factory):
    headers = await _auth(db_session_factory, *READ_WRITE)
    assert (await client.get("/api/v1/wallets/not-a-uuid", headers=headers)).status_code == 400
    assert (
        await client.get("/api/v1/wallets/00000000-0000-0000-0000-000000000000", headers=headers)
    ).status_code == 404


@pytest.mark.asyncio
async def test_list_filters_and_pagination(client, db_session_factory):
    headers = await _auth(db_session_factory, *READ_WRITE)
    for index in range(3):
        assert (
            await client.post(
                "/api/v1/wallets/watch",
                json={"label": f"Watched {index}", "address": "0x" + f"{index:02x}" * 20},
                headers=headers,
            )
        ).status_code == 201

    page = await client.get("/api/v1/wallets?limit=2&offset=0", headers=headers)
    assert page.status_code == 200
    body = page.json()
    assert body["count"] == 2
    assert body["limit"] == 2
    assert body["offset"] == 0

    filtered = await client.get("/api/v1/wallets?chain_type=EVM", headers=headers)
    assert filtered.json()["count"] == 3

    none_other = await client.get("/api/v1/wallets?chain_type=SOLANA", headers=headers)
    assert none_other.json()["count"] == 0


@pytest.mark.asyncio
async def test_mnemonic_import_is_accepted(client, db_session_factory):
    """A BIP-39 phrase is valid key material and gets a derivation path."""
    headers = await _auth(db_session_factory, *READ_WRITE)
    response = await client.post(
        "/api/v1/wallets/import",
        json={"label": "From phrase", "secret": TEST_MNEMONIC},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert TEST_MNEMONIC not in response.text
    assert response.json()["derivation_path"] == "m/44'/60'/0'/0/0"


# ======================================================================
# Service-level guarantees that do not need the HTTP layer
# ======================================================================
@pytest.mark.asyncio
async def test_service_signing_is_blocked_by_state(db_session_factory):
    """`can_sign` must be false for every combination that is not safe."""
    from backend.app.wallets.service import WalletService

    async with db_session_factory() as db:
        service = WalletService(db)
        row = Wallet(
            label="Matrix",
            address="0x" + "cc" * 20,
            chain_type="EVM",
            encrypted_secret="cipher",
            encryption_iv="iv",
            encryption_tag="tag",
            is_active=True,
            is_locked=False,
        )
        db.add(row)
        await db.flush()

        assert service.can_sign(row) is True
        row.is_locked = True
        assert service.can_sign(row) is False
        row.is_locked = False
        row.is_active = False
        assert service.can_sign(row) is False
        row.is_active = True
        row.encrypted_secret = None
        assert service.can_sign(row) is False