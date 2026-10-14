"""Transaction Engine: construction, gas estimation, nonce coordination, submission.

All value-moving logic lives here, behind deterministic guards that run
INDEPENDENTLY of anything the LLM said. The LLM proposes; this engine disposes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Protocol, runtime_checkable

import httpx

from backend.app.core.config import settings
from backend.app.security.emergency_stop import EmergencyStopError, emergency_stop
from backend.app.blockchain.chains import (
    ChainError,
    EvmChainAdapter,
    classify_rpc_http_status,
    get_chain,
)
from backend.app.transactions.spend_ledger import SpendLedger, SpendLimitExceeded

logger = logging.getLogger(__name__)

# Native gas cost model (native token price intentionally NOT hardcoded; the
# caller supplies an externally-sourced price or the engine records cost as unknown).
GAS_COST_CATEGORIES = {
    "SWAP": 150_000,
    "BRIDGE_DEPOSIT": 220_000,
    "CLAIM": 180_000,
    "APPROVE": 55_000,
    "NFT_MINT": 120_000,
    "TRANSFER": 21_000,
    "UNKNOWN": 100_000,
}

# Addresses the platform refuses to send value to, by category.
DENYLIST_CATEGORIES = {
    "MIXER": {
        "0x0000000000000000000000000000000000000000",
    },
    "SANCTIONED": set(),
}


class TransactionRejected(Exception):
    """A transaction was refused by deterministic validation."""

    def __init__(self, reason: str, category: str = "VALIDATION"):
        super().__init__(reason)
        self.category = category


class NonceManager:
    """Coordinates nonces per (network, address) to prevent conflicts and
    duplicate submissions across concurrent workers."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}
        self._pending: Dict[str, int] = {}

    def _key(self, network: str, address: str) -> str:
        return f"{network.lower()}:{address.lower()}"

    def lock_for(self, network: str, address: str) -> asyncio.Lock:
        return self._locks.setdefault(self._key(network, address), asyncio.Lock())

    def reserve(self, network: str, address: str, nonce: int) -> None:
        """Record an in-flight nonce so no other worker reuses it."""
        key = self._key(network, address)
        self._pending[key] = max(self._pending.get(key, -1), nonce)

    def release(self, network: str, address: str, nonce: int) -> None:
        key = self._key(network, address)
        if self._pending.get(key) == nonce:
            self._pending.pop(key, None)

    def reserved(self, network: str, address: str) -> int:
        return self._pending.get(self._key(network, address), -1)


nonce_manager = NonceManager()


@dataclass
class TransactionIntent:
    """A fully-specified, validated intent to move value on-chain."""

    network: str
    chain_id: int
    wallet_address: str
    to_address: str
    value_wei: int = 0
    data: str = "0x"
    category: str = "UNKNOWN"
    task_id: Optional[str] = None
    max_gas_gwei: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# Approver identities that are never a human. A broadcast needs a person's approval,
# not the agent approving itself.
NON_HUMAN_APPROVERS = frozenset({"", "agent", "system", "llm", "assistant", "claude", "worker", "none", "auto"})


@runtime_checkable
class ApprovalVerifier(Protocol):
    """Server-side proof that a named human approved exactly this broadcast.

    The engine never trusts a flag the caller sets. It asks an independent verifier, backed by
    storage the caller cannot write to, to confirm that ``approval_id`` is a real, unexpired,
    unused approval issued to ``approved_by`` for exactly the transaction described by
    ``binding`` (network, chain id, wallet, destination, value, raw-transaction hash and cost),
    and to mark it used. Return False (or raise) to refuse. Implementations must make the
    check-and-consume atomic so one approval can authorise one broadcast only.
    """

    async def verify_and_consume(
        self, *, approval_id: str, approved_by: str, binding: Mapping[str, str]
    ) -> bool: ...


