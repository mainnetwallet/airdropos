"""Planner: free-text verification methods and DB lock hygiene around LLM calls."""

from unittest.mock import AsyncMock

import pytest

from backend.app.planning.planner import Planner, normalize_verification_method


def test_wallet_sentence_maps_to_wallet_record():
    text = "verify that get_wallet_address returns a valid 0x public evm address for label x"
    assert normalize_verification_method(text) == "wallet_record"


def test_known_methods_pass_through_lowercased():
    assert normalize_verification_method("Chain_Receipt") == "chain_receipt"
    assert normalize_verification_method("tool_evidence") == "tool_evidence"


def test_empty_is_unspecified():
    assert normalize_verification_method(None) == "unspecified"
    assert normalize_verification_method("  ") == "unspecified"


def test_ambiguous_text_is_left_unmapped():
    # Names two different methods: never guess, so the controller keeps it UNVERIFIED.
    text = "screenshot of page and http status"
    assert normalize_verification_method(text) == text


def test_unrecognised_text_is_left_unmapped():
    text = "the airdrop looks claimed"
    assert normalize_verification_method(text) == text


@pytest.mark.asyncio
async def test_planner_commits_before_llm_call():
    db = AsyncMock()
    planner = Planner(db)
    await planner._release_write_lock()
    db.commit.assert_awaited_once()
