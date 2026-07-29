"""Continuous, latest-only vision for the realtime duplex timeline.

Capture never stops while the robot is speaking.  A lightweight scene
signature decides whether an idle frame is worth another vision-cache entry,
while a bounded heartbeat keeps the model visually grounded in static scenes.
Only one encoded frame is retained, so camera work can never back up audio.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

try:  # optional: the Reachy media API can provide JPEG directly
    import cv2
except ImportError:  # pragma: no cover - exercised on the robot image
    cv2 = None

logger = logging.getLogger(__name__)

FRAME_MAX_DIM = 448
FRAME_JPEG_QUALITY = 80


def shrink_jpeg(bgr_frame: np.ndarray | None) -> bytes | None:
    """Encode a frame at the model's native conversational vision scale."""
    if bgr_frame is None or cv2 is None:
        return None
    height, width = bgr_frame.shape[:2]
    scale = FRAME_MAX_DIM / max(height, width)
    if scale < 1.0:
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        bgr_frame = cv2.resize(bgr_frame, size, interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(
        ".jpg",
        bgr_frame,
        [cv2.IMWRITE_JPEG_QUALITY, FRAME_JPEG_QUALITY],
    )
    return encoded.tobytes() if ok else None


class SceneChangeDetector:
    """Compare tiny luminance signatures instead of full camera frames."""

    GRID = 16

    def __init__(self, threshold: float = 0.04) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("scene threshold must be between 0 and 1")
        self._threshold = threshold
        self._previous: np.ndarray | None = None

    def changed(self, bgr_frame: np.ndarray) -> bool:
        height, width = bgr_frame.shape[:2]
        ys = np.linspace(0, height - 1, self.GRID, dtype=np.intp)
        xs = np.linspace(0, width - 1, self.GRID, dtype=np.intp)
        sample = bgr_frame[np.ix_(ys, xs)].astype(np.float32)
        # BGR luminance weights; absolute lighting changes count as scene
        # changes because they are often meaningful to a situated robot.
        signature = 0.114 * sample[..., 0] + 0.587 * sample[..., 1] + 0.299 * sample[..., 2]
        previous, self._previous = self._previous, signature
        if previous is None:
            return True
        return float(np.mean(np.abs(signature - previous))) / 255.0 >= self._threshold


@dataclass(frozen=True)
class VisionStats:
    captured: int = 0
    changed: int = 0
    published: int = 0
    selected: int = 0
    sent: int = 0
    failures: int = 0


class LatestCamera(threading.Thread):
    """Capture continuously and publish only the newest useful JPEG.

    ``active`` identifies conversation periods where every captured frame is
    useful.  At idle, changed frames publish immediately and an unchanged
    scene publishes at ``idle_heartbeat_s`` so visual context never goes stale.
    """

    def __init__(
        self,
        media,
        active: Callable[[float], bool],
        *,
        capture_period_s: float = 1.0,
        idle_heartbeat_s: float = 3.0,
        scene_threshold: float = 0.04,
    ) -> None:
        super().__init__(name="yrobot-camera", daemon=True)
        if capture_period_s <= 0 or idle_heartbeat_s <= 0:
            raise ValueError("camera periods must be positive")
        self._media = media
        self._active = active
        self._capture_period_s = capture_period_s
        self._idle_heartbeat_s = idle_heartbeat_s
        self._scene = SceneChangeDetector(scene_threshold)
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._latest: tuple[int, bytes] | None = None
        self._sequence = 0
        self._taken_sequence = 0
        self._last_published_at = -1e9
        self._last_direct_jpeg: bytes | None = None
        self._stats = VisionStats()

    def close(self) -> None:
        self._halt.set()

    def take_latest(self) -> bytes | None:
        """Return each published frame at most once."""
        with self._lock:
            if self._latest is None or self._latest[0] == self._taken_sequence:
                return None
            self._taken_sequence, jpeg = self._latest
            self._stats = self._with_stat(selected=1)
            return jpeg

    def mark_sent(self) -> None:
        with self._lock:
            self._stats = self._with_stat(sent=1)

    def stats(self) -> VisionStats:
        with self._lock:
            return self._stats

    def run(self) -> None:
        next_capture = 0.0
        while not self._halt.wait(0.02):
            now = time.monotonic()
            if now < next_capture:
                continue
            next_capture = now + self._capture_period_s
            try:
                jpeg, changed = self._capture()
            except Exception as exc:  # noqa: BLE001 - a camera miss must not kill audio
                logger.debug("camera capture skipped: %s", exc)
                with self._lock:
                    self._stats = self._with_stat(failures=1)
                continue
            if not jpeg:
                continue
            publish = (
                changed
                or self._active(now)
                or now - self._last_published_at >= self._idle_heartbeat_s
            )
            with self._lock:
                self._stats = self._with_stat(captured=1, changed=int(changed))
                if not publish:
                    continue
                self._sequence += 1
                self._latest = (self._sequence, jpeg)
                self._last_published_at = now
                self._stats = self._with_stat(published=1)

    def _capture(self) -> tuple[bytes | None, bool]:
        if cv2 is not None:
            frame = self._media.get_frame()
            if frame is None:
                return None, False
            return shrink_jpeg(frame), self._scene.changed(frame)

        jpeg = self._media.get_frame_jpeg()
        # Without an image decoder, exact equality still removes static test
        # sources; real cameras safely fall back to the continuous 1 fps path.
        changed = jpeg != self._last_direct_jpeg
        self._last_direct_jpeg = jpeg
        return jpeg, changed

    def _with_stat(self, **increments: int) -> VisionStats:
        values = {
            name: getattr(self._stats, name) + increments.get(name, 0)
            for name in VisionStats.__dataclass_fields__
        }
        return VisionStats(**values)
