"""Tests for the claim lifecycle state machine and service.

The claim machine is the airdrop-side analogue of the task machine: it exists so
that "confirmed" always means "verified against a receipt", never "submitted and
hoping".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from backend.app.airdrops.claims import (
    ClaimLifecycleService,
    ClaimStatus,
    InvalidClaimTransition,
    TERMINAL_CLAIM_STATES,
    assert_transition,
    assert_verified,
    can_transition,
)
from backend.app.database.session import get_db
from backend.app.main import app
from backend.app.models import AirdropCampaign, Base, ClaimRecord, Project, Wallet


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


# ======================================================================
# Transition table
# ======================================================================
def test_terminal_states_have_no_exits():
    for status in TERMINAL_CLAIM_STATES:
        successors = {
            s for s in ClaimStatus if can_transition(status, s)
        }
        # DISTRIBUTED, FAILED and EXPIRED are terminal except FAILED -> EXPIRED,
        # which is allowed so a dead claim can still be recorded as expired.
        expected = {ClaimStatus.EXPIRED} if status is ClaimStatus.FAILED else set()
        assert successors == expected, f"{status} has unexpected exits: {successors}"


def test_confirmed_cannot_go_back_to_submitted():
    assert not can_transition(ClaimStatus.CONFIRMED, ClaimStatus.SUBMITTED)


def test_distributed_is_final():
    for target in ClaimStatus:
        assert not can_transition(ClaimStatus.DISTRIBUTED, target)


def test_expired_is_final():
    for target in ClaimStatus:
        assert not can_transition(ClaimStatus.EXPIRED, target)


def test_invalid_transition_raises_with_guidance():
    with pytest.raises(InvalidClaimTransition) as exc:
        assert_transition(ClaimStatus.DISTRIBUTED, ClaimStatus.PENDING)
    assert "terminal" in str(exc.value)


def test_valid_forward_transition_is_allowed():
    assert_transition(ClaimStatus.PENDING, ClaimStatus.SUBMITTED)
    assert_transition(ClaimStatus.SUBMITTED, ClaimStatus.CONFIRMED)


# ======================================================================
# Verification rule
# ======================================================================
def test_confirmed_requires_verified_flag():
    with pytest.raises(ValueError, match="is_verified"):
        assert_verified(ClaimStatus.CONFIRMED, is_verified=False, has_receipt=True)


def test_confirmed_requires_a_receipt():
    with pytest.raises(ValueError, match="receipt"):
        assert_verified(ClaimStatus.CONFIRMED, is_verified=True, has_receipt=False)


def test_confirmed_allowed_with_both():
    assert_verified(ClaimStatus.CONFIRMED, is_verified=True, has_receipt=True)


def test_non_confirmed_states_need_no_verification():
    assert_verified(ClaimStatus.SUBMITTED, is_verified=False, has_receipt=False)


# ======================================================================
# Service transitions against the database
# ======================================================================
async def _seed(factory, *, deadline=None, tx_hash=None):
    async with factory() as db:
        slug = f"proj-{uuid.uuid4().hex[:8]}"
        project = Project(name=f"Project {slug}", slug=slug, description="test")
        db.add(project)
        await db.flush()

        wallet = Wallet(label="w", address="0x" + "11" * 20)
        db.add(wallet)
        await db.flush()

        campaign = AirdropCampaign(
            project_id=project.id,
            title="Test campaign",
            lifecycle_status="CLAIM_OPEN",
            claim_deadline=deadline,
        )
        db.add(campaign)
        await db.flush()

        claim = ClaimRecord(
            campaign_id=campaign.id,
            wallet_id=wallet.id,
            network="ethereum",
            tx_hash=tx_hash,
        )
        db.add(claim)
        await db.commit()

        return claim.id, campaign.id, wallet.id


@pytest.mark.asyncio
async def test_submitted_stamps_claimed_at(db_session_factory):
    claim_id, _, _ = await _seed(db_session_factory)
    async with db_session_factory() as db:
        service = ClaimLifecycleService(db)
        claim = await db.get(ClaimRecord, claim_id)
        await service.transition(claim, ClaimStatus.SUBMITTED)
        await db.commit()

        assert claim.claim_status == "SUBMITTED"
        assert claim.claimed_at is not None


@pytest.mark.asyncio
async def test_confirmed_stamps_verified_at(db_session_factory):
    claim_id, _, _ = await _seed(db_session_factory)
    async with db_session_factory() as db:
        service = ClaimLifecycleService(db)
        claim = await db.get(ClaimRecord, claim_id)
        await service.transition(claim, ClaimStatus.SUBMITTED)
        claim.is_verified = True
        await service.transition(claim, ClaimStatus.CONFIRMED, receipt_status="SUCCESS")
        await db.commit()

        assert claim.claim_status == "CONFIRMED"
        assert claim.verified_at is not None
        assert claim.is_verified is True


@pytest.mark.asyncio
async def test_transition_blocks_illegal_move_in_database(db_session_factory):
    claim_id, _, _ = await _seed(db_session_factory)
    async with db_session_factory() as db:
        service = ClaimLifecycleService(db)
        claim = await db.get(ClaimRecord, claim_id)
        with pytest.raises(InvalidClaimTransition):
            await service.transition(claim, ClaimStatus.DISTRIBUTED)


# ======================================================================
# Chain observation
# ======================================================================
@pytest.mark.asyncio
async def test_expired_deadline_forces_expiry_even_with_receipt(db_session_factory, monkeypatch):
    """A passed deadline is terminal; a late receipt cannot resurrect the claim."""
    claim_id, _, _ = await _seed(
        db_session_factory,
        deadline=datetime.now(timezone.utc) - timedelta(hours=1),
        tx_hash="0xdeadbeef",
    )

    class FakeEngine:
        async def get_receipt(self, network, tx_hash):
            return {"status": "CONFIRMED", "block_number": 1, "logs": []}

    import backend.app.transactions.engine as engine_module

    monkeypatch.setattr(engine_module, "engine", FakeEngine())

    async with db_session_factory() as db:
        service = ClaimLifecycleService(db)
        claim = await db.get(ClaimRecord, claim_id)
        result = await service.refresh(claim)
        assert result == "EXPIRED"
        assert claim.claim_status == "EXPIRED"


@pytest.mark.asyncio
async def test_missing_receipt_stays_submitted_not_confirmed(db_session_factory, monkeypatch):
    """No receipt means still in flight. CONFIRMED here would be a lie."""
    claim_id, _, _ = await _seed(
        db_session_factory,
        deadline=datetime.now(timezone.utc) + timedelta(days=30),
        tx_hash="0xfeedface",
    )

    class FakeEngine:
        async def get_receipt(self, network, tx_hash):
            return None

    import backend.app.transactions.engine as engine_module

    monkeypatch.setattr(engine_module, "engine", FakeEngine())

    async with db_session_factory() as db:
        service = ClaimLifecycleService(db)
        claim = await db.get(ClaimRecord, claim_id)
        result = await service.refresh(claim)
        assert result == "SUBMITTED"
        assert claim.claim_status == "SUBMITTED"
        assert claim.is_verified is False


@pytest.mark.asyncio
async def test_reverted_receipt_marks_failed(db_session_factory, monkeypatch):
    claim_id, _, _ = await _seed(
        db_session_factory,
        deadline=datetime.now(timezone.utc) + timedelta(days=30),
        tx_hash="0xbad",
    )

    class FakeEngine:
        async def get_receipt(self, network, tx_hash):
            return {"status": "REVERTED", "block_number": 9, "logs": []}

    import backend.app.transactions.engine as engine_module

    monkeypatch.setattr(engine_module, "engine", FakeEngine())

    async with db_session_factory() as db:
        service = ClaimLifecycleService(db)
        claim = await db.get(ClaimRecord, claim_id)
        result = await service.refresh(claim)
        assert result == "FAILED"
        assert claim.claim_status == "FAILED"
        assert claim.distribution_status == "FAILED"


@pytest.mark.asyncio
async def test_rpc_failure_leaves_claim_untouched(db_session_factory, monkeypatch):
    """An unreachable RPC must not mark the claim failed."""
    claim_id, _, _ = await _seed(
        db_session_factory,
        deadline=datetime.now(timezone.utc) + timedelta(days=30),
        tx_hash="0xdead",
    )

    class FakeEngine:
        async def get_receipt(self, network, tx_hash):
            raise ConnectionError("rpc down")

    import backend.app.transactions.engine as engine_module

    monkeypatch.setattr(engine_module, "engine", FakeEngine())

    async with db_session_factory() as db:
        service = ClaimLifecycleService(db)
        claim = await db.get(ClaimRecord, claim_id)
        result = await service.refresh(claim)
        assert result == "PENDING"
        assert claim.claim_status == "PENDING"


@pytest.mark.asyncio
async def test_successful_receipt_confirms(db_session_factory, monkeypatch):
    claim_id, _, _ = await _seed(
        db_session_factory,
        deadline=datetime.now(timezone.utc) + timedelta(days=30),
        tx_hash="0xgood",
    )

    class FakeEngine:
        async def get_receipt(self, network, tx_hash):
            return {"status": "CONFIRMED", "block_number": 42, "gas_used": 21000, "logs": []}

    import backend.app.transactions.engine as engine_module

    monkeypatch.setattr(engine_module, "engine", FakeEngine())

    async with db_session_factory() as db:
        service = ClaimLifecycleService(db)
        claim = await db.get(ClaimRecord, claim_id)
        result = await service.refresh(claim)
        assert result == "CONFIRMED"
        assert claim.claim_status == "CONFIRMED"
        assert claim.receipt_status == "SUCCESS"
        assert claim.is_verified is True


@pytest.mark.asyncio
async def test_summary_counts_by_status(db_session_factory):
    claim_id, campaign_id, _ = await _seed(db_session_factory)
    async with db_session_factory() as db:
        service = ClaimLifecycleService(db)
        await service.transition(await db.get(ClaimRecord, claim_id), ClaimStatus.SUBMITTED)
        await db.commit()

        summary = await service.summary(campaign_id)
        assert summary["TOTAL"] == 1
        assert summary["SUBMITTED"] == 1
        assert summary["PENDING"] == 0