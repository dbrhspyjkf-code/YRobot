from threading import Event
from types import SimpleNamespace

import pytest

import yrobot.main as main_module
from yrobot.audio_runtime import PcmPlayback, WakeGate
from yrobot.config import Settings
from yrobot.main import Yrobot, _qwen_should_reconnect


def make_robot(monkeypatch):
    robot = Yrobot.__new__(Yrobot)
    robot._media_holder = SimpleNamespace(media=None)
    monkeypatch.setattr(main_module, "_clear_startup_failure_counter", lambda: None)
    monkeypatch.setattr(
        main_module,
        "_enter_safe_mode",
        lambda holder, exc, stop: pytest.fail(f"unexpected safe mode: {exc}"),
    )
    return robot


def test_run_routes_xiaozhi_without_constructing_qwen(monkeypatch):
    robot = make_robot(monkeypatch)
    calls = []
    monkeypatch.setenv("YROBOT_CONVERSATION_BACKEND", "xiaozhi")
    monkeypatch.setattr(robot, "_run_xiaozhi", lambda reachy, stop: calls.append("xiaozhi"))
    monkeypatch.setattr(
        robot,
        "_run_qwen",
        lambda reachy, stop, settings: pytest.fail("QWEN must not be constructed"),
        raising=False,
    )

    robot.run(SimpleNamespace(media=object()), Event())

    assert calls == ["xiaozhi"]


def test_run_routes_qwen_without_connecting_xiaozhi(monkeypatch):
    robot = make_robot(monkeypatch)
    calls = []
    monkeypatch.setenv("YROBOT_CONVERSATION_BACKEND", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(
        robot,
        "_run_xiaozhi",
        lambda reachy, stop: pytest.fail("XIAOZHI must not be connected"),
    )
    monkeypatch.setattr(
        robot,
        "_run_qwen",
        lambda reachy, stop, settings: calls.append(("qwen", settings.qwen_model)),
        raising=False,
    )

    robot.run(SimpleNamespace(media=object()), Event())

    assert calls == [("qwen", "qwen3.5-omni-flash-realtime")]


def test_missing_qwen_key_fails_before_hardware_setup(monkeypatch):
    robot = make_robot(monkeypatch)

    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        robot._run_qwen(object(), Event(), Settings(conversation_backend="qwen"))


def test_qwen_interrupt_flushes_queue_and_stops_playback():
    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    playback = PcmPlayback()
    process = FakeProcess()
    playback._process = process
    playback.put(b"old-1")
    playback.put(b"old-2")

    removed = playback.flush()

    assert removed == 2
    assert playback.pending == 0
    assert process.terminated is True


def test_qwen_playback_targets_conversion_capable_reachy_audio_sink():
    playback = PcmPlayback()

    assert playback.command[:3] == (
        "/usr/bin/aplay",
        "-D",
        "plug:reachymini_audio_sink",
    )


def test_qwen_wake_list_contains_nihao_xiaobai():
    gate = WakeGate()

    assert gate.observe_transcript("你好小白，今天怎么样？", now=100.0) is True
    assert gate.active is True


def test_qwen_wake_expires_after_sixty_seconds():
    gate = WakeGate()
    gate.observe_transcript("你好小白", now=100.0)

    assert gate.expire(now=159.9) is False
    assert gate.expire(now=160.1) is True
    assert gate.active is False


def test_qwen_wake_is_not_reactivated_while_active():
    gate = WakeGate()

    assert gate.observe_transcript("你好小白", now=100.0) is True
    assert gate.observe_transcript("你好小白", now=101.0) is False


def test_qwen_idle_timeout_is_reconnectable():
    error = RuntimeError(
        "Your session was closed because no response was generated for 300 seconds."
    )

    assert _qwen_should_reconnect(error) is True
    assert _qwen_should_reconnect(RuntimeError("invalid API key")) is False
