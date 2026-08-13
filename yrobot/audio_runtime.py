"""Testable runtime guards for the Xiaozhi audio path."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")
WAKE_WORDS = ("你好小白", "小白", "阿皮", "reachy", "hey reachy", "嘿", "Hello Reachy")
WAKE_ASR_ALIASES = ("明白", "你好明白")
WAKE_TIMEOUT = 60.0


def _compact_wake_text(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


WAKE_ASR_ALIAS_TEXTS = frozenset(_compact_wake_text(alias) for alias in WAKE_ASR_ALIASES)


class BoundedLatestQueue(Generic[T]):
    """Bounded queue that drops the oldest item when playback falls behind."""

    def __init__(self, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self._queue: queue.Queue[T] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def put_latest(self, item: T) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            else:
                self.dropped += 1
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self.dropped += 1

    def get(self, timeout: float | None = None) -> T:
        return self._queue.get(timeout=timeout)

    def flush(self) -> int:
        removed = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return removed
            removed += 1

    def qsize(self) -> int:
        return self._queue.qsize()


@dataclass
class WakeGate:
    """Track explicit wake transcripts and the bounded active window."""

    phrases: tuple[str, ...] = WAKE_WORDS
    timeout: float = WAKE_TIMEOUT
    active: bool = False
    deadline: float = 0.0

    def observe_transcript(self, transcript: str, now: float | None = None) -> bool:
        if self.active:
            return False
        text = transcript.casefold()
        compact_text = _compact_wake_text(transcript)
        alias_match = compact_text in WAKE_ASR_ALIAS_TEXTS
        if not alias_match and not any(phrase.casefold() in text for phrase in self.phrases):
            return False
        current = time.monotonic() if now is None else now
        self.active = True
        self.deadline = current + self.timeout
        return True

    def note_speech(self, now: float | None = None) -> None:
        if not self.active:
            return
        current = time.monotonic() if now is None else now
        self.deadline = current + self.timeout

    def expire(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        if not self.active or current <= self.deadline:
            return False
        self.active = False
        self.deadline = 0.0
        return True


class PcmPlayback:
    """Bounded aplay writer whose flush also stops buffered device audio."""

    def __init__(
        self,
        command: tuple[str, ...] = (
            "/usr/bin/aplay",
            "-D",
            "plug:reachymini_audio_sink",
            "-r",
            "24000",
            "-f",
            "S16_LE",
            "-c",
            "1",
            "-q",
        ),
        max_chunks: int = 50,
    ) -> None:
        self.command = command
        self._queue = BoundedLatestQueue[bytes | None](max_chunks)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._process: Any | None = None
        self._thread: threading.Thread | None = None

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        return self._queue.dropped

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._write_loop, name="qwen-aplay", daemon=True)
        self._thread.start()

    def put(self, pcm: bytes) -> None:
        self._queue.put_latest(pcm)

    def flush(self) -> int:
        removed = self._queue.flush()
        self._terminate_current()
        return removed

    def close(self) -> None:
        self._stop.set()
        self._queue.put_latest(None)
        self._terminate_current()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _open(self) -> Any:
        process = subprocess.Popen(
            list(self.command),
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        with self._lock:
            self._process = process
        return process

    def _write_loop(self) -> None:
        process = None
        try:
            while not self._stop.is_set():
                try:
                    chunk = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                try:
                    if process is None or process.poll() is not None:
                        process = self._open()
                    process.stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    self._terminate_current()
                    process = None
        finally:
            self._terminate_current()

    def _terminate_current(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass


@dataclass
class TtsWatchdog:
    """Track TTS packet liveness and detect both startup and mid-stream stalls."""

    no_packet_timeout: float = 30.0
    packet_gap_timeout: float = 15.0
    max_duration: float = 180.0
    active: bool = False
    started_at: float = 0.0
    last_packet_at: float = 0.0
    packets: int = 0

    def start(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self.active = True
        self.started_at = current
        self.last_packet_at = 0.0
        self.packets = 0

    def packet(self, now: float | None = None) -> None:
        if not self.active:
            return
        current = time.monotonic() if now is None else now
        self.last_packet_at = current
        self.packets += 1

    def stop(self) -> None:
        self.active = False

    def stalled(self, now: float | None = None) -> bool:
        if not self.active:
            return False
        current = time.monotonic() if now is None else now
        if current - self.started_at >= self.max_duration:
            return True
        if self.packets == 0:
            return current - self.started_at >= self.no_packet_timeout
        return current - self.last_packet_at >= self.packet_gap_timeout
