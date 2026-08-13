"""Camera frame capture for QWEN realtime vision.

`LatestCamera` is a small background-thread wrapper around the
Reachy Mini camera that keeps the most recently captured JPEG
bytes available for the uplink loop. The thread runs at a
configurable cadence and is owned by the main conversation
lifecycle (``start()`` after the media pipelines are running,
``close()`` followed by ``join(timeout=...)`` in the conversation's
``finally`` block).

The camera is configured to deliver 1280x720 native frames on the
Reachy Mini Wireless build, which encodes to ~290 KB at the SDK's
default quality — too large for two reasons:

  * the QWEN Qwen-Omni-Flash-Realtime endpoint caps decoded
    image input at 190 KB (base64 string <= ~253 KB),
  * the websockets library used by QwenRealtimeClient rejects
    frames above its default 262144 byte ceiling, and the
    server then mirrors back close code 1009 (message too big),
    which kills the realtime session.

To stay well under both limits the capturer pulls raw BGR frames
via ``media.get_frame()``, resizes them to ``target_width x
target_height`` (default 640x360), and re-encodes with OpenCV at
JPEG quality ``jpeg_quality`` (default 50). The resulting payload
is typically 12-25 KB of JPEG bytes (16-35 KB base64) — comfortably
inside the QWEN budget and ~10x cheaper to ship than the SDK's
default JPEG.

The class is otherwise minimal:

  * one background ``threading.Thread`` (daemon) wakes on a
    wall-clock period and snapshots the freshest frame from
    the camera,
  * ``take_latest()`` returns the most recent frame for the
    audio send loop to interleave with PCM,
  * ``mark_sent()`` records that the latest frame has been
    consumed so the next scene-change check starts from a
    known baseline,
  * frame pull, resize, and re-encode are all wrapped in a
    broad try/except so a transient camera failure cannot
    crash the audio loop.

Scene change detection is left to the caller via the
``active_fn`` callback, which already encapsulates whether the
user is speaking or the robot is playing back. A future iteration
can add byte-level or pixel-level diffing without changing this
API.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import cv2

logger = logging.getLogger(__name__)


class LatestCamera:
    """Background JPEG capturer; keeps only the freshest frame.

    Pulls raw BGR frames from ``media.get_frame()`` and re-encodes
    them as compact JPEGs sized for the QWEN realtime vision
    budget (190 KB decoded image, 262 KB websockets frame).
    """

    def __init__(
        self,
        media: object,
        *,
        active_fn: Callable[[float], bool],
        capture_period_s: float,
        idle_heartbeat_s: float,
        scene_change_threshold: float,
        target_width: int = 640,
        target_height: int = 360,
        jpeg_quality: int = 50,
    ) -> None:
        self._media = media
        self._active_fn = active_fn
        self._capture_period_s = capture_period_s
        self._idle_heartbeat_s = idle_heartbeat_s
        self._scene_change_threshold = scene_change_threshold
        self._target_width = target_width
        self._target_height = target_height
        self._jpeg_quality = jpeg_quality
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
            "vision capture started: active=%.2fs idle=%.2fs size=%dx%d quality=%d",
            self._capture_period_s,
            self._idle_heartbeat_s,
            self._target_width,
            self._target_height,
            self._jpeg_quality,
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
                jpeg_bytes = self._capture_jpeg()
            except Exception as exc:  # noqa: BLE001 — camera is best-effort
                logger.debug("vision frame pull/encode failed: %s", exc)
                jpeg_bytes = None
            if jpeg_bytes is not None:
                self._publish(jpeg_bytes)
            period = (
                self._capture_period_s
                if self._active_fn(time.monotonic())
                else self._idle_heartbeat_s
            )
            elapsed = time.monotonic() - start
            sleep_for = max(0.0, period - elapsed)
            if self._stop_event.wait(sleep_for):
                return

    def _capture_jpeg(self) -> bytes | None:
        """Pull a fresh BGR frame, resize, re-encode as JPEG bytes."""
        frame = self._media.get_frame()  # type: ignore[attr-defined]
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if (w, h) != (self._target_width, self._target_height):
            frame = cv2.resize(
                frame,
                (self._target_width, self._target_height),
                interpolation=cv2.INTER_AREA,
            )
        ok, buf = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(self._jpeg_quality)],
        )
        if not ok:
            return None
        return buf.tobytes()

    def _publish(self, frame: bytes) -> None:
        # Reserved hook: a future scene-change detector can compare
        # ``frame`` against ``self._sent_size`` (or a thumbnail) and
        # surface a flag here. Today we simply swap in the freshest frame.
        with self._lock:
            self._latest = frame
