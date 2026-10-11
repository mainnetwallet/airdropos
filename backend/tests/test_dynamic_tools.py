"""Regression tests for dynamic tool sandbox isolation.

The original implementation called ``exec()`` on agent-authored source at
*proposal* time. That is arbitrary code execution inside the API process: a
single tool proposal could import ``os``, read configuration and credentials,
write files, and spawn subprocesses before any registration, authorization, or
emergency-stop check had run.

These tests pin the replacement contract: source is parsed and inspected, never
executed, and every escape route is refused at proposal time.
"""

from __future__ import annotations

import ast
import contextlib
from typing import Iterable, Iterator

import pytest

from backend.app.tools.base import SideEffect, ToolResultStatus, ToolValidationError
from backend.app.tools.dynamic import DynamicToolManager, SourceRejected
from backend.app.tools.registry import AuthorizationPolicy, registry

SAFE_TOOL_SOURCE = """
class Tool(BaseTool):
    async def execute(self, params):
        items = sorted(params.get("items", []))
        return ToolResult.success({"count": len(items), "items": items})
"""


@contextlib.contextmanager
def _policy(permissions: Iterable[str]) -> Iterator[AuthorizationPolicy]:
    """Install a registry policy for the duration of a test."""
    original = registry.policy
    registry.policy = AuthorizationPolicy(enabled_permissions=list(permissions))
    try:
        yield registry.policy
    finally:
        registry.policy = original


@pytest.fixture
def manager() -> DynamicToolManager:
    return DynamicToolManager()


def _propose(manager, source, **kwargs):
    params = {
        "name": "probe",
        "description": "probe tool",
        "input_schema": {"type": "object"},
        "required_permissions": [],
        "side_effect": SideEffect.NONE,
        "source_code": source,
    }
    params.update(kwargs)
    return manager.propose_tool(**params)


# ======================================================================
# No code path executes the proposal
# ======================================================================
@pytest.mark.parametrize(
    "name, source",
    [
        (
            "writes a file at import time",
            'open("/tmp/pwned.txt", "w").write("x")\n'
            "class Tool(BaseTool):\n"
            "    async def execute(self, params): return ToolResult.success({})\n",
        ),
        (
            "imports os",
            "import os\n"
            "class Tool(BaseTool):\n"
            "    async def execute(self, params): return ToolResult.success({})\n",
        ),
        (
            "imports settings",
            "from backend.app.core.config import settings\n"
            "class Tool(BaseTool):\n"
            "    async def execute(self, params): return ToolResult.success({})\n",
        ),
        (
            "imports from a relative module",
            "from . import sibling\n"
            "class Tool(BaseTool):\n"
            "    async def execute(self, params): return ToolResult.success({})\n",
        ),
        (
            "reaches subprocess",
            "from subprocess import run\n"
            "class Tool(BaseTool):\n"
            "    async def execute(self, params): return ToolResult.success({})\n",
        ),
    ],
)
def test_module_level_execution_is_refused(manager, name, source):
    """Nothing that runs at import time may appear in a proposal."""
    with pytest.raises(SourceRejected):
        _propose(manager, source)


@pytest.mark.parametrize(
    "expression",
    [
        "eval('1+1')",
        "exec('x=1')",
        "__import__('os')",
        "globals()",
        "getattr(object, '__subclasses__')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd')",
        "breakpoint()",
    ],
)
def test_escape_expressions_are_refused(manager, expression):
    """The classic namespace-escape spellings are all rejected."""
    source = (
        "class Tool(BaseTool):\n"
        "    async def execute(self, params):\n"
        f"        value = {expression}\n"
        "        return ToolResult.success({'v': str(value)})\n"
    )
    with pytest.raises(SourceRejected):
        _propose(manager, source)


def test_module_globals_dunder_is_refused(manager):
    source = (
        "class Tool(BaseTool):\n"
        "    async def execute(self, params):\n"
        "        return ToolResult.success({'v': str(globals)})\n"
    )
    with pytest.raises(SourceRejected):
        _propose(manager, source)


def test_rejection_message_names_the_offending_construct(manager):
    """The agent must be able to correct its proposal from the error."""
    with pytest.raises(SourceRejected) as exc:
        _propose(
            manager,
            "import os\nclass Tool(BaseTool):\n"
            "    async def execute(self, params): return ToolResult.success({})\n",
        )
    assert "Import" in str(exc.value)


# ======================================================================
# Structural validation happens without execution
# ======================================================================
def test_well_formed_tool_is_accepted(manager):
    tool = _propose(manager, SAFE_TOOL_SOURCE)
    assert tool.name == "probe"
    assert tool.source_hash


def test_absent_tool_class_is_refused(manager):
    with pytest.raises(SourceRejected, match="class named `Tool`"):
        _propose(
            manager,
            "class Helper(BaseTool):\n"
            "    async def execute(self, params): return ToolResult.success({})\n",
        )


def test_missing_execute_is_refused(manager):
    with pytest.raises(SourceRejected, match="execute"):
        _propose(manager, "class Tool(BaseTool):\n    pass\n")


def test_synchronous_execute_is_refused(manager):
    with pytest.raises(SourceRejected, match="async def"):
        _propose(
            manager,
            "class Tool(BaseTool):\n"
            "    def execute(self, params): return ToolResult.success({})\n",
        )


def test_syntax_error_is_refused(manager):
    with pytest.raises(SourceRejected, match="syntax"):
        _propose(manager, "class Tool(BaseTool)\n    pass\n")


