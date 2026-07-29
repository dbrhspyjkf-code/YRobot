"""Acoustic qualification for low-latency, echo-safe barge-in.

The detector has two confidence tiers.  Clear near-end speech can commit after
a short window; ambiguous double-talk keeps the longer confirmation that
protects against head bumps and motor noise.  Turn causality remains in
``turn.py`` -- this module only decides whether captured sound is the user.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from yrobot.audio import EchoMatch

FRAME_S = 0.02
SAFE_WINDOW_FRAMES = 10  # 200 ms for robust echo comparison
RECHECK_FRAMES = 5  # 100 ms
GAP_FRAMES = 2  # tolerate 40 ms XVF double-talk suppression holes


@dataclass(frozen=True)
class BargeConfig:
    echo_similarity: float = 0.75
    unexplained_db: float = -42.0
    confirm_ms: int = 500
    fast_confirm_ms: int = 140
    fast_echo_similarity: float = 0.45
    fast_unexplained_db: float = -36.0


@dataclass(frozen=True)
class BargeDecision:
    path: str
    onset_at: float
    decided_at: float
    evidence_ms: float
    match: EchoMatch


class BargeDetector:
    """Qualify a continuous raw-VAD run as playback echo or near-end voice."""

    def __init__(self, config: BargeConfig) -> None:
        if not 60 <= config.fast_confirm_ms <= config.confirm_ms:
            raise ValueError("fast barge confirmation must be between 60 ms and safe confirmation")
        self._config = config
        self._frames: deque[np.ndarray] = deque(maxlen=SAFE_WINDOW_FRAMES)
        self._frames_since_match = 0
        self._quiet_frames = 0
        self._safe_evaluated = False
        self._run_onset_at: float | None = None
        self._near_end_started_at: float | None = None
        self._near_end_onset_at: float | None = None

    def reset(self) -> None:
        self._frames.clear()
        self._frames_since_match = 0
        self._quiet_frames = 0
        self._safe_evaluated = False
        self._run_onset_at = None
        self._near_end_started_at = None
        self._near_end_onset_at = None

    def process(
        self,
        frame: np.ndarray,
        *,
        voiced: bool,
        raw_streak: int,
        now: float,
        echo_match: Callable[[np.ndarray, float], EchoMatch],
    ) -> BargeDecision | None:
        if raw_streak <= 0:
            self._quiet_frames += 1
            if not self._frames or self._quiet_frames > GAP_FRAMES:
                self.reset()
            else:
                self._frames.append(frame)
                self._frames_since_match += 1
            return None

        self._quiet_frames = 0
        if self._run_onset_at is None:
            self._run_onset_at = now - max(0, raw_streak - 1) * FRAME_S
        self._frames.append(frame)
        self._frames_since_match += 1

        evidence_ms = (now - self._run_onset_at + FRAME_S) * 1000.0
        fast_frames = math.ceil(self._config.fast_confirm_ms / (FRAME_S * 1000.0))
        ready = voiced and len(self._frames) >= fast_frames
        if not ready:
            return None
        first_match = len(self._frames) == fast_frames
        first_safe_match = len(self._frames) == SAFE_WINDOW_FRAMES and not self._safe_evaluated
        if not first_match and not first_safe_match and self._frames_since_match < RECHECK_FRAMES:
            return None

        self._frames_since_match = 0
        if len(self._frames) == SAFE_WINDOW_FRAMES:
            self._safe_evaluated = True
        candidate = np.concatenate(tuple(self._frames))
        match = echo_match(candidate, now)
        if self._fast_confidence(candidate, match):
            decision = BargeDecision(
                path="fast",
                onset_at=self._run_onset_at,
                decided_at=now,
                evidence_ms=evidence_ms,
                match=match,
            )
            self.reset()
            return decision

        if len(self._frames) < SAFE_WINDOW_FRAMES:
            return None
        echo_explained = (
            match.similarity >= self._config.echo_similarity
            and match.unexplained_db < self._config.unexplained_db
        )
        if echo_explained:
            self._near_end_started_at = None
            self._near_end_onset_at = None
            return None

        if self._near_end_started_at is None:
            self._near_end_started_at = now
            self._near_end_onset_at = now - (len(self._frames) - 1) * FRAME_S
        safe_evidence_ms = (
            SAFE_WINDOW_FRAMES * FRAME_S + max(0.0, now - self._near_end_started_at)
        ) * 1000.0
        if safe_evidence_ms + 1e-6 < self._config.confirm_ms:
            return None

        decision = BargeDecision(
            path="safe",
            onset_at=self._near_end_onset_at or self._run_onset_at,
            decided_at=now,
            evidence_ms=safe_evidence_ms,
            match=match,
        )
        self.reset()
        return decision

    def _fast_confidence(self, candidate: np.ndarray, match: EchoMatch) -> bool:
        if (
            match.similarity > self._config.fast_echo_similarity
            or match.unexplained_db < self._config.fast_unexplained_db
        ):
            return False
        rms = float(np.sqrt(np.mean(np.square(candidate, dtype=np.float64))))
        if rms <= 1e-6:
            return False
        crest = float(np.max(np.abs(candidate))) / rms
        signs = np.signbit(candidate)
        zcr = float(np.mean(signs[1:] != signs[:-1])) if len(candidate) > 1 else 0.0
        # Reject impulsive knocks and DC-like motor pushes. WebRTC VAD remains
        # the primary speech test; these cheap features only unlock fast clear.
        return crest <= 7.0 and 0.005 <= zcr <= 0.35
