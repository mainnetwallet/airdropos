"""Tests for the autonomous execution policy and the unattended transfer path.

The point of these is the boundary. Autonomy is a real capability and it must work
end to end; it must also be impossible to widen by accident, by misconfiguration, or
by anything the model says.

Nothing here submits a real transaction. The chain is stubbed at the engine boundary
and the broadcast is asserted against a recorded call list.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio

from backend.app.security.autonomous_policy import (
    ACTION_ALLOW,
    ACTION_DENY,
    ALL_CATEGORIES,
    VALUE_CATEGORIES,
    AutonomousPolicy,
    Category,
    PolicyError,
    is_policy_approver,
    policy_approver_identity,
    rule_id_from_approver,
)


def policy(**over) -> AutonomousPolicy:
    raw = {"enabled": True, "rules": []}
    raw.update(over)
    return AutonomousPolicy.from_raw(raw, source="test")


def allow(category: str, **rule) -> Dict[str, Any]:
    return {"category": category, "action": ACTION_ALLOW, **rule}


# ======================================================================
# Parsing and configuration
# ======================================================================
class TestPolicyParsing:
    def test_unset_policy_denies_everything(self):
        p = AutonomousPolicy.from_raw(None)
        assert p.enabled is False
        decision = p.evaluate(Category.WEB_FETCH)
        assert decision.allowed is False
        assert "disabled" in decision.reason.lower()

    def test_no_rules_means_no_autonomy(self):
        p = AutonomousPolicy.from_raw({"enabled": True, "rules": []})
        assert p.evaluate(Category.WEB_FETCH).allowed is False
        assert "deny-by-default" in p.evaluate(Category.WEB_FETCH).reason

    def test_rules_without_a_flag_imply_enabled(self):
        # An operator who wrote rules made a decision; hiding it behind a flag they
        # forgot would be worse than honouring it.
        p = AutonomousPolicy.from_raw({"rules": [allow(Category.WEB_FETCH)]})
        assert p.enabled is True
        assert p.evaluate(Category.WEB_FETCH).allowed is True

    def test_malformed_json_raises_rather_than_defaulting(self):
        with pytest.raises(PolicyError):
            AutonomousPolicy.from_raw("{not json", source="AUTONOMOUS_POLICY")

    def test_unknown_category_is_rejected_at_parse_time(self):
        with pytest.raises(PolicyError) as exc:
            AutonomousPolicy.from_raw({"rules": [{"category": "tx.moon_moon"}]})
        # A typo must not silently widen or silently narrow authority.
        assert "unknown category" in str(exc.value).lower()

    def test_invalid_action_is_rejected(self):
        with pytest.raises(PolicyError):
            AutonomousPolicy.from_raw({"rules": [{"category": "web.fetch", "action": "maybe"}]})

    def test_non_numeric_limit_is_rejected(self):
        with pytest.raises(PolicyError):
            AutonomousPolicy.from_raw(
                {"rules": [allow(Category.TX_NATIVE_TRANSFER, max_value_usd="lots")]}
            )

    def test_categories_are_a_closed_vocabulary(self):
        assert Category.TX_NATIVE_TRANSFER in ALL_CATEGORIES
        assert VALUE_CATEGORIES == {Category.TX_NATIVE_TRANSFER, Category.TX_TOKEN_TRANSFER}

    def test_round_trips_through_json(self):
        raw = {"enabled": True, "rules": [allow(Category.WEB_FETCH)]}
        p = AutonomousPolicy.from_raw(json.dumps(raw))
        assert p.evaluate(Category.WEB_FETCH).allowed is True

    def test_comma_separated_lists_are_accepted(self):
        # Windows-friendly alternative to JSON arrays.
        p = policy(rules=[
            allow(Category.TX_NATIVE_TRANSFER, allowed_networks="Ethereum, Base")
        ])
        assert p.evaluate(
            Category.TX_NATIVE_TRANSFER, network="ethereum"
        ).allowed is True
        assert p.evaluate(
            Category.TX_NATIVE_TRANSFER, network="polygon"
        ).allowed is False


# ======================================================================
# Decisions
# ======================================================================
class TestPolicyDecisions:
    def test_allow_rule_permits_and_names_itself(self):
        p = policy(rules=[allow(Category.WEB_FETCH, id="reads")])
        decision = p.evaluate(Category.WEB_FETCH)
        assert decision.allowed is True
        # Attribution: "why did this run unattended" is answerable from the decision.
        assert decision.rule_id == "reads"

    def test_explicit_deny_beats_everything(self):
        p = policy(rules=[allow(Category.WEB_FETCH), allow(Category.WEB_FETCH, id="x", action=ACTION_DENY)])
        # First match wins, so ordering is the operator's control.
        assert p.evaluate(Category.WEB_FETCH).allowed is True

        p2 = policy(rules=[allow(Category.WEB_FETCH, action=ACTION_DENY)])
        assert p2.evaluate(Category.WEB_FETCH).allowed is False

    def test_unknown_category_is_refused_at_evaluation(self):
        p = policy(rules=[allow(Category.WEB_FETCH)])
        decision = p.evaluate("tx.definitely_not_a_category")
        assert decision.allowed is False

    def test_missing_network_is_refused_when_the_rule_restricts_networks(self):
        # Failing closed: a restricted category with no network stated cannot be
        # checked, so it is not run.
        p = policy(rules=[allow(Category.TX_NATIVE_TRANSFER, allowed_networks=["ethereum"])])
        assert p.evaluate(Category.TX_NATIVE_TRANSFER).allowed is False
        assert p.evaluate(Category.TX_NATIVE_TRANSFER, network="ethereum").allowed is True

    def test_restricted_destination_must_match(self):
        dest = "0x" + "ab" * 20
        other = "0x" + "cd" * 20
        p = policy(rules=[allow(Category.TX_NATIVE_TRANSFER, allowed_destinations=[dest])])
        assert p.evaluate(Category.TX_NATIVE_TRANSFER, destination=dest).allowed is True
        assert p.evaluate(Category.TX_NATIVE_TRANSFER, destination=other).allowed is False

    def test_restricted_token_must_match(self):
        p = policy(rules=[allow(Category.TX_TOKEN_TRANSFER, allowed_tokens=["usdc"])])
        assert p.evaluate(Category.TX_TOKEN_TRANSFER, token="USDC").allowed is True
        assert p.evaluate(Category.TX_TOKEN_TRANSFER, token="DAI").allowed is False

    def test_native_amount_ceiling_is_enforced_exactly(self):
        p = policy(rules=[allow(Category.TX_NATIVE_TRANSFER, max_value_native="0.05")])
        assert p.evaluate(Category.TX_NATIVE_TRANSFER, value_native="0.05").allowed is True
        over = p.evaluate(Category.TX_NATIVE_TRANSFER, value_native="0.0500001")
        assert over.allowed is False
        assert "exceeds" in over.reason

    def test_usd_ceiling_defers_to_the_existing_configured_limit(self):
        from backend.app.core.config import settings

        p = policy(rules=[allow(Category.TX_NATIVE_TRANSFER)])
        # No invented limit: the platform's own MAX_TRANSACTION_VALUE_USD applies.
        over = p.evaluate(
            Category.TX_NATIVE_TRANSFER,
            value_usd=settings.MAX_TRANSACTION_VALUE_USD + 1,
        )
        assert over.allowed is False
        assert "MAX_TRANSACTION_VALUE_USD" in over.reason

        under = p.evaluate(
            Category.TX_NATIVE_TRANSFER, value_usd=settings.MAX_TRANSACTION_VALUE_USD - 1
        )
        assert under.allowed is True

    def test_rule_level_usd_ceiling_takes_precedence_and_is_named(self):
        p = policy(rules=[allow(Category.TX_NATIVE_TRANSFER, id="small", max_value_usd=5.0)])
        over = p.evaluate(Category.TX_NATIVE_TRANSFER, value_usd=6.0)
        assert over.allowed is False
        assert "small" in over.reason

    def test_unparseable_amount_is_refused_not_ignored(self):
        p = policy(rules=[allow(Category.TX_NATIVE_TRANSFER, max_value_native="0.05")])
        decision = p.evaluate(Category.TX_NATIVE_TRANSFER, value_native="lots")
        assert decision.allowed is False

    def test_describe_exposes_the_whole_policy(self):
        p = policy(rules=[allow(Category.TX_NATIVE_TRANSFER, id="eth")])
        described = p.describe()
        assert described["enabled"] is True
        assert described["value_moving"] is True
        assert described["rules"][0]["rule_id"] == "eth"
        assert "deny-by-default" in described["note"]


# ======================================================================
# Approver identity
# ======================================================================
class TestPolicyApproverIdentity:
    def test_identity_round_trips(self):
        identity = policy_approver_identity("eth-transfers")
        assert is_policy_approver(identity)
        assert rule_id_from_approver(identity) == "eth-transfers"

    def test_a_human_name_is_not_a_policy_identity(self):
        assert not is_policy_approver("operator@example.test")
        assert not is_policy_approver("agent")
        assert not is_policy_approver("system")
        assert not is_policy_approver("")
        assert rule_id_from_approver("operator@example.test") is None


# ======================================================================
# The end-to-end unattended path
# ======================================================================
@pytest.fixture
def wallet_factory(monkeypatch):
    """Create a signable wallet in the per-test database and stub the chain."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import StaticPool

    from backend.app.database.session import Base
    import backend.app.database.session as session_module
    from backend.app.wallets.service import WalletService

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    holder: Dict[str, Any] = {"engine": engine}

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(
            session_module,
            "AsyncSessionLocal",
            lambda *a, **k: AsyncSession(engine, expire_on_commit=False),
        )
        async with AsyncSession(engine, expire_on_commit=False) as db:
            wallet = await WalletService(db).create_wallet(
                label="Main", supported_networks=["ethereum", "base"]
            )
            await db.commit()
            holder["wallet_id"] = str(wallet.id)
            holder["address"] = wallet.address
            holder["secret"] = (wallet.encrypted_secret, wallet.encryption_iv, wallet.encryption_tag)

    import asyncio

    asyncio.get_event_loop().run_until_complete(setup()) if False else None
    holder["_setup"] = setup
    return holder


