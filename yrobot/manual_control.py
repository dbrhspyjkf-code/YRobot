"""Short-lease manual control (plan Task 12).

The dashboard routes all head / antenna setpoints through `/api/manual-control/...`
so Choreographer stays the single writer of `set_target`. The lease model:

- A `ManualCoordinator` owns at most one active lease. A second acquire is
  rejected with `PermissionError` (the HTTP layer turns that into 409).
- The lease carries a monotonic deadline; the Choreographer's 50 Hz loop
  drops the manual target as soon as the deadline passes. Renew (heartbeat)
  / update / release commands carry the session id and are rejected if it
  does not match.
- All targets are validated for finiteness, envelope, and a per-frame rate
  budget before they reach the Choreographer. Rejected targets raise
  `ValidationError` (HTTP 422).
- The public status exposes `active`, `remaining_ms`, and the bounds — the
  session id never leaves the coordinator.

Thread safety: the coordinator holds a single lock; reads are quick so the
Choreographer's 50 Hz loop calls `is_active()` without contention in
practice.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class Limits:
    """Hard envelope for manual setpoints. Inclusive bounds."""

    roll_rad: float
    pitch_rad: float
    yaw_rad: float
    z_m: float
    antenna_rad: float

# -------- defaults ------------------------------------------------------

# Reasonable physical envelope for the Reachy Mini head + antennas. These
# mirror the Choreographer's existing `YAW_LIMIT` and antenna saturation so
# the manual path can never bypass them.
DEFAULT_LIMITS = Limits(
    roll_rad=0.6,           # ~35°
    pitch_rad=0.7,          # ~40°
    yaw_rad=math.radians(160.0),  # ±160° body envelope
    z_m=0.05,
    antenna_rad=2.0,
)

# 20 Hz client + 50 Hz server: the iPad sends at 20 Hz; the Choreographer
# enforces a per-tick delta cap so a single bad packet cannot jerk the
# servo stack. 5°/tick at 50 Hz = 250°/s, which is well above the 20 Hz
# client's reach but below the mechanical slew limit.
DEFAULT_MAX_DELTA_PER_TICK = {
    "roll": math.radians(5.0),
    "pitch": math.radians(5.0),
    "yaw": math.radians(5.0),
    "antenna": 0.2,
}

# Two-second TTL: the iPad sends a heartbeat every 500 ms during active
# control. A missed heart therefore drops the manual lease after ~4 missed
# pings, which is what gives us a hard 2 s "exit and recover" budget
# (plan §4.4).
DEFAULT_TTL_SECONDS = 2.0


# -------- dataclasses ---------------------------------------------------


@dataclass(frozen=True)
class ManualTarget:
    """One manual setpoint. All values are validated on arrival."""

    roll: float
    pitch: float
    yaw: float
    z: float
    antennas: tuple[float, float]
    session_id: str
    received_at: float = field(default_factory=time.monotonic)


# -------- errors --------------------------------------------------------


class ValidationError(ValueError):
    """422 — the manual target was malformed or out of envelope."""


# -------- session id -----------------------------------------------------


def generate_session_id() -> str:
    """Return a session id with enough entropy that guessing is impractical.

    Format: 16 hex chars from `os.urandom(8)`. A 64-bit space is more than
    enough — the dashboard never enumerates them, and the wrong-id path
    costs nothing to retry.
    """
    import secrets

    return secrets.token_hex(8)


# -------- validation ----------------------------------------------------


def validate_target(
    target: ManualTarget,
    limits: Limits = DEFAULT_LIMITS,
    previous: ManualTarget | None = None,
    max_delta: Mapping[str, float] = DEFAULT_MAX_DELTA_PER_TICK,
) -> None:
    """Raise `ValidationError` if the target is finite, in envelope, and
    within the per-tick rate limit against `previous`."""
    roll, pitch, yaw, z = target.roll, target.pitch, target.yaw, target.z
    for name, value in (("roll", roll), ("pitch", pitch), ("yaw", yaw), ("z", z)):
        if not math.isfinite(value):
            raise ValidationError(f"{name} must be finite (got {value!r})")
    if abs(roll) > limits.roll_rad + 1e-9:
        raise ValidationError(f"roll out of bounds ({roll} > ±{limits.roll_rad})")
    if abs(pitch) > limits.pitch_rad + 1e-9:
        raise ValidationError(f"pitch out of bounds ({pitch} > ±{limits.pitch_rad})")
    if abs(yaw) > limits.yaw_rad + 1e-9:
        raise ValidationError(f"yaw out of bounds ({yaw} > ±{limits.yaw_rad})")
    if abs(z) > limits.z_m + 1e-9:
        raise ValidationError(f"z out of bounds ({z} > ±{limits.z_m})")

    if len(target.antennas) != 2:
        raise ValidationError(f"antennas must have exactly 2 values (got {len(target.antennas)})")
    for value in target.antennas:
        if not math.isfinite(value):
            raise ValidationError("antenna values must be finite")
        if abs(value) > limits.antenna_rad + 1e-9:
            raise ValidationError(f"antenna value out of bounds ({value} > ±{limits.antenna_rad})")

    if previous is not None:
        if abs(roll - previous.roll) > max_delta["roll"] + 1e-9:
            raise ValidationError("roll step exceeds per-tick rate limit")
        if abs(pitch - previous.pitch) > max_delta["pitch"] + 1e-9:
            raise ValidationError("pitch step exceeds per-tick rate limit")
        # yaw wraps; compare on the shortest arc.
        dyaw = ((yaw - previous.yaw + math.pi) % (2 * math.pi)) - math.pi
        if abs(dyaw) > max_delta["yaw"] + 1e-9:
            raise ValidationError("yaw step exceeds per-tick rate limit")
        for prev, curr in zip(previous.antennas, target.antennas):
            if abs(curr - prev) > max_delta["antenna"] + 1e-9:
                raise ValidationError("antenna step exceeds per-tick rate limit")


# -------- coordinator ---------------------------------------------------


class ManualCoordinator:
    """Owns the single active manual lease. All access is thread-safe."""

    def __init__(
        self,
        limits: Limits = DEFAULT_LIMITS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._deadline_monotonic: float = 0.0
        self._target: ManualTarget | None = None
        self._previous: ManualTarget | None = None
        self._limits = limits
        self._ttl_seconds = ttl_seconds

    # ---- public status ----

    def public_status(self) -> dict[str, object]:
        """Return a snapshot safe for HTTP serialization: no session id."""
        with self._lock:
            return self._public_status_locked()

    def _public_status_locked(self) -> dict[str, object]:
        self._expire_locked()
        if self._session_id is None:
            return {
                "active": False,
                "remaining_ms": 0,
                "limits": self._limits_dict(),
            }
        remaining_ms = max(0, int((self._deadline_monotonic - time.monotonic()) * 1000))
        return {
            "active": True,
            "remaining_ms": remaining_ms,
            "limits": self._limits_dict(),
        }

    def is_active(self, session_id: str | None = None) -> bool:
        with self._lock:
            self._expire_locked()
            if self._session_id is None:
                return False
            if session_id is None:
                return True
            return self._session_id == session_id

    def current_target(self) -> ManualTarget | None:
        with self._lock:
            self._expire_locked()
            return self._target

    # ---- lease lifecycle ----

    def acquire(
        self, target: ManualTarget | None = None
    ) -> tuple[str, dict[str, object]]:
        with self._lock:
            self._expire_locked()
            if self._session_id is not None:
                raise PermissionError("another client already holds the manual lease")
            if target is not None:
                validate_target(target, self._limits, previous=self._previous)
            self._session_id = generate_session_id()
            self._deadline_monotonic = time.monotonic() + self._ttl_seconds
            if target is not None:
                self._previous = target
                self._target = target
            else:
                self._target = None
            return self._session_id, self._public_status_locked()

    def renew(self, session_id: str) -> dict[str, object]:
        with self._lock:
            self._require_locked(session_id)
            self._deadline_monotonic = time.monotonic() + self._ttl_seconds
            return self._public_status_locked()

    def heartbeat(self, session_id: str) -> dict[str, object]:
        return self.renew(session_id)

    def update(self, target: ManualTarget) -> dict[str, object]:
        with self._lock:
            self._require_locked(target.session_id)
            self._expire_locked()
            if self._session_id is None:
                raise PermissionError("manual lease has expired")
            validate_target(target, self._limits, previous=self._previous)
            self._previous = target
            self._target = target
            self._deadline_monotonic = time.monotonic() + self._ttl_seconds
            return self._public_status_locked()

    def release(self, session_id: str) -> dict[str, object]:
        with self._lock:
            if self._session_id is not None and self._session_id != session_id:
                raise PermissionError("session_id does not match the active lease")
            self._session_id = None
            self._target = None
            self._deadline_monotonic = 0.0
            return self._public_status_locked()

    # ---- internals ----

    def _require_locked(self, session_id: str) -> None:
        self._expire_locked()
        if self._session_id is None:
            raise PermissionError("no active manual lease")
        if self._session_id != session_id:
            raise PermissionError("session_id does not match the active lease")

    def _expire_locked(self) -> None:
        if self._session_id is None:
            return
        if time.monotonic() >= self._deadline_monotonic:
            self._session_id = None
            self._target = None
            self._deadline_monotonic = 0.0

    def _limits_dict(self) -> dict[str, float]:
        return {
            "roll_rad": self._limits.roll_rad,
            "pitch_rad": self._limits.pitch_rad,
            "yaw_rad": self._limits.yaw_rad,
            "z_m": self._limits.z_m,
            "antenna_rad": self._limits.antenna_rad,
        }