"""Unit tests for DoA mapping and motion primitives (no hardware)."""

import math
import time

import numpy as np
import pytest

from yrobot.motion import (
    Choreographer,
    GazeSpring,
    IDLE,
    LISTEN,
    SoundCompass,
    circular_mean,
    doa_confidence_weight,
    doa_to_yaw_delta,
    head_yaw_of,
    rpy_pose,
    weighted_circular_mean,
)


def test_doa_angle_convention():
    # XVF3800: 0 = left, pi/2 = front, pi = right (head-relative).
    assert math.isclose(doa_to_yaw_delta(0.0), math.pi / 2)  # turn left
    assert math.isclose(doa_to_yaw_delta(math.pi), -math.pi / 2)  # turn right
    assert math.isclose(doa_to_yaw_delta(math.pi / 2), 0.0)  # already facing


def test_circular_mean_handles_wraparound():
    mean = circular_mean([math.pi - 0.1, -math.pi + 0.1])
    assert math.isclose(abs(mean), math.pi, abs_tol=1e-6)


def test_weighted_circular_mean_prioritizes_device_confirmed_samples():
    mean = weighted_circular_mean([(0.0, 2.0), (math.pi / 2, 1.0)])
    assert 0.0 < mean < math.pi / 4


def test_device_confirmed_doa_has_higher_weight():
    assert doa_confidence_weight(True) > doa_confidence_weight(False)


def test_rpy_pose_yaw_roundtrip():
    for yaw in (-1.2, 0.0, 0.7, 2.0):
        assert math.isclose(head_yaw_of(rpy_pose(0.05, -0.1, yaw, 0.002)), yaw, abs_tol=1e-9)


def test_rpy_pose_shape_and_z():
    pose = rpy_pose(0.0, 0.0, 0.0, 0.004)
    assert pose.shape == (4, 4)
    assert np.allclose(pose[:3, :3], np.eye(3))
    assert pose[2, 3] == 0.004


def test_gaze_spring_converges_without_overshoot():
    spring = GazeSpring()
    spring.target = 1.0
    peak = 0.0
    for _ in range(500):  # 10 s at 50 Hz
        peak = max(peak, spring.step(0.02))
    assert peak <= 1.0 + 1e-6
    assert math.isclose(spring.pos, 1.0, abs_tol=0.01)


def test_gaze_spring_velocity_clamp():
    spring = GazeSpring(max_vel=1.0)
    spring.target = 100.0
    spring.step(0.02)
    assert abs(spring.vel) <= 1.0


def test_sound_compass_survives_usb_errors():
    # XVF3800 control reads throw transient USB I/O errors under bus
    # contention; the thread must back off, not die (hardware 2026-07-24).
    class FlakyMedia:
        def get_DoA(self):
            raise OSError(5, "Input/Output Error")

    compass = SoundCompass(
        FlakyMedia(),
        current_head_yaw=lambda: 0.0,
        user_active=lambda: True,
        on_target=lambda yaw: None,
    )
    compass.start()
    time.sleep(0.5)
    try:
        assert compass.is_alive()  # backed off instead of crashing
    finally:
        compass.close()
        compass.join(timeout=2)
    assert not compass.is_alive()


def test_gaze_spring_freeze_brakes_smoothly_and_holds():
    spring = GazeSpring()
    spring.target = 2.0
    for _ in range(10):
        spring.step(0.02)  # mid-turn
    assert abs(spring.vel) > 0.1
    for _ in range(25):
        spring.step(0.02, freeze=1.0)  # 0.5 s of hold-still
    assert abs(spring.vel) < 0.01  # braked, no jerk, no drive
    held = spring.pos
    spring.step(0.02, freeze=1.0)
    assert abs(spring.pos - held) < 1e-3


def test_idle_saccade_target_is_trajectory_limited(monkeypatch):
    class FakeMini:
        pass

    choreo = Choreographer(FakeMini())

    def upper_bound(low, high):
        return high

    monkeypatch.setattr("yrobot.motion.random.uniform", upper_bound)
    pose, _ = choreo._compose(t=0.0, now=1.0, dt=0.02)
    yaw = head_yaw_of(pose)
    # The random target is +0.25 rad, but it must not appear in one 20 ms tick.
    assert 0.0 < yaw < 0.03


def test_motion_inputs_are_applied_by_motion_thread_only():
    class FakeMini:
        pass

    choreo = Choreographer(FakeMini())
    choreo.set_mode(LISTEN)
    choreo.play_move("nod", now=10.0)
    choreo.set_gaze_target(1.0, now=11.0)

    assert choreo._mode == IDLE
    assert choreo._move_name is None
    assert choreo._gaze.target == 0.0

    choreo._apply_commands()

    assert choreo._mode == LISTEN
    assert choreo._move_name == "nod"
    assert choreo._move_start == 10.0
    assert choreo._gaze.target == 1.0
    assert choreo._last_voice_at == 11.0


def test_gaze_target_status_tracks_source():
    class FakeMini:
        pass

    choreo = Choreographer(FakeMini())
    choreo.set_gaze_target(1.0, now=11.0, source="audio+visual")
    choreo._apply_commands()

    status = choreo.get_status()

    assert status["gaze_target_rad"] == 1.0
    assert status["gaze_source"] == "audio+visual"


