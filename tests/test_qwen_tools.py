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


class FakeVolumeController:
    def __init__(self, percent):
        self.percent = percent
        self.writes = []

    def read_percent(self):
        return self.percent

    def write_percent(self, percent):
        self.percent = max(0, min(100, int(percent)))
        self.writes.append(self.percent)
        return self.percent


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


def make_settings_with_hermes(tmp_path, entries=()):
    return Settings(
        conversation_backend="qwen",
        ha_enabled=True,
        ha_url="http://homeassistant.local:8123",
        ha_token="private-ha-token",
        ha_whitelist_path=str(write_whitelist(tmp_path, entries)),
        hermes_tools_enabled=True,
        hermes_tools_url="http://192.168.1.200:8766",
        hermes_ios_api_url="http://192.168.1.200:8900",
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


def test_spoken_control_can_raise_robot_speaker_volume(tmp_path):
    opener = RecordingOpener([])
    volume = FakeVolumeController(88)
    executor = ToolExecutor(make_settings(tmp_path), opener=opener, volume_controller=volume)

    result = executor.execute_spoken_control("你的音量调大")

    assert result == {
        "ok": True,
        "device": "音量",
        "action": "volume_up",
        "volume_percent": 98,
    }
    assert volume.writes == [98]
    assert opener.calls == []


def test_spoken_control_can_raise_robot_speaker_volume_from_joined_asr(tmp_path):
    volume = FakeVolumeController(88)
    executor = ToolExecutor(make_settings(tmp_path), volume_controller=volume)

    result = executor.execute_spoken_control("小白音量调大一下")

    assert result["ok"] is True
    assert result["action"] == "volume_up"
    assert result["volume_percent"] == 98
    assert volume.writes == [98]


def test_spoken_control_can_set_local_speaker_volume_with_chinese_number(tmp_path):
    volume = FakeVolumeController(98)
    executor = ToolExecutor(make_settings(tmp_path), volume_controller=volume)

    result = executor.execute_spoken_control("你的音量到二十")

    assert result == {
        "ok": True,
        "device": "音量",
        "action": "volume_set",
        "volume_percent": 20,
    }
    assert volume.writes == [20]


def test_spoken_control_can_set_local_speaker_volume_with_digits(tmp_path):
    volume = FakeVolumeController(98)
    executor = ToolExecutor(make_settings(tmp_path), volume_controller=volume)

    result = executor.execute_spoken_control("小白音量调到10")

    assert result["ok"] is True
    assert result["action"] == "volume_set"
    assert result["volume_percent"] == 10
    assert volume.writes == [10]


def test_spoken_control_can_set_robot_speaker_volume_without_set_word(tmp_path):
    volume = FakeVolumeController(85)
    executor = ToolExecutor(make_settings(tmp_path), volume_controller=volume)

    result = executor.execute_spoken_control("你的音量七十")

    assert result == {
        "ok": True,
        "device": "音量",
        "action": "volume_set",
        "volume_percent": 70,
    }
    assert volume.writes == [70]


def test_spoken_control_can_lower_robot_speaker_volume(tmp_path):
    volume = FakeVolumeController(25)
    executor = ToolExecutor(make_settings(tmp_path), volume_controller=volume)

    result = executor.execute_spoken_control("你的声音小一点")

    assert result == {
        "ok": True,
        "device": "音量",
        "action": "volume_down",
        "volume_percent": 15,
    }


def test_spoken_control_ignores_ambiguous_speaker_phrase(tmp_path):
    volume = FakeVolumeController(50)
    executor = ToolExecutor(make_settings(tmp_path), volume_controller=volume)

    assert executor.execute_spoken_control("音响") is None
    assert volume.writes == []



class SequenceOpener:
    def __init__(self, documents):
        self.documents = list(documents)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if not self.documents:
            raise AssertionError("unexpected request")
        return FakeResponse(self.documents.pop(0))


def test_spoken_control_sonos_volume_up_steps_by_10_and_caps_at_70(tmp_path):
    opener = SequenceOpener([
        {"entity_id": "media_player.ke_ting", "state": "idle", "attributes": {"volume_level": 0.65}},
        {},
    ])
    executor = ToolExecutor(make_settings_with_hermes(tmp_path), opener=opener)

    result = executor.execute_spoken_control("音响音量调大")

    assert result == {
        "ok": True,
        "device": "音响音量",
        "action": "volume_up",
        "volume_percent": 70,
    }
    get_request, _ = opener.calls[0]
    post_request, _ = opener.calls[1]
    assert get_request.full_url == "http://homeassistant.local:8123/api/states/media_player.ke_ting"
    assert post_request.full_url == "http://homeassistant.local:8123/api/services/media_player/volume_set"
    assert json.loads(post_request.data) == {
        "entity_id": "media_player.ke_ting",
        "volume_level": 0.7,
    }


def test_spoken_control_sonos_volume_down_steps_by_10(tmp_path):
    opener = SequenceOpener([
        {"entity_id": "media_player.ke_ting", "state": "idle", "attributes": {"volume_level": 0.2}},
        {},
    ])
    executor = ToolExecutor(make_settings_with_hermes(tmp_path), opener=opener)

    result = executor.execute_spoken_control("音响音量调小")

    assert result == {
        "ok": True,
        "device": "音响音量",
        "action": "volume_down",
        "volume_percent": 10,
    }
    post_request, _ = opener.calls[1]
    assert json.loads(post_request.data) == {
        "entity_id": "media_player.ke_ting",
        "volume_level": 0.1,
    }


def test_spoken_control_sonos_short_volume_up_steps_by_10(tmp_path):
    opener = SequenceOpener([
        {"entity_id": "media_player.ke_ting", "state": "idle", "attributes": {"volume_level": 0.2}},
        {},
    ])
    executor = ToolExecutor(make_settings_with_hermes(tmp_path), opener=opener)

    result = executor.execute_spoken_control("音量大")

    assert result == {
        "ok": True,
        "device": "音响音量",
        "action": "volume_up",
        "volume_percent": 30,
    }
    post_request, _ = opener.calls[1]
    assert json.loads(post_request.data) == {
        "entity_id": "media_player.ke_ting",
        "volume_level": 0.3,
    }


def test_spoken_control_sonos_short_volume_down_steps_by_10(tmp_path):
    opener = SequenceOpener([
        {"entity_id": "media_player.ke_ting", "state": "idle", "attributes": {"volume_level": 0.2}},
        {},
    ])
    executor = ToolExecutor(make_settings_with_hermes(tmp_path), opener=opener)

    result = executor.execute_spoken_control("音量小")

    assert result == {
        "ok": True,
        "device": "音响音量",
        "action": "volume_down",
        "volume_percent": 10,
    }
    post_request, _ = opener.calls[1]
    assert json.loads(post_request.data) == {
        "entity_id": "media_player.ke_ting",
        "volume_level": 0.1,
    }


def test_spoken_control_routes_sonos_volume_away_from_robot_speaker(tmp_path):
    opener = RecordingOpener({"ok": True, "result": "Sonos volume set"})
    volume = FakeVolumeController(50)
    executor = ToolExecutor(
        make_settings_with_hermes(tmp_path),
        opener=opener,
        volume_controller=volume,
    )

    result = executor.execute_spoken_control("音响音量到二十")

    assert result == {"ok": True, "result": "Sonos volume set"}
    assert volume.writes == []
    assert len(opener.calls) == 1
    body = json.loads(opener.calls[0][0].data.decode())
    assert body == {
        "name": "control_sonos",
        "arguments": {"prompt": "音响音量调到20"},
    }


def test_spoken_control_routes_sonos_numeric_volume_without_volume_word(tmp_path):
    opener = RecordingOpener({"ok": True, "result": "Sonos volume set"})
    volume = FakeVolumeController(50)
    executor = ToolExecutor(
        make_settings_with_hermes(tmp_path),
        opener=opener,
        volume_controller=volume,
    )

    result = executor.execute_spoken_control("音响一十")

    assert result == {"ok": True, "result": "Sonos volume set"}
    assert volume.writes == []
    body = json.loads(opener.calls[0][0].data.decode())
    assert body["arguments"]["prompt"] == "音响音量调到10"


def test_spoken_control_normalizes_terse_sonos_volume_number(tmp_path):
    opener = RecordingOpener({"ok": True, "result": "Sonos volume set"})
    executor = ToolExecutor(make_settings_with_hermes(tmp_path), opener=opener)

    result = executor.execute_spoken_control("音响二十")

    assert result == {"ok": True, "result": "Sonos volume set"}
    body = json.loads(opener.calls[0][0].data.decode())
    assert body["arguments"]["prompt"] == "音响音量调到20"


def test_spoken_control_treats_truncated_sonos_volume_digit_as_tens(tmp_path):
    opener = RecordingOpener({"ok": True, "result": "Sonos volume set"})
    executor = ToolExecutor(make_settings_with_hermes(tmp_path), opener=opener)

    result = executor.execute_spoken_control("音响音量二")

    assert result == {"ok": True, "result": "Sonos volume set"}
    body = json.loads(opener.calls[0][0].data.decode())
    assert body["arguments"]["prompt"] == "音响音量调到20"


def test_spoken_control_ignores_incomplete_sonos_volume_fragment(tmp_path):
    opener = RecordingOpener({"ok": True, "result": "should not call"})
    executor = ToolExecutor(make_settings_with_hermes(tmp_path), opener=opener)

    assert executor.execute_spoken_control("音响音量") is None
    assert opener.calls == []


def test_spoken_control_returns_failure_for_unconnected_tv_volume(tmp_path):
    volume = FakeVolumeController(50)
    executor = ToolExecutor(make_settings(tmp_path), volume_controller=volume)

    result = executor.execute_spoken_control("电视音量到二十")

    assert result == {
        "ok": False,
        "device": "电视音量",
        "error": "电视音量尚未接入本地控制",
    }
    assert volume.writes == []


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


def test_spoken_control_sonos_ignores_complaint_with_chinese_one(tmp_path):
    executor = ToolExecutor(make_settings(tmp_path))

    assert executor.execute_spoken_control("我说音响，然后他一直调") is None


def test_spoken_control_sonos_allows_explicit_numeric_set(tmp_path, monkeypatch):
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True}

    executor = ToolExecutor(make_settings(tmp_path))
    monkeypatch.setattr(executor, "execute", fake_execute)

    assert executor.execute_spoken_control("音响音量调到20") == {"ok": True}
    assert calls == [("control_sonos", {"prompt": "音响音量调到20"})]

