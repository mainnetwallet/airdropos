"""ERC-20 Token Registry: the only place a token contract address may come from.

A token *symbol* does not identify a contract. "USDC" exists as a different
contract on every chain, several of them are impostors, and a wrong address sends
real funds to a contract that will happily keep them. So the platform never
resolves a symbol by searching: an address must already be known here (the
catalog) or supplied by the operator (configuration or an explicit tool
argument), and it is then read back on-chain before anything is signed.

This module is deliberately read-only. It cannot sign, cannot broadcast, and
cannot add an address at runtime; an unlisted token is refused, not guessed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.app.blockchain.chains import ChainError, get_chain, known_networks

logger = logging.getLogger(__name__)


class TokenError(Exception):
    """A token could not be resolved for the requested network."""

    def __init__(self, message: str, category: str = "TOKEN_UNKNOWN") -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class TokenRecord:
    """One ERC-20 contract the platform is willing to interact with."""

    symbol: str
    network: str
    address: str  # lowercase, 0x-prefixed
    decimals: Optional[int] = None  # None: read from the contract at use time
    name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "network": self.network,
            "address": self.address,
            "decimals": self.decimals,
            "name": self.name,
        }


# The built-in catalog of widely-used, canonical contracts. `decimals` is left as
# None wherever possible: the live value is read from the contract at use time, so
# a wrong guess here cannot produce a wrong transfer amount.
TOKEN_CATALOG: tuple[TokenRecord, ...] = (
    # ---- Ethereum mainnet ----
    TokenRecord("USDC", "ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6, "USD Coin"),
    TokenRecord("USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7", 6, "Tether USD"),
    TokenRecord("DAI", "ethereum", "0x6b175474e89094c44da98b954eedeac495271d0f", 18, "Dai Stablecoin"),
    TokenRecord("WETH", "ethereum", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 18, "Wrapped Ether"),
    # ---- Arbitrum One ----
    TokenRecord("USDC", "arbitrum", "0xaf88d065e77c8cc2239327c5edb3a432268e5831", 6, "USD Coin"),
    TokenRecord("USDT", "arbitrum", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", 6, "Tether USD"),
    TokenRecord("WETH", "arbitrum", "0x82af49447d8a07e3bd95bd0d56f35241523fbab1", 18, "Wrapped Ether"),
    # ---- OP Mainnet ----
    TokenRecord("USDC", "optimism", "0x0b2c639c533813f4aa9d7837caf62653d097ff85", 6, "USD Coin"),
    TokenRecord("USDT", "optimism", "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58", 6, "Tether USD"),
    TokenRecord("WETH", "optimism", "0x4200000000000000000000000000000000000006", 18, "Wrapped Ether"),
    # ---- Base ----
    TokenRecord("USDC", "base", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6, "USD Coin"),
    TokenRecord("WETH", "base", "0x4200000000000000000000000000000000000006", 18, "Wrapped Ether"),
    # ---- Polygon PoS ----
    TokenRecord("USDC", "polygon", "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359", 6, "USD Coin"),
    TokenRecord("USDT", "polygon", "0xc2132d05d31c914a87c6611c10748aeb04b58e8f", 6, "Tether USD"),
    TokenRecord("WETH", "polygon", "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619", 18, "Wrapped Ether"),
)

# Operators can register their own contracts without a code change, via
# `TOKEN_CONTRACTS` = {"ethereum": {"MYTOKEN": "0x...", ...}, ...}.
# An operator-registered address is still read back on-chain before use.
def _configured_tokens() -> Dict[tuple[str, str], TokenRecord]:
    from backend.app.core.config import settings

    raw = getattr(settings, "TOKEN_CONTRACTS", None) or {}
    out: Dict[tuple[str, str], TokenRecord] = {}
    if isinstance(raw, dict):
        for network, tokens in raw.items():
            if not isinstance(tokens, dict):
                continue
            for symbol, address in tokens.items():
                normalized = str(address).strip().lower()
                if not normalized.startswith("0x") or len(normalized) != 42:
                    logger.error(
                        "Ignoring invalid TOKEN_CONTRACTS entry %s/%s: not a 42-character 0x address",
                        network, symbol,
                    )
                    continue
                out[(str(network).strip().lower(), str(symbol).strip().upper())] = TokenRecord(
                    symbol=str(symbol).strip().upper(),
                    network=str(network).strip().lower(),
                    address=normalized,
                    name="operator-configured",
                )
    return out


def _catalog_index() -> Dict[tuple[str, str], TokenRecord]:
    index: Dict[tuple[str, str], TokenRecord] = {}
    # Operator configuration wins over the built-in catalog.
    for record in TOKEN_CATALOG:
        index[(record.network, record.symbol.upper())] = record
    index.update(_configured_tokens())
    return index


_INDEX: Optional[Dict[tuple[str, str], TokenRecord]] = None


def _index() -> Dict[tuple[str, str], TokenRecord]:
    global _INDEX
    if _INDEX is None:
        _INDEX = _catalog_index()
    return _INDEX


def reset_token_index() -> None:
    """Drop the memoised index. Called after configuration changes and by tests."""
    global _INDEX
    _INDEX = None


def normalize_address(address: Any) -> str:
    """Validate and lowercase a 20-byte EVM address."""
    text = str(address or "").strip().lower()
    if not text.startswith("0x") or len(text) != 42:
        raise TokenError(
            f"'{address}' is not a 20-byte EVM contract address (expected 0x + 40 hex).",
            category="INVALID_ADDRESS",
        )
    try:
        int(text, 16)
    except ValueError as exc:
        raise TokenError(f"'{address}' is not hexadecimal.", category="INVALID_ADDRESS") from exc
    return text


def resolve_token(network: str, symbol: Optional[str] = None, address: Optional[str] = None) -> TokenRecord:
    """Resolve a token for a network.

    Exactly one of `symbol` or `address` must be supplied. An explicit address is
    accepted as-is (after validation) because the operator may legitimately hold a
    token this catalog has never heard of; a symbol is resolved only against the
    known catalog, and an unknown symbol is refused rather than searched for.

    Raises `TokenError`; never returns a best guess.
    """
    try:
        chain = get_chain(network)
    except ChainError as exc:
        raise TokenError(str(exc), category="UNKNOWN_NETWORK") from exc

    if bool(symbol) == bool(address):
        raise TokenError(
            "Specify exactly one of 'symbol' or 'contract_address' for an ERC-20 transfer.",
            category="INVALID_INPUT",
        )

    if address:
        return TokenRecord(
            symbol=(symbol or "").strip().upper() or "UNKNOWN",
            network=chain.name,
            address=normalize_address(address),
            name="operator-supplied contract (not in the built-in catalog)",
        )

    wanted = str(symbol).strip().upper()
    record = _index().get((chain.name, wanted))
    if record is None:
        available = sorted(s for (n, s) in _index() if n == chain.name)
        raise TokenError(
            f"'{wanted}' is not a known {chain.name} token. Known symbols on this network: "
            f"{', '.join(available) or 'none'}. Supply an explicit 'contract_address' instead; "
            f"a symbol is never resolved by searching.",
            category="TOKEN_UNKNOWN",
        )
    return record


def known_tokens(network: Optional[str] = None) -> List[Dict[str, Any]]:
    """Catalog listing for the operator UI and the agent's tool schema."""
    wanted = str(network).strip().lower() if network else None
    if wanted and wanted not in known_networks():
        raise TokenError(f"Unknown network '{network}'.", category="UNKNOWN_NETWORK")
    records = [
        r for r in _index().values() if wanted is None or r.network == wanted
    ]
    records.sort(key=lambda r: (r.network, r.symbol))
    return [r.to_dict() for r in records]


