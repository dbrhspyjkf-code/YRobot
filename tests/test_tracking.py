"""Focused tests for camera-backed speaker tracking."""

import math

import pytest

from yrobot.tracking import SpeakerTracker


def test_speaker_tracker_accepts_shared_camera_streamer():
    class Camera:
        def set_running(self, value):
            return value

        def latest(self):
            return b"jpeg"

    tracker = SpeakerTracker(object(), object(), camera_streamer=Camera())

    assert tracker._camera_streamer.latest() == b"jpeg"


# ── Face-led gaze during conversation (official-app positioning parity) ────

def test_face_leads_whenever_conversation_active():
    from yrobot.tracking import visual_gaze_should_lead

    # Official-style positioning: a fresh face lock IS the target, no matter
    # what the audio DoA is doing (it may even be chasing TTS echo).
    assert visual_gaze_should_lead(face_age_s=0.2, conversation_active=True)
    assert visual_gaze_should_lead(face_age_s=0.9, conversation_active=True)


def test_face_never_leads_when_conversation_inactive():
    from yrobot.tracking import visual_gaze_should_lead

    assert not visual_gaze_should_lead(face_age_s=0.2, conversation_active=False)


def test_face_leads_requires_fresh_face():
    from yrobot.tracking import visual_gaze_should_lead

    assert not visual_gaze_should_lead(face_age_s=2.0, conversation_active=True)


class _GazeRecorderChoreo:
    def __init__(self):
        self.targets = []

    def set_gaze_target(self, world_yaw, now=None, source="audio"):
        self.targets.append((world_yaw, source))

    def set_tracking_debug(self, **kwargs):
        pass


def test_face_loop_publishes_face_gaze_during_conversation():
    """With conversation active, a locked face publishes source='face' gaze
    targets (official-app positioning: the face IS the target)."""
    from yrobot.tracking import SpeakerTracker

    choreo = _GazeRecorderChoreo()
    tracker = SpeakerTracker(object(), choreo)
    tracker.set_conversation_active(True)

    tracker._publish_face_gaze(math.radians(25.0))
    tracker._publish_face_gaze(math.radians(26.0))  # within deadband: ignored
    tracker._publish_face_gaze(math.radians(45.0))

    sources = [src for _, src in choreo.targets]
    assert sources == ["face", "face"]
    assert choreo.targets[0][0] == pytest.approx(math.radians(25.0))
    assert choreo.targets[-1][0] == pytest.approx(math.radians(45.0))


def test_doa_path_yields_to_fresh_face_lock():
    """While a face is locked, the DoA callback publishes the face yaw
    directly instead of blending audio toward the speaker echo."""
    import time as _time

    from yrobot.tracking import SpeakerTracker

    choreo = _GazeRecorderChoreo()
    tracker = SpeakerTracker(object(), choreo)

    tracker._visual_gaze[0] = (math.radians(40.0), _time.time())
    tracker._set_speaker_gaze(math.radians(-170.0))  # TTS-echo-style garbage

    assert len(choreo.targets) == 1
    yaw, source = choreo.targets[0]
    assert source == "face"
    assert yaw == pytest.approx(math.radians(40.0))


def test_doa_path_audio_only_without_face():
    import time as _time

    from yrobot.tracking import SpeakerTracker

    choreo = _GazeRecorderChoreo()
    tracker = SpeakerTracker(object(), choreo)
    tracker._visual_gaze[0] = None

    tracker._set_speaker_gaze(math.radians(30.0))

    yaw, source = choreo.targets[0]
    assert source == "audio"
    assert yaw == pytest.approx(math.radians(30.0))


def test_doa_muted_while_robot_speaking():
    """TTS echo must never open the DoA gate (the head chased its own
    speaker around the room when VAD mistook echo for user speech)."""
    from yrobot.tracking import SpeakerTracker

    tracker = SpeakerTracker(object(), _GazeRecorderChoreo())

    tracker.set_user_speaking(True)
    assert tracker._doa_active() is True

    tracker.set_robot_speaking(True)
    assert tracker._doa_active() is False  # echo guard

    tracker.set_robot_speaking(False)
    tracker.set_user_speaking(False)
    assert tracker._doa_active() is False


def test_face_loop_silent_when_conversation_inactive():
    from yrobot.tracking import SpeakerTracker

    choreo = _GazeRecorderChoreo()
    tracker = SpeakerTracker(object(), choreo)
    tracker.set_conversation_active(False)

    tracker._publish_face_gaze(math.radians(25.0))

    assert choreo.targets == []


