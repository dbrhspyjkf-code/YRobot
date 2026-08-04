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


def test_one_phrase_can_trigger_multiple_actions_once(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "书台灯",
                    "phrases": ["关灯睡觉"],
                    "service": "switch.turn_off",
                    "entity_id": "switch.desk_light",
                },
                {
                    "name": "电视机",
                    "phrases": ["关灯睡觉"],
                    "service": "switch.turn_on",
                    "entity_id": "switch.tv_speaker_mode",
                },
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

    first = controller.handle_text("好的，关灯睡觉。", "resp-scene")
    second = controller.handle_text("关灯睡觉。", "resp-scene")

    assert first is not None
    assert second is None
    assert [(call.service, call.entity_id) for call in calls] == [
        ("switch.turn_off", "switch.desk_light"),
        ("switch.turn_on", "switch.tv_speaker_mode"),
    ]


def test_split_response_text_matches_after_accumulation(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "书台灯",
                    "phrases": ["打开书台灯"],
                    "service": "switch.turn_on",
                    "entity_id": "switch.desk_light",
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

    first = controller.handle_text("打开书", "resp-1")
    second = controller.handle_text("台灯。", "resp-1")

    assert first is None
    assert second is not None
    assert second.action.name == "书台灯"
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


def test_rest_client_merges_service_data_into_request_body():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data
        return FakeResponse()

    client = HomeAssistantClient("http://ha.local:8123/", "secret-token", opener=opener)
    action = HomeAssistantAction(
        "客厅电视",
        ("电视音量大一点",),
        "media_player.volume_set",
        "media_player.living_room_tv",
        {"volume_level": 0.35},
    )

    client.call(action)

    assert captured["url"] == "http://ha.local:8123/api/services/media_player/volume_set"
    assert json.loads(captured["body"]) == {
        "entity_id": "media_player.living_room_tv",
        "volume_level": 0.35,
    }


def test_whitelist_loads_optional_service_data(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "客厅电视",
                    "phrases": ["电视音量大一点"],
                    "service": "media_player.volume_set",
                    "entity_id": "media_player.living_room_tv",
                    "service_data": {"volume_level": 0.35},
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

    result = controller.handle_text("好的，电视音量大一点。", "resp-volume")

    assert result is not None
    assert calls[0].service_data == {"volume_level": 0.35}


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
