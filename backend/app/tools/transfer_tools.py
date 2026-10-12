"""Value-moving tools: quote, approve, sign and broadcast.

The flow is split so that no single call can move money by accident:

1. ``prepare_native_transfer`` / ``prepare_token_transfer`` - read-only. Resolve the
   wallet, validate the destination, read the real balance, estimate the real fee,
   simulate the call, and return a fully specified proposal. Nothing is signed and
   nothing is sent. Side effect: external read.
2. ``approve_transfer`` - records a human's approval of one specific, fully quoted
   transaction, bound to exactly the fields they were shown.
3. ``submit_prepared_transfer`` - the only tool that signs and broadcasts. It runs
   only when the policy permits signing and submission, the emergency stop is clear,
   a real unexpired server-side approval exists for this transaction, the vault holds
   usable key material, and the bytes about to be broadcast decode to exactly what
   was approved.

Every refusal is reported as a refusal. A missing RPC key, a locked wallet, an absent
approval or an engaged emergency stop produces a specific, actionable result - never a
success, and never a fabricated transaction hash.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from backend.app.blockchain.chains import ChainError, get_chain
from backend.app.blockchain.tokens import (
    TokenError,
    encode_erc20_transfer,
    format_units,
    known_tokens,
    parse_units,
    read_token_balance,
    read_token_metadata,
    resolve_token,
)
from backend.app.database.session import session_scope
from backend.app.models import Wallet as WalletRow
from backend.app.security.autonomous_policy import get_policy, is_policy_approver
from backend.app.security.emergency_stop import emergency_stop
from backend.app.tools.base import BaseTool, SideEffect, ToolResult, ToolResultStatus
from backend.app.tools.wallet_tools import resolve_wallet
from backend.app.transactions.approvals import ApprovalService, intent_fingerprint
from backend.app.transactions.engine import (
    SubmissionAuthorization,
    TransactionIntent,
    TransactionRejected,
    decode_raw_transaction,
    engine,
)
from backend.app.wallets.service import WalletError, WalletService
from backend.app.wallets.vault import DecryptionError, SigningDenied, SigningRequest, vault

logger = logging.getLogger(__name__)

NATIVE_DECIMALS = 18


@dataclass(frozen=True)
class TransferProposal:
    """A fully specified, not-yet-signed transfer awaiting human approval."""

    proposal_id: str
    kind: str  # native | erc20
    network: str
    chain_id: int
    wallet_id: str
    wallet_label: str
    wallet_address: str
    to_address: str
    asset: str
    amount: str
    amount_wei: str
    value_wei: str
    data: str
    gas_limit: int
    gas_price_gwei: float
    estimated_fee_wei: str
    estimated_fee_native: str
    simulation: Dict[str, Any]
    native_balance_wei: str
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def transfer_category(binding: Dict[str, Any]) -> str:
    """Which value-moving category a binding belongs to.

    An ERC-20 transfer carries calldata and sends zero native coin; a native transfer
    does the opposite. Getting this wrong would let a rule written for one authorise
    the other, so it is derived from the binding rather than supplied.
    """
    from backend.app.security.autonomous_policy import Category

    return (
        Category.TX_TOKEN_TRANSFER
        if str(binding.get("data") or "0x") not in ("", "0x")
        else Category.TX_NATIVE_TRANSFER
    )


def _native_amount(binding: Dict[str, Any]) -> Optional[str]:
    """The amount as a decimal native-coin string, for a policy ceiling to compare.

    ERC-20 transfers carry their amount in calldata, so there is no native amount for
    a rule to bound; those are governed by `allowed_tokens` and `max_value_usd`
    instead. Returning None is correct, not a gap.
    """
    if transfer_category(binding) != "tx.native_transfer":
        return None
    try:
        return format_units(int(binding.get("value_wei") or 0), NATIVE_DECIMALS)
    except (TypeError, ValueError):
        return None


def _approval_fields(
    *,
    network: str,
    chain_id: int,
    wallet_address: str,
    to_address: str,
    value_wei: int,
    data: str,
    gas_limit: int,
) -> Dict[str, Any]:
    """The exact fields an operator is shown, and the approval is bound to.

    Deliberately excludes the gas price and any cost figure: those are estimates
    that drift between quoting and broadcasting, and binding an approval to them
    would make a correct transfer impossible whenever the network moved. What is
    bound is everything that determines where the money goes - chain, source,
    destination, amount and calldata.
    """
    return {
        "network": str(network).lower(),
        "chain_id": int(chain_id),
        "wallet": str(wallet_address).lower(),
        "to": str(to_address).lower(),
        "value_wei": str(int(value_wei)),
        "data": str(data),
        "gas_limit": str(int(gas_limit)),
    }


def _funding_problem(value_wei: int, fee_wei: int, balance_wei: int) -> Optional[str]:
    """Can the wallet pay the transfer AND the fee? Both, or it reverts on-chain."""
    required = value_wei + fee_wei
    if balance_wei >= required:
        return None
    return (
        f"Insufficient funds. The wallet holds {format_units(balance_wei, NATIVE_DECIMALS)} but "
        f"needs {format_units(required, NATIVE_DECIMALS)} "
        f"({format_units(value_wei, NATIVE_DECIMALS)} to send plus "
        f"{format_units(fee_wei, NATIVE_DECIMALS)} in fees), which is "
        f"{format_units(required - balance_wei, NATIVE_DECIMALS)} short."
    )


async def _resolve_signer(wallet_id, label, address, network):
    """Resolve the sending wallet and confirm it can actually sign."""
    async with session_scope() as db:
        service = WalletService(db)
        row, reason = await resolve_wallet(
            db, wallet_id=wallet_id, label=label, address=address, network=network
        )
        if row is None:
            return None, reason
        if not service.can_sign(row):
            if not row.encrypted_secret:
                detail = (
                    "it is watch-only: it holds a public address but no key material, so "
                    "AirdropOS cannot sign for it"
                )
            elif row.is_locked:
                detail = "it is locked"
            elif not row.is_active:
                detail = "it is deactivated"
            else:
                detail = "the vault holds no usable key material for it"
            return None, f"Wallet '{row.label}' cannot sign: {detail}."
        return {
            "wallet_id": str(row.id),
            "label": row.label,
            "address": row.address,
            "encrypted_secret": row.encrypted_secret,
            "encryption_iv": row.encryption_iv,
            "encryption_tag": row.encryption_tag,
        }, None


def _guard_network(network: str):
    try:
        chain = get_chain(network)
    except ChainError as exc:
        return None, ToolResult.failure(str(exc), category="UNKNOWN_NETWORK")
    if not engine.rpc_url(chain.name):
        return None, ToolResult.failure(
            f"No RPC endpoint is configured for '{chain.name}', so no balance can be read and "
            f"no fee can be quoted. Set the RPC setting for that network first.",
            category="NO_RPC",
        )
    return chain, None


def _not_retriable(category: str) -> bool:
    return category in ("RPC_AUTH", "NO_RPC", "INVALID_ADDRESS", "INVALID_VALUE", "DENYLISTED")


class PrepareNativeTransferTool(BaseTool):
    name = "prepare_native_transfer"
    version = "1.0.0"
    description = (
        "Prepare (do NOT send) a transfer of the chain's native coin, e.g. ETH. Reads the real "
        "balance, estimates the real fee, simulates the call, and returns every detail to show "
        "the operator for approval. Signs and sends nothing."
    )
    required_permissions = ["wallet.read", "blockchain.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 45
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "maxLength": 50},
            "to_address": {"type": "string", "maxLength": 42},
            "amount": {
                "type": "string",
                "maxLength": 40,
                "description": "Amount in the native coin as a decimal string, e.g. '0.01'",
            },
            "wallet_id": {"type": "string", "maxLength": 64},
            "label": {"type": "string", "maxLength": 255},
        },
        "required": ["network", "to_address", "amount"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        chain, failure = _guard_network(params["network"])
        if failure is not None:
            return failure

        try:
            value_wei = parse_units(params["amount"], NATIVE_DECIMALS)
        except TokenError as exc:
            return ToolResult.failure(str(exc), category=exc.category)
        if value_wei <= 0:
            return ToolResult.failure(
                f"Amount must be greater than zero (got {params['amount']!r}).",
                category="INVALID_VALUE",
            )

        wallet, reason = await _resolve_signer(
            params.get("wallet_id"), params.get("label"), None, chain.name
        )
        if wallet is None:
            return ToolResult.needs_user(reason or "Which wallet should send this?")

        intent = TransactionIntent(
            network=chain.name,
            chain_id=chain.chain_id,
            wallet_address=wallet["address"],
            to_address=params["to_address"],
            value_wei=value_wei,
            data="0x",
            category="TRANSFER",
        )
        try:
            engine.validate_intent(intent)
            balance_wei = await engine.get_balance_wei(chain.name, wallet["address"])
            estimate = await engine.estimate_gas(intent)
            simulation = await engine.simulate(intent)
        except TransactionRejected as exc:
            return ToolResult.failure(
                str(exc), category=exc.category, retryable=not _not_retriable(exc.category)
            )

        fee_wei = int(estimate["estimated_cost_wei"])
        problem = _funding_problem(value_wei, fee_wei, balance_wei)
        if problem:
            return ToolResult.failure(problem, category="INSUFFICIENT_BALANCE")

        warnings = [
            "Prepared only. Nothing has been signed or sent.",
            "Explicit operator approval is required before this can be signed or broadcast.",
        ]
        if simulation.get("will_revert"):
            warnings.append(
                "The dry run reverted; submitting this would most likely fail on-chain."
            )

        proposal = TransferProposal(
            proposal_id=_proposal_id(),
            kind="native",
            network=chain.name,
            chain_id=chain.chain_id,
            wallet_id=wallet["wallet_id"],
            wallet_label=wallet["label"],
            wallet_address=wallet["address"],
            to_address=params["to_address"].lower(),
            asset=chain.native_symbol,
            amount=str(params["amount"]),
            amount_wei=str(value_wei),
            value_wei=str(value_wei),
            data="0x",
            gas_limit=int(estimate["gas_limit"]),
            gas_price_gwei=float(estimate["gas_price_gwei"]),
            estimated_fee_wei=str(fee_wei),
            estimated_fee_native=format_units(fee_wei, NATIVE_DECIMALS),
            simulation=simulation,
            native_balance_wei=str(balance_wei),
            warnings=warnings,
        ).to_dict()

        proposal["approval_fields"] = _approval_fields(
            network=chain.name,
            chain_id=chain.chain_id,
            wallet_address=wallet["address"],
            to_address=proposal["to_address"],
            value_wei=value_wei,
            data="0x",
            gas_limit=int(estimate["gas_limit"]),
        )

        return ToolResult.success(
            data={
                "proposal": proposal,
                "approval": {
                    "required": True,
                    "recorded": False,
                    "next_step": (
                        "Show these exact details to the operator and, only after they confirm, "
                        "call approve_transfer with the approval_fields and their identity."
                    ),
                },
            },
            evidence={
                "authoritative": True,
                "kind": "prepared_transfer",
                "proposal_id": proposal["proposal_id"],
                "network": chain.name,
                "simulated": not simulation.get("will_revert"),
            },
        )


class PrepareTokenTransferTool(BaseTool):
    name = "prepare_token_transfer"
    version = "1.0.0"
    description = (
        "Prepare (do NOT send) an ERC-20 token transfer, e.g. 100 USDC. Resolves the token "
        "contract for that network, reads decimals and balance from the contract itself, "
        "computes the exact amount, estimates the fee and simulates the call. Signs and sends "
        "nothing. A symbol is never resolved by searching; an unknown symbol is refused."
    )
    required_permissions = ["wallet.read", "blockchain.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 45
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "maxLength": 50},
            "to_address": {"type": "string", "maxLength": 42},
            "amount": {"type": "string", "maxLength": 40, "description": "Decimal string, e.g. '100'"},
            "symbol": {"type": "string", "maxLength": 20, "description": "e.g. USDC"},
            "contract_address": {
                "type": "string",
                "maxLength": 42,
                "description": "Exact ERC-20 contract, required when the symbol is not known",
            },
            "wallet_id": {"type": "string", "maxLength": 64},
            "label": {"type": "string", "maxLength": 255},
        },
        "required": ["network", "to_address", "amount"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        chain, failure = _guard_network(params["network"])
        if failure is not None:
            return failure

        try:
            token = resolve_token(
                chain.name,
                symbol=params.get("symbol"),
                address=params.get("contract_address"),
            )
        except TokenError as exc:
            data = (
                {"known_tokens_on_network": known_tokens(chain.name)}
                if exc.category == "TOKEN_UNKNOWN"
                else None
            )
            return ToolResult.failure(str(exc), category=exc.category, data=data or {})

        wallet, reason = await _resolve_signer(
            params.get("wallet_id"), params.get("label"), None, chain.name
        )
        if wallet is None:
            return ToolResult.needs_user(reason or "Which wallet should send this?")

        try:
            meta = await read_token_metadata(engine, chain.name, token)
            decimals = int(meta["decimals"])
            balance_text = await read_token_balance(
                engine, chain.name, wallet["address"], token, decimals
            )
            amount_wei = parse_units(params["amount"], decimals)
            if amount_wei <= 0:
                raise TokenError(
                    f"Amount must be greater than zero (got {params['amount']!r}).",
                    category="INVALID_VALUE",
                )
            data_field = encode_erc20_transfer(params["to_address"], amount_wei)
        except TokenError as exc:
            return ToolResult.failure(str(exc), category=exc.category)
        except TransactionRejected as exc:
            return ToolResult.failure(
                str(exc), category=exc.category, retryable=not _not_retriable(exc.category)
            )

        intent = TransactionIntent(
            network=chain.name,
            chain_id=chain.chain_id,
            wallet_address=wallet["address"],
            to_address=token.address,
            value_wei=0,  # ERC-20 value lives in calldata; no native coin moves
            data=data_field,
            category="TRANSFER",
        )
        try:
            engine.validate_intent(intent)
            estimate = await engine.estimate_gas(intent)
            simulation = await engine.simulate(intent)
            native_balance = await engine.get_balance_wei(chain.name, wallet["address"])
        except TransactionRejected as exc:
            return ToolResult.failure(
                str(exc), category=exc.category, retryable=not _not_retriable(exc.category)
            )

        fee_wei = int(estimate["estimated_cost_wei"])
        # An ERC-20 transfer moves no native coin, so the gas balance must cover the
        # fee alone; the token balance is checked separately below.
        problem = _funding_problem(0, fee_wei, native_balance)
        if problem:
            return ToolResult.failure(
                f"{problem} The wallet cannot pay the gas for a {token.symbol} transfer.",
                category="INSUFFICIENT_BALANCE",
            )

        token_balance_base = parse_units(balance_text, decimals)
        if amount_wei > token_balance_base:
            return ToolResult.failure(
                f"Insufficient {token.symbol}. The wallet holds {balance_text} {token.symbol} "
                f"but is sending {params['amount']}.",
                category="INSUFFICIENT_TOKEN_BALANCE",
            )

        warnings = [
            "Prepared only. Nothing has been signed or sent.",
            "Explicit operator approval is required before this can be signed or broadcast.",
        ]
        if meta.get("decimals_mismatch"):
            warnings.append(
                f"The catalog recorded {meta['catalog_decimals']} decimals but the contract "
                f"reports {meta['decimals']}. The contract's value was used."
            )
        if simulation.get("will_revert"):
            warnings.append(
                "The dry run reverted; submitting this would most likely fail on-chain."
            )

        proposal = TransferProposal(
            proposal_id=_proposal_id(),
            kind="erc20",
            network=chain.name,
            chain_id=chain.chain_id,
            wallet_id=wallet["wallet_id"],
            wallet_label=wallet["label"],
            wallet_address=wallet["address"],
            to_address=params["to_address"].lower(),
            asset=token.symbol,
            amount=str(params["amount"]),
            amount_wei=str(amount_wei),
            value_wei="0",
            data=data_field,
            gas_limit=int(estimate["gas_limit"]),
            gas_price_gwei=float(estimate["gas_price_gwei"]),
            estimated_fee_wei=str(fee_wei),
            estimated_fee_native=format_units(fee_wei, NATIVE_DECIMALS),
            simulation=simulation,
            native_balance_wei=str(native_balance),
            warnings=warnings,
        ).to_dict()

        proposal["approval_fields"] = _approval_fields(
            network=chain.name,
            chain_id=chain.chain_id,
            wallet_address=wallet["address"],
            to_address=token.address,
            value_wei=0,
            data=data_field,
            gas_limit=int(estimate["gas_limit"]),
        )

        return ToolResult.success(
            data={
                "proposal": proposal,
                "token": {
                    "symbol": token.symbol,
                    "contract": token.address,
                    "decimals": decimals,
                    "symbol_read_onchain": meta.get("symbol_onchain"),
                    "balance": balance_text,
                    "resolved_from": (
                        "operator-supplied contract"
                        if token.name and token.name.startswith("operator-supplied")
                        else "built-in token catalog"
                    ),
                },
                "approval": {
                    "required": True,
                    "recorded": False,
                    "next_step": (
                        "Show these exact details to the operator and, only after they confirm, "
                        "call approve_transfer with the approval_fields and their identity."
                    ),
                },
            },
            evidence={
                "authoritative": True,
                "kind": "prepared_transfer",
                "proposal_id": proposal["proposal_id"],
                "network": chain.name,
                "token_contract": token.address,
                "simulated": not simulation.get("will_revert"),
            },
        )


def _proposal_id() -> str:
    import secrets

    return secrets.token_urlsafe(12)[:24]


class ApproveTransferTool(BaseTool):
    name = "approve_transfer"
    version = "1.1.0"
    description = (
        "Authorize one specific prepared transfer. Pass approved_by set to the operator's own "
        "identity when they personally confirmed the details. When the autonomous execution "
        "policy already covers this exact transfer, omit approved_by and the policy is "
        "consulted instead - the policy is the authorization, not the model. Never invent an "
        "identity."
    )
    required_permissions = ["transaction.approve"]
    side_effect = SideEffect.STATE_MUTATION
    timeout_seconds = 25
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "maxLength": 50},
            "chain_id": {"type": "integer"},
            "wallet_address": {"type": "string", "maxLength": 42},
            "to_address": {"type": "string", "maxLength": 42},
            "value_wei": {"type": "string", "maxLength": 78},
            "data": {"type": "string", "maxLength": 200000},
            "gas_limit": {"type": "string", "maxLength": 40},
            "proposal_id": {"type": "string", "maxLength": 64},
            "approved_by": {
                "type": "string",
                "maxLength": 128,
                "description": (
                    "The human who approved it. Omit to use the configured autonomous policy, "
                    "which is consulted for this exact transfer. Must not be 'agent' or 'system'."
                ),
            },
            "summary": {"type": "string", "maxLength": 2000},
        },
        "required": ["network", "wallet_address", "to_address"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from backend.app.security.autonomous_policy import (
            Category,
            get_policy,
            policy_approver_identity,
        )

        if emergency_stop.is_stopped:
            return ToolResult.failure(
                "Emergency stop is active. Approvals are not being recorded while execution "
                "is halted.",
                category="EMERGENCY_STOP",
            )

        try:
            binding = _approval_fields(
                network=params["network"],
                chain_id=int(params.get("chain_id") or 0),
                wallet_address=params["wallet_address"],
                to_address=params["to_address"],
                value_wei=int(params.get("value_wei") or 0),
                data=params.get("data") or "0x",
                gas_limit=int(params.get("gas_limit") or 0),
            )
        except (TypeError, ValueError) as exc:
            return ToolResult.failure(
                f"The approval fields are not valid integers: {exc}", category="INVALID_INPUT"
            )

        identity = str(params.get("approved_by") or "").strip()

        if not identity:
            # No human identity supplied: ask the policy. This is the autonomous path,
            # and the decision comes from configuration, not from the caller.
            category = transfer_category(binding)
            decision = get_policy().evaluate(
                category,
                network=binding["network"],
                wallet=binding["wallet"],
                destination=binding["to"],
                value_native=_native_amount(binding),
            )
            if not decision.allowed:
                return ToolResult.needs_user(
                    f"{decision.reason} Show these details to the operator and record their "
                    f"approval with approve_transfer(approved_by=<their identity>) to proceed."
                )
            identity = policy_approver_identity(decision.rule_id or category)

        async with session_scope() as db:
            try:
                row, was_new = await ApprovalService(db).find_or_create(
                    approved_by=identity,
                    binding=binding,
                    summary=params.get("summary") or "",
                    proposal_id=params.get("proposal_id"),
                    idempotency_key=intent_fingerprint(binding),
                )
            except Exception as exc:
                return ToolResult.failure(str(exc), category="APPROVAL_INVALID")
            approval_id = row.approval_id

        policy_authorised = identity.startswith("policy:autonomous")
        note = (
            "Authorized by the configured autonomous policy. It expires shortly and covers "
            "exactly this one transaction."
            if policy_authorised
            else (
                "Approval recorded. It expires shortly and authorises exactly this one "
                "transaction; it cannot be reused for a different transfer."
            )
        )
        if not was_new:
            note += (
                " This exact transfer already has an approval, so no second one was created "
                "and the transfer cannot be broadcast twice."
            )

        return ToolResult.success(
            data={
                "approval_id": approval_id,
                "binding": binding,
                "authorized_by": identity,
                "authorized_by_policy": policy_authorised,
                "new_approval": was_new,
                "note": note,
            },
            evidence={
                "authoritative": True,
                "kind": "record_read",
                "entity": "transaction_approval",
                "approval_id": approval_id,
            },
        )


class SubmitPreparedTransferTool(BaseTool):
    name = "submit_prepared_transfer"
    version = "1.0.0"
    description = (
        "Sign and broadcast one approved transfer, then wait for and verify its receipt. "
        "Refuses to run without a valid server-side approval bound to this exact transaction. "
        "Returns the real transaction hash, and does not report success until a receipt "
        "confirms it."
    )
    required_permissions = ["wallet.sign", "transaction.submit"]
    side_effect = SideEffect.TRANSACTION_SUBMISSION
    timeout_seconds = 120
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "maxLength": 50},
            "approval_id": {"type": "string", "maxLength": 64},
            "approved_by": {
                "type": "string",
                "maxLength": 128,
                "description": (
                    "Optional. The identity the approval was issued to. Omit it to use the "
                    "identity already recorded on the approval."
                ),
            },
            "wait_for_receipt": {"type": "boolean"},
        },
        "required": ["network", "approval_id"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        try:
            chain = get_chain(params["network"])
        except ChainError as exc:
            return ToolResult.failure(str(exc), category="UNKNOWN_NETWORK")

        if not emergency_stop.can_sign() or not emergency_stop.can_submit_transactions():
            return ToolResult.failure(
                "Emergency stop is active; nothing will be signed or broadcast.",
                category="EMERGENCY_STOP",
            )

        async with session_scope() as db:
            approvals = ApprovalService(db)
            approval = await approvals.get(str(params["approval_id"]))
            if approval is None:
                return ToolResult.failure(
                    f"No approval with id '{params['approval_id']}'. Prepare the transfer and "
                    f"ask the operator to approve it first.",
                    category="APPROVAL_UNKNOWN",
                )
            ok, reason = ApprovalService.evaluate(approval)
            if not ok:
                return ToolResult.failure(reason, category="APPROVAL_INVALID")
            # `approved_by` is optional. When supplied it must match the approval; when
            # omitted the approval's own identity is used. Either way the value comes
            # from the stored row, so the caller cannot assert someone else's approval.
            claimed = str(params.get("approved_by") or "").strip()
            if claimed and str(approval.approved_by).strip().lower() != claimed.lower():
                return ToolResult.failure(
                    "That approval was issued to a different identity than the one named here.",
                    category="APPROVAL_INVALID",
                )
            binding = dict(approval.binding or {})
            approved_by = str(approval.approved_by)

            # An autonomous approval is re-authorized against the live policy here,
            # immediately before signing, so a rule narrowed since approval stops it.
            if is_policy_approver(approved_by):
                category = transfer_category(binding)
                decision = get_policy().evaluate(
                    category,
                    network=binding.get("network"),
                    wallet=binding.get("wallet"),
                    destination=binding.get("to"),
                    value_native=_native_amount(binding),
                )
                if not decision.allowed:
                    return ToolResult.failure(
                        f"The autonomous policy no longer authorizes this transfer: "
                        f"{decision.reason}",
                        category="POLICY_DENIED",
                    )
            wallet_address = str(approval.wallet_address)

            if binding.get("network") != chain.name:
                return ToolResult.failure(
                    f"This approval was issued for '{binding.get('network')}', not "
                    f"'{chain.name}'.",
                    category="APPROVAL_CHAIN_MISMATCH",
                )

            wallet_row = await _load_signer(db, wallet_address)
            if wallet_row is None:
                return ToolResult.failure(
                    f"No wallet is registered at {wallet_address}, so nothing can sign.",
                    category="WALLET_UNKNOWN",
                )
            service = WalletService(db)
            if not service.can_sign(wallet_row):
                return ToolResult.failure(
                    f"Wallet '{wallet_row.label}' cannot sign right now (locked, inactive, or "
                    f"no usable key material).",
                    category="WALLET_LOCKED",
                )
            material = (
                wallet_row.encrypted_secret,
                wallet_row.encryption_iv,
                wallet_row.encryption_tag,
            )
            wallet_label = wallet_row.label

        # A repeat of an already-sent transfer must not send again. The hash is
        # recorded at broadcast time (before any receipt wait), so this check is
        # accurate even if the previous attempt never got to confirm.
        async with session_scope() as db:
            prior_hash = await ApprovalService(db).already_broadcast(binding)
        if prior_hash:
            return ToolResult.success(
                data={
                    "tx_hash": prior_hash,
                    "network": chain.name,
                    "wallet_address": wallet_address,
                    "to": binding.get("to"),
                    "value_wei": binding.get("value_wei"),
                    "status": "ALREADY_BROADCAST",
                    "resent": False,
                    "note": (
                        "This exact transfer was already broadcast as "
                        f"{prior_hash}. Nothing was signed or sent again."
                    ),
                },
                evidence={
                    "authoritative": False,
                    "kind": "chain_receipt",
                    "tx_hash": prior_hash,
                    "status": "ALREADY_BROADCAST",
                },
            )

        try:
            raw_tx = await _sign(binding, chain, wallet_address, material)
        except (SigningDenied, DecryptionError) as exc:
            return ToolResult.failure(str(exc), category="SIGNING_DENIED")
        except TransactionRejected as exc:
            return ToolResult.failure(str(exc), category=exc.category)
        except Exception as exc:
            return ToolResult.failure(
                f"Signing failed: {type(exc).__name__}: {exc}", category="SIGNING_FAILED"
            )

        # The signed bytes must do exactly what was approved. The engine re-checks
        # this too, but a mismatch is worth naming precisely rather than letting a
        # generic refusal surface.
        mismatch = _assert_matches_approval(raw_tx, chain, binding, wallet_address)
        if mismatch:
            return ToolResult.failure(mismatch, category="APPROVAL_BINDING_MISMATCH")

        try:
            tx_hash = await engine.submit_raw(
                chain.name,
                raw_tx,
                authorization=SubmissionAuthorization(
                    wallet_address=wallet_address,
                    authorized_addresses=frozenset({wallet_address}),
                    estimated_cost_usd=0.0,  # never invented; the USD feed is external
                    approved_by=approved_by,
                    approval_id=str(params["approval_id"]),
                    human_approved=True,
                    allowed_destinations=frozenset({str(binding.get("to", "")).lower()}),
                ),
            )
        except Exception as exc:
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                category=getattr(exc, "category", "BROADCAST_FAILED"),
            )

        # Persist before waiting on the receipt: if the process dies now, the hash is
        # still on disk and a retry reports it instead of sending a second transfer.
        try:
            async with session_scope() as db:
                await ApprovalService(db).record_broadcast(
                    str(params["approval_id"]), tx_hash, "SUBMITTED"
                )
        except Exception as exc:
            logger.error("Could not record broadcast hash for approval %s: %s",
                         params["approval_id"], exc)

        result: Dict[str, Any] = {
            "tx_hash": tx_hash,
            "network": chain.name,
            "chain_id": chain.chain_id,
            "wallet_address": wallet_address,
            "wallet_label": wallet_label,
            "to": binding.get("to"),
            "value_wei": binding.get("value_wei"),
            "status": "SUBMITTED",
            "note": "The node accepted the broadcast. That is not yet proof it succeeded.",
        }

        if not params.get("wait_for_receipt", True):
            result["receipt"] = {"status": "NOT_CHECKED"}
            return ToolResult.success(
                data=result,
                evidence={
                    "authoritative": False,
                    "kind": "chain_receipt",
                    "tx_hash": tx_hash,
                    "status": "SUBMITTED",
                },
            )

        receipt = await _await_receipt(chain.name, tx_hash)
        result["receipt"] = receipt
        result["status"] = receipt.get("status", "PENDING")

        if result["status"] == "CONFIRMED":
            return ToolResult.success(
                data=result,
                evidence={
                    "authoritative": True,
                    "kind": "chain_receipt",
                    "tx_hash": tx_hash,
                    "block_number": receipt.get("block_number"),
                    "status": "CONFIRMED",
                },
            )
        if result["status"] == "PENDING":
            return ToolResult(
                status=ToolResultStatus.SUCCESS,
                data=result,
                error="Broadcast, but not mined yet. Submission alone does not prove success.",
                error_category="TX_PENDING",
                evidence={"authoritative": False, "kind": "chain_receipt", "tx_hash": tx_hash},
            )
        return ToolResult(
            status=ToolResultStatus.FAILURE,
            data=result,
            error="The transaction was broadcast but its receipt reports it reverted.",
            error_category="TX_REVERTED",
            evidence={
                "authoritative": True,
                "kind": "chain_receipt",
                "tx_hash": tx_hash,
                "status": "REVERTED",
            },
        )


async def _load_signer(db, address: str):
    from sqlalchemy import select

    return await db.scalar(
        select(WalletRow).where(WalletRow.address == str(address).strip().lower())
    )


def _checksum(address: Optional[str]) -> Optional[str]:
    """EIP-55 form, which `eth_account` requires for `to`.

    The binding case-folds addresses on purpose so approvals compare equal regardless
    of checksum case. `sign_transaction` does the opposite and rejects anything that
    is not in checksum form, so the address is converted here rather than at the point
    of comparison.
    """
    if not address:
        return None
    from eth_utils import to_checksum_address

    try:
        return to_checksum_address(str(address))
    except Exception:
        # Leave it alone: the vault's own validation will reject a malformed address
        # with a message about the address, which is more useful than one about
        # checksumming.
        return str(address)


async def _sign(binding: Dict[str, Any], chain, wallet_address: str, material) -> str:
    """Sign the approved transaction locally. Nothing is broadcast here."""
    nonce = await engine.get_nonce(chain.name, wallet_address)
    gas_limit = int(binding.get("gas_limit") or 0)
    gas_price_wei = await engine._rpc(chain.name, "eth_gasPrice", [])
    gas_price = int(gas_price_wei, 16)
    if gas_limit <= 0:
        gas_limit = 21_000 if not (binding.get("data") or "0x") or binding["data"] == "0x" else 90_000

    result = vault.sign_transaction(
        SigningRequest(
            wallet_address=_checksum(wallet_address),
            chain_id=chain.chain_id,
            transaction={
                "to": _checksum(binding.get("to")),
                "value": int(binding.get("value_wei") or 0),
                "data": binding.get("data") or "0x",
                "gas": gas_limit,
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": chain.chain_id,
            },
        ),
        material[0],
        material[1],
        material[2],
        authorized_addresses={wallet_address},
    )
    raw = str((result.signed_transaction or {}).get("raw") or "")
    return raw if raw.startswith("0x") else "0x" + raw


def _assert_matches_approval(
    raw_tx: str, chain, binding: Dict[str, Any], wallet_address: str
) -> Optional[str]:
    """Compare the signed bytes with the approved intent. Returns a reason, or None."""
    try:
        decoded = decode_raw_transaction(raw_tx)
    except TransactionRejected as exc:
        return f"The signed transaction could not be decoded, so it was not broadcast: {exc}"

    if decoded.chain_id != chain.chain_id:
        return (
            f"The signed transaction is for chain {decoded.chain_id}, but '{chain.name}' is "
            f"chain {chain.chain_id}. Refusing to broadcast."
        )
    if decoded.to is None or decoded.to.lower() != str(binding.get("to", "")).lower():
        return (
            f"The signed transaction goes to {decoded.to}, which is not the approved "
            f"destination {binding.get('to')}. Refusing to broadcast."
        )
    if decoded.value != int(binding.get("value_wei") or 0):
        return (
            f"The signed transaction moves {decoded.value} wei, but {binding.get('value_wei')} "
            f"wei was approved. Refusing to broadcast."
        )
    approved_data = str(binding.get("data") or "0x").lower()
    if ("0x" + decoded.data.hex()).lower() != approved_data:
        return (
            "The signed transaction's calldata does not match the approved calldata. "
            "Refusing to broadcast."
        )
    return None


async def _await_receipt(network: str, tx_hash: str, attempts: int = 4) -> Dict[str, Any]:
    """Poll for a receipt. 'Still pending' is a real answer, never a success."""
    import asyncio

    for attempt in range(attempts):
        try:
            receipt = await engine.get_receipt(network, tx_hash)
        except TransactionRejected:
            receipt = None
        if receipt is not None:
            try:
                receipt["confirmations"] = await engine.get_confirmations(network, tx_hash)
            except TransactionRejected:
                receipt["confirmations"] = 0
            return receipt
        if attempt < attempts - 1:
            await asyncio.sleep(3)
    return {
        "status": "PENDING",
        "confirmations": 0,
        "note": "Not mined yet. Check again with check_transaction_receipt.",
    }


class ListApprovalsTool(BaseTool):
    name = "list_transfer_approvals"
    version = "1.0.0"
    description = "List recent transfer approvals and whether each is still usable."
    required_permissions = ["transaction.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 20
    input_schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        async with session_scope() as db:
            rows = await ApprovalService(db).list_recent(int(params.get("limit") or 25))
        items = [ApprovalService.to_dict(r) for r in rows]
        return ToolResult.success(
            data={"approvals": items, "count": len(items)},
            evidence={
                "authoritative": True,
                "kind": "record_read",
                "entity": "transaction_approval",
                "count": len(items),
            },
        )


class ListTokensTool(BaseTool):
    name = "list_supported_tokens"
    version = "1.0.0"
    description = (
        "List the ERC-20 contracts this platform is willing to transfer, per network. "
        "A token not listed here cannot be sent by symbol; supply an explicit "
        "contract_address instead."
    )
    required_permissions = ["blockchain.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 15
    input_schema = {
        "type": "object",
        "properties": {"network": {"type": "string", "maxLength": 50}},
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        try:
            tokens = known_tokens(params.get("network"))
        except TokenError as exc:
            return ToolResult.failure(str(exc), category=exc.category)
        return ToolResult.success(
            data={
                "tokens": tokens,
                "count": len(tokens),
                "note": (
                    "Addresses come from the built-in catalog or TOKEN_CONTRACTS configuration. "
                    "A symbol is never resolved by searching, and decimals are read from the "
                    "contract at use time."
                ),
            },
            evidence={"authoritative": True, "kind": "contract_read", "count": len(tokens)},
        )


def register_transfer_tools(registry) -> None:
    registry.register_or_replace_many(
        [
            PrepareNativeTransferTool(),
            PrepareTokenTransferTool(),
            ApproveTransferTool(),
            SubmitPreparedTransferTool(),
            ListApprovalsTool(),
            ListTokensTool(),
        ]
    )