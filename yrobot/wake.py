from __future__ import annotations

import math


def _normalize(text: str) -> str:
    ignored = set(" ，,。.!！?？、")
    return "".join(ch for ch in text if ch not in ignored).lower()


class WakeGate:
    def __init__(self, phrase: str, window_s: float, enabled: bool = True) -> None:
        self._phrase = _normalize(phrase)
        self._window_s = window_s
        self._enabled = enabled
        self._awake_until = -math.inf

    def awake(self, now: float) -> bool:
        return not self._enabled or now <= self._awake_until

    def observe_text(self, text: str, now: float) -> bool:
        if not self._enabled:
            return True
        if self._phrase and self._phrase in _normalize(text):
            self._awake_until = now + self._window_s
            return True
        return self.awake(now)
