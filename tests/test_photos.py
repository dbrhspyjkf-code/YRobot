"""Photo capture and OrangePi sync configuration tests."""

import asyncio
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yrobot.app_config import (
    CameraStreamer,
    _MediaHolder,
    build_status,
    register_photo_routes,
    register_settings_routes,
)
from yrobot.config import Settings
from yrobot.hermes_photo_intent import (
    HermesPhotoIntentNotifier,
    sign_photo_intent,
)
from yrobot.photo_cloud_guard import LocalPhotoCloudQuarantine
from yrobot.photos import (
    PhotoCommandController,
    PhotoLibrary,
    PhotoStatus,
    SftpResult,
)
from yrobot.xiaozhi_photo import close_channel_for_local_photo, start_local_photo_flow

_COMPLETE_UPLOAD_ENV = {
    "YROBOT_PHOTO_UPLOAD_ENABLED": "1",
    "YROBOT_PHOTO_SFTP_HOST": "orangepi.example.test",
    "YROBOT_PHOTO_SFTP_PORT": "22",
    "YROBOT_PHOTO_SFTP_USERNAME": "photo-uploader",
    "YROBOT_PHOTO_SFTP_PASSWORD": "test-only-photo-password",
    "YROBOT_PHOTO_SFTP_REMOTE_DIR": "/srv/reachy-photos",
    "YROBOT_PHOTO_SFTP_KNOWN_HOSTS": "/tmp/orangepi_known_hosts",
}


def test_photo_upload_defaults_to_disabled_and_hides_password():
    settings = Settings.from_env({"YROBOT_PHOTO_SFTP_PASSWORD": "test-only-photo-password"})

    assert settings.photo_upload_enabled is False
    assert "test-only-photo-password" not in repr(settings)


def test_photo_upload_requires_complete_configuration():
    with pytest.raises(ValueError, match="PHOTO_SFTP"):
        Settings.from_env({"YROBOT_PHOTO_UPLOAD_ENABLED": "1"})


def test_photo_upload_accepts_complete_configuration():
    settings = Settings.from_env(_COMPLETE_UPLOAD_ENV)

    assert settings.photo_upload_enabled is True
    assert settings.photo_sftp_host == "orangepi.example.test"
    assert settings.photo_sftp_port == 22
    assert settings.photo_sftp_remote_dir == "/srv/reachy-photos"
    assert settings.photo_pending_max_bytes > 0
    assert "test-only-photo-password" not in repr(settings)


def test_photo_intent_settings_require_url_and_secret_together():
    with pytest.raises(ValueError, match="HERMES_PHOTO_INTENT"):
        Settings.from_env({"YROBOT_HERMES_PHOTO_INTENT_URL": "http://example.test/intent"})
    with pytest.raises(ValueError, match="HERMES_PHOTO_INTENT"):
        Settings.from_env({"YROBOT_HERMES_PHOTO_INTENT_SECRET": "test-secret"})

    settings = Settings.from_env(
        {
            "YROBOT_HERMES_PHOTO_INTENT_URL": "http://example.test/intent",
            "YROBOT_HERMES_PHOTO_INTENT_SECRET": "test-secret",
        }
    )
    assert "test-secret" not in repr(settings)


def test_photo_intent_notifier_signs_a_one_shot_request_without_logging_payload():
    captured = {}

    class Response:
        status = 202

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = request.data
        return Response()

    notifier = HermesPhotoIntentNotifier(
        "http://192.168.1.200:8766/api/reachy/local-photo-intent",
        "test-shared-secret",
        now=lambda: 1_000,
        nonce_factory=lambda: "f" * 32,
        opener=opener,
    )

    assert notifier.notify() is True
    payload = json.loads(captured["payload"])
    assert captured["url"].endswith("/api/reachy/local-photo-intent")
    assert captured["timeout"] <= 1.5
    assert payload["signature"] == (
        "25c0a0b8c3dadfb190a594c6832b0bfde46ff6edd22a71b2e9a987bf0506925e"
    )
    assert payload == {
        "timestamp": 1_000,
        "nonce": "f" * 32,
        "signature": sign_photo_intent("test-shared-secret", 1_000, "f" * 32),
    }


