"""Wallet tools: what the operator's own wallets are, and where they live on-chain.

Every one of these calls the real `WalletService` / `WalletRow` records. None of them
can see key material: the service's public projection (`WalletService.to_dict`) has no
field for it, and no tool here decrypts anything. Wallet creation goes through
`WalletService.create_wallet`, so the generated key is encrypted by the vault and the
plaintext never leaves that call.

Wallet access is deny-by-default through `AuthorizationPolicy.allowed_wallet_ids`, so
a tool that names a wallet the operator has not authorised is refused before it runs,
not after.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.app.blockchain.chains import ChainError, get_chain
from backend.app.blockchain.tokens import format_units
from backend.app.database.session import session_scope
from backend.app.models import Wallet
from backend.app.tools.base import BaseTool, SideEffect, ToolResult
from backend.app.wallets.service import WalletError, WalletService, validate_address
from backend.app.wallets.vault import DecryptionError

logger = logging.getLogger(__name__)


async def resolve_wallet(
    db,
    *,
    wallet_id: Optional[str] = None,
    label: Optional[str] = None,
    address: Optional[str] = None,
    network: Optional[str] = None,
) -> tuple[Optional[Wallet], Optional[str]]:
    """Resolve exactly one wallet, or explain why it could not be determined safely.

    Returns `(wallet, None)` on success and `(None, reason)` otherwise. The
    reason is written for the operator, not for a log: ambiguity is reported as
    ambiguity ("three wallets match 'main', say which") rather than resolved by
    picking the first row, because picking the wrong wallet means signing from the
    wrong account.
    """
    service = WalletService(db)

    if wallet_id:
        # The model may pass the id as a string. `AsyncSession.get` needs the native
        # type: on PostgreSQL a str id raises DataError, and on SQLite it silently
        # matches nothing - so an operator naming a real wallet by id got
        # "No wallet with id ...".
        import uuid as _uuid

        try:
            key = _uuid.UUID(str(wallet_id).strip())
        except (ValueError, AttributeError, TypeError):
            return None, f"'{wallet_id}' is not a wallet id."
        row = await service.get(key)
        if row is None:
            return None, f"No wallet with id '{wallet_id}'."
        return row, None

    if address:
        try:
            normalized = validate_address(address)
        except WalletError as exc:
            return None, str(exc)
        row = await db.scalar(select(Wallet).where(Wallet.address == normalized))
        if row is None:
            return None, f"No wallet is registered at {normalized}."
        return row, None

    query = select(Wallet).where(Wallet.is_active.is_(True)).order_by(Wallet.created_at.desc())
    if label:
        query = query.where(Wallet.label.ilike(f"%{label.strip()}%"))
    rows: List[Wallet] = list((await db.execute(query.limit(20))).scalars().all())

    if network:
        wanted = str(network).strip().lower()
        rows = [r for r in rows if wanted in {str(n).lower() for n in (r.supported_networks or [])}]
        if not rows:
            return None, f"No active wallet declares support for '{network}'."

    if not rows:
        return None, (
            "No wallet matched. Use list_wallets to see what is configured, or "
            "create_wallet to make one."
        )
    if len(rows) > 1:
        # The operator chose a default wallet on the Wallets page: it settles "which one"
        # when the task did not name a wallet. Only among wallets that already passed the
        # label/network filters above, so a default that does not fit is never forced on a
        # task that needs another network.
        defaults = [r for r in rows if getattr(r, "is_default", False)]
        if len(defaults) == 1:
            return defaults[0], None
        names = ", ".join(f"'{r.label}' ({r.address})" for r in rows[:5])
        return None, (
            f"{len(rows)} wallets match. Say which one, by label, id or address. Candidates: {names}"
        )
    return rows[0], None


def _public(row: Wallet, service: WalletService) -> Dict[str, Any]:
    """Public projection plus an explicit 'no secrets here' guarantee.

    `WalletService.to_dict` is the single source of truth for what may leave the
    service; nothing decrypts the key to build this.
    """
    data = service.to_dict(row)
    data["secrets_exposed"] = False
    return data


class ListWalletsTool(BaseTool):
    name = "list_wallets"
    version = "1.0.0"
    description = (
        "List the operator's configured wallets: public address, label, networks, "
        "and whether each can sign. Never returns private keys."
    )
    required_permissions = ["wallet.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 20
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "description": "Only wallets supporting this network"},
            "include_inactive": {"type": "boolean"},
        },
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        async with session_scope() as db:
            service = WalletService(db)
            rows = await service.list(
                is_active=None if params.get("include_inactive") else True, limit=100
            )
            if params.get("network"):
                wanted = str(params["network"]).strip().lower()
                rows = [r for r in rows if wanted in {str(n).lower() for n in (r.supported_networks or [])}]

            if not rows:
                return ToolResult.success(
                    data={
                        "wallets": [],
                        "count": 0,
                        "note": "No wallets are configured. Use create_wallet to add one.",
                    },
                    evidence={"authoritative": True, "kind": "wallet_record", "count": 0},
                )

            return ToolResult.success(
                data={
                    "wallets": [_public(r, service) for r in rows],
                    "count": len(rows),
                    "note": "Public metadata only. Key material is never returned to the agent.",
                },
                evidence={
                    "authoritative": True,
                    "kind": "wallet_record",
                    "count": len(rows),
                },
            )


class GetWalletAddressTool(BaseTool):
    name = "get_wallet_address"
    version = "1.0.0"
    description = (
        "Return the PUBLIC address of one of the operator's wallets. Identify it by "
        "wallet id, label, address, or simply 'the only one'. When a task asks for a wallet "
        "address and names none, call this with no arguments (add `network` for the chain the "
        "task needs): it returns the operator's DEFAULT wallet from the Wallets page. When "
        "several wallets match and none is the default, the tool says so instead of guessing."
    )
    required_permissions = ["wallet.read"]
    side_effect = SideEffect.NONE
    timeout_seconds = 20
    input_schema = {
        "type": "object",
        "properties": {
            "wallet_id": {"type": "string", "maxLength": 64},
            "label": {"type": "string", "maxLength": 255},
            "address": {"type": "string", "maxLength": 42},
            "network": {"type": "string", "maxLength": 50},
        },
        "required": [],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        async with session_scope() as db:
            service = WalletService(db)
            row, reason = await resolve_wallet(
                db,
                wallet_id=params.get("wallet_id"),
                label=params.get("label"),
                address=params.get("address"),
                network=params.get("network"),
            )
            if row is None:
                # Ambiguity is not an error: it is the operator being asked a question.
                return ToolResult.needs_user(reason or "The wallet could not be identified.")

            data = _public(row, service)
            if params.get("network"):
                try:
                    get_chain(params["network"])
                except ChainError as exc:
                    return ToolResult.failure(str(exc), category="UNKNOWN_NETWORK")
                if str(params["network"]).lower() not in {
                    str(n).lower() for n in (row.supported_networks or [])
                }:
                    return ToolResult.needs_user(
                        f"Wallet '{row.label}' does not declare support for '{params['network']}'. "
                        f"It supports: {', '.join(row.supported_networks or []) or 'none'}."
                    )

            return ToolResult.success(
                data={
                    "wallet_id": data["id"],
                    "label": data["label"],
                    "address": data["address"],
                    "chain_type": data["chain_type"],
                    "supported_networks": data["supported_networks"],
                    "can_sign": data["can_sign"],
                    "is_locked": data["is_locked"],
                    "is_active": data["is_active"],
                    "has_key_material": data["has_key_material"],
                    # The frontend's Copy Address button copies exactly this value.
                    "copyable_address": data["address"],
                    "is_default": data.get("is_default", False),
                },
                evidence={
                    "authoritative": True,
                    "kind": "wallet_record",
                    "wallet_id": data["id"],
                    "address": data["address"],
                },
            )


class CreateWalletTool(BaseTool):
    name = "create_wallet"
    version = "1.0.0"
    description = (
        "Generate a new EVM wallet in the secure vault and return its public address. "
        "The private key is generated, encrypted and never returned, logged or shown."
    )
    required_permissions = ["wallet.write"]
    side_effect = SideEffect.STATE_MUTATION
    timeout_seconds = 30
    is_idempotent = False
    input_schema = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "maxLength": 255, "description": "A name for the wallet"},
            "networks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Networks it should support. Defaults to every supported network.",
            },
            "notes": {"type": "string", "maxLength": 2000},
        },
        "required": ["label"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        async with session_scope() as db:
            service = WalletService(db)
            try:
                row = await service.create_wallet(
                    label=params["label"],
                    supported_networks=params.get("networks"),
                    notes=params.get("notes"),
                )
            except WalletError as exc:
                return ToolResult.failure(str(exc), category="WALLET_INVALID")
            except DecryptionError:
                return ToolResult.failure(
                    "The wallet vault could not be used. WALLET_VAULT_MASTER_KEY is wrong "
                    "for the existing database; wallets cannot be created until it is fixed.",
                    category="VAULT_UNAVAILABLE",
                )

            data = _public(row, service)
            return ToolResult.success(
                data={
                    "wallet_id": data["id"],
                    "label": data["label"],
                    "address": data["address"],
                    "supported_networks": data["supported_networks"],
                    "can_sign": data["can_sign"],
                    "copyable_address": data["address"],
                    "warning": (
                        "The private key was generated inside the vault and is not exportable "
                        "from AirdropOS. Back up the vault master key itself or this wallet is "
                        "unrecoverable."
                    ),
                },
                evidence={
                    "authoritative": True,
                    "kind": "wallet_record",
                    "wallet_id": data["id"],
                    "address": data["address"],
                    "created": True,
                },
            )


class GetWalletBalancesTool(BaseTool):
    name = "get_wallet_balances"
    version = "1.0.0"
    description = (
        "Read the live native-token balance of one of the operator's wallets on a "
        "network. Reports the RPC problem honestly when the network is unreachable "
        "or the endpoint rejects the request."
    )
    required_permissions = ["wallet.read", "blockchain.read"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    input_schema = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "maxLength": 50},
            "wallet_id": {"type": "string", "maxLength": 64},
            "label": {"type": "string", "maxLength": 255},
            "address": {"type": "string", "maxLength": 42},
        },
        "required": ["network"],
    }

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        from backend.app.transactions.engine import TransactionRejected, engine

        network = params["network"]
        try:
            chain = get_chain(network)
        except ChainError as exc:
            return ToolResult.failure(str(exc), category="UNKNOWN_NETWORK")

        if not engine.rpc_url(chain.name):
            return ToolResult.failure(
                f"No RPC endpoint is configured for '{chain.name}'. Set the RPC setting for "
                f"that network before balances can be read.",
                category="NO_RPC",
            )

        async with session_scope() as db:
            service = WalletService(db)
            row, reason = await resolve_wallet(
                db,
                wallet_id=params.get("wallet_id"),
                label=params.get("label"),
                address=params.get("address"),
                network=chain.name,
            )
            if row is None:
                return ToolResult.needs_user(reason or "The wallet could not be identified.")
            wallet_id, address, label = str(row.id), row.address, row.label

        try:
            balance_wei = await engine.get_balance_wei(chain.name, address)
        except TransactionRejected as exc:
            return ToolResult.failure(
                str(exc),
                category=exc.category,
                retryable=exc.category not in ("RPC_AUTH", "NO_RPC"),
            )

        return ToolResult.success(
            data={
                "network": chain.name,
                "chain_id": chain.chain_id,
                "native_symbol": chain.native_symbol,
                "wallet_id": wallet_id,
                "label": label,
                "address": address,
                "balance_wei": str(balance_wei),
                "balance_native": format_units(balance_wei, 18),
                "note": "Native gas token only. ERC-20 balances need a token contract read.",
            },
            evidence={
                "authoritative": True,
                "kind": "chain_read",
                "network": chain.name,
                "address": address,
                "block_source": "eth_getBalance",
            },
        )


def register_wallet_tools(registry) -> None:
    registry.register_or_replace_many(
        [
            ListWalletsTool(),
            GetWalletAddressTool(),
            CreateWalletTool(),
            GetWalletBalancesTool(),
        ]
    )