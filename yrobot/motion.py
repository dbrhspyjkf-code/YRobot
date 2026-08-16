"""Sound-source tracking and the single motion owner.

Design rules that keep motion lifelike:

* exactly one thread commands the robot (``Choreographer``, 50 Hz
  ``set_target``) — every pose is the sum of smooth, phase-shifted
  oscillators plus a critically damped turn toward the speaker, so nothing
  ever steps or fights;
* speech articulation is delegated to the SDK's ``enable_wobbling()``
  (daemon-side, PTS-synced to the actual speaker output) and body rotation
  to ``set_automatic_body_yaw(True)`` — both compose with our target pose;
* the XVF3800 DoA angle (0 = left, π/2 = front/back-ambiguous, π = right,
  head-relative) is sampled only after VAD *and* echo rejection confirm the
  user. The firmware speech flag supplies confidence rather than the gate,
  and the physical daemon pose transforms samples into the world frame.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections.abc import Callable
from queue import Empty, Full, Queue
from typing import Any

import numpy as np
try:
    from scipy.spatial.transform import Rotation as R
except ImportError:  # pragma: no cover - scipy is a hard dep of the SDK
    R = None

logger = logging.getLogger(__name__)

IDLE, LISTEN, SPEAK = "idle", "listen", "speak"

# Idle antenna sway tuned to the official Conversation App breathing move
# (±15° at 0.5 Hz, mirrored between the two antennas) so the robot reads as
# alive while waiting, matching the reference implementation.
IDLE_ANTENNA_SWAY = math.radians(15.0)
IDLE_ANTENNA_SWAY_HZ = 0.5

# One-shot expressive moves (played on top of the current mode, then fade out).
SHAKE, NOD, TILT, SURPRISE, THINK, YAWN, SAD, ANGRY = (
    "shake", "nod", "tilt", "surprise", "think", "yawn", "sad", "angry")
MOVES = (SHAKE, NOD, TILT, SURPRISE, THINK, YAWN, SAD, ANGRY)

# name -> (duration_s, roll_amp, pitch_amp, yaw_amp, antenna_delta)
# Amplitudes are radians; antenna_delta is added to the antenna neutral.
MOVE_SPECS = {
    SHAKE:    (1.0,  0.00,  0.00,  0.14,  0.00),   # fast left-right no
    NOD:      (1.0,  0.00,  0.12,  0.00,  0.00),   # clear yes
    TILT:     (1.2,  0.16,  0.00,  0.00,  0.00),   # curious tilt
    SURPRISE: (1.1,  0.00, -0.10,  0.00,  0.45),   # antenna up + head up
    THINK:    (2.5,  0.02,  0.10,  0.05, -0.20),   # head down, slow sway
    YAWN:     (2.8,  0.02,  0.12,  0.00, -0.55),   # head down then up, antenna droop
    SAD:      (2.2,  0.00,  0.12, -0.03, -0.35),   # droop: head down, antennas down
    ANGRY:    (1.2,  0.00, -0.08,  0.16,  0.30),   # raised head + fast shake, antenna up
}

# Xiaozhi protocol emotion -> Reachy move name (see xiaozhi.tech websocket doc).
# Prefer official recorded-emotion moves for vividness; the programmatic moves
# (shake/nod/...) are fallbacks used when the emotion library is unavailable.
#
# Recorded-move whitelist ported from the official Conversation App
# (play_emotion.py): moves curated by Pollen Robotics as looking good and
# staying reachable on this robot. Only names in this set are mapped below.
_RECORDED_EXCELLENT_MOVES: tuple[str, ...] = (
    "anxiety1", "boredom2", "dance2", "dance3", "downcast1", "dying1",
    "exhausted1", "grateful1", "helpful1", "loving1", "rage1", "reprimand1",
    "resigned1", "sad1", "sad2", "scared1", "sleep1", "surprised1",
    "thoughtful1", "welcoming2",
)
_RECORDED_OK_CLEAR_MOVES: tuple[str, ...] = (
    "amazed1", "attentive1", "attentive2", "boredom1", "confused1",
    "disgusted1", "displeased1", "displeased2", "fear1", "impatient2",
    "irritated1", "irritated2", "laughing1", "laughing2", "lonely1", "no1",
    "no_excited1", "no_sad1", "reprimand2", "shy1", "success1", "success2",
    "surprised2", "thoughtful2", "uncertain1", "understanding2", "yes1",
)
RECORDED_MOVE_WHITELIST: frozenset[str] = frozenset(
    _RECORDED_EXCELLENT_MOVES + _RECORDED_OK_CLEAR_MOVES
)

# Xiaozhi emotion -> whitelisted recorded-move candidates. Multiple
# candidates per emotion are rotated so consecutive replies don't repeat the
# exact same animation (official Conversation App pattern).
EMOTION_TO_MOVES: dict[str, tuple[str, ...]] = {
    "happy": ("laughing2", "laughing1"),
    "laughing": ("laughing1", "laughing2"),
    "funny": ("laughing2", "laughing1"),
    "winking": ("shy1", "welcoming2"),
    "confident": ("success1", "success2"),
    "surprised": ("surprised1", "surprised2"),
    "shocked": ("amazed1", "surprised1"),
    "thinking": ("thoughtful1", "thoughtful2"),
    "confused": ("confused1", "uncertain1"),
    "sleepy": ("sleep1",),
    "tired": ("exhausted1", "sleep1"),
    "sad": ("sad1", "sad2", "downcast1"),
    "crying": ("sad2", "sad1"),
    "downcast": ("downcast1", "sad1"),
    "angry": ("reprimand1", "irritated2", "irritated1"),
    "furious": ("rage1",),
    "irritated": ("irritated1", "irritated2"),
    "loving": ("loving1",),
    "kissy": ("loving1",),
    # Extra vocabulary borrowed from the official intent table.
    "excited": ("dance3", "dance2"),
    "grateful": ("grateful1",),
    "scared": ("scared1", "fear1", "anxiety1"),
    "anxious": ("anxiety1", "fear1"),
    "bored": ("boredom2", "boredom1"),
    "lonely": ("lonely1",),
    "embarrassed": ("shy1",),
    "yes": ("yes1", "understanding2"),
    "no": ("no1",),
}
# Legacy single-name view (first candidate) kept for existing callers.
EMOTION_TO_MOVE = {emotion: moves[0] for emotion, moves in EMOTION_TO_MOVES.items()}
# Fallback: if the recorded move above can't be played, use a programmatic
# move that conveys the same idea.
EMOTION_FALLBACK_MOVE = {
    "happy": NOD, "laughing": NOD, "funny": NOD, "winking": TILT,
    "confident": NOD,
    "surprised": SURPRISE, "shocked": SURPRISE,
    "thinking": THINK, "confused": THINK,
    "sleepy": YAWN, "tired": YAWN,
    "sad": SAD, "crying": SAD, "downcast": SAD,
    "angry": ANGRY, "furious": ANGRY, "irritated": ANGRY,
    "loving": TILT, "kissy": TILT,
}

_recent_recorded_choice: dict[str, str] = {}


def recorded_move_for(emotion: str) -> str | None:
    """Return the next whitelisted recorded move for *emotion*.

    Candidates rotate per call so back-to-back replies with the same emotion
    use different animations (official Conversation App pattern).
    """
    candidates = EMOTION_TO_MOVES.get((emotion or "").strip().lower(), ())
    if not candidates:
        return None
    previous = _recent_recorded_choice.get(emotion)
    ordered = [name for name in candidates if name != previous]
    chosen = ordered[0] if ordered else candidates[0]
    _recent_recorded_choice[emotion] = chosen
    return chosen


def head_yaw_of(pose: np.ndarray) -> float:
    """Extract world yaw from a 4x4 head pose."""
    return math.atan2(pose[1, 0], pose[0, 0])


def rpy_pose(roll: float, pitch: float, yaw: float, z: float) -> np.ndarray:
    """Build a 4x4 head pose from roll/pitch/yaw (rad) and a z offset (m)."""
    cr, sr, cp, sp, cy, sy = (
        math.cos(roll), math.sin(roll), math.cos(pitch),
        math.sin(pitch), math.cos(yaw), math.sin(yaw),
    )  # fmt: skip
    pose = np.eye(4)
    pose[:3, :3] = np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )
    pose[2, 3] = z
    return pose


def _blend_pose(a: np.ndarray, b: np.ndarray, alpha: float) -> np.ndarray:
    """Lerp two 4x4 poses by alpha (0 -> a, 1 -> b).

    Translation is lerped linearly; the rotation is lerped in SO(3) via
    slerp on the quaternions so the blend never skews or flips.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    out = np.eye(4)
    out[:3, 3] = a[:3, 3] * (1.0 - alpha) + b[:3, 3] * alpha
    if R is None:
        out[:3, :3] = a[:3, :3] * (1.0 - alpha) + b[:3, :3] * alpha
    else:
        try:
            from scipy.spatial.transform import Slerp
            qa = R.from_matrix(a[:3, :3])
            qb = R.from_matrix(b[:3, :3])
            out[:3, :3] = Slerp([0.0, 1.0], R.concatenate([qa, qb]))(alpha).as_matrix()
        except Exception:  # pragma: no cover - fall back to matrix lerp
            out[:3, :3] = a[:3, :3] * (1.0 - alpha) + b[:3, :3] * alpha
    return out


