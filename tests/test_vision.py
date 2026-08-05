"""Continuous vision policy tests (no camera hardware)."""

import time

import numpy as np
import pytest

import yrobot.vision as vision_module
from yrobot.vision import FRAME_MAX_DIM, LatestCamera, SceneChangeDetector, shrink_jpeg


def test_shrink_jpeg_downscales_to_model_vision_size():
    cv2 = pytest.importorskip("cv2")
    frame = np.random.default_rng(0).integers(0, 255, (720, 1280, 3), dtype=np.uint8)
    jpeg = shrink_jpeg(frame)
    assert jpeg is not None
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert max(decoded.shape[:2]) == FRAME_MAX_DIM
    assert len(jpeg) < len(cv2.imencode(".jpg", frame)[1].tobytes()) / 3


def test_shrink_jpeg_keeps_small_frames_and_handles_none():
    cv2 = pytest.importorskip("cv2")
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    decoded = cv2.imdecode(np.frombuffer(shrink_jpeg(frame), np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[:2] == (240, 320)
    assert shrink_jpeg(None) is None


def test_scene_change_detector_ignores_small_noise_but_sees_new_scene():
    detector = SceneChangeDetector(threshold=0.04)
    base = np.full((64, 64, 3), 80, np.uint8)
    assert detector.changed(base)
    assert not detector.changed(np.full_like(base, 84))
    assert detector.changed(np.full_like(base, 160))


def test_latest_camera_keeps_only_newest_frame_while_active(monkeypatch):
    monkeypatch.setattr(vision_module, "cv2", None)

    class FakeMedia:
        def __init__(self):
            self.count = 0

        def get_frame_jpeg(self):
            self.count += 1
            return f"frame-{self.count}".encode()

    camera = LatestCamera(
        FakeMedia(),
        active=lambda now: True,
        capture_period_s=0.02,
        idle_heartbeat_s=1.0,
    )
    camera.start()
    try:
        time.sleep(0.09)
        latest = camera.take_latest()
        assert latest is not None
        assert camera.take_latest() is None
        time.sleep(0.05)
        assert camera.take_latest() != latest
    finally:
        camera.close()
        camera.join(timeout=2)


def test_idle_camera_deduplicates_static_scene_but_keeps_heartbeat(monkeypatch):
    monkeypatch.setattr(vision_module, "cv2", None)

    class StaticMedia:
        def get_frame_jpeg(self):
            return b"same-frame"

    camera = LatestCamera(
        StaticMedia(),
        active=lambda now: False,
        capture_period_s=0.02,
        idle_heartbeat_s=0.20,
    )
    camera.start()
    try:
        time.sleep(0.05)
        assert camera.take_latest() == b"same-frame"
        time.sleep(0.04)
        assert camera.take_latest() is None
        time.sleep(0.18)
        assert camera.take_latest() == b"same-frame"
    finally:
        camera.close()
        camera.join(timeout=2)
