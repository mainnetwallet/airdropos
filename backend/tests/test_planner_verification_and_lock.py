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


def test_telegram_sentences_map_to_telegram_methods():
    assert normalize_verification_method(
        "check that recent messages and inline keyboard buttons are retrieved"
    ) == "message_observed"
    assert normalize_verification_method(
        "bot messages read and message id with 'continue' button identified"
    ) == "message_observed"
    assert normalize_verification_method("continue button was clicked") == "button_clicked"
    # names both a click and a message read: ambiguous, never guessed
    text = "continue button clicked and bot messages show the next step"
    assert normalize_verification_method(text) == text