def doa_to_yaw_delta(angle: float) -> float:
    """Map an XVF3800 DoA angle to a head-relative yaw turn (assume front)."""
    return math.pi / 2 - angle


def doa_confidence_weight(device_speech: bool) -> float:
    """Weight a DoA sample; hardware-confirmed speech should dominate."""
    return 3.0 if device_speech else 1.0


class SoundCompass(threading.Thread):
    """Polls DoA at 12 Hz and publishes stable world-frame gaze targets."""

    RATE_HZ = 12
    WINDOW_S = 1.0
    MIN_CONFIDENCE = 3.0
    DEADBAND_RAD = 0.12  # ≈7°: don't chase noise around the current gaze

    def __init__(
        self,
        media,
        current_head_yaw: Callable[[], float],
        user_active: Callable[[], bool],
        on_target: Callable[[float], None],
    ) -> None:
        super().__init__(name="yrobot-doa", daemon=True)
        self._media = media
        self._head_yaw = current_head_yaw
        self._user_active = user_active
        self._on_target = on_target
        self._halt = threading.Event()
        self._muted_ticks = 0

    def close(self) -> None:
        self._halt.set()

    def run(self) -> None:
        samples: list[tuple[float, float, float]] = []  # (time, world yaw, confidence)
        failures = 0
        while not self._halt.wait(1 / self.RATE_HZ):
            if not self._user_active():
                samples.clear()
                self._muted_ticks += 1
                if self._muted_ticks % 600 == 0:
                    logger.info("DoA muted (user_active=False for ~50s)")
                continue
            self._muted_ticks = 0
            try:
                # The XVF3800 control interface shares the USB bus with the
                # daemon and throws transient I/O errors under contention —
                # they must never kill this thread, only slow it down.
                reading = self._media.get_DoA()
                failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                if failures in (1, 10):
                    logger.warning("DoA read failed (%s); backing off", exc)
                if self._halt.wait(min(0.2 * failures, 5.0)):
                    return
                continue
            if reading is None:
                continue
            angle, device_speech = reading
            now = time.monotonic()
            # Diagnostic: log DoA activity every ~30 s so we can tell whether
            # the thread is alive and user_active() is passing.
            if not hasattr(self, "_doa_log_tick"):
                self._doa_log_tick = 0
            self._doa_log_tick += 1
            if self._doa_log_tick % 600 == 0:
                logger.info("DoA alive angle=%.0f° yaw_delta=%.0f°",
                    math.degrees(angle), math.degrees(doa_to_yaw_delta(angle)))
            try:
                head_yaw = self._head_yaw()
                yaw = head_yaw + doa_to_yaw_delta(angle)
            except Exception:  # daemon read hiccup: skip this sample
                continue
            # The firmware flag is pre-AEC and cannot establish "user", but
            # once the post-echo application gate is open it is useful as a
            # confidence boost: two device-confirmed samples react faster,
            # while three software-confirmed samples still work during
            # XVF double-talk suppression.
            confidence = doa_confidence_weight(device_speech)
            samples.append((now, yaw, confidence))
            samples = [(t, y, w) for t, y, w in samples if now - t <= self.WINDOW_S]
            total = sum(w for _, _, w in samples)
            if total < self.MIN_CONFIDENCE:
                continue
            target = weighted_circular_mean([(y, w) for _, y, w in samples])
            if abs(_wrap(target - head_yaw)) > self.DEADBAND_RAD:
                self._on_target(target)


