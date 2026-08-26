"""Authenticated local completion cue for Hermes workspace tasks.

This endpoint is intentionally independent of Xiaozhi cloud/MCP: a completed
workspace task is acknowledged by Reachy only after it enters this device's
local audio queue. It accepts no arbitrary text and only plays one bundled,
fixed Mandarin completion cue.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import queue
import re
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yrobot.audio_runtime import SHARED_APLAY_DEVICE

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "v1"
NOTIFY_EVENT = "completed"
TASK_ID_RE = re.compile(r"^HX-\d{8}-\d{6}-[0-9a-f]{4}$")
MAX_CLOCK_SKEW_S = 60
NONCE_TTL_S = 300
MAX_RECENT_ITEMS = 256
_ASSET_PATH = Path(__file__).with_name("assets") / "hermes-task-completed.wav"


def sign_workspace_notification(
    secret: str, *, event: str, task_id: str, timestamp: int, nonce: str
) -> str:
    """Return the canonical HMAC-SHA256 signature shared with Orange Pi."""
    payload = f"{PROTOCOL_VERSION}:{event}:{task_id}:{timestamp}:{nonce}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class NotificationDecision:
    status: int
    body: dict[str, Any]


class CompletionCuePlayer:
    """Serial, idempotent player for the fixed local completion WAV."""

    def __init__(
        self,
        *,
        asset_path: Path = _ASSET_PATH,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._asset_path = asset_path
        self._runner = runner
        self._queue: queue.Queue[str] = queue.Queue(maxsize=16)
        self._recent: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name="hermes-completion-cue", daemon=True
        )
        self._thread.start()

    def enqueue(self, task_id: str) -> tuple[bool, bool]:
        """Queue one task; return (accepted, duplicate)."""
        with self._lock:
            if task_id in self._recent:
                return True, True
            try:
                self._queue.put_nowait(task_id)
            except queue.Full:
                return False, False
            self._recent[task_id] = time.monotonic()
            while len(self._recent) > MAX_RECENT_ITEMS:
                self._recent.popitem(last=False)
            return True, False

    def _run(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                if not self._asset_path.is_file():
                    logger.error("hermes completion cue missing")
                    continue
                result = self._runner(
                    ("/usr/bin/aplay", "-q", "-D", SHARED_APLAY_DEVICE, str(self._asset_path)),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=12,
                )
                if getattr(result, "returncode", 1) == 0:
                    logger.info("hermes completion cue played: task=%s", task_id)
                else:
                    logger.warning("hermes completion cue failed: task=%s", task_id)
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("hermes completion cue error: task=%s error=%s", task_id, exc)
            finally:
                self._queue.task_done()


class HermesWorkspaceNotificationReceiver:
    """Verify local Pi messages before admitting them to the audio queue."""

    def __init__(
        self,
        secret: str,
        player: CompletionCuePlayer,
        *,
        allowed_ip: str = "192.168.1.200",
        now: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._secret = secret
        self._player = player
        self._allowed_ip = allowed_ip
        self._now = now
        self._nonce_factory = nonce_factory
        self._nonces: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._secret)

    def receive(self, remote_ip: str | None, document: Any) -> NotificationDecision:
        if not self.enabled:
            return NotificationDecision(503, {"ok": False, "error": "notify not configured"})
        if remote_ip != self._allowed_ip:
            return NotificationDecision(403, {"ok": False, "error": "untrusted peer"})
        if not isinstance(document, dict):
            return NotificationDecision(422, {"ok": False, "error": "document must be object"})
        event = document.get("event")
        task_id = document.get("task_id")
        timestamp = document.get("timestamp")
        nonce = document.get("nonce")
        signature = document.get("signature")
        valid_task = isinstance(task_id, str) and TASK_ID_RE.match(task_id)
        if event != NOTIFY_EVENT or not valid_task:
            return NotificationDecision(422, {"ok": False, "error": "invalid event or task_id"})
        valid_auth = (
            isinstance(timestamp, int)
            and isinstance(nonce, str)
            and isinstance(signature, str)
        )
        if not valid_auth:
            return NotificationDecision(
                422, {"ok": False, "error": "invalid authentication fields"}
            )
        if len(nonce) < 16 or len(nonce) > 128:
            return NotificationDecision(422, {"ok": False, "error": "invalid nonce"})
        if abs(self._now() - timestamp) > MAX_CLOCK_SKEW_S:
            return NotificationDecision(401, {"ok": False, "error": "expired timestamp"})
        expected = sign_workspace_notification(
            self._secret, event=event, task_id=task_id, timestamp=timestamp, nonce=nonce
        )
        if not hmac.compare_digest(signature, expected):
            return NotificationDecision(401, {"ok": False, "error": "invalid signature"})
        with self._lock:
            self._purge_nonces()
            if nonce in self._nonces:
                return NotificationDecision(409, {"ok": False, "error": "replayed nonce"})
            accepted, duplicate = self._player.enqueue(task_id)
            # Only consume the nonce once the local audio queue accepted it.
            # A transient full queue must remain retryable from Orange Pi.
            if accepted:
                self._nonces[nonce] = self._now()
        if not accepted:
            return NotificationDecision(503, {"ok": False, "error": "audio queue full"})
        return NotificationDecision(202, {"ok": True, "queued": True, "duplicate": duplicate})

    def _purge_nonces(self) -> None:
        cutoff = self._now() - NONCE_TTL_S
        while self._nonces:
            _nonce, created = next(iter(self._nonces.items()))
            if created >= cutoff and len(self._nonces) <= MAX_RECENT_ITEMS:
                break
            self._nonces.popitem(last=False)
