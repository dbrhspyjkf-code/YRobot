"""Fail-closed cloud-uplink quarantine after a device-owned photo command."""

from __future__ import annotations

import time
from collections.abc import Callable

# A local photo command closes Xiaozhi immediately, then the session reconnects
# after three seconds.  Keep the microphone fenced long enough to discard its
# residual frames rather than sending the same utterance to the new session.
LOCAL_PHOTO_CLOUD_QUARANTINE_S = 10.0


class LocalPhotoCloudQuarantine:
    """Monotonic, extend-only time fence for Xiaozhi microphone uplink."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        duration_s: float = LOCAL_PHOTO_CLOUD_QUARANTINE_S,
    ) -> None:
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        self._now = now
        self._duration_s = duration_s
        self._until = 0.0

    def arm(self) -> None:
        """Fence residual cloud uplink; repeat calls can only extend it."""
        self._until = max(self._until, self._now() + self._duration_s)

    def active(self) -> bool:
        return self._now() < self._until

    def remaining_s(self) -> float:
        return max(0.0, self._until - self._now())
