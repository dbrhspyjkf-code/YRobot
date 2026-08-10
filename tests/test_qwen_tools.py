import json
from urllib.error import URLError

import pytest

from yrobot.config import Settings
from yrobot.qwen_tools import ToolExecutor


class FakeResponse:
    def __init__(self, document):
        self.body = json.dumps(document, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body


class RecordingOpener:
    def __init__(self, document=None, error=None):
        self.document = document or {"ok": True}
        self.error = error
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.document)


def write_whitelist(tmp_path, entries):
    path = tmp_path / "whitelist.json"
    path.write_text(json.dumps(entries, ensure_ascii=False))
    return path


def make_settings(tmp_path, entries=()):
    return Settings(
        conversation_backend="qwen",
        ha_enabled=True,
        ha_url="http://homeassistant.local:8123",
        ha_token="private-ha-token",
        ha_whitelist_path=str(write_whitelist(tmp_path, entries)),
        hermes_tools_url="http://192.168.1.200:8766",
    )


def test_schemas_expose_only_explicit_tools(tmp_path):
    executor = ToolExecutor(make_settings(tmp_path))

    assert [item["function"]["name"] for item in executor.schemas()] == [
        "get_weather",
        "get_device_state",
        "control_allowed_device",
    ]
    assert "entity_id" not in json.dumps(executor.schemas())
    assert "token" not in json.dumps(executor.schemas()).lower()


def test_unknown_tool_is_rejected_before_network(tmp_path):
    opener = RecordingOpener()
    executor = ToolExecutor(make_settings(tmp_path), opener=opener)

    result = executor.execute("run_shell", {"command": "poweroff"})

    assert result == {"ok": False, "error": "unknown tool: run_shell"}
    assert opener.calls == []


def test_unknown_device_is_rejected_before_network(tmp_path):
    opener = RecordingOpener()
    executor = ToolExecutor(make_settings(tmp_path), opener=opener)

    result = executor.execute(
        "control_allowed_device", {"device": "不存在的灯", "action": "turn_on"}
    )

    assert result == {"ok": False, "error": "device is not allowlisted"}
    assert opener.calls == []


@pytest.mark.parametrize(
    ("name", "service", "entity_id"),
    [
        ("前门锁", "lock.unlock", "lock.front_door"),
        ("车库门", "cover.open_cover", "cover.garage"),
        ("警报器", "alarm_control_panel.alarm_disarm", "alarm_control_panel.home"),
        ("燃气阀", "switch.turn_on", "switch.gas_valve"),
        ("暖气", "switch.turn_on", "switch.heater"),
    ],
)
def test_high_risk_devices_are_blocked_before_network(
    tmp_path, name, service, entity_id
):
    opener = RecordingOpener()
    settings = make_settings(
        tmp_path,
        [{"name": name, "phrases": [name], "service": service, "entity_id": entity_id}],
    )
    executor = ToolExecutor(settings, opener=opener)

    result = executor.execute(
        "control_allowed_device", {"device": name, "action": "turn_on"}
    )

    assert result == {"ok": False, "error": "device class is blocked"}
    assert opener.calls == []


def test_rest_weather_uses_only_verified_8766_route(tmp_path):
    opener = RecordingOpener({"ok": True, "city": "广州", "temp_c": 30})
    executor = ToolExecutor(make_settings(tmp_path), opener=opener)

    result = executor.execute("get_weather", {"city": "广州"})

    assert result["ok"] is True
    request, timeout = opener.calls[0]
    assert request.full_url == "http://192.168.1.200:8766/weather?city=%E5%B9%BF%E5%B7%9E"
    assert request.get_method() == "GET"
    assert timeout == 5.0


def test_ha_light_action_uses_whitelist_mapping_not_model_entity_id(tmp_path):
    opener = RecordingOpener([])
    settings = make_settings(
        tmp_path,
        [
            {
                "name": "书台灯",
                "phrases": ["打开书台灯"],
                "service": "light.turn_on",
                "entity_id": "light.desk",
            }
        ],
    )
    executor = ToolExecutor(settings, opener=opener)

    result = executor.execute(
        "control_allowed_device",
        {"device": "书台灯", "action": "turn_on", "entity_id": "lock.front_door"},
    )

    assert result == {"ok": True, "device": "书台灯", "action": "turn_on"}
    request, _ = opener.calls[0]
    assert request.full_url == "http://homeassistant.local:8123/api/services/light/turn_on"
    assert json.loads(request.data) == {"entity_id": "light.desk"}
    assert request.headers["Authorization"] == "Bearer private-ha-token"


def test_spoken_control_uses_allowlisted_phrase(tmp_path):
    opener = RecordingOpener([])
    settings = make_settings(
        tmp_path,
        [
            {
                "name": "书台灯",
                "phrases": ["关闭书台灯", "关闭书灯"],
                "service": "switch.turn_off",
                "entity_id": "switch.desk",
            }
        ],
    )
    executor = ToolExecutor(settings, opener=opener)

    result = executor.execute_spoken_control("你好小白，请关闭书灯")

    assert result == {"ok": True, "device": "书台灯", "action": "turn_off"}
    request, _ = opener.calls[0]
    assert request.full_url == "http://homeassistant.local:8123/api/services/switch/turn_off"
    assert json.loads(request.data) == {"entity_id": "switch.desk"}


