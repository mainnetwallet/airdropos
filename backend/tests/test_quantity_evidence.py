"""Regression tests for the two findings against e3a655a.

**P1 - conflicting evidence closed a holding requirement.** `observed_state` and the
tool `result` were flattened into one string and every number in it was treated as proof
of every requirement. So

    requirement   hold 100 tokens
    observed      {"balance": "50 tokens"}
    result        {"amount": "100 tokens", "status": "pending"}

closed "hold 100 tokens" on a wallet holding 50, because "100 tokens" appeared in the
pending transfer. A holding is proved by a balance reading; a pending amount is not one.

**P2 - float precision on crypto amounts.** Quantities were parsed with `float`, which
cannot represent `0.1` or `1.250` exactly, so a balance sitting on a threshold could land
on the wrong side of it. They are `Decimal` now.

Nothing here contacts Telegram, a chain, a browser or an LLM.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.app.agent.context import (
    TaskContextService,
    _collect_quantity_evidence,
    _extract_quantities,
    _quantities_met,
)
from backend.app.models import Base, Task

AUTHORITATIVE = {"authoritative": True, "kind": "balance_verified"}


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    made = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield made
    await engine.dispose()


async def _outstanding_after(factory, requirement: str, **observation) -> list[str]:
    """Record one requirement and one confirmed observation; return what is still open."""
    async with factory() as db:
        task = Task(
            task_id="AIR-2026-424242",
            title="Quantity evidence",
            description="Quantity evidence",
            status="IN_PROGRESS",
            owner_email="operator@test.local",
        )
        db.add(task)
        await db.commit()
        task = await db.scalar(sa_select(Task).where(Task.task_id == "AIR-2026-424242"))

        service = TaskContextService(db)
        await service.record_requirement(task, requirement)
        await db.commit()

        await service.record_observation(
            task,
            action="Check wallet",
            outcome="confirmed",
            tool="check_wallet_balance",
            parameters={},
            evidence=AUTHORITATIVE,
            **observation,
        )
        await db.commit()

        return [r.text for r in await service.requirements(task, only_outstanding=True)]


# ======================================================================
# P1 - the right kind of evidence for the right kind of requirement
# ======================================================================
class TestHoldingNeedsABalanceReading:
    @pytest.mark.asyncio
    async def test_a_low_balance_with_a_larger_pending_transfer_is_not_satisfied(self, factory):
        """The reported case, verbatim: 50 held, 100 merely in flight."""
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            observed_state={"balance": "50 tokens"},
            result_digest={"amount": "100 tokens", "status": "pending"},
        )
        assert outstanding == ["hold 100 tokens"], (
            "a pending transfer's amount was read as the wallet's balance"
        )

    @pytest.mark.asyncio
    async def test_a_verified_balance_that_is_large_enough_is_satisfied(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            observed_state={"balance": "150 tokens"},
        )
        assert outstanding == [], "a verified balance of 150 meets a threshold of 100"

    @pytest.mark.asyncio
    async def test_a_verified_balance_still_satisfies_beside_a_pending_transfer(self, factory):
        """The pending transfer is irrelevant noise, not a reason to reject real proof."""
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            observed_state={"balance": "150 tokens"},
            result_digest={"amount": "100 tokens", "status": "pending"},
        )
        assert outstanding == []

    @pytest.mark.asyncio
    async def test_a_pending_result_alone_is_not_satisfied(self, factory):
        """No balance was observed at all, only a transfer that has not happened."""
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={"amount": "100 tokens", "status": "pending"},
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_a_settled_transfer_amount_is_still_not_a_balance(self, factory):
        """Even a completed transfer of 100 does not show the wallet holds 100."""
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={"amount": "100 tokens", "status": "confirmed"},
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_a_balance_below_the_threshold_is_not_satisfied(self, factory):
        outstanding = await _outstanding_after(
            factory, "hold 100 tokens", observed_state={"balance": "99.999 tokens"}
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_a_balance_exactly_on_the_threshold_is_satisfied(self, factory):
        outstanding = await _outstanding_after(
            factory, "hold 100 tokens", observed_state={"balance": "100.000 tokens"}
        )
        assert outstanding == []

    @pytest.mark.asyncio
    async def test_a_pending_balance_key_is_not_a_holding(self, factory):
        outstanding = await _outstanding_after(
            factory, "hold 100 tokens", observed_state={"pending_balance": "500 tokens"}
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_a_balance_nested_under_a_pending_key_is_not_a_holding(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={"pending": {"balance": "500 tokens"}},
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_a_nested_balance_reading_is_found(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={"wallet": {"balance": "150 tokens"}},
        )
        assert outstanding == []

    @pytest.mark.asyncio
    async def test_a_balance_in_the_wrong_unit_is_not_satisfied(self, factory):
        outstanding = await _outstanding_after(
            factory, "hold 100 tokens", observed_state={"balance": "500 usdt"}
        )
        assert outstanding == ["hold 100 tokens"]


class TestConflictingEvidenceKeepsItOutstanding:
    @pytest.mark.asyncio
    async def test_observed_and_returned_balances_that_disagree_do_not_satisfy(self, factory):
        """150 in one place and 50 in another: neither is trusted over the other."""
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            observed_state={"balance": "150 tokens"},
            result_digest={"balance": "50 tokens"},
        )
        assert outstanding == ["hold 100 tokens"], (
            "two balance readings disagree, so the requirement must wait for one that settles it"
        )

    @pytest.mark.asyncio
    async def test_two_conflicting_readings_inside_one_source_do_not_satisfy(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            observed_state={"balance": "150 tokens", "available_balance": "40 tokens"},
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_agreeing_readings_in_two_places_do_satisfy(self, factory):
        """Agreement is not a conflict. The same balance reported twice is one balance."""
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            observed_state={"balance": "150 tokens"},
            result_digest={"balance": "150.0 tokens"},
        )
        assert outstanding == []


class TestOtherQuantitiesStillWork:
    @pytest.mark.asyncio
    async def test_a_settled_amount_satisfies_a_transfer_requirement(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "send 100 usdt",
            result_digest={"amount": "100 usdt", "status": "confirmed"},
        )
        assert outstanding == []

    @pytest.mark.asyncio
    async def test_a_pending_amount_does_not_satisfy_a_transfer_requirement(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "send 100 usdt",
            result_digest={"amount": "100 usdt", "status": "pending"},
        )
        assert outstanding == ["send 100 usdt"]

    @pytest.mark.asyncio
    async def test_a_balance_reading_is_not_proof_of_a_transfer(self, factory):
        """Having 150 usdt does not show 100 usdt was sent."""
        outstanding = await _outstanding_after(
            factory, "send 100 usdt", observed_state={"balance": "150 usdt"}
        )
        assert outstanding == ["send 100 usdt"]

    @pytest.mark.asyncio
    async def test_a_duration_requirement_is_met_by_a_larger_duration(self, factory):
        outstanding = await _outstanding_after(
            factory, "keep the account for 7 days", result_digest={"account_age": "9 days"}
        )
        assert outstanding == []


# ======================================================================
# P2 - Decimal, not float
# ======================================================================
class TestQuantitiesAreDecimals:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("0.500 tokens", "0.500"),
            ("0.0001 eth", "0.0001"),
            ("1.250 tokens", "1.250"),
            ("1000.500 tokens", "1000.500"),
            ("1,000.500 tokens", "1000.500"),
            ("100 tokens", "100"),
        ],
    )
    def test_the_number_is_a_decimal_with_every_digit_as_written(self, text, expected):
        (value, _unit), = _extract_quantities(text)
        assert isinstance(value, Decimal), f"{text!r} parsed as {type(value).__name__}"
        assert str(value) == expected, f"{text!r} lost precision: {value}"

    def test_a_malformed_number_is_skipped_not_guessed(self):
        assert _extract_quantities("balance 1.2.3 tokens") == []

    def test_a_number_beyond_float_precision_is_kept_exactly(self):
        (value, _unit), = _extract_quantities("balance 12345678901234567890.123456789 tokens")
        assert value == Decimal("12345678901234567890.123456789")
        assert float(value) != value, "this value is chosen so that a float cannot hold it"


class TestThresholdBoundaries:
    def test_a_balance_exactly_on_a_fractional_threshold_is_met(self):
        """`0.1` as a float is 0.1000000000000000055..., which a Decimal 0.1 is below."""
        assert _quantities_met(
            [(Decimal("0.1"), "token")], _extract_quantities("balance 0.1000 tokens")
        ) is True

    def test_a_float_threshold_is_compared_as_it_is_written(self):
        """Callers that still pass a float get the same answer as callers passing a Decimal."""
        assert _quantities_met([(0.1, "token")], _extract_quantities("0.1 tokens")) is True
        assert _quantities_met([(0.1, "token")], _extract_quantities("0.0999 tokens")) is False

    def test_one_unit_in_the_last_place_below_the_threshold_is_insufficient(self):
        assert _quantities_met(
            [(Decimal("0.0002"), "eth")], _extract_quantities("balance 0.0001 eth")
        ) is False
        assert _quantities_met(
            [(Decimal("0.0001"), "eth")], _extract_quantities("balance 0.0001 eth")
        ) is True

    def test_a_difference_below_float_resolution_is_still_seen(self):
        """A float collapses these two to the same number; a Decimal does not."""
        required = [(Decimal("12345678901234567890.123456789"), "token")]
        assert _quantities_met(
            required, _extract_quantities("12345678901234567890.123456789 tokens")
        ) is True
        assert _quantities_met(
            required, _extract_quantities("12345678901234567890.123456788 tokens")
        ) is False

    def test_trailing_zeros_do_not_change_the_value(self):
        assert _quantities_met(
            [(Decimal("1.25"), "token")], _extract_quantities("balance 1.250 tokens")
        ) is True
        assert _quantities_met(
            [(Decimal("1.251"), "token")], _extract_quantities("balance 1.250 tokens")
        ) is False

    def test_a_missing_unit_is_never_met(self):
        assert _quantities_met([(Decimal("1"), "eth")], _extract_quantities("500 usdt")) is False

    @pytest.mark.asyncio
    async def test_an_insufficient_fractional_balance_stays_outstanding(self, factory):
        outstanding = await _outstanding_after(
            factory, "hold 0.0002 eth", observed_state={"balance": "0.0001 eth"}
        )
        assert outstanding == ["hold 0.0002 eth"]

    @pytest.mark.asyncio
    async def test_a_fractional_balance_on_the_boundary_closes_the_requirement(self, factory):
        outstanding = await _outstanding_after(
            factory, "hold 0.1 eth", observed_state={"balance": "0.1000 eth"}
        )
        assert outstanding == []


# ======================================================================
# Follow-up to 77d4d1b - an unsettled source proves nothing, balance included
# ======================================================================
# 77d4d1b dropped pending/failed sources from the *transfer* evidence but let a balance
# key through regardless, and it read only the top-level `status`. So
#
#     {"status": "failed", "balance": "150 tokens"}
#
# still verified "hold 100 tokens", and `{"transaction": {"status": "pending", ...}}` was
# not looked at at all. The status of an object now covers what is *inside* it - and
# nothing beside it.
class TestAnUnsettledSourceDoesNotVerifyAHolding:
    @pytest.mark.asyncio
    async def test_failed_status_with_a_large_balance_is_not_satisfied(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={"status": "failed", "balance": "150 tokens"},
        )
        assert outstanding == ["hold 100 tokens"], (
            "a balance reported by a failed call was accepted as a verified holding"
        )

    @pytest.mark.asyncio
    async def test_pending_status_with_a_large_balance_is_not_satisfied(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={"status": "pending", "balance": "150 tokens"},
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status", ["pending", "failed", "rejected", "reverted", "processing", "Awaiting Confirmation"]
    )
    async def test_every_unsettled_status_blocks_an_observed_balance(self, factory, status):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            observed_state={"status": status, "balance": "150 tokens"},
        )
        assert outstanding == ["hold 100 tokens"], f"status {status!r} did not block the balance"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "marker", [{"success": False}, {"confirmed": False}, {"pending": True}, {"error": "rpc timeout"}]
    )
    async def test_a_flag_that_says_unfinished_blocks_the_balance_too(self, factory, marker):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={**marker, "balance": "150 tokens"},
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_a_status_given_as_an_object_blocks_the_balance(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={"status": {"state": "pending"}, "balance": "150 tokens"},
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_a_balance_inside_an_unsettled_object_is_not_satisfied(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={"transaction": {"status": "pending", "balance": "150 tokens"}},
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_a_nested_balance_under_a_failed_root_is_not_satisfied(self, factory):
        """The verdict flows down: the root failed, so what is under it goes with it."""
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={"status": "failed", "wallet": {"balance": "150 tokens"}},
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["confirmed", "success", "completed", "active", "finalized"])
    async def test_each_settled_status_still_verifies_a_balance(self, factory, status):
        """The fix must not turn every status field into a veto."""
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            observed_state={"status": status, "balance": "150 tokens"},
        )
        assert outstanding == []


class TestNestedTransactionsAreNotProofOfTransfer:
    @pytest.mark.asyncio
    async def test_a_nested_pending_transaction_amount_is_not_a_completed_transfer(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "send 100 tokens",
            result_digest={"transaction": {"status": "pending", "amount": "100 tokens"}},
        )
        assert outstanding == ["send 100 tokens"], (
            "the amount sits inside a pending transaction and proves nothing was sent"
        )

    @pytest.mark.asyncio
    async def test_a_nested_failed_transaction_in_a_list_is_not_a_completed_transfer(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "send 100 usdt",
            result_digest={"transfers": [{"status": "failed", "amount": "100 usdt"}]},
        )
        assert outstanding == ["send 100 usdt"]

    @pytest.mark.asyncio
    async def test_a_nested_settled_transaction_is_proof_of_transfer(self, factory):
        """Control: the same shape with a settled status does satisfy it."""
        outstanding = await _outstanding_after(
            factory,
            "send 100 tokens",
            result_digest={"transaction": {"status": "confirmed", "amount": "100 tokens"}},
        )
        assert outstanding == []

    @pytest.mark.asyncio
    async def test_one_pending_transaction_does_not_cancel_a_settled_one_beside_it(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "send 100 usdt",
            result_digest={
                "transfers": [
                    {"status": "pending", "amount": "40 usdt"},
                    {"status": "confirmed", "amount": "100 usdt"},
                ]
            },
        )
        assert outstanding == []


class TestAnIndependentBalanceKeepsItsScope:
    @pytest.mark.asyncio
    async def test_an_independent_authoritative_balance_satisfies_the_holding(self, factory):
        outstanding = await _outstanding_after(
            factory, "hold 100 tokens", observed_state={"balance": "150 tokens"}
        )
        assert outstanding == []

    @pytest.mark.asyncio
    async def test_a_pending_transaction_beside_the_balance_does_not_invalidate_it(self, factory):
        """The balance is the transaction's sibling, not its child."""
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={
                "transaction": {"status": "pending", "amount": "100 tokens"},
                "balance": "150 tokens",
            },
        )
        assert outstanding == [], (
            "a transfer in flight beside an independent wallet reading must not veto the reading"
        )

    @pytest.mark.asyncio
    async def test_a_pending_transaction_beside_a_nested_wallet_balance_keeps_it(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={
                "transaction": {"status": "pending", "amount": "100 tokens"},
                "wallet": {"status": "active", "balance": "150 tokens"},
            },
        )
        assert outstanding == []

    @pytest.mark.asyncio
    async def test_a_balance_that_is_too_small_still_fails_beside_a_pending_transaction(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={
                "transaction": {"status": "pending", "amount": "100 tokens"},
                "balance": "50 tokens",
            },
        )
        assert outstanding == ["hold 100 tokens"]

    def test_the_scope_is_visible_in_the_collected_evidence(self):
        evidence = _collect_quantity_evidence(
            {
                "transaction": {"status": "pending", "amount": "100 tokens", "balance": "999 tokens"},
                "balance": "150 tokens",
            }
        )
        assert evidence.balances == {"token": [Decimal("150")]}, (
            "only the independent reading survives; the one inside the pending object is dropped"
        )
        assert evidence.settled == [], "the pending amount is not settled evidence"


class TestConflictingBalanceReadingsStillBlock:
    @pytest.mark.asyncio
    async def test_two_settled_readings_that_disagree_keep_it_outstanding(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            observed_state={"balance": "150 tokens"},
            result_digest={"balance": "50 tokens"},
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_disagreeing_nested_wallet_readings_keep_it_outstanding(self, factory):
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            result_digest={
                "wallet_a": {"balance": "150 tokens"},
                "wallet_b": {"balance": "50 tokens"},
            },
        )
        assert outstanding == ["hold 100 tokens"]

    @pytest.mark.asyncio
    async def test_an_unsettled_reading_is_not_evidence_and_does_not_create_a_conflict(self, factory):
        """A pending 50 is not a reading, so it cannot contradict the settled 150."""
        outstanding = await _outstanding_after(
            factory,
            "hold 100 tokens",
            observed_state={"balance": "150 tokens"},
            result_digest={"status": "pending", "balance": "50 tokens"},
        )
        assert outstanding == []
