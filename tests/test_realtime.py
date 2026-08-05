"""Unit tests for protocol helpers (no network)."""

import base64
import json
import threading
import wave

import numpy as np
import pytest

from yrobot.config import Settings, normalize_url
from yrobot.realtime import (
    RealtimeClient,
    ThinkFilter,
    _parse_delta,
    build_session_payload,
    encode_reference_wav,
)


def test_normalize_url_variants():
    video = "wss://minicpmo45.modelbest.cn/v1/realtime?mode=video"
    audio = "wss://minicpmo45.modelbest.cn/v1/realtime?mode=audio"
    assert normalize_url("minicpmo45.modelbest.cn") == video
    assert (
        normalize_url("wss://minicpmo45.modelbest.cn/v1/realtime?mode=video")
        == "wss://minicpmo45.modelbest.cn/v1/realtime?mode=video"
    )
    assert normalize_url("wss://minicpmo45.modelbest.cn/v1/realtime?mode=video", "audio") == audio
    assert (
        normalize_url("wss://10.0.16.184:8006") == "wss://10.0.16.184:8006/v1/realtime?mode=video"
    )


def test_parse_audio_delta_roundtrip():
    pcm = np.array([0.0, 0.5, -0.5], dtype="<f4")
    event = {
        "type": "response.output.delta",
        "kind": "audio",
        "audio": base64.b64encode(pcm.tobytes()).decode(),
        "response_id": "resp-1",
        "input_id": "input-1",
        "metrics": {"kv_cache_length": 321},
    }
    delta = _parse_delta(event)
    assert delta.kind == "audio"
    assert np.allclose(delta.audio, pcm)
    assert delta.response_id == "resp-1"
    assert delta.input_id == "input-1"
    assert delta.metrics["kv_cache_length"] == 321


def test_parse_listen_and_text():
    listen = _parse_delta({"kind": "listen"})
    assert (listen.kind, listen.text, len(listen.audio)) == ("listen", "", 0)
    assert _parse_delta({"kind": "text", "text": "hi"}).text == "hi"


def test_send_chunk_carries_input_id_and_exact_model_unit():
    class FakeSocket:
        def __init__(self):
            self.messages = []

        def send(self, raw):
            self.messages.append(json.loads(raw))

    client = RealtimeClient(Settings(), lambda delta: None, lambda reason: None)
    client._ws = FakeSocket()
    client._send_lock = threading.Lock()
    client.send_chunk(
        np.zeros(16_000, np.float32),
        jpeg=None,
        force_listen=True,
        input_id="input-42",
    )
    sent = client._ws.messages[0]
    assert sent["input"]["input_id"] == "input-42"
    assert sent["input"]["force_listen"] is True


def test_send_chunk_rejects_partial_inference_unit():
    client = RealtimeClient(Settings(), lambda delta: None, lambda reason: None)
    with pytest.raises(ValueError, match="16000 samples"):
        client.send_chunk(
            np.zeros(8_000, np.float32),
            jpeg=None,
            force_listen=True,
            input_id="partial",
        )


def test_think_filter_strips_spans_across_deltas():
    f = ThinkFilter()
    assert f.feed("hello <thi") == "hello "
    assert f.feed("nk>secret plan</th") == ""
    assert f.feed("ink> world") == " world"


def test_think_filter_passes_plain_text():
    f = ThinkFilter()
    assert f.feed("你好，") + f.feed("今天天气不错。") == "你好，今天天气不错。"


def test_reference_wav_is_converted_to_float32_16k_mono(tmp_path):
    path = tmp_path / "voice.wav"
    stereo = np.column_stack([np.full(8_000, 8192, np.int16), np.full(8_000, -4096, np.int16)])
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(stereo.astype("<i2").tobytes())

    decoded = np.frombuffer(base64.b64decode(encode_reference_wav(str(path))), dtype="<f4")
    assert len(decoded) == 16_000
    assert np.allclose(decoded.mean(), 0.0625, atol=1e-3)


def test_session_payload_includes_proactive_prompt_and_reference_voice(tmp_path):
    path = tmp_path / "voice.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(np.zeros(1_600, dtype="<i2").tobytes())

    payload = build_session_payload(Settings(ref_audio_path=str(path)))

    assert payload["system_prompt"].startswith("You are a helpful assistant.")
    assert "持续观察和倾听" in payload["system_prompt"]
    assert payload["config"] == {"length_penalty": 1.1}
    assert payload["voice"]["ref_audio_base64"]


def test_session_payload_can_disable_proactive_policy():
    payload = build_session_payload(Settings(proactive_enabled=False))
    assert "持续观察和倾听" not in payload["system_prompt"]
    assert "voice" not in payload
