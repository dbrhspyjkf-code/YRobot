"""Dashboard configuration persistence and API tests."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yrobot.app_config import AppConfig, build_status, register_settings_routes, validate_document


def _document(**overrides):
    document = {
        "gateway_url": "gateway.example.test",
        "tls_verify": True,
        "video_enabled": True,
        "proactive_enabled": True,
        "persona": "你是一个简洁、友好的桌面机器人。",
    }
    document.update(overrides)
    return document


def test_validate_document_normalizes_mode_and_disables_proactive_in_audio():
    document = validate_document(_document(video_enabled=False))
    assert document["gateway_url"] == "wss://gateway.example.test/v1/realtime?mode=audio"
    assert document["proactive_enabled"] is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"persona": "line one\nline two"}, "single line"),
        ({"persona": "x" * 241}, "240"),
        ({"gateway_url": "https://example.com"}, "ws://"),
    ],
)
def test_validate_document_rejects_unsafe_values(override, message):
    with pytest.raises(ValueError, match=message):
        validate_document(_document(**override))


def test_store_round_trip_and_environment_precedence(tmp_path):
    path = tmp_path / "config" / "settings.json"
    store = AppConfig(path)

    saved = store.save(_document(), {})
    assert saved["video_enabled"] is True
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["persona"].startswith("你是")

    effective = store.view({"YROBOT_PERSONA": "Managed persona."})
    assert effective["persona"] == "Managed persona."
    assert effective["environment_overrides"] == ["persona"]


def test_invalid_persisted_file_falls_back_to_defaults(tmp_path, caplog):
    path = tmp_path / "settings.json"
    path.write_text("not-json", encoding="utf-8")
    view = AppConfig(path).view({})
    assert view["video_enabled"] is True
    assert "ignoring invalid settings file" in caplog.text


def test_settings_api_saves_and_reports_restart(tmp_path):
    store = AppConfig(tmp_path / "settings.json")
    app = FastAPI()
    register_settings_routes(app, store, get_environment=lambda: {})
    client = TestClient(app)

    initial = client.get("/api/settings")
    assert initial.status_code == 200

    response = client.put("/api/settings", json=_document(video_enabled=False))
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert response.json()["settings"]["video_enabled"] is False


def test_settings_api_rejects_unknown_fields(tmp_path):
    store = AppConfig(tmp_path / "settings.json")
    app = FastAPI()
    register_settings_routes(app, store, get_environment=lambda: {})
    client = TestClient(app)

    response = client.put("/api/settings", json={**_document(), "token": "nope"})
    assert response.status_code == 422
    assert "unknown: token" in response.json()["detail"]


def test_build_status_reports_safe_runtime_state(tmp_path):
    store = AppConfig(tmp_path / "settings.json")
    status = build_status(
        store,
        {
            "YROBOT_HA_ENABLED": "1",
            "YROBOT_HA_URL": "http://homeassistant.local:8123",
            "YROBOT_HA_TOKEN": "super-secret-token",
            "YROBOT_HERMES_TOOLS_ENABLED": "1",
            "YROBOT_HERMES_TOOLS_URL": "http://192.168.1.200:8766",
            "YROBOT_PROACTIVE": "0",
        },
    )

    assert status["service"]["state"] == "running"
    assert status["conversation"]["proactive_enabled"] is False
    assert status["integrations"]["home_assistant"]["configured"] is True
    assert status["integrations"]["hermes_tools"]["enabled"] is True
    assert status["integrations"]["local_info"]["enabled"] is True
    assert "super-secret-token" not in json.dumps(status)


def test_status_api_returns_status(tmp_path):
    store = AppConfig(tmp_path / "settings.json")
    app = FastAPI()
    register_settings_routes(app, store, get_environment=lambda: {})
    client = TestClient(app)

    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["status"]["service"]["name"] == "YRobot"
