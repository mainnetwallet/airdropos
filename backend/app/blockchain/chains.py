"""EVM Chain Adapter: the single source of truth for networks, chain ids, and RPC.

Before this module the network list was written out in eight places -- settings,
`TransactionEngine.rpc_url`, the vault's `SUPPORTED_CHAIN_IDS`, the bootstrap
authorization policy, the registry's default policy, the worker's RPC sweep, and
five separate JSON-schema enums inside `blockchain_tools.py`. They agreed today.
Nothing kept them agreeing: adding a network meant editing eight sites, and a
missed edit produced a tool schema advertising a network the engine cannot reach.

Everything here is read-only. Adapters describe a chain and talk JSON-RPC; none
of them can sign or broadcast. Submission stays behind
`transactions.engine.TransactionEngine`, which is where the emergency stop and
spend limits live.

An adapter exists for a network only if it has a configured RPC endpoint. A
network with no endpoint is `is_configured() == False` and is refused by
`require_network()` rather than silently resolving to `None`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

DEFAULT_RPC_TIMEOUT_SECONDS = 20.0


class ChainError(Exception):
    """A chain could not be resolved or reached."""

    def __init__(self, message: str, category: str = "CHAIN") -> None:
        super().__init__(message)
        self.category = category


class UnknownNetworkError(ChainError):
    """The requested network is not a supported chain."""

    def __init__(self, network: str, known: Optional[List[str]] = None) -> None:
        super().__init__(
            f"Unknown network '{network}'. Known networks: {', '.join(known or known_networks())}",
            category="UNKNOWN_NETWORK",
        )


class RpcError(ChainError):
    """The RPC endpoint did not answer, or answered with an error."""

    def __init__(self, message: str, category: str = "RPC_FAILURE") -> None:
        super().__init__(message, category=category)


@dataclass(frozen=True)
class EvmChain:
    """An EVM-compatible chain the platform knows how to read.

    `native_symbol` is the gas token's ticker. Native-token USD prices are never
    stored here: the platform does not invent prices, so cost estimates report the
    native amount and leave the USD figure to an external feed.
    """

    name: str
    chain_id: int
    rpc_setting: str
    native_symbol: str
    explorer_url: Optional[str] = None
    is_testnet: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


# The supported chain set. This table is the authority; everything else derives
# from it. `rpc_setting` is the *attribute name* on `settings`, resolved lazily so
# this module has no import-time dependency on configuration.
CHAIN_CATALOG: Tuple[EvmChain, ...] = (
    EvmChain(
        name="ethereum",
        chain_id=1,
        rpc_setting="RPC_ETHEREUM_MAINNET",
        native_symbol="ETH",
        explorer_url="https://etherscan.io",
    ),
    EvmChain(
        name="arbitrum",
        chain_id=42161,
        rpc_setting="RPC_ARBITRUM_ONE",
        native_symbol="ETH",
        explorer_url="https://arbiscan.io",
    ),
    EvmChain(
        name="optimism",
        chain_id=10,
        rpc_setting="RPC_OPTIMISM",
        native_symbol="ETH",
        explorer_url="https://optimistic.etherscan.io",
    ),
    EvmChain(
        name="base",
        chain_id=8453,
        rpc_setting="RPC_BASE",
        native_symbol="ETH",
        explorer_url="https://basescan.org",
    ),
    EvmChain(
        name="polygon",
        chain_id=137,
        rpc_setting="RPC_POLYGON",
        native_symbol="POL",
        explorer_url="https://polygonscan.com",
    ),
    EvmChain(
        name="sepolia",
        chain_id=11155111,
        rpc_setting="RPC_SEPOLIA",
        native_symbol="ETH",
        is_testnet=True,
        explorer_url="https://sepolia.etherscan.io",
    ),
)

_BY_NAME: Dict[str, EvmChain] = {c.name: c for c in CHAIN_CATALOG}
_BY_ID: Dict[int, EvmChain] = {c.chain_id: c for c in CHAIN_CATALOG}


def known_networks() -> List[str]:
    """Every supported network name, in catalog order."""
    return [c.name for c in CHAIN_CATALOG]


def all_chain_ids() -> Dict[int, str]:
    """Chain id -> name, matching the shape the vault expects."""
    return dict(_BY_ID and {c.chain_id: c.name for c in CHAIN_CATALOG})


def get_chain(network: str) -> EvmChain:
    """Resolve a network by name. Raises `UnknownNetworkError` if unsupported."""
    try:
        return _BY_NAME[network.strip().lower()]
    except (AttributeError, KeyError):
        raise UnknownNetworkError(str(network)) from None


def get_chain_by_id(chain_id: int) -> Optional[EvmChain]:
    return _BY_ID.get(chain_id)


def chain_for_tx(network: str, chain_id: int) -> EvmChain:
    """Resolve and verify the two agree.

    A mismatch means one of them is wrong, and signing against the wrong chain id
    is how funds get sent to an unexpected place. Refusing is the whole point.
    """
    chain = get_chain(network)
    if chain.chain_id != chain_id:
        raise ChainError(
            f"Network '{chain.name}' is chain id {chain.chain_id}, not {chain_id}.",
            category="CHAIN_ID_MISMATCH",
        )
    return chain


class EvmChainAdapter:
    """Read-only JSON-RPC access to one EVM chain.

    Deliberately has no signing or broadcasting capability. `send_raw_transaction`
    is absent by design, not by omission -- broadcast must stay behind the
    transaction engine's emergency-stop and spend-limit guards.
    """

    def __init__(self, chain: EvmChain, timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS) -> None:
        self.chain = chain
        self.timeout_seconds = timeout_seconds
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.chain.name

    @property
    def chain_id(self) -> int:
        return self.chain.chain_id

    def rpc_url(self) -> Optional[str]:
        """The configured endpoint, or None when unset."""
        from backend.app.core.config import settings

        return getattr(settings, self.chain.rpc_setting, None)

    def is_configured(self) -> bool:
        return bool(self.rpc_url())

    def require_configured(self) -> str:
        url = self.rpc_url()
        if not url:
            raise ChainError(
                f"No RPC endpoint configured for '{self.chain.name}' "
                f"(set {self.chain.rpc_setting}).",
                category="NO_RPC",
            )
        return url

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def rpc(self, method: str, params: Optional[List[Any]] = None) -> Any:
        """Issue a single JSON-RPC call and return `result`.

        Any transport problem becomes `RpcError` so callers can distinguish an
        unreachable endpoint from a valid endpoint that returned null (which is a
        legitimate answer meaning "no such block/transaction/receipt yet").
        """
        url = self.require_configured()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        try:
            client = await self._get_client()
            response = await client.post(url, json=payload)
            body = response.json()
        except Exception as exc:
            # The endpoint URL is not echoed: it usually carries an API key.
            raise RpcError(
                f"RPC call '{method}' to '{self.chain.name}' failed: {type(exc).__name__}",
                category="RPC_FAILURE",
            ) from exc

        if not isinstance(body, dict):
            raise RpcError(
                f"RPC endpoint for '{self.chain.name}' returned a non-object body.",
                category="RPC_MALFORMED",
            )
        if "error" in body and body["error"] is not None:
            error = body["error"]
            message = error.get("message", "unknown") if isinstance(error, dict) else str(error)
            raise RpcError(
                f"RPC error on '{self.chain.name}' for {method}: {message}",
                category="RPC_ERROR",
            )
        return body.get("result")

    async def call(self, method: str, params: Optional[List[Any]] = None) -> Any:
        """Alias for `rpc`, named to read well at call sites."""
        return await self.rpc(method, params)

    # ------------------------------------------------------------------
    # Chain reads
    # ------------------------------------------------------------------
    async def block_number(self) -> int:
        result = await self.rpc("eth_blockNumber", [])
        try:
            return int(result, 16)
        except (TypeError, ValueError):
            raise RpcError(
                f"'{self.chain.name}' returned a non-hex block number.",
                category="RPC_MALFORMED",
            ) from None

    async def get_balance_wei(self, address: str) -> int:
        result = await self.rpc("eth_getBalance", [address, "latest"])
        try:
            return int(result, 16)
        except (TypeError, ValueError):
            raise RpcError(
                f"'{self.chain.name}' returned a non-hex balance.",
                category="RPC_MALFORMED",
            ) from None

    async def get_transaction_count(self, address: str, block: str = "pending") -> int:
        result = await self.rpc("eth_getTransactionCount", [address, block])
        try:
            return int(result, 16)
        except (TypeError, ValueError):
            raise RpcError(
                f"'{self.chain.name}' returned a non-hex transaction count.",
                category="RPC_MALFORMED",
            ) from None

    async def gas_price_wei(self) -> int:
        return int(await self.rpc("eth_gasPrice", []), 16)

    async def query_chain_id(self) -> int:
        """Ask the node which chain it serves.

        Named `query_chain_id` rather than `chain_id` because the attribute
        `chain_id` is already the local catalogue value. Defining both would
        leave the async method shadowing the property, so `adapter.chain_id` would
        return a bound method instead of `1`.

        Useful as a sanity check at startup: a misconfigured RPC that points at a
        different chain will answer, and finding out now is better than signing
        against the wrong chain id later.
        """
        return int(await self.rpc("eth_chainId", []), 16)

    async def is_reachable(self) -> bool:
        try:
            await self.block_number()
            return True
        except ChainError:
            return False

    async def health(self) -> Dict[str, Any]:
        """Probe result for the diagnostics watchdog. Never raises."""
        url_configured = self.is_configured()
        if not url_configured:
            return {
                "network": self.chain.name,
                "status": "NOT_CONFIGURED",
                "detail": f"{self.chain.rpc_setting} is unset.",
            }
        try:
            block = await self.block_number()
        except ChainError as exc:
            return {
                "network": self.chain.name,
                "status": "UNAVAILABLE",
                "detail": str(exc),
                "category": exc.category,
            }
        return {
            "network": self.chain.name,
            "status": "HEALTHY",
            "block": block,
            "chain_id": self.chain.chain_id,
            "native_symbol": self.chain.native_symbol,
        }


class ChainRegistry:
    """Resolves networks to adapters and caches the client per chain."""

    def __init__(self, timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self._adapters: Dict[str, EvmChainAdapter] = {}
        self._lock = asyncio.Lock()

    async def get(self, network: str) -> EvmChainAdapter:
        """Resolve a network to its adapter, creating it on first use."""
        chain = get_chain(network)
        async with self._lock:
            adapter = self._adapters.get(chain.name)
            if adapter is None:
                adapter = EvmChainAdapter(chain, timeout_seconds=self.timeout_seconds)
                self._adapters[chain.name] = adapter
            return adapter

    def require_configured(self, network: str) -> EvmChainAdapter:
        """Synchronous resolution that also asserts an endpoint is configured.

        Used on the request path where an unset RPC should be a clean refusal
        rather than a failed HTTP round trip.
        """
        chain = get_chain(network)
        adapter = self._adapters.get(chain.name)
        if adapter is None:
            adapter = EvmChainAdapter(chain, timeout_seconds=self.timeout_seconds)
            self._adapters[chain.name] = adapter
        adapter.require_configured()
        return adapter

    def configured_networks(self) -> List[str]:
        """Networks with an endpoint, in catalog order."""
        return [c.name for c in CHAIN_CATALOG if self._peek(c.name).is_configured()]

    def _peek(self, network: str) -> EvmChainAdapter:
        adapter = self._adapters.get(network)
        if adapter is None:
            adapter = EvmChainAdapter(
                _BY_NAME[network], timeout_seconds=self.timeout_seconds
            )
            self._adapters[network] = adapter
        return adapter

    async def health_sweep(self) -> Dict[str, Dict[str, Any]]:
        """Probe every catalogued chain concurrently for the watchdog."""
        adapters = [self._peek(c.name) for c in CHAIN_CATALOG]
        results = await asyncio.gather(
            *(a.health() for a in adapters), return_exceptions=True
        )
        sweep: Dict[str, Dict[str, Any]] = {}
        for adapter, result in zip(adapters, results):
            if isinstance(result, BaseException):
                # `health()` already swallows its own errors; this catches a bug
                # in the probe itself rather than letting one chain void the sweep.
                sweep[adapter.name] = {
                    "network": adapter.name,
                    "status": "UNAVAILABLE",
                    "detail": f"probe raised {type(result).__name__}",
                }
            else:
                sweep[adapter.name] = result
        return sweep

    async def aclose(self) -> None:
        for adapter in list(self._adapters.values()):
            await adapter.aclose()
        self._adapters.clear()


registry = ChainRegistry()


def known_network_enum() -> List[str]:
    """Values for a JSON-schema `enum`.

    Used to build tool input schemas from the catalog so a tool can never
    advertise a network the engine does not support.
    """
    return known_networks()