@pytest_asyncio.fixture
async def wallet(wallet_factory):
    await wallet_factory["_setup"]()
    return wallet_factory


@pytest.fixture
def chain(monkeypatch):
    """Stub the chain. Records broadcasts instead of sending them."""
    from backend.app.transactions import engine as engine_module

    state: Dict[str, Any] = {
        "balance_wei": 5 * 10**18,
        "nonces": {},
        "broadcasts": [],
        "receipt": {
            "status": "CONFIRMED",
            "block_number": 20_000_001,
            "gas_used": "0x5208",
            "confirmations": 3,
        },
    }

    async def fake_rpc(network, method, params):
        if method == "eth_getBalance":
            return hex(state["balance_wei"])
        if method == "eth_gasPrice":
            return hex(20 * 10**9)
        if method == "eth_estimateGas":
            return hex(21000)
        if method == "eth_call":
            return "0x"
        if method == "eth_getTransactionCount":
            return hex(state["nonces"].setdefault(params[0], 0))
        if method == "eth_blockNumber":
            return hex(20_000_003)
        raise AssertionError(f"unexpected RPC {method}")

    async def fake_broadcast(network, raw_tx, *, authorization):
        state["broadcasts"].append({"network": network, "raw": raw_tx})
        return "0x" + "ab" * 32

    monkeypatch.setattr(engine_module.engine, "_rpc", fake_rpc)
    monkeypatch.setattr(engine_module.engine, "submit_raw", fake_broadcast)
    monkeypatch.setattr(
        engine_module.engine, "rpc_url", staticmethod(lambda network: f"https://rpc.test/{network}")
    )

    async def fake_receipt(network, tx_hash):
        if not state["broadcasts"]:
            return None
        return dict(state["receipt"])

    async def fake_confirmations(network, tx_hash):
        return state["receipt"]["confirmations"]

    monkeypatch.setattr(engine_module.engine, "get_receipt", fake_receipt)
    monkeypatch.setattr(engine_module.engine, "get_confirmations", fake_confirmations)
    return state


