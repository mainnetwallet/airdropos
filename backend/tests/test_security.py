"""Future test suite: security boundaries, key vault isolation, and tool permissions.

NOT EXECUTED during the creation phase, per specification Part 37.
"""

import pytest

from backend.app.security.emergency_stop import EmergencyStopError, EmergencyStopController
from backend.app.tools.base import BaseTool, SideEffect, ToolResult, ToolValidationError
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry
from backend.app.wallets.vault import KeyVault, SigningDenied, SigningRequest


class DummyTool(BaseTool):
    name = "dummy_read"
    description = "A no-op tool for testing"
    required_permissions = ["test.read"]
    side_effect = SideEffect.NONE
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    async def execute(self, params):
        return ToolResult.success(data={"echo": params.get("value")})


class TestToolParameterStrictness:
    def test_unknown_parameters_are_rejected(self):
        """Structural defence against prompt-injection smuggling extra arguments."""
        tool = DummyTool()
        with pytest.raises(ToolValidationError):
            tool.validate_params({"value": "x", "system_override": True})

    def test_missing_required_parameter(self):
        tool = DummyTool()
        with pytest.raises(ToolValidationError):
            tool.validate_params({})

    def test_type_enforcement(self):
        tool = DummyTool()
        with pytest.raises(ToolValidationError):
            tool.validate_params({"value": 123})


class TestAuthorizationPolicy:
    @pytest.mark.asyncio
    async def test_tool_without_permission_is_refused(self):
        registry = ToolRegistry()
        registry.register(DummyTool())
        result = await registry.invoke("dummy_read", {"value": "a"}, policy=AuthorizationPolicy(enabled_permissions=[]))
        assert result.status.value == "not_authorized"

    @pytest.mark.asyncio
    async def test_tool_with_permission_runs(self):
        registry = ToolRegistry()
        registry.register(DummyTool())
        result = await registry.invoke("dummy_read", {"value": "a"}, policy=AuthorizationPolicy(enabled_permissions=["test.read"]))
        assert result.ok

    @pytest.mark.asyncio
    async def test_unregistered_tool_is_refused(self):
        registry = ToolRegistry()
        result = await registry.invoke("nonexistent", {}, policy=AuthorizationPolicy())
        assert result.status.value == "failure"


class TestEmergencyStop:
    def test_stop_blocks_signing_and_submission(self):
        controller = EmergencyStopController()
        assert controller.can_sign()
        controller.activate("test", "unit-test")
        assert not controller.can_sign()
        assert not controller.can_submit_transactions()
        assert not controller.can_execute_tasks()

    def test_resume_restores_capability(self):
        controller = EmergencyStopController()
        controller.activate("test", "unit-test")
        controller.resume("unit-test")
        assert controller.can_sign()

    def test_status_carries_disclaimer(self):
        controller = EmergencyStopController()
        controller.activate("test", "unit-test")
        assert "cannot be cancelled" in controller.status()["disclaimer"]


class TestVaultIsolation:
    def test_encryption_round_trip(self):
        vault = KeyVault("0" * 64)
        secret = "0x" + "ab" * 32
        ciphertext, iv, tag = vault.encrypt(secret)
        assert secret not in ciphertext
        assert vault.decrypt(ciphertext, iv, tag) == secret

    def test_wrong_key_cannot_decrypt(self):
        vault = KeyVault("0" * 64)
        other = KeyVault("1" * 64)
        ciphertext, iv, tag = vault.encrypt("0xsecret")
        with pytest.raises(Exception):
            other.decrypt(ciphertext, iv, tag)

    def test_forbidden_selector_is_refused(self):
        """A prompt-injected 'sign this ownership transfer' must be rejected."""
        vault = KeyVault("0" * 64)
        request = SigningRequest(
            wallet_address="0x" + "11" * 20,
            chain_id=1,
            transaction={"to": "0x" + "22" * 20, "data": "0xf2fde38b000000000000000000000000" + "33" * 20},
        )
        with pytest.raises(SigningDenied, match="restricted operation"):
            vault.validate_request(request, {request.wallet_address})

    def test_unsupported_chain_refused(self):
        vault = KeyVault("0" * 64)
        request = SigningRequest(
            wallet_address="0x" + "11" * 20,
            chain_id=999_999,
            message="hello",
        )
        with pytest.raises(SigningDenied, match="Unsupported chain_id"):
            vault.validate_request(request, {request.wallet_address})