def test_spoken_control_routes_spoken_stock_code_to_hermes_price(tmp_path, monkeypatch):
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "result": "乐鑫科技 当前价格 114.64 元"}

    executor = ToolExecutor(make_settings_with_hermes(tmp_path))
    monkeypatch.setattr(executor, "execute", fake_execute)

    result = executor.execute_spoken_control("帮我查询六八八零幺八的价格")

    assert result == {"ok": True, "result": "乐鑫科技 当前价格 114.64 元"}
    assert calls == [("get_stock_price", {"prompt": "688018"})]


def test_spoken_control_routes_arabic_stock_code_to_hermes_price(tmp_path, monkeypatch):
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "result": "乐鑫科技 当前价格 114.64 元"}

    executor = ToolExecutor(make_settings_with_hermes(tmp_path))
    monkeypatch.setattr(executor, "execute", fake_execute)

    result = executor.execute_spoken_control("查询688018价格")

    assert result == {"ok": True, "result": "乐鑫科技 当前价格 114.64 元"}
    assert calls == [("get_stock_price", {"prompt": "688018"})]

def test_spoken_control_recovers_dropped_zero_in_688_stock_code(tmp_path, monkeypatch):
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "result": "乐鑫科技 当前价格 114.64 元"}

    executor = ToolExecutor(make_settings_with_hermes(tmp_path))
    monkeypatch.setattr(executor, "execute", fake_execute)

    result = executor.execute_spoken_control("查询六八八幺八价格")

    assert result == {"ok": True, "result": "乐鑫科技 当前价格 114.64 元"}
    assert calls == [("get_stock_price", {"prompt": "688018"})]





