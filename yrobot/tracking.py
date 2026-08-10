"""Speaker-direction + face tracking shared between Xiaozhi and Qwen backends.

Wraps SoundCompass (audio DoA via XVF3800 mic array) and an OpenCV Haar
face tracker running at ~2 fps. Fuses the two yaw estimates into a single
gaze target written to the Choreographer, which the dashboard exposes
through `motion.tracking` in /api/status.

This module is backend-agnostic: pass any Choreographer and reachy_mini
instance, and the rest of the wiring (camera fetch, audio mic, set target)
runs the same regardless of which conversation backend (XIAOZHI or QWEN)
drives the conversation.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def fuse_speaker_gaze(
    audio_yaw: float,
    visual_yaw: Optional[float],
    *,
    max_visual_audio_delta: float = math.radians(70.0),
    visual_weight: float = 0.7,
) -> tuple[float, str]:
    """Use visual gaze only when it agrees with audio speaker direction.

    Returns (target_yaw, source_label). source_label is one of:
      "audio"        - audio DoA only (no recent face, or face direction disagrees)
      "audio+visual" - fused (audio + visual within tolerance, weighted blend)
    """
    if visual_yaw is None:
        return audio_yaw, "audio"
    delta = abs((visual_yaw - audio_yaw + math.pi) % (2 * math.pi) - math.pi)
    if delta > max_visual_audio_delta:
        return audio_yaw, "audio"
    weight = max(0.0, min(1.0, visual_weight))
    fused = audio_yaw + ((visual_yaw - audio_yaw + math.pi) % (2 * math.pi) - math.pi) * weight
    return fused, "audio+visual"


class SpeakerTracker:
    """Combined audio DoA + visual face tracker, shared by XIAOZHI and QWEN.

    Lifecycle:
        tracker = SpeakerTracker(reachy_mini, choreo, head_tracking_weight=0.7)
        tracker.start()
        ... (drive tracker.set_user_speaking() or tracker.note_speech()) ...
        tracker.stop()
    """

    def __init__(
        self,
        reachy_mini: Any,
        choreo: Any,
        *,
        head_tracking_weight: float = 0.7,
    ) -> None:
        self.reachy_mini = reachy_mini
        self.choreo = choreo
        self.head_tracking_weight = float(head_tracking_weight)
        # Use lists as mutable single-cell containers so closures can update them.
        self._user_speaking: list[bool] = [False]
        self._visual_gaze: list[Optional[tuple[float, float]]] = [None]
        self._last_gaze_log: list[float] = [0.0]
        self._compass: Any = None
        self._face_thread: Optional[threading.Thread] = None
        self._face_stop: Optional[threading.Event] = None
        self._face_cascade: Any = None
        self._vis_frames = 0
        self._last_emotion_move: dict[str, float] = {}

    # ── Public API ──────────────────────────────────────────────────────────
    def set_user_speaking(self, speaking: bool) -> None:
        """Immediately set the user-active flag (used by XIAOZHI VAD)."""
        self._user_speaking[0] = bool(speaking)

    def note_speech(self, window_s: float = 2.0) -> None:
        """Pulse the user-active flag for `window_s` seconds (used by QWEN
        wake-word + VAD that fire once per utterance).

        Sets `_user_speaking[0]=True`, then schedules a timer that flips
        it back to False after `window_s` seconds (clamped to 1-10 s).
        """
        window_s = max(1.0, min(10.0, float(window_s)))
        self._user_speaking[0] = True
        threading.Timer(window_s, lambda: self._user_speaking.__setitem__(0, False)).start()

    def record_emotion(self, emo: str) -> None:
        """Stamp the last-fire-time of an emotion name (for cooldown tracking)."""
        self._last_emotion_move[emo] = time.monotonic()

    def last_emotion_time(self, emo: str) -> float:
        """Return the last time `record_emotion` was called for `emo`.

        Returns -1e9 if never recorded (so a fresh emotion always fires).
        """
        return self._last_emotion_move.get(emo, -1e9)

    # ── Internal: gaze target ───────────────────────────────────────────────
    def _current_head_yaw(self) -> float:
        try:
            import numpy as np
            from yrobot.motion import head_yaw_of
            return head_yaw_of(np.asarray(self.reachy_mini.get_current_head_pose()))
        except Exception:
            return self.choreo.current_yaw()

    def _set_speaker_gaze(self, audio_yaw: float) -> None:
        visual_yaw = None
        if self._visual_gaze[0] is not None:
            vy, vt = self._visual_gaze[0]
            if time.time() - vt < 0.8:
                visual_yaw = vy
        target, source = fuse_speaker_gaze(
            audio_yaw,
            visual_yaw,
            visual_weight=self.head_tracking_weight,
        )
        self.choreo.set_gaze_target(target, source=source)
        self.choreo.set_tracking_debug(
            audio_yaw=audio_yaw,
            visual_yaw=visual_yaw,
            target_yaw=target,
            source=source,
        )
        if time.time() - self._last_gaze_log[0] > 2.0:
            self._last_gaze_log[0] = time.time()
            visual_label = "none" if visual_yaw is None else f"{math.degrees(visual_yaw):.0f}°"
            logger.info(
                "gaze target source=%s audio=%.0f° visual=%s target=%.0f°",
                source,
                math.degrees(audio_yaw),
                visual_label,
                math.degrees(target),
            )

    # ── Internal: face tracker thread ──────────────────────────────────────
    def _face_tracker_loop(self) -> None:
        try:
            import cv2
            import numpy as np
            import json as _json
            import urllib.request as _ur
        except ImportError:
            logger.warning("face tracker deps missing, skipping visual gaze")
            return

        if self._face_cascade is None:
            self._face_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )

        frame_url = "http://127.0.0.1:8042/api/camera/frame"
        state_url = "http://127.0.0.1:8042/api/camera/state"
        last_cam_check = [0.0]

        def _ensure_camera() -> None:
            now = time.time()
            if now - last_cam_check[0] < 30:
                return
            last_cam_check[0] = now
            try:
                req = _ur.Request(
                    state_url,
                    method="PUT",
                    data=_json.dumps({"running": True}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                _ur.urlopen(req, timeout=3)
            except Exception:
                pass

        while not self._face_stop.is_set():
            _ensure_camera()
            try:
                req = _ur.Request(frame_url)
                with _ur.urlopen(req, timeout=3) as resp:
                    jpeg = resp.read()
                arr = np.frombuffer(jpeg, dtype=np.uint8)
                bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if bgr is None:
                    self._face_stop.wait(0.5)
                    continue
                scale = bgr.shape[1] / 320.0
                if scale > 1.0:
                    small = cv2.resize(bgr, (320, int(bgr.shape[0] / scale)))
                else:
                    small = bgr
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                faces = self._face_cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.15,
                    minNeighbors=4,
                    minSize=(24, 24),
                )
                if len(faces) == 0:
                    self._visual_gaze[0] = None
                    self._vis_frames = 0
                    self._face_stop.wait(0.5)
                    continue
                # Require 2 consecutive detections before visual gaze overrides
                # the audio DoA: a single spurious Haar hit at the frame edge
                # would otherwise yank the head around.
                self._vis_frames += 1
                if self._vis_frames < 2:
                    self._face_stop.wait(0.5)
                    continue
                x, y, w, h = max(faces, key=lambda r: r[2] * r[3])
                cx = (x + w / 2) * scale
                try:
                    K = self.reachy_mini.media.camera.K
                    fx = float(K[0, 0])
                    cx_princ = float(K[0, 2])
                    cam_rad = math.atan2(cx - cx_princ, fx)
                except Exception:
                    cam_rad = math.radians(
                        (cx - bgr.shape[1] / 2) * (80.0 / bgr.shape[1])
                    )
                try:
                    head_yaw = self._current_head_yaw()
                except Exception:
                    head_yaw = self.choreo.current_yaw()
                self._visual_gaze[0] = (head_yaw + cam_rad, time.time())
                self._face_stop.wait(0.5)
            except Exception:
                self._face_stop.wait(0.5)

    # ── Public lifecycle ────────────────────────────────────────────────────
    def start(self) -> None:
        """Start SoundCompass + the face tracker thread. Idempotent."""
        from yrobot.motion import SoundCompass

        SoundCompass.WINDOW_S = 2.0
        SoundCompass.MIN_CONFIDENCE = 6.0
        SoundCompass.DEADBAND_RAD = 0.20

        try:
            import cv2
            self._face_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
        except ImportError:
            logger.warning("OpenCV not available, visual gaze disabled")

        self._compass = SoundCompass(
            self.reachy_mini.media,
            current_head_yaw=self._current_head_yaw,
            user_active=lambda: self._user_speaking[0],
            on_target=self._set_speaker_gaze,
        )
        self._compass.start()

        self._face_stop = threading.Event()
        self._face_thread = threading.Thread(
            target=self._face_tracker_loop,
            name="yrobot-face-tracker",
            daemon=True,
        )
        self._face_thread.start()
        logger.info("SpeakerTracker started (audio DoA + visual face)")

    def stop(self) -> None:
        """Stop face tracker + compass. Safe to call multiple times."""
        if self._face_stop is not None:
            self._face_stop.set()
        if self._face_thread is not None:
            self._face_thread.join(timeout=2.0)
            self._face_thread = None
        if self._compass is not None:
            try:
                self._compass.stop()
            except Exception:
                pass
            self._compass = None
        self._face_stop = None
        logger.info("SpeakerTracker stopped")