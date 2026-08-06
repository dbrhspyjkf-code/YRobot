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


class _State:
    """Single mutable cell holding the robot state.

    Values: ``"active"`` | ``"sleeping"`` | ``"safe_mode"``.
    """

    def __init__(self) -> None:
        self._value: str = "active"

    @property
    def current(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        """Set the state. Silently ignores unknown values to avoid typos."""
        if value in ("active", "sleeping", "safe_mode"):
            self._value = value


ROBOT_STATE: _State = _State()
