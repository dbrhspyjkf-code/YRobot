"""Xiaozhi emotion wiring: recorded whitelist preference and idle show."""

from pathlib import Path

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


def test_wake_paths_do_not_queue_a_nod():
    source = Path(main_mod.__file__).read_text(encoding="utf-8")

    # A single nod remains for a pure acknowledgement; waking must transition
    # directly to conversation without adding a movement clip.
    assert source.count('choreo.play_move("nod")') == 1


def test_xiaozhi_emotion_prefers_recorded_when_library_available():
    motion._recent_recorded_choice.clear()
    choreo = _FakeChoreo()
    rec = object()

    _handle_xiaozhi_emotion(
        choreo, "happy", {}, lambda: rec, prefer_recorded=True, source="sentence"
    )

    assert choreo.recorded == ["laughing2"]
    assert choreo.moves == []


def test_xiaozhi_emotion_falls_back_to_programmatic_without_library():
    motion._recent_recorded_choice.clear()
    choreo = _FakeChoreo()

    _handle_xiaozhi_emotion(
        choreo, "happy", {}, lambda: None, prefer_recorded=True, source="sentence"
    )

    assert choreo.recorded == []
    assert choreo.moves == ["nod"]


def test_xiaozhi_emotion_cooldown_suppresses_repeat():
    motion._recent_recorded_choice.clear()
    choreo = _FakeChoreo()
    last = {}

    _handle_xiaozhi_emotion(
        choreo, "happy", last, lambda: None, prefer_recorded=False, source="sentence"
    )
    _handle_xiaozhi_emotion(
        choreo, "happy", last, lambda: None, prefer_recorded=False, source="sentence"
    )

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


def test_llm_default_happy_is_noise_not_gesture():
    """Xiaozhi sends emotion=happy on nearly every reply; it must not
    trigger a move on its own. Content-corroborated happy (sentence
    keywords like 哈哈) still gestures via source='sentence'."""
    motion._recent_recorded_choice.clear()
    choreo = _FakeChoreo()

    for emo in ("happy", "neutral", "none", ""):
        _handle_xiaozhi_emotion(
            choreo, emo, {}, lambda: object(), prefer_recorded=True, source="llm"
        )

    assert choreo.recorded == []
    assert choreo.moves == []


def test_sentence_happy_still_gestures():
    motion._recent_recorded_choice.clear()
    choreo = _FakeChoreo()

    _handle_xiaozhi_emotion(
        choreo, "happy", {}, lambda: object(), prefer_recorded=True, source="sentence"
    )

    assert choreo.recorded == ["laughing2"]


def test_informative_llm_emotion_is_not_an_autonomous_gesture():
    motion._recent_recorded_choice.clear()
    choreo = _FakeChoreo()

    _handle_xiaozhi_emotion(
        choreo, "surprised", {}, lambda: object(), prefer_recorded=True, source="llm"
    )

    assert choreo.recorded == []
    assert choreo.moves == []


def test_global_cooldown_suppresses_back_to_back_moves():
    motion._recent_recorded_choice.clear()
    choreo = _FakeChoreo()
    last = {}

    _handle_xiaozhi_emotion(
        choreo, "surprised", last, lambda: object(), prefer_recorded=True, source="llm"
    )
    _handle_xiaozhi_emotion(
        choreo, "sad", last, lambda: object(), prefer_recorded=True, source="sentence"
    )

    assert len(choreo.recorded) == 1
