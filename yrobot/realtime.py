"""MiniCPM-o 4.5 realtime gateway client.

Protocol (verified against https://minicpmo45.modelbest.cn/docs/en/realtime-api/):

    connect  ?mode={audio|video}
      <- session.queued / session.queue_update      (position in line)
      <- session.queue_done                          (worker assigned)
      -> session.init {system_prompt, config}
      <- session.created                             (~14 s: server model reset)
      -> input.append {audio, input_id, force_listen?}        every 1 s
                       + video_frames only in mode=video
      <- response.output.delta kind in {listen,text,audio}
      -> session.close / <- session.closed

Uplink audio is base64 float32 PCM 16 kHz mono; downlink audio deltas are
24 kHz. Only a ``listen`` delta marks an utterance boundary — text and audio
deltas are independent streams. A synchronous ``websockets`` client plus one
receiver thread keeps the whole app thread-based like the Reachy SDK.
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from websockets.sync.client import connect

from yrobot.config import Settings

logger = logging.getLogger(__name__)

DOWNLINK_RATE = 24_000
UPLINK_RATE = 16_000
UPLINK_UNIT_SAMPLES = UPLINK_RATE


def encode_reference_wav(path: str) -> str:
    """Return a WAV file as base64 float32 16 kHz mono PCM.

    Reference clips are startup configuration, so a small in-memory conversion
    is preferable to adding a heavyweight audio dependency to the robot.
    """
    source = Path(path).expanduser()
    try:
        with wave.open(str(source), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (OSError, wave.Error) as exc:
        raise ValueError(f"cannot read reference audio {source}: {exc}") from exc
    if channels < 1 or rate <= 0 or sample_width not in (1, 2, 4):
        raise ValueError("reference WAV must be PCM with 8, 16, or 32-bit samples")

    if sample_width == 1:
        pcm = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    else:
        pcm = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2_147_483_648.0
    usable = len(pcm) // channels * channels
    if usable == 0:
        raise ValueError("reference WAV is empty")
    pcm = pcm[:usable].reshape(-1, channels).mean(axis=1)
    if rate != UPLINK_RATE:
        count = max(1, round(len(pcm) * UPLINK_RATE / rate))
        positions = np.arange(count, dtype=np.float64) * rate / UPLINK_RATE
        pcm = np.interp(positions, np.arange(len(pcm)), pcm).astype(np.float32)
    return base64.b64encode(pcm.astype("<f4", copy=False).tobytes()).decode()


def _voice_payload(settings: Settings) -> dict[str, str]:
    voice: dict[str, str] = {}
    if settings.ref_audio_path:
        voice["ref_audio_base64"] = encode_reference_wav(settings.ref_audio_path)
    if settings.tts_ref_audio_path:
        voice["tts_ref_audio_base64"] = encode_reference_wav(settings.tts_ref_audio_path)
    return voice


def build_session_payload(settings: Settings, system_prompt: str | None = None) -> dict:
    """Build the documented ``session.init.payload`` object.

    Keeping protocol construction outside the socket lifecycle makes the
    video/voice contract directly testable without a gateway connection.
    """
    payload: dict = {
        "system_prompt": system_prompt or settings.effective_system_prompt,
        "config": {"length_penalty": settings.length_penalty},
    }
    voice = _voice_payload(settings)
    if voice:
        payload["voice"] = voice
    return payload


@dataclass(frozen=True)
class Delta:
    """One ``response.output.delta`` server event."""

    kind: str  # "listen" | "text" | "audio"
    text: str = ""
    audio: np.ndarray = field(default_factory=lambda: np.empty(0, np.float32))
    response_id: str = ""
    input_id: str = ""
    metrics: dict = field(default_factory=dict)
    received_at: float = field(default_factory=time.monotonic)


class ThinkFilter:
    """Drop ``<think>…</think>`` spans that leak across text deltas.

    The Qwen3 base occasionally emits reasoning tags on the duplex path;
    they are noise for captions and must never be logged as speech.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._thinking = False

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while True:
            tag = "</think>" if self._thinking else "<think>"
            pos = self._buf.find(tag)
            if pos >= 0:
                if not self._thinking:
                    out.append(self._buf[:pos])
                self._buf = self._buf[pos + len(tag) :]
                self._thinking = not self._thinking
                continue
            tail = _partial_tag_suffix(self._buf, tag)
            if not self._thinking:
                out.append(self._buf[: len(self._buf) - tail])
            self._buf = self._buf[len(self._buf) - tail :]
            return "".join(out)


def _partial_tag_suffix(buf: str, tag: str) -> int:
    """Length of the longest ``buf`` suffix that is a proper prefix of ``tag``."""
    for size in range(min(len(tag) - 1, len(buf)), 0, -1):
        if tag.startswith(buf[-size:]):
            return size
    return 0


