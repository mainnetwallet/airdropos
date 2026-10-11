"""Future test suite: single fixed LLM enforcement and memory secret screening.

NOT EXECUTED during the creation phase, per specification Part 37.
"""

import pytest

from backend.app.llm.client import FixedLLMClient, LLMError, LLMPolicyError
from backend.app.memory.manager import MemoryManager, SecretLeakError


class TestSingleModelEnforcement:
    def test_model_switching_is_rejected(self):
        """There is exactly one model. Asking for another must raise."""
        client = FixedLLMClient()
        with pytest.raises(LLMPolicyError):
            client.assert_model_locked("gpt-3.5-turbo")

    def test_locked_model_is_accepted(self):
        client = FixedLLMClient()
        client.assert_model_locked(client.model_id)  # must not raise

    def test_describe_never_exposes_api_key(self):
        client = FixedLLMClient()
        described = client.describe()
        assert "api_key" not in described
        assert described["api_key_masked"] != described.get("api_key")
        assert client.api_key not in str(described)

    def test_policy_field_declares_single_model(self):
        assert FixedLLMClient().describe()["policy"] == "single_fixed_model"


class TestJsonParsing:
    def test_plain_json_parses(self):
        assert FixedLLMClient.parse_json('{"a": 1}') == {"a": 1}

    def test_fenced_json_parses(self):
        assert FixedLLMClient.parse_json('```json\n{"a": 2}\n```') == {"a": 2}

    def test_prose_raises(self):
        with pytest.raises(LLMError):
            FixedLLMClient.parse_json("Sure! Here is what I found: nothing useful.")

    def test_empty_raises(self):
        with pytest.raises(LLMError):
            FixedLLMClient.parse_json("")


class TestMemorySecretScreening:
    @pytest.mark.parametrize(
        "content",
        [
            "the seed phrase is correct horse battery staple",
            "private_key=0x1234",
            "my api_key: sk-abcdef1234567890abcdef",
        ],
    )
    def test_secret_content_is_rejected(self, content):
        with pytest.raises(SecretLeakError):
            MemoryManager.assert_no_secrets(content)

    def test_ordinary_content_is_allowed(self):
        MemoryManager.assert_no_secrets("Project requires 5 transactions on Arbitrum before snapshot.")