def test_set_target_consecutive_failures_reset_after_success():
    class FakeMini:
        pass

    choreo = Choreographer(FakeMini())
    choreo._record_set_target_failure()
    choreo._record_set_target_failure()

    assert choreo.get_status()["set_target_failures"] == 2
    assert choreo.get_status()["set_target_consecutive_failures"] == 2

    choreo._record_set_target_success()

    assert choreo.get_status()["set_target_failures"] == 2
    assert choreo.get_status()["set_target_consecutive_failures"] == 0


def test_startup_blend_first_frame_uses_captured_robot_pose():
    class FakeMini:
        pass

    startup_pose = rpy_pose(0.2, -0.1, 0.4, 0.03)
    startup_antennas = (0.7, -0.6)
    choreo = Choreographer(
        FakeMini(),
        startup_head_pose=startup_pose,
        startup_antennas=startup_antennas,
        startup_blend_duration=4.0,
    )

    pose, antennas = choreo._compose(t=0.0, now=10.0, dt=0.02)

    assert np.allclose(pose, startup_pose)
    assert np.allclose(antennas, startup_antennas)


def test_startup_blend_finishes_and_releases_to_normal_motion():
    class FakeMini:
        pass

    startup_pose = rpy_pose(0.2, -0.1, 0.4, 0.03)
    choreo = Choreographer(
        FakeMini(),
        startup_head_pose=startup_pose,
        startup_antennas=(0.7, -0.6),
        startup_blend_duration=1.0,
    )

    choreo._compose(t=0.0, now=10.0, dt=0.02)
    pose, antennas = choreo._compose(t=1.1, now=11.1, dt=0.02)

    assert choreo._startup_pose is None
    assert choreo._startup_antennas is None
    assert not np.allclose(pose, startup_pose)
    assert not np.allclose(antennas, (0.7, -0.6))


# ── Recorded-emotion whitelist and rotation ────────────────────────────────

def test_recorded_whitelist_matches_official_app_curation():
    from yrobot.motion import RECORDED_MOVE_WHITELIST

    assert len(RECORDED_MOVE_WHITELIST) == 47
    for name in ("laughing1", "rage1", "welcoming2", "thoughtful1", "yes1", "sleep1"):
        assert name in RECORDED_MOVE_WHITELIST


def test_emotion_to_moves_all_whitelisted():
    from yrobot.motion import EMOTION_TO_MOVES, RECORDED_MOVE_WHITELIST

    assert EMOTION_TO_MOVES, "emotion table must not be empty"
    for emotion, moves in EMOTION_TO_MOVES.items():
        assert moves, emotion
        assert len(set(moves)) == len(moves), emotion
        for name in moves:
            assert name in RECORDED_MOVE_WHITELIST, (emotion, name)


def test_recorded_move_for_rotates_and_ignores_unknown():
    from yrobot import motion
    from yrobot.motion import EMOTION_TO_MOVES, recorded_move_for

    motion._recent_recorded_choice.clear()

    assert recorded_move_for("nosuchemotion") is None
    assert recorded_move_for("") is None

    first = recorded_move_for("happy")
    second = recorded_move_for("happy")
    assert first in EMOTION_TO_MOVES["happy"]
    assert second in EMOTION_TO_MOVES["happy"]
    assert first != second


def test_recorded_move_carries_full_antenna_pair():
    class _FakeRecorded:
        duration = 1.0

        def evaluate(self, t):
            return rpy_pose(0.0, 0.0, 0.0, 0.0), np.array([-0.6, 0.5]), 0.0

    choreo = Choreographer(mini=object())
    choreo._recorded_move = _FakeRecorded()
    choreo._recorded_name = "fake"
    start = time.monotonic()
    choreo._recorded_start = start
    choreo._recorded_duration = 1.0

    dt = 0.02
    now = start + 0.5  # past the 300 ms blend-in, before the blend-out
    for _ in range(200):
        _, antennas = choreo._compose(now - start, now, dt)

    left, right = antennas
    assert abs(left - (-0.6)) < 0.05, antennas
    assert abs(right - 0.5) < 0.05, antennas
    # A flattened (averaged) pair would collapse both antennas to ~-0.05.
    assert abs(left - right) > 0.9


def test_idle_antenna_sway_matches_official_app_breathing():
    """Idle antennas must sway as visibly as the official Conversation App."""
    from yrobot.motion import IDLE_ANTENNA_SWAY, IDLE_ANTENNA_SWAY_HZ

    assert IDLE_ANTENNA_SWAY >= math.radians(13.0)
    assert IDLE_ANTENNA_SWAY_HZ == pytest.approx(0.5, abs=0.05)


def test_compose_idle_antennas_reach_official_amplitude():
    choreo = Choreographer(mini=object())
    dt = 0.02
    samples = []
    for i in range(300):  # ~6 s of idle at 50 Hz
        t = i * dt
        _, antennas = choreo._compose(t, 1000.0 + t, dt)
        if i > 100:  # skip warm-up
            samples.append(antennas[0] - choreo.ANTENNA_NEUTRAL)
    peak = max(abs(v) for v in samples)
    assert peak >= 0.15, peak  # official app reaches ~0.26 rad per side
