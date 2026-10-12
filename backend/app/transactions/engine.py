"""Transaction Engine: construction, gas estimation, nonce coordination, submission.

All value-moving logic lives here, behind deterministic guards that run
INDEPENDENTLY of anything the LLM said. The LLM proposes; this engine disposes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from backend.app.core.config import settings
from backend.app.security.emergency_stop import EmergencyStopError, emergency_stop
from backend.app.blockchain.chains import ChainError, EvmChainAdapter
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


@dataclass(frozen=True)
class SubmissionAuthorization:
    """Everything `submit_raw` requires before it will broadcast.

    There is deliberately no default: a caller cannot broadcast without
    stating the wallet it is authorised for, the cost it is spending, and the
    human who approved it.
    """

    wallet_address: str
    authorized_addresses: frozenset
    estimated_cost_usd: float
    approved_by: str
    approval_id: str
    human_approved: bool = False
    task_id: Optional[str] = None


class TransactionEngine:
    """Validates, builds, and submits transactions within hard limits."""

    def __init__(self, ledger: Optional[SpendLedger] = None) -> None:
        # Spend is persisted and shared across processes; see spend_ledger.py.
        self.ledger = ledger or SpendLedger()

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
        """Advisory, read-only check of the per-transaction and rolling daily ceilings.

        This does NOT reserve anything, so on its own it cannot stop concurrent
        callers from collectively overspending. Anything that actually spends
        must use `reserve_spend`, which checks and reserves atomically.
        """
        if estimated_cost_usd > settings.MAX_TRANSACTION_VALUE_USD:
            raise TransactionRejected(
                f"Estimated transaction cost ${estimated_cost_usd:.2f} exceeds the per-transaction "
                f"limit of ${settings.MAX_TRANSACTION_VALUE_USD:.2f}.",
                "LIMIT_EXCEEDED",
            )
        projected = (await asyncio.to_thread(self.ledger.spent_last_24h)) + estimated_cost_usd
        if projected > settings.MAX_DAILY_GAS_SPEND_USD:
            raise TransactionRejected(
                f"Projected daily spend ${projected:.2f} exceeds the daily limit of "
                f"${settings.MAX_DAILY_GAS_SPEND_USD:.2f}.",
                "DAILY_LIMIT_EXCEEDED",
            )

    async def reserve_spend(self, estimated_cost_usd: float, ref: Optional[str] = None) -> str:
        """Atomically enforce both limits and reserve the cost. Returns a reservation id."""
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
            body = response.json()
        except Exception as exc:
            raise TransactionRejected(f"RPC call '{method}' failed: {exc}", "RPC_FAILURE") from exc

        if "error" in body:
            raise TransactionRejected(
                f"RPC error for {method}: {body['error'].get('message', 'unknown')}", "RPC_ERROR"
            )
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
        except TransactionRejected:
            # Estimation reverts often mean the call would revert on-chain.
            gas_limit = GAS_COST_CATEGORIES.get(intent.category, GAS_COST_CATEGORIES["UNKNOWN"])

        try:
            gas_price = int(await self._rpc(intent.network, "eth_gasPrice", []), 16)
        except TransactionRejected:
            gas_price = 0

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
          2. explicit human approval,
          3. that the transaction was signed by an authorised wallet,
          4. the per-transaction and rolling daily spend limits (atomic reservation),
          5. Emergency Stop again, immediately before the RPC call.
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
                "Broadcast requires an explicit human approval (approved_by and approval_id).",
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