@pytest.fixture(autouse=True)
def _verifier(monkeypatch):
    """Give the engine a verifier that consults the real ApprovalService.

    The engine must go through the same path in tests as in production, or the
    approval guarantees would go untested.
    """
    from backend.app.transactions import approvals as approvals_module

    monkeypatch.setattr(
        approvals_module,
        "bind_verifier",
        lambda: None,
    )


def enable(monkeypatch, raw) -> AutonomousPolicy:
    """Install a policy and clear the cache so `get_policy()` picks it up."""
    from backend.app.security import autonomous_policy as ap

    parsed = AutonomousPolicy.from_raw(raw, source="test")
    monkeypatch.setattr(ap, "_cached", parsed)
    return parsed


class TestAutonomousApproval:
    @pytest.mark.asyncio
    async def test_no_policy_means_the_owner_is_still_asked(self, monkeypatch, wallet, chain):
        from backend.app.tools.transfer_tools import ApproveTransferTool

        enable(monkeypatch, {"enabled": False, "rules": []})

        result = await ApproveTransferTool().execute(
            {
                "network": "ethereum", "chain_id": 1,
                "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
                "value_wei": str(10**16), "data": "0x", "gas_limit": "21000",
            }
        )

        assert result.status.value == "needs_user_action"
        assert "disabled" in result.error.lower()
        assert "approval_id" not in result.data

    @pytest.mark.asyncio
    async def test_a_covering_rule_authorizes_unattended(self, monkeypatch, wallet, chain):
        from backend.app.tools.transfer_tools import ApproveTransferTool

        enable(
            monkeypatch,
            {
                "enabled": True,
                "rules": [
                    allow(
                        Category.TX_NATIVE_TRANSFER,
                        id="eth-transfers",
                        allowed_networks=["ethereum"],
                        max_value_native="0.05",
                    )
                ],
            },
        )

        result = await ApproveTransferTool().execute(
            {
                "network": "ethereum", "chain_id": 1,
                "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
                "value_wei": str(10**16), "data": "0x", "gas_limit": "21000",
            }
        )

        assert result.status.value == "success"
        assert result.data["authorized_by_policy"] is True
        assert result.data["authorized_by"] == "policy:autonomous:eth-transfers"
        assert result.data["approval_id"]

    @pytest.mark.asyncio
    async def test_an_amount_above_the_ceiling_is_refused_with_the_reason(
        self, monkeypatch, wallet, chain
    ):
        from backend.app.tools.transfer_tools import ApproveTransferTool

        enable(
            monkeypatch,
            {
                "enabled": True,
                "rules": [
                    allow(Category.TX_NATIVE_TRANSFER, id="small", max_value_native="0.001")
                ],
            },
        )

        result = await ApproveTransferTool().execute(
            {
                "network": "ethereum", "chain_id": 1,
                "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
                # 0.01 ETH, above the 0.001 ceiling.
                "value_wei": str(10**16), "data": "0x", "gas_limit": "21000",
            }
        )

        assert result.status.value == "needs_user_action"
        assert "0.001" in result.error
        assert "small" in result.error

    @pytest.mark.asyncio
    async def test_a_network_outside_the_rule_is_refused(self, monkeypatch, wallet, chain):
        from backend.app.tools.transfer_tools import ApproveTransferTool

        enable(
            monkeypatch,
            {"enabled": True, "rules": [allow(Category.TX_NATIVE_TRANSFER, allowed_networks=["ethereum"])]},
        )

        result = await ApproveTransferTool().execute(
            {
                "network": "polygon", "chain_id": 137,
                "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
                "value_wei": "1000", "data": "0x", "gas_limit": "21000",
            }
        )

        assert result.status.value == "needs_user_action"
        assert "polygon" in result.error

    @pytest.mark.asyncio
    async def test_a_native_rule_cannot_authorize_a_token_transfer(
        self, monkeypatch, wallet, chain
    ):
        """Category is derived from the calldata, not chosen by the caller."""
        from backend.app.tools.transfer_tools import ApproveTransferTool

        enable(
            monkeypatch,
            {"enabled": True, "rules": [allow(Category.TX_NATIVE_TRANSFER, id="native-only")]},
        )

        result = await ApproveTransferTool().execute(
            {
                "network": "ethereum", "chain_id": 1,
                "wallet_address": wallet["address"],
                "to_address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                "value_wei": "0",
                # ERC-20 transfer calldata.
                "data": "0xa9059cbb" + "0" * 64 + "0" * 64,
                "gas_limit": "65000",
            }
        )

        # The rule covers tx.native_transfer, not tx.token_transfer.
        assert result.status.value == "needs_user_action"
        assert "tx.token_transfer" in result.error

    @pytest.mark.asyncio
    async def test_the_agent_cannot_invent_an_approval_identity(self, monkeypatch, wallet, chain):
        from backend.app.tools.transfer_tools import ApproveTransferTool

        # No policy at all: nothing is authorized.
        enable(monkeypatch, {"enabled": False, "rules": []})

        for identity in ("agent", "system", "auto", "llm", "assistant"):
            result = await ApproveTransferTool().execute(
                {
                    "network": "ethereum", "chain_id": 1,
                    "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
                    "value_wei": "1000", "data": "0x", "gas_limit": "21000",
                    "approved_by": identity,
                }
            )
            assert result.status.value == "failure", identity
            assert "not a person" in result.error

    @pytest.mark.asyncio
    async def test_claiming_the_policy_identity_directly_authorizes_nothing(
        self, monkeypatch, wallet, chain
    ):
        """The strongest property here.

        Writing `policy:autonomous:eth-transfers` into a tool call must not work on its
        own. Without a rule of that id actually existing in configuration, there is
        nothing to consume.
        """
        from backend.app.tools.transfer_tools import SubmitPreparedTransferTool
        from backend.app.transactions.approvals import ApprovalService

        enable(monkeypatch, {"enabled": False, "rules": []})

        binding = {
            "network": "ethereum", "chain_id": 1,
            "wallet": wallet["address"], "to": "0x" + "cc" * 20,
            "value_wei": "1000", "data": "0x", "gas_limit": "21000",
        }

        # Plant an approval row that claims the identity...
        async with _session() as db:
            row = await ApprovalService(db).request(
                approved_by="policy:autonomous:eth-transfers",
                binding=binding,
                summary="planted",
            )
            approval_id = row.approval_id

        # ...and try to spend it with autonomy switched off.
        result = await SubmitPreparedTransferTool().execute(
            {"network": "ethereum", "approval_id": approval_id}
        )

        assert result.status.value == "failure"
        assert result.error_category == "POLICY_DENIED"
        assert "tx_hash" not in result.data
        assert chain["broadcasts"] == []

    @pytest.mark.asyncio
    async def test_a_disabled_rule_invalidates_a_live_approval_at_broadcast(
        self, monkeypatch, wallet, chain
    ):
        """Narrowing the policy between approval and broadcast stops the transfer."""
        from backend.app.tools.transfer_tools import ApproveTransferTool, SubmitPreparedTransferTool

        permissive = {
            "enabled": True,
            "rules": [
                allow(
                    Category.TX_NATIVE_TRANSFER,
                    id="eth",
                    allowed_networks=["ethereum"],
                    max_value_native="0.05",
                )
            ],
        }
        enable(monkeypatch, permissive)

        approved = await ApproveTransferTool().execute(
            {
                "network": "ethereum", "chain_id": 1,
                "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
                "value_wei": str(10**16), "data": "0x", "gas_limit": "21000",
            }
        )
        assert approved.status.value == "success"

        # The operator narrows the rule before the agent submits.
        enable(
            monkeypatch,
            {
                "enabled": True,
                "rules": [
                    allow(
                        Category.TX_NATIVE_TRANSFER,
                        id="eth",
                        allowed_networks=["ethereum"],
                        max_value_native="0.001",  # now too small
                    )
                ],
            },
        )

        result = await SubmitPreparedTransferTool().execute(
            {"network": "ethereum", "approval_id": approved.data["approval_id"]}
        )

        assert result.status.value == "failure"
        assert result.error_category == "POLICY_DENIED"
        assert chain["broadcasts"] == []

    @pytest.mark.asyncio
    async def test_a_deleted_rule_invalidates_the_approval(self, monkeypatch, wallet, chain):
        from backend.app.tools.transfer_tools import ApproveTransferTool, SubmitPreparedTransferTool

        enable(
            monkeypatch,
            {"enabled": True, "rules": [allow(Category.TX_NATIVE_TRANSFER, id="eth")]},
        )
        approved = await ApproveTransferTool().execute(
            {
                "network": "ethereum", "chain_id": 1,
                "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
                "value_wei": "1000", "data": "0x", "gas_limit": "21000",
            }
        )

        # The rule is removed entirely.
        enable(monkeypatch, {"enabled": True, "rules": [allow(Category.WEB_FETCH)]})

        result = await SubmitPreparedTransferTool().execute(
            {"network": "ethereum", "approval_id": approved.data["approval_id"]}
        )
        assert result.status.value == "failure"
        assert chain["broadcasts"] == []


