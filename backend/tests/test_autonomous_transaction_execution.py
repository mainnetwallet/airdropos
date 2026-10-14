"""Autonomous transaction execution: authorization, integrity, broadcast and recovery.

Every test here uses a key generated inside the test, a fake RPC and an in-memory
database. No real private key, no real funds and no real broadcast is involved.

The tests are grouped by the claim they defend:

* ``TestDecoding``            what the engine can and cannot read out of signed bytes
* ``TestApprovalBinding``     the defect that made every broadcast impossible
* ``TestAutonomousAuthorization`` unattended execution without a human per transaction
* ``TestSpendCeilings``       no fixed application-level amount or count cap
* ``TestIntegrityRefusals``   tampering, malformed bytes, unauthorized wallets
* ``TestEmergencyStop``       the stop still stops broadcasts
* ``TestBroadcastLifecycle``  pending / confirmed / reverted / failed, and duplicates
* ``TestPersistence``         what is written, and what a restart can reconcile
* ``TestVaultProtections``    the signing guard rails that were already there
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import pytest
from eth_account import Account
from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.models import BlockchainTransaction, TransactionApproval, Wallet
from backend.app.security import autonomous_policy as policy_module
from backend.app.security.emergency_stop import EmergencyStopError, StopPolicy, emergency_stop
from backend.app.tools import transfer_tools
from backend.app.tools.transfer_tools import (
    ApproveTransferTool,
    PrepareNativeTransferTool,
    PrepareTokenTransferTool,
    SubmitPreparedTransferTool,
    _approval_fields,
)
from backend.app.transactions import history
from backend.app.transactions.approvals import ApprovalService, bind_verifier, canonical_binding
from backend.app.transactions.engine import (
    SubmissionAuthorization,
    TransactionEngine,
    TransactionRejected,
    canonical_calldata,
    decode_raw_transaction,
    nonce_manager,
)
from backend.app.transactions.spend_ledger import SpendLedger
from backend.app.wallets.vault import KeyVault, SigningDenied, SigningRequest, vault

CHAIN_ID = 1
RECIPIENT = "0x2222222222222222222222222222222222222222"
ATTACKER = "0x3333333333333333333333333333333333333333"
ERC20 = "0x4444444444444444444444444444444444444444"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
GAS_LIMIT = 65_000
GAS_PRICE = 3_000_000_000
NONCE = 7
ERC20_CALLDATA = (
    "0xa9059cbb" + "00" * 12 + "ab" * 20 + format(1_234_567_890, "064x")
)


# ======================================================================
# Fixtures
# ======================================================================
@pytest.fixture
def burner():
    """A real throwaway EVM key, stored through the real vault.

    Generated here, used only in this process, never funded and never broadcast.
    """
    account = Account.create()
    key_hex = account.key.hex()
    if not key_hex.startswith("0x"):
        key_hex = "0x" + key_hex
    ciphertext, iv_b64, tag_b64 = vault.encrypt(key_hex)
    return {
        "address": account.address,
        "private_key": key_hex,
        "material": (ciphertext, iv_b64, tag_b64),
    }


@pytest.fixture
async def burner_wallet(_isolated_database, burner):
    """The throwaway key persisted as a signable wallet row."""
    from backend.app.database.session import session_scope

    async with session_scope() as db:
        row = Wallet(
            label="burner",
            address=burner["address"].lower(),
            chain_type="EVM",
            encrypted_secret=burner["material"][0],
            encryption_iv=burner["material"][1],
            encryption_tag=burner["material"][2],
            supported_networks=["ethereum"],
            is_active=True,
            is_locked=False,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {
            "id": row.id,
            "address": row.address,
            "private_key": burner["private_key"],
            "material": burner["material"],
        }


@pytest.fixture
def autonomous():
    """A policy that authorizes unattended native and token transfers."""
    policy_module.reset_policy_cache()
    policy_module._cached = policy_module.AutonomousPolicy.from_raw(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "native-allow",
                    "category": "tx.native_transfer",
                    "action": "allow",
                    "allowed_networks": ["ethereum"],
                    "allowed_source_wallets": None,
                },
                {
                    "id": "token-allow",
                    "category": "tx.token_transfer",
                    "action": "allow",
                    "allowed_networks": ["ethereum"],
                },
            ],
        }
    )
    yield policy_module.get_policy()
    policy_module.reset_policy_cache()


@pytest.fixture
def no_autonomy():
    policy_module.reset_policy_cache()
    policy_module._cached = policy_module.AutonomousPolicy.from_raw(
        {"enabled": False, "rules": []}
    )
    yield
    policy_module.reset_policy_cache()


@pytest.fixture
def verifier_bound():
    bind_verifier()
    from backend.app.transactions.engine import engine

    previous = engine.approval_verifier
    yield engine
    engine.approval_verifier = previous


def _abi_string(text: str) -> str:
    """ABI-encode a single `string` return value, as a contract would."""
    body = text.encode()
    padded = body + b"\x00" * ((32 - len(body) % 32) % 32)
    return (
        "0x"
        + format(0x20, "064x")
        + format(len(body), "064x")
        + padded.hex()
    )


# ERC-20 view selectors, so the fake answers like the real contract would.
_SELECTOR_DECIMALS = "313ce567"
_SELECTOR_SYMBOL = "95d89b41"
_SELECTOR_BALANCE_OF = "70a08231"


class FakeRpc:
    """Scriptable stand-in for the JSON-RPC transport. Records every send."""

    def __init__(self) -> None:
        self.sent: List[str] = []
        self.receipt: Optional[Dict[str, Any]] = None
        self.balance_wei = 10 ** 20
        self.nonce = NONCE
        self.gas_price = GAS_PRICE
        self.gas_limit = GAS_LIMIT
        self.token_decimals = 18
        self.token_symbol = "USDC"
        self.token_balance = 10 ** 12
        self.fail_send_with: Optional[Exception] = None

    async def __call__(self, network, method, params):
        if method == "eth_sendRawTransaction":
            if self.fail_send_with is not None:
                raise self.fail_send_with
            self.sent.append(params[0])
            return "0x" + uuid.uuid4().hex * 2
        if method == "eth_getBalance":
            return hex(self.balance_wei)
        if method == "eth_getTransactionCount":
            return hex(self.nonce)
        if method == "eth_gasPrice":
            return hex(self.gas_price)
        if method == "eth_estimateGas":
            return hex(self.gas_limit)
        if method == "eth_call":
            data = str((params[0] or {}).get("data") or "0x")[2:10].lower()
            if data == _SELECTOR_DECIMALS:
                return hex(self.token_decimals)
            if data == _SELECTOR_SYMBOL:
                return _abi_string(self.token_symbol)
            if data == _SELECTOR_BALANCE_OF:
                return hex(self.token_balance)
            return "0x" + "00" * 32
        if method == "eth_getTransactionReceipt":
            return self.receipt
        if method == "eth_blockNumber":
            return hex(100)
        raise AssertionError(f"unexpected RPC method {method}")


@pytest.fixture
def rpc(monkeypatch):
    """Replace the transport for *every* engine instance with a fake.

    Patched on the class, not on the shared instance, so a test that builds its own
    `TransactionEngine` is faked too. No test in this file may reach a real node.
    """
    fake = FakeRpc()
    monkeypatch.setattr(TransactionEngine, "_rpc", fake)
    monkeypatch.setattr(
        TransactionEngine, "rpc_url", staticmethod(lambda network: "https://rpc.invalid")
    )
    nonce_manager._locks.clear()
    nonce_manager._pending.clear()
    yield fake
    nonce_manager._pending.clear()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Belt and braces: any un-faked HTTP call in this file is a bug, so fail loudly."""
    import httpx

    async def forbidden(*args, **kwargs):
        raise AssertionError("a test attempted a real HTTP request")

    monkeypatch.setattr(httpx.AsyncClient, "post", forbidden)