def circular_mean(angles: list[float]) -> float:
    return math.atan2(
        sum(math.sin(a) for a in angles) / len(angles),
        sum(math.cos(a) for a in angles) / len(angles),
    )


def weighted_circular_mean(samples: list[tuple[float, float]]) -> float:
    """Circular mean for ``(angle, positive weight)`` samples."""
    total = sum(weight for _, weight in samples)
    if total <= 0:
        raise ValueError("circular weights must sum to a positive value")
    return math.atan2(
        sum(math.sin(angle) * weight for angle, weight in samples) / total,
        sum(math.cos(angle) * weight for angle, weight in samples) / total,
    )


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2 * math.pi) - math.pi


def _smoothstep(alpha: float) -> float:
    alpha = max(0.0, min(1.0, alpha))
    return alpha * alpha * (3.0 - 2.0 * alpha)


class GazeSpring:
    """Critically damped 2nd-order tracker: fast, smooth, never overshoots."""

    def __init__(self, omega: float = 6.0, max_vel: float = 3.0) -> None:
        self._omega = omega
        self._max_vel = max_vel
        self.pos = 0.0
        self.vel = 0.0
        self.target = 0.0

    def step(self, dt: float, freeze: float = 0.0) -> float:
        """Advance by ``dt``. ``freeze`` in [0, 1] brakes smoothly to a stop
        (drive scaled out, velocity exponentially damped) — used while the
        robot holds still, so mid-turn motion never stops with a jerk."""
        acc = self._omega * self._omega * (self.target - self.pos) - 2 * self._omega * self.vel
        self.vel += acc * (1.0 - freeze) * dt
        self.vel = max(-self._max_vel, min(self._max_vel, self.vel)) * (1.0 - 0.6 * freeze)
        self.pos += self.vel * dt
        return self.pos


