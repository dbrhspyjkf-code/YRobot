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
