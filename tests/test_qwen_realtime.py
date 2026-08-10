import asyncio
import base64
import json
import threading

import pytest

from yrobot.config import HA_CONTROL_POLICY, Settings
from yrobot.qwen_realtime import QwenRealtimeClient


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


class FakeTools:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True, "weather": "sunny"}

    def schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ]

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def make_client(**callbacks):
    tools = callbacks.pop("tools", FakeTools())
    settings = callbacks.pop("settings", Settings.from_env({"DASHSCOPE_API_KEY": "test-key"}))
    client = QwenRealtimeClient(
        settings,
        tools,
        **callbacks,
    )
    client.websocket = FakeWebSocket()
    return client, tools


def test_session_update_uses_fixed_model_and_tools():
    client, tools = make_client()

    asyncio.run(client.handle_event({"type": "session.created"}))

    assert client.url.endswith("?model=qwen3.5-omni-flash-realtime")
    assert client.websocket.sent == [
        {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": "Ethan",
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
                "instructions": client.instructions,
                "turn_detection": None,
                "tools": tools.schemas(),
            },
        }
    ]


def test_session_update_includes_enabled_home_assistant_policy():
    settings = Settings.from_env(
        {"DASHSCOPE_API_KEY": "test-key", "YROBOT_HA_ENABLED": "true"}
    )
    client, _ = make_client(settings=settings)

    assert HA_CONTROL_POLICY in client.session_update()["session"]["instructions"]


def test_session_updated_marks_client_ready():
    client, _ = make_client()

    asyncio.run(client.handle_event({"type": "session.updated"}))

    assert client.ready.is_set()


def test_turn_detection_can_switch_to_semantic_vad_after_wake():
    client, _ = make_client()

    asyncio.run(client.set_turn_detection("semantic_vad"))

    assert client.websocket.sent == [
        {
            "type": "session.update",
            "session": {"turn_detection": {"type": "semantic_vad"}},
        }
    ]


def test_audio_delta_is_decoded_and_forwarded():
    audio = []
    client, _ = make_client(on_audio=audio.append)

    asyncio.run(
        client.handle_event(
            {
                "type": "response.audio.delta",
                "response_id": "resp_1",
                "delta": base64.b64encode(b"\x01\x02").decode(),
            }
        )
    )

    assert audio == [b"\x01\x02"]


def test_transcript_callbacks_receive_completed_text():
    inputs = []
    outputs = []
    client, _ = make_client(on_input_transcript=inputs.append, on_output_transcript=outputs.append)

    asyncio.run(
        client.handle_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "你好",
            }
        )
    )
    asyncio.run(
        client.handle_event(
            {"type": "response.audio_transcript.done", "transcript": "你好，有什么可以帮你？"}
        )
    )

    assert inputs == ["你好"]
    assert outputs == ["你好，有什么可以帮你？"]


def test_speech_started_cancels_response_and_flushes_playback():
    audio = []
    flushes = []
    client, _ = make_client(on_audio=audio.append, on_interrupt=lambda: flushes.append(True))
    asyncio.run(
        client.handle_event({"type": "response.created", "response": {"id": "resp_1"}})
    )

    asyncio.run(client.handle_event({"type": "input_audio_buffer.speech_started"}))
    asyncio.run(
        client.handle_event(
            {
                "type": "response.audio.delta",
                "response_id": "resp_1",
                "delta": base64.b64encode(b"stale").decode(),
            }
        )
    )

    assert client.websocket.sent == [{"type": "response.cancel"}]
    assert flushes == [True]
    assert audio == []


def test_speech_started_flushes_buffered_audio_after_response_done():
    flushes = []
    client, _ = make_client(on_interrupt=lambda: flushes.append(True))

    asyncio.run(
        client.handle_event({"type": "response.created", "response": {"id": "resp_1"}})
    )
    asyncio.run(client.handle_event({"type": "response.done", "response": {"id": "resp_1"}}))
    asyncio.run(client.handle_event({"type": "input_audio_buffer.speech_started"}))

    assert client.websocket.sent == []
    assert flushes == [True]


