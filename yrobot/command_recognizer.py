"""External command recognizer client for deterministic home control."""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

from yrobot.config import Settings

logger = logging.getLogger(__name__)

ALLOWED_COMMAND_TEXTS = frozenset({
    "音响音量调大",
    "音响音量调小",
})


class CommandRecognizer:
    """Call a local/LAN ASR command service and return safe command text."""

    def __init__(
        self,
        settings: Settings,
        *,
        opener: Any = urllib.request.urlopen,
        timeout: float = 3.0,
    ) -> None:
        self.settings = settings
        self._opener = opener
        self._timeout = timeout

    def recognize(self, wav_bytes: bytes) -> str | None:
        if not (self.settings.command_recognizer_enabled and self.settings.command_recognizer_url):
            return None
        request = urllib.request.Request(
            self.settings.command_recognizer_url,
            data=wav_bytes,
            method="POST",
            headers={"Content-Type": "audio/wav"},
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("command recognizer failed: %s", exc)
            return None
        if not isinstance(result, dict) or not result.get("ok"):
            return None
        command = str(result.get("command_text") or "").strip()
        if command not in ALLOWED_COMMAND_TEXTS:
            logger.info("command recognizer rejected non-allowlisted command: %r", command)
            return None
        return command
