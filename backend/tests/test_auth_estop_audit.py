"""Regression tests from the focused Authentication/Authorization + Emergency Stop audit.

Confirmed bugs guarded here:

Emergency Stop
  E1. A failed persist was swallowed and the API still answered 200 "engaged".
  E2. Engaging shared the 10-per-5-minutes bucket with the status poll the dashboard fires every
      15 seconds, so the kill switch itself could be answered 429.
  E3. The recorded actor was a client-supplied string, not the authenticated principal.
  E4. A session holding only `emergency_stop` could release the stop it had engaged.
  E5. A state file that vanished, could not be stat'ed, or lived in unwritable storage left the
      process running instead of failing closed.
  E6. A later activation with a narrower (or empty) policy weakened a stop already in force.
  E7. Activation/resume had no cross-process exclusion.
  E8. Nothing stopped a non-human identity (worker, scheduler, agent) from calling resume().

Authentication / authorization
  A1. A tool declaring no permissions passed every policy, including the deny-all default.
  A2. Unknown-account logins skipped the PBKDF2 work, leaking which emails exist via timing.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from backend.app.security import emergency_stop as es_module
from backend.app.security.auth import Scope
from backend.app.security.emergency_stop import (
    EmergencyStopAuthorizationError,
    EmergencyStopController,
    EmergencyStopPersistenceError,
    StopPolicy,
    emergency_stop,
)
from backend.app.tools.base import BaseTool, SideEffect, ToolResult
from backend.app.tools.registry import DEFAULT_POLICY, AuthorizationPolicy, ToolRegistry
from backend.tests.test_api_auth import _make_user, client, db_session_factory  # noqa: F401


def _controller(tmp_path, name="estop.json") -> EmergencyStopController:
    return EmergencyStopController(str(tmp_path / name))


def _break_persist(monkeypatch, controller):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(controller, "_persist", boom)


# ======================================================================
# E1 persistence failure is never reported as success
# ======================================================================
def test_activate_raises_when_persist_fails_but_stays_stopped_locally(tmp_path, monkeypatch):
    ctl = _controller(tmp_path)
    _break_persist(monkeypatch, ctl)
    with pytest.raises(EmergencyStopPersistenceError):
        ctl.activate("incident", "alice@test.local")
    assert ctl.is_stopped, "a failed save must never un-stop the local process"
    assert ctl.status()["persisted"] is False
    assert not ctl.can_execute_tasks() and not ctl.can_sign() and not ctl.can_submit_transactions()


@pytest.mark.asyncio
async def test_api_engage_returns_503_not_success_when_persist_fails(client, db_session_factory, monkeypatch):
    token, _ = await _make_user(db_session_factory, scopes={Scope.EMERGENCY_STOP.value})
    _break_persist(monkeypatch, emergency_stop)
    response = await client.post(
        "/api/v1/emergency-stop",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "persistence failure drill"},
    )
    assert response.status_code == 503
    body = response.json()["detail"]
    assert body["persisted"] is False and body["engaged_in_this_process"] is True
    assert emergency_stop.is_stopped


# ======================================================================
# E2/E3/E4 API behaviour
# ======================================================================
@pytest.mark.asyncio
async def test_status_polling_cannot_lock_out_engage(client, db_session_factory):
    token, _ = await _make_user(db_session_factory, scopes={Scope.EMERGENCY_STOP.value, Scope.READ.value})
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(40):  # far past the old 10-per-5-minutes bucket
        assert (await client.get("/api/v1/emergency-stop", headers=headers)).status_code == 200
    response = await client.post("/api/v1/emergency-stop", headers=headers, json={"reason": "after heavy polling"})
    assert response.status_code == 200
    assert emergency_stop.is_stopped


@pytest.mark.asyncio
async def test_engage_records_authenticated_actor_not_client_supplied_name(client, db_session_factory):
    token, email = await _make_user(db_session_factory, scopes={Scope.EMERGENCY_STOP.value})
    response = await client.post(
        "/api/v1/emergency-stop",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "identity check", "activated_by": "someone-else@evil.test"},
    )
    assert response.status_code == 200
    assert response.json()["by"] == email
    assert emergency_stop.status()["activated_by"] == email


@pytest.mark.asyncio
async def test_stop_only_scope_cannot_release(client, db_session_factory):
    stop_only, _ = await _make_user(db_session_factory, scopes={Scope.EMERGENCY_STOP.value})
    headers = {"Authorization": f"Bearer {stop_only}"}
    assert (await client.post("/api/v1/emergency-stop", headers=headers, json={"reason": "panic button"})).status_code == 200
    assert (await client.post("/api/v1/emergency-stop/resume", headers=headers)).status_code == 403
    assert emergency_stop.is_stopped, "a 403 must leave the stop in force"


@pytest.mark.asyncio
async def test_admin_with_stop_scope_can_release(client, db_session_factory):
    token, email = await _make_user(
        db_session_factory, scopes={Scope.EMERGENCY_STOP.value, Scope.SYSTEM_ADMIN.value}
    )
    headers = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/emergency-stop", headers=headers, json={"reason": "drill"})
    response = await client.post("/api/v1/emergency-stop/resume", headers=headers)
    assert response.status_code == 200
    assert response.json()["by"] == email
    assert not emergency_stop.is_stopped


@pytest.mark.asyncio
async def test_unauthenticated_and_read_only_cannot_release(client, db_session_factory):
    assert (await client.post("/api/v1/emergency-stop/resume")).status_code == 401
    token, _ = await _make_user(db_session_factory, scopes={Scope.READ.value, Scope.SYSTEM_ADMIN.value})
    response = await client.post("/api/v1/emergency-stop/resume", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_release_failure_keeps_stop_engaged(client, db_session_factory, monkeypatch):
    token, _ = await _make_user(
        db_session_factory, scopes={Scope.EMERGENCY_STOP.value, Scope.SYSTEM_ADMIN.value}
    )
    headers = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/emergency-stop", headers=headers, json={"reason": "drill"})
    _break_persist(monkeypatch, emergency_stop)
    response = await client.post("/api/v1/emergency-stop/resume", headers=headers)
    assert response.status_code == 503
    assert emergency_stop.is_stopped


# ======================================================================
# E5 restart / unreliable storage fails closed
# ======================================================================
def test_stop_survives_restart(tmp_path):
    ControllerA = _controller(tmp_path)
    ControllerA.activate("incident", "alice@test.local")
    restarted = _controller(tmp_path)
    assert restarted.is_stopped
    assert restarted.status()["activated_by"] == "alice@test.local"


def test_resume_survives_restart(tmp_path):
    first = _controller(tmp_path)
    first.activate("incident", "alice@test.local")
    first.resume("alice@test.local")
    assert not _controller(tmp_path).is_stopped


def test_corrupt_state_file_fails_closed_on_startup(tmp_path):
    (tmp_path / "estop.json").write_text("{not json", encoding="utf-8")
    assert _controller(tmp_path).is_stopped


def test_wrong_shape_state_file_fails_closed(tmp_path):
    (tmp_path / "estop.json").write_text(json.dumps({"stopped": "no"}), encoding="utf-8")
    assert _controller(tmp_path).is_stopped


def test_deleted_state_file_fails_closed_in_running_process(tmp_path):
    ctl = _controller(tmp_path)
    ctl.activate("incident", "alice@test.local")
    ctl.resume("alice@test.local")
    assert not ctl.is_stopped
    (tmp_path / "estop.json").unlink()
    assert ctl.is_stopped, "a vanished state file means the true state is unknown"
    with pytest.raises(es_module.EmergencyStopError):
        ctl.assert_running("task_execution")
    # Only an explicit operator action clears it, and that re-creates the file.
    ctl.resume("alice@test.local")
    assert not ctl.is_stopped and (tmp_path / "estop.json").exists()


def test_unreadable_state_storage_fails_closed(tmp_path, monkeypatch):
    ctl = _controller(tmp_path)
    ctl.activate("incident", "alice@test.local")
    ctl.resume("alice@test.local")

    def denied():
        raise PermissionError("denied")

    monkeypatch.setattr(ctl, "_signature", denied)
    assert ctl.is_stopped


def test_unwritable_storage_at_startup_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "access", lambda *a, **k: False)
    ctl = _controller(tmp_path, "fresh.json")
    assert ctl.is_stopped
    assert ctl.status()["persisted"] is False


def test_fresh_writable_storage_starts_running(tmp_path):
    assert not _controller(tmp_path, "fresh.json").is_stopped


def test_second_process_view_sees_stop_immediately(tmp_path):
    api, worker = _controller(tmp_path), _controller(tmp_path)
    assert not worker.can_execute_tasks() is False  # running
    api.activate("incident", "alice@test.local")
    assert not worker.can_execute_tasks()
    assert not worker.can_submit_transactions()
    assert not worker.can_run_jobs()


# ======================================================================
# E6 a stop is never weakened; empty policy is not a no-op stop
# ======================================================================
def test_reactivation_cannot_narrow_an_active_stop(tmp_path):
    ctl = _controller(tmp_path)
    ctl.activate("full", "alice@test.local")
    ctl.activate("narrow", "alice@test.local", policy={StopPolicy.HALT_TRANSACTION_SUBMISSION})
    assert not ctl.can_execute_tasks() and not ctl.can_sign() and not ctl.can_run_jobs()


def test_empty_or_invalid_policy_engages_the_full_stop(tmp_path):
    for bad in (set(), {"not_a_real_policy"}):
        ctl = _controller(tmp_path, f"p{len(bad)}.json")
        ctl.activate("x", "alice@test.local", policy=bad)
        assert ctl.is_stopped
        assert not ctl.can_execute_tasks() and not ctl.can_sign() and not ctl.can_submit_transactions()


# ======================================================================
# E7 concurrency
# ======================================================================
def test_concurrent_activate_and_resume_leave_consistent_valid_state(tmp_path):
    a, b = _controller(tmp_path), _controller(tmp_path)
    errors: list[BaseException] = []

    def hammer(ctl, activate):
        try:
            for _ in range(15):
                if activate:
                    ctl.activate("race", "alice@test.local")
                else:
                    try:
                        ctl.resume("bob@test.local")
                    except EmergencyStopPersistenceError:
                        pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(a, True)), threading.Thread(target=hammer, args=(b, False))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    data = json.loads((tmp_path / "estop.json").read_text(encoding="utf-8"))
    assert isinstance(data["stopped"], bool)
    # A final activation always wins: the stop can never be lost to an interleaved resume.
    a.activate("final", "alice@test.local")
    assert _controller(tmp_path).is_stopped and b.is_stopped


# ======================================================================
# E8 only a human can release
# ======================================================================
@pytest.mark.parametrize("actor", ["worker", "system", "scheduler", "agent", "environment", "", "  Anonymous "])
def test_non_human_identities_cannot_release(tmp_path, actor):
    ctl = _controller(tmp_path)
    ctl.activate("incident", "alice@test.local")
    with pytest.raises(EmergencyStopAuthorizationError):
        ctl.resume(actor)
    assert ctl.is_stopped


# ======================================================================
# Blocked execution at the boundary (not only the API)
# ======================================================================
class _WebTool(BaseTool):
    name = "web_act"
    description = "interacts with a page"
    required_permissions = ["browser.interact"]
    side_effect = SideEffect.WEB_INTERACTION
    input_schema = {"type": "object", "properties": {}, "required": []}
    executed = 0

    async def execute(self, params):
        type(self).executed += 1
        return ToolResult.success(data={})


@pytest.mark.asyncio
async def test_active_stop_blocks_tool_execution_when_state_is_uncertain(tmp_path, monkeypatch):
    """Corrupt state => stopped => the registry refuses, and the tool body never runs."""
    _WebTool.executed = 0
    registry = ToolRegistry()
    registry.register(_WebTool())
    state = emergency_stop._state_file
    state.write_text("garbage", encoding="utf-8")
    result = await registry.invoke(
        "web_act", {}, policy=AuthorizationPolicy(enabled_permissions=["browser.interact"])
    )
    assert result.error_category == "EMERGENCY_STOP"
    assert _WebTool.executed == 0


# ======================================================================
# A1 tools must declare permissions
# ======================================================================
class _NoPermsTool(BaseTool):
    name = "no_perms"
    description = "forgot to declare permissions"
    required_permissions = ()
    side_effect = SideEffect.STATE_MUTATION
    input_schema = {"type": "object", "properties": {}, "required": []}
    executed = 0

    async def execute(self, params):
        type(self).executed += 1
        return ToolResult.success(data={})


@pytest.mark.asyncio
async def test_tool_without_declared_permissions_is_refused_by_every_policy():
    _NoPermsTool.executed = 0
    registry = ToolRegistry()
    registry.register(_NoPermsTool())
    for policy in (DEFAULT_POLICY, AuthorizationPolicy(), AuthorizationPolicy(enabled_permissions=["anything"])):
        result = await registry.invoke("no_perms", {}, policy=policy)
        assert result.error_category == "MISSING_PERMISSION"
    assert _NoPermsTool.executed == 0


def test_default_policy_is_deny_all_for_value_and_web_actions():
    assert not DEFAULT_POLICY.allow_web_interaction
    assert not DEFAULT_POLICY.allow_transaction_submission
    assert not DEFAULT_POLICY.allow_wallet_signing


# ======================================================================
# A2 login timing
# ======================================================================
@pytest.mark.asyncio
async def test_unknown_account_login_still_does_password_work(client, db_session_factory, monkeypatch):
    calls: list[str] = []
    from backend.app.api.v1.endpoints import auth as auth_endpoint

    real = auth_endpoint.verify_against_dummy
    monkeypatch.setattr(auth_endpoint, "verify_against_dummy", lambda pw: (calls.append(pw), real(pw))[1])
    response = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@test.local", "password": "whatever-password"}
    )
    assert response.status_code == 401
    assert calls == ["whatever-password"]