def test_oversized_source_is_refused(manager):
    padding = "# " + ("x" * 60) + "\n"
    with pytest.raises(SourceRejected):
        _propose(manager, SAFE_TOOL_SOURCE + padding * 400)


# ======================================================================
# Accepted proposals still cannot run
# ======================================================================
@pytest.mark.asyncio
async def test_accepted_tool_refuses_to_execute(manager):
    """Validation succeeding must not imply the tool is runnable."""
    tool = _propose(manager, SAFE_TOOL_SOURCE)
    result = await tool.execute({"items": [3, 1, 2]})

    assert result.status is ToolResultStatus.UNAVAILABLE
    assert result.retryable is False
    assert result.error_category == "DYNAMIC_EXECUTION_DISABLED"
    assert "does not run agent-authored code" in (result.error or "")


def test_describe_marks_tool_non_executable(manager):
    tool = _propose(manager, SAFE_TOOL_SOURCE)
    descriptor = tool.describe()
    assert descriptor["executable"] is False
    assert descriptor["source_hash"] == tool.source_hash


# ======================================================================
# Value-moving tools are refused outright
# ======================================================================
@pytest.mark.parametrize(
    "side_effect",
    [SideEffect.WALLET_SIGNING, SideEffect.TRANSACTION_SUBMISSION],
)
def test_dynamic_tool_cannot_claim_value_movement(manager, side_effect):
    with pytest.raises(ValueError, match="reviewed static tool"):
        _propose(manager, SAFE_TOOL_SOURCE, side_effect=side_effect)


# ======================================================================
# Registration is gated by the live authorization policy
# ======================================================================
def test_registration_requires_granted_permissions(manager):
    """A tool cannot self-grant a permission the operator withheld."""
    with _policy(["web.read"]):
        tool = _propose(manager, SAFE_TOOL_SOURCE, required_permissions=["browser.click"])
        with pytest.raises(ToolValidationError, match="browser.click"):
            manager.register_tool(tool)


def test_registered_tool_is_disabled(manager):
    """Even a fully permitted dynamic tool lands disabled in the registry."""
    with _policy(["web.read"]):
        tool = _propose(manager, SAFE_TOOL_SOURCE, name="permitted_probe")
        manager.register_tool(tool)

        assert registry.get("permitted_probe") is tool
        assert not registry.is_available("permitted_probe")
        assert "cannot execute agent-authored code" in registry._disabled["permitted_probe"]


def test_unregister_removes_from_tracking(manager):
    tool = _propose(manager, SAFE_TOOL_SOURCE)
    manager.unregister_tool(tool.name)
    assert manager.get_tool(tool.name) is None


def test_cannot_register_unproposed_tool(manager):
    class Fake:
        name = "never_proposed"

    with pytest.raises(ValueError, match="not been proposed"):
        manager.register_tool(Fake())


# ======================================================================
# The source checker itself is not a blanket ban on computation
# ======================================================================
def test_pure_computation_is_still_allowed(manager):
    """Read-only analysis remains possible; only escape routes are blocked."""
    source = """
class Tool(BaseTool):
    async def execute(self, params):
        values = [v for v in params.get("values", []) if isinstance(v, int)]
        total = sum(values)
        return ToolResult.success({"total": total, "mean": total / len(values) if values else 0})
"""
    tool = _propose(manager, source)
    assert tool.name == "probe"


def test_checker_parses_without_executing(manager):
    """_check_source returns an AST; it does not compile or run the code."""
    from backend.app.tools.dynamic import _check_source

    tree = _check_source(SAFE_TOOL_SOURCE)
    assert isinstance(tree, ast.Module)
    assert [type(n).__name__ for n in tree.body] == ["ClassDef"]


# ======================================================================
# Startup authorization policy actually reaches invocation
# ======================================================================
@pytest.mark.asyncio
async def test_registry_declares_a_policy_attribute():
    """ToolRegistry must own `policy`, not acquire it by incidental assignment.

    `bootstrap_tools` assigns `registry.policy`. When the attribute was undeclared,
    that assignment silently created a new instance attribute that `invoke`
    never consulted, so the deny-all DEFAULT_POLICY applied and every tool call
    was refused regardless of operator configuration.
    """
    assert "policy" in vars(registry), "ToolRegistry instances have no policy attribute"
    assert isinstance(registry.policy, AuthorizationPolicy)


@pytest.mark.asyncio
async def test_invoke_consults_registry_policy_when_caller_passes_none():
    """The agent loop passes policy=None; the configured policy must apply."""
    from backend.app.tools.web_tools import WebFetchTool
    from backend.app.tools.base import ToolResultStatus

    registry.register_or_replace(WebFetchTool())

    with _policy(["web.read"]):
        # policy=None is exactly what AgentController.execute_step passes.
        result = await registry.invoke(
            "web_fetch", {"url": "http://127.0.0.1:1/none"}, policy=None
        )
        # Authorization passed; the failure is now a transport error, not NOT_AUTHORIZED.
        assert result.status is not ToolResultStatus.NOT_AUTHORIZED, result.error


@pytest.mark.asyncio
async def test_invoke_is_denied_under_default_policy():
    """With no operator policy configured, nothing runs."""
    from backend.app.tools.web_tools import WebFetchTool
    from backend.app.tools.base import ToolResultStatus

    registry.register_or_replace(WebFetchTool())

    with _policy([]):
        result = await registry.invoke("web_fetch", {"url": "https://example.com"}, policy=None)
        assert result.status is ToolResultStatus.NOT_AUTHORIZED
        assert result.error_category == "MISSING_PERMISSION"