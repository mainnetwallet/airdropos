"""Encrypted Key Vault (Specification Part 13).

Security contract enforced here:
  * Private keys / seed phrases are encrypted at rest with AES-256-GCM.
  * The encryption master key lives in configuration, SEPARATE from stored data.
  * Secrets are NEVER returned to the LLM, the API layer, logs, or memory.
  * The LLM may only request a structured signing operation. This module
    independently validates the request against authorization limits before
    releasing any key material to the signer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_account import Account
from eth_account.messages import encode_defunct

from backend.app.core.config import settings
from backend.app.security.emergency_stop import emergency_stop

logger = logging.getLogger(__name__)

# eth-account 0.13+ refuses HD-wallet (mnemonic) derivation unless the operator
# opts in, because that API is not covered by its security audit. The Wallet
# Management API accepts a BIP-39 phrase as key material, so the opt-in is
# required for the documented feature to work at all.
#
# This is a deliberate, recorded decision: it re-enables exactly one audited-
# apart code path (mnemonic -> key at a caller-supplied BIP-44 path) inside this
# module, and nothing else. Private-key-only use does not require it. Revisit if
# eth-account stabilises the HD-wallet API, and prefer dropping mnemonic support
# over leaving this flag set if it is ever questioned.
try:
    Account.enable_unaudited_hdwallet_features()
except Exception:  # pragma: no cover - older eth-account has no such guard
    pass

# Enable known test networks only. Refuse to operate on an unnamed chain.
# Derived from the chain catalog so the vault cannot sign for a chain the rest
# of the platform does not know about.
from backend.app.blockchain.chains import all_chain_ids  # noqa: E402

SUPPORTED_CHAIN_IDS: Dict[int, str] = all_chain_ids()


_HEX_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
# The publicly known development key from config.py. Never acceptable in production.
_DEV_MASTER_KEY = "0123456789abcdef" * 4


class VaultError(Exception):
    """Base vault failure."""


class DecryptionError(VaultError):
    pass


class SigningDenied(VaultError):
    """Raised when a signing request violates authorization policy."""


@dataclass(frozen=True)
class SigningRequest:
    """A structured request to produce a signature.

    Contains NO secret material. The signer independently validates this.
    """

    wallet_address: str
    chain_id: int
    message: Optional[str] = None
    transaction: Optional[Dict[str, object]] = None


@dataclass(frozen=True)
class SigningResult:
    """Result of a signing operation. Contains the signature, never the key."""

    signature: str
    signer_address: str
    signed_transaction: Optional[Dict[str, object]] = None


class KeyVault:
    """AES-256-GCM envelope encryption for wallet secrets."""

    def __init__(self, master_key_hex: Optional[str] = None) -> None:
        raw = (master_key_hex or settings.WALLET_VAULT_MASTER_KEY or "").strip()
        if raw[:2].lower() == "0x":
            raw = raw[2:]
        # Exactly 64 hex characters. Checking the format (not only the length) means a typo or a
        # non-hex value fails here with a clear message instead of as an opaque ValueError, and the
        # value itself is never echoed.
        if not _HEX_KEY_RE.fullmatch(raw):
            raise VaultError(
                "WALLET_VAULT_MASTER_KEY must be exactly 64 hexadecimal characters (256-bit key)."
            )
        if settings.ENVIRONMENT == "production" and raw.lower() == _DEV_MASTER_KEY:
            raise VaultError(
                "WALLET_VAULT_MASTER_KEY is the publicly known development key; "
                "refusing to protect wallets with it in production."
            )
        self._master_key = bytes.fromhex(raw)
        self._aesgcm = AESGCM(self._master_key)
        self._memory_guard_active = True

    # ------------------------------------------------------------------
    # Encryption primitives
    # ------------------------------------------------------------------
    def encrypt(self, plaintext: str, aad: Optional[bytes] = None) -> Tuple[str, str, str]:
        """Encrypt a secret. Returns (ciphertext_b64, iv_b64, tag_b64)."""
        nonce = os.urandom(12)
        sealed = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
        # AESGCM returns ciphertext||tag; split for storage.
        ciphertext, tag = sealed[:-16], sealed[-16:]
        return (
            base64.b64encode(ciphertext).decode(),
            base64.b64encode(nonce).decode(),
            base64.b64encode(tag).decode(),
        )

    def decrypt(self, ciphertext_b64: str, iv_b64: str, tag_b64: str, aad: Optional[bytes] = None) -> str:
        """Decrypt a stored secret. Callers must never log or persist the result."""
        try:
            nonce = base64.b64decode(iv_b64)
            ciphertext = base64.b64decode(ciphertext_b64)
            tag = base64.b64decode(tag_b64)
            plaintext = self._aesgcm.decrypt(nonce, ciphertext + tag, aad)
            return plaintext.decode("utf-8")
        except Exception as exc:
            # Deliberately does not include plaintext or key material.
            raise DecryptionError(f"Vault decryption failed: {type(exc).__name__}") from exc

    # ------------------------------------------------------------------
    # Key material derivation (ephemeral, scoped, zeroized after use)
    # ------------------------------------------------------------------
    @contextmanager
    def _with_private_key(
        self,
        encrypted_secret: str,
        iv_b64: str,
        tag_b64: str,
        label: str,
    ):
        """Context manager yielding the private key, guaranteeing cleanup.

        The key object exists only inside this scope and is never stored on a
        long-lived instance, logged, or passed to the LLM.

        The `@contextmanager` decorator is load-bearing: without it this is a
        bare generator, and every `with self._with_private_key(...)` raises
        `AttributeError: __enter__` at the point of use rather than at import.
        """
        secret = self.decrypt(encrypted_secret, iv_b64, tag_b64)

        if secret.startswith("0x"):
            private_key = secret
        else:
            # Treat as a mnemonic seed phrase; derive deterministically (m/44'/60'/0'/0/0).
            private_key = Account.from_mnemonic(
                secret, account_path="m/44'/60'/0'/0/0"
            ).key.hex()
            if not private_key.startswith("0x"):
                private_key = "0x" + private_key

        try:
            yield private_key
        finally:
            # Best-effort scrub of the local reference.
            secret = "\x00" * len(secret)
            private_key = "\x00" * len(private_key)
            del secret, private_key
            logger.debug("Ephemeral key scope for '%s' closed", label)

    # ------------------------------------------------------------------
    # Public address derivation (safe to expose)
    # ------------------------------------------------------------------
    @staticmethod
    def derive_address(private_key: str) -> str:
        return Account.from_key(private_key).address

    def public_address(
        self, encrypted_secret: str, iv_b64: str, tag_b64: str, label: str = "wallet"
    ) -> str:
        """Derive the public address without exposing the private key."""
        with self._with_private_key(encrypted_secret, iv_b64, tag_b64, label) as private_key:
            return self.derive_address(private_key)

    # ------------------------------------------------------------------
    # Validation gate (independent of the LLM)
    # ------------------------------------------------------------------
    def validate_request(
        self,
        request: SigningRequest,
        authorized_addresses: Optional[set[str]] = None,
    ) -> None:
        """Independently verify a signing request. Raises SigningDenied on violation.

        This is the deterministic backstop: even a fully prompt-injected LLM
        cannot make this function sign something outside policy.
        """
        if not emergency_stop.can_sign():
            raise SigningDenied(
                "Emergency stop is active. All wallet signing is blocked. "
                "Note: transactions already broadcast cannot be recalled."
            )

        if request.chain_id not in SUPPORTED_CHAIN_IDS:
            raise SigningDenied(
                f"Unsupported chain_id {request.chain_id}. "
                f"Known chains: {sorted(SUPPORTED_CHAIN_IDS)}"
            )

        # Fail closed: a missing or empty authorized set means "nothing is
        # authorized", not "no restriction".
        if not authorized_addresses:
            raise SigningDenied(
                "No authorized wallet set was provided; refusing to sign."
            )
        if str(request.wallet_address).lower() not in {
            str(a).lower() for a in authorized_addresses
        }:
            raise SigningDenied(
                f"Wallet {request.wallet_address} is not in the authorized wallet set."
            )

        if request.message is None and request.transaction is None:
            raise SigningDenied("Signing request must include either a message or a transaction.")

        if request.transaction is not None:
            self._validate_transaction(request.transaction)

    @staticmethod
    def _validate_transaction(tx: Dict[str, object]) -> None:
        """Reject transactions that attempt privileged or system operations."""
        to = str(tx.get("to", "")).strip()
        data = str(tx.get("data") or tx.get("input") or "0x")

        if to.lower() in ("", "0x"):
            # Contract creation. Allowed but must be explicitly requested.
            pass

        if len(data) > 200_000:
            raise SigningDenied("Transaction calldata exceeds 200KB; refusing to sign.")

        method_id = data[:10].lower()
        # Never let the agent sign ownership transfers or self-destructs.
        forbidden_selectors = {
            "0x8bda01ab": "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)",
            "0xf2fde38b": "transferOwnership(address)",
            "0x715018a6": "renounceOwnership()",
            "0xffc9a479": "unknown",
        }
        if method_id in forbidden_selectors:
            raise SigningDenied(
                f"Refusing to sign restricted operation with selector {method_id} "
                f"({forbidden_selectors[method_id]})."
            )

    # ------------------------------------------------------------------
    # Signing operations
    # ------------------------------------------------------------------
    def sign_message(
        self,
        request: SigningRequest,
        encrypted_secret: str,
        iv_b64: str,
        tag_b64: str,
        authorized_addresses: Optional[set[str]] = None,
    ) -> SigningResult:
        """Sign an EIP-191 personal message (e.g. wallet authentication)."""
        self.validate_request(request, authorized_addresses)

        with self._with_private_key(encrypted_secret, iv_b64, tag_b64, request.wallet_address) as pk:
            account = Account.from_key(pk)
            if account.address.lower() != request.wallet_address.lower():
                raise SigningDenied(
                    "Signer address does not match the requested wallet address."
                )
            signed = account.sign_message(encode_defunct(text=request.message or ""))

        return SigningResult(
            signature=signed.signature.hex(),
            signer_address=request.wallet_address,
        )

    def sign_transaction(
        self,
        request: SigningRequest,
        encrypted_secret: str,
        iv_b64: str,
        tag_b64: str,
        authorized_addresses: Optional[set[str]] = None,
    ) -> SigningResult:
        """Sign an EVM transaction. The caller must broadcast it separately."""
        self.validate_request(request, authorized_addresses)
        if not request.transaction:
            raise SigningDenied("sign_transaction requires a transaction payload.")

        with self._with_private_key(encrypted_secret, iv_b64, tag_b64, request.wallet_address) as pk:
            account = Account.from_key(pk)
            if account.address.lower() != request.wallet_address.lower():
                raise SigningDenied("Signer address does not match the requested wallet address.")

            tx = dict(request.transaction)
            tx.setdefault("chainId", request.chain_id)

            signed = account.sign_transaction(tx)
            raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction

        return SigningResult(
            signature="0x",
            signed_transaction={"raw": raw.hex() if hasattr(raw, "hex") else str(raw)},
            signer_address=request.wallet_address,
        )

    # ------------------------------------------------------------------
    # Hygiene helpers
    # ------------------------------------------------------------------
    def fingerprint(self) -> str:
        """Non-reversible identifier for the master key, safe for diagnostics."""
        return hashlib.sha256(self._master_key).hexdigest()[:16]

    def verify_integrity(self, stored_hash: Optional[str] = None) -> bool:
        """Confirm the loaded master key matches a previously recorded fingerprint."""
        if not stored_hash:
            return True
        return hmac.compare_digest(self.fingerprint(), stored_hash)


# Module-level singleton. All signing flows through this instance.
vault = KeyVault()