@dataclass(frozen=True)
class DecodedRawTransaction:
    """Every field of a signed raw transaction that an authorization binds to.

    ``nonce`` and ``gas_limit`` are decoded alongside destination, value and calldata
    because an approval binds to the gas limit too: a transfer approved at 65,000 gas
    is a different authorization from one approved at 21,000, and the engine can only
    tell them apart from the signed bytes themselves.
    """

    tx_type: int
    chain_id: Optional[int]
    nonce: int
    gas_limit: int
    to: Optional[str]
    value: int
    data: bytes

    @property
    def data_hex(self) -> str:
        """Calldata as canonical lowercase ``0x`` hex. Hex case carries no meaning."""
        return "0x" + self.data.hex()


# Signed transaction types whose payload layout is decoded below, as
# (chainId, nonce, gas, to, value, data, field count). Anything else (blob,
# set-code/EIP-7702, unknown future types) is refused rather than guessed at.
_TX_LAYOUT = {
    # EIP-2930: chainId, nonce, gasPrice, gas, to, value, data, accessList, v, r, s
    1: (0, 1, 3, 4, 5, 6, 11),
    # EIP-1559: chainId, nonce, tip, maxFee, gas, to, value, data, accessList, v, r, s
    2: (0, 1, 4, 5, 6, 7, 12),
}

# A legacy transaction carries no chainId field and has a different arity:
# nonce, gasPrice, gas, to, value, data, v, r, s.
_LEGACY_FIELDS = 9
_LEGACY_NONCE, _LEGACY_GAS, _LEGACY_TO, _LEGACY_VALUE, _LEGACY_DATA = 0, 2, 3, 4, 5


def _uint(value: bytes) -> int:
    """RLP integers are big-endian and may be zero-length, which is still zero."""
    return int.from_bytes(value, "big")


def decode_raw_transaction(raw_tx_hex: str) -> DecodedRawTransaction:
    """Decode chain id, nonce, gas limit, destination, value and calldata.

    Raises TransactionRejected for anything that cannot be decoded with certainty. This
    is the only place a signed transaction is interpreted, and everything the engine
    later authorizes is read from here rather than from whatever the caller asserted.
    """
    import rlp
    from hexbytes import HexBytes

    def reject(why: str) -> TransactionRejected:
        return TransactionRejected(f"Raw transaction rejected: {why}", "UNDECODABLE_TX")

    try:
        raw = bytes(HexBytes(raw_tx_hex if raw_tx_hex.startswith("0x") else "0x" + raw_tx_hex))
    except Exception:
        raise reject("not valid hex") from None
    if not raw:
        raise reject("empty payload")

    def to_hex(to_bytes: bytes) -> Optional[str]:
        if to_bytes == b"":
            return None  # contract creation
        if len(to_bytes) != 20:
            raise reject("malformed destination")
        return "0x" + to_bytes.hex()

    try:
        if raw[0] >= 0xC0:  # legacy transaction: a bare RLP list
            fields = rlp.decode(raw)
            if not isinstance(fields, list) or len(fields) != _LEGACY_FIELDS:
                raise reject("malformed legacy transaction")
            v = _uint(fields[6])
            chain_id = (v - 35) // 2 if v >= 35 else None  # v of 27/28 carries no chain id
            return DecodedRawTransaction(
                tx_type=0,
                chain_id=chain_id,
                nonce=_uint(fields[_LEGACY_NONCE]),
                gas_limit=_uint(fields[_LEGACY_GAS]),
                to=to_hex(fields[_LEGACY_TO]),
                value=_uint(fields[_LEGACY_VALUE]),
                data=bytes(fields[_LEGACY_DATA]),
            )
        layout = _TX_LAYOUT.get(raw[0])
        if layout is None:
            raise reject(f"unsupported transaction type 0x{raw[0]:02x}")
        fields = rlp.decode(raw[1:])
        c_i, n_i, g_i, to_i, val_i, data_i, count = layout
        if not isinstance(fields, list) or len(fields) != count:
            raise reject("malformed typed transaction")
        return DecodedRawTransaction(
            tx_type=raw[0],
            chain_id=_uint(fields[c_i]),
            nonce=_uint(fields[n_i]),
            gas_limit=_uint(fields[g_i]),
            to=to_hex(fields[to_i]),
            value=_uint(fields[val_i]),
            data=bytes(fields[data_i]),
        )
    except TransactionRejected:
        raise
    except Exception:
        raise reject("could not be decoded") from None