def test_photo_intent_notifier_fails_closed_on_request_construction_or_transport_error():
    invalid_url = HermesPhotoIntentNotifier("not-an-http-url", "test-shared-secret")

    def failing_opener(*_args, **_kwargs):
        raise OSError("unreachable")

    unreachable = HermesPhotoIntentNotifier(
        "http://192.168.1.200:8766/api/reachy/local-photo-intent",
        "test-shared-secret",
        opener=failing_opener,
    )

    assert invalid_url.notify() is False
    assert unreachable.notify() is False


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("YROBOT_PHOTO_SFTP_PORT", "0", "PHOTO_SFTP_PORT"),
        ("YROBOT_PHOTO_SFTP_REMOTE_DIR", "relative/photos", "PHOTO_SFTP_REMOTE_DIR"),
        ("YROBOT_PHOTO_PENDING_MAX_BYTES", "0", "PHOTO_PENDING_MAX_BYTES"),
        ("YROBOT_PHOTO_COMMAND_COOLDOWN_S", "-1", "PHOTO_COMMAND_COOLDOWN_S"),
    ],
)
def test_photo_upload_rejects_unsafe_values(key, value, message):
    environment = dict(_COMPLETE_UPLOAD_ENV)
    environment[key] = value

    with pytest.raises(ValueError, match=message):
        Settings.from_env(environment)


class _StaticMedia:
    def __init__(self, frame):
        self._frame = frame
        self.calls = 0

    def get_frame(self):
        self.calls += 1
        return self._frame.copy()


def test_camera_photo_capture_uses_current_media_frame():
    holder = _MediaHolder()
    holder.media = _StaticMedia(np.full((720, 1280, 3), 80, dtype=np.uint8))
    camera = CameraStreamer(holder)

    photo = camera.capture_photo_jpeg(long_edge=1280, jpeg_quality=85)

    assert photo is not None
    assert photo.startswith(b"\xff\xd8")
    assert holder.media.calls == 1


def test_camera_photo_capture_returns_none_without_a_media_frame():
    camera = CameraStreamer(_MediaHolder())

    assert camera.capture_photo_jpeg() is None


class _TransientEmptyFrameMedia:
    def __init__(self, frame):
        self._frame = frame
        self.calls = 0

    def get_frame(self):
        self.calls += 1
        return None if self.calls == 1 else self._frame.copy()


def test_camera_photo_capture_retries_a_transient_empty_frame():
    holder = _MediaHolder()
    holder.media = _TransientEmptyFrameMedia(np.full((40, 60, 3), 80, dtype=np.uint8))
    camera = CameraStreamer(holder)

    photo = camera.capture_photo_jpeg()

    assert photo is not None
    assert photo.startswith(b"\xff\xd8")
    assert holder.media.calls == 2


class _BlockingMedia:
    def __init__(self, frame):
        self._frame = frame
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.active_calls = 0
        self.max_active_calls = 0

    def get_frame(self):
        with self._lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
        self.entered.set()
        self.release.wait(timeout=2)
        with self._lock:
            self.active_calls -= 1
        return self._frame.copy()


def test_camera_preview_and_photo_capture_serialize_media_reads():
    holder = _MediaHolder()
    holder.media = _BlockingMedia(np.zeros((20, 20, 3), dtype=np.uint8))
    camera = CameraStreamer(holder)
    camera.set_running(True)
    assert holder.media.entered.wait(timeout=1)

    captured = []
    worker = threading.Thread(target=lambda: captured.append(camera.capture_photo_jpeg()))
    worker.start()
    time.sleep(0.1)
    holder.media.release.set()
    worker.join(timeout=2)
    camera.set_running(False)

    assert not worker.is_alive()
    assert captured and captured[0] is not None
    assert holder.media.max_active_calls == 1


def _library(tmp_path, *, pending_max_bytes=64 * 1024 * 1024, runner=None):
    holder = _MediaHolder()
    holder.media = _StaticMedia(np.full((720, 1280, 3), 120, dtype=np.uint8))
    camera = CameraStreamer(holder)
    settings = Settings.from_env(
        {
            "YROBOT_PHOTO_SPOOL_DIR": str(tmp_path / "spool"),
            "YROBOT_PHOTO_METADATA_PATH": str(tmp_path / "photos.sqlite3"),
            "YROBOT_PHOTO_PENDING_MAX_BYTES": str(pending_max_bytes),
        }
    )
    return PhotoLibrary(settings=settings, camera=camera, sftp=runner or _FakeRunner())