def test_spoken_control_ignores_unknown_phrase_before_network(tmp_path):
    opener = RecordingOpener([])
    settings = make_settings(
        tmp_path,
        [
            {
                "name": "书台灯",
                "phrases": ["关闭书台灯"],
                "service": "switch.turn_off",
                "entity_id": "switch.desk",
            }
        ],
    )
    executor = ToolExecutor(settings, opener=opener)

    assert executor.execute_spoken_control("关闭保险箱") is None
    assert opener.calls == []


def test_bare_close_can_follow_recent_spoken_device(tmp_path):
    opener = RecordingOpener([])
    settings = make_settings(
        tmp_path,
        [
            {
                "name": "厨房灯",
                "phrases": ["打开厨房灯"],
                "service": "switch.turn_on",
                "entity_id": "switch.kitchen",
            },
            {
                "name": "厨房灯",
                "phrases": ["关闭厨房灯"],
                "service": "switch.turn_off",
                "entity_id": "switch.kitchen",
            },
        ],
    )
    executor = ToolExecutor(settings, opener=opener)

    assert executor.execute_spoken_control("打开厨房灯", now=100.0) == {
        "ok": True,
        "device": "厨房灯",
        "action": "turn_on",
    }
    assert executor.execute_spoken_control("关闭", now=116.0) == {
        "ok": True,
        "device": "厨房灯",
        "action": "turn_off",
    }

    assert len(opener.calls) == 2
    request, _ = opener.calls[1]
    assert request.full_url == "http://homeassistant.local:8123/api/services/switch/turn_off"
    assert json.loads(request.data) == {"entity_id": "switch.kitchen"}


def test_bare_close_without_recent_device_is_ignored(tmp_path):
    opener = RecordingOpener([])
    settings = make_settings(
        tmp_path,
        [
            {
                "name": "厨房灯",
                "phrases": ["关闭厨房灯"],
                "service": "switch.turn_off",
                "entity_id": "switch.kitchen",
            }
        ],
    )
    executor = ToolExecutor(settings, opener=opener)

    assert executor.execute_spoken_control("关闭", now=100.0) is None
    assert opener.calls == []


def test_bare_close_context_expires(tmp_path):
    opener = RecordingOpener([])
    settings = make_settings(
        tmp_path,
        [
            {
                "name": "厨房灯",
                "phrases": ["打开厨房灯"],
                "service": "switch.turn_on",
                "entity_id": "switch.kitchen",
            },
            {
                "name": "厨房灯",
                "phrases": ["关闭厨房灯"],
                "service": "switch.turn_off",
                "entity_id": "switch.kitchen",
            },
        ],
    )
    executor = ToolExecutor(settings, opener=opener)

    executor.execute_spoken_control("打开厨房灯", now=100.0)

    assert executor.execute_spoken_control("关闭", now=140.1) is None
    assert len(opener.calls) == 1


def test_bare_open_is_not_inferred_from_context(tmp_path):
    opener = RecordingOpener([])
    settings = make_settings(
        tmp_path,
        [
            {
                "name": "厨房灯",
                "phrases": ["关闭厨房灯"],
                "service": "switch.turn_off",
                "entity_id": "switch.kitchen",
            },
            {
                "name": "厨房灯",
                "phrases": ["打开厨房灯"],
                "service": "switch.turn_on",
                "entity_id": "switch.kitchen",
            },
        ],
    )
    executor = ToolExecutor(settings, opener=opener)

    executor.execute_spoken_control("关闭厨房灯", now=100.0)

    assert executor.execute_spoken_control("打开", now=110.0) is None
    assert len(opener.calls) == 1


def test_device_state_uses_allowlisted_entity(tmp_path):
    opener = RecordingOpener({"entity_id": "fan.study", "state": "on"})
    settings = make_settings(
        tmp_path,
        [
            {
                "name": "书房风扇",
                "phrases": ["书房风扇"],
                "service": "fan.turn_on",
                "entity_id": "fan.study",
            }
        ],
    )
    executor = ToolExecutor(settings, opener=opener)

    result = executor.execute("get_device_state", {"device": "书房风扇"})

    assert result == {"ok": True, "device": "书房风扇", "state": "on"}
    request, _ = opener.calls[0]
    assert request.full_url == "http://homeassistant.local:8123/api/states/fan.study"


def test_tool_timeout_returns_failure_not_success(tmp_path):
    opener = RecordingOpener(error=URLError("timed out"))
    executor = ToolExecutor(make_settings(tmp_path), opener=opener)

    result = executor.execute("get_weather", {"city": "广州"})

    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_tool_result_is_bounded_and_contains_no_token(tmp_path):
    opener = RecordingOpener({"ok": True, "text": "x" * 5000})
    executor = ToolExecutor(make_settings(tmp_path), opener=opener)

    result = executor.execute("get_weather", {"city": "广州"})
    serialized = json.dumps(result, ensure_ascii=False)

    assert len(serialized.encode()) <= 4096
    assert "private-ha-token" not in serialized