def test_spoken_control_routes_portfolio_query_to_hermes(tmp_path, monkeypatch):
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "result": "portfolio result"}

    executor = ToolExecutor(make_settings_with_hermes(tmp_path))
    monkeypatch.setattr(executor, "execute", fake_execute)

    result = executor.execute_spoken_control("查询我的自选股")

    assert result == {"ok": True, "result": "portfolio result"}
    assert calls == [("get_portfolio_stocks", {})]


def test_spoken_control_routes_add_portfolio_stock_to_hermes(tmp_path, monkeypatch):
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "result": "added"}

    executor = ToolExecutor(make_settings_with_hermes(tmp_path))
    monkeypatch.setattr(executor, "execute", fake_execute)

    result = executor.execute_spoken_control("把乐鑫科技加入自选股")

    assert result == {"ok": True, "result": "added"}
    assert calls == [("add_portfolio_stock", {"name": "乐鑫科技"})]


def test_spoken_control_routes_remove_portfolio_stock_to_hermes(tmp_path, monkeypatch):
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "result": "removed"}

    executor = ToolExecutor(make_settings_with_hermes(tmp_path))
    monkeypatch.setattr(executor, "execute", fake_execute)

    result = executor.execute_spoken_control("从自选股删除芒果超媒")

    assert result == {"ok": True, "result": "removed"}
    assert calls == [("remove_portfolio_stock", {"name": "芒果超媒"})]


def test_spoken_control_routes_portfolio_advice_to_hermes(tmp_path, monkeypatch):
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "result": "portfolio advice"}

    executor = ToolExecutor(make_settings_with_hermes(tmp_path))
    monkeypatch.setattr(executor, "execute", fake_execute)

    result = executor.execute_spoken_control("查询自选股建议")

    assert result == {"ok": True, "result": "portfolio advice"}
    assert calls == [("get_stock_advice", {"prompt": "自选股"})]


def test_spoken_control_does_not_route_single_stock_advice_to_portfolio_tool(tmp_path, monkeypatch):
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "result": "advice"}

    executor = ToolExecutor(make_settings_with_hermes(tmp_path))
    monkeypatch.setattr(executor, "execute", fake_execute)

    assert executor.execute_spoken_control("六八八零一八能不能买") is None
    assert calls == []


def test_spoken_control_routes_stock_detail_to_hermes(tmp_path, monkeypatch):
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "result": "detail"}

    executor = ToolExecutor(make_settings_with_hermes(tmp_path))
    monkeypatch.setattr(executor, "execute", fake_execute)

    result = executor.execute_spoken_control("查询六八八零一八基本面")

    assert result == {"ok": True, "result": "detail"}
    assert calls == [("get_stock_detail", {"prompt": "688018"})]