class _FakeRunner:
    def __init__(self):
        self.uploads: list[tuple[str, bytes]] = []
        self.deletes: list[str] = []
        self.fetches: list[str] = []
        self.failures: set[str] = set()
        self.payloads: dict[str, bytes] = {}

    def upload(self, *, photo_id, variant, local_path, remote_rel):
        rel = f"{remote_rel}.{variant}.jpg"
        if variant in self.failures:
            raise RuntimeError(f"simulated upload failure for {variant}")
        data = local_path.read_bytes()
        self.uploads.append((rel, data))
        self.payloads[rel] = data
        return SftpResult(remote_rel=rel, byte_count=len(data))

    def delete(self, *, photo_id, remote_rel):
        if remote_rel in self.failures:
            raise RuntimeError("simulated delete failure")
        self.deletes.append(remote_rel)
        self.payloads.pop(remote_rel, None)

    def fetch(self, *, photo_id, remote_rel, local_path):
        if remote_rel in self.failures:
            raise RuntimeError("simulated fetch failure")
        data = self.payloads.get(remote_rel)
        if data is None:
            raise RuntimeError("simulated fetch missing")
        self.fetches.append(remote_rel)
        local_path.write_bytes(data)

    def list_remote(self, *, prefix=""):
        return [
            (rel, len(data))
            for rel, data in self.uploads
            if rel.startswith(prefix)
        ]


def test_successful_upload_removes_only_local_images_after_both_remote_renames(tmp_path):
    library = _library(tmp_path)

    record = library.capture_from_voice(source="voice-xz")
    library._run_once_for_test(record.photo_id)  # noqa: SLF001 - private helper
    state = library.get(record.photo_id)

    assert state.status == PhotoStatus.UPLOADED
    assert state.full_path is None
    assert state.thumb_path is None
    remaining = list(Path(tmp_path / "spool").rglob("*"))
    assert remaining == []
    assert library.sftp_runner.uploads  # already collected via _FakeRunner


def test_failed_upload_keeps_pending_images_and_marks_retryable_failure(tmp_path):
    runner = _FakeRunner()
    runner.failures.add("full")
    library = _library(tmp_path, runner=runner)

    record = library.capture_from_voice(source="voice-qwen")
    library._run_once_for_test(record.photo_id)  # noqa: SLF001
    state = library.get(record.photo_id)

    assert state.status == PhotoStatus.FAILED
    assert state.full_path is not None
    assert state.thumb_path is not None
    assert state.full_path.exists()
    assert state.thumb_path.exists()


def test_pending_retry_schedule_survives_process_restart(tmp_path, monkeypatch):
    wall_time = [1_700_000_000.0]
    monotonic_time = [10_000.0]
    monkeypatch.setattr("yrobot.photos.time.time", lambda: wall_time[0])
    monkeypatch.setattr("yrobot.photos.time.monotonic", lambda: monotonic_time[0])
    library = _library(tmp_path)

    record = library.capture_from_dashboard()
    assert record.accepted is True

    # Simulate a process/device restart: monotonic time starts over while wall
    # time advances beyond the scheduled five-second retry delay.
    monotonic_time[0] = 1.0
    wall_time[0] += 6.0
    restarted = _library(tmp_path)

    assert (record.photo_id, 0) in restarted._ready_photo_ids()  # noqa: SLF001


def test_photo_metadata_never_contains_password_or_local_temp_path(tmp_path):
    library = _library(tmp_path)

    record = library.capture_from_voice(source="voice-xz")

    listing = library.list_metadata()
    entry = listing[0]
    serialized = entry.as_public_dict()
    state = library.get(record.photo_id)
    forbidden_paths = {str(state.full_path.parent), str(state.thumb_path.parent)}
    assert state.full_path is not None
    assert state.thumb_path is not None
    payload = repr(serialized).lower()
    for forbidden in forbidden_paths:
        assert forbidden.lower() not in payload
    assert "test-only-photo-password" not in repr(serialized)
    assert "remote_path" not in serialized
    assert "remote_rel" not in serialized