@pytest.fixture(autouse=True)
def _no_singleton_leak():
    """The process-wide `engine` is shared; a test must not leave overrides on it.

    Patching an attribute on the singleton rather than on the class survives
    `monkeypatch`'s undo, because undo restores whatever the attribute held *at patch
    time* - which may itself have been a class-level fake. This removes any instance
    level override afterwards so one test cannot silently reconfigure the next.
    """
    from backend.app.transactions.engine import engine as shared

    yield
    for name in ("_rpc", "rpc_url"):
        shared.__dict__.pop(name, None)


def sign_raw(burner: Dict[str, Any], **overrides) -> str:
    """Sign a transaction with the throwaway key, with any field overridable."""
    tx: Dict[str, Any] = {
        "to": RECIPIENT,
        "value": 0,
        "data": ERC20_CALLDATA,
        "gas": GAS_LIMIT,
        "gasPrice": GAS_PRICE,
        "nonce": NONCE,
        "chainId": CHAIN_ID,
    }
    tx.update(overrides)
    raw = Account.from_key(burner["private_key"]).sign_transaction(tx).raw_transaction.hex()
    return raw if raw.startswith("0x") else "0x" + raw


def authorization_for(burner: Dict[str, Any], *, approved_by: str = "policy:autonomous:native-allow",
                      approval_id: str = "appr-1", **overrides) -> SubmissionAuthorization:
    fields: Dict[str, Any] = {
        "wallet_address": burner["address"],
        "authorized_addresses": frozenset({burner["address"]}),
        "estimated_cost_usd": 0.0,
        "approved_by": approved_by,
        "approval_id": approval_id,
        "human_approved": True,
        "allowed_destinations": frozenset({RECIPIENT}),
        "expected_chain_id": CHAIN_ID,
        "expected_to": RECIPIENT,
        "expected_value_wei": 0,
        "expected_data": ERC20_CALLDATA,
        "expected_gas_limit": GAS_LIMIT,
    }
    fields.update(overrides)
    return SubmissionAuthorization(**fields)


# ======================================================================
# TestDecoding
# ======================================================================
class TestDecoding:
    """The engine reads the transaction, not the caller's description of it."""

    def test_every_supported_format_decodes_every_bound_field(self, burner):
        variants = {
            "legacy": dict(to=RECIPIENT, value=0, data=ERC20_CALLDATA, gas=GAS_LIMIT,
                           gasPrice=GAS_PRICE, nonce=NONCE, chainId=CHAIN_ID),
            "EIP-2930": dict(to=RECIPIENT, value=0, data=ERC20_CALLDATA, gas=GAS_LIMIT,
                             gasPrice=GAS_PRICE, nonce=NONCE, chainId=CHAIN_ID, type=1),
            "EIP-1559": dict(to=RECIPIENT, value=0, data=ERC20_CALLDATA, gas=GAS_LIMIT,
                             maxFeePerGas=GAS_PRICE * 2, maxPriorityFeePerGas=GAS_PRICE,
                             nonce=NONCE, chainId=CHAIN_ID, type=2),
        }
        for label, tx in variants.items():
            raw = "0x" + Account.from_key(burner["private_key"]).sign_transaction(tx).raw_transaction.hex()
            decoded = decode_raw_transaction(raw)
            assert decoded.chain_id == CHAIN_ID, label
            assert decoded.nonce == NONCE, label
            assert decoded.gas_limit == GAS_LIMIT, label
            assert decoded.to and decoded.to.lower() == RECIPIENT, label
            assert decoded.value == 0, label
            assert decoded.data_hex == ERC20_CALLDATA, label

    def test_a_legacy_transaction_without_eip155_carries_no_chain_id(self, burner):
        # v = 27/28: unprotected, therefore replayable anywhere. The engine must not
        # invent a chain id for it.
        raw = "0x" + Account.from_key(burner["private_key"]).sign_transaction(
            dict(to=RECIPIENT, value=0, data="0x", gas=21000, gasPrice=GAS_PRICE,
                 nonce=NONCE, chainId=None)
        ).raw_transaction.hex()
        decoded = decode_raw_transaction(raw)
        assert decoded.chain_id is None
        assert decoded.gas_limit == 21000

    @pytest.mark.parametrize(
        "blob",
        ["0x", "0xdeadbeef", "0x03f8", "0x04c0", "0x05", "0xc0", "0xf8"],
    )
    def test_malformed_or_unsupported_bytes_are_refused(self, blob):
        with pytest.raises(TransactionRejected) as caught:
            decode_raw_transaction(blob)
        assert caught.value.category == "UNDECODABLE_TX"

    def test_a_destination_of_the_wrong_length_is_refused(self, burner):
        import rlp

        fields = [b"", b"", b"", bytes.fromhex("22" * 19), b"", b"", b"\x1d", b"\x01", b"\x02"]
        raw = b"\x02" + rlp.encode(fields)
        with pytest.raises(TransactionRejected):
            decode_raw_transaction("0x" + raw.hex())

    def test_calldata_case_is_normalised_but_meaning_is_not(self):
        assert canonical_calldata("0xA9059CBB") == canonical_calldata("0xa9059cbb")
        assert canonical_calldata("0xA9059CBB") != canonical_calldata("0xA9059CBC")


