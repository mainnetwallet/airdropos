"""Base Tool Interface with Declarative Schema & Permission Metadata.

Every capability the LLM may invoke is a registered Tool. Tools declare their
input schema, required permissions, side-effect class, and idempotency. The
registry validates every invocation before execution.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence


class SideEffect(str, Enum):
    """Classification that drives confirmation and emergency-stop policy."""

    NONE = "none"                # Pure read. Safe.
    EXTERNAL_READ = "external_read"  # Reads a remote service. Safe but consumes quota.
    WEB_INTERACTION = "web_interaction"  # Clicks/fills forms. Cannot be undone silently.
    WALLET_SIGNING = "wallet_signing"  # Requires vault access. Subject to strict limits.
    TRANSACTION_SUBMISSION = "transaction_submission"  # Broadcasts value. Irreversible.
    STATE_MUTATION = "state_mutation"  # Modifies AirdropOS data.


class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEEDS_USER_ACTION = "needs_user_action"  # CAPTCHA, MFA, hardware wallet, expiry.
    NOT_AUTHORIZED = "not_authorized"
    UNAVAILABLE = "unavailable"  # Dependency down; do not keep dispatching work here.


@dataclass
class ToolResult:
    """Uniform result envelope so the agent never guesses at tool semantics."""

    status: ToolResultStatus
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_category: Optional[str] = None
    retryable: bool = False
    duration_ms: int = 0
    evidence: Optional[Dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        return self.status is ToolResultStatus.SUCCESS

    @classmethod
    def success(cls, data: Optional[Dict[str, Any]] = None, **kwargs: Any) -> "ToolResult":
        return cls(status=ToolResultStatus.SUCCESS, data=data or {}, **kwargs)

    @classmethod
    def failure(
        cls,
        error: str,
        category: str = "TOOL_FAILURE",
        retryable: bool = False,
        **kwargs: Any,
    ) -> "ToolResult":
        return cls(
            status=ToolResultStatus.FAILURE,
            error=error,
            error_category=category,
            retryable=retryable,
            **kwargs,
        )

    @classmethod
    def needs_user(cls, reason: str, **kwargs: Any) -> "ToolResult":
        return cls(
            status=ToolResultStatus.NEEDS_USER_ACTION,
            error=reason,
            error_category="HUMAN_INTERVENTION_REQUIRED",
            retryable=False,
            **kwargs,
        )


class ToolValidationError(Exception):
    """Raised when caller-supplied arguments violate the tool's schema."""


class BaseTool(ABC):
    """Abstract base for every registered capability."""

    name: str = "base_tool"
    version: str = "1.0.0"
    description: str = ""

    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    output_schema: Dict[str, Any] = {"type": "object"}

    # BaseTool is not a dataclass, so dataclasses.field() here would leave a raw
    # Field object on every subclass that does not override it (which broke
    # GET /tools serialization). Immutable tuples are safe shared defaults.
    required_permissions: Sequence[str] = ()
    supported_networks: Sequence[str] = ()
    side_effect: SideEffect = SideEffect.NONE
    timeout_seconds: int = 60
    max_retries: int = 2
    is_idempotent: bool = True

    # ------------------------------------------------------------------
    @abstractmethod
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Perform the tool's work. Implementations return ToolResult, never raise."""

    # ------------------------------------------------------------------
    def validate_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Lightweight JSON-schema subset validation. Rejects unknown keys loudly.

        Strictness matters: an unexpected parameter may indicate a prompt
        injection attempting to smuggle extra instructions into a tool call.
        """
        params = params or {}
        if not isinstance(params, dict):
            raise ToolValidationError("Tool parameters must be an object")

        schema_props: Dict[str, Any] = self.input_schema.get("properties", {})
        required: List[str] = self.input_schema.get("required", [])

        missing = [key for key in required if key not in params or params[key] in (None, "")]
        if missing:
            raise ToolValidationError(
                f"Tool '{self.name}' missing required parameter(s): {', '.join(missing)}"
            )

        unexpected = [key for key in params if key not in schema_props]
        if unexpected:
            raise ToolValidationError(
                f"Tool '{self.name}' received unexpected parameter(s): {', '.join(unexpected)}. "
                f"Accepted: {sorted(schema_props) or 'none'}"
            )

        validated: Dict[str, Any] = {}
        for key, value in params.items():
            spec = schema_props.get(key, {})
            expected_type = spec.get("type")

            if expected_type == "string" and not isinstance(value, str):
                raise ToolValidationError(f"Parameter '{key}' must be a string")
            if expected_type == "integer" and not isinstance(value, int):
                raise ToolValidationError(f"Parameter '{key}' must be an integer")
            if expected_type == "number" and not isinstance(value, (int, float)):
                raise ToolValidationError(f"Parameter '{key}' must be a number")
            if expected_type == "boolean" and not isinstance(value, bool):
                raise ToolValidationError(f"Parameter '{key}' must be a boolean")
            if expected_type == "array" and not isinstance(value, list):
                raise ToolValidationError(f"Parameter '{key}' must be an array")
            if expected_type == "object" and not isinstance(value, dict):
                raise ToolValidationError(f"Parameter '{key}' must be an object")

            if "enum" in spec and value not in spec["enum"]:
                raise ToolValidationError(
                    f"Parameter '{key}' must be one of {spec['enum']}, got {value!r}"
                )
            if "maximum" in spec and isinstance(value, (int, float)) and value > spec["maximum"]:
                raise ToolValidationError(f"Parameter '{key}' exceeds maximum {spec['maximum']}")
            if "minimum" in spec and isinstance(value, (int, float)) and value < spec["minimum"]:
                raise ToolValidationError(f"Parameter '{key}' below minimum {spec['minimum']}")
            if "maxLength" in spec and isinstance(value, str) and len(value) > spec["maxLength"]:
                raise ToolValidationError(f"Parameter '{key}' exceeds maxLength {spec['maxLength']}")

            validated[key] = value

        return validated

    def describe(self) -> Dict[str, Any]:
        """Full registry descriptor. This is what the LLM sees when choosing tools."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "required_permissions": list(self.required_permissions),
            "supported_networks": list(self.supported_networks),
            "side_effect": self.side_effect.value,
            "timeout_seconds": self.timeout_seconds,
            "is_idempotent": self.is_idempotent,
        }

    def summary_for_llm(self) -> str:
        """Compact one-line-per-field description to keep prompts small."""
        props = self.input_schema.get("properties", {})
        required = set(self.input_schema.get("required", []))
        arg_docs = []
        for key, spec in props.items():
            flag = "required" if key in required else "optional"
            desc = spec.get("description", spec.get("type", "any"))
            arg_docs.append(f"{key} ({desc}, {flag})")
        return (
            f"{self.name} v{self.version}: {self.description}\n"
            f"  Args: {'; '.join(arg_docs) or 'none'}\n"
            f"  Side effects: {self.side_effect.value}; permissions: "
            f"{', '.join(self.required_permissions) or 'none'}"
        )