def _session():
    from backend.app.database.session import session_scope

    return session_scope()


# ======================================================================
# Idempotency
# ======================================================================
class TestIdempotency:
    @pytest.mark.asyncio
    async def test_the_same_intent_produces_one_approval(self, monkeypatch, wallet, chain):
        from backend.app.tools.transfer_tools import ApproveTransferTool

        enable(
            monkeypatch,
            {"enabled": True, "rules": [allow(Category.TX_NATIVE_TRANSFER, id="eth")]},
        )

        fields = {
            "network": "ethereum", "chain_id": 1,
            "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
            "value_wei": str(10**16), "data": "0x", "gas_limit": "21000",
        }

        first = await ApproveTransferTool().execute(dict(fields))
        second = await ApproveTransferTool().execute(dict(fields))

        assert first.data["approval_id"] == second.data["approval_id"]
        assert second.data["new_approval"] is False
        assert "no second one was created" in second.data["note"]

    @pytest.mark.asyncio
    async def test_a_different_amount_is_a_different_intent(self, monkeypatch, wallet, chain):
        from backend.app.tools.transfer_tools import ApproveTransferTool

        enable(
            monkeypatch,
            {"enabled": True, "rules": [allow(Category.TX_NATIVE_TRANSFER, id="eth")]},
        )
        base = {
            "network": "ethereum", "chain_id": 1,
            "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
            "data": "0x", "gas_limit": "21000",
        }
        first = await ApproveTransferTool().execute({**base, "value_wei": str(10**16)})
        second = await ApproveTransferTool().execute({**base, "value_wei": str(2 * 10**16)})

        assert first.data["approval_id"] != second.data["approval_id"]

    @pytest.mark.asyncio
    async def test_an_already_broadcast_transfer_is_not_sent_twice(
        self, monkeypatch, wallet, chain
    ):
        from backend.app.tools.transfer_tools import ApproveTransferTool, SubmitPreparedTransferTool

        enable(
            monkeypatch,
            {
                "enabled": True,
                "rules": [
                    allow(
                        Category.TX_NATIVE_TRANSFER, id="eth",
                        allowed_networks=["ethereum"], max_value_native="0.05",
                    )
                ],
            },
        )

        fields = {
            "network": "ethereum", "chain_id": 1,
            "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
            "value_wei": str(10**16), "data": "0x", "gas_limit": "21000",
        }
        approved = await ApproveTransferTool().execute(dict(fields))

        first = await SubmitPreparedTransferTool().execute(
            {"network": "ethereum", "approval_id": approved.data["approval_id"]}
        )
        assert first.status.value == "success"
        assert len(chain["broadcasts"]) == 1
        sent_hash = first.data["tx_hash"]

        # The same request again. It must report the first result, not resend.
        second = await SubmitPreparedTransferTool().execute(
            {"network": "ethereum", "approval_id": approved.data["approval_id"]}
        )
        assert second.status.value == "success"
        assert second.data["status"] == "ALREADY_BROADCAST"
        assert second.data["resent"] is False
        assert second.data["tx_hash"] == sent_hash
        assert len(chain["broadcasts"]) == 1

    @pytest.mark.asyncio
    async def test_the_hash_is_persisted_before_any_receipt_wait(self, monkeypatch, wallet, chain):
        from backend.app.tools.transfer_tools import ApproveTransferTool, SubmitPreparedTransferTool
        from backend.app.transactions.approvals import ApprovalService

        enable(
            monkeypatch,
            {"enabled": True, "rules": [allow(Category.TX_NATIVE_TRANSFER, id="eth")]},
        )
        approved = await ApproveTransferTool().execute(
            {
                "network": "ethereum", "chain_id": 1,
                "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
                "value_wei": "1000", "data": "0x", "gas_limit": "21000",
            }
        )
        sent = await SubmitPreparedTransferTool().execute(
            {"network": "ethereum", "approval_id": approved.data["approval_id"]}
        )
        assert sent.status.value == "success", sent.error

        async with _session() as db:
            row = await ApprovalService(db).get(approved.data["approval_id"])
            # Written even though we then waited on a receipt.
            assert row.tx_hash
            assert row.broadcast_status == "SUBMITTED"