# ======================================================================
# TestApprovalBinding
# ======================================================================
class TestApprovalBinding:
    """The defect: the engine's binding omitted `data` and `gas_limit`.

    `ApprovalService.evaluate` requires every field an approval covers to be stated by
    the transaction offered for broadcast, so omitting those two made every legitimate
    transfer - however correct - fail verification.
    """

    def test_the_approval_covers_data_and_gas_limit(self):
        binding = _approval_fields(
            network="ethereum", chain_id=1, wallet_address="0xabc", to_address=RECIPIENT,
            value_wei=0, data=ERC20_CALLDATA, gas_limit=GAS_LIMIT,
        )
        assert set(binding) == {"network", "chain_id", "wallet", "to", "value_wei", "data", "gas_limit"}

    async def test_a_correct_authorization_verifies_and_broadcasts(
        self, monkeypatch, burner, autonomous, rpc
    ):
        """The whole point: a correct, policy-authorized transfer reaches the node."""
        stored = canonical_binding(
            _approval_fields(
                network="ethereum", chain_id=CHAIN_ID, wallet_address=burner["address"],
                to_address=RECIPIENT, value_wei=0, data=ERC20_CALLDATA, gas_limit=GAS_LIMIT,
            )
        )
        seen: Dict[str, Any] = {}

        class Verifier:
            async def verify_and_consume(self, *, approval_id, approved_by, binding):
                seen["binding"] = binding
                seen["approval_id"] = approval_id
                seen["approved_by"] = approved_by
                offered = canonical_binding(binding)
                return all(key in offered and offered[key] == value for key, value in stored.items())

        engine = TransactionEngine(approval_verifier=Verifier())
        tx_hash = await engine.submit_raw(
            "ethereum", sign_raw(burner), authorization=authorization_for(burner)
        )
        assert tx_hash.startswith("0x")
        assert len(rpc.sent) == 1

        # The binding carried the decoded calldata and gas limit, not the approval's.
        assert seen["binding"]["data"] == ERC20_CALLDATA
        assert seen["binding"]["gas_limit"] == str(GAS_LIMIT)
        assert seen["binding"]["to"].lower() == RECIPIENT

    async def test_the_binding_is_built_from_the_bytes_not_the_approval(
        self, monkeypatch, burner, autonomous, rpc
    ):
        """If the engine copied the approval's own fields into the binding, the
        comparison would be vacuous. Sign something else and it must still be caught."""
        stored = canonical_binding(
            _approval_fields(
                network="ethereum", chain_id=CHAIN_ID, wallet_address=burner["address"],
                to_address=RECIPIENT, value_wei=0, data=ERC20_CALLDATA, gas_limit=GAS_LIMIT,
            )
        )

        class Verifier:
            async def verify_and_consume(self, *, approval_id, approved_by, binding):
                offered = canonical_binding(binding)
                assert offered["data"] == "0xdeadbeef"
                assert offered["gas_limit"] == "21000"
                return all(key in offered and offered[key] == value for key, value in stored.items())

        engine = TransactionEngine(approval_verifier=Verifier())
        # The intent is declared as the calldata below; the bytes say otherwise.
        with pytest.raises(TransactionRejected) as caught:
            await engine.submit_raw(
                "ethereum",
                sign_raw(burner, data="0xdeadbeef", gas=21000),
                authorization=authorization_for(
                    burner, expected_data="0xdeadbeef", expected_gas_limit=21000
                ),
            )
        assert caught.value.category == "HUMAN_APPROVAL_REQUIRED"
        assert rpc.sent == []

    async def test_evaluation_reports_exactly_which_fields_are_unstated(self):
        """`evaluate` is the check that refused everything; keep its message precise."""
        from datetime import datetime, timedelta, timezone

        approved = TransactionApproval(
            approval_id="a1",
            approved_by="policy:autonomous:native-allow",
            network="ethereum", chain_id=1,
            wallet_address="0xabc", to_address=RECIPIENT, value_wei="0",
            binding={
                "network": "ethereum", "chain_id": "1", "wallet": "0xabc",
                "to": RECIPIENT, "value_wei": "0", "data": ERC20_CALLDATA,
                "gas_limit": str(GAS_LIMIT),
            },
            policy_rule_id="native-allow",
            status="PENDING",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        ok, reason = ApprovalService.evaluate(
            approved, {"network": "ethereum", "chain_id": "1", "wallet": "0xabc",
                       "to": RECIPIENT, "value_wei": "0"}
        )
        assert ok is False
        assert "data" in reason and "gas_limit" in reason

        # Stating them, and stating them correctly, is what makes it usable again.
        complete = {
            "network": "ethereum", "chain_id": "1", "wallet": "0xabc", "to": RECIPIENT,
            "value_wei": "0", "data": ERC20_CALLDATA, "gas_limit": str(GAS_LIMIT),
        }
        ok, reason = ApprovalService.evaluate(approved, complete)
        assert ok is True, reason


# ======================================================================
# TestAutonomousAuthorization
# ======================================================================
class TestAutonomousAuthorization:
    """Unattended execution authorized by configuration, not by an LLM statement."""

    async def test_a_policy_rule_authorizes_without_any_human_approving(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        prepared = await PrepareNativeTransferTool().execute({
            "network": "ethereum", "to_address": RECIPIENT, "amount": "0.01",
        })
        assert prepared.status.value == "success", prepared.error
        binding = prepared.data["proposal"]["approval_fields"]

        # No `approved_by`: the policy is consulted, and no person is asked.
        approved = await ApproveTransferTool().execute(approve_fields("ethereum", binding))
        assert approved.status.value == "success", approved.error
        assert approved.data["authorized_by_policy"] is True
        assert approved.data["authorized_by"].startswith("policy:autonomous:")

        rpc.receipt = _raw_receipt(status=1)
        submitted = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approved.data["approval_id"],
            "wait_for_receipt": False,
        })
        assert submitted.status.value == "success", submitted.error
        assert len(rpc.sent) == 1

    async def test_a_transfer_is_refused_when_the_policy_is_off(
        self, burner_wallet, no_autonomy, rpc, verifier_bound
    ):
        prepared = await PrepareNativeTransferTool().execute({
            "network": "ethereum", "to_address": RECIPIENT, "amount": "0.01",
        })
        binding = prepared.data["proposal"]["approval_fields"]

        approved = await ApproveTransferTool().execute(approve_fields("ethereum", binding))
        # Deny by default: the operator is asked rather than the agent proceeding.
        assert approved.status.value == "needs_user_action"
        assert "disabled" in (approved.error or "").lower()
        assert rpc.sent == []

    async def test_a_policy_naming_the_wallet_is_required_not_guessed(
        self, burner_wallet, rpc, verifier_bound, monkeypatch
    ):
        policy_module.reset_policy_cache()
        policy_module._cached = policy_module.AutonomousPolicy.from_raw({
            "enabled": True,
            "rules": [{
                "id": "narrow", "category": "tx.native_transfer", "action": "allow",
                "allowed_networks": ["ethereum"],
                "allowed_source_wallets": [ATTACKER],
            }],
        })
        try:
            prepared = await PrepareNativeTransferTool().execute({
                "network": "ethereum", "to_address": RECIPIENT, "amount": "0.01",
            })
            binding = prepared.data["proposal"]["approval_fields"]
            approved = await ApproveTransferTool().execute(approve_fields("ethereum", binding))
            assert approved.status.value == "needs_user_action"
            assert "permitted" in (approved.error or "").lower()
        finally:
            policy_module.reset_policy_cache()

    async def test_the_agent_cannot_name_itself_as_the_approver(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        """A machine identity is refused outright, so the model cannot approve itself."""
        for identity in ("agent", "system", "llm", "auto", "assistant", "claude", "worker"):
            approved = await ApproveTransferTool().execute(
                approve_fields(
                    "ethereum",
                    {"chain_id": "1", "wallet": burner_wallet["address"], "to": RECIPIENT,
                     "value_wei": "0", "data": "0x", "gas_limit": str(GAS_LIMIT)},
                    approved_by=identity,
                )
            )
            assert approved.status.value != "success", identity
        assert rpc.sent == []

    async def test_an_empty_approver_falls_back_to_the_policy_not_to_a_person(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        """An empty identity is 'no human named', which means the policy decides."""
        approved = await ApproveTransferTool().execute(
            approve_fields(
                "ethereum",
                {"chain_id": "1", "wallet": burner_wallet["address"], "to": RECIPIENT,
                 "value_wei": "0", "data": "0x", "gas_limit": str(GAS_LIMIT)},
                approved_by="",
            )
        )
        assert approved.status.value == "success"
        assert approved.data["authorized_by_policy"] is True

    async def test_a_narrowed_policy_stops_a_broadcast_that_was_already_approved(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        """Approval is not a blank cheque: the live policy is re-read before signing."""
        prepared = await PrepareNativeTransferTool().execute({
            "network": "ethereum", "to_address": RECIPIENT, "amount": "0.01",
        })
        binding = prepared.data["proposal"]["approval_fields"]
        approved = await ApproveTransferTool().execute(approve_fields("ethereum", binding))
        approval_id = approved.data["approval_id"]

        # The operator narrows the rule between approval and broadcast.
        policy_module._cached = policy_module.AutonomousPolicy.from_raw({
            "enabled": True,
            "rules": [{"id": "native-allow", "category": "tx.native_transfer", "action": "deny"}],
        })
        submitted = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id, "wait_for_receipt": False,
        })
        assert submitted.status.value == "failure"
        assert submitted.error_category == "POLICY_DENIED"
        assert rpc.sent == []


# ======================================================================
# TestSpendCeilings
# ======================================================================
class TestSpendCeilings:
    """No fixed application-level amount cap, and no daily count cap."""

    def test_the_platform_ships_with_no_amount_ceiling(self):
        """With no operator value set, nothing in the platform imposes a ceiling."""
        parsed = settings.__class__(_env_file=None)
        assert parsed.MAX_TRANSACTION_VALUE_USD is None
        assert parsed.MAX_DAILY_GAS_SPEND_USD is None

    @pytest.mark.parametrize("blank", ["", "   ", "none", "NULL", "unlimited"])
    def test_a_blank_setting_means_unset_not_zero(self, blank):
        parsed = settings.__class__(
            MAX_TRANSACTION_VALUE_USD=blank, MAX_DAILY_GAS_SPEND_USD=blank, _env_file=None
        )
        assert parsed.MAX_TRANSACTION_VALUE_USD is None
        assert parsed.MAX_DAILY_GAS_SPEND_USD is None

    def test_a_number_is_still_honoured(self):
        parsed = settings.__class__(
            MAX_TRANSACTION_VALUE_USD="12.5", MAX_DAILY_GAS_SPEND_USD="99", _env_file=None
        )
        assert parsed.MAX_TRANSACTION_VALUE_USD == 12.5
        assert parsed.MAX_DAILY_GAS_SPEND_USD == 99.0

    def test_there_is_no_daily_transaction_count_cap(self):
        """Assert the absence explicitly: no count-based transaction limiter exists."""
        names = {n for n in dir(settings) if n.isupper()}
        assert not any(
            "PER_DAY" in n and "TRANSACTION" in n.replace("SPEND", "")
            or "TRANSACTION_COUNT" in n
            or "MAX_TRANSACTIONS" in n
            for n in names
        )

    async def test_a_large_transfer_is_not_refused_for_its_size(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MAX_TRANSACTION_VALUE_USD", None)
        monkeypatch.setattr(settings, "MAX_DAILY_GAS_SPEND_USD", None)
        engine = TransactionEngine(ledger=SpendLedger(path=str(tmp_path / "ledger.db")))
        await engine.check_spend_limits(10_000_000.0)  # would have raised before
        reservation = await engine.reserve_spend(10_000_000.0)
        assert reservation

    async def test_an_operator_ceiling_is_still_enforced_when_configured(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(settings, "MAX_TRANSACTION_VALUE_USD", 10.0)
        engine = TransactionEngine(ledger=SpendLedger(path=str(tmp_path / "ledger.db")))
        with pytest.raises(TransactionRejected) as caught:
            await engine.reserve_spend(11.0)
        assert caught.value.category == "LIMIT_EXCEEDED"

    async def test_an_operator_daily_ceiling_is_still_enforced_when_configured(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(settings, "MAX_TRANSACTION_VALUE_USD", None)
        monkeypatch.setattr(settings, "MAX_DAILY_GAS_SPEND_USD", 5.0)
        engine = TransactionEngine(ledger=SpendLedger(path=str(tmp_path / "ledger.db")))
        await engine.reserve_spend(4.0)
        with pytest.raises(TransactionRejected) as caught:
            await engine.reserve_spend(2.0)
        assert caught.value.category == "DAILY_LIMIT_EXCEEDED"

    async def test_spend_is_still_recorded_with_no_ceiling_configured(self, monkeypatch, tmp_path):
        """Accounting survives the removal of the cap, so a ceiling can be added later."""
        monkeypatch.setattr(settings, "MAX_TRANSACTION_VALUE_USD", None)
        monkeypatch.setattr(settings, "MAX_DAILY_GAS_SPEND_USD", None)
        ledger = SpendLedger(path=str(tmp_path / "ledger.db"))
        engine = TransactionEngine(ledger=ledger)
        for _ in range(50):
            await engine.reserve_spend(100.0)
        assert ledger.spent_last_24h() == pytest.approx(5000.0)

    async def test_the_policy_does_not_invent_a_usd_ceiling(self, autonomous, monkeypatch):
        """With no platform ceiling and none on the rule, nothing caps the value here."""
        monkeypatch.setattr(settings, "MAX_TRANSACTION_VALUE_USD", None)
        decision = autonomous.evaluate(
            "tx.native_transfer", network="ethereum",
            wallet=RECIPIENT, destination=ATTACKER, value_usd=10_000_000.0,
        )
        assert decision.allowed is True

    async def test_a_rules_own_ceiling_is_still_enforced(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_TRANSACTION_VALUE_USD", None)
        policy_module.reset_policy_cache()
        policy_module._cached = policy_module.AutonomousPolicy.from_raw({
            "enabled": True,
            "rules": [{
                "id": "capped", "category": "tx.native_transfer", "action": "allow",
                "max_value_native": "0.001",
            }],
        })
        try:
            decision = policy_module.get_policy().evaluate(
                "tx.native_transfer", network="ethereum", wallet=RECIPIENT,
                destination=ATTACKER, value_native="1.0",
            )
            assert decision.allowed is False
            assert "exceeds" in decision.reason
        finally:
            policy_module.reset_policy_cache()


# ======================================================================
# TestIntegrityRefusals
# ======================================================================
class TestIntegrityRefusals:
    """Nothing reaches the node unless the bytes match the authorized intent."""

    async def _submit(self, raw, authorization):
        class Verifier:
            async def verify_and_consume(self, **kwargs):
                return True

        engine = TransactionEngine(approval_verifier=Verifier())
        return await engine.submit_raw("ethereum", raw, authorization=authorization)

    @pytest.mark.parametrize(
        "field,override,category",
        [
            ("to", dict(to=ATTACKER), "DESTINATION_MISMATCH"),
            ("value", dict(value=1), "VALUE_MISMATCH"),
            ("data", dict(data="0xdeadbeef"), "CALLDATA_MISMATCH"),
            ("gas", dict(gas=21000), "GAS_LIMIT_MISMATCH"),
        ],
    )
    async def test_a_tampered_field_is_refused(self, burner, monkeypatch, field, override, category):
        """The authorization still states the approved intent; the bytes say otherwise.

        The engine compares the two itself, before the verifier is ever consulted, so a
        substituted destination, amount, calldata or gas limit cannot reach the node.
        """
        sent: List[str] = []

        async def spy(self, network, m, params):
            sent.append(m)
            return "0x" + "cd" * 32

        monkeypatch.setattr(TransactionEngine, "_rpc", spy)

        class Verifier:
            async def verify_and_consume(self, **kwargs):
                raise AssertionError("the verifier must not be consulted for a tampered transaction")

        engine = TransactionEngine(approval_verifier=Verifier())
        with pytest.raises(TransactionRejected) as caught:
            await engine.submit_raw(
                "ethereum", sign_raw(burner, **override), authorization=authorization_for(burner)
            )
        assert caught.value.category == category
        assert "eth_sendRawTransaction" not in sent

    async def test_a_contract_creation_transaction_is_refused(self, burner):
        raw = "0x" + Account.from_key(burner["private_key"]).sign_transaction(
            dict(to=None, value=0, data=ERC20_CALLDATA, gas=GAS_LIMIT,
                 gasPrice=GAS_PRICE, nonce=NONCE, chainId=CHAIN_ID)
        ).raw_transaction.hex()
        with pytest.raises(TransactionRejected) as caught:
            await self._submit(
                raw if raw.startswith("0x") else "0x" + raw,
                authorization_for(burner, expected_to=RECIPIENT),
            )
        assert caught.value.category == "DESTINATION_NOT_ALLOWED"

    async def test_a_wrong_chain_id_is_refused(self, burner):
        with pytest.raises(TransactionRejected) as caught:
            await self._submit(
                sign_raw(burner, chainId=137),
                authorization_for(burner, expected_chain_id=137),
            )
        assert caught.value.category == "CHAIN_ID_MISMATCH"

    async def test_a_signature_by_another_key_is_refused(self, burner):
        other = Account.create()
        raw = "0x" + other.sign_transaction(
            dict(to=RECIPIENT, value=0, data=ERC20_CALLDATA, gas=GAS_LIMIT,
                 gasPrice=GAS_PRICE, nonce=NONCE, chainId=CHAIN_ID)
        ).raw_transaction.hex()
        with pytest.raises(TransactionRejected) as caught:
            await self._submit(raw if raw.startswith("0x") else "0x" + raw,
                               authorization_for(burner))
        assert caught.value.category == "NOT_AUTHORIZED"

    async def test_an_unauthorized_wallet_cannot_broadcast(self, burner):
        with pytest.raises(TransactionRejected) as caught:
            await self._submit(
                sign_raw(burner),
                authorization_for(burner, authorized_addresses=frozenset({ATTACKER})),
            )
        assert caught.value.category == "NOT_AUTHORIZED"

    async def test_an_empty_authorized_set_fails_closed(self, burner):
        with pytest.raises(TransactionRejected) as caught:
            await self._submit(
                sign_raw(burner), authorization_for(burner, authorized_addresses=frozenset())
            )
        assert caught.value.category == "NOT_AUTHORIZED"

    async def test_a_destination_outside_the_allowed_set_is_refused(self, burner):
        """The allowlist is a second, independent check on the same field."""
        with pytest.raises(TransactionRejected) as caught:
            await self._submit(
                sign_raw(burner, to=ATTACKER),
                authorization_for(burner, expected_to=ATTACKER,
                                  allowed_destinations=frozenset({RECIPIENT})),
            )
        assert caught.value.category == "DESTINATION_NOT_ALLOWED"

    async def test_an_authorization_that_omits_its_intent_is_refused(self, burner):
        auth = authorization_for(burner)
        assert auth.unstated_intent_fields() == []
        incomplete = authorization_for(burner, expected_gas_limit=None)
        assert incomplete.unstated_intent_fields() == ["gas_limit"]
        with pytest.raises(TransactionRejected) as caught:
            await self._submit(sign_raw(burner), incomplete)
        assert caught.value.category == "INTENT_NOT_AUTHORIZED"

    @pytest.mark.parametrize(
        "field", ["expected_chain_id", "expected_to", "expected_value_wei",
                  "expected_data", "expected_gas_limit"]
    )
    async def test_every_intent_field_is_required(self, burner, field):
        auth = authorization_for(burner, **{field: None})
        with pytest.raises(TransactionRejected) as caught:
            await self._submit(sign_raw(burner), auth)
        assert caught.value.category == "INTENT_NOT_AUTHORIZED"

    @pytest.mark.parametrize("blob", ["0x", "0xdeadbeef", "0x03f8", "0x04c0"])
    async def test_malformed_bytes_never_reach_the_node(self, burner, blob):
        with pytest.raises(TransactionRejected) as caught:
            await self._submit(blob, authorization_for(burner))
        assert caught.value.category == "UNDECODABLE_TX"

    async def test_broadcast_is_disabled_without_a_verifier(self, burner):
        engine = TransactionEngine(approval_verifier=None)
        with pytest.raises(TransactionRejected) as caught:
            await engine.submit_raw(
                "ethereum", sign_raw(burner), authorization=authorization_for(burner)
            )
        assert caught.value.category == "HUMAN_APPROVAL_REQUIRED"

    async def test_a_non_human_approver_string_is_refused(self, burner):
        with pytest.raises(TransactionRejected) as caught:
            await self._submit(sign_raw(burner), authorization_for(burner, approved_by="agent"))
        assert caught.value.category == "HUMAN_APPROVAL_REQUIRED"


# ======================================================================
# TestEmergencyStop
# ======================================================================
class TestEmergencyStop:
    """The stop still stops broadcasts, and it is never read as 'probably fine'."""

    @pytest.fixture(autouse=True)
    def _stopped(self):
        emergency_stop._stopped = True
        emergency_stop._policy = set(StopPolicy.ALLOWED)
        yield
        emergency_stop._stopped = False
        emergency_stop._policy = set(StopPolicy.ALLOWED)

    async def test_the_engine_refuses_to_broadcast(self, burner):
        class Verifier:
            async def verify_and_consume(self, **kwargs):
                return True

        engine = TransactionEngine(approval_verifier=Verifier())
        with pytest.raises(EmergencyStopError):
            await engine.submit_raw(
                "ethereum", sign_raw(burner), authorization=authorization_for(burner)
            )

    async def test_the_approval_is_not_consumed_while_stopped(self, burner):
        class Verifier:
            async def verify_and_consume(self, **kwargs):
                raise AssertionError("the verifier must not be consulted while stopped")

        engine = TransactionEngine(approval_verifier=Verifier())
        with pytest.raises(EmergencyStopError):
            await engine.submit_raw(
                "ethereum", sign_raw(burner), authorization=authorization_for(burner)
            )

    async def test_the_submission_tool_refuses_to_sign(self, burner_wallet, autonomous):
        result = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": "whatever",
        })
        assert result.status.value == "failure"
        assert result.error_category == "EMERGENCY_STOP"

    async def test_approvals_are_not_recorded_while_stopped(self, burner_wallet, autonomous):
        result = await ApproveTransferTool().execute({
            "network": "ethereum", "wallet_address": burner_wallet["address"],
            "to_address": RECIPIENT, "value_wei": "0", "data": "0x",
            "gas_limit": str(GAS_LIMIT),
        })
        assert result.status.value == "failure"
        assert result.error_category == "EMERGENCY_STOP"


# ======================================================================
# TestBroadcastLifecycle
# ======================================================================
def _raw_receipt(status: int, block: int = 50) -> Dict[str, Any]:
    """A receipt exactly as a JSON-RPC node returns it (hex-encoded)."""
    return {
        "transactionHash": "0x" + "ab" * 32,
        "blockNumber": hex(block),
        "blockHash": "0x" + "cd" * 32,
        "gasUsed": "0x5208",
        "effectiveGasPrice": hex(GAS_PRICE),
        "contractAddress": None,
        "status": "0x1" if status == 1 else "0x0",
        "logs": [],
    }


def _engine_receipt(status: int, block: int = 50) -> Dict[str, Any]:
    """A receipt in the shape `engine.get_receipt` returns, for reconciliation tests."""
    return {
        "tx_hash": "0x" + "ab" * 32,
        "block_number": block,
        "block_hash": "0x" + "cd" * 32,
        "gas_used": 0x5208,
        "effective_gas_price": GAS_PRICE,
        "contract_address": None,
        "status": "CONFIRMED" if status == 1 else "REVERTED",
        "logs": [],
    }


def approve_fields(network: str, binding: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    """Map a stored binding onto `approve_transfer`'s parameter names.

    The binding is the canonical comparison form (`wallet`, `to`); the tool's schema
    uses its own names (`wallet_address`, `to_address`). This is the mapping the agent
    has to perform, done once here so the tests read as intent rather than plumbing.
    """
    params = {
        "network": network,
        "chain_id": int(binding["chain_id"]),
        "wallet_address": binding["wallet"],
        "to_address": binding["to"],
        "value_wei": binding["value_wei"],
        "data": binding["data"],
        "gas_limit": binding["gas_limit"],
    }
    params.update(extra)
    return params


async def _authorized_native(rpc, burner_wallet, autonomous, verifier_bound):
    """Prepare a native transfer and let the *policy* authorize it. No human involved."""
    prepared = await PrepareNativeTransferTool().execute({
        "network": "ethereum", "to_address": RECIPIENT, "amount": "0.01",
    })
    assert prepared.status.value == "success", prepared.error
    binding = prepared.data["proposal"]["approval_fields"]
    approved = await ApproveTransferTool().execute(approve_fields("ethereum", binding))
    assert approved.status.value == "success", approved.error
    return approved.data["approval_id"]


class TestBroadcastLifecycle:
    """What the tool reports is exactly what the receipt says."""

    async def test_a_confirmed_transaction_is_reported_as_confirmed(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = _raw_receipt(1)
        result = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id,
        })
        assert result.status.value == "success", result.error
        assert result.data["status"] == "CONFIRMED"
        assert result.evidence["authoritative"] is True

    async def test_a_pending_transaction_is_never_reported_as_confirmed(
        self, burner_wallet, autonomous, rpc, verifier_bound, monkeypatch
    ):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = None
        async def pending(network, tx_hash, attempts=4):
            return {"status": "PENDING", "confirmations": 0}
        monkeypatch.setattr(transfer_tools, "_await_receipt", pending)

        result = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id,
        })
        assert result.data["status"] == "PENDING"
        assert result.evidence["authoritative"] is False
        assert result.error_category == "TX_PENDING"

    async def test_a_reverted_transaction_is_reported_as_a_failure(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = _raw_receipt(0)
        result = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id,
        })
        assert result.status.value == "failure"
        assert result.error_category == "TX_REVERTED"
        assert result.data["status"] == "REVERTED"

    async def test_an_unreachable_node_is_reported_as_unknown_not_as_pending(
        self, burner_wallet, autonomous, rpc, verifier_bound, monkeypatch
    ):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        async def unreachable(network, tx_hash, attempts=4):
            return {"status": "UNKNOWN", "confirmations": 0}
        monkeypatch.setattr(transfer_tools, "_await_receipt", unreachable)
        result = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id,
        })
        assert result.error_category == "TX_OUTCOME_UNKNOWN"
        assert result.evidence["authoritative"] is False

    async def test_the_receipt_wait_distinguishes_unreachable_from_unmined(
        self, monkeypatch
    ):
        from backend.app.transactions.engine import TransactionRejected as Rejected

        async def always_fails(self, network, method, params):
            raise Rejected("node unreachable", "RPC_FAILURE")

        monkeypatch.setattr(TransactionEngine, "_rpc", always_fails)
        assert (await transfer_tools._await_receipt("ethereum", "0xab", attempts=2))["status"] == "UNKNOWN"

        async def answers_pending(self, network, method, params):
            return None

        monkeypatch.setattr(TransactionEngine, "_rpc", answers_pending)
        assert (await transfer_tools._await_receipt("ethereum", "0xab", attempts=2))["status"] == "PENDING"


