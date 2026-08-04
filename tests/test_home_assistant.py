import json

from yrobot.config import Settings
from yrobot.home_assistant import HomeAssistantAction, HomeAssistantClient, HomeAssistantController


def test_whitelist_phrase_matches_one_action(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "客厅灯",
                    "phrases": ["打开客厅灯", "客厅灯打开"],
                    "service": "light.turn_on",
                    "entity_id": "light.living_room",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings(
        ha_enabled=True,
        ha_url="http://ha.local:8123",
        ha_token="secret",
        ha_whitelist_path=str(path),
    )
    controller = HomeAssistantController.from_settings(settings, caller=lambda action: None)

    result = controller.handle_text("好的，我现在打开客厅灯。", "resp-1")

    assert result is not None
    assert result.action.name == "客厅灯"
    assert result.action.service == "light.turn_on"
    assert result.action.entity_id == "light.living_room"
    assert result.ok is True


def test_unknown_text_returns_none(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text("[]", encoding="utf-8")
    settings = Settings(
        ha_enabled=True,
        ha_url="http://ha.local:8123",
        ha_token="secret",
        ha_whitelist_path=str(path),
    )
    controller = HomeAssistantController.from_settings(settings, caller=lambda action: None)

    assert controller.handle_text("只是聊天，不控制设备。", "resp-1") is None


def test_duplicate_response_does_not_fire_same_action_twice(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "客厅灯",
                    "phrases": ["打开客厅灯"],
                    "service": "light.turn_on",
                    "entity_id": "light.living_room",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls = []
    settings = Settings(
        ha_enabled=True,
        ha_url="http://ha.local:8123",
        ha_token="secret",
        ha_whitelist_path=str(path),
    )
    controller = HomeAssistantController.from_settings(settings, caller=calls.append)

    first = controller.handle_text("打开客厅灯", "resp-1")
    second = controller.handle_text("打开客厅灯", "resp-1")

    assert first is not None
    assert second is None
    assert len(calls) == 1


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b"{}"


def test_rest_client_sends_expected_home_assistant_request():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data
        captured["timeout"] = timeout
        return FakeResponse()

    client = HomeAssistantClient("http://ha.local:8123/", "secret-token", opener=opener)
    action = HomeAssistantAction("客厅灯", ("打开客厅灯",), "light.turn_on", "light.living_room")

    client.call(action)

    assert captured["url"] == "http://ha.local:8123/api/services/light/turn_on"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"] == b'{"entity_id": "light.living_room"}'
    assert captured["timeout"] == 5.0


def test_controller_converts_rest_failure_to_failed_result(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "客厅灯",
                    "phrases": ["打开客厅灯"],
                    "service": "light.turn_on",
                    "entity_id": "light.living_room",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings(
        ha_enabled=True,
        ha_url="http://ha.local:8123",
        ha_token="secret",
        ha_whitelist_path=str(path),
    )
    controller = HomeAssistantController.from_settings(
        settings,
        caller=lambda action: (_ for _ in ()).throw(RuntimeError("HA unavailable")),
    )

    result = controller.handle_text("打开客厅灯", "resp-1")

    assert result is not None
    assert result.ok is False
    assert "HA unavailable" in result.detail
