"""Shared runtime state — minimal, hardware-free, imported by both main
and the dashboard.

Lives in its own module so dashboard requests (which hit ``build_status``
on every poll) don't transitively import the entire Reachy stack via
``yrobot.main``.

State is a single mutable container (``_State``) so callers always see the
latest value. Mutating it replaces the inner reference; readers do
``state.current`` and always get the live string.
"""

from __future__ import annotations

import threading
from typing import Any


class _RuntimeHealth:
    """Small thread-safe snapshot shared by runtime and dashboard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {
            "motor_ready": False,
            "ws_state": "not_started",
            "session_id": None,
            "reconnects": 0,
            "tts_active": False,
            "tts_packets": 0,
            "audio_queue": 0,
            "audio_dropped": 0,
            "last_rx_at": None,
            "last_tts_packet_at": None,
            "wake_active": False,
        }

    def update(self, **values: Any) -> None:
        with self._lock:
            self._values.update(values)

    def increment(self, key: str, amount: int = 1) -> int:
        with self._lock:
            value = int(self._values.get(key, 0)) + amount
            self._values[key] = value
            return value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._values)


RUNTIME_HEALTH = _RuntimeHealth()


class _State:
    """Single mutable cell holding the robot state.

    Values:
      ``"active"``     — uplink live, speaker may be audible, VAD running.
      ``"sleeping"``   — silence gate paused; user is away or quiet.
      ``"deep_sleep"`` — extended absence: head frozen, dashboard slowed.
      ``"safe_mode"``  — startup failed; dashboard up, conversation not running.
    """

    def __init__(self) -> None:
        self._value: str = "active"

    @property
    def current(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        """Set the state. Silently ignores unknown values to avoid typos."""
        if value in ("active", "sleeping", "deep_sleep", "safe_mode"):
            self._value = value


ROBOT_STATE: _State = _State()