# ── Daemon-side head tracking (official Conversation App mechanism) ─────────

class _FaceTarget:
    def __init__(self, detected):
        self.detected = detected


class _DaemonTrackingRobot:
    """Fake ReachyMini recording daemon head-tracking commands."""

    def __init__(self, detected=True, head_yaw_rad=0.3):
        self.calls = []
        self._detected = detected
        self._head_yaw = head_yaw_rad

    def start_head_tracking(self, weight=1.0):
        self.calls.append(("start", weight))
        return None

    def stop_head_tracking(self):
        self.calls.append(("stop", None))
        return None

    def get_tracked_face(self, wait=True, timeout=5.0):
        self.calls.append(("face", self._detected))
        return _FaceTarget(self._detected)

    def get_current_head_pose(self):
        from yrobot.motion import rpy_pose

        return rpy_pose(0.0, 0.0, self._head_yaw, 0.0)


class _YawChoreo(_GazeRecorderChoreo):
    def __init__(self):
        super().__init__()
        self.synced = []

    def current_yaw(self):
        return 0.0

    def sync_gaze(self, world_yaw):
        self.synced.append(world_yaw)


def test_conversation_start_enables_daemon_tracking():
    from yrobot.tracking import SpeakerTracker

    robot = _DaemonTrackingRobot()
    choreo = _YawChoreo()
    tracker = SpeakerTracker(robot, choreo)

    tracker.set_conversation_active(True)

    assert ("start", 1.0) in robot.calls


def test_conversation_end_stops_daemon_tracking():
    from yrobot.tracking import SpeakerTracker

    robot = _DaemonTrackingRobot()
    tracker = SpeakerTracker(robot, _YawChoreo())
    tracker.set_conversation_active(True)
    robot.calls.clear()

    tracker.set_conversation_active(False)

    assert ("stop", None) in robot.calls


def test_speech_pauses_tracking_with_anchor_when_face_locked():
    import math

    from yrobot.tracking import SpeakerTracker

    robot = _DaemonTrackingRobot(detected=True, head_yaw_rad=0.3)
    choreo = _YawChoreo()
    tracker = SpeakerTracker(robot, choreo)
    tracker.set_conversation_active(True)
    robot.calls.clear()
    choreo.targets.clear()

    tracker.set_robot_speaking(True)

    assert ("start", 0.0) in robot.calls
    # Anchor: the gaze spring POSITION syncs to the daemon's exact head
    # pose so the handoff never snaps. (2026-08-19: no face query anymore —
    # the handoff is unconditional; see the no-face-lock test.)
    assert choreo.synced == [pytest.approx(0.3)]


def test_speech_pauses_tracking_even_without_face_lock():
    """2026-08-19 gesture-first handoff.

    Field data: with a flaky face lock (acquired->lost every second),
    6/6 TTS turns took the old no-face-lock branch, daemon tracking kept
    weight=1.0 on the head and expression moves could only move the
    antennas. Speech must always hand the head to the Choreographer.
    """
    from yrobot.tracking import SpeakerTracker

    robot = _DaemonTrackingRobot(detected=False)
    choreo = _YawChoreo()
    tracker = SpeakerTracker(robot, choreo)
    tracker.set_conversation_active(True)
    robot.calls.clear()

    tracker.set_robot_speaking(True)

    assert ("start", 0.0) in robot.calls
    assert choreo.synced


def test_speech_end_resumes_full_tracking():
    from yrobot.tracking import SpeakerTracker

    robot = _DaemonTrackingRobot(detected=True)
    tracker = SpeakerTracker(robot, _YawChoreo())
    tracker.set_conversation_active(True)
    tracker.set_robot_speaking(True)
    robot.calls.clear()

    tracker.set_robot_speaking(False)

    assert ("start", 1.0) in robot.calls


def test_local_gaze_writers_suppressed_during_daemon_tracking():
    from yrobot.tracking import SpeakerTracker

    robot = _DaemonTrackingRobot()
    choreo = _YawChoreo()
    tracker = SpeakerTracker(robot, choreo)
    tracker.set_conversation_active(True)
    choreo.targets.clear()

    # DoA callback and face-loop publish must not fight the daemon.
    tracker._set_speaker_gaze(1.0)
    tracker._publish_face_gaze(1.0)

    assert choreo.targets == []
