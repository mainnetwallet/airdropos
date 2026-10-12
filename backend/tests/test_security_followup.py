"""Regression tests for the follow-up security review of PR #8.

Covers: Emergency Stop shared state across REAL processes, persistence failure, missing/corrupt
state, cross-platform locking and contention, enforcement at the tool-registry and live-gateway
boundaries, WebSocket session revocation/expiry, and proxy-aware rate limiting.

What these tests do and do not prove (see also the final report):
  * The multi-process tests spawn real Python processes, but only on the platform running the
    tests. They were run on Linux; nothing here proves Windows behaviour.
  * The ``msvcrt`` test drives the Windows code path with a FAKE ``msvcrt`` module. It checks the
    calls our code makes, not the behaviour of the real Windows API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.app.core.config import settings
from backend.app.security import emergency_stop as es_module
from backend.app.security.auth import (
    Scope,
    client_ip,
    enforce_rate_limit,
    rate_limiter,
    resolve_session,
)
from backend.app.security.emergency_stop import (
    EmergencyStopAuthorizationError,
    EmergencyStopController,
    EmergencyStopPersistenceError,
    StopPolicy,
    emergency_stop,
)
from backend.app.tools.base import BaseTool, SideEffect, ToolResult
from backend.app.tools.registry import AuthorizationPolicy, ToolRegistry

from backend.tests.test_api_auth import (  # noqa: F401
    _make_expired_session,
    _make_user,
    db_session_factory,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def state_file(tmp_path) -> Path:
    return tmp_path / "estop" / "emergency_stop.json"


def _child_env(state_file: Path) -> dict:
    env = dict(os.environ)
    # Keep the module-level singleton in the child away from the real ./data directory.
    env["EMERGENCY_STOP_STATE_FILE"] = str(state_file)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _spawn(code: str, *args: str, state_file: Path, **kw) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code, str(state_file), *args],
        cwd=str(REPO_ROOT),
        env=_child_env(state_file),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kw,
    )


def _write_state(path: Path, *, stopped: bool, reason: str = "written by another process") -> None:
    """Replace the state file the way another process would (new inode, atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_text(
        json.dumps(
            {
                "stopped": stopped,
                "activated_at": datetime.now(timezone.utc).isoformat() if stopped else None,
                "activated_by": "other-process" if stopped else None,
                "reason": reason if stopped else None,
                "policy": sorted(StopPolicy.ALLOWED),
                "history": [],
            }
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ======================================================================
# 1. Shared Emergency Stop state across real processes
# ======================================================================
_ACTOR = """
import sys
from backend.app.security.emergency_stop import EmergencyStopController, EmergencyStopPersistenceError
ctl = EmergencyStopController(sys.argv[1])
mode, n = sys.argv[2], int(sys.argv[3])
refused = 0
for i in range(n):
    try:
        if mode == "activate":
            ctl.activate(f"reason-{i}", "child-process")
        elif mode == "resume":
            ctl.resume("alice")
    except EmergencyStopPersistenceError:
        refused += 1
print(refused)
"""


class TestSharedStateAcrossProcesses:
    def test_activation_in_another_process_is_seen_by_this_one(self, state_file):
        mine = EmergencyStopController(str(state_file))
        assert not mine.is_stopped

        child = _spawn(_ACTOR, "activate", "1", state_file=state_file)
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err

        assert mine.is_stopped, "an already-running controller must adopt a stop engaged elsewhere"
        assert not mine.can_sign() and not mine.can_submit_transactions() and not mine.can_execute_tasks()
        assert EmergencyStopController(str(state_file)).is_stopped, "a fresh process must see it too"

    def test_resume_in_another_process_is_seen_by_this_one(self, state_file):
        mine = EmergencyStopController(str(state_file))
        mine.activate("test", "operator")
        assert mine.is_stopped

        child = _spawn(_ACTOR, "resume", "1", state_file=state_file)
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err
        assert not mine.is_stopped

    def test_concurrent_activate_and_resume_never_corrupt_the_state(self, state_file):
        """Hammer the file from several real processes at once.

        Individual resumes MAY be refused (lock timeout or the compare-and-swap noticed a newer
        state); that is the safe outcome. What must never happen is a torn/corrupt file or an
        exception other than the documented refusal.
        """
        EmergencyStopController(str(state_file)).activate("seed", "operator")
        procs = [
            _spawn(_ACTOR, mode, "15", state_file=state_file)
            for mode in ("activate", "resume", "activate", "resume")
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=300)
            assert proc.returncode == 0, err  # only EmergencyStopPersistenceError is tolerated

        data = json.loads(state_file.read_text(encoding="utf-8"))  # must parse: no torn write
        assert isinstance(data["stopped"], bool)
        assert len(data["history"]) <= EmergencyStopController.MAX_HISTORY
        final = EmergencyStopController(str(state_file))
        assert final.is_stopped == data["stopped"]
        if final.is_stopped:
            assert "unreadable" not in (final.status()["reason"] or ""), "state must not have been corrupted"

    def test_stop_survives_a_process_restart(self, state_file):
        EmergencyStopController(str(state_file)).activate("persist me", "operator")
        restarted = EmergencyStopController(str(state_file))
        assert restarted.is_stopped and restarted.status()["reason"] == "persist me"

    def test_environment_flag_does_not_undo_an_operator_resume_on_restart(self, state_file, monkeypatch):
        first = EmergencyStopController(str(state_file))
        first.activate("x", "operator")
        first.resume("alice")
        monkeypatch.setattr(settings, "EMERGENCY_STOP_ENABLED", True)
        assert not EmergencyStopController(str(state_file)).is_stopped  # file is the source of truth


# ======================================================================
# 2. Persistence failure and missing / corrupt state fail closed
# ======================================================================
class TestPersistenceFailureAndBadState:
    def test_activation_that_cannot_be_saved_is_never_reported_as_success(self, state_file, monkeypatch):
        ctl = EmergencyStopController(str(state_file))

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(es_module.os, "replace", boom)
        with pytest.raises(EmergencyStopPersistenceError):
            ctl.activate("must not look successful", "operator")
        # Still engaged in THIS process, and the status says it was not persisted.
        assert ctl.is_stopped
        assert ctl.status()["persisted"] is False
        assert not list(state_file.parent.glob(".estop-*.tmp")), "temp file must be cleaned up"

    def test_resume_that_cannot_be_saved_leaves_the_stop_engaged(self, state_file, monkeypatch):
        ctl = EmergencyStopController(str(state_file))
        ctl.activate("x", "operator")
        monkeypatch.setattr(es_module.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("ro fs")))
        with pytest.raises(OSError):
            ctl.resume("alice")
        assert ctl.is_stopped
        assert json.loads(state_file.read_text())["stopped"] is True

    @pytest.mark.parametrize(
        "content",
        ["{not json", "", "[]", '{"stopped": "no"}', '{"stopped": null}', "\x00\x01\x02"],
    )
    def test_corrupt_state_at_startup_is_treated_as_stopped(self, state_file, content):
        state_file.parent.mkdir(parents=True)
        state_file.write_text(content, encoding="utf-8")
        ctl = EmergencyStopController(str(state_file))
        assert ctl.is_stopped
        assert not ctl.can_sign() and not ctl.can_submit_transactions() and not ctl.can_execute_tasks()
        assert not ctl.can_run_jobs()

    def test_unreadable_state_fails_closed(self, state_file, monkeypatch):
        ctl = EmergencyStopController(str(state_file))
        assert not ctl.is_stopped

        def deny():
            raise PermissionError("denied")

        monkeypatch.setattr(ctl, "_signature", deny)
        assert ctl.is_stopped and not ctl.can_sign()

    def test_state_file_deleted_while_running_fails_closed(self, state_file):
        ctl = EmergencyStopController(str(state_file))
        ctl.activate("x", "operator")
        ctl.resume("alice")
        assert not ctl.is_stopped
        state_file.unlink()
        assert ctl.is_stopped, "a vanished state file must not be read as 'running'"

    def test_non_writable_storage_at_startup_starts_stopped(self, state_file, monkeypatch):
        monkeypatch.setattr(es_module.os, "access", lambda *a, **k: False)
        assert EmergencyStopController(str(state_file)).is_stopped

    @pytest.mark.parametrize("actor", ["", "system", "SYSTEM", " worker ", "Agent", "scheduler", "anonymous", None])
    def test_non_human_and_anonymous_actors_cannot_resume(self, state_file, actor):
        ctl = EmergencyStopController(str(state_file))
        ctl.activate("x", "operator")
        with pytest.raises(EmergencyStopAuthorizationError):
            ctl.resume(actor)
        assert ctl.is_stopped
        assert EmergencyStopController(str(state_file)).is_stopped


# ======================================================================
# 3. Locking: contention, crash cleanup, no-primitive, Windows code path
# ======================================================================
_HOLD_LOCK = """
import sys, time
from backend.app.security import emergency_stop as m
path = sys.argv[1] + ".lock"
handle = open(path, "a+")
m._try_lock(handle)
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
"""


def _hold_lock_in_child(state_file: Path, seconds: float) -> subprocess.Popen:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    proc = _spawn(_HOLD_LOCK, str(seconds), state_file=state_file)
    assert proc.stdout.readline().strip() == "locked", proc.stderr.read()
    return proc


class TestLocking:
    def test_resume_is_refused_while_another_process_holds_the_lock(self, state_file, monkeypatch):
        ctl = EmergencyStopController(str(state_file))
        ctl.activate("x", "operator")
        monkeypatch.setattr(EmergencyStopController, "LOCK_TIMEOUT_SECONDS", 0.3)
        holder = _hold_lock_in_child(state_file, 8)
        try:
            with pytest.raises(EmergencyStopPersistenceError):
                ctl.resume("alice")
            assert ctl.is_stopped
            assert json.loads(state_file.read_text())["stopped"] is True
        finally:
            holder.kill()
            holder.wait(timeout=30)

    def test_activation_is_never_blocked_by_a_held_lock(self, state_file, monkeypatch, caplog):
        ctl = EmergencyStopController(str(state_file))
        monkeypatch.setattr(EmergencyStopController, "LOCK_TIMEOUT_SECONDS", 0.3)
        holder = _hold_lock_in_child(state_file, 8)
        try:
            with caplog.at_level(logging.CRITICAL, logger=es_module.logger.name):
                ctl.activate("must engage regardless", "operator")
            assert ctl.is_stopped
            assert json.loads(state_file.read_text())["stopped"] is True  # and it was persisted
            assert any("proceeding without it" in r.getMessage() for r in caplog.records), "must be loud"
        finally:
            holder.kill()
            holder.wait(timeout=30)

    def test_lock_is_released_when_the_holding_process_crashes(self, state_file, monkeypatch):
        ctl = EmergencyStopController(str(state_file))
        ctl.activate("x", "operator")
        monkeypatch.setattr(EmergencyStopController, "LOCK_TIMEOUT_SECONDS", 2.0)
        holder = _hold_lock_in_child(state_file, 60)
        holder.kill()  # simulate a crash while holding the lock
        holder.wait(timeout=30)
        ctl.resume("alice")  # the OS released the lock; resume must go through
        assert not ctl.is_stopped

    def test_lock_is_released_after_every_transition(self, state_file):
        ctl = EmergencyStopController(str(state_file))
        for _ in range(5):
            ctl.activate("x", "operator")
            ctl.resume("alice")
        other = EmergencyStopController(str(state_file))
        other.activate("y", "operator")  # would time out if a previous call leaked the lock
        assert other.is_stopped

    def test_resume_is_refused_when_no_locking_primitive_exists(self, state_file, monkeypatch):
        """Never silently run unlocked: with neither fcntl nor msvcrt, a release must be refused."""
        ctl = EmergencyStopController(str(state_file))
        ctl.activate("x", "operator")
        monkeypatch.setattr(es_module, "fcntl", None)
        monkeypatch.setattr(es_module, "msvcrt", None)
        with pytest.raises(EmergencyStopPersistenceError):
            ctl.resume("alice")
        assert ctl.is_stopped
        assert json.loads(state_file.read_text())["stopped"] is True

    def test_activation_still_engages_when_no_locking_primitive_exists(self, state_file, monkeypatch, caplog):
        ctl = EmergencyStopController(str(state_file))
        monkeypatch.setattr(es_module, "fcntl", None)
        monkeypatch.setattr(es_module, "msvcrt", None)
        with caplog.at_level(logging.CRITICAL, logger=es_module.logger.name):
            ctl.activate("x", "operator")
        assert ctl.is_stopped and json.loads(state_file.read_text())["stopped"] is True
        assert any("lock unavailable" in r.getMessage() for r in caplog.records)

    def test_windows_code_path_uses_msvcrt_byte_range_locks(self, state_file, monkeypatch):
        """SIMULATION of the Windows branch with a fake msvcrt. This is NOT Windows verification."""
        calls = []
        fake = types.SimpleNamespace(
            LK_NBLCK=2,
            LK_UNLCK=0,
            locking=lambda fd, mode, nbytes: calls.append((mode, nbytes)),
        )
        monkeypatch.setattr(es_module, "fcntl", None)
        monkeypatch.setattr(es_module, "msvcrt", fake)
        ctl = EmergencyStopController(str(state_file))
        ctl.activate("x", "operator")
        ctl.resume("alice")
        modes = [m for m, _ in calls]
        assert modes == [fake.LK_NBLCK, fake.LK_UNLCK, fake.LK_NBLCK, fake.LK_UNLCK]
        assert all(n == 1 for _, n in calls)

    def test_windows_code_path_refuses_resume_when_the_lock_is_held(self, state_file, monkeypatch):
        def locked_elsewhere(fd, mode, nbytes):
            if mode == 2:
                raise OSError("locked by another process")

        fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locked_elsewhere)
        ctl = EmergencyStopController(str(state_file))
        ctl.activate("x", "operator")
        monkeypatch.setattr(es_module, "fcntl", None)
        monkeypatch.setattr(es_module, "msvcrt", fake)
        monkeypatch.setattr(EmergencyStopController, "LOCK_TIMEOUT_SECONDS", 0.2)
        with pytest.raises(EmergencyStopPersistenceError):
            ctl.resume("alice")
        assert ctl.is_stopped

    def test_resume_never_overwrites_a_stop_engaged_after_it_read_the_state(self, state_file, monkeypatch):
        """The lost-stop race: another process engages the stop between resume's read and write."""
        ctl = EmergencyStopController(str(state_file))
        ctl.activate("original", "operator")
        real_persist = ctl._persist

        def racing_persist(*args, **kwargs):
            _write_state(state_file, stopped=True, reason="engaged by the worker mid-resume")
            return real_persist(*args, **kwargs)

        monkeypatch.setattr(ctl, "_persist", racing_persist)
        with pytest.raises(EmergencyStopPersistenceError):
            ctl.resume("alice")

        data = json.loads(state_file.read_text())
        assert data["stopped"] is True and data["reason"] == "engaged by the worker mid-resume"
        assert ctl.is_stopped  # and this process adopts the newer state rather than its own stale view
        assert ctl.status()["reason"] == "engaged by the worker mid-resume"


# ======================================================================
# 4. Enforcement at the trusted tool boundary
# ======================================================================
class _Tool(BaseTool):
    name = "t"
    description = "test tool"
    required_permissions: list = ["test.run"]
    input_schema = {"type": "object", "properties": {}, "required": []}
    ran = 0

    async def execute(self, params):
        type(self).ran += 1
        return ToolResult.success(data={})


def _tool(name: str, effect: SideEffect):
    cls = type(f"T_{name}", (_Tool,), {"name": name, "side_effect": effect, "ran": 0})
    return cls()


POLICY = AuthorizationPolicy(enabled_permissions=["test.run", "notify.send"])


class TestToolBoundaryEnforcement:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "effect",
        [
            SideEffect.WALLET_SIGNING,
            SideEffect.TRANSACTION_SUBMISSION,
            SideEffect.WEB_INTERACTION,
            SideEffect.STATE_MUTATION,
        ],
    )
    async def test_every_side_effecting_class_is_blocked_while_stopped(self, effect):
        registry = ToolRegistry()
        tool = _tool("mutating", effect)
        registry.register(tool)
        emergency_stop.activate("test", "operator")
        result = await registry.invoke("mutating", {}, policy=POLICY)
        assert result.error_category == "EMERGENCY_STOP"
        assert type(tool).ran == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("effect", [SideEffect.NONE, SideEffect.EXTERNAL_READ])
    async def test_read_only_tools_stay_available_for_diagnostics(self, effect):
        registry = ToolRegistry()
        tool = _tool("reader", effect)
        registry.register(tool)
        emergency_stop.activate("test", "operator")
        result = await registry.invoke("reader", {}, policy=POLICY)
        assert result.ok and type(tool).ran == 1

    @pytest.mark.asyncio
    async def test_state_mutation_tool_is_blocked_when_the_stop_state_cannot_be_read(self, monkeypatch):
        """Deny when the stop state cannot be determined, not only when it is known to be active."""
        registry = ToolRegistry()
        tool = _tool("mutating", SideEffect.STATE_MUTATION)
        registry.register(tool)
        assert not emergency_stop.is_stopped

        def unreadable():
            raise OSError("state file unreadable")

        monkeypatch.setattr(emergency_stop, "_signature", unreadable)
        result = await registry.invoke("mutating", {}, policy=POLICY)
        assert result.error_category == "EMERGENCY_STOP" and type(tool).ran == 0

    @pytest.mark.asyncio
    async def test_send_notification_tool_does_not_reach_telegram_while_stopped(self, monkeypatch):
        from backend.app.notifications import dispatcher as dispatcher_module
        from backend.app.tools.notification_tools import SendNotificationTool

        notify = AsyncMock(return_value={"results": {}})
        monkeypatch.setattr(dispatcher_module.dispatcher, "notify", notify)
        registry = ToolRegistry()
        registry.register(SendNotificationTool())
        emergency_stop.activate("test", "operator")

        result = await registry.invoke("send_notification", {"title": "t", "body": "b"}, policy=POLICY)

        assert result.error_category == "EMERGENCY_STOP"
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_denied_call_is_not_replayed_and_release_does_not_authorize_it(self):
        registry = ToolRegistry()
        tool = _tool("mutating", SideEffect.STATE_MUTATION)
        registry.register(tool)
        emergency_stop.activate("test", "operator")
        assert (await registry.invoke("mutating", {}, policy=POLICY)).error_category == "EMERGENCY_STOP"

        emergency_stop.resume("alice")
        assert type(tool).ran == 0, "releasing the stop must not run what was denied"

        # And releasing the stop grants no permissions: the normal authorization still applies.
        denied = await registry.invoke("mutating", {}, policy=AuthorizationPolicy(enabled_permissions=[]))
        assert denied.error_category == "MISSING_PERMISSION" and type(tool).ran == 0


