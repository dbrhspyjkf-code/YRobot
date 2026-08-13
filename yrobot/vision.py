"""Camera frame capture for QWEN realtime vision.

`LatestCamera` is a small background-thread wrapper around
``reachy_mini.media.MediaManager.get_frame_jpeg`` that keeps the most
recently captured JPEG bytes available for the uplink loop. The thread
runs at a configurable cadence and is owned by the main conversation
lifecycle (``start()`` after the media pipelines are running, ``close()``
followed by ``join(timeout=...)`` in the conversation's ``finally`` block).

The class is intentionally minimal:

  * one background ``threading.Thread`` (daemon) wakes on a wall-clock
    period and snapshots the freshest JPEG from the camera,
  * ``take_latest()`` returns the most recent frame for the audio
    send loop to interleave with PCM,
  * ``mark_sent()`` records that the latest frame has been consumed
    so the next scene-change check starts from a known baseline,
  * no OpenCV or numpy dependency — the frame is just JPEG bytes.

Scene change detection is left to the caller via the
``active_fn`` callback, which already encapsulates whether the
user is speaking or the robot is playing back. A future iteration
can add byte-level or pixel-level diffing without changing this API.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class LatestCamera:
    """Background JPEG capturer; keeps only the freshest frame."""

    def __init__(
        self,
        media: object,
        *,
        active_fn: Callable[[float], bool],
        capture_period_s: float,
        idle_heartbeat_s: float,
        scene_change_threshold: float,
    ) -> None:
        self._media = media
        self._active_fn = active_fn
        self._capture_period_s = capture_period_s
        self._idle_heartbeat_s = idle_heartbeat_s
        self._scene_change_threshold = scene_change_threshold
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._sent_size: int | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="yrobot-vision", daemon=True)
        self._thread.start()
        logger.info(
            "vision capture started: active=%.2fs idle=%.2fs",
            self._capture_period_s,
            self._idle_heartbeat_s,
        )

    def close(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def take_latest(self) -> bytes | None:
        with self._lock:
            return self._latest

    def mark_sent(self) -> None:
        with self._lock:
            self._sent_size = len(self._latest) if self._latest is not None else None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            start = time.monotonic()
            try:
                frame = self._media.get_frame_jpeg()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — camera is best-effort
                logger.debug("vision frame pull failed: %s", exc)
                frame = None
            if isinstance(frame, (bytes, bytearray)) and frame:
                self._publish(bytes(frame))
            period = (
                self._capture_period_s
                if self._active_fn(time.monotonic())
                else self._idle_heartbeat_s
            )
            elapsed = time.monotonic() - start
            sleep_for = max(0.0, period - elapsed)
            if self._stop_event.wait(sleep_for):
                return

    def _publish(self, frame: bytes) -> None:
        # Reserved hook: a future scene-change detector can compare
        # ``frame`` against ``self._sent_size`` (or a thumbnail) and
        # surface a flag here. Today we simply swap in the freshest frame.
        with self._lock:
            self._latest = frame
