"""Startup logging: the terminal is quieter without anything real going missing.

These tests pin the two properties that are easy to break and expensive to notice:

* the tool inventory is printed once, by the API process, instead of once per tool in
  both the API and the worker;
* the per-request access log is off by default, while warnings, errors, tracebacks,
  and every security-relevant event keep being written exactly as before.

Quietening the terminal is only acceptable as long as a failure is still visible.
The last test class exists for that reason: it re-checks that a transaction refusal,
an emergency stop, a failed login and a broadcast refusal are still logged, and still
recorded in the audit table, at the levels they always used.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.core.config import settings  # noqa: E402
from backend.app.core.logging_setup import (  # noqa: E402
    access_log_enabled,
    configure_logging,
    uvicorn_args,
)
from backend.app.tools.base import BaseTool, SideEffect, ToolResult  # noqa: E402
from backend.app.tools.bootstrap import bootstrap_tools, registered_tool_names  # noqa: E402
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry  # noqa: E402

import run as run_script  # noqa: E402


class _StubTool(BaseTool):
    name = "stub_tool"
    description = "A tool that exists only so this test can register something."
    side_effect = SideEffect.NONE

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        return ToolResult.success()


class _PermissionedTool(_StubTool):
    """A tool the deny-all default policy must refuse, and say why it refused."""

    name = "permissioned_tool"
    required_permissions = ("test.permission",)


class _ExplodingTool(_StubTool):
    """Raises on purpose, to prove an unhandled crash is still visible."""

    name = "exploding_tool"

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        raise RuntimeError("boom")


# ======================================================================
# Tool registration is logged once, not once per tool per process
# ======================================================================
class TestToolRegistrationLogging:
    def test_registering_a_tool_says_nothing_at_info(self, caplog):
        """One INFO line per tool, times two processes, buried the startup banner."""
        registry = ToolRegistry()
        with caplog.at_level(logging.INFO, logger="backend.app.tools.registry"):
            registry.register(_StubTool())

        assert not [r for r in caplog.records if "Registered tool" in r.getMessage()]

    def test_the_per_tool_line_is_still_available_at_debug(self, caplog):
        """Demoted, not deleted: `LOG_LEVEL=DEBUG` still shows what was registered."""
        registry = ToolRegistry()
        with caplog.at_level(logging.DEBUG, logger="backend.app.tools.registry"):
            registry.register(_StubTool())

        lines = [r.getMessage() for r in caplog.records if "Registered tool" in r.getMessage()]
        assert len(lines) == 1
        assert "stub_tool" in lines[0]

    def test_the_api_process_prints_the_whole_inventory_on_one_line(self, caplog, monkeypatch):
        from backend.app.tools import bootstrap as bootstrap_module

        monkeypatch.setattr(bootstrap_module, "_bootstrapped", False)
        with caplog.at_level(logging.INFO, logger="airdropos.tools"):
            bootstrap_tools(announce_inventory=True)

        lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("Registered ") and "tools" in r.getMessage()
        ]
        assert len(lines) == 1, f"inventory must be one line, got {lines}"

        names = registered_tool_names()
        assert len(names) > 10
        for name in names:
            assert name in lines[0]

    def test_the_worker_process_does_not_repeat_the_inventory(self, caplog, monkeypatch):
        """The worker holds the same tools. It logs the count, never the names.

        The flag is reset here so this really is a second bootstrap in one process,
        which is what starting the worker after the API looks like from the logs.
        """
        from backend.app.tools import bootstrap as bootstrap_module

        bootstrap_tools()
        monkeypatch.setattr(bootstrap_module, "_bootstrapped", False)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="airdropos.tools"):
            bootstrap_tools()

        messages = [r.getMessage() for r in caplog.records]
        joined = " ".join(messages)
        assert messages, "the worker still needs one summary line"
        for name in registered_tool_names():
            assert name not in joined, f"tool name {name} leaked into the worker's startup log"
        assert str(len(registered_tool_names())) in joined

    def test_bootstrap_is_still_idempotent(self, monkeypatch):
        from backend.app.tools.bootstrap import bootstrap_tools as bootstrap

        monkeypatch.setattr("backend.app.tools.bootstrap._bootstrapped", False)
        bootstrap()
        bootstrap(announce_inventory=True)
        # The second call must not re-register: `register` raises on a duplicate.
        assert len(registered_tool_names()) > 10


# ======================================================================
# Access logging
# ======================================================================
class TestAccessLogConfiguration:
    def test_the_access_log_is_off_by_default(self):
        assert access_log_enabled() is False
        assert "--no-access-log" in uvicorn_args()

    def test_the_access_log_can_be_turned_back_on(self, monkeypatch):
        monkeypatch.setattr(settings, "ACCESS_LOG", True)
        assert access_log_enabled() is True
        assert "--no-access-log" not in uvicorn_args()

    def test_only_the_access_logger_is_quietened(self, monkeypatch):
        """The request line goes; the server's own error line stays.

        `uvicorn.error` is where a failed startup or a traceback from the ASGI stack
        lands. Setting the access logger to WARNING must not touch it, or a server
        that failed to start would look silent rather than broken.
        """
        monkeypatch.setattr(settings, "ACCESS_LOG", False)
        touched = ("uvicorn.access", "uvicorn.error", "httpx", "httpcore", "websockets")
        saved = {name: logging.getLogger(name).level for name in touched}
        try:
            for name in touched:
                logging.getLogger(name).setLevel(logging.NOTSET)
            configure_logging(force=True)

            assert logging.getLogger("uvicorn.access").level >= logging.WARNING
            assert logging.getLogger("httpx").level >= logging.WARNING
            assert logging.getLogger("httpcore").level >= logging.WARNING
            assert logging.getLogger("uvicorn.error").level < logging.WARNING
        finally:
            for name, level in saved.items():
                logging.getLogger(name).setLevel(level)

    def test_the_request_only_loggers_still_emit_warnings(self):
        """WARNING is the ceiling, not the level. A real warning still gets through."""
        saved = logging.getLogger("httpx").level
        try:
            configure_logging(force=True)
            assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)
            assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
        finally:
            logging.getLogger("httpx").setLevel(saved)

    def test_no_airdropos_logger_is_raised_above_warning(self):
        """Application loggers keep their configured level.

        Transaction, auth, emergency-stop and audit events are logged at INFO by
        their own modules. Quietening the terminal must not move any of them.
        """
        for name in (
            "airdropos.api",
            "airdropos.worker",
            "airdropos.tools",
            "backend.app.security.auth",
            "backend.app.security.emergency_stop",
            "backend.app.transactions.approvals",
            "backend.app.transactions.engine",
        ):
            assert logging.getLogger(name).level <= logging.INFO, name


class TestRunnerCommand:
    """`run.py` is what an operator actually types, so its flags are part of the fix."""

    def test_backend_starts_without_the_access_log(self):
        cmd = run_script.uvicorn_command(
            Path("python"), "127.0.0.1", "8000", access_log=False, reload=False
        )
        assert "--no-access-log" in cmd
        assert "backend.app.main:app" in cmd
        assert "--reload" not in cmd

    def test_reload_still_works_alongside_it(self):
        cmd = run_script.uvicorn_command(
            Path("python"), "127.0.0.1", "8000", access_log=False, reload=True
        )
        assert "--reload" in cmd
        assert "--no-access-log" in cmd

    def test_the_access_log_can_be_asked_for(self):
        cmd = run_script.uvicorn_command(
            Path("python"), "127.0.0.1", "8000", access_log=True, reload=False
        )
        assert "--no-access-log" not in cmd

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", False),
            ("false", False),
            ("0", False),
            ("no", False),
            ("true", True),
            ("TRUE", True),
            ("1", True),
            ("on", True),
        ],
    )
    def test_the_env_flag_reads_the_same_booleans_as_the_settings_loader(self, raw, expected):
        assert run_script._env_flag({"ACCESS_LOG": raw}, "ACCESS_LOG") is expected


# ======================================================================
# Pre-login requests
# ======================================================================
class TestPreLoginProbe:
    @pytest.mark.asyncio
    async def test_health_reports_whether_a_session_is_needed_without_credentials(self):
        from httpx import ASGITransport, AsyncClient

        from backend.app.main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/health")

        # The dashboard reads this before there is any token. It must never 401:
        # that was the guaranteed failure the login page produced on every load.
        assert response.status_code == 200
        assert response.json()["auth_required"] is True

    @pytest.mark.asyncio
    async def test_the_probe_still_refuses_to_expose_anything_protected(self):
        from httpx import ASGITransport, AsyncClient

        from backend.app.main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/v1/overview")).status_code == 401
            assert (await client.get("/api/v1/tools")).status_code == 401
            assert (await client.get("/api/v1/emergency-stop")).status_code == 401
            assert (await client.get("/api/v1/agent/info")).status_code == 401

    @pytest.mark.asyncio
    async def test_an_unauthenticated_websocket_is_still_refused(self):
        """The dashboard stops opening the socket; the server must keep refusing it."""
        from backend.app.api.v1.endpoints.ws import _extract_ws_token

        class _Socket:
            headers: Dict[str, str] = {}
            scope: Dict[str, Any] = {"subprotocols": []}

        assert _extract_ws_token(_Socket()) is None
        # And with a token offered it is still read from the handshake, never a query.
        socket = _Socket()
        socket.scope = {"subprotocols": ["bearer", "t0ken"]}
        assert _extract_ws_token(socket) == "t0ken"


# ======================================================================
# Nothing that matters was silenced along with the noise
# ======================================================================
class TestSecurityLoggingIsIntact:
    """The whole point of quieting the terminal is that nothing real is in it.

    Each test here triggers an event an operator would need after an incident and
    asserts it is still logged. If someone later "fixes" the noise by raising one of
    these loggers' levels, these fail.
    """

    @pytest.mark.asyncio
    async def test_a_failed_login_is_still_logged(self, caplog):
        from httpx import ASGITransport, AsyncClient

        from backend.app.main import app

        with caplog.at_level(logging.WARNING):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/auth/login",
                    json={"email": "nobody@airdropos.local", "password": "wrong-password"},
                )

        assert response.status_code == 401
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "Failed login attempt" in logged
        assert "nobody@airdropos.local" in logged

    @pytest.mark.asyncio
    async def test_a_successful_login_is_still_written_to_the_audit_trail(self, caplog):
        """Security history does not live in stdout, so quietening it loses nothing."""
        from sqlalchemy import select

        from backend.app.models import AuditLog, User
        from backend.app.security.auth import hash_password

        from backend.app.database.session import AsyncSessionLocal

        from httpx import ASGITransport, AsyncClient

        from backend.app.main import app

        email = "operator-audit@example.test"
        async with AsyncSessionLocal() as db:
            db.add(User(email=email, hashed_password=hash_password("a-real-password"), is_active=True))
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/auth/login", json={"email": email, "password": "a-real-password"}
            )
        assert response.status_code == 200

        async with AsyncSessionLocal() as db:
            rows = list(
                (await db.execute(select(AuditLog).where(AuditLog.action == "AUTH_LOGIN"))).scalars()
            )
        assert rows, "a successful login must still leave an audit row"

    def test_engaging_and_releasing_the_emergency_stop_is_still_logged(self, caplog):
        from backend.app.security.emergency_stop import emergency_stop

        with caplog.at_level(logging.WARNING):
            emergency_stop.activate("test incident", "operator@example.test")
            emergency_stop.resume("operator@example.test")

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "EMERGENCY STOP ACTIVATED" in logged
        assert "Emergency stop resumed" in logged
        levels = {r.levelno for r in caplog.records if "EMERGENCY STOP" in r.getMessage()}
        assert logging.CRITICAL in levels, "an engaged kill-switch must be CRITICAL"

    @pytest.mark.asyncio
    async def test_a_refused_broadcast_is_still_logged(self, caplog):
        from backend.app.database.session import AsyncSessionLocal
        from backend.app.transactions.approvals import ApprovalService

        async with AsyncSessionLocal() as db:
            service = ApprovalService(db)
            with caplog.at_level(logging.WARNING):
                # An approval that does not exist must be refused AND said so.
                assert await service.verify_and_consume(
                    approval_id="does-not-exist", approved_by="operator", binding={"tx_hash": "0x0"}
                ) is False

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "Broadcast refused" in logged

    @pytest.mark.asyncio
    async def test_a_tool_refused_by_policy_is_still_logged(self, caplog):
        registry = ToolRegistry()
        registry.register(_PermissionedTool())

        with caplog.at_level(logging.WARNING):
            result = await registry.invoke("permissioned_tool", {})

        # The default policy grants nothing, so this is a refusal, not an execution.
        assert result.status.value == "not_authorized"
        assert "refused by authorization policy" in " ".join(r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_unhandled_tool_failure_still_logs_its_traceback(self, caplog):
        registry = ToolRegistry()
        registry.register(_ExplodingTool())
        # An instance-level policy, never the shared DEFAULT_POLICY: the registry
        # default is a module singleton and mutating it would leak into other tests.
        registry.policy = AuthorizationPolicy(enabled_permissions=["test.permission"])

        with caplog.at_level(logging.ERROR):
            await registry.invoke("exploding_tool", {})

        records = [r for r in caplog.records if "Unhandled exception in tool" in r.getMessage()]
        assert records
        assert records[0].exc_info, "the traceback must be attached, not just the message"