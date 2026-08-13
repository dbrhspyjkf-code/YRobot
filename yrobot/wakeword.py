"""Lightweight wake-word detector using local faster-whisper-medium.

Keeps a rolling 3-second mic buffer; every 2.5 seconds the buffer is
transcribed and checked for the phrase "你好小白" (configurable
via ``YROBOT_WAKE_PHRASE``). Detection is best-effort and runs on
the same CPU as the rest of YRobot, so a miss or slow load does
not crash the conversation loop.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

WAKE_PHRASE = os.environ.get("YROBOT_WAKE_PHRASE", "你好小白")
MODEL_PATH = "/home/pollen/stt-models/faster-whisper-medium"
WINDOW_S = 1.5
DETECT_PERIOD_S = 4.0
FRAME_MS = 20


class WakeWordDetector:
    """Best-effort detection of a local wake phrase via faster-whisper.

    The WhisperModel is loaded in a background thread at init time so the
    mic loop is never blocked. Until the model is ready, every ``feed``
    returns False quickly.
    """

    def __init__(self, model_path: str = MODEL_PATH, wake_phrase: str = WAKE_PHRASE) -> None:
        self._model_path = model_path
        self._wake_phrase = wake_phrase
        self._model: WhisperModel | None = None
        self._model_ready = threading.Event()
        self._model_error: str | None = None
        self._buf: list[np.ndarray] = []
        self._last_detect_at = -1e9
        # Load the heavy model in the background so the mic loop stays live.
        threading.Thread(target=self._load_model, name="yrobot-ww-load", daemon=True).start()

    def _load_model(self) -> None:
        try:
            from faster_whisper import WhisperModel

            t0 = time.monotonic()
            self._model = WhisperModel(self._model_path, device="cpu", compute_type="int8")
            logger.info("wake-word whisper model loaded in %.1f s", time.monotonic() - t0)
            self._model_ready.set()
        except Exception as exc:
            self._model_error = str(exc)
            logger.warning("wake-word whisper load failed: %s", exc)

    @property
    def available(self) -> bool:
        return self._model_ready.is_set()

    def feed(self, frame: np.ndarray) -> bool:
        """Feed one 20 ms float32 frame. Return True if the wake phrase was
        detected in the current detection window.
        """
        self._buf.append(frame)
        max_frames = int(WINDOW_S * 1000 / FRAME_MS)  # 150
        if len(self._buf) > max_frames:
            self._buf = self._buf[-max_frames:]

        now = time.monotonic()
        if now - self._last_detect_at < DETECT_PERIOD_S:
            return False
        if len(self._buf) < 30:  # need at least ~0.6 s
            return False
        logger.info(
            "wake-word: running detection (buf=%d frames, %.1fs since last)",
            len(self._buf),
            now - self._last_detect_at,
        )
        self._last_detect_at = now
        if not self._model_ready.is_set():
            return False  # model still loading in background
        audio = np.concatenate(self._buf)
        self._buf = self._buf[-50:]  # keep a bit of overlap
        return self._search(audio)

    def _search(self, audio_16k: np.ndarray) -> bool:
        try:
            assert self._model is not None
            segments, _info = self._model.transcribe(
                audio_16k,
                language="zh",
                beam_size=1,
                vad_filter=False,
            )
            for seg in segments:
                text = seg.text.replace(" ", "")
                # debug: log the transcription so we can tune sensitivity
                logger.info("wake-word whisper: raw=%r", seg.text[:60])
                if self._wake_phrase in text:
                    logger.info("WAKE PHRASE DETECTED: %r", text)
                    self._buf.clear()
                    return True
        except Exception as exc:
            logger.debug("wake-word inference skipped: %s", exc)
        return False

    def feed_raw(self, chunk: np.ndarray, frame_samples: int = 320) -> bool:
        """Feed a raw audio chunk (e.g. 1 s), splitting into 20 ms frames."""
        n = len(chunk) // frame_samples
        chunk_trimmed = chunk[: n * frame_samples]
        frames = np.array_split(chunk_trimmed, max(1, n))
        for frame in frames:
            if self.feed(frame):
                return True
        return False

    def clear(self) -> None:
        self._buf.clear()
