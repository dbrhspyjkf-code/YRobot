import subprocess
import sys
import types

sys.modules.setdefault("cv2", types.ModuleType("cv2"))
fastapi_module = types.ModuleType("fastapi")
fastapi_module.FastAPI = object
fastapi_module.Response = object

class HTTPException(Exception):
    def __init__(self, status_code=None, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

fastapi_module.HTTPException = HTTPException
sys.modules.setdefault("fastapi", fastapi_module)
from yrobot import app_config


def test_pi_power_state_parses_seen_under_voltage_and_throttling(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="throttled=0x50000\n", stderr="")

    monkeypatch.setattr(app_config.subprocess, "run", fake_run)

    power = app_config._read_pi_power_state()

    assert power["available"] is True
    assert power["raw"] == "0x50000"
    assert power["under_voltage"] is False
    assert power["under_voltage_seen"] is True
    assert power["throttled"] is False
    assert power["throttled_seen"] is True


def test_pi_power_state_reports_unavailable_without_vcgencmd(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("vcgencmd")

    monkeypatch.setattr(app_config.subprocess, "run", fake_run)

    power = app_config._read_pi_power_state()

    assert power["available"] is False
    assert power["under_voltage_seen"] is False


def test_reachy_daemon_status_keeps_partial_data_when_one_endpoint_fails():
    payloads = {
        "/api/daemon/status": {
            "state": "running",
            "version": "1.8.4",
            "hardware_id": "unit-1234",
            "robot_name": "reachy",
            "backend_status": {"motor_control_mode": "enabled"},
        },
        "/api/daemon/robot-app-lock-status": RuntimeError("boom"),
        "/api/state/doa": {"angle": 1.57, "speech_detected": True},
    }

    def fake_fetch(base_url, path):
        value = payloads[path]
        if isinstance(value, Exception):
            return None, str(value)
        return value, None

    daemon = app_config._read_reachy_daemon_status(fetch=fake_fetch)

    assert daemon["available"] is True
    assert daemon["daemon_state"] == "running"
    assert daemon["motor_mode"] == "enabled"
    assert daemon["awake"] is True
    assert daemon["doa_angle_rad"] == 1.57
    assert daemon["doa_speech_detected"] is True
    assert daemon["errors"] == {"/api/daemon/robot-app-lock-status": "boom"}


def test_reachy_daemon_wake_sleep_restart_use_official_endpoints():
    calls = []

    controller = app_config.ReachyDaemonController(
        status_reader=lambda: {"daemon_state": "running"},
        post=lambda path: calls.append(path),
    )

    assert controller.action("wake")["ok"] is True
    assert calls == ["/api/motors/set_mode/enabled", "/api/move/play/wake_up"]

    calls.clear()
    assert controller.action("sleep")["ok"] is True
    assert calls == ["/api/daemon/stop?goto_sleep=true"]

    calls.clear()
    assert controller.action("restart")["ok"] is True
    assert calls == ["/api/daemon/restart"]


def test_system_power_runs_pre_power_hook_before_command(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(app_config.subprocess, "run", fake_run)

    app_config.SystemController._dispatch_power(
        ["sudo", "-n", "systemctl", "poweroff"],
        "poweroff",
        lambda: calls.append("sleep"),
    )

    assert calls == ["sleep", ["sudo", "-n", "systemctl", "poweroff"]]
