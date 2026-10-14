"""Three edge cases found reviewing 33c24d7, each proven before it was fixed.

**1. Nested mapping keys were not screened.** `_screen_mapping_key` was applied only at
the root of `_screened_dict`; the dictionary branch inside the recursive walker still did
`screened[str(key)[:80]]`, so a secret one level down was written verbatim.

**2. Submitted parameters counted as proof of an outcome.** The proof text was a
concatenation of the observed state, the returned result *and* the submitted parameters.
So `hold 100 tokens` was satisfied by submitting `{"amount": "100 tokens"}` while the
observed balance read `50 tokens` - the attempted amount was read back as the achieved
one. Sending an amount is not holding it.

**3. Decimal quantities were inflated.** A trailing three-digit group after a point was
treated as a thousands separator, so `0.500 tokens` parsed as **500** and `1.250 tokens`
as **1250**. Crypto balances are fractional by nature; a wallet holding 0.5 tokens would
have satisfied "hold 100 tokens". The heuristic is removed rather than refined - there is
no format it gets both right.

Nothing here contacts Telegram, a chain, a browser or an LLM.
"""

from __future__ import annotations

import pytest

from backend.app.agent.context import (
    TaskContextService,
    _extract_identifiers,
    _extract_quantities,
    _quantities_met,
    _screen_value,
    REDACTION_MARKER,
)

API_KEY = "api_key: sk-live-0123456789abcdefghij"
PRIVATE_KEY = "0x" + "a" * 64


class TestNestedKeysAreScreened:
    def test_a_secret_nested_key_is_redacted(self):
        """The root key was screened; a key one level down was not."""
        result = _screen_value({"metadata": {API_KEY: "value"}}, limit=500)

        assert API_KEY not in str(result)
        assert REDACTION_MARKER in str(result)

    def test_a_secret_deeply_nested_key_is_redacted(self):
        result = _screen_value(
            {"a": {"b": {"c": {"d": {API_KEY: "value"}}}}}, limit=500
        )
        assert API_KEY not in str(result)

    def test_an_ordinary_nested_key_is_untouched(self):
        result = _screen_value({"metadata": {"page_url": "https://x.org"}}, limit=500)
        assert result == {"metadata": {"page_url": "https://x.org"}}

    def test_the_dict_helper_screens_nested_keys_too(self):
        result = TaskContextService._screened_dict({"outer": {PRIVATE_KEY: "v"}})
        assert PRIVATE_KEY not in str(result)


class TestDecimalQuantitiesAreNotInflated:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("0.500 tokens", 0.5),
            ("1.250 tokens", 1.25),
            ("1,000 tokens", 1000.0),
            ("1,000.500 tokens", 1000.5),
            ("100 tokens", 100.0),
            ("0.0001 eth", 0.0001),
            ("12.5 usd", 12.5),
        ],
    )
    def test_the_number_is_read_as_written(self, text, expected):
        parsed = _extract_quantities(text)
        assert parsed, f"{text!r} produced no quantity at all"
        assert parsed[0][0] == pytest.approx(expected), (
            f"{text!r} parsed as {parsed[0][0]}, expected {expected}. Inflating a decimal "
            "turns an insufficient balance into a satisfied requirement."
        )

    def test_a_tiny_balance_cannot_meet_a_large_threshold(self):
        """The concrete harm: 0.5 tokens must not satisfy 'hold 100 tokens'."""
        assert _quantities_met(
            [(100.0, "token")], _extract_quantities("balance 0.500 tokens")
        ) is False

    def test_a_sub_token_balance_does_not_meet_a_sub_token_threshold(self):
        assert _quantities_met(
            [(0.1, "token")], _extract_quantities("balance 0.0500 tokens")
        ) is False

    def test_a_sufficient_fractional_balance_does_meet_it(self):
        assert _quantities_met(
            [(0.1, "token")], _extract_quantities("balance 0.5000 tokens")
        ) is True

    def test_units_still_matter(self):
        assert _quantities_met(
            [(100.0, "token")], _extract_quantities("balance 500 usdt")
        ) is False


class TestSubmittedParametersAreNotProof:
    def test_an_attempted_amount_does_not_meet_a_holding_threshold(self):
        """Submitting 100 is not holding 100 when the observed balance reads 50."""
        submitted = _extract_quantities('{"amount": "100 tokens"}')
        observed = _extract_quantities('{"balance": "50 tokens"}')

        assert _quantities_met([(100.0, "token")], submitted) is True, (
            "the submitted amount alone would satisfy it - which is the whole bug"
        )
        assert _quantities_met([(100.0, "token")], observed) is False, (
            "what the wallet actually holds does not meet the threshold"
        )

    def test_a_concatenated_proof_is_not_authoritative(self):
        """Why the concatenation was wrong, stated as a test.

        Merging intent and outcome lets the intent supply the number the outcome denies.
        """
        combined = _extract_quantities('{"balance": "50 tokens"} {"amount": "100 tokens"}')
        assert _quantities_met([(100.0, "token")], combined) is True
        assert _quantities_met(
            [(100.0, "token")], _extract_quantities('{"balance": "50 tokens"}')
        ) is False, (
            "with intent excluded the requirement correctly stays outstanding; the "
            "concatenation must not be used for proof"
        )


class TestIdentifiersAreUnaffected:
    def test_handles_still_parse(self):
        assert _extract_identifiers("join @examplechannel") == ["examplechannel"]
        assert _extract_identifiers("join @otherchannel") != _extract_identifiers(
            "join @examplechannel"
        )

    def test_a_handle_matches_whether_or_not_it_carries_the_sigil(self):
        """What the tools actually return.

        `telegram_join_chat` resolves the chat and reports `username` WITHOUT the `@`,
        while the requirement a bot states is written with it. Matching the two forms is
        what lets a genuine join close its own requirement at all.
        """
        requirement = _extract_identifiers("Join Telegram channel @examplechannel")
        tool_result = _extract_identifiers('{"username": "examplechannel", "chat_id": 4242}')

        assert requirement == ["examplechannel"]
        assert set(requirement).issubset(set(tool_result)), (
            "a successful join must close the requirement it was made for"
        )

    def test_the_sigil_does_not_make_two_channels_the_same(self):
        assert not set(_extract_identifiers("@examplechannel")).intersection(
            set(_extract_identifiers("@otherchannel"))
        )