def canonical_calldata(value: Any) -> str:
    """Normalise calldata to lowercase ``0x`` hex so equivalent bytes compare equal.

    Hex digit case is not semantic - ``0xA9059CBB`` and ``0xa9059cbb`` are the same four
    bytes - so comparing them verbatim would refuse a correct transfer whenever the
    encoder and the decoder disagree on case, which is not a security property.
    """
    text = str(value or "0x").strip()
    if not text.startswith("0x"):
        text = "0x" + text
    body = text[2:]
    try:
        bytes.fromhex(body)
    except ValueError:
        return text.lower()  # not hex; leave it for the caller to reject explicitly
    return "0x" + body.lower()


@dataclass(frozen=True)
class SubmissionAuthorization:
    """Everything `submit_raw` requires before it will broadcast.

    There is deliberately no default: a caller cannot broadcast without
    stating the wallet it is authorised for, the cost it is spending, the
    identity that authorized it, and the transaction it claims to cover.

    The ``expected_*`` fields are the trusted execution context: the statement of
    what this authorization is *for*. The engine decodes the signed bytes and
    requires them to match these exactly before it sends anything, and then builds
    the verification binding from the decoded values - never from these fields.
    Copying them into the binding unchecked would make the approval comparison
    vacuous: it would compare the approval to itself instead of to the transaction.
    Leaving them unset refuses the broadcast rather than skipping the comparison.
    """

    wallet_address: str
    authorized_addresses: frozenset
    estimated_cost_usd: float
    approved_by: str
    approval_id: str
    # Compatibility only. This flag is caller-supplied, so it proves nothing: it is necessary
    # (False always refuses) but never sufficient. The engine's ApprovalVerifier is the proof.
    human_approved: bool = False
    task_id: Optional[str] = None
    # Destinations this broadcast may be sent to. Empty means none: it fails closed.
    allowed_destinations: frozenset = frozenset()
    # The authorized intent, verified against the decoded signed transaction.
    expected_chain_id: Optional[int] = None
    expected_to: Optional[str] = None
    expected_value_wei: Optional[int] = None
    expected_data: Optional[str] = None
    expected_gas_limit: Optional[int] = None

    def unstated_intent_fields(self) -> List[str]:
        """Which parts of the authorized intent this authorization does not state."""
        missing = []
        if self.expected_chain_id is None:
            missing.append("chain_id")
        if self.expected_to is None:
            missing.append("to")
        if self.expected_value_wei is None:
            missing.append("value_wei")
        if self.expected_data is None:
            missing.append("data")
        if self.expected_gas_limit is None:
            missing.append("gas_limit")
        return missing