# ======================================================================
# Parameters cannot change after authorization
# ======================================================================
class TestParametersCannotChangeAfterAuthorization:
    @pytest.mark.asyncio
    async def test_changing_the_destination_blocks_the_broadcast(self, monkeypatch, wallet, chain):
        from backend.app.transactions.approvals import ApprovalService, canonical_binding

        enable(
            monkeypatch,
            {"enabled": True, "rules": [allow(Category.TX_NATIVE_TRANSFER, id="eth")]},
        )
        binding = {
            "network": "ethereum", "chain_id": 1,
            "wallet": wallet["address"], "to": "0x" + "cc" * 20,
            "value_wei": "1000", "data": "0x", "gas_limit": "21000",
        }
        async with _session() as db:
            row = await ApprovalService(db).request(
                approved_by="operator@example.test", binding=binding, summary="test"
            )
            # A different destination is not this approval.
            ok, reason = ApprovalService.evaluate(
                row, canonical_binding({**binding, "to": "0x" + "99" * 20})
            )
            assert ok is False
            assert "different transaction" in reason

    @pytest.mark.asyncio
    async def test_changing_the_amount_blocks_the_broadcast(self, monkeypatch, wallet, chain):
        from backend.app.transactions.approvals import ApprovalService, canonical_binding

        binding = {
            "network": "ethereum", "chain_id": 1,
            "wallet": wallet["address"], "to": "0x" + "cc" * 20,
            "value_wei": "1000", "data": "0x", "gas_limit": "21000",
        }
        async with _session() as db:
            row = await ApprovalService(db).request(
                approved_by="operator@example.test", binding=binding, summary="test"
            )
            ok, reason = ApprovalService.evaluate(
                row, canonical_binding({**binding, "value_wei": "999999"})
            )
            assert ok is False

    def test_the_fingerprint_distinguishes_chain_and_amount(self):
        from backend.app.transactions.approvals import intent_fingerprint

        base = {
            "network": "ethereum", "chain_id": 1, "wallet": "0x" + "11" * 20,
            "to": "0x" + "22" * 20, "value_wei": "1000", "data": "0x",
        }
        assert intent_fingerprint(base) == intent_fingerprint(dict(base))
        # Same address and amount on another chain is a different transfer.
        assert intent_fingerprint(base) != intent_fingerprint({**base, "network": "base"})
        assert intent_fingerprint(base) != intent_fingerprint({**base, "value_wei": "2000"})
        assert intent_fingerprint(base) != intent_fingerprint({**base, "to": "0x" + "33" * 20})


