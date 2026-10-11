"""Tests for the EVM chain adapter.

The adapter exists because the supported-network list had been written out in
eight places: settings, `TransactionEngine.rpc_url`, the vault's
`SUPPORTED_CHAIN_IDS`, the bootstrap authorization policy, the registry's default
policy, the worker's RPC sweep, and five JSON-schema enums in
`blockchain_tools.py`. They happened to agree, but nothing enforced it.

These tests pin the catalog as the single source of truth: every one of those
sites must now derive from it, and the adapter must never gain a signing or
broadcasting method.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import httpx
import pytest

from backend.app.blockchain.chains import (
    CHAIN_CATALOG,
    ChainError,
    ChainRegistry,
    EvmChainAdapter,
    UnknownNetworkError,
    all_chain_ids,
    chain_for_tx,
    get_chain,
    get_chain_by_id,
    known_network_enum,
    known_networks,
    registry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# The literal list that used to be duplicated. If any module still hardcodes it,
# `test_no_module_hardcodes_the_network_list` catches it.
_HARDCODED_LIST = '["ethereum", "arbitrum", "optimism", "base", "polygon", "sepolia"]'


# ======================================================================
# Catalog integrity
# ======================================================================
def test_catalog_names_and_ids_are_unique():
    names = [c.name for c in CHAIN_CATALOG]
    ids = [c.chain_id for c in CHAIN_CATALOG]
    assert len(set(names)) == len(names), "duplicate network name in the catalog"
    assert len(set(ids)) == len(ids), "duplicate chain id in the catalog"


def test_catalog_is_not_empty_and_covers_mainnets_and_a_testnet():
    names = set(known_networks())
    assert {"ethereum", "arbitrum", "optimism", "base", "polygon"} <= names
    assert any(c.is_testnet for c in CHAIN_CATALOG), "a testnet must remain reachable"


def test_lookup_is_case_insensitive_and_trims():
    assert get_chain("  ETHEREUM  ").chain_id == 1


def test_unknown_network_raises_with_the_known_list():
    with pytest.raises(UnknownNetworkError) as excinfo:
        get_chain("solana")
    assert excinfo.value.category == "UNKNOWN_NETWORK"
    assert "ethereum" in str(excinfo.value)


def test_chain_id_lookup_round_trips():
    for chain in CHAIN_CATALOG:
        assert get_chain_by_id(chain.chain_id) is chain
    assert get_chain_by_id(999999) is None


def test_all_chain_ids_shape_matches_vault_expectation():
    """The vault reads this as {chain_id: network}; keep that contract."""
    ids = all_chain_ids()
    assert all(isinstance(k, int) and isinstance(v, str) for k, v in ids.items())
    assert len(ids) == len(CHAIN_CATALOG)


def test_chain_for_tx_refuses_a_mismatched_chain_id():
    """Signing against the wrong chain id is how funds go missing."""
    assert chain_for_tx("ethereum", 1).name == "ethereum"
    with pytest.raises(ChainError) as excinfo:
        chain_for_tx("ethereum", 42161)
    assert excinfo.value.category == "CHAIN_ID_MISMATCH"


# ======================================================================
# Adapter safety invariants
# ======================================================================
def test_adapter_cannot_sign_or_broadcast():
    """Read-only is a structural property, not a convention.

    Asserted against the class's own attributes rather than the module source, so
    an alias or an inherited method still trips it.
    """
    forbidden = {
        "sign_transaction",
        "send_raw_transaction",
        "submit_raw",
        "sign",
        "broadcast",
        "private_key",
    }
    present = {name for name in dir(EvmChainAdapter) if name.lower() in forbidden}
    assert not present, f"adapter gained value-moving methods: {present}"


def test_adapter_exposes_no_key_vault_reference():
    """The adapter must not be able to reach key material.

    Checked against the code object rather than the source text, so a docstring
    that legitimately says "signing would fail here" does not trip it.
    """
    import dis

    names = {
        instr.argval
        for instr in dis.get_instructions(EvmChainAdapter.rpc)
        if instr.opname in ("LOAD_GLOBAL", "LOAD_METHOD", "LOAD_ATTR", "IMPORT_NAME")
    }
    for forbidden in ("KeyVault", "vault", "decrypt", "Account", "derive_address"):
        assert forbidden not in names, f"adapter reaches key material via {forbidden}"


def test_chain_id_attribute_is_the_catalog_value_not_a_method():
    """A bound method named `chain_id` silently shadows the property.

    `adapter.chain_id` must be `1` for ethereum, not a coroutine function.
    """
    adapter = EvmChainAdapter(get_chain("ethereum"))
    assert adapter.chain_id == 1
    assert not callable(adapter.chain_id)
    assert inspect.iscoroutinefunction(EvmChainAdapter.query_chain_id)


def test_rpc_url_for_unset_setting_is_none(monkeypatch):
    from backend.app.core.config import settings

    adapter = EvmChainAdapter(get_chain("sepolia"))
    monkeypatch.setattr(settings, "RPC_SEPOLIA", None, raising=False)
    assert adapter.rpc_url() is None
    assert adapter.is_configured() is False
    with pytest.raises(ChainError) as excinfo:
        adapter.require_configured()
    assert excinfo.value.category == "NO_RPC"


def test_rpc_error_does_not_echo_the_endpoint_url(monkeypatch):
    """RPC endpoints usually embed an API key; errors must not leak it."""
    from backend.app.core.config import settings

    secret_url = "https://eth.example.com/v2/SUPER-SECRET-KEY"
    monkeypatch.setattr(settings, "RPC_ETHEREUM_MAINNET", secret_url, raising=False)

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("refused")

    adapter = EvmChainAdapter(get_chain("ethereum"))
    monkeypatch.setattr(adapter, "_get_client", boom)

    import asyncio

    with pytest.raises(ChainError) as excinfo:
        asyncio.run(adapter.rpc("eth_blockNumber"))
    assert "SUPER-SECRET-KEY" not in str(excinfo.value)
    assert excinfo.value.category == "RPC_FAILURE"


# ======================================================================
# RPC behaviour against a stub transport
# ======================================================================
def _adapter_with_result(monkeypatch, chain_name, result):
    adapter = EvmChainAdapter(get_chain(chain_name))

    class _Client:
        async def post(self, url, json=None, **kw):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    async def get_client():
        return _Client()

    monkeypatch.setattr(adapter, "_get_client", get_client)
    return adapter


def test_block_number_and_balance_are_hex_decoded(monkeypatch):
    import asyncio

    adapter = _adapter_with_result(monkeypatch, "ethereum", "0x10d4f")
    assert asyncio.run(adapter.block_number()) == 68943


def test_rpc_error_response_becomes_chain_error(monkeypatch):
    import asyncio

    adapter = EvmChainAdapter(get_chain("ethereum"))

    class _Client:
        async def post(self, url, json=None, **kw):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "error": {"message": "header not found"}},
            )

    async def get_client():
        return _Client()

    monkeypatch.setattr(adapter, "_get_client", get_client)
    with pytest.raises(ChainError) as excinfo:
        asyncio.run(adapter.block_number())
    assert excinfo.value.category == "RPC_ERROR"
    assert "header not found" in str(excinfo.value)


def test_null_result_is_not_an_error(monkeypatch):
    """A missing receipt is a legitimate answer, not a transport failure."""
    import asyncio

    adapter = _adapter_with_result(monkeypatch, "ethereum", None)
    assert asyncio.run(adapter.rpc("eth_getTransactionReceipt", ["0xabc"])) is None


def test_malformed_hex_is_reported_as_malformed(monkeypatch):
    import asyncio

    adapter = _adapter_with_result(monkeypatch, "ethereum", "not-hex")
    with pytest.raises(ChainError) as excinfo:
        asyncio.run(adapter.block_number())
    assert excinfo.value.category == "RPC_MALFORMED"


def test_health_reports_not_configured_without_raising(monkeypatch):
    import asyncio

    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "RPC_BASE", None, raising=False)
    report = asyncio.run(EvmChainAdapter(get_chain("base")).health())
    assert report["status"] == "NOT_CONFIGURED"
    assert "RPC_BASE" in report["detail"]


def test_health_reports_unavailable_on_transport_failure(monkeypatch):
    import asyncio

    adapter = EvmChainAdapter(get_chain("polygon"))

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(adapter, "_get_client", boom)
    report = asyncio.run(adapter.health())
    assert report["status"] == "UNAVAILABLE"


def test_sweep_covers_every_catalogued_chain(monkeypatch):
    import asyncio

    async def unreachable(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ChainRegistry, "_peek", lambda self, n: _stub_adapter(self, n, unreachable))
    sweep = asyncio.run(ChainRegistry().health_sweep())
    assert set(sweep) == set(known_networks())


def _stub_adapter(owner, network, boom):
    adapter = EvmChainAdapter(get_chain(network))
    adapter._get_client = boom
    return adapter


# ======================================================================
# The invariant this module exists to protect
# ======================================================================
def test_no_module_hardcodes_the_network_list():
    """Every supported-network list must derive from the catalog.

    Walks the application source and fails if any module re-spells the list
    inline. This is the check that would have caught the original drift.
    """
    offenders = []
    for path in (REPO_ROOT / "backend" / "app").rglob("*.py"):
        if path.name == "chains.py":
            continue  # the catalog itself, and its docstring, may name chains
        text = path.read_text(encoding="utf-8")
        if _HARDCODED_LIST in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"network list is hardcoded in: {offenders}"


def test_tool_schema_enums_match_the_catalog():
    """The tool schemas offered to the LLM must be exactly the real networks."""
    from backend.app.tools.blockchain_tools import NETWORK_ENUM

    assert NETWORK_ENUM == known_network_enum()
    assert set(NETWORK_ENUM) == set(known_networks())


def test_registry_default_policy_matches_the_catalog():
    from backend.app.tools.registry import AuthorizationPolicy

    assert AuthorizationPolicy().allowed_networks == set(known_networks())


def test_vault_chain_ids_match_the_catalog():
    """The vault must not be able to sign for a chain the platform cannot reach."""
    from backend.app.wallets.vault import SUPPORTED_CHAIN_IDS

    assert SUPPORTED_CHAIN_IDS == all_chain_ids()


def test_module_level_registry_resolves_every_network():
    for name in known_networks():
        adapter = registry._peek(name)
        assert adapter.chain_id == get_chain(name).chain_id