def test_pending_bytes_cap_rejects_new_capture_without_disk_growth(tmp_path):
    library = _library(tmp_path, pending_max_bytes=1)

    outcome = library.capture_from_voice(source="dashboard")

    assert outcome.accepted is False
    assert "pending" in outcome.message.lower() or "\u6682\u5b58" in outcome.message


def test_remote_delete_requires_success_before_metadata_is_removed(tmp_path):
    runner = _FakeRunner()
    library = _library(tmp_path, runner=runner)
    record = library.capture_from_voice(source="voice-xz")
    library._run_once_for_test(record.photo_id)  # noqa: SLF001
    state = library.get(record.photo_id)
    assert state is not None and state.remote_rel is not None

    runner.failures.add(f"{state.remote_rel}.full.jpg")
    runner.failures.add(f"{state.remote_rel}.thumb.jpg")

    outcome = library.delete_remote(record.photo_id)
    assert outcome.ok is False
    assert library.get(record.photo_id).status == PhotoStatus.UPLOADED

    runner.failures.clear()
    outcome = library.delete_remote(record.photo_id)
    assert outcome.ok is True
    assert library.get(record.photo_id) is None


def test_photo_command_accepts_user_phrase_but_not_unrelated_camera_talk():
    controller = PhotoCommandController(cooldown_s=4.0)
    a = controller.observe("帮我拍张照")
    b = controller.observe("你看到了什么？")
    c = controller.observe("帮我拍张照")

    assert a is True
    assert b is False
    assert c is False


def test_photo_command_accepts_bare_photo_word_from_xiaozhi_stt():
    controller = PhotoCommandController(cooldown_s=4.0)

    assert controller.observe("拍照。") is True


def test_local_photo_command_closes_xiaozhi_channel_before_cloud_tool_can_run():
    class FakeChannel:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    channel = FakeChannel()

    asyncio.run(close_channel_for_local_photo(channel))

    assert channel.closed is True


def test_local_photo_flow_fences_cloud_before_failed_intent_notification():
    events: list[str] = []

    class FakeChannel:
        closed = False

        async def close(self):
            events.append("closed")
            self.closed = True

    channel = FakeChannel()
    intent_notified = asyncio.run(
        start_local_photo_flow(
            channel,
            suppress_cloud_uplink=lambda: events.append("fenced"),
            notify_intent=lambda: events.append("notified") or False,
            start_capture=lambda: events.append("capture"),
        )
    )

    assert intent_notified is False
    assert events == ["fenced", "notified", "capture", "closed"]
    assert channel.closed is True


def test_local_photo_flow_fences_cloud_before_successful_intent_notification():
    events: list[str] = []

    class FakeChannel:
        closed = False

        async def close(self):
            events.append("closed")
            self.closed = True

    channel = FakeChannel()
    intent_notified = asyncio.run(
        start_local_photo_flow(
            channel,
            suppress_cloud_uplink=lambda: events.append("fenced"),
            notify_intent=lambda: events.append("notified") or True,
            start_capture=lambda: events.append("capture"),
        )
    )

    assert intent_notified is True
    assert events == ["fenced", "notified", "capture", "closed"]
    assert channel.closed is True


def test_local_photo_cloud_quarantine_drops_residual_uplink_until_window_expires():
    now = [100.0]
    quarantine = LocalPhotoCloudQuarantine(now=lambda: now[0], duration_s=10.0)

    assert quarantine.active() is False
    quarantine.arm()
    assert quarantine.active() is True
    assert quarantine.remaining_s() == 10.0

    now[0] = 109.9
    quarantine.arm()
    assert quarantine.remaining_s() == 10.0

    now[0] = 119.8
    assert quarantine.active() is True
    assert quarantine.take_conversation_resume() is False
    now[0] = 119.9
    assert quarantine.active() is False
    assert quarantine.take_conversation_resume() is True
    assert quarantine.take_conversation_resume() is False


