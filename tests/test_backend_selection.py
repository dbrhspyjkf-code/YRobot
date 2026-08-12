import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import yrobot.app_config as app_config_module
from yrobot.app_config import build_status, register_settings_routes
from yrobot.config import Settings
from yrobot.state import RUNTIME_HEALTH


def test_backend_defaults_to_xiaozhi():
    assert Settings.from_env({}).conversation_backend == "xiaozhi"


@pytest.mark.parametrize("value", ["xiaozhi", "qwen"])
def test_backend_accepts_supported_values(value):
    assert Settings.from_env({"YROBOT_CONVERSATION_BACKEND": value}).conversation_backend == value


def test_backend_rejects_unknown_value():
    with pytest.raises(ValueError, match="YROBOT_CONVERSATION_BACKEND"):
        Settings.from_env({"YROBOT_CONVERSATION_BACKEND": "both"})


def test_qwen_secret_is_read_but_not_in_repr():
    settings = Settings.from_env({"DASHSCOPE_API_KEY": "secret-value"})

    assert settings.qwen_api_key == "secret-value"
    assert "secret-value" not in repr(settings)


def test_backend_api_persists_choice_and_reports_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("YROBOT_CONVERSATION_BACKEND", "xiaozhi")
    env_path = tmp_path / "ha.env"
    RUNTIME_HEALTH.update(backend="xiaozhi", ws_state="connected", last_error=None)
    app = FastAPI()
    register_settings_routes(app, vad_env_path=env_path)
    client = TestClient(app)

    response = client.put("/api/conversation/backend", json={"backend": "qwen"})

    assert response.status_code == 200
    assert response.json() == {"configured_backend": "qwen", "restart_required": True}
    assert env_path.read_text() == "YROBOT_CONVERSATION_BACKEND=qwen\n"
    assert client.get("/api/conversation/backend").json() == {
        "configured_backend": "qwen",
        "running_backend": "xiaozhi",
        "connection_state": "connected",
        "error": None,
    }


def test_backend_api_rejects_unknown_value(tmp_path):
    app = FastAPI()
    register_settings_routes(app, vad_env_path=tmp_path / "ha.env")

    response = TestClient(app).put("/api/conversation/backend", json={"backend": "both"})

    assert response.status_code == 422


def test_backend_api_rejects_non_string_value(tmp_path):
    app = FastAPI()
    register_settings_routes(app, vad_env_path=tmp_path / "ha.env")

    response = TestClient(app, raise_server_exceptions=False).put(
        "/api/conversation/backend", json={"backend": ["qwen"]}
    )

    assert response.status_code == 422


@pytest.fixture
def fresh_runtime_health():
    """Snapshot the runtime health so each test starts clean.

    YRobot is a long-lived process; tests in this module would otherwise
    leak state between them and produce flaky assertions on the
    configured_backend / running_backend split.
    """
    snapshot = RUNTIME_HEALTH.snapshot()
    RUNTIME_HEALTH.update(
        backend="xiaozhi",
        ws_state="not_started",
        session_id=None,
        reconnects=0,
        tts_active=False,
        tts_packets=0,
        audio_queue=0,
        audio_dropped=0,
        last_rx_at=None,
        last_tts_packet_at=None,
        last_error=None,
    )
    yield RUNTIME_HEALTH
    RUNTIME_HEALTH.update(**snapshot)


@pytest.fixture
def no_volume_controller(monkeypatch):
    """build_status reads the speaker volume via amixer; the test bench has
    no audio hardware. Stub the volume controller so the field becomes None
    instead of raising FileNotFoundError.
    """
    class StubVolume:
        PCM_CONTROL = "PCM"
        MIN_PERCENT = 0
        MAX_PERCENT = 100

        def __init__(self):
            self.range = (0, 100)

        def read_percent(self):
            return None

    monkeypatch.setattr(app_config_module, "volume_controller_singleton", lambda: StubVolume())


def test_build_status_reports_xiaozhi_when_runtime_is_xiaozhi(
    fresh_runtime_health, no_volume_controller
):
    fresh_runtime_health.update(backend="xiaozhi", ws_state="connected", last_error=None)

    status = build_status({})

    assert status["conversation"]["backend"] == "xiaozhi"


def test_build_status_reports_qwen_when_runtime_is_qwen(
    fresh_runtime_health, no_volume_controller
):
    fresh_runtime_health.update(backend="qwen", ws_state="connected", last_error=None)

    status = build_status({"YROBOT_CONVERSATION_BACKEND": "qwen"})

    assert status["conversation"]["backend"] == "qwen"


def test_build_status_keeps_qwen_when_qwen_failed(
    fresh_runtime_health, no_volume_controller
):
    """QWEN failure must surface as 'qwen' + non-empty error, not fall back
    to xiaozhi. This guards the project invariant that QWEN failures stay
    visible and are never silently replaced with XIAOZHI."""
    fresh_runtime_health.update(
        backend="qwen",
        ws_state="failed",
        last_error="dashscope connection refused",
    )

    status = build_status({"YROBOT_CONVERSATION_BACKEND": "qwen"})

    assert status["conversation"]["backend"] == "qwen"
    assert status["runtime"]["last_error"] == "dashscope connection refused"


def test_build_status_does_not_leak_qwen_voice_into_status(
    fresh_runtime_health, no_volume_controller
):
    fresh_runtime_health.update(backend="qwen", ws_state="connected", last_error=None)

    status = build_status(
        {
            "YROBOT_CONVERSATION_BACKEND": "qwen",
            "YROBOT_QWEN_VOICE": "secret-voice",
            "DASHSCOPE_API_KEY": "secret-key",
        }
    )

    rendered = str(status)
    assert "secret-voice" not in rendered
    assert "secret-key" not in rendered


def test_get_conversation_backend_distinguishes_configured_from_running(
    fresh_runtime_health,
):
    """running_backend comes from the live runtime (RUNTIME_HEALTH);
    configured_backend comes from the env that started this process.
    They are independent: saving a new backend never rewrites
    running_backend without an explicit restart.
    """
    fresh_runtime_health.update(backend="xiaozhi", ws_state="connected", last_error=None)
    app = FastAPI()
    register_settings_routes(app)
    client = TestClient(app)

    response = client.get("/api/conversation/backend")

    payload = response.json()
    assert response.status_code == 200
    assert payload["running_backend"] == "xiaozhi"
    assert payload["connection_state"] == "connected"
    assert payload["error"] is None


def test_get_conversation_backend_reports_qwen_failed_without_fallback(
    fresh_runtime_health,
):
    """When QWEN is the running backend and the WebSocket fails, the API
    must continue to show running_backend='qwen' and the last error.
    It must NOT fall back to xiaozhi.
    """
    fresh_runtime_health.update(
        backend="qwen",
        ws_state="failed",
        last_error="dashscope auth failed",
    )
    app = FastAPI()
    register_settings_routes(app)
    client = TestClient(app)

    response = client.get("/api/conversation/backend")

    payload = response.json()
    assert response.status_code == 200
    assert payload["running_backend"] == "qwen"
    assert payload["connection_state"] == "failed"
    assert payload["error"] == "dashscope auth failed"