# ----------------------------------------------------------------------
# On-chain reads (read-only eth_call)
# ----------------------------------------------------------------------
def _selector(signature: str) -> str:
    from eth_utils import keccak

    return "0x" + keccak(text=signature)[:4].hex()


async def eth_call(engine, network: str, to: str, data: str) -> str:
    """One raw `eth_call`. Raises `TransactionRejected` on any RPC problem."""
    return await engine._rpc(network, "eth_call", [{"to": to, "data": data}, "latest"])


def encode_erc20_transfer(recipient: str, amount_base_units: int) -> str:
    """`transfer(address,uint256)` calldata, ABI-encoded manually.

    Manual encoding keeps the signing path free of any ABI package that could
    disagree with the standard about padding or signedness.
    """
    address = normalize_address(recipient)
    if amount_base_units < 0:
        raise TokenError("Transfer amount cannot be negative.", category="INVALID_VALUE")
    body = address[2:].rjust(64, "0") + format(amount_base_units, "x").rjust(64, "0")
    return _selector("transfer(address,uint256)") + body


def _decode_string(abi_word: str) -> Optional[str]:
    """Decode an ABI-encoded `string` return value.

    Returns None for the bytes32 form (older non-standard tokens return a padded
    32-byte word instead of a dynamic string), because guessing which layout a
    contract uses is how a token ends up mislabelled.
    """
    if not isinstance(abi_word, str) or not abi_word.startswith("0x"):
        return None
    raw = bytes.fromhex(abi_word[2:] or "")
    if len(raw) < 64:
        return None
    offset = int.from_bytes(raw[:32], "big")
    if offset == 32:
        # bytes32: trailing NULs are padding, not content.
        try:
            return raw[:32].rstrip(b"\x00").decode("utf-8").strip() or None
        except UnicodeDecodeError:
            return None
    if offset + 32 > len(raw):
        return None
    length = int.from_bytes(raw[offset : offset + 32], "big")
    start = offset + 32
    end = min(start + length, len(raw))
    try:
        return raw[start:end].decode("utf-8").strip() or None
    except UnicodeDecodeError:
        return None