class TestDuplicatesAndAmbiguity:
    """A repeat must not send again, and an unknown outcome must not be resent."""

    async def test_a_duplicate_task_does_not_broadcast_twice(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = _raw_receipt(1)
        first = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id, "wait_for_receipt": False,
        })
        assert first.status.value == "success"
        assert len(rpc.sent) == 1

        # A duplicated task re-asks for the very same transfer.
        again = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id, "wait_for_receipt": False,
        })
        assert again.status.value == "success"
        assert again.data["resent"] is False
        assert len(rpc.sent) == 1, "the transaction was broadcast a second time"
        assert again.data["tx_hash"] == first.data["tx_hash"]

    async def test_an_ambiguous_broadcast_is_not_resent_and_stays_in_flight(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.fail_send_with = TransactionRejected("connection reset", "RPC_FAILURE")

        result = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id, "wait_for_receipt": False,
        })
        assert result.status.value == "failure"
        assert result.error_category == "RPC_FAILURE"

        from backend.app.database.session import session_scope

        async with session_scope() as db:
            rows = (await db.execute(select(BlockchainTransaction))).scalars().all()
        assert len(rows) == 1
        # Neither confirmed nor failed: the node may have taken it.
        assert rows[0].status == history.STATUS_SIGNED
        assert rows[0].tx_hash is None

    async def test_two_workers_reserve_different_nonces(self, burner_wallet, rpc):
        """Concurrent signers must not pick the same nonce."""
        binding = {"to": RECIPIENT, "value_wei": "0", "data": "0x", "gas_limit": "21000"}
        address = burner_wallet["address"]
        first = await transfer_tools._sign(binding, _chain(), address, burner_wallet["material"])
        assert decode_raw_transaction(first).nonce == NONCE
        nonce_manager.reserve("ethereum", address, NONCE)
        second = await transfer_tools._sign(binding, _chain(), address, burner_wallet["material"])
        assert decode_raw_transaction(second).nonce == NONCE + 1

    async def test_an_approval_with_no_gas_limit_cannot_be_signed(self, burner_wallet, rpc):
        from backend.app.transactions.engine import TransactionRejected as Rejected

        with pytest.raises(Rejected) as caught:
            await transfer_tools._sign(
                {"to": RECIPIENT, "value_wei": "0", "data": "0x", "gas_limit": "0"},
                _chain(), burner_wallet["address"], burner_wallet["material"],
            )
        assert caught.value.category == "GAS_LIMIT_NOT_AUTHORIZED"


