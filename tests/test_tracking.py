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


# ── Face-led gaze during conversation (official-app face-anchor parity) ─────

def test_visual_leads_when_doa_silent_and_conversation_active():
    from yrobot.tracking import visual_gaze_should_lead

    assert visual_gaze_should_lead(
        face_age_s=0.2, last_doa_age_s=None, conversation_active=True
    )
    # DoA went stale (robot speaking, user quiet) -> face takes over
    assert visual_gaze_should_lead(
        face_age_s=0.2, last_doa_age_s=2.0, conversation_active=True
    )


def test_visual_never_leads_when_conversation_inactive():
    from yrobot.tracking import visual_gaze_should_lead

    assert not visual_gaze_should_lead(
        face_age_s=0.2, last_doa_age_s=None, conversation_active=False
    )


def test_visual_yields_to_fresh_doa():
    from yrobot.tracking import visual_gaze_should_lead

    assert not visual_gaze_should_lead(
        face_age_s=0.2, last_doa_age_s=0.5, conversation_active=True
    )


def test_visual_leads_requires_fresh_face():
    from yrobot.tracking import visual_gaze_should_lead

    assert not visual_gaze_should_lead(
        face_age_s=2.0, last_doa_age_s=None, conversation_active=True
    )


class _GazeRecorderChoreo:
    def __init__(self):
        self.targets = []

    def set_gaze_target(self, world_yaw, now=None, source="audio"):
        self.targets.append((world_yaw, source))


def test_face_loop_publishes_face_gaze_during_conversation():
    """End-to-end-ish: with conversation active and DoA silent, a locked face
    publishes source='face' gaze targets (the official-app face anchor)."""
    import time as _time

    from yrobot.tracking import SpeakerTracker

    choreo = _GazeRecorderChoreo()
    tracker = SpeakerTracker(object(), choreo)
    tracker.set_conversation_active(True)

    # Simulate the face-loop publishing path directly (Haar excluded here).
    tracker._publish_face_gaze(math.radians(25.0))
    tracker._publish_face_gaze(math.radians(26.0))  # within deadband: ignored
    tracker._publish_face_gaze(math.radians(45.0))

    sources = [s for _, s in choreo.targets]
    assert sources == ["face", "face"]
    assert choreo.targets[0][0] == pytest.approx(math.radians(25.0))
    assert choreo.targets[-1][0] == pytest.approx(math.radians(45.0))


def test_face_loop_silent_when_conversation_inactive():
    from yrobot.tracking import SpeakerTracker

    choreo = _GazeRecorderChoreo()
    tracker = SpeakerTracker(object(), choreo)
    tracker.set_conversation_active(False)

    tracker._publish_face_gaze(math.radians(25.0))

    assert choreo.targets == []