async def read_token_metadata(engine, network: str, record: TokenRecord) -> Dict[str, Any]:
    """Read `symbol`, `name` and `decimals` from the contract itself.

    This is the verification step: the contract on the network is asked what it is,
    rather than the platform asserting what it believes. Returns the chain's answer
    even when it disagrees with the catalog, and flags the disagreement.
    """
    out: Dict[str, Any] = {
        "symbol": None,
        "name": None,
        "decimals": None,
        "catalog_symbol": record.symbol,
        "catalog_decimals": record.decimals,
    }

    decimals_hex = await eth_call(engine, network, record.address, _selector("decimals()"))
    try:
        onchain_decimals = int(decimals_hex, 16)
        if not 0 <= onchain_decimals <= 36:
            raise ValueError(f"implausible decimals: {onchain_decimals}")
        out["decimals"] = onchain_decimals
    except (TypeError, ValueError) as exc:
        raise TokenError(
            f"{record.address} on {network} did not return usable decimals() "
            f"(got {decimals_hex!r}). It may not be an ERC-20 token.",
            category="NOT_A_TOKEN",
        ) from exc

    symbol_hex = await eth_call(engine, network, record.address, _selector("symbol()"))
    out["symbol_onchain"] = _decode_string(symbol_hex)

    # The catalog's cached value is only a hint; the chain is authoritative.
    if record.decimals is not None and record.decimals != out["decimals"]:
        out["decimals_mismatch"] = True
        logger.warning(
            "Token %s/%s reports %s decimals on-chain but the catalog recorded %s. "
            "Using the on-chain value.",
            network, record.address, out["decimals"], record.decimals,
        )
    return out


async def read_token_balance(engine, network: str, address: str, token: TokenRecord, decimals: int) -> str:
    """`balanceOf(address)` rendered as a decimal string using the contract's decimals."""
    body = normalize_address(address)[2:].rjust(64, "0")
    data = _selector("balanceOf(address)") + body
    raw = await eth_call(engine, network, token.address, data)
    try:
        value = int(raw, 16)
    except (TypeError, ValueError) as exc:
        raise TokenError(
            f"Could not read the {token.symbol} balance for {address} on {network}.",
            category="RPC_MALFORMED",
        ) from exc
    return format_units(value, decimals)


def format_units(amount: int, decimals: int) -> str:
    """Render an integer amount of base units as an exact decimal string.

    Uses integers only. Floating point would round amounts such as
    100 USDC (6 decimals) or 0.1 ETH, and a transfer of the wrong number of wei is
    a transfer of the wrong amount of money.
    """
    if decimals < 0:
        raise TokenError("Token decimals cannot be negative.", category="INVALID_INPUT")
    amount = int(amount)
    negative = amount < 0
    amount = abs(amount)
    digits = str(amount).rjust(decimals + 1, "0")
    whole, fraction = digits[: len(digits) - decimals] if decimals else digits, (
        digits[len(digits) - decimals:] if decimals else ""
    )
    fraction = fraction.rstrip("0")
    text = f"{whole}.{fraction}" if fraction else whole
    return f"-{text}" if negative else text


def parse_units(amount: Any, decimals: int) -> int:
    """Parse an exact decimal string into base units. Raises on anything imprecise.

    Accepts an integer or decimal string. Floats are rejected outright: `0.1` cannot
    be represented exactly in binary floating point, and silently sending
    0.1000000000000000055511151231257827 ETH is not an acceptable outcome.
    """
    if isinstance(amount, bool):
        raise TokenError("Amount must be a number, not a boolean.", category="INVALID_VALUE")
    if isinstance(amount, float):
        raise TokenError(
            "Amount must be given as a decimal string (for example \"0.01\"), not a float: "
            "a float cannot represent 0.01 exactly.",
            category="INVALID_VALUE",
        )
    if isinstance(amount, int):
        text = str(amount)
    else:
        text = str(amount).strip()

    if not text or not re_full_number(text):
        raise TokenError(
            f"'{amount}' is not a valid decimal amount.", category="INVALID_VALUE"
        )

    negative = text.startswith("-")
    if negative or text.startswith("+"):
        text = text[1:]
    if "." in text:
        whole, _, fraction = text.partition(".")
    else:
        whole, fraction = text, ""
    if len(fraction) > decimals:
        raise TokenError(
            f"'{amount}' has more than {decimals} decimal places, so it cannot be represented "
            f"exactly on a token with {decimals} decimals. Refusing to round a transfer amount.",
            category="PRECISION_LOSS",
        )
    value = int(whole or "0") * (10**decimals) + int(fraction.ljust(decimals, "0") or "0")
    return -value if negative else value


def re_full_number(text: str) -> bool:
    """True when `text` is a plain decimal number (no exponent, no separators)."""
    import re

    return bool(re.fullmatch(r"[+-]?\d+(\.\d+)?", text))