"""Deterministic local audio feedback for a device-owned photo command."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar

logger = logging.getLogger(__name__)

PHOTO_START_TEXT = "好的，现在拍"
PHOTO_DONE_TEXT = "拍好啦"
_AUDIO_DEVICE = "plug:reachymini_audio_sink"
_ASSET_DIRECTORY = Path(__file__).with_name("assets")
_ASSET_BY_CUE = {
    "start": "photo-start.wav",
    "done": "photo-done.wav",
}


class CaptureOutcome(Protocol):
    """Minimal result contract supplied by ``PhotoLibrary.capture_from_voice``."""

    accepted: bool


class PhotoFeedback(Protocol):
    """The two prompts needed by the local photo flow."""

    def play_start(self) -> bool: ...

    def play_done(self) -> bool: ...


T = TypeVar("T", bound=CaptureOutcome)


class LocalPhotoFeedback:
    """Play bundled, fixed Mandarin prompts through Reachy's local speaker.

    The clips are intentionally local rather than an LLM/TTS request: the
    device always says precisely the same short phrase and the flow still works
    while Xiaozhi is disconnected to stop the original cloud turn.
    """

    def __init__(
        self,
        *,
        asset_directory: Path = _ASSET_DIRECTORY,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._asset_directory = asset_directory
        self._runner = runner

    def play_start(self) -> bool:
        return self._play("start")

    def play_done(self) -> bool:
        return self._play("done")

    def _play(self, cue: str) -> bool:
        filename = _ASSET_BY_CUE[cue]
        path = self._asset_directory / filename
        if not path.is_file():
            logger.warning("photo feedback audio missing: cue=%s", cue)
            return False
        try:
            result = self._runner(
                ("/usr/bin/aplay", "-q", "-D", _AUDIO_DEVICE, str(path)),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("photo feedback audio failed: cue=%s error=%s", cue, exc)
            return False
        if getattr(result, "returncode", 1) != 0:
            logger.warning("photo feedback audio exited nonzero: cue=%s", cue)
            return False
        logger.info("photo feedback audio played: cue=%s", cue)
        return True


def run_local_photo_flow(capture: Callable[[], T], feedback: PhotoFeedback) -> T:
    """Speak the fixed start cue, capture, then speak only on success."""
    feedback.play_start()
    outcome = capture()
    if outcome.accepted:
        feedback.play_done()
    return outcome
