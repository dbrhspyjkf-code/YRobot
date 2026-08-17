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


def _wrap_yaw(angle: float) -> float:
    return (angle + math.pi) % (2 * math.pi) - math.pi


def visual_gaze_should_lead(
    face_age_s: float,
    conversation_active: bool,
    *,
    face_max_age_s: float = 0.9,
) -> bool:
    """Whether the visual face lock drives the gaze (official-app positioning).

    The official app positions the head purely on the user's face during a
    conversation. Mirroring that: a fresh face lock IS the gaze target —
    audio DoA never overrides it (DoA can chase the robot's own TTS echo).
    Idle robots never stare at bystanders.
    """
    if not conversation_active:
        return False
    return face_age_s <= face_max_age_s


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
        camera_streamer: Any | None = None,
    ) -> None:
        self.reachy_mini = reachy_mini
        self.choreo = choreo
        self.head_tracking_weight = float(head_tracking_weight)
        self._camera_streamer = camera_streamer
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
        # Face-lead state (official-app positioning parity).
        self._conversation_active: list[bool] = [False]
        self._robot_speaking: list[bool] = [False]
        self._daemon_tracking: list[bool] = [False]
        self._last_doa_at: list[float] = [0.0]  # monotonic; 0 = never
        self._face_lead_target: list[Optional[float]] = [None]

    # ── Public API ──────────────────────────────────────────────────────────
    def set_user_speaking(self, speaking: bool) -> None:
        """Immediately set the user-active flag (used by XIAOZHI VAD)."""
        self._user_speaking[0] = bool(speaking)

    def set_robot_speaking(self, speaking: bool) -> None:
        """Official-app speaking handoff on daemon head tracking.

        While the robot speaks, the daemon tracker must release the head so
        expression moves and speak nods can play. Exactly like the official
        app: pause only once a face is locked (else speech blocks
        acquisition), anchoring the gaze to the current head pose so the
        robot keeps facing the user; resume full tracking afterwards.

        Also an echo guard: the DoA gate is forced closed while TTS plays
        (the pre-AEC firmware flag mistakes TTS echo for user speech).
        """
        self._robot_speaking[0] = bool(speaking)
        if not self._conversation_active[0]:
            return
        try:
            if speaking:
                face = self.reachy_mini.get_tracked_face(wait=False)
                if face is not None and face.detected:
                    # Anchor: sync the gaze spring position to the daemon's
                    # exact head pose so the handoff never snaps.
                    self.choreo.sync_gaze(self._current_head_yaw())
                    self.reachy_mini.start_head_tracking(weight=0.0)
                    logger.info("speaking handoff: anchored + tracking paused")
                else:
                    logger.info("speaking handoff: no face lock, tracking keeps acquiring")
            else:
                if self._daemon_tracking[0]:
                    self.reachy_mini.start_head_tracking(weight=1.0)
        except Exception as exc:  # noqa: BLE001 — tracking is best effort
            logger.warning("speaking handoff failed: %s", exc)

    def _doa_active(self) -> bool:
        """SoundCompass gate: user speaking AND robot not speaking."""
        return self._user_speaking[0] and not self._robot_speaking[0]

    def set_conversation_active(self, active: bool) -> None:
        """Gate face-led gaze on an ongoing conversation.

        While active, daemon-side YuNet head tracking owns the head
        orientation (the official Conversation App mechanism); the local
        DoA/Haar gaze writers stand down. While inactive the robot never
        turns its head toward faces (respects the 'no unprompted motion
        while idle' preference).
        """
        if active == self._conversation_active[0]:
            return
        self._conversation_active[0] = bool(active)
        self._face_lead_target[0] = None
        try:
            if active:
                self.reachy_mini.start_head_tracking(weight=1.0)
                self._daemon_tracking[0] = True
                logger.info("daemon head tracking enabled (weight=1.0)")
            else:
                if self._daemon_tracking[0]:
                    self.reachy_mini.stop_head_tracking()
                self._daemon_tracking[0] = False
                logger.info("daemon head tracking stopped")
        except Exception as exc:  # noqa: BLE001 — tracking is best effort
            self._daemon_tracking[0] = False
            logger.warning("daemon head tracking toggle failed: %s", exc)

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
        self._last_doa_at[0] = time.monotonic()
        # Daemon-side tracking owns the head during a conversation; local
        # writers must not fight it (they would only drag the body yaw).
        if self._conversation_active[0] and self._daemon_tracking[0]:
            return
        visual_yaw = None
        vg = self._visual_gaze[0]
        if vg is not None and time.time() - vg[1] < 0.8:
            visual_yaw = vg[0]
        # Official-style positioning: a fresh face lock IS the target; audio
        # DoA is only a fallback when no face is currently visible.
        if visual_yaw is not None:
            target, source = visual_yaw, "face"
        else:
            target, source = audio_yaw, "audio"
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

    def _publish_face_gaze(self, world_yaw: float) -> None:
        """Face-lead path: drive gaze straight from a locked face.

        The face IS the target during a conversation (official-app parity);
        the audio DoA never overrides a fresh face lock.
        """
        if not visual_gaze_should_lead(
            face_age_s=0.0,
            conversation_active=(
                self._conversation_active[0] and not self._daemon_tracking[0]
            ),
        ):
            return
        last = self._face_lead_target[0]
        if last is not None and abs(_wrap_yaw(world_yaw - last)) < math.radians(4.0):
            return
        self._face_lead_target[0] = world_yaw
        self.choreo.set_gaze_target(world_yaw, source="face")

    # ── Internal: face tracker thread ──────────────────────────────────────
    def _poll_daemon_face(self) -> None:
        """Log daemon face-lock state transitions (field diagnostics)."""
        if self._robot_speaking[0]:
            return  # tracking paused while speaking: detection is off too
        try:
            face = self.reachy_mini.get_tracked_face(wait=False)
        except Exception:
            return
        detected = bool(face is not None and face.detected)
        prev = getattr(self, "_daemon_face_detected", None)
        if detected != prev:
            self._daemon_face_detected = detected
            logger.info("daemon face lock: %s", "acquired" if detected else "lost")

    def _face_tracker_loop(self) -> None:
        try:
            import cv2
            import numpy as np
        except ImportError:
            logger.warning("face tracker deps missing, skipping visual gaze")
            return

        if self._face_cascade is None:
            self._face_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )

        while not self._face_stop.is_set():
            try:
                # Daemon-side tracking owns the head during a conversation;
                # local Haar is pointless then. Poll the daemon lock state at
                # 1 Hz for field visibility instead.
                if self._conversation_active[0] and self._daemon_tracking[0]:
                    self._poll_daemon_face()
                    self._face_stop.wait(1.0)
                    continue
                if self._camera_streamer is None:
                    self._face_stop.wait(0.5)
                    continue
                self._camera_streamer.set_running(True)
                jpeg = self._camera_streamer.latest()
                if jpeg is None:
                    self._face_stop.wait(0.5)
                    continue
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
                # Face anchor (official-app parity): while the conversation is
                # active and DoA is quiet, keep the head aimed at the user.
                self._publish_face_gaze(head_yaw + cam_rad)
                self._face_stop.wait(0.2)
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
            user_active=self._doa_active,
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
        try:
            if self._daemon_tracking[0]:
                self.reachy_mini.stop_head_tracking()
                self._daemon_tracking[0] = False
        except Exception:
            pass
        self._face_stop = None
        logger.info("SpeakerTracker stopped")
