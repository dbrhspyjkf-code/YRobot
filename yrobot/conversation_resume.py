"""Session-independent wake lease for reconnecting Xiaozhi conversations."""

from __future__ import annotations

import time
from collections.abc import Callable


class ConversationWakeLease:
    """Keep an already-woken conversation alive across a short reconnect.

    This never opens a new conversation by itself: callers must first activate
    it from a verified wake event.  The monotonic lease only lets the next
    Xiaozhi session restore that still-active conversation after transport loss.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        timeout_s: float = 120.0,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._now = now
        self._timeout_s = timeout_s
        self._until = 0.0

    def activate(self) -> None:
        """Start or renew the wake lease after a verified active turn."""
        self._until = self._now() + self._timeout_s

    def active(self) -> bool:
        return self._now() < self._until

    def remaining_s(self) -> float:
        return max(0.0, self._until - self._now())

    def clear(self) -> None:
        self._until = 0.0
