"""Xiaozhi emotion wiring: recorded whitelist preference and idle show."""

import yrobot.main as main_mod
from yrobot import motion
from yrobot.main import _handle_xiaozhi_emotion, _play_idle_show


class _FakeChoreo:
    def __init__(self):
        self.recorded = []
        self.moves = []

    def play_recorded(self, name, rec):
        self.recorded.append(name)
        return True

    def play_move(self, name):
        self.moves.append(name)
        return True


def test_xiaozhi_emotion_prefers_recorded_when_library_available():
    motion._recent_recorded_choice.clear()
    choreo = _FakeChoreo()
    rec = object()

    _handle_xiaozhi_emotion(choreo, "happy", {}, lambda: rec, prefer_recorded=True)

    assert choreo.recorded == ["laughing2"]
    assert choreo.moves == []


def test_xiaozhi_emotion_falls_back_to_programmatic_without_library():
    motion._recent_recorded_choice.clear()
    choreo = _FakeChoreo()

    _handle_xiaozhi_emotion(choreo, "happy", {}, lambda: None, prefer_recorded=True)

    assert choreo.recorded == []
    assert choreo.moves == ["nod"]


def test_xiaozhi_emotion_cooldown_suppresses_repeat():
    motion._recent_recorded_choice.clear()
    choreo = _FakeChoreo()
    last = {}

    _handle_xiaozhi_emotion(choreo, "happy", last, lambda: None, prefer_recorded=False)
    _handle_xiaozhi_emotion(choreo, "happy", last, lambda: None, prefer_recorded=False)

    assert choreo.moves == ["nod"]


def test_play_idle_show_branches():
    motion._recent_recorded_choice.clear()

    def run(random_value, provider=lambda: object()):
        values = iter([random_value])
        original = main_mod.random.random
        main_mod.random.random = lambda: next(values)
        try:
            choreo = _FakeChoreo()
            choreo.play_dance = lambda name: (choreo.moves.append(f"dance:{name}") or True)
            return _play_idle_show(choreo, provider), choreo
        finally:
            main_mod.random.random = original

    result, _ = run(0.5)
    assert result is None

    result, choreo = run(0.7)
    assert result and result.startswith("recorded:")
    assert choreo.recorded

    result, _ = run(0.7, provider=lambda: None)
    assert result is None

    result, _ = run(0.85)
    assert result == "dance:simple_nod"

    result, choreo = run(0.95)
    assert result == "tilt"
    assert choreo.moves == ["tilt"]
