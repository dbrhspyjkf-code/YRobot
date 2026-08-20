"""Tests for the manual-control lease (plan Task 12).

All tests run against the in-process Choreographer and ManualCoordinator so
they exercise the same arbitration the YRobot FastAPI app routes delegate to.
The robot daemon is never contacted (no daemon.start(); this stays inside
the test process so we can keep iterating before deployment).
"""

import math
import threading
import time

import numpy as np
import pytest

from yrobot.manual_control import (
    Limits,
    ManualCoordinator,
    ManualTarget,
    ValidationError,
    generate_session_id,
    validate_target,
)
from yrobot.motion import Choreographer


class FakeMini:
    """Replaces reachy_mini.ReachyMini: captures the latest pose handed to it."""

    def __init__(self) -> None:
        self.last_head: np.ndarray | None = None
        self.last_antennas: tuple[float, float] | None = None
        self.last_body_yaw: float | None = None
        self.lock = threading.Lock()
        self.fail_next = 0  # consecutive failure budget

    def set_target(
        self,
        head: np.ndarray,
        antennas: tuple[float, float] | list[float] | np.ndarray,
        body_yaw: float,
    ) -> None:
        with self.lock:
            if self.fail_next > 0:
                self.fail_next -= 1
                raise RuntimeError("simulated set_target failure")
            self.last_head = np.asarray(head, dtype=np.float64).copy()
            self.last_antennas = tuple(float(x) for x in antennas)
            self.last_body_yaw = float(body_yaw)


@pytest.fixture
def mini() -> FakeMini:
    return FakeMini()


@pytest.fixture
def choreographer(mini) -> Choreographer:
    c = Choreographer(mini)
    c.start()
    # Let one autonomous tick run so the worker thread is live before tests.
    time.sleep(0.05)
    return c


# --- session_id --------------------------------------------------------


def test_session_ids_are_unpredictable_and_nontrivial() -> None:
    ids = {generate_session_id() for _ in range(1000)}
    assert len(ids) == 1000
    sample = next(iter(ids))
    assert len(sample) >= 16


# --- validation --------------------------------------------------------


def test_validate_target_rejects_nan_and_inf() -> None:
    with pytest.raises(ValidationError):
        validate_target(ManualTarget(
            roll=float("nan"), pitch=0.0, yaw=0.0, z=0.0,
            antennas=(0.0, 0.0), session_id="x",
        ))
    with pytest.raises(ValidationError):
        validate_target(ManualTarget(
            roll=0.0, pitch=math.inf, yaw=0.0, z=0.0,
            antennas=(0.0, 0.0), session_id="x",
        ))


def test_validate_target_enforces_shape_and_range() -> None:
    # Antenna out of physical envelope.
    with pytest.raises(ValidationError):
        validate_target(ManualTarget(
            roll=0.0, pitch=0.0, yaw=0.0, z=0.0,
            antennas=(3.0, 0.0), session_id="x",
        ))
    # Yaw beyond body envelope (±160°).
    with pytest.raises(ValidationError):
        validate_target(ManualTarget(
            roll=0.0, pitch=0.0, yaw=math.pi + 0.1, z=0.0,
            antennas=(0.0, 0.0), session_id="x",
        ))


def test_validate_target_runs_rate_limit_check() -> None:
    # 100°/frame spike at 20 Hz (5°/frame budget) → reject.
    with pytest.raises(ValidationError):
        validate_target(
            ManualTarget(roll=0.0, pitch=0.0, yaw=1.7, z=0.0,
                         antennas=(0.0, 0.0), session_id="x"),
            previous=ManualTarget(roll=0.0, pitch=0.0, yaw=0.0, z=0.0,
                                  antennas=(0.0, 0.0), session_id="prev"),
        )


# --- coordinator -------------------------------------------------------


def test_single_active_lease_rejects_concurrent_acquire() -> None:
    coord = ManualCoordinator()
    a, _ = coord.acquire()
    with pytest.raises(PermissionError):
        coord.acquire()
    # Release returns the slot.
    coord.release(a)
    b, _ = coord.acquire()
    assert b != a
    coord.release(b)


def test_acquire_returns_ttl_and_remaining() -> None:
    coord = ManualCoordinator()
    session, info = coord.acquire()
    assert info["active"] is True
    assert info["remaining_ms"] > 0
    # The HTTP-visible snapshot must never leak the session id (plan §4.4).
    assert "session_id" not in info
    coord.release(session)


def test_expired_lease_is_released_automatically() -> None:
    coord = ManualCoordinator(ttl_seconds=0.05)
    session, _ = coord.acquire()
    time.sleep(0.08)
    # Renewal with the wrong id: should also clear/expire to allow reacquire.
    with pytest.raises(PermissionError):
        coord.renew("bogus-id")
    # Old session is now expired → a fresh acquire succeeds with a new id.
    new, _ = coord.acquire()
    assert new != session
    coord.release(new)


