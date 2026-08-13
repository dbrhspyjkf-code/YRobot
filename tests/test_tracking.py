"""Focused tests for camera-backed speaker tracking."""

from yrobot.tracking import SpeakerTracker


def test_speaker_tracker_accepts_shared_camera_streamer():
    class Camera:
        def set_running(self, value):
            return value

        def latest(self):
            return b"jpeg"

    tracker = SpeakerTracker(object(), object(), camera_streamer=Camera())

    assert tracker._camera_streamer.latest() == b"jpeg"