def test_function_call_done_executes_and_writes_result():
    client, tools = make_client()

    asyncio.run(
        client.handle_event(
            {
                "type": "response.function_call_arguments.done",
                "name": "get_weather",
                "call_id": "call_1",
                "arguments": '{"city":"广州"}',
            }
        )
    )

    assert tools.calls == [("get_weather", {"city": "广州"})]
    assert client.websocket.sent[0]["type"] == "conversation.item.create"
    assert client.websocket.sent[0]["item"]["type"] == "function_call_output"
    assert client.websocket.sent[0]["item"]["call_id"] == "call_1"
    assert json.loads(client.websocket.sent[0]["item"]["output"]) == {
        "ok": True,
        "weather": "sunny",
    }
    assert client.websocket.sent[1] == {"type": "response.create"}


def test_response_done_function_call_executes_and_writes_result():
    client, tools = make_client()

    asyncio.run(
        client.handle_event(
            {
                "type": "response.done",
                "response": {
                    "id": "resp_1",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "control_allowed_device",
                            "call_id": "call_lamp",
                            "arguments": '{"device":"书台灯","action":"turn_off"}',
                        }
                    ],
                },
            }
        )
    )

    assert tools.calls == [("control_allowed_device", {"device": "书台灯", "action": "turn_off"})]
    assert client.websocket.sent[0]["type"] == "conversation.item.create"
    assert client.websocket.sent[0]["item"]["type"] == "function_call_output"
    assert client.websocket.sent[0]["item"]["call_id"] == "call_lamp"
    assert client.websocket.sent[1] == {"type": "response.create"}


def test_malformed_function_arguments_return_tool_error():
    client, tools = make_client()

    asyncio.run(
        client.handle_event(
            {
                "type": "response.function_call_arguments.done",
                "name": "get_weather",
                "call_id": "call_bad",
                "arguments": "{bad",
            }
        )
    )

    assert tools.calls == []
    output = json.loads(client.websocket.sent[0]["item"]["output"])
    assert output == {"ok": False, "error": "invalid function arguments"}
    assert client.websocket.sent[1] == {"type": "response.create"}


def test_oversized_tool_result_is_replaced_with_bounded_error():
    client, _ = make_client(tools=FakeTools({"ok": True, "data": "x" * 5000}))

    asyncio.run(
        client.handle_event(
            {
                "type": "response.function_call_arguments.done",
                "name": "get_weather",
                "call_id": "call_large",
                "arguments": '{"city":"广州"}',
            }
        )
    )

    output = client.websocket.sent[0]["item"]["output"]
    assert len(output.encode()) <= 4096
    assert json.loads(output) == {"ok": False, "error": "tool result too large"}


def test_duplicate_function_call_id_executes_only_once():
    client, tools = make_client()
    event = {
        "type": "response.function_call_arguments.done",
        "name": "get_weather",
        "call_id": "call_once",
        "arguments": '{"city":"广州"}',
    }

    asyncio.run(client.handle_event(event))
    asyncio.run(client.handle_event(event))

    assert tools.calls == [("get_weather", {"city": "广州"})]
    assert len(client.websocket.sent) == 2


class DisconnectingConnection(FakeWebSocket):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def recv(self):
        raise ConnectionError("network lost")


def test_disconnect_reports_error_without_provider_fallback():
    states = []
    errors = []
    connect_call = {}
    connection = DisconnectingConnection()
    client, _ = make_client(on_state=states.append, on_error=errors.append)

    def connect(url, **kwargs):
        connect_call.update(url=url, **kwargs)
        return connection

    client.websocket_factory = connect

    with pytest.raises(ConnectionError, match="network lost"):
        asyncio.run(client.run(threading.Event()))

    assert states == ["connecting", "connected", "error"]
    assert errors == ["network lost"]
    assert connect_call == {
        "url": client.url,
        "additional_headers": {"Authorization": "Bearer test-key"},
    }


def test_audio_input_commands_use_pcm_base64_and_manual_turn_events():
    client, _ = make_client()

    asyncio.run(client.append_pcm(b"pcm"))
    asyncio.run(client.commit_turn())
    asyncio.run(client.request_response())

    assert client.websocket.sent == [
        {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(b"pcm").decode(),
        },
        {"type": "input_audio_buffer.commit"},
        {"type": "response.create"},
    ]
