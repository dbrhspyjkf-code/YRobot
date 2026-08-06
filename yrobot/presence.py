"""Lightweight presence detector — decides whether a person is in the
camera's field of view.

Uses OpenCV's bundled Haar cascade (haarcascade_frontalface_default.xml).
Runs only when the robot is in the *sleeping* state; the moment a face
is detected, the silence gate resumes the conversation uplink.

Design choices:
- 1 Hz polling, not per-frame: 1-3 ms/frame at 320×240 is fine, but
  constant detection wastes CPU when no one cares.
- 3-frame hysteresis on "no one": a single missed frame does NOT trigger
  the deep-sleep path; three consecutive empties do.
- 1-frame trigger on "someone present": a single detected face returns
  presence=True immediately, so the conversation resumes with no perceptible
  lag.
- Lazy classifier init: importing cv2 + the cascade is deferred until the
  first poll so non-camera deployments (e.g. CI) don't pay the cost.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PresenceState:
    """Current presence read; updated by the detector thread.

    Attributes:
      present:  last best estimate (``True`` if a face was seen recently)
      last_seen_ts: monotonic time of last positive detection (0.0 if never)
      checks_total / positive_total: cumulative counters
    """

    present: bool = False
    last_seen_ts: float = 0.0
    checks_total: int = 0
    positive_total: int = 0
    last_check_ts: float = 0.0
    last_error: str | None = None


class PresenceDetector:
    """Periodic face-presence detector driven by an external frame source.

    Typical wiring:
        detector = PresenceDetector(frame_provider=lambda: latest_camera.frame())
        detector.start()          # launches a 1 Hz polling thread
        ...
        if detector.state.present: ...     # read from anywhere
        detector.stop()
    """

    def __init__(
        self,
        frame_provider,
        *,
        poll_interval_s: float = 1.0,
        min_size: int = 60,
        hysteresis: int = 3,
    ) -> None:
        self._frames = frame_provider
        self._poll_interval = poll_interval_s
        self._min_size = min_size
        self._hysteresis = hysteresis
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = PresenceState()
        # A small sliding window of recent results; presence is True if any of
        # the last `hysteresis` frames contained a face.
        self._recent: deque[bool] = deque(maxlen=hysteresis)
        self._classifier = None  # lazy

    @property
    def state(self) -> PresenceState:
        return self._state

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="presence-detector", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _ensure_classifier(self):
        if self._classifier is not None:
            return
        import cv2  # type: ignore[import-not-found]

        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        classifier = cv2.CascadeClassifier(cascade_path)
        if classifier.empty():
            raise RuntimeError(
                f"OpenCV face cascade not found at {cascade_path}"
            )
        self._classifier = classifier

    def check_once(self) -> bool:
        """Run one detection pass on the current frame; update state.

        Returns the new ``present`` value.

        ``frame_provider`` may return either:
          - ``np.ndarray`` (BGR), or
          - ``bytes`` (JPEG) — common when wired to ``LatestCamera.take_latest``.
        """
        try:
            self._ensure_classifier()
        except Exception as exc:  # noqa: BLE001 — no OpenCV → best-effort skip
            self._state.last_error = f"classifier unavailable: {exc}"
            return self._state.present
        frame = None
        try:
            frame = self._frames()
        except Exception as exc:  # noqa: BLE001 — best-effort polling
            self._state.last_error = f"frame provider: {exc}"
            return self._state.present
        if frame is None:
            # Camera not ready yet; treat as 'no detection'.
            self._recent.append(False)
        else:
            try:
                import cv2  # type: ignore[import-not-found]
                import numpy as np  # type: ignore[import-not-found]
            except Exception:  # noqa: BLE001
                return self._state.present
            if isinstance(frame, (bytes, bytearray, memoryview)):
                arr = np.frombuffer(bytes(frame), dtype=np.uint8)
                bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            else:
                bgr = frame
            if bgr is None:
                self._recent.append(False)
            else:
                # Run on a small grayscale — full-res is wasted.
                small = cv2.resize(bgr, (320, 240), interpolation=cv2.INTER_AREA)
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                faces = self._classifier.detectMultiScale(
                    gray,
                    scaleFactor=1.2,
                    minNeighbors=4,
                    minSize=(self._min_size, self._min_size),
                    flags=cv2.CASCADE_SCALE_IMAGE,
                )
                self._recent.append(len(faces) > 0)
        self._state.checks_total += 1
        if self._recent[-1]:
            self._state.positive_total += 1
            self._state.last_seen_ts = time.monotonic()
        # Presence is True iff at least one of the last ``hysteresis`` checks
        # saw a face. Missing a single frame does not flip us to "no one".
        self._state.present = any(self._recent)
        self._state.last_check_ts = time.monotonic()
        return self._state.present

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 — detector must not crash
                logger.debug("presence check failed: %s", exc)
                self._state.last_error = str(exc)
            self._stop.wait(self._poll_interval)