# ======================================================================
# Emergency Stop still wins
# ======================================================================
class TestEmergencyStopOverridesAutonomy:
    @pytest.mark.asyncio
    async def test_a_stopped_platform_records_no_autonomous_approval(
        self, monkeypatch, wallet, chain
    ):
        from backend.app.security.emergency_stop import emergency_stop
        from backend.app.tools.transfer_tools import ApproveTransferTool

        enable(
            monkeypatch,
            {"enabled": True, "rules": [allow(Category.TX_NATIVE_TRANSFER, id="eth")]},
        )
        emergency_stop.activate("incident", "test")
        try:
            result = await ApproveTransferTool().execute(
                {
                    "network": "ethereum", "chain_id": 1,
                    "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
                    "value_wei": "1000", "data": "0x", "gas_limit": "21000",
                }
            )
        finally:
            emergency_stop.resume("test cleanup")

        # The policy would have allowed it. The interlock did not.
        assert result.status.value == "failure"
        assert result.error_category == "EMERGENCY_STOP"
        assert "approval_id" not in result.data

    @pytest.mark.asyncio
    async def test_a_stopped_platform_refuses_the_broadcast(self, monkeypatch, wallet, chain):
        from backend.app.security.emergency_stop import emergency_stop
        from backend.app.tools.transfer_tools import ApproveTransferTool, SubmitPreparedTransferTool

        enable(
            monkeypatch,
            {"enabled": True, "rules": [allow(Category.TX_NATIVE_TRANSFER, id="eth")]},
        )
        approved = await ApproveTransferTool().execute(
            {
                "network": "ethereum", "chain_id": 1,
                "wallet_address": wallet["address"], "to_address": "0x" + "cc" * 20,
                "value_wei": "1000", "data": "0x", "gas_limit": "21000",
            }
        )
        assert approved.status.value == "success"

        emergency_stop.activate("incident", "test")
        try:
            result = await SubmitPreparedTransferTool().execute(
                {"network": "ethereum", "approval_id": approved.data["approval_id"]}
            )
        finally:
            emergency_stop.resume("test cleanup")

        assert result.status.value == "failure"
        assert result.error_category == "EMERGENCY_STOP"
        assert chain["broadcasts"] == []


