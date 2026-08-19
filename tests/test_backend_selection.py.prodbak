import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yrobot.app_config import register_settings_routes
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