class TransactionEngine:
    """Validates, builds, and submits transactions within hard limits."""

    def __init__(
        self,
        ledger: Optional[SpendLedger] = None,
        approval_verifier: Optional[ApprovalVerifier] = None,
    ) -> None:
        # Spend is persisted and shared across processes; see spend_ledger.py.
        self.ledger = ledger or SpendLedger()
        # None means raw broadcast stays disabled: there is no server-side approval store yet,
        # and a caller-supplied `human_approved=True` is not proof of approval.
        self.approval_verifier = approval_verifier

    @property
    def daily_spend_usd(self) -> float:
        """Spend (committed + reserved) in the rolling 24-hour window."""
        return self.ledger.spent_last_24h()

    # ------------------------------------------------------------------
    # Deterministic validation (never trusts the LLM)
    # ------------------------------------------------------------------
    def validate_intent(self, intent: TransactionIntent) -> None:
        """Reject malformed or over-limit intents. Raises TransactionRejected."""
        if not emergency_stop.can_submit_transactions():
            raise EmergencyStopError(
                "transaction_submission",
                "Emergency stop is active. New submissions are blocked. "
                "Already-broadcast transactions cannot be recalled.",
            )

        if intent.value_wei < 0:
            raise TransactionRejected("Negative transaction value.", "INVALID_VALUE")
        if not intent.to_address or not intent.to_address.startswith("0x"):
            raise TransactionRejected(f"Malformed destination address: {intent.to_address!r}", "INVALID_ADDRESS")
        if len(intent.to_address) != 42:
            raise TransactionRejected(
                f"Destination address must be 42 characters (0x + 40 hex): {intent.to_address!r}",
                "INVALID_ADDRESS",
            )
        try:
            int(intent.to_address, 16)
        except ValueError as exc:
            raise TransactionRejected(f"Destination is not hex: {intent.to_address!r}", "INVALID_ADDRESS") from exc

        for denylist in DENYLIST_CATEGORIES.values():
            if intent.to_address.lower() in {a.lower() for a in denylist}:
                raise TransactionRejected(
                    f"Destination {intent.to_address} is denylisted. Refusing to send value.",
                    "DENYLISTED",
                )

        if len(intent.data) > 200_000:
            raise TransactionRejected("Calldata exceeds 200KB.", "DATA_TOO_LARGE")

        estimated_gas = GAS_COST_CATEGORIES.get(intent.category, GAS_COST_CATEGORIES["UNKNOWN"])
        if estimated_gas <= 0:
            raise TransactionRejected("Invalid gas estimate.", "GAS_ESTIMATE")

    async def check_spend_limits(self, estimated_cost_usd: float) -> None:
        """Advisory, read-only check of any configured spend ceilings.

        Unset ceilings impose nothing: the platform ships without a fixed
        application-level amount or daily-spend cap, so a legitimate autonomous
        transfer is never refused for being large. An operator who configures a
        ceiling gets it enforced here and, atomically, in `reserve_spend`.

        This does NOT reserve anything, so on its own it cannot stop concurrent
        callers from collectively overspending. Anything that actually spends
        must use `reserve_spend`, which checks and reserves atomically.
        """
        per_transaction = settings.MAX_TRANSACTION_VALUE_USD
        if per_transaction is not None and estimated_cost_usd > per_transaction:
            raise TransactionRejected(
                f"Estimated transaction cost ${estimated_cost_usd:.2f} exceeds the per-transaction "
                f"limit of ${per_transaction:.2f} configured by the operator.",
                "LIMIT_EXCEEDED",
            )
        daily = settings.MAX_DAILY_GAS_SPEND_USD
        if daily is None:
            return
        projected = (await asyncio.to_thread(self.ledger.spent_last_24h)) + estimated_cost_usd
        if projected > daily:
            raise TransactionRejected(
                f"Projected daily spend ${projected:.2f} exceeds the daily limit of "
                f"${daily:.2f} configured by the operator.",
                "DAILY_LIMIT_EXCEEDED",
            )

    async def reserve_spend(self, estimated_cost_usd: float, ref: Optional[str] = None) -> str:
        """Atomically enforce any configured ceilings and reserve the cost.

        Returns a reservation id. The reservation is recorded either way: it is the
        durable record of what was attempted, and it is what an ambiguous broadcast
        outcome keeps counting against.
        """
        try:
            return await asyncio.to_thread(
                self.ledger.reserve,
                estimated_cost_usd,
                per_transaction_limit=settings.MAX_TRANSACTION_VALUE_USD,
                daily_limit=settings.MAX_DAILY_GAS_SPEND_USD,
                ref=ref,
            )
        except SpendLimitExceeded as exc:
            raise TransactionRejected(str(exc), exc.category) from exc

    async def commit_spend(self, reservation_id: str, actual_cost_usd: Optional[float] = None) -> None:
        await asyncio.to_thread(self.ledger.commit, reservation_id, actual_cost_usd)

    async def release_spend(self, reservation_id: str) -> bool:
        return await asyncio.to_thread(self.ledger.release, reservation_id)

    def record_spend(self, amount_usd: float) -> None:
        """Record already-incurred spend. Synchronous and durable (no fire-and-forget task)."""
        self.ledger.record(amount_usd)

    def reset_daily_spend(self) -> None:
        self.ledger.reset()

    # ------------------------------------------------------------------
    # RPC interaction
    # ------------------------------------------------------------------
    @staticmethod
    def rpc_url(network: str) -> Optional[str]:
        """Resolve a network's endpoint from the chain catalog.

        Returns None for an unknown or unconfigured network, which is the
        existing contract every caller already handles.
        """
        from backend.app.blockchain.chains import get_chain

        try:
            return EvmChainAdapter(get_chain(network)).rpc_url()
        except ChainError:
            return None

    async def _rpc(self, network: str, method: str, params: List[Any]) -> Any:
        url = self.rpc_url(network)
        if not url:
            raise TransactionRejected(f"No RPC configured for network '{network}'.", "NO_RPC")
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(url, json=payload)
        except Exception as exc:
            raise TransactionRejected(f"RPC call '{method}' failed: {exc}", "RPC_FAILURE") from exc

        # Status first. Previously `response.json()` ran unconditionally, so a provider
        # that answers 401/403 with an HTML error page raised JSONDecodeError and the
        # operator saw "RPC call failed: JSONDecodeError" - indistinguishable from a
        # dead node and impossible to act on. A rejected key is a configuration fault,
        # not an outage, and the category says so.
        http_error = classify_rpc_http_status(response.status_code, network)
        if http_error is not None:
            raise TransactionRejected(str(http_error), http_error.category)

        try:
            body = response.json()
        except Exception as exc:
            raise TransactionRejected(
                f"RPC endpoint returned a non-JSON body for '{method}' (HTTP {response.status_code}).",
                "RPC_MALFORMED",
            ) from exc

        if not isinstance(body, dict):
            raise TransactionRejected(
                f"RPC endpoint returned a non-object body for '{method}'.", "RPC_MALFORMED"
            )

        if body.get("error"):
            error = body["error"]
            message = error.get("message", "unknown") if isinstance(error, dict) else str(error)
            code = error.get("code") if isinstance(error, dict) else None
            category = "RPC_ERROR"
            # Some providers signal a bad key inside a 200 JSON-RPC error envelope.
            if code in (-32001, -32602, 401, 403) or "unauthorized" in str(message).lower():
                category = "RPC_AUTH"
            elif code == -32005 or "rate limit" in str(message).lower():
                category = "RPC_RATE_LIMITED"
            raise TransactionRejected(f"RPC error for {method}: {message}", category)

        return body.get("result")

    async def get_balance_wei(self, network: str, address: str) -> int:
        return int(await self._rpc(network, "eth_getBalance", [address, "latest"]), 16)

    async def get_nonce(self, network: str, address: str) -> int:
        """Return the next safe nonce, accounting for our own in-flight sends."""
        chain_nonce = int(await self._rpc(network, "eth_getTransactionCount", [address, "pending"]), 16)
        return max(chain_nonce, nonce_manager.reserved(network, address) + 1)

    async def estimate_gas(self, intent: TransactionIntent) -> Dict[str, Any]:
        """Estimate gas and cost. Returns cost as unknown unless a price is provided."""
        tx = {
            "from": intent.wallet_address,
            "to": intent.to_address,
            "value": hex(intent.value_wei),
            "data": intent.data,
        }
        try:
            gas_limit = int(await self._rpc(intent.network, "eth_estimateGas", [tx]), 16)
        except TransactionRejected as exc:
            # Only an on-chain revert justifies guessing. A rejected key, a missing
            # endpoint or a rate limit must NOT be downgraded to a static default:
            # that produced a confident-looking estimate built on no RPC data at all.
            if exc.category in ("RPC_AUTH", "RPC_RATE_LIMITED", "NO_RPC", "RPC_MALFORMED"):
                raise
            # Estimation reverts often mean the call would revert on-chain.
            gas_limit = GAS_COST_CATEGORIES.get(intent.category, GAS_COST_CATEGORIES["UNKNOWN"])

        try:
            gas_price = int(await self._rpc(intent.network, "eth_gasPrice", []), 16)
        except TransactionRejected as exc:
            if exc.category in ("RPC_AUTH", "RPC_RATE_LIMITED", "NO_RPC", "RPC_MALFORMED"):
                raise
            # A node that answers but cannot price gas is reported as unknown, never
            # as zero: "0 wei fee" reads as "free" and hides the gap.
            raise TransactionRejected(
                f"The RPC endpoint for '{intent.network}' did not return a gas price, so the "
                f"fee for this transaction cannot be determined. Refusing to quote a transfer "
                f"with an unknown cost.",
                "GAS_PRICE_UNAVAILABLE",
            ) from exc

        gas_price_gwei = gas_price / 1e9
        if intent.max_gas_gwei is not None and gas_price_gwei > intent.max_gas_gwei:
            raise TransactionRejected(
                f"Gas price {gas_price_gwei:.2f} gwei exceeds the caller's cap of {intent.max_gas_gwei} gwei.",
                "GAS_PRICE_TOO_HIGH",
            )

        cost_wei = gas_limit * gas_price
        return {
            "gas_limit": gas_limit,
            "gas_price_gwei": round(gas_price_gwei, 4),
            "estimated_cost_wei": cost_wei,
            "estimated_cost_native": cost_wei / 1e18,
            # Native-token USD price is never invented; supply externally.
            "estimated_cost_usd": None,
            "note": "USD cost requires an externally sourced native-token price; not hardcoded.",
        }

    async def simulate(self, intent: TransactionIntent) -> Dict[str, Any]:
        """eth_call simulation. A revert here predicts an on-chain revert."""
        tx = {
            "from": intent.wallet_address,
            "to": intent.to_address,
            "value": hex(intent.value_wei),
            "data": intent.data,
        }
        try:
            result = await self._rpc(intent.network, "eth_call", [tx, "latest"])
            return {"success": True, "return_value": result, "will_revert": False}
        except TransactionRejected as exc:
            return {
                "success": False,
                "will_revert": True,
                "reason": str(exc),
                "note": "Simulation reverted. Submission would likely revert on-chain.",
            }

    @staticmethod
    def _recover_sender(raw_tx_hex: str) -> str:
        """Recover the signer of a raw signed transaction. Raises on anything undecodable."""
        try:
            from eth_account import Account

            raw = raw_tx_hex if raw_tx_hex.startswith("0x") else "0x" + raw_tx_hex
            return str(Account.recover_transaction(raw))
        except Exception as exc:
            raise TransactionRejected(
                f"Raw transaction could not be decoded; refusing to broadcast: {exc}", "UNDECODABLE_TX"
            ) from exc

    @staticmethod
    def _verify_signed_transaction(
        network: str, raw_tx_hex: str, authorization: SubmissionAuthorization
    ) -> DecodedRawTransaction:
        """Judge what a pre-signed transaction actually does, from its own bytes.

        Every field compared here is decoded from the transaction. Nothing is taken
        from the approval dictionary: an approval says what was authorized, and this
        says what is about to be sent, and the two must be identical.
        """
        missing = authorization.unstated_intent_fields()
        if missing:
            raise TransactionRejected(
                "The authorization does not state what the transaction is supposed to be "
                f"({', '.join(missing)}); refusing to broadcast.",
                "INTENT_NOT_AUTHORIZED",
            )

        decoded = decode_raw_transaction(raw_tx_hex)
        try:
            expected_chain = get_chain(network).chain_id
        except ChainError as exc:
            raise TransactionRejected(f"Unknown network: {exc}", "UNKNOWN_NETWORK") from exc

        if decoded.chain_id != expected_chain:
            # No chain id (pre-EIP-155) is replayable on any chain; a different one is the wrong chain.
            raise TransactionRejected(
                f"Raw transaction chain id {decoded.chain_id} does not match network "
                f"'{network}' ({expected_chain}); refusing to broadcast.",
                "CHAIN_ID_MISMATCH",
            )
        if decoded.to is None:
            raise TransactionRejected(
                "Contract-creation transactions cannot be broadcast through submit_raw.", "DESTINATION_NOT_ALLOWED"
            )
        to = decoded.to.lower()

        if decoded.chain_id != authorization.expected_chain_id:
            raise TransactionRejected(
                f"The transaction is for chain {decoded.chain_id}, but it was authorized for "
                f"chain {authorization.expected_chain_id}; refusing to broadcast.",
                "CHAIN_ID_MISMATCH",
            )
        if to != str(authorization.expected_to or "").lower():
            raise TransactionRejected(
                f"The transaction goes to {decoded.to}, but it was authorized to go to "
                f"{authorization.expected_to}; refusing to broadcast.",
                "DESTINATION_MISMATCH",
            )
        if decoded.value != authorization.expected_value_wei:
            raise TransactionRejected(
                f"The transaction moves {decoded.value} wei, but "
                f"{authorization.expected_value_wei} wei was authorized; refusing to broadcast.",
                "VALUE_MISMATCH",
            )
        if decoded.data_hex != canonical_calldata(authorization.expected_data):
            raise TransactionRejected(
                "The transaction's calldata does not match the authorized calldata; "
                "refusing to broadcast.",
                "CALLDATA_MISMATCH",
            )
        if decoded.gas_limit != authorization.expected_gas_limit:
            raise TransactionRejected(
                f"The transaction was signed with a gas limit of {decoded.gas_limit}, but "
                f"{authorization.expected_gas_limit} was authorized; refusing to broadcast.",
                "GAS_LIMIT_MISMATCH",
            )

        allowed = {str(a).lower() for a in (authorization.allowed_destinations or ())}
        if not allowed or to not in allowed:
            raise TransactionRejected(
                "Transaction destination is not in the approved destination set; refusing to broadcast.",
                "DESTINATION_NOT_ALLOWED",
            )
        for category, addresses in DENYLIST_CATEGORIES.items():
            if to in {a.lower() for a in addresses}:
                raise TransactionRejected(
                    f"Destination is on the {category} denylist; refusing to broadcast.", "DESTINATION_DENIED"
                )
        # Same restricted-operation rules the vault applies when it signs.
        from backend.app.wallets.vault import KeyVault, SigningDenied

        try:
            KeyVault._validate_transaction({"to": to, "data": decoded.data_hex})
        except SigningDenied as exc:
            raise TransactionRejected(str(exc), "RESTRICTED_OPERATION") from exc
        return decoded

    async def submit_raw(
        self,
        network: str,
        raw_tx_hex: str,
        *,
        authorization: SubmissionAuthorization,
    ) -> str:
        """Broadcast a signed transaction and return its hash.

        Fails closed. Before any broadcast this enforces, in order:
          1. Emergency Stop,
          2. a named authorizing identity, confirmed by the server-side ApprovalVerifier
             (the caller-supplied ``human_approved`` flag alone is never accepted),
          3. that the transaction was signed by an authorised wallet,
          4. that the decoded transaction - chain id, destination, value, calldata and
             gas limit read from the signed bytes themselves - is exactly the
             authorized intent, and is not contract creation, not on a denylist and
             carries no restricted selector,
          5. the approval being consumed for exactly this transaction, bound to those
             same decoded values,
          6. any operator-configured spend ceilings (atomic reservation), and
          7. Emergency Stop again, immediately before the RPC call.
        """
        if not emergency_stop.can_submit_transactions():
            raise EmergencyStopError(
                "broadcast", "Emergency stop is active; refusing to broadcast."
            )

        if not isinstance(authorization, SubmissionAuthorization):
            raise TransactionRejected("A SubmissionAuthorization is required to broadcast.", "NOT_AUTHORIZED")
        if (
            not authorization.human_approved
            or str(authorization.approved_by).strip().lower() in NON_HUMAN_APPROVERS
            or not str(authorization.approval_id or "").strip()
        ):
            raise TransactionRejected(
                "Broadcast requires an explicit authorization naming who approved it "
                "(approved_by and approval_id).",
                "HUMAN_APPROVAL_REQUIRED",
            )

        if self.approval_verifier is None:
            raise TransactionRejected(
                "Broadcast is disabled: no server-side approval verifier is configured, and a "
                "caller-supplied approval flag is not accepted as proof.",
                "HUMAN_APPROVAL_REQUIRED",
            )

        authorized = {str(a).lower() for a in (authorization.authorized_addresses or ())}
        if not authorized or str(authorization.wallet_address).lower() not in authorized:
            raise TransactionRejected(
                "Wallet is not in the authorized wallet set; refusing to broadcast.", "NOT_AUTHORIZED"
            )
        sender = self._recover_sender(raw_tx_hex)
        if sender.lower() != str(authorization.wallet_address).lower():
            raise TransactionRejected(
                "Raw transaction signer does not match the authorised wallet; refusing to broadcast.",
                "NOT_AUTHORIZED",
            )

        decoded = self._verify_signed_transaction(network, raw_tx_hex, authorization)

        try:
            from eth_utils import keccak

            raw_bytes = bytes.fromhex(raw_tx_hex[2:] if raw_tx_hex.startswith("0x") else raw_tx_hex)
            approved = await self.approval_verifier.verify_and_consume(
                approval_id=str(authorization.approval_id),
                approved_by=str(authorization.approved_by),
                # Built from the decoded transaction, and only after `_verify_signed_transaction`
                # proved those decoded values equal the authorized intent. Copying the intent
                # here instead would compare the approval against itself.
                binding={
                    "network": str(network).lower(),
                    "chain_id": str(decoded.chain_id),
                    "wallet": str(authorization.wallet_address).lower(),
                    "to": str(decoded.to),
                    "value_wei": str(decoded.value),
                    "data": decoded.data_hex,
                    "gas_limit": str(decoded.gas_limit),
                    "tx_hash": "0x" + keccak(raw_bytes).hex(),
                    "estimated_cost_usd": repr(float(authorization.estimated_cost_usd)),
                },
            )
        except Exception as exc:
            logger.warning("Approval verification failed: %s", type(exc).__name__)
            approved = False
        if approved is not True:
            raise TransactionRejected(
                "Broadcast requires an authorization that the server can verify for this exact "
                "transaction; none was found.",
                "HUMAN_APPROVAL_REQUIRED",
            )

        reservation_id = await self.reserve_spend(
            authorization.estimated_cost_usd,
            ref=f"{network}:{authorization.approval_id}:{authorization.task_id or '-'}",
        )

        try:
            # Stop may have been engaged while we were reserving.
            if not emergency_stop.can_submit_transactions():
                raise EmergencyStopError(
                    "broadcast", "Emergency stop is active; refusing to broadcast."
                )
            tx_hash = await self._rpc(network, "eth_sendRawTransaction", [raw_tx_hex])
        except (EmergencyStopError, TransactionRejected) as exc:
            # Release only when the transaction definitely was not sent. An
            # RPC_FAILURE (timeout/connection drop) is ambiguous: the node may
            # have accepted it, so the reservation keeps counting.
            if isinstance(exc, EmergencyStopError) or getattr(exc, "category", "") in ("RPC_ERROR", "NO_RPC"):
                await self.release_spend(reservation_id)
            else:
                logger.warning(
                    "Broadcast outcome unknown (%s); spend reservation %s retained.",
                    getattr(exc, "category", "?"), reservation_id,
                )
            raise
        except BaseException:
            logger.warning("Broadcast interrupted; spend reservation %s retained.", reservation_id)
            raise

        if not tx_hash:
            # The node answered without a hash: ambiguous, so keep the reservation.
            raise TransactionRejected("RPC accepted the transaction but returned no hash.", "NO_TX_HASH")

        await self.commit_spend(reservation_id)
        return tx_hash

    async def get_receipt(self, network: str, tx_hash: str) -> Optional[Dict[str, Any]]:
        """Fetch a receipt. None means still pending; it does not mean failure."""
        receipt = await self._rpc(network, "eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return None
        status = int(receipt.get("status", "0x0"), 16)
        return {
            "tx_hash": tx_hash,
            "block_number": int(receipt["blockNumber"], 16) if receipt.get("blockNumber") else None,
            "block_hash": receipt.get("blockHash"),
            "gas_used": int(receipt["gasUsed"], 16) if receipt.get("gasUsed") else None,
            "effective_gas_price": int(receipt["effectiveGasPrice"], 16) if receipt.get("effectiveGasPrice") else None,
            "contract_address": receipt.get("contractAddress"),
            "status": "CONFIRMED" if status == 1 else "REVERTED",
            "logs": receipt.get("logs", []),
        }

    async def get_confirmations(self, network: str, tx_hash: str) -> int:
        receipt = await self._rpc(network, "eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return 0
        latest = int(await self._rpc(network, "eth_blockNumber", []), 16)
        block = int(receipt["blockNumber"], 16)
        return max(0, latest - block + 1)


engine = TransactionEngine()
