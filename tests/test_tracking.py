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
