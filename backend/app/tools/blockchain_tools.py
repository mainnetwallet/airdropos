"""Blockchain Query & Transaction Tools.

Read tools are safe. Value-moving tools declare WALLET_SIGNING or
TRANSACTION_SUBMISSION side effects, are gated by AuthorizationPolicy, and pass
through the deterministic TransactionEngine and KeyVault guards. The LLM cannot
bypass any of those layers by constructing the call itself.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from backend.app.blockchain.chains import known_network_enum
from backend.app.tools.base import BaseTool, SideEffect, ToolResult
from backend.app.transactions.engine import (
    TransactionIntent,
    TransactionRejected,
    engine,
)

logger = logging.getLogger(__name__)

# Every tool's `network` enum comes from the chain catalog. These schemas used to
# repeat the list inline, five times, which meant adding a chain required five
# coordinated edits and a missed one advertised a network the engine could not
# reach.
NETWORK_ENUM = known_network_enum()


class WalletBalanceTool(BaseTool):
    name = "check_wallet_balance"
    version = "1.0.0"
    description = "Read the native-token balance of a public address on a supported EVM network."
    required_permissions = ["blockchain.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 25
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "enum": NETWORK_ENUM},
            "address": {"type": "string", "maxLength": 42},
        },
        "required": ["network", "address"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        if not engine.rpc_url(params["network"]):
            return ToolResult.failure(
                f"No RPC configured for network '{params['network']}'.", category="NO_RPC"
            )
        try:
            balance = await engine.get_balance_wei(params["network"], params["address"])
        except TransactionRejected as exc:
            return ToolResult.failure(str(exc), category=exc.category, retryable=exc.category in ("RPC_FAILURE", "NETWORK_ERROR"))

        return ToolResult.success(
            data={
                "network": params["network"],
                "address": params["address"],
                "balance_wei": balance,
                "balance_native": balance / 1e18,
                "note": "Native token only. Token balances require a token contract query.",
            }
        )


class EstimateGasTool(BaseTool):
    name = "estimate_transaction_cost"
    version = "1.0.0"
    description = "Estimate gas and cost for a proposed transaction without submitting it. Does not require signing."
    required_permissions = ["blockchain.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "enum": NETWORK_ENUM},
            "from_address": {"type": "string", "maxLength": 42},
            "to_address": {"type": "string", "maxLength": 42},
            "value_wei": {"type": "integer", "minimum": 0, "description": "Value in wei"},
            "data": {"type": "string", "description": "Calldata hex", "maxLength": 200000},
            "category": {"type": "string", "enum": ["SWAP", "BRIDGE_DEPOSIT", "CLAIM", "APPROVE", "NFT_MINT", "TRANSFER", "UNKNOWN"]},
        },
        "required": ["network", "from_address", "to_address"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        intent = TransactionIntent(
            network=params["network"],
            chain_id=0,
            wallet_address=params["from_address"],
            to_address=params["to_address"],
            value_wei=params.get("value_wei", 0),
            data=params.get("data", "0x"),
            category=params.get("category", "UNKNOWN"),
        )
        try:
            engine.validate_intent(intent)
            estimate = await engine.estimate_gas(intent)
        except TransactionRejected as exc:
            return ToolResult.failure(str(exc), category=exc.category)

        simulation = await engine.simulate(intent)
        return ToolResult.success(data={**estimate, "simulation": simulation})


class SimulateTransactionTool(BaseTool):
    name = "simulate_transaction"
    version = "1.0.0"
    description = "Run an eth_call simulation to predict whether a transaction would revert. Read-only."
    required_permissions = ["blockchain.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 25
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "enum": NETWORK_ENUM},
            "from_address": {"type": "string", "maxLength": 42},
            "to_address": {"type": "string", "maxLength": 42},
            "value_wei": {"type": "integer", "minimum": 0},
            "data": {"type": "string", "maxLength": 200000},
        },
        "required": ["network", "from_address", "to_address"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        intent = TransactionIntent(
            network=params["network"],
            chain_id=0,
            wallet_address=params["from_address"],
            to_address=params["to_address"],
            value_wei=params.get("value_wei", 0),
            data=params.get("data", "0x"),
        )
        try:
            engine.validate_intent(intent)
            simulation = await engine.simulate(intent)
        except TransactionRejected as exc:
            return ToolResult.failure(str(exc), category=exc.category)

        # A reverting simulation is a legitimate, informative answer, not a tool failure.
        return ToolResult.success(
            data=simulation,
            evidence={"authoritative": True, "kind": "eth_call_simulation"},
        )


class CheckReceiptTool(BaseTool):
    name = "check_transaction_receipt"
    version = "1.0.0"
    description = "Fetch a transaction receipt and confirmation count. Returns pending if not yet mined."
    required_permissions = ["blockchain.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 25
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "enum": NETWORK_ENUM},
            "tx_hash": {"type": "string", "maxLength": 128},
        },
        "required": ["network", "tx_hash"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        try:
            receipt = await engine.get_receipt(params["network"], params["tx_hash"])
            confirmations = await engine.get_confirmations(params["network"], params["tx_hash"])
        except TransactionRejected as exc:
            return ToolResult.failure(str(exc), category=exc.category, retryable=True)

        if receipt is None:
            # Pending is a real, distinct state. It is never reported as success.
            return ToolResult.success(
                data={
                    "tx_hash": params["tx_hash"],
                    "status": "PENDING",
                    "confirmations": 0,
                    "note": "Not yet mined. Submission alone does not prove execution.",
                }
            )

        receipt["confirmations"] = confirmations
        return ToolResult.success(
            data=receipt,
            evidence={
                "authoritative": receipt.get("status") == "CONFIRMED",
                "kind": "chain_receipt",
                "block_number": receipt.get("block_number"),
            },
        )


class GetPendingNonceTool(BaseTool):
    name = "get_pending_nonce"
    version = "1.0.0"
    description = "Get the next safe nonce for an address, accounting for in-flight transactions to prevent conflicts."
    required_permissions = ["blockchain.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 20
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "enum": NETWORK_ENUM},
            "address": {"type": "string", "maxLength": 42},
        },
        "required": ["network", "address"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        try:
            nonce = await engine.get_nonce(params["network"], params["address"])
        except TransactionRejected as exc:
            return ToolResult.failure(str(exc), category=exc.category, retryable=True)
        return ToolResult.success(data={"network": params["network"], "address": params["address"], "next_nonce": nonce})


def register_blockchain_tools(registry) -> None:
    registry.register_or_replace_many(
        [
            WalletBalanceTool(),
            EstimateGasTool(),
            SimulateTransactionTool(),
            CheckReceiptTool(),
            GetPendingNonceTool(),
        ]
    )