def test_photo_command_strips_extra_whitespace_and_normalises_unicode():
    controller = PhotoCommandController(cooldown_s=4.0)

    assert controller.observe("帮我   拍 张   照") is True
    assert controller.observe("帮我 拍 照") is False  # cooldown active


def test_photo_library_initialises_pending_bytes_zero(tmp_path):
    library = _library(tmp_path)

    snapshot = library.status()

    assert snapshot.enabled is False
    assert snapshot.pending_bytes == 0
    assert snapshot.queue_depth == 0


def test_duplicate_transcript_within_cooldown_creates_one_photo():
    controller = PhotoCommandController(cooldown_s=10.0)

    assert controller.observe("帮我拍张照") is True
    assert controller.observe("帮我拍张照") is False


def test_photo_command_rejects_long_unrelated_sentence():
    controller = PhotoCommandController(cooldown_s=0.0)

    assert controller.observe("请介绍一下今天的天气情况") is False
    assert controller.observe("你看前面那个东西是什么") is False


def test_fetch_remote_bytes_returns_payload_and_cleans_up(tmp_path):
    runner = _FakeRunner()
    library = _library(tmp_path, runner=runner)
    record = library.capture_from_voice(source="voice-xz")
    library._run_once_for_test(record.photo_id)  # noqa: SLF001

    payload = library.fetch_remote_bytes(record.photo_id, "thumb")

    assert payload is not None
    assert payload.startswith(b"\xff\xd8")
    assert runner.fetches and runner.fetches[0].endswith(".thumb.jpg")
    # Spool directory is empty again after the fetch temp file cleanup
    leftover = list((tmp_path / "spool").glob(".fetch-*"))
    assert leftover == []


def test_fetch_remote_bytes_returns_none_for_unknown_photo(tmp_path):
    library = _library(tmp_path)

    assert library.fetch_remote_bytes("0" * 32, "thumb") is None
    assert library.fetch_remote_bytes("deadbeef" * 4, "bogus") is None


def test_status_public_dict_never_leaks_remote_host_or_spool(tmp_path):
    runner = _FakeRunner()
    library = _library(tmp_path, runner=runner)
    record = library.capture_from_voice(source="voice-xz")
    library._run_once_for_test(record.photo_id)  # noqa: SLF001
    snapshot = library.status()
    assert snapshot.enabled is False
    assert snapshot.queue_depth == 0

    listing = library.list_metadata()
    assert listing, "expected at least one metadata entry"
    payload = repr([entry.as_public_dict() for entry in listing]).lower()
    raw = library.get(record.photo_id)
    assert raw is not None
    forbidden_paths: set[str] = set()
    if raw.full_path is not None:
        forbidden_paths.add(str(raw.full_path.parent))
    if raw.thumb_path is not None:
        forbidden_paths.add(str(raw.thumb_path.parent))
    for forbidden in ("orangepi", "sftp", "test-only-photo-password", *forbidden_paths):
        assert forbidden.lower() not in payload


def _photo_client(tmp_path, *, runner=None) -> tuple[TestClient, PhotoLibrary]:
    library = _library(tmp_path, runner=runner)
    app = FastAPI()
    register_photo_routes(app, library)
    return TestClient(app), library


def test_photo_list_returns_metadata_without_credentials(tmp_path):
    runner = _FakeRunner()
    client, library = _photo_client(tmp_path, runner=runner)
    record = library.capture_from_voice(source="voice-xz")
    library._run_once_for_test(record.photo_id)  # noqa: SLF001

    response = client.get("/api/photos?limit=5")

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 5
    assert body["photos"]
    entry = body["photos"][0]
    for forbidden in (
        "password",
        "host",
        "remote_host",
        "remote_path",
        "remote_rel",
        "orangepi",
    ):
        assert forbidden not in entry
    assert entry["status"] == PhotoStatus.UPLOADED.value


def test_photo_capture_endpoint_returns_202_and_queue_row(tmp_path):
    client, library = _photo_client(tmp_path)

    response = client.post("/api/photos/capture")

    assert response.status_code == 202
    body = response.json()
    assert body["photo_id"]
    listing = library.list_metadata()
    assert listing and listing[0].source == "dashboard"


