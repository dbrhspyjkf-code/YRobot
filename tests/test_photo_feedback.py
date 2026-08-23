"""Regression tests for deterministic local photo voice feedback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from yrobot.audio_runtime import SHARED_APLAY_DEVICE, xiaozhi_aplay_command
from yrobot.photo_feedback import (
    PHOTO_DONE_TEXT,
    PHOTO_START_TEXT,
    LocalPhotoFeedback,
    run_local_photo_flow,
)


@dataclass
class _Outcome:
    accepted: bool


class _FeedbackRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def play_start(self) -> bool:
        self.events.append("start")
        return True

    def play_done(self) -> bool:
        self.events.append("done")
        return True


def test_local_photo_flow_speaks_exactly_before_and_after_successful_capture():
    events: list[str] = []
    feedback = _FeedbackRecorder(events)

    def capture() -> _Outcome:
        events.append("capture")
        return _Outcome(accepted=True)

    outcome = run_local_photo_flow(capture, feedback)

    assert outcome.accepted is True
    assert events == ["start", "capture", "done"]
    assert PHOTO_START_TEXT == "好的，现在拍"
    assert PHOTO_DONE_TEXT == "拍好啦"


def test_local_photo_flow_never_claims_success_after_rejected_capture():
    events: list[str] = []
    feedback = _FeedbackRecorder(events)

    outcome = run_local_photo_flow(lambda: _Outcome(accepted=False), feedback)

    assert outcome.accepted is False
    assert events == ["start"]


def test_xiaozhi_and_fixed_photo_cues_share_the_dmix_audio_device():
    command = xiaozhi_aplay_command()

    assert command[:3] == ("/usr/bin/aplay", "-D", SHARED_APLAY_DEVICE)
    assert command[3:] == ("-r", "16000", "-f", "S16_LE", "-c", "2", "-q")


def test_feedback_player_uses_reachy_audio_sink_for_bundled_wav(tmp_path: Path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "photo-start.wav").write_bytes(b"RIFF")
    (assets / "photo-done.wav").write_bytes(b"RIFF")
    calls: list[tuple[str, ...]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    feedback = LocalPhotoFeedback(asset_directory=assets, runner=runner)

    assert feedback.play_start() is True
    assert feedback.play_done() is True
    assert calls == [
        ("/usr/bin/aplay", "-q", "-D", SHARED_APLAY_DEVICE, str(assets / "photo-start.wav")),
        ("/usr/bin/aplay", "-q", "-D", SHARED_APLAY_DEVICE, str(assets / "photo-done.wav")),
    ]
