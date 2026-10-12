"""Tests for: state-file loss detected at startup (marker / strict setting) and the live-gateway
input endpoint being refused while the Emergency Stop is active.

Limits: the marker lives next to the state file, so it detects a deleted FILE, not a lost data
DIRECTORY. The strict setting (EMERGENCY_STOP_REQUIRE_STATE) is what covers a lost directory.
Run on Linux only; nothing here proves Windows or PostgreSQL behaviour.
"""

from __future__ import annotations

import pytest

from backend.app.core.config import settings
from backend.app.security.emergency_stop import EmergencyStopController, emergency_stop


def _marker(state_file):
    return state_file.with_name(state_file.name + ".initialized")


class TestStateLossAtStartup:
    def test_first_boot_without_marker_starts_running(self, tmp_path):
        c = EmergencyStopController(str(tmp_path / "stop.json"))
        assert c.is_stopped is False

    def test_marker_is_written_after_first_persist(self, tmp_path):
        f = tmp_path / "stop.json"
        EmergencyStopController(str(f)).activate("incident", "operator")
        assert _marker(f).exists()

    def test_restart_with_deleted_state_file_fails_closed(self, tmp_path):
        f = tmp_path / "stop.json"
        c = EmergencyStopController(str(f))
        c.activate("incident", "operator")
        c.resume("operator")
        f.unlink()  # state file lost, marker remains

        restarted = EmergencyStopController(str(f))
        assert restarted.is_stopped is True
        assert "missing at startup" in (restarted._reason or "")

    def test_operator_resume_clears_the_missing_state_stop(self, tmp_path):
        f = tmp_path / "stop.json"
        c = EmergencyStopController(str(f))
        c.activate("incident", "operator")
        c.resume("operator")
        f.unlink()

        restarted = EmergencyStopController(str(f))
        assert restarted.is_stopped is True
        restarted.resume("operator")
        assert restarted.is_stopped is False
        assert f.exists()
        assert EmergencyStopController(str(f)).is_stopped is False

    def test_normal_restart_with_state_file_is_unchanged(self, tmp_path):
        f = tmp_path / "stop.json"
        c = EmergencyStopController(str(f))
        c.activate("incident", "operator")
        c.resume("operator")
        assert EmergencyStopController(str(f)).is_stopped is False

    def test_strict_setting_makes_missing_file_stopped_on_first_boot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "EMERGENCY_STOP_REQUIRE_STATE", True, raising=False)
        c = EmergencyStopController(str(tmp_path / "stop.json"))
        assert c.is_stopped is True
        c.resume("operator")
        assert c.is_stopped is False

    def test_marker_write_failure_does_not_break_activation(self, tmp_path, monkeypatch):
        f = tmp_path / "stop.json"
        c = EmergencyStopController(str(f))

        def boom(*a, **k):
            raise OSError("read-only")

        monkeypatch.setattr(type(c._marker_path), "touch", boom)
        c.activate("incident", "operator")
        assert c.is_stopped is True
        assert f.exists()


class TestLiveInputRefusedWhileStopped:
    @pytest.fixture
    def gateway(self):
        from starlette.testclient import TestClient

        from backend.app.live.gateway import build_app
        from backend.app.live.security import GATEWAY_HEADER, gateway_secret

        client = TestClient(build_app())
        client.headers.update({GATEWAY_HEADER: gateway_secret()})
        return client

    def test_input_is_refused_while_stopped(self, gateway):
        emergency_stop.activate("test", "operator")
        r = gateway.post("/sessions/browser/abc/input", json={"type": "click", "x": 1, "y": 1})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "emergency_stop"

    def test_input_is_not_refused_by_the_stop_when_running(self, gateway):
        r = gateway.post("/sessions/no-such-integration/abc/input", json={"type": "click"})
        assert r.status_code != 409

    def test_reads_and_pause_still_work_while_stopped(self, gateway):
        emergency_stop.activate("test", "operator")
        assert gateway.get("/health").status_code == 200
        pause = gateway.post("/sessions/browser/abc/action", json={"action": "pause"})
        assert pause.status_code != 409
