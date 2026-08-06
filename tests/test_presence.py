"""Presence detector + deep-sleep transition tests (no camera needed)."""

import threading
import time
from unittest.mock import MagicMock

from yrobot.presence import PresenceDetector, PresenceState
from yrobot.state import ROBOT_STATE


class _FrameSource:
    """Returns a fake face frame N times, then None."""

    def __init__(self, face_frames: int, total: int):
        self._face_left = face_frames
        self._left = total
        self._faces = 0

    def __call__(self):
        if self._left <= 0:
            return None
        self._left -= 1
        if self._face_left > 0:
            self._face_left -= 1
            self._faces += 1
            return b"\xff\xd8\xff\xe0" + b"0" * 400  # fake JPEG header + junk
        return b"\xff\xd8\xff\xe0" + b"0" * 400


def test_presence_detector_survives_without_cv2():
    """Without OpenCV installed, check_once must not crash and must keep
    the last presence value (best-effort)."""
    # Force cv2 to appear missing for this test.
    import sys
    saved = sys.modules.get("cv2")
    sys.modules["cv2"] = None

    class Src:
        def __call__(self):
            return b"\xff\xd8" + b"0" * 100

    try:
        det = PresenceDetector(Src(), poll_interval_s=0.01)
        det.check_once()
        assert det.state.checks_total == 0  # no cv2 → early return before counting
    finally:
        if saved is not None:
            sys.modules["cv2"] = saved
        else:
            sys.modules.pop("cv2", None)


def test_state_set_deep_sleep():
    """state.py accepts deep_sleep and rejects garbage."""
    ROBOT_STATE.set("deep_sleep")
    assert ROBOT_STATE.current == "deep_sleep"
    ROBOT_STATE.set("bogus")
    assert ROBOT_STATE.current == "deep_sleep"  # unchanged

