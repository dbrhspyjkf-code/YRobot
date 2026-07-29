"""Two-tier acoustic barge-in qualification tests."""

from collections.abc import Iterable

import numpy as np

from yrobot.audio import EchoMatch
from yrobot.barge import BargeConfig, BargeDecision, BargeDetector

FRAME_SAMPLES = 320


def _speech_frame(index: int) -> np.ndarray:
    offset = index * FRAME_SAMPLES
    t = (np.arange(FRAME_SAMPLES) + offset) / 16_000
    return (0.03 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _run(
    detector: BargeDetector,
    matches: Iterable[EchoMatch],
    frames: int,
    *,
    raw_pattern: list[bool] | None = None,
    constant: bool = False,
) -> tuple[BargeDecision | None, int]:
    values = list(matches)
    match_index = 0

    def echo_match(candidate, now):
        nonlocal match_index
        value = values[min(match_index, len(values) - 1)]
        match_index += 1
        return value

    streak = 0
    decision = None
    for index in range(frames):
        raw = True if raw_pattern is None else raw_pattern[index]
        streak = streak + 1 if raw else 0
        frame = np.full(FRAME_SAMPLES, 0.03, np.float32) if constant else _speech_frame(index)
        decision = detector.process(
            frame,
            voiced=streak >= 3,
            raw_streak=streak,
            now=index * 0.02,
            echo_match=echo_match,
        )
        if decision is not None:
            break
    return decision, match_index


def test_clear_near_end_speech_takes_fast_path():
    detector = BargeDetector(BargeConfig())
    decision, _ = _run(
        detector,
        [EchoMatch(similarity=0.2, unexplained_db=-32.0)],
        frames=12,
    )
    assert decision is not None
    assert decision.path == "fast"
    assert 120 <= decision.evidence_ms <= 160


def test_weak_double_talk_uses_safe_confirmation():
    detector = BargeDetector(BargeConfig())
    decision, _ = _run(
        detector,
        [EchoMatch(similarity=0.88, unexplained_db=-40.0)],
        frames=30,
    )
    assert decision is not None
    assert decision.path == "safe"
    assert 490 <= decision.evidence_ms <= 520


def test_playback_echo_never_interrupts():
    detector = BargeDetector(BargeConfig())
    decision, _ = _run(
        detector,
        [EchoMatch(similarity=0.94, unexplained_db=-49.0)],
        frames=35,
    )
    assert decision is None


def test_short_dc_like_head_bump_does_not_take_fast_path():
    detector = BargeDetector(BargeConfig())
    decision, _ = _run(
        detector,
        [EchoMatch(similarity=0.2, unexplained_db=-26.0)],
        frames=9,
        constant=True,
    )
    assert decision is None


def test_short_xvf_suppression_gap_preserves_real_barge():
    detector = BargeDetector(BargeConfig())
    pattern = [True] * 8 + [False] + [True] * 24
    decision, _ = _run(
        detector,
        [EchoMatch(similarity=0.4, unexplained_db=-39.0)],
        frames=len(pattern),
        raw_pattern=pattern,
    )
    assert decision is not None
    assert decision.path == "safe"
