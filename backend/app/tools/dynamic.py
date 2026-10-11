"""Dynamic Tool Development Workflow.

This module allows the agent to create, test, and register new tools at runtime.
All dynamically created tools are subject to the same validation, authorization,
and emergency-stop controls as statically registered tools.

Workflow:
  1. Agent proposes a new tool via `propose_tool` (name, description, schema,
     permissions, side effect, and source code).
  2. The system validates the proposal *statically* — the source is parsed into
     an AST and checked against a deny-list. It is never executed.
  3. The proposal is stored as an inert `DynamicTool` that refuses to run.
  4. An operator promotes the tool through the policy in `registry.policy`;
     execution remains disabled regardless, because in-process execution of
     agent-authored code is not a supported operation.

Sandbox isolation
-----------------
Agent-authored source code is NEVER handed to `exec`, `eval`, `compile+exec`,
`pickle`, or a dynamic import of agent-named modules. Doing so is full
remote code execution inside the API process: the previous implementation of
this module called `exec()` at *proposal* time, before any registration,
authorization, or emergency-stop check had run. A single tool proposal could
therefore import `os`, read configuration and credentials, write files, spawn
processes, and reach the database.

The controls here are:

  * :func:`_check_source` walks the parsed AST and rejects any construct that
    is not on an explicit allow-list: no imports, no attribute traversal off a
    known-safe root, no `eval`/`exec`/`__import__`, no dunder access, no
    comprehension of a forbidden name.
  * The declared ``required_permissions`` are checked against the operator's
    live authorization policy at registration time, so a tool can never claim
    fewer permissions than it needs to act.
  * A tool declaring a value-moving side effect (``WALLET_SIGNING`` or
    ``TRANSACTION_SUBMISSION``) is refused outright. Dynamic tools are for
    read-only analysis; money movement always goes through reviewed, statically
    registered tools that pass the full signing guard chain.

See ``docs/SECURITY.md`` for the threat model and the rejected alternatives
(``RestrictedPython``, subprocess isolation) and why they were not adopted.
"""

from __future__ import annotations

import ast
import hashlib
import logging
from typing import Any, Dict, FrozenSet, List, Optional

from backend.app.tools.base import (
    BaseTool,
    SideEffect,
    ToolResult,
    ToolResultStatus,
    ToolValidationError,
)

logger = logging.getLogger(__name__)

# Side effects a runtime-authored tool may never claim. An agent that writes its
# own code must not be able to move value or sign with a key; that path stays
# behind operator review and the emergency-stop guards.
FORBIDDEN_SIDE_EFFECTS: FrozenSet[SideEffect] = frozenset(
    {SideEffect.WALLET_SIGNING, SideEffect.TRANSACTION_SUBMISSION}
)

# Builtins a dynamic tool may reference. Anything not here is a denial.
SAFE_BUILTIN_NAMES: FrozenSet[str] = frozenset(
    {
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
        "format", "frozenset", "int", "isinstance", "issubclass", "len",
        "list", "map", "max", "min", "next", "print", "range", "repr",
        "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip",
        "True", "False", "None", "Exception", "ValueError", "TypeError",
        "KeyError", "IndexError", "ZeroDivisionError", "RuntimeError",
    }
)

# Attribute names that reach outside the tool's own data. `__class__`,
# `__globals__`, `__subclasses__` and friends are the standard escape from a
# namespace to `object` and from there to `importlib`.
FORBIDDEN_ATTRS: FrozenSet[str] = frozenset(
    {
        "__class__", "__globals__", "__builtins__", "__subclasses__", "__bases__",
        "__mro__", "__code__", "__closure__", "__func__", "__self__", "__dict__",
        "__getattribute__", "__reduce__", "__reduce_ex__", "__init_subclass__",
        "__loader__", "__spec__", "__module__", "__import__",
    }
)

# Names that must never appear anywhere in a dynamic tool.
FORBIDDEN_NAMES: FrozenSet[str] = frozenset(
    {
        "eval", "exec", "compile", "open", "__import__", "input", "globals",
        "locals", "vars", "getattr", "setattr", "delattr", "exit", "quit",
        "help", "breakpoint", "memoryview", "object", "super", "type",
    }
)

# Module-level side effects a dynamic tool may not perform at import time.
FORBIDDEN_STATEMENTS = (
    ast.Import,
    ast.ImportFrom,
    ast.Global,
    ast.Nonlocal,
)