def _chain():
    from backend.app.blockchain.chains import get_chain

    return get_chain("ethereum")


# ======================================================================
# TestPersistence
# ======================================================================
class TestPersistence:
    """What is written, and what survives a restart."""

    async def test_the_full_lifecycle_is_recorded(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = _raw_receipt(1)
        result = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id,
        })
        assert result.status.value == "success"

        from backend.app.database.session import session_scope

        async with session_scope() as db:
            row = (await db.execute(select(BlockchainTransaction))).scalars().one()
        assert row.status == history.STATUS_CONFIRMED
        assert row.tx_hash == result.data["tx_hash"]
        assert row.network == "ethereum"
        assert row.from_address == burner_wallet["address"]
        assert row.to_address == RECIPIENT
        assert row.value_wei == str(prepare_value())
        assert row.submitted_at is not None
        assert row.confirmed_at is not None
        assert row.confirmation_count >= 1

    async def test_a_reverted_receipt_is_recorded_as_reverted(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = _raw_receipt(0)
        await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id,
        })
        from backend.app.database.session import session_scope

        async with session_scope() as db:
            row = (await db.execute(select(BlockchainTransaction))).scalars().one()
        assert row.status == history.STATUS_REVERTED
        assert row.error_category == "TX_REVERTED"

    async def test_a_failed_persistence_never_reports_success(
        self, burner_wallet, autonomous, rpc, verifier_bound, monkeypatch
    ):
        """The node took it, the record failed: that is a loud failure, not a success."""
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = None
        async def pending(network, tx_hash, attempts=4):
            return {"status": "PENDING", "confirmations": 0}
        monkeypatch.setattr(transfer_tools, "_await_receipt", pending)

        async def explode(db, row, tx_hash):
            raise RuntimeError("disk is full")

        monkeypatch.setattr(history, "record_submitted", explode)

        result = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id,
        })
        assert result.status.value == "failure"
        assert result.error_category == "BROADCAST_UNRECORDED"
        assert result.retryable is False
        assert result.data["tx_hash"].startswith("0x")
        assert len(rpc.sent) == 1

    async def test_a_restart_reconciles_an_orphaned_broadcast(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        """A hash on disk with no settlement is settled from the chain after a restart."""
        from backend.app.database.session import session_scope

        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = None
        async def pending(network, tx_hash, attempts=4):
            return {"status": "PENDING", "confirmations": 0}
        import backend.app.tools.transfer_tools as tt
        original = tt._await_receipt
        tt._await_receipt = pending
        try:
            await SubmitPreparedTransferTool().execute({
                "network": "ethereum", "approval_id": approval_id,
            })
        finally:
            tt._await_receipt = original

        async with session_scope() as db:
            row = (await db.execute(select(BlockchainTransaction))).scalars().one()
        assert row.status == history.STATUS_PENDING
        tx_hash = row.tx_hash

        # The process restarts. The chain now has a receipt.
        rpc.receipt = _raw_receipt(1, block=80)
        async with session_scope() as db:
            settled = await history.reconcile_in_flight(db, transfer_tools.engine)

        assert tx_hash in settled.get(history.STATUS_CONFIRMED, [])
        async with session_scope() as db:
            row = await db.get(BlockchainTransaction, row.id)
        assert row.status == history.STATUS_CONFIRMED
        assert row.confirmed_at is not None

    async def test_an_unreachable_node_does_not_settle_a_transaction(
        self, burner_wallet, autonomous, rpc, verifier_bound, monkeypatch
    ):
        from backend.app.database.session import session_scope

        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = None
        async def pending(network, tx_hash, attempts=4):
            return {"status": "PENDING", "confirmations": 0}
        import backend.app.tools.transfer_tools as tt
        original = tt._await_receipt
        tt._await_receipt = pending
        try:
            await SubmitPreparedTransferTool().execute({
                "network": "ethereum", "approval_id": approval_id,
            })
        finally:
            tt._await_receipt = original

        async with session_scope() as db:
            await db.execute(
                BlockchainTransaction.__table__.update()
                .values(status=history.STATUS_SUBMITTED)
            )
            await db.commit()

        async def unreachable(self, network, method, params):
            raise TransactionRejected("node down", "RPC_FAILURE")

        monkeypatch.setattr(TransactionEngine, "_rpc", unreachable)
        async with session_scope() as db:
            settled = await history.reconcile_in_flight(db, transfer_tools.engine)

        assert not settled.get(history.STATUS_CONFIRMED)
        async with session_scope() as db:
            row = (await db.execute(select(BlockchainTransaction))).scalars().one()
        assert row.status != history.STATUS_CONFIRMED

    async def test_an_existing_approval_cannot_be_reused(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = _raw_receipt(1)
        first = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id, "wait_for_receipt": False,
        })
        assert first.status.value == "success"

        from backend.app.database.session import session_scope

        async with session_scope() as db:
            row = await db.get(TransactionApproval, await _approval_pk(db, approval_id))
        assert row.status == "CONSUMED"

    async def test_a_consumed_approval_is_refused_for_a_second_broadcast(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = _raw_receipt(1)
        await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id, "wait_for_receipt": False,
        })
        # A different intent that reuses the spent approval id.
        from backend.app.database.session import session_scope

        async with session_scope() as db:
            row = await db.get(TransactionApproval, await _approval_pk(db, approval_id))
            ok, reason = ApprovalService.evaluate(row)
        assert ok is False
        assert "already been used" in reason