class RealtimeClient:
    """One gateway session: open() → send_chunk()* → close().

    ``on_delta`` is invoked from the receiver thread; ``on_closed`` fires
    exactly once when the session ends for any reason.
    """

    def __init__(
        self,
        settings: Settings,
        on_delta: Callable[[Delta], None],
        on_closed: Callable[[str], None],
        *,
        system_prompt: str | None = None,
    ) -> None:
        self._settings = settings
        self._on_delta = on_delta
        self._on_closed = on_closed
        self._ws = None
        self._send_lock = threading.Lock()
        self._queue_done = threading.Event()
        self._created = threading.Event()
        self._closed_once = threading.Event()
        self._system_prompt = system_prompt or settings.effective_system_prompt
        self.session_id = ""

    def open(self, timeout: float = 120.0) -> None:
        """Connect, pass the queue, init the session and await creation."""
        s = self._settings
        ssl_ctx: ssl.SSLContext | None = None
        if s.url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if not s.tls_verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        t0 = time.monotonic()
        self._ws = connect(s.url, ssl=ssl_ctx, open_timeout=30, max_size=32 * 1024 * 1024)
        threading.Thread(target=self._receiver, name="yrobot-recv", daemon=True).start()

        if not self._wait_queue(timeout):
            raise TimeoutError("gateway queue timeout")
        payload = build_session_payload(s, self._system_prompt)
        self._send({"type": "session.init", "payload": payload})
        if not self._created.wait(timeout):
            raise TimeoutError("session.created timeout")
        logger.info("session %s ready in %.1f s", self.session_id, time.monotonic() - t0)

    def send_chunk(
        self,
        audio_16k: np.ndarray,
        jpeg: bytes | None,
        force_listen: bool,
        input_id: str,
    ) -> None:
        """Send one complete one-second inference unit plus an optional frame."""
        if len(audio_16k) != UPLINK_UNIT_SAMPLES:
            raise ValueError(
                f"MiniCPM-o duplex input must contain {UPLINK_UNIT_SAMPLES} samples, "
                f"got {len(audio_16k)}"
            )
        payload: dict = {
            "audio": base64.b64encode(audio_16k.astype("<f4").tobytes()).decode(),
            "input_id": input_id,
        }
        if jpeg is not None:
            payload["video_frames"] = [base64.b64encode(jpeg).decode()]
            payload["max_slice_nums"] = 1
        if force_listen:
            payload["force_listen"] = True
        self._send({"type": "input.append", "input": payload})

    def close(self, reason: str = "user_stop") -> None:
        """Best-effort graceful close; safe to call from any thread, twice."""
        try:
            self._send({"type": "session.close", "reason": reason})
            # Give the worker a moment to acknowledge and recycle cleanly;
            # an abrupt transport close tends to wedge the next connect.
            self._closed_once.wait(3.0)
        except Exception:
            pass
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass

    # -- internals ---------------------------------------------------------

    def _send(self, message: dict) -> None:
        if self._ws is None:
            raise ConnectionError("session not open")
        with self._send_lock:
            self._ws.send(json.dumps(message))

    def _wait_queue(self, timeout: float) -> bool:
        return self._queue_done.wait(timeout)

    def _receiver(self) -> None:
        reason = "connection_lost"
        queued_since: float | None = None
        try:
            assert self._ws is not None
            for raw in self._ws:
                event = json.loads(raw)
                etype = event.get("type", "")
                if etype == "response.output.delta":
                    self._on_delta(_parse_delta(event))
                elif etype in ("session.queued", "session.queue_update"):
                    now = time.monotonic()
                    queued_since = queued_since or now
                    logger.info(
                        "queued: position %s, ~%ss wait",
                        event.get("position"),
                        event.get("estimated_wait_s"),
                    )
                    if now - queued_since > 15.0:
                        queued_since = float("inf")  # warn once per connection
                        logger.warning(
                            "still queued after 15 s: the gateway worker is held by "
                            "another client — check for a second yrobot instance "
                            "(pgrep -af yrobot; dashboard-launched app?), another "
                            "machine/browser demo on this gateway, or a wedged "
                            "session that needs a gateway restart"
                        )
                elif etype == "session.queue_done":
                    self._queue_done.set()
                elif etype == "session.created":
                    self.session_id = event.get("session_id", "")
                    self._created.set()
                elif etype == "session.closed":
                    reason = event.get("reason", "closed")
                    break
                elif etype == "error":
                    logger.error("gateway error: %s", event.get("error"))
        except Exception as exc:  # noqa: BLE001 — any transport failure ends the session
            logger.info("receiver ended: %s", exc)
        if not self._closed_once.is_set():
            self._closed_once.set()
            self._on_closed(reason)


def _parse_delta(event: dict) -> Delta:
    common = {
        "response_id": event.get("response_id", ""),
        "input_id": event.get("input_id", ""),
        "metrics": dict(event.get("metrics") or {}),
    }
    kind = event.get("kind", "")
    if kind == "audio":
        pcm = np.frombuffer(base64.b64decode(event.get("audio", "")), dtype="<f4")
        return Delta(kind="audio", audio=pcm, **common)
    return Delta(kind=kind, text=event.get("text", ""), **common)
