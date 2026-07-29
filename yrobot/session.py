"""Session rotation policy and small cross-session conversation memory."""

from __future__ import annotations

import re
from collections import deque


class RotationPolicy:
    """Rotate over budget at a quiet boundary, or forcibly after a grace period."""

    def __init__(self, time_budget_s: float, kv_budget: float, grace_s: float = 30.0) -> None:
        self._time_budget_s = time_budget_s
        self._kv_budget = kv_budget
        self._grace_s = grace_s
        self._over_since_s: float | None = None

    def reset(self) -> None:
        """Start the budget clock for a new gateway session."""
        self._over_since_s = None

    def should_rotate(self, *, elapsed_s: float, kv_tokens: float, quiet: bool) -> bool:
        over = elapsed_s > self._time_budget_s or kv_tokens > self._kv_budget
        if not over:
            self._over_since_s = None
            return False
        if self._over_since_s is None:
            self._over_since_s = elapsed_s
        return quiet or elapsed_s - self._over_since_s >= self._grace_s


class ConversationMemory:
    """Carry a bounded assistant-side continuity hint across gateway sessions.

    The duplex protocol does not expose user transcripts, so this deliberately
    avoids pretending to be a full transcript.  It retains only recent clean
    assistant text and tells the next session not to mention the transport
    rollover.
    """

    def __init__(self, max_chars: int = 800) -> None:
        self._max_chars = max_chars
        self._fragments: deque[str] = deque()
        self._chars = 0

    def append_assistant(self, text: str) -> None:
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return
        self._fragments.append(clean)
        self._chars += len(clean)
        while self._chars > self._max_chars and len(self._fragments) > 1:
            self._chars -= len(self._fragments.popleft())

    def prompt(self, base_prompt: str) -> str:
        if not self._fragments:
            return base_prompt
        tail = " ".join(self._fragments)[-self._max_chars :]
        return (
            f"{base_prompt}\n"
            "Transport continuity note: a realtime session was rotated. "
            "Continue naturally without mentioning the restart. "
            f"Your recent spoken output was: {tail}"
        )