async def _approval_pk(db, approval_id: str):
    row = await db.scalar(
        select(TransactionApproval).where(TransactionApproval.approval_id == approval_id)
    )
    return row.id


def prepare_value() -> int:
    return 10 ** 16  # 0.01 ETH


# ======================================================================
# TestVaultProtections
# ======================================================================
class TestVaultProtections:
    """The signing guard rails that already existed must still hold."""

    def test_an_empty_authorized_set_refuses_to_sign(self, burner):
        request = SigningRequest(
            wallet_address=burner["address"], chain_id=CHAIN_ID,
            transaction={"to": RECIPIENT, "value": 0, "data": "0x", "gas": 21000,
                         "gasPrice": GAS_PRICE, "nonce": NONCE, "chainId": CHAIN_ID},
        )
        with pytest.raises(SigningDenied):
            vault.sign_transaction(request, *burner["material"], authorized_addresses=set())

    def test_an_unlisted_wallet_refuses_to_sign(self, burner):
        request = SigningRequest(
            wallet_address=burner["address"], chain_id=CHAIN_ID,
            transaction={"to": RECIPIENT, "value": 0, "data": "0x", "gas": 21000,
                         "gasPrice": GAS_PRICE, "nonce": NONCE, "chainId": CHAIN_ID},
        )
        with pytest.raises(SigningDenied):
            vault.sign_transaction(request, *burner["material"], authorized_addresses={ATTACKER})

    def test_an_unsupported_chain_refuses_to_sign(self, burner):
        request = SigningRequest(
            wallet_address=burner["address"], chain_id=999_999,
            transaction={"to": RECIPIENT, "value": 0, "data": "0x", "gas": 21000,
                         "gasPrice": GAS_PRICE, "nonce": NONCE, "chainId": 999_999},
        )
        with pytest.raises(SigningDenied):
            vault.sign_transaction(request, *burner["material"],
                                   authorized_addresses={burner["address"]})

    @pytest.mark.parametrize(
        "selector",
        ["0x8bda01ab", "0xf2fde38b", "0x715018a6", "0xffc9a479"],
    )
    def test_a_restricted_selector_is_never_signed(self, burner, selector):
        with pytest.raises(SigningDenied):
            vault.sign_transaction(
                SigningRequest(
                    wallet_address=burner["address"], chain_id=CHAIN_ID,
                    transaction={"to": ERC20, "value": 0, "data": selector + "00" * 32,
                                 "gas": 21000, "gasPrice": GAS_PRICE, "nonce": NONCE,
                                 "chainId": CHAIN_ID},
                ),
                *burner["material"], authorized_addresses={burner["address"]},
            )

    def test_oversized_calldata_is_refused(self, burner):
        with pytest.raises(SigningDenied):
            KeyVault._validate_transaction({"to": ERC20, "data": "0x" + "ab" * 100_001})

    async def test_the_private_key_never_reaches_the_tool_result(self, burner_wallet,
                                                                 autonomous, rpc, verifier_bound):
        approval_id = await _authorized_native(rpc, burner_wallet, autonomous, verifier_bound)
        rpc.receipt = _raw_receipt(1)
        result = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approval_id,
        })
        serialized = repr(result.to_dict() if hasattr(result, "to_dict") else result.__dict__)
        assert burner_wallet["private_key"] not in serialized
        assert burner_wallet["private_key"][2:] not in serialized