# ======================================================================
# 5. Enforcement at the live gateway (worker-side boundary)
# ======================================================================
class TestLiveGatewayEnforcement:
    @pytest.fixture
    def gateway(self):
        from starlette.testclient import TestClient

        from backend.app.live.gateway import build_app
        from backend.app.live.security import GATEWAY_HEADER, gateway_secret

        client = TestClient(build_app())
        client.headers.update({GATEWAY_HEADER: gateway_secret()})
        return client

    def test_starting_a_session_is_refused_while_stopped(self, gateway):
        emergency_stop.activate("test", "operator")
        response = gateway.post("/sessions", json={"integration": "browser"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "emergency_stop"

    def test_resuming_the_agent_is_refused_while_stopped(self, gateway):
        emergency_stop.activate("test", "operator")
        response = gateway.post("/sessions/browser/abc/action", json={"action": "resume"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "emergency_stop"

    def test_reads_and_pause_stay_available_while_stopped(self, gateway):
        emergency_stop.activate("test", "operator")
        assert gateway.get("/health").status_code == 200
        pause = gateway.post("/sessions/browser/abc/action", json={"action": "pause"})
        assert pause.status_code != 409  # pausing is never blocked by the stop

    def test_not_stopped_requests_are_not_refused_by_the_stop(self, gateway):
        response = gateway.post("/sessions", json={"integration": "no-such-integration"})
        assert response.status_code != 409  # fails for its own reasons (unknown integration)


# ======================================================================
# 6. WebSocket session revocation and expiry
# ======================================================================
class _FakeWebSocket:
    def __init__(self) -> None:
        self.closed: list[tuple[int, str]] = []

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


@pytest.fixture
def ws_module(monkeypatch):
    from backend.app.api.v1.endpoints import ws

    monkeypatch.setattr(ws, "WS_REVALIDATE_SECONDS", 0.05)
    return ws


async def _watch(ws_module, fake, expires_at=None, run_for=0.6):
    task = asyncio.create_task(ws_module._watch_session(fake, "token", expires_at))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=run_for)
    except asyncio.TimeoutError:
        pass
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class TestWebSocketRevocation:
    @pytest.mark.asyncio
    async def test_revoked_session_closes_the_socket_on_the_next_check(self, ws_module, monkeypatch):
        answers = iter([object(), object(), None])  # valid, valid, then revoked
        monkeypatch.setattr(ws_module, "_authenticate_websocket", AsyncMock(side_effect=lambda *a, **k: next(answers)))
        fake = _FakeWebSocket()
        await _watch(ws_module, fake)
        assert fake.closed == [(4401, "Session ended")]

    @pytest.mark.asyncio
    async def test_valid_session_stays_connected(self, ws_module, monkeypatch):
        auth = AsyncMock(return_value=object())
        monkeypatch.setattr(ws_module, "_authenticate_websocket", auth)
        fake = _FakeWebSocket()
        await _watch(ws_module, fake, run_for=0.4)
        assert fake.closed == [] and auth.await_count >= 2

    @pytest.mark.asyncio
    async def test_revalidation_does_not_write_last_used_at(self, ws_module, monkeypatch):
        auth = AsyncMock(return_value=object())
        monkeypatch.setattr(ws_module, "_authenticate_websocket", auth)
        await _watch(ws_module, _FakeWebSocket(), run_for=0.3)
        assert auth.await_count >= 1 and all(c.kwargs.get("touch") is False for c in auth.await_args_list)

    @pytest.mark.asyncio
    async def test_expiry_closes_the_socket_at_the_exact_time_without_a_database_check(self, ws_module, monkeypatch):
        monkeypatch.setattr(ws_module, "WS_REVALIDATE_SECONDS", 30)  # far longer than the expiry
        auth = AsyncMock(return_value=object())
        monkeypatch.setattr(ws_module, "_authenticate_websocket", auth)
        fake = _FakeWebSocket()
        started = time.monotonic()
        await _watch(ws_module, fake, expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.3), run_for=5)
        assert fake.closed == [(4401, "Session ended")]
        assert time.monotonic() - started < 2, "must not wait for the 30s revalidation interval"
        auth.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_expired_session_closes_immediately(self, ws_module, monkeypatch):
        auth = AsyncMock(return_value=object())
        monkeypatch.setattr(ws_module, "_authenticate_websocket", auth)
        fake = _FakeWebSocket()
        await _watch(ws_module, fake, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1), run_for=1)
        assert fake.closed == [(4401, "Session ended")]
        auth.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unverifiable_session_fails_closed_instead_of_the_watcher_dying(self, ws_module, monkeypatch):
        """A DB outage used to kill the watcher task silently, leaving the socket unchecked forever."""
        auth = AsyncMock(side_effect=RuntimeError("database unreachable"))
        monkeypatch.setattr(ws_module, "_authenticate_websocket", auth)
        fake = _FakeWebSocket()
        await _watch(ws_module, fake)
        assert fake.closed == [(1011, "Session could not be verified")]
        assert auth.await_count == ws_module.WS_MAX_CHECK_FAILURES

    @pytest.mark.asyncio
    async def test_a_transient_check_failure_does_not_drop_a_legitimate_connection(self, ws_module, monkeypatch):
        outcomes = iter([RuntimeError("blip"), object(), object(), RuntimeError("blip"), object()])

        async def flaky(*a, **k):
            outcome = next(outcomes, object())
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(ws_module, "_authenticate_websocket", flaky)
        fake = _FakeWebSocket()
        await _watch(ws_module, fake, run_for=0.5)
        assert fake.closed == []

    @pytest.mark.asyncio
    async def test_real_session_resolution_carries_the_expiry_and_skips_the_write(self, db_session_factory):
        token, _ = await _make_user(db_session_factory, scopes={Scope.READ.value})
        async with db_session_factory() as db:
            principal = await resolve_session(db, token, touch=False)
            await db.commit()
        assert principal.session_expires_at is not None
        assert principal.session_expires_at > datetime.now(timezone.utc)

        from sqlalchemy import select

        from backend.app.models import UserSession
        from backend.app.security.auth import hash_token

        async with db_session_factory() as db:
            row = await db.scalar(select(UserSession).where(UserSession.token == hash_token(token)))
            assert row.last_used_at is None, "touch=False must not write last_used_at"
            await resolve_session(db, token)  # default still records use
            await db.commit()
        async with db_session_factory() as db:
            row = await db.scalar(select(UserSession).where(UserSession.token == hash_token(token)))
            assert row.last_used_at is not None

    @pytest.mark.asyncio
    async def test_real_revoked_and_expired_sessions_are_rejected_by_the_websocket_check(self, db_session_factory, monkeypatch):
        from sqlalchemy import select

        from backend.app.api.v1.endpoints import ws
        from backend.app.models import UserSession
        from backend.app.security.auth import hash_token

        monkeypatch.setattr(ws, "AsyncSessionLocal", db_session_factory)
        token, _ = await _make_user(db_session_factory, scopes={Scope.READ.value})
        assert await ws._authenticate_websocket(token, touch=False) is not None

        async with db_session_factory() as db:
            row = await db.scalar(select(UserSession).where(UserSession.token == hash_token(token)))
            row.revoked_at = datetime.now(timezone.utc)
            await db.commit()
        assert await ws._authenticate_websocket(token, touch=False) is None

        expired = await _make_expired_session(db_session_factory)
        assert await ws._authenticate_websocket(expired, touch=False) is None


# ======================================================================
# 7. Proxy-aware rate limiting
# ======================================================================
def _request(peer: str | None, forwarded: str | None = None):
    headers = {"x-forwarded-for": forwarded} if forwarded is not None else {}
    client = types.SimpleNamespace(host=peer) if peer is not None else None
    return types.SimpleNamespace(client=client, headers=headers)


@pytest.fixture
def trusted(monkeypatch):
    def _set(value: str):
        monkeypatch.setattr(settings, "TRUSTED_PROXIES", value)

    rate_limiter.reset()
    yield _set
    rate_limiter.reset()


class TestClientIp:
    def test_direct_request_uses_the_connecting_address(self, trusted):
        trusted("")
        assert client_ip(_request("198.51.100.4")) == "198.51.100.4"

    def test_forwarded_header_is_ignored_when_no_proxy_is_trusted(self, trusted):
        trusted("")
        assert client_ip(_request("198.51.100.4", "1.2.3.4")) == "198.51.100.4"

    def test_forwarded_header_is_ignored_from_an_untrusted_peer(self, trusted):
        trusted("172.18.0.0/16")
        assert client_ip(_request("198.51.100.4", "1.2.3.4")) == "198.51.100.4"

    def test_trusted_proxy_forwards_the_real_client(self, trusted):
        trusted("172.18.0.0/16")
        assert client_ip(_request("172.18.0.5", "203.0.113.9")) == "203.0.113.9"

    def test_a_client_cannot_spoof_its_address_through_a_trusted_proxy(self, trusted):
        """nginx appends the real peer; the spoofed leftmost entry must not win."""
        trusted("172.18.0.0/16")
        assert client_ip(_request("172.18.0.5", "1.1.1.1, 203.0.113.9")) == "203.0.113.9"

    def test_multiple_trusted_hops_are_skipped(self, trusted):
        trusted("172.18.0.0/16, 10.0.0.1")
        assert client_ip(_request("172.18.0.5", "203.0.113.9, 10.0.0.1, 172.18.0.2")) == "203.0.113.9"

    @pytest.mark.parametrize("header", ["not-an-ip", "203.0.113.9, garbage", "999.1.1.1"])
    def test_malformed_forwarded_header_falls_back_to_the_peer(self, trusted, header):
        trusted("172.18.0.0/16")
        assert client_ip(_request("172.18.0.5", header)) == "172.18.0.5"

    def test_trusted_peer_without_a_header_uses_the_peer(self, trusted):
        trusted("172.18.0.0/16")
        assert client_ip(_request("172.18.0.5")) == "172.18.0.5"

    def test_ipv4_mapped_ipv6_peer_matches_an_ipv4_trusted_range(self, trusted):
        trusted("172.18.0.0/16")
        assert client_ip(_request("::ffff:172.18.0.5", "203.0.113.9")) == "203.0.113.9"

    def test_invalid_trusted_entries_are_ignored_not_fatal(self, trusted):
        trusted("garbage, 172.18.0.0/16, 300.1.1.1/8")
        assert client_ip(_request("172.18.0.5", "203.0.113.9")) == "203.0.113.9"
        assert client_ip(_request("198.51.100.4", "1.2.3.4")) == "198.51.100.4"

    def test_missing_client_is_handled(self, trusted):
        trusted("172.18.0.0/16")
        assert client_ip(_request(None)) == "unknown"


class TestRateLimitBuckets:
    def _exhaust(self, request, bucket="auth"):
        from fastapi import HTTPException

        for _ in range(10):
            enforce_rate_limit(request, bucket)
        with pytest.raises(HTTPException) as exc:
            enforce_rate_limit(request, bucket)
        assert exc.value.status_code == 429

    def test_clients_behind_a_trusted_proxy_get_separate_buckets(self, trusted):
        """The confirmed bug: every client behind the proxy shared one bucket."""
        trusted("172.18.0.0/16")
        self._exhaust(_request("172.18.0.5", "203.0.113.1"))
        enforce_rate_limit(_request("172.18.0.5", "203.0.113.2"), "auth")  # a different client is unaffected

    def test_rotating_a_forged_header_does_not_evade_the_limit_without_a_trusted_proxy(self, trusted):
        trusted("")
        self._exhaust(_request("198.51.100.4", "9.9.9.1"))
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            enforce_rate_limit(_request("198.51.100.4", "9.9.9.2"), "auth")

    def test_rotating_a_forged_header_does_not_evade_the_limit_through_a_trusted_proxy(self, trusted):
        trusted("172.18.0.0/16")
        self._exhaust(_request("172.18.0.5", "9.9.9.1, 203.0.113.1"))
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            enforce_rate_limit(_request("172.18.0.5", "9.9.9.2, 203.0.113.1"), "auth")
