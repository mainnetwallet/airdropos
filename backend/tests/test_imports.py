"""Import smoke tests for every backend module.

The agent execution path was entirely unimportable on this environment:
`planning/planner.py` contained an f-string with a backslash in the expression
part, which is a SyntaxError before Python 3.12. Because
`agent/controller.py` imports it, and `scheduler/worker.py` imports the
controller, the worker -- the process that actually runs tasks -- could not
start at all. Nothing caught it: the API module never imports the planner, so
`import backend.app.main` succeeded and looked healthy.

These tests walk the package and import every module, so a syntax error or a
missing import anywhere surfaces as a normal test failure instead of a
production crash.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import pkgutil
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
# backend/app -> repo root, so relative_to() yields "backend.app.<mod>".
REPO_ROOT = APP_ROOT.parent.parent
PACKAGE = "backend.app"

# Modules that must not be imported as a side effect of this suite. Alembic's
# `env.py` executes migration logic at import time against a configured engine.
EXCLUDE = {f"{PACKAGE}.migrations.env"}


def _module_names() -> list[str]:
    """Every importable module under backend.app, as dotted names."""
    names: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if not parts:
            continue
        name = ".".join(parts)
        if name not in EXCLUDE:
            names.append(name)
    return names


ALL_MODULES = _module_names()


def test_module_discovery_finds_the_package():
    """Guard against the walker silently returning nothing."""
    assert len(ALL_MODULES) > 30, f"only discovered {len(ALL_MODULES)} modules"
    assert f"{PACKAGE}.main" in ALL_MODULES
    assert all(name.startswith(PACKAGE) for name in ALL_MODULES)


@pytest.mark.parametrize("module_name", ALL_MODULES)
def test_module_imports(module_name: str):
    """Import every module. A SyntaxError or bad import fails here."""
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", ALL_MODULES)
def test_module_is_already_imported(module_name: str):
    """Each module must be importable in a process where the others are loaded.

    `backend.app.models` re-declares tables on shared MetaData; importing two
    model modules independently in a fresh interpreter is fine, but importing
    the same module twice must not redefine tables. This catches modules that
    only work when something else was imported first.
    """
    importlib.import_module(module_name)
    importlib.import_module(module_name)  # idempotent re-import


# ======================================================================
# The specific breakages found by this suite
# ======================================================================
def test_planner_is_importable():
    """`agent/controller.py` imports this; without it the worker cannot start."""
    planner = importlib.import_module("backend.app.planning.planner")
    assert hasattr(planner, "Planner")


def test_agent_controller_is_importable():
    """The controller drives the Observe-Plan-Act-Verify loop."""
    controller = importlib.import_module("backend.app.agent.controller")
    assert hasattr(controller, "AgentController")


def test_worker_is_importable():
    """The worker process entry point."""
    worker = importlib.import_module("backend.app.scheduler.worker")
    assert hasattr(worker, "task_execution_loop")
    assert hasattr(worker, "recover_interrupted_tasks")


def test_controller_module_does_not_use_backslash_in_fstring_expression():
    """Backslashes in f-string expressions are a SyntaxError before Python 3.12.

    This pins the declared minimum interpreter (>=3.11) rather than the one
    this happens to run on.
    """
    planner_path = APP_ROOT / "planning" / "planner.py"
    tree = ast.parse(planner_path.read_text(encoding="utf-8"))

    offenders: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue) and "\\" in ast.dump(value.value):
                    offenders.append(node.lineno)
    assert not offenders, (
        f"{planner_path} has backslashes inside f-string expressions at lines "
        f"{sorted(set(offenders))}; build the section outside the f-string instead"
    )


# ======================================================================
# Undefined names hidden behind authentication
# ======================================================================
def test_no_unbound_names_in_backend():
    """No module may reference a name it never binds.

    Handlers that run after authentication are invisible to unauthenticated
    tests: `campaigns.py` used `uuid.UUID` five times without importing `uuid`,
    so every authenticated campaign request would have raised NameError -> 500
    while the 401 regression tests all passed.
    """
    builtin_names = set(dir(builtins))
    offenders: list[str] = []

    for path in sorted(APP_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:  # reported by test_module_imports already
            offenders.append(f"{path}: SYNTAX ERROR line {exc.lineno}: {exc.msg}")
            continue

        bound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
                args = getattr(node, "args", None)
                if args is not None:
                    for arg in args.args + args.kwonlyargs + args.posonlyargs:
                        bound.add(arg.arg)
                    if args.vararg:
                        bound.add(args.vararg.arg)
                    if args.kwarg:
                        bound.add(args.kwarg.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.Global):
                bound.update(node.names)
            elif isinstance(node, ast.Lambda):
                args = node.args
                for arg in args.args + args.kwonlyargs + args.posonlyargs:
                    bound.add(arg.arg)

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
                if name in bound or name in builtin_names or name.startswith("__"):
                    continue
                offenders.append(f"{path}:{node.lineno}: undefined name '{name}'")

    assert not offenders, "undefined names found:\n" + "\n".join(offenders)