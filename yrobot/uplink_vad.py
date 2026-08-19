"""Energy-hangover VAD for the xiaozhi uplink loop.

Field report (2026-08-19): the user's speech was heavily truncated on the
way to the xiaozhi cloud — ASR received fragments and "had no idea what
the user said". Root cause: the old uplink gate ended an utterance on the
FIRST 60 ms frame below rms 1000 once ``min_deadline`` (3 s) had elapsed,
so any inter-word pause, breath, or soft trailing syllable cut the audio
mid-sentence, and a 6 s hard deadline halved longer requests.

This module replaces that single-frame judgement with the standard
energy-VAD hangover pattern:

- speech keeps streaming until silence has been SUSTAINED for ~0.9 s
  (inter-word pauses and breaths no longer cut the turn);
- a 1 s minimum floor keeps a single loud blip from opening and
  immediately closing the gate;
- a 12 s hard cap (was 6 s) bounds runaway noise / non-speech;
- :class:`PrerollBuffer` keeps the pre-speech tail across failed gate
  windows so a quiet onset does not lose its first syllables.

Pure logic: no audio I/O and no clock access unless ``now`` is injected,
so the behaviour is fully unit-testable (``tests/test_uplink_vad.py``).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable
from typing import Any

DECISION_CONTINUE = "continue"
DECISION_END = "end"


class EnergyHangoverVAD:
    """Frame-level speech-activity decision with a hangover tail.

    Call :meth:`begin` when the uplink gate opens (listen start), then
    :meth:`feed` once per uplink frame with that frame's RMS.
    """

    def __init__(
        self,
        *,
        stop_rms: float = 1000.0,
        hangover_s: float = 0.9,
        max_utterance_s: float = 12.0,
        min_utterance_s: float = 1.0,
        frame_ms: float = 60.0,
    ) -> None:
        self._stop_rms = float(stop_rms)
        self._hangover_frames = max(1, round(hangover_s * 1000.0 / frame_ms))
        self._max_utterance_s = float(max_utterance_s)
        self._min_utterance_s = float(min_utterance_s)
        self._frame_ms = float(frame_ms)
        self._silence_run = 0
        self._started_at: float | None = None

    @property
    def hangover_frames(self) -> int:
        return self._hangover_frames

    def begin(self, now: float | None = None) -> None:
        """Start a new utterance window (called at listen start)."""
        self._started_at = time.monotonic() if now is None else float(now)
        self._silence_run = 0

    def feed(self, rms: float, now: float | None = None) -> str:
        """Classify one frame. Returns ``"continue"`` or ``"end"``.

        ``end`` fires when sustained silence reaches the hangover budget
        (after the minimum floor) or the hard max-utterance cap is hit —
        never on a single sub-hangover silent frame.
        """
        if self._started_at is None:
            self.begin(now)
        current = time.monotonic() if now is None else float(now)
        if rms < self._stop_rms:
            self._silence_run += 1
        else:
            self._silence_run = 0
        elapsed = current - self._started_at
        if elapsed >= self._max_utterance_s:
            return DECISION_END
        if elapsed >= self._min_utterance_s and self._silence_run >= self._hangover_frames:
            return DECISION_END
        return DECISION_CONTINUE


class PrerollBuffer:
    """Rolling buffer of pre-speech frames so first syllables survive.

    The uplink gate only opens once a 16-frame window clears the start
    threshold; without a preroll, a quiet onset loses its first syllables
    because the frames containing them belonged to a window that failed
    the gate. Callers :meth:`extend` each failed window's frames and
    :meth:`drain` the (fresh) tail to prepend it once the gate opens.

    Frames older than ``max_age_s`` are dropped on drain so stale audio
    from minutes ago can never leak into a new utterance.
    """

    def __init__(
        self,
        *,
        seconds: float = 0.5,
        frame_ms: float = 60.0,
        max_age_s: float = 2.0,
    ) -> None:
        n = max(1, round(seconds * 1000.0 / frame_ms))
        self._max_age_s = float(max_age_s)
        self._buf: deque[tuple[float, Any]] = deque(maxlen=n)

    def extend(self, frames: Iterable[Any], now: float | None = None) -> None:
        """Append a batch of frames (one failed gate window)."""
        ts = time.monotonic() if now is None else float(now)
        for frame in frames:
            self._buf.append((ts, frame))

    def drain(self, now: float | None = None) -> list[Any]:
        """Return the fresh tail (≤ preroll seconds) and clear the buffer."""
        current = time.monotonic() if now is None else float(now)
        fresh = [frame for ts, frame in self._buf if current - ts <= self._max_age_s]
        self._buf.clear()
        return fresh
