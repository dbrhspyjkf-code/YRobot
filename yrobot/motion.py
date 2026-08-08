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
EMOTION_TO_MOVE = {
    "happy": "cheerful1", "laughing": "laughing1", "funny": "laughing2",
    "winking": "welcoming1", "confident": "proud2",
    "surprised": "surprised1", "shocked": "amazed1",
    "thinking": "thoughtful1", "confused": "confused1",
    "sleepy": "sleep1", "tired": "tired1",
    "sad": "sad1", "crying": "sad2", "downcast": "downcast1",
    "angry": "reprimand1", "furious": "rage1", "irritated": "irritated1",
    "loving": "loving1", "kissy": "loving1",
}
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
            confidence = 0.1 if device_speech else 1.0
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

    def __init__(self, mini) -> None:
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
        self.BODY_YAW_LIMIT = math.radians(150.0)
        self.BODY_FOLLOW_HEAD_DEG = 10.0   # start turning body beyond 10°
        self.BODY_YAW_SPEED = 1.2          # rad/s, brisk but smooth body turn

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
            elif command == "set_gaze_target":
                target, voice_at = payload
                self._gaze.target = target
                self._last_voice_at = voice_at
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

    def set_gaze_target(self, world_yaw: float, now: float | None = None) -> None:
        target = max(-self.YAW_LIMIT, min(self.YAW_LIMIT, _wrap(world_yaw)))
        voice_at = time.monotonic() if now is None else now
        self._enqueue_command("set_gaze_target", (target, voice_at))

    def current_yaw(self) -> float:
        return self._gaze.pos

    def hold_still(self, until: float) -> None:
        """Queue a smooth freeze request for the motion thread."""
        self._enqueue_command("hold_still", until)

    def release_still(self) -> None:
        self._enqueue_command("release_still")

    def close(self) -> None:
        self._halt.set()

    # -- 50 Hz loop -----------------------------------------------------------

    def run(self) -> None:
        try:
            self._mini.set_automatic_body_yaw(True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("automatic body yaw unavailable: %s", exc)
        dt = 1 / self.RATE_HZ
        t0 = time.monotonic()
        next_tick = t0
        while not self._halt.is_set():
            now = time.monotonic()
            t = now - t0
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
            except Exception as exc:
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
            next_tick += dt
            sleep = next_tick - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_tick = time.monotonic()  # never try to catch up with a jump

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
                    ra = np.asarray(rec_ant, dtype=np.float64)
                    m_ant = float((ra[0] + ra[1]) / 2.0)
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
            sway = 0.05 * idle * math.sin(2 * math.pi * 0.3 * t) + 0.10 * speak * math.sin(
                2 * math.pi * 1.4 * t
            )
            target_arr = np.array([target + sway, target - sway])
            if hasattr(self, '_listen_antennas'):
                target_arr = self._listen_antennas * (1.0 - self._antenna_blend) + target_arr * self._antenna_blend
            goal = target_arr
        self._last_listen = listen
        goal = goal * (1.0 - self._still) + self._antennas * self._still  # freeze in place
        self._antennas += (goal - self._antennas) * min(dt / 0.12, 1.0)
        return pose, [float(self._antennas[0]), float(self._antennas[1])]