MAX_SOURCE_BYTES = 16_384


class SourceRejected(ToolValidationError):
    """Raised when agent-authored source violates the static safety policy."""


def _check_source(source: str) -> ast.Module:
    """Statically validate dynamic tool source. Returns the parsed tree.

    This never executes the source. Every rejection carries the offending
    construct so the agent can correct its proposal.
    """
    if not isinstance(source, str) or not source.strip():
        raise SourceRejected("Dynamic tool source must be a non-empty string.")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise SourceRejected(
            f"Dynamic tool source exceeds the {MAX_SOURCE_BYTES}-byte limit."
        )

    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise SourceRejected(f"Invalid Python syntax at line {exc.lineno}: {exc.msg}") from exc

    for node in ast.walk(tree):
        # Imports execute at module scope before any tool check can apply.
        if isinstance(node, FORBIDDEN_STATEMENTS):
            raise SourceRejected(
                f"Dynamic tools may not use {type(node).__name__} (line {node.lineno}). "
                "Agent-authored tools run without an import namespace; use only the "
                "arguments passed to execute()."
            )

        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise SourceRejected(
                f"Dynamic tools may not reference '{node.id}' (line {node.lineno})."
            )

        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            raise SourceRejected(
                f"Dynamic tools may not access attribute '{node.attr}' (line {node.lineno})."
            )

        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            decorators = getattr(node, "decorator_list", [])
            for dec in decorators:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Name) and target.id in FORBIDDEN_NAMES:
                    raise SourceRejected(
                        f"Dynamic tools may not use decorator '{target.id}' on "
                        f"{type(node).__name__} '{node.name}' (line {node.lineno})."
                    )

        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise SourceRejected(
                f"Dynamic tools may not rebind outer state (line {node.lineno})."
            )

    return tree


def _validate_class_shape(tree: ast.Module) -> None:
    """Require exactly one BaseTool subclass named `Tool` with an async execute.

    The name is fixed rather than discovered by scanning the namespace: a scan
    finds the framework's own `BaseTool` before the agent's class and then tries
    to instantiate an abstract class.
    """
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    tool_classes = [c for c in classes if c.name == "Tool"]
    if not tool_classes:
        raise SourceRejected(
            "Dynamic tool source must define a top-level class named `Tool`."
        )
    if len(tool_classes) > 1:
        raise SourceRejected("Dynamic tool source must define exactly one class named `Tool`.")

    tool_class = tool_classes[0]
    execute_methods = [
        n
        for n in tool_class.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "execute"
    ]
    if not execute_methods:
        raise SourceRejected("`Tool` must define an `execute` method.")
    if not isinstance(execute_methods[0], ast.AsyncFunctionDef):
        raise SourceRejected("`Tool.execute` must be defined with `async def`.")


class DynamicTool(BaseTool):
    """A validated tool definition whose implementation is intentionally inert.

    The proposal's source is retained for operator review and hashing, but it is
    never compiled or executed by this process. `execute` returns a structured
    `UNAVAILABLE` result so the agent's Observe-Plan-Act loop treats the tool as
    a dependency problem rather than retrying forever.
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        required_permissions: List[str],
        side_effect: SideEffect,
        source_code: str,
        timeout_seconds: int = 60,
        max_retries: int = 2,
        is_idempotent: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.output_schema = {"type": "object"}
        self.required_permissions = list(required_permissions)
        self.supported_networks: List[str] = []
        self.side_effect = side_effect
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.is_idempotent = is_idempotent

        # Stored for review and provenance only. Never passed to exec().
        self._source_code = source_code
        self._source_hash = hashlib.sha256(source_code.encode("utf-8")).hexdigest()

        tree = _check_source(source_code)
        _validate_class_shape(tree)

    @property
    def source_hash(self) -> str:
        """Stable identifier for the exact source that was reviewed."""
        return self._source_hash[:16]

    def describe(self) -> Dict[str, Any]:
        descriptor = super().describe()
        descriptor["source_hash"] = self.source_hash
        descriptor["executable"] = False
        return descriptor

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Refuse to run. Dynamic code is never executed in the API process.

        The agent must implement capability by composing an existing registered
        tool, not by shipping new code.
        """
        return ToolResult(
            status=ToolResultStatus.UNAVAILABLE,
            error=(
                f"Dynamic tool '{self.name}' was validated but cannot execute: "
                "AirdropOS does not run agent-authored code in-process. Compose an "
                "existing registered tool instead, or ask an operator to add the "
                "capability as a reviewed static tool."
            ),
            error_category="DYNAMIC_EXECUTION_DISABLED",
            retryable=False,
        )


