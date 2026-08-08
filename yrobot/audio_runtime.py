"""Testable runtime guards for the Xiaozhi audio path."""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from typing import Generic, TypeVar


T = TypeVar("T")


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
