"""BNB Smart Chain ("bsc") is an EVM network the agent may name in a task."""

from backend.app.blockchain.chains import get_chain, known_networks
from backend.app.planning.planner import PlanConstraints
from backend.app.tools.registry import AuthorizationPolicy


def test_bsc_is_a_known_evm_network():
    assert "bsc" in known_networks()
    chain = get_chain("BSC")  # lookup is case-insensitive
    assert chain.chain_id == 56
    assert chain.native_symbol == "BNB"
    assert chain.rpc_setting == "RPC_BSC"
    assert not chain.is_testnet


def test_registry_permits_bsc_by_default():
    ok, reason = AuthorizationPolicy().permits_network("bsc")
    assert ok, reason


def test_planner_default_allows_bsc_and_follows_the_catalog():
    assert PlanConstraints().allowed_networks == set(known_networks())


def test_unknown_networks_are_still_refused():
    ok, _ = AuthorizationPolicy().permits_network("not-a-chain")
    assert not ok