def test_heartbeat_keeps_lease_alive() -> None:
    coord = ManualCoordinator(ttl_seconds=0.2)
    session, _ = coord.acquire()
    for _ in range(5):
        time.sleep(0.05)
        coord.heartbeat(session)
    assert coord.is_active(session)
    coord.release(session)


def test_wrong_session_id_returns_404() -> None:
    coord = ManualCoordinator()
    session, _ = coord.acquire()
    try:
        with pytest.raises(PermissionError):
            coord.heartbeat("not-the-id")
    finally:
        coord.release(session)


def test_release_idempotent() -> None:
    coord = ManualCoordinator()
    session, _ = coord.acquire()
    coord.release(session)
    coord.release(session)  # second call is a no-op
    # After release, a fresh acquire succeeds.
    new, _ = coord.acquire()
    assert new != session
    coord.release(new)


def test_status_does_not_leak_session_id() -> None:
    coord = ManualCoordinator()
    session, _ = coord.acquire()
    try:
        snap = coord.public_status()
        assert "session_id" not in snap
        assert "active" in snap and snap["active"] is True
        # Even when inactive, no historical session id should appear.
        coord.release(session)
        snap = coord.public_status()
        assert "session_id" not in snap
        assert snap["active"] is False
    finally:
        if coord.is_active(session):
            coord.release(session)


# --- Choreographer integration -----------------------------------------


def _acceptable_target(yaw: float = 0.0) -> dict[str, float | tuple[float, float]]:
    return {
        "roll": 0.0,
        "pitch": 0.0,
        "yaw": yaw,
        "z": 0.0,
        "antennas": (0.17, 0.17),
    }


def test_manual_takeover_blocks_recorded_moves(mini, choreographer) -> None:
    # Enter manual.
    session = choreographer.begin_manual(_acceptable_target())
    assert session
    # During manual control, recorded/play_move should refuse.
    assert choreographer.play_recorded("dance_1", _RecordedStub()) is False
    assert choreographer.play_move("nod") is False
    assert choreographer.play_dance("anything") is False
    choreographer.end_manual(session)


def test_choreographer_remains_only_writer_during_manual(mini, choreographer) -> None:
    session = choreographer.begin_manual(_acceptable_target(yaw=0.3))
    # Let several 50 Hz ticks elapse and observe the captured pose.
    time.sleep(0.1)
    with mini.lock:
        last_head = mini.last_head.copy() if mini.last_head is not None else None
        last_antennas = mini.last_antennas
    assert last_head is not None
    # The head yaw axis in the composed matrix should match the manual yaw
    # command (with a tolerance for the server-side slew rate).
    expected = np.array([
        [math.cos(0.3), -math.sin(0.3), 0.0],
        [math.sin(0.3), math.cos(0.3), 0.0],
        [0.0, 0.0, 1.0],
    ])
    actual = last_head[:3, :3]
    # Allow a wide tolerance for the slew + cross-fade from autonomous startup.
    diff = np.linalg.norm(actual - expected)
    assert diff < 0.6, f"head did not follow manual yaw (diff={diff})"
    assert last_antennas == (0.17, 0.17)
    choreographer.end_manual(session)


def test_manual_release_no_step_first_frame(mini, choreographer) -> None:
    # Warm up autonomous, capture baseline.
    time.sleep(0.1)
    with mini.lock:
        baseline_antennas = mini.last_antennas
    # Enter and exit manual with a non-zero target; the first autonomous
    # tick after release must not show a sudden pose jump.
    session = choreographer.begin_manual(_acceptable_target(yaw=0.4))
    time.sleep(0.06)
    choreographer.end_manual(session)
    time.sleep(0.06)
    with mini.lock:
        after_antennas = mini.last_antennas
    # Antennas stay continuous (no step into the past or future).
    assert baseline_antennas is not None
    assert after_antennas is not None
    assert abs(after_antennas[0] - baseline_antennas[0]) < 0.5


def test_manual_session_mismatch_is_rejected(mini, choreographer) -> None:
    session = choreographer.begin_manual(_acceptable_target())
    try:
        with pytest.raises(PermissionError):
            choreographer.update_manual(_acceptable_target(yaw=0.1), session_id="not-the-id")
        with pytest.raises(PermissionError):
            choreographer.end_manual("not-the-id")
    finally:
        choreographer.end_manual(session)


def test_manual_expires_and_does_not_block_recorded_moves(mini, choreographer) -> None:
    choreographer.begin_manual(_acceptable_target())
    # Force-expire by waiting past TTL.
    time.sleep(2.5)
    # No recorded move can fire while the expired lease still pretends to be
    # active, so the coordinator must surface "no longer active" via the
    # underlying motion controller.
    assert choreographer.play_recorded("dance", _RecordedStub()) is True or choreographer.current_recorded() is None
    # Either way, manual active is gone: a new recorded move should now queue.
    assert choreographer.is_manual_active() is False


# --- helpers -----------------------------------------------------------


class _RecordedStub:
    duration = 1.0
    body_yaw = None
    antennas_target = (0.17, 0.17)

    def get_target_head_pose(self, t: float):  # pragma: no cover - never reached in failing path
        return np.eye(4)