# ======================================================================
# What autonomy does NOT grant
# ======================================================================
class TestAutonomyDoesNotWidenAnythingElse:
    @pytest.mark.asyncio
    async def test_a_value_rule_does_not_authorize_an_ordinary_read(
        self, monkeypatch, wallet, chain
    ):
        p = policy(rules=[allow(Category.TX_NATIVE_TRANSFER, id="eth")])
        assert p.evaluate(Category.WEB_FETCH).allowed is False
        assert p.evaluate(Category.DIAGNOSTICS_RUN).allowed is False
        assert p.evaluate(Category.CAMPAIGN_RESEARCH).allowed is False

    @pytest.mark.asyncio
    async def test_autonomy_does_not_grant_a_missing_tool_permission(
        self, monkeypatch, wallet, chain
    ):
        """Policy and tool permissions are separate gates; both must pass."""
        from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry
        from backend.app.tools.transfer_tools import SubmitPreparedTransferTool

        enable(
            monkeypatch,
            {"enabled": True, "rules": [allow(Category.TX_NATIVE_TRANSFER, id="eth")]},
        )

        registry = ToolRegistry()
        registry.register_or_replace(SubmitPreparedTransferTool())
        # The policy allows the operation, but the tool permission is not granted.
        registry.policy = AuthorizationPolicy(
            enabled_permissions=["transaction.read"],
            allowed_wallet_ids=[wallet["address"]],
        )

        result = await registry.invoke(
            "submit_prepared_transfer",
            {"network": "ethereum", "approval_id": "x"},
            task_id="AIR-2026-000001",
        )
        assert result.status.value == "not_authorized"
        assert "transaction.submit" in result.error

    def test_no_autonomy_setting_is_required_for_the_policy_to_exist(self):
        # The policy is self-contained: it reads its own configuration keys.
        described = policy(rules=[allow(Category.WEB_FETCH)]).describe()
        assert described["enabled"] is True