# ======================================================================
# TestTokenTransfers
# ======================================================================
class TestTokenTransfers:
    """ERC-20 transfers move value in calldata, with zero native coin."""

    async def test_a_token_transfer_binds_the_token_contract_and_the_calldata(
        self, burner_wallet, autonomous, rpc, verifier_bound
    ):
        rpc.receipt = _raw_receipt(1)
        rpc.token_balance = 2 * 10 ** 21
        prepared = await PrepareTokenTransferTool().execute({
            "network": "ethereum", "to_address": RECIPIENT, "amount": "1234.56789",
            "contract_address": USDC,
        })
        assert prepared.status.value == "success", prepared.error
        proposal = prepared.data["proposal"]
        assert proposal["kind"] == "erc20"
        assert proposal["value_wei"] == "0"
        assert proposal["data"].startswith("0xa9059cbb")

        fields = proposal["approval_fields"]
        # The destination is the token contract, not the recipient.
        assert fields["to"] == USDC.lower()
        assert fields["value_wei"] == "0"

        approved = await ApproveTransferTool().execute(
            approve_fields("ethereum", proposal["approval_fields"])
        )
        assert approved.status.value == "success", approved.error

        result = await SubmitPreparedTransferTool().execute({
            "network": "ethereum", "approval_id": approved.data["approval_id"],
        })
        assert result.status.value == "success", result.error
        assert len(rpc.sent) == 1

        decoded = decode_raw_transaction(rpc.sent[0])
        assert decoded.data_hex == proposal["data"]
        assert decoded.to.lower() == USDC.lower()
        assert decoded.value == 0

    async def test_a_native_transfer_rule_cannot_authorize_a_token_transfer(
        self, burner_wallet, rpc, verifier_bound, monkeypatch
    ):
        policy_module.reset_policy_cache()
        policy_module._cached = policy_module.AutonomousPolicy.from_raw({
            "enabled": True,
            "rules": [{"id": "native-only", "category": "tx.native_transfer", "action": "allow"}],
        })
        try:
            fields = {
                "network": "ethereum", "chain_id": "1",
                "wallet": burner_wallet["address"], "to": USDC,
                "value_wei": "0", "data": ERC20_CALLDATA,
                "gas_limit": str(GAS_LIMIT),
            }
            result = await ApproveTransferTool().execute(approve_fields("ethereum", fields))
            assert result.status.value == "needs_user_action"
        finally:
            policy_module.reset_policy_cache()

    def test_the_category_is_derived_from_the_binding_not_supplied(self):
        from backend.app.tools.transfer_tools import transfer_category

        assert transfer_category({"data": "0x", "value_wei": "1"}) == "tx.native_transfer"
        assert transfer_category({"data": ERC20_CALLDATA, "value_wei": "0"}) == "tx.token_transfer"