class Choreographer(threading.Thread):
    """The one and only writer of robot pose, ticking at 50 Hz.

    Layers, all continuous in position and velocity:
      breathing (slow multi-phase sway) + idle saccades (held glances)
      + conversation posture (listening lean / speaking lift)
      + gaze spring toward the current speaker.
    """

    RATE_HZ = 50
    YAW_LIMIT = 2.4  # rad, stay inside the ±160° body envelope
    ANTENNA_NEUTRAL = 0.17

    def __init__(
        self,
        mini,
        startup_head_pose: np.ndarray | None = None,
        startup_antennas: tuple[float, float] | list[float] | np.ndarray | None = None,
        startup_blend_duration: float = 3.0,
    ) -> None:
        super().__init__(name="yrobot-motion", daemon=True)
        self._mini = mini
        self._halt = threading.Event()
        self._command_queue: Queue[tuple[str, Any]] = Queue(maxsize=128)
        self._mode = IDLE
        self._mode_blend = {IDLE: 1.0, LISTEN: 0.0, SPEAK: 0.0}
        self._gaze = GazeSpring()
        self._last_voice_at = -1e9
        self._saccade_yaw = GazeSpring(omega=10.0, max_vel=1.2)
        self._saccade_pitch = GazeSpring(omega=10.0, max_vel=0.8)
        self._next_saccade_at = 0.0
        self._antennas = np.array([self.ANTENNA_NEUTRAL, self.ANTENNA_NEUTRAL])
        self._still_until = 0.0
        self._still = 0.0  # blended stillness scalar, continuous like modes
        # One-shot move state (mutated only by play_move / the 50 Hz loop).
        self._move_name = None
        self._move_start = -1e9
        self._move_freqs = {SHAKE: 5.0, NOD: 4.0, TILT: 1.0, SURPRISE: 1.0,
                            THINK: 0.4, YAWN: 0.5}
        # Recorded move (official emotion library) state.
        self._recorded_move = None
        self._recorded_name = None
        self._recorded_start = -1e9
        self._recorded_duration = 0.0
        # Explicit body yaw: when the gaze target drifts far from the current
        # head yaw, the body slowly turns to carry the head (like a human
        # turning toward a speaker) so the head never has to crank past ~35°.
        self._body_yaw = 0.0
        self._body_yaw_target = 0.0
        self._last_set_target_err = 0.0
        self._set_target_err_interval = 1.0  # rate-limit error logs
        self._set_target_err_suppressed = 0
        self._status_lock = threading.Lock()
        self._gaze_source = "idle"
        self._gaze_target_updated_at = 0.0
        self._tracking_debug: dict[str, Any] = {
            "audio_yaw_rad": None,
            "visual_yaw_rad": None,
            "target_yaw_rad": None,
            "source": "idle",
            "face_detected": False,
            "updated_at": 0.0,
        }
        self._loop_hz = 0.0
        self._last_tick_ms = 0.0
        self._last_loop_at = 0.0
        self._deadline_misses = 0
        self._set_target_failures = 0
        self._set_target_consecutive_failures = 0
        self.BODY_YAW_LIMIT = math.radians(150.0)
        self.BODY_FOLLOW_HEAD_DEG = 10.0   # start turning body beyond 10°
        self.BODY_YAW_SPEED = 1.2          # rad/s, brisk but smooth body turn
        self._startup_pose = self._valid_pose_or_none(startup_head_pose)
        self._startup_antennas = self._valid_antennas_or_none(startup_antennas)
        self._startup_blend_duration = max(0.0, float(startup_blend_duration))
        self._startup_started_at: float | None = None
        if self._startup_antennas is not None:
            self._antennas = self._startup_antennas.copy()

    @staticmethod
    def _valid_pose_or_none(pose: np.ndarray | None) -> np.ndarray | None:
        if pose is None:
            return None
        arr = np.asarray(pose, dtype=np.float64)
        if arr.shape != (4, 4):
            logger.warning("Ignoring startup head pose with invalid shape: %s", arr.shape)
            return None
        return arr

    @staticmethod
    def _valid_antennas_or_none(
        antennas: tuple[float, float] | list[float] | np.ndarray | None,
    ) -> np.ndarray | None:
        if antennas is None:
            return None
        arr = np.asarray(antennas, dtype=np.float64)
        if arr.shape != (2,):
            logger.warning("Ignoring startup antennas with invalid shape: %s", arr.shape)
            return None
        return arr

    # -- thread-safe inputs -------------------------------------------------

    def _enqueue_command(self, command: str, payload: Any = None) -> bool:
        try:
            self._command_queue.put_nowait((command, payload))
        except Full:
            logger.warning("Motion command queue full; dropped %s", command)
            return False
        return True

    def set_mode(self, mode: str) -> None:
        if mode not in self._mode_blend:
            logger.warning("Ignoring unknown motion mode: %s", mode)
            return
        self._enqueue_command("set_mode", mode)

    def play_move(self, name: str, now: float | None = None) -> bool:
        """Queue a one-shot expressive move; return whether it was accepted."""
        if name not in MOVE_SPECS:
            return False
        start = time.monotonic() if now is None else now
        return self._enqueue_command("play_move", (name, start))

    def play_recorded(self, name: str, recorded_moves: Any = None) -> bool:
        """Validate and queue a recorded emotion move."""
        if recorded_moves is None:
            return False
        try:
            move = recorded_moves.get(name)
        except Exception:
            return False
        return self._enqueue_command("play_recorded", (name, move, time.monotonic()))

    def play_dance(self, name: str) -> bool:
        """Validate and queue a dance move from the optional library."""
        try:
            from reachy_mini_dances_library.dance_move import DanceMove
            move = DanceMove(name)
        except Exception:
            return False
        return self._enqueue_command("play_recorded", (name, move, time.monotonic()))

    def stop_recorded(self) -> bool:
        """Stop an active recorded emotion or dance at the next motion tick."""
        return self._enqueue_command("stop_recorded")

    def current_move(self) -> str | None:
        return self._move_name

    def current_recorded(self) -> str | None:
        return getattr(self, "_recorded_name", None) if self._recorded_move is not None else None

    def _apply_commands(self) -> None:
        """Apply all pending external requests inside the motion thread."""
        while True:
            try:
                command, payload = self._command_queue.get_nowait()
            except Empty:
                break

            if command == "set_mode":
                self._mode = str(payload)
            elif command == "play_move":
                self._move_name, self._move_start = payload
            elif command == "play_recorded":
                name, move, start = payload
                self._recorded_move = move
                self._recorded_name = name
                self._recorded_start = start
                self._recorded_duration = float(move.duration)
            elif command == "stop_recorded":
                self._recorded_move = None
                self._recorded_name = None
            elif command == "set_gaze_target":
                if len(payload) == 2:
                    target, voice_at = payload
                    source = "audio"
                else:
                    target, voice_at, source = payload
                self._gaze.target = target
                self._last_voice_at = voice_at
                with self._status_lock:
                    self._gaze_source = str(source)
                    self._gaze_target_updated_at = time.time()
            elif command == "hold_still":
                self._still_until = max(self._still_until, float(payload))
            elif command == "release_still":
                self._still_until = 0.0
            else:
                logger.warning("Unknown motion command: %s", command)

    def _move_offsets(
        self, now: float, dt: float
    ) -> tuple[float, float, float, float] | None:
        """Return (roll, pitch, yaw, antenna_delta) for the active move, or
        None once the move has finished.  A fade envelope keeps the first and
        last ~200 ms smooth so starting/ending a move never steps the pose.
        """
        name = self._move_name
        if name is None:
            return None
        elapsed = now - self._move_start
        dur, roll_amp, pitch_amp, yaw_amp, ant_delta = MOVE_SPECS[name]
        if elapsed >= dur:
            self._move_name = None  # expired
            return None
        fade = min(1.0, elapsed / 0.2, (dur - elapsed) / 0.2)
        freq = self._move_freqs.get(name, 1.0)
        w = 2.0 * math.pi * freq * elapsed
        # First cycle of a sine starts at 0 and returns to 0; the fade envelope
        # guarantees the pose is continuous at both boundaries.
        roll = roll_amp * math.sin(w) * fade
        pitch = pitch_amp * math.sin(w) * fade
        yaw = yaw_amp * math.sin(w) * fade
        # Antennas: ease the delta in/out with the same fade (bounded).
        ant = ant_delta * fade
        return roll, pitch, yaw, ant

    def set_gaze_target(
        self,
        world_yaw: float,
        now: float | None = None,
        source: str = "audio",
    ) -> None:
        target = max(-self.YAW_LIMIT, min(self.YAW_LIMIT, _wrap(world_yaw)))
        voice_at = time.monotonic() if now is None else now
        self._enqueue_command("set_gaze_target", (target, voice_at, source))

    def set_tracking_debug(
        self,
        *,
        audio_yaw: float,
        visual_yaw: float | None,
        target_yaw: float,
        source: str,
    ) -> None:
        with self._status_lock:
            self._tracking_debug = {
                "audio_yaw_rad": float(audio_yaw),
                "visual_yaw_rad": None if visual_yaw is None else float(visual_yaw),
                "target_yaw_rad": float(target_yaw),
                "source": str(source),
                "face_detected": visual_yaw is not None,
                "updated_at": time.time(),
            }

    def current_yaw(self) -> float:
        return self._gaze.pos

    def hold_still(self, until: float) -> None:
        """Queue a smooth freeze request for the motion thread."""
        self._enqueue_command("hold_still", until)

    def release_still(self) -> None:
        self._enqueue_command("release_still")

    def close(self) -> None:
        self._halt.set()

    def get_status(self) -> dict[str, Any]:
        """Return a lightweight, thread-safe motion health snapshot."""
        with self._status_lock:
            tracking = dict(self._tracking_debug)
            if tracking.get("updated_at"):
                tracking["age_s"] = round(time.time() - float(tracking["updated_at"]), 1)
            else:
                tracking["age_s"] = None
            return {
                "thread_alive": self.is_alive(),
                "mode": self._mode,
                "current_move": self._move_name,
                "current_recorded": self.current_recorded(),
                "command_queue": self._command_queue.qsize(),
                "loop_hz": round(self._loop_hz, 2),
                "last_tick_ms": round(self._last_tick_ms, 2),
                "last_loop_at": self._last_loop_at or None,
                "deadline_misses": self._deadline_misses,
                "set_target_failures": self._set_target_failures,
                "set_target_consecutive_failures": self._set_target_consecutive_failures,
                "antennas": [float(value) for value in self._antennas],
                "gaze_target_rad": round(float(self._gaze.target), 3),
                "gaze_source": self._gaze_source,
                "gaze_age_s": (
                    round(time.time() - self._gaze_target_updated_at, 1)
                    if self._gaze_target_updated_at
                    else None
                ),
                "tracking": tracking,
            }

    def run(self) -> None:
        try:
            self._mini.set_automatic_body_yaw(True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("automatic body yaw unavailable: %s", exc)
        dt = 1 / self.RATE_HZ
        t0 = time.monotonic()
        next_tick = t0
        previous_tick = t0
        while not self._halt.is_set():
            loop_start = time.monotonic()
            now = loop_start
            t = now - t0
            with self._status_lock:
                if loop_start > previous_tick:
                    self._loop_hz = 1.0 / (loop_start - previous_tick)
                self._last_loop_at = time.time()
                if loop_start > next_tick + dt:
                    self._deadline_misses += 1
            previous_tick = loop_start
            self._apply_commands()
            self._blend_modes(dt)
            pose, antennas = self._compose(t, now, dt)
            # Body yaw follows the gaze target: turn the body (gently) so the
            # head only needs a small relative yaw.  Automatic body yaw on the
            # daemon still keeps us inside mechanical limits; this explicit
            # tracking makes the body proactively face the speaker instead of
            # waiting for the head to hit its 65° limit.
            rel = _wrap(self._gaze.target - self._body_yaw)
            # Always aim the body at the gaze target, but only actually move
            # once the head-relative yaw exceeds the follow threshold.  This
            # gives natural "look first, then turn body" behaviour and keeps
            # the body from chasing tiny gaze jitter.
            if abs(rel) > math.radians(self.BODY_FOLLOW_HEAD_DEG):
                step = self.BODY_YAW_SPEED * dt * (1.0 if rel > 0 else -1.0)
                self._body_yaw += step
            self._body_yaw = max(
                -self.BODY_YAW_LIMIT, min(self.BODY_YAW_LIMIT, self._body_yaw))
            try:
                self._mini.set_target(head=pose, antennas=antennas, body_yaw=self._body_yaw)
                self._record_set_target_success()
            except Exception as exc:
                self._record_set_target_failure()
                now_err = time.monotonic()
                if now_err - self._last_set_target_err >= self._set_target_err_interval:
                    msg = f"set_target failed: {exc}"
                    if self._set_target_err_suppressed:
                        msg += f" (suppressed {self._set_target_err_suppressed} repeats)"
                        self._set_target_err_suppressed = 0
                    logger.warning(msg)
                    self._last_set_target_err = now_err
                else:
                    self._set_target_err_suppressed += 1
            with self._status_lock:
                self._last_tick_ms = (time.monotonic() - loop_start) * 1000.0
            next_tick += dt
            sleep = next_tick - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_tick = time.monotonic()  # never try to catch up with a jump

    def _record_set_target_success(self) -> None:
        with self._status_lock:
            self._set_target_consecutive_failures = 0

    def _record_set_target_failure(self) -> None:
        with self._status_lock:
            self._set_target_failures += 1
            self._set_target_consecutive_failures += 1
        # A recorded move that the daemon keeps rejecting (unreachable pose)
        # would freeze all motion for its whole duration; drop it early so the
        # composed layers take back over.
        if (
            self._recorded_move is not None
            and self._set_target_consecutive_failures >= 3
        ):
            dropped = self._recorded_name
            self._recorded_move = None
            self._recorded_name = None
            logger.warning(
                "recorded move %r dropped after repeated set_target failures",
                dropped,
            )

    def _blend_modes(self, dt: float) -> None:
        """Cross-fade posture weights (~250 ms) so mode flips never step."""
        rate = dt / 0.25
        for mode in self._mode_blend:
            goal = 1.0 if mode == self._mode else 0.0
            blend = self._mode_blend[mode]
            self._mode_blend[mode] = blend + max(-rate, min(rate, goal - blend))

    def _compose(self, t: float, now: float, dt: float) -> tuple[np.ndarray, list[float]]:
        idle, listen, speak = (self._mode_blend[m] for m in (IDLE, LISTEN, SPEAK))

        # Stillness ramps in ~150 ms; scales every oscillator so a freeze
        # is silent (no servo noise) yet never steps.
        still_goal = 1.0 if now < self._still_until else 0.0
        self._still += max(-dt / 0.15, min(dt / 0.15, still_goal - self._still))
        calm = 1.0 - 0.95 * self._still

        # Breathing — quieter while listening (attention), fuller when idle.
        amp = (0.5 + 0.5 * idle) * calm
        z = 0.004 * amp * math.sin(2 * math.pi * 0.16 * t)
        pitch = 0.020 * amp * math.sin(2 * math.pi * 0.16 * t + 0.9)
        roll = 0.012 * amp * math.sin(2 * math.pi * 0.11 * t + 2.1)

        # Idle saccades: brief held glances, walked back when engaged.
        if idle > 0.8 and now >= self._next_saccade_at and self._still < 0.1:
            self._next_saccade_at = now + random.uniform(4.0, 9.0)
            self._saccade_yaw.target = random.uniform(-0.25, 0.25)
            self._saccade_pitch.target = random.uniform(-0.10, 0.12)
        elif idle < 0.5:
            self._saccade_yaw.target = 0.0
            self._saccade_pitch.target = 0.0
        sac_yaw = self._saccade_yaw.step(dt, freeze=self._still) * idle * calm
        sac_pitch = self._saccade_pitch.step(dt, freeze=self._still) * idle * calm

        # Conversation posture. Speaking gets an obvious but bounded nod so the
        # robot reads as actively talking rather than merely holding a pose.
        pitch += 0.06 * listen - 0.03 * speak  # lean in to listen, lift to speak
        roll += 0.05 * listen * calm * math.sin(2 * math.pi * 0.05 * t)  # curious tilt
        speak_nod = speak * calm * (
            0.075 * math.sin(2 * math.pi * 1.15 * t)
            + 0.018 * math.sin(2 * math.pi * 2.3 * t + 0.8)
        )
        pitch += speak_nod
        roll += 0.018 * speak * calm * math.sin(2 * math.pi * 0.58 * t + 0.4)

        # After long silence, drift the gaze home.
        if now - self._last_voice_at > 45.0:
            self._gaze.target *= 0.999

        yaw = self._gaze.step(dt, freeze=self._still) + sac_yaw
        pose = rpy_pose(roll, pitch + sac_pitch, yaw, z)

        # One-shot expressive move layered on top of the composed pose.
        moffs = self._move_offsets(now, dt)
        if moffs is not None:
            m_roll, m_pitch, m_yaw, m_ant = moffs
            roll += m_roll
            pitch += m_pitch
            yaw += m_yaw
            pose = rpy_pose(roll, pitch + sac_pitch, yaw, z)
        else:
            m_ant = 0.0

        # Recorded move (official emotion library) takes over the pose
        # entirely while playing, with a 300 ms blend in/out so neither
        # start nor end ever steps.
        rec_ant_pair = None
        rmv = self._recorded_move
        if rmv is not None:
            rel = now - self._recorded_start
            dur = self._recorded_duration
            if rel >= dur:
                self._recorded_move = None
            else:
                try:
                    rec_pose, rec_ant, _ = rmv.evaluate(rel)
                    blend = min(1.0, rel / 0.3, (dur - rel) / 0.3)
                    # Interpolate pose elements (position + rotation matrix)
                    # between the composed pose and the recorded pose.
                    pose = _blend_pose(pose, np.asarray(rec_pose, dtype=np.float64), blend)
                    # Carry the full recorded antenna pair (antisymmetric
                    # trajectories included) instead of flattening it to a
                    # mean offset.
                    ra = np.asarray(rec_ant, dtype=np.float64)
                    if ra.shape == (2,):
                        rec_ant_pair = (1.0 - blend) * self._antennas + blend * ra
                except Exception:
                    self._recorded_move = None

        # Antennas: freeze when listening (official app pattern), sway otherwise.
        if listen > 0.5:
            if not hasattr(self, '_listen_antennas') or getattr(self, '_last_listen', 0) < 0.5:
                self._listen_antennas = self._antennas.copy()
            goal = self._listen_antennas.copy()
        else:
            if hasattr(self, '_listen_antennas') and getattr(self, '_last_listen', 0) > 0.5:
                self._antenna_blend = 0.0
            self._antenna_blend = min(1.0, getattr(self, '_antenna_blend', 1.0) + dt / 0.4)
            target = self.ANTENNA_NEUTRAL * (1.0 - 0.6 * listen) + m_ant
            sway = (
                IDLE_ANTENNA_SWAY
                * idle
                * math.sin(2 * math.pi * IDLE_ANTENNA_SWAY_HZ * t)
                + 0.10 * speak * math.sin(2 * math.pi * 1.4 * t)
            )
            target_arr = np.array([target + sway, target - sway])
            if rec_ant_pair is not None:
                target_arr = rec_ant_pair
            if hasattr(self, '_listen_antennas'):
                target_arr = self._listen_antennas * (1.0 - self._antenna_blend) + target_arr * self._antenna_blend
            goal = target_arr
        self._last_listen = listen
        goal = goal * (1.0 - self._still) + self._antennas * self._still  # freeze in place
        self._antennas += (goal - self._antennas) * min(dt / 0.12, 1.0)
        pose, antennas = self._apply_startup_blend(pose, self._antennas, now)
        self._antennas = antennas.copy()
        return pose, [float(self._antennas[0]), float(self._antennas[1])]

    def _apply_startup_blend(
        self,
        pose: np.ndarray,
        antennas: np.ndarray,
        now: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        if (
            self._startup_pose is None
            and self._startup_antennas is None
        ) or self._startup_blend_duration <= 0:
            return pose, antennas

        if self._startup_started_at is None:
            self._startup_started_at = now
        alpha = _smoothstep((now - self._startup_started_at) / self._startup_blend_duration)

        if alpha >= 1.0:
            self._startup_pose = None
            self._startup_antennas = None
            return pose, antennas

        blended_pose = pose
        if self._startup_pose is not None:
            blended_pose = _blend_pose(self._startup_pose, pose, alpha)

        blended_antennas = antennas
        if self._startup_antennas is not None:
            blended_antennas = self._startup_antennas * (1.0 - alpha) + antennas * alpha
        return blended_pose, blended_antennas