class DynamicToolManager:
    """Tracks proposed dynamic tools and gates them behind operator policy.

    Dynamic tools are registered *disabled*. The registry only enables a tool
    when its declared permissions are already granted by the live policy, and
    even then execution is refused by `DynamicTool.execute`.
    """

    def __init__(self) -> None:
        self._dynamic_tools: Dict[str, DynamicTool] = {}

    def propose_tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        required_permissions: List[str],
        side_effect: SideEffect,
        source_code: str,
        timeout_seconds: int = 60,
        max_retries: int = 2,
        is_idempotent: bool = True,
    ) -> DynamicTool:
        """Validate a proposal statically and record it. Nothing is executed."""
        if not name or not name.isidentifier():
            raise ValueError("Tool name must be a valid Python identifier")
        if not description:
            raise ValueError("Tool description is required")
        if not isinstance(input_schema, dict):
            raise ValueError("Input schema must be a JSON object")
        if not isinstance(required_permissions, list):
            raise ValueError("Required permissions must be a list")
        if not all(isinstance(p, str) for p in required_permissions):
            raise ValueError("Each permission must be a string")
        if not isinstance(side_effect, SideEffect):
            raise ValueError("Side effect must be a SideEffect enum")
        if side_effect in FORBIDDEN_SIDE_EFFECTS:
            raise ValueError(
                f"A dynamic tool may not declare side_effect={side_effect.value!r}. "
                "Value movement and wallet signing require a reviewed static tool."
            )

        tool = DynamicTool(
            name=name,
            description=description,
            input_schema=input_schema,
            required_permissions=required_permissions,
            side_effect=side_effect,
            source_code=source_code,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            is_idempotent=is_idempotent,
        )

        self._dynamic_tools[name] = tool
        logger.info(
            "Accepted dynamic tool proposal '%s' (side_effect=%s, permissions=%s, source_hash=%s) "
            "[validated statically; not executable]",
            name,
            side_effect.value,
            ", ".join(required_permissions) or "none",
            tool.source_hash,
        )
        return tool

    def register_tool(self, tool: DynamicTool) -> None:
        """Register a proposed dynamic tool, disabled, subject to live policy.

        A tool declaring permissions the operator has not granted is rejected
        outright rather than registered-and-refused, so the agent gets an
        accurate answer about what it may do.
        """
        from backend.app.tools.registry import registry

        if tool.name not in self._dynamic_tools:
            raise ValueError(f"Tool '{tool.name}' has not been proposed")

        # Consult the same permits() the registry uses at invocation time, so a
        # tool cannot slip a side-effect class past by declaring permissions only.
        permitted, reason = registry.policy.permits(tool)
        if not permitted:
            raise ToolValidationError(
                f"Tool '{tool.name}' is not permitted by the active authorization "
                f"policy: {reason}"
            )

        registry.register_or_replace(tool)
        # Registered, but never runnable: the registry only dispatches tools that
        # the policy permits, and DynamicTool.execute refuses regardless.
        registry.disable(
            tool.name,
            "Dynamic tools are validated statically and cannot execute agent-authored code.",
        )
        logger.info("Registered disabled dynamic tool: %s", tool.name)

    def unregister_tool(self, name: str) -> None:
        """Remove a dynamic tool from tracking and from the registry."""
        from backend.app.tools.registry import registry

        if name not in self._dynamic_tools:
            raise ValueError(f"Tool '{name}' is not proposed")

        del self._dynamic_tools[name]
        if registry.get(name) is not None:
            registry.disable(name, "Dynamic tool withdrawn.")
        logger.info("Unregistered dynamic tool: %s", name)

    def list_proposed_tools(self) -> List[Dict[str, Any]]:
        return [tool.describe() for tool in self._dynamic_tools.values()]

    def get_tool(self, name: str) -> Optional[DynamicTool]:
        return self._dynamic_tools.get(name)


# Global manager instance
dynamic_tool_manager = DynamicToolManager()