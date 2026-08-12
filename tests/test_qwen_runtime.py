from threading import Event
from types import SimpleNamespace

import pytest

import yrobot.main as main_module
from yrobot.audio_runtime import PcmPlayback, WakeGate
from yrobot.config import Settings
from yrobot.main import (
    RecentTranscriptWindow,
    Yrobot,
    _qwen_assistant_sonos_step_command,
    _qwen_contextual_sonos_step_command,
    _qwen_unmatched_spoken_control_feedback,
    _qwen_spoken_control_result_feedback,
    _qwen_spoken_control_result_text,
    _qwen_should_request_response_after_local_control,
    _qwen_should_reconnect,
    _qwen_should_resume_wake_after_reconnect,
)


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


def test_qwen_wake_resumes_after_reconnect_when_gate_is_active():
    gate = WakeGate()
    gate.observe_transcript("你好小白", now=100.0)

    assert _qwen_should_resume_wake_after_reconnect(gate) is True
    assert gate.expire(now=170.0) is True
    assert _qwen_should_resume_wake_after_reconnect(gate) is False


def test_qwen_idle_timeout_is_reconnectable():
    error = RuntimeError(
        "Your session was closed because no response was generated for 300 seconds."
    )

    assert _qwen_should_reconnect(error) is True
    assert _qwen_should_reconnect(RuntimeError("invalid API key")) is False


def test_qwen_internal_service_error_is_reconnectable():
    error = RuntimeError("Internal service error: null")

    assert _qwen_should_reconnect(error) is True



def test_qwen_none_active_response_error_is_reconnectable():
    error = RuntimeError("Conversation has none active response")

    assert _qwen_should_reconnect(error) is True

def test_qwen_active_response_error_is_reconnectable():
    error = RuntimeError("Conversation already has an active response")

    assert _qwen_should_reconnect(error) is True


def test_qwen_opening_handshake_timeout_is_reconnectable():
    error = TimeoutError("timed out during opening handshake")

    assert _qwen_should_reconnect(error) is True


def test_recent_transcript_window_joins_asr_fragments():
    window = RecentTranscriptWindow(window_s=1.5)

    assert window.candidates("关闭餐", now=100.0) == ["关闭餐"]
    assert window.candidates("厅灯", now=100.8) == ["厅灯", "关闭餐厅灯"]
    assert window.candidates("厨房灯", now=103.0) == ["厨房灯"]


def test_recent_transcript_window_joins_longer_asr_command_fragments():
    window = RecentTranscriptWindow(window_s=3.0)

    assert window.candidates("音响音", now=100.0) == ["音响音"]
    assert window.candidates("量调大一下", now=102.0) == [
        "量调大一下",
        "音响音量调大一下",
    ]



def test_qwen_assistant_sonos_step_never_executes_from_model_reply():
    assert _qwen_assistant_sonos_step_command("音响", "音量调大一点。") is None
    assert _qwen_assistant_sonos_step_command("音响音量", "声音小一点。") is None
    assert _qwen_assistant_sonos_step_command("音响", "我会把音量调小一点。") is None


def test_qwen_assistant_sonos_step_ignores_non_sonos_context():
    assert _qwen_assistant_sonos_step_command("你的音量", "音量调大一点。") is None
    assert _qwen_assistant_sonos_step_command("音响", "有什么可以帮你？") is None


def test_qwen_assistant_sonos_step_does_not_execute_questions():
    assert _qwen_assistant_sonos_step_command("音响", "还想再调大一点吗？") is None
    assert _qwen_assistant_sonos_step_command("音响音量", "需要我帮你调小吗？") is None


def test_qwen_contextual_sonos_step_recovers_standalone_direction():
    assert _qwen_contextual_sonos_step_command("音响音量", "调小") == "音响音量调小"
    assert _qwen_contextual_sonos_step_command("音响", "声音大") == "音响音量调大"
    assert _qwen_contextual_sonos_step_command("音响音量", "搅拌") == "音响音量调大"
    assert _qwen_contextual_sonos_step_command("音响音量", "交大") == "音响音量调大"


def test_qwen_contextual_sonos_step_requires_sonos_context():
    assert _qwen_contextual_sonos_step_command("你的音量", "调小") is None
    assert _qwen_contextual_sonos_step_command("", "声音大") is None




def test_qwen_requests_model_response_when_no_local_or_command_match():
    assert _qwen_should_request_response_after_local_control(False, False) is True
    assert _qwen_should_request_response_after_local_control(True, False) is False
    assert _qwen_should_request_response_after_local_control(False, True) is False


def test_qwen_spoken_control_result_text_detects_exact_speak_result():
    exact = "STOCK 688018 price 114.64 CNY"

    assert _qwen_spoken_control_result_text({"ok": True, "result": exact}) == exact
    assert _qwen_spoken_control_result_text({"ok": False, "result": exact}) is None
    assert _qwen_spoken_control_result_text({"ok": True, "device": "sonos"}) is None


def test_qwen_spoken_control_feedback_preserves_exact_stock_result():
    exact = "STOCK 688018 price 114.64 CNY"

    feedback = _qwen_spoken_control_result_feedback({"ok": True, "result": exact})

    assert exact in feedback
    assert "114.64" in feedback
    assert "688018" in feedback
    assert "None" not in feedback


def test_qwen_spoken_control_feedback_keeps_device_action_result():
    feedback = _qwen_spoken_control_result_feedback(
        {"ok": True, "device": "sonos", "action": "volume up"}
    )

    assert "sonos volume up" in feedback


def test_qwen_spoken_control_feedback_reports_failure_error():
    feedback = _qwen_spoken_control_result_feedback({"ok": False, "error": "boom"})

    assert "boom" in feedback
    assert "执行失败" in feedback


def test_qwen_unmatched_spoken_control_asks_for_sonos_direction():
    feedback = _qwen_unmatched_spoken_control_feedback(["音响音量"])

    assert feedback is not None
    assert "本地没有执行" in feedback
    assert "调大" in feedback


def test_qwen_unmatched_spoken_control_asks_for_volume_target():
    feedback = _qwen_unmatched_spoken_control_feedback(["音量，音量"])

    assert feedback is not None
    assert "音响" in feedback
    assert "电视" in feedback
    assert "你的音量" in feedback


def test_qwen_vad_default_is_not_overly_aggressive():
    from yrobot.audio import get_vad_rms_min

    assert get_vad_rms_min() == 0.065