def test_photo_image_returns_jpeg_with_private_no_store_headers(tmp_path):
    runner = _FakeRunner()
    client, library = _photo_client(tmp_path, runner=runner)
    record = library.capture_from_voice(source="voice-xz")
    library._run_once_for_test(record.photo_id)  # noqa: SLF001

    response = client.get(f"/api/photos/{record.photo_id}/image?variant=thumb")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content.startswith(b"\xff\xd8")


def test_photo_retry_requeues_only_known_photo_id(tmp_path):
    runner = _FakeRunner()
    runner.failures.add("full")
    client, library = _photo_client(tmp_path, runner=runner)
    record = library.capture_from_voice(source="voice-xz")
    library._run_once_for_test(record.photo_id)  # noqa: SLF001
    state = library.get(record.photo_id)
    assert state is not None and state.status == PhotoStatus.FAILED.value

    retry_response = client.post(f"/api/photos/{record.photo_id}/retry")
    assert retry_response.status_code == 200
    runner.failures.discard("full")
    library._run_once_for_test(record.photo_id)  # noqa: SLF001
    after = library.get(record.photo_id)
    assert after is not None and after.status == PhotoStatus.UPLOADED.value

    missing_response = client.post("/api/photos/00000000000000000000000000000000/retry")
    assert missing_response.status_code == 404


def test_photo_retry_rejects_pending_and_uploaded_photo(tmp_path):
    client, library = _photo_client(tmp_path, runner=_FakeRunner())
    record = library.capture_from_voice(source="dashboard")

    pending_response = client.post(f"/api/photos/{record.photo_id}/retry")
    assert pending_response.status_code == 409
    assert library.get(record.photo_id).status == PhotoStatus.PENDING

    library._run_once_for_test(record.photo_id)  # noqa: SLF001
    assert library.get(record.photo_id).status == PhotoStatus.UPLOADED
    uploaded_response = client.post(f"/api/photos/{record.photo_id}/retry")
    assert uploaded_response.status_code == 409
    assert library.get(record.photo_id).status == PhotoStatus.UPLOADED


def test_photo_delete_keeps_metadata_when_remote_delete_fails(tmp_path):
    runner = _FakeRunner()
    client, library = _photo_client(tmp_path, runner=runner)
    record = library.capture_from_voice(source="voice-xz")
    library._run_once_for_test(record.photo_id)  # noqa: SLF001
    state = library.get(record.photo_id)
    assert state is not None and state.remote_rel is not None
    runner.failures.add(f"{state.remote_rel}.full.jpg")

    response = client.delete(f"/api/photos/{record.photo_id}")
    assert response.status_code == 502
    assert library.get(record.photo_id) is not None

    runner.failures.clear()
    ok_response = client.delete(f"/api/photos/{record.photo_id}")
    assert ok_response.status_code == 200
    assert library.get(record.photo_id) is None


def test_photo_status_endpoint_returns_summary(tmp_path):
    client, _ = _photo_client(tmp_path)

    response = client.get("/api/photos/status")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["queue_depth"] == 0
    assert "host" not in body
    assert "remote_dir" not in body


def test_build_status_includes_photos_partition_without_secrets(tmp_path):
    library = _library(tmp_path)

    snapshot = build_status({}, photo_library=library)

    assert "photos" in snapshot
    photos = snapshot["photos"]
    assert photos["enabled"] is False
    for forbidden in ("host", "password", "orangepi", "remote_dir"):
        assert forbidden not in repr(photos).lower()


def test_register_settings_routes_and_register_photo_routes_share_library(tmp_path):
    """Regression: `/api/status` must use the library injected into photo routes."""
    environment = {
        **_COMPLETE_UPLOAD_ENV,
        "YROBOT_PHOTO_SPOOL_DIR": str(tmp_path / "spool"),
        "YROBOT_PHOTO_METADATA_PATH": str(tmp_path / "meta.sqlite3"),
    }
    enabled_library = PhotoLibrary(
        settings=Settings.from_env(environment),
        camera=CameraStreamer(_MediaHolder()),
        sftp=_FakeRunner(),
    )
    app = FastAPI()
    register_settings_routes(app, media_holder=_MediaHolder())
    register_photo_routes(app, enabled_library)

    response = TestClient(app).get("/api/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"]["photos"]["enabled"] is True
