"""Wallet Management Service.

Creates and maintains `Wallet` records. The private key or mnemonic is encrypted
with the AES-256-GCM vault before it ever reaches the database, and the
plaintext is never returned, logged, or included in an audit record.

This service does **not** sign or broadcast. It stores key material so that
`app/wallets/vault.py` and the transaction engine can sign later through the
reviewed path, with the emergency-stop and spend-limit guards intact.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Wallet
from backend.app.wallets.vault import SUPPORTED_CHAIN_IDS, KeyVault, vault

logger = logging.getLogger(__name__)

# Networks a wallet may declare support for. Mirrors the engine's RPC map so an
# imported wallet cannot advertise a network the platform cannot reach.
KNOWN_NETWORKS = frozenset(SUPPORTED_CHAIN_IDS.values())

ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


class WalletError(Exception):
    """A wallet could not be created or updated."""


class WalletValidationError(WalletError):
    """The supplied wallet material is malformed or unsafe."""


def validate_address(address: str) -> str:
    """Validate an EVM address and return it checksum-normalized lowercase."""
    if not isinstance(address, str) or not ADDRESS_PATTERN.match(address.strip()):
        raise WalletValidationError(
            "Invalid EVM address: expected 0x followed by 40 hex characters."
        )
    return address.strip().lower()


def validate_networks(networks: Optional[List[str]]) -> List[str]:
    if not networks:
        return sorted(KNOWN_NETWORKS)
    unknown = [n for n in networks if n.lower() not in KNOWN_NETWORKS]
    if unknown:
        raise WalletValidationError(
            f"Unknown network(s): {', '.join(unknown)}. "
            f"Known networks: {', '.join(sorted(KNOWN_NETWORKS))}"
        )
    return sorted({n.lower() for n in networks})


def _looks_like_mnemonic(secret: str) -> bool:
    """BIP-39 mnemonics are 12/15/18/21/24 lowercase words."""
    words = secret.strip().split()
    return len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() for w in words)


class WalletService:
    """CRUD for wallets. Key material is encrypted on the way in, never on the way out."""

    def __init__(self, db: AsyncSession, key_vault: Optional[KeyVault] = None) -> None:
        self.db = db
        self.vault = key_vault or vault

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    async def import_wallet(
        self,
        *,
        label: str,
        secret: str,
        chain_type: str = "EVM",
        derivation_path: Optional[str] = None,
        supported_networks: Optional[List[str]] = None,
        notes: Optional[str] = None,
    ) -> Wallet:
        """Encrypt and store a private key or mnemonic as a new wallet.

        The secret is validated by deriving an address from it, so a malformed
        key is rejected before anything is written.
        """
        if not label or not label.strip():
            raise WalletValidationError("A wallet label is required.")
        if not secret or not secret.strip():
            raise WalletValidationError("A private key or mnemonic is required.")
        if chain_type.upper() != "EVM":
            raise WalletValidationError(
                f"Unsupported chain_type '{chain_type}'. Only EVM is supported."
            )

        candidate = secret.strip()
        if candidate.startswith("0x") and len(candidate) != 66:
            raise WalletValidationError(
                "An 0x-prefixed private key must be 32 bytes (66 characters)."
            )

        # Encrypt first, then derive the address from the ciphertext. This proves
        # the stored material is usable without the plaintext outliving the call.
        ciphertext, iv_b64, tag_b64 = self.vault.encrypt(candidate)
        try:
            address = self.vault.public_address(ciphertext, iv_b64, tag_b64, label=label)
        except Exception:
            # `from None` deliberately discards the original exception. A library
            # parse error can echo the key it choked on, and this is a secrets
            # path: the cause must not reach a traceback or an error log.
            raise WalletValidationError(
                "The supplied key or mnemonic could not be used to derive an address."
            ) from None

        networks = validate_networks(supported_networks)

        existing = await self.db.scalar(select(Wallet).where(Wallet.address == address))
        if existing is not None:
            raise WalletError(
                f"A wallet with address {address} already exists (id={existing.id})."
            )

        wallet_row = Wallet(
            label=label.strip()[:255],
            address=address,
            chain_type="EVM",
            derivation_path=derivation_path or (
                "m/44'/60'/0'/0/0" if _looks_like_mnemonic(candidate) else None
            ),
            encrypted_secret=ciphertext,
            encryption_iv=iv_b64,
            encryption_tag=tag_b64,
            supported_networks=networks,
            notes=notes,
        )
        self.db.add(wallet_row)
        await self.db.flush()

        logger.info("Imported wallet '%s' at %s (%s)", wallet_row.label, address, wallet_row.id)
        return wallet_row

    async def watch_address(
        self,
        *,
        label: str,
        address: str,
        chain_type: str = "EVM",
        supported_networks: Optional[List[str]] = None,
        notes: Optional[str] = None,
    ) -> Wallet:
        """Register a public address with no key material.

        Airdrop participation frequently needs an address the operator holds
        elsewhere. This records it without pretending we can sign for it.
        """
        if chain_type.upper() != "EVM":
            raise WalletValidationError(
                f"Unsupported chain_type '{chain_type}'. Only EVM is supported."
            )
        if not label or not label.strip():
            raise WalletValidationError("A wallet label is required.")

        normalized = validate_address(address)
        existing = await self.db.scalar(select(Wallet).where(Wallet.address == normalized))
        if existing is not None:
            raise WalletError(f"A wallet with address {normalized} already exists.")

        row = Wallet(
            label=label.strip()[:255],
            address=normalized,
            chain_type="EVM",
            encrypted_secret=None,
            encryption_iv=None,
            encryption_tag=None,
            supported_networks=validate_networks(supported_networks),
            notes=notes,
        )
        self.db.add(row)
        await self.db.flush()
        logger.info("Registered watch-only wallet '%s' at %s", row.label, normalized)
        return row

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    async def get(self, wallet_id) -> Optional[Wallet]:
        return await self.db.get(Wallet, wallet_id)

    async def list(
        self,
        *,
        is_active: Optional[bool] = None,
        chain_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Wallet]:
        query = select(Wallet).order_by(Wallet.created_at.desc())
        if is_active is not None:
            query = query.where(Wallet.is_active.is_(is_active))
        if chain_type:
            query = query.where(Wallet.chain_type == chain_type.upper())
        result = await self.db.execute(query.offset(offset).limit(limit))
        return list(result.scalars().all())

    def can_sign(self, row: Wallet) -> bool:
        """Whether this wallet holds usable key material and is unlocked."""
        return bool(
            row.encrypted_secret
            and row.encryption_iv
            and row.encryption_tag
            and row.is_active
            and not row.is_locked
        )

    def to_dict(self, row: Wallet) -> Dict[str, Any]:
        """Public projection. `can_sign` is a capability flag, not the key."""
        return {
            "id": str(row.id),
            "label": row.label,
            "address": row.address,
            "chain_type": row.chain_type,
            "derivation_path": row.derivation_path,
            "supported_networks": row.supported_networks or [],
            "is_active": row.is_active,
            "is_locked": row.is_locked,
            "has_key_material": bool(row.encrypted_secret),
            "can_sign": self.can_sign(row),
            "total_spent_usd": row.total_spent_usd,
            "notes": row.notes,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    # ------------------------------------------------------------------
    # Mutate
    # ------------------------------------------------------------------
    async def update(
        self,
        row: Wallet,
        *,
        label: Optional[str] = None,
        notes: Optional[str] = None,
        supported_networks: Optional[List[str]] = None,
    ) -> Wallet:
        before = self.to_dict(row)
        if label is not None:
            if not label.strip():
                raise WalletValidationError("Label cannot be empty.")
            row.label = label.strip()[:255]
        if notes is not None:
            row.notes = notes
        if supported_networks is not None:
            row.supported_networks = validate_networks(supported_networks)
        await self.db.flush()
        return before

    async def set_locked(self, row: Wallet, locked: bool) -> None:
        """Locking prevents signing without destroying the encrypted key."""
        row.is_locked = bool(locked)
        await self.db.flush()

    async def set_active(self, row: Wallet, active: bool) -> None:
        row.is_active = bool(active)
        await self.db.flush()

    async def delete(self, row: Wallet) -> None:
        """Remove a wallet and its key material.

        Transactions, claims, and eligibility rows cascade on delete, so this is
        destructive and is only reachable with the wallet_write scope.
        """
        await self.db.delete(row)
        await self.db.flush()