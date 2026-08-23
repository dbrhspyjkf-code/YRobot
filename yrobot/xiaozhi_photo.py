"""Small, dependency-free helpers for device-owned Xiaozhi photo commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol


class ClosableXiaozhiChannel(Protocol):
    """The minimal channel contract needed to interrupt a cloud turn."""

    async def close(self) -> None: ...


async def close_channel_for_local_photo(channel: ClosableXiaozhiChannel) -> None:
    """Close the active cloud channel before its model tool call can run."""
    await channel.close()


async def start_local_photo_flow(
    channel: ClosableXiaozhiChannel,
    *,
    suppress_cloud_uplink: Callable[[], None],
    notify_intent: Callable[[], bool],
    start_capture: Callable[[], None],
) -> bool:
    """Fence cloud audio, arm the stale-tool guard, then run local work.

    The local capture runs in a detached thread supplied by ``start_capture``.
    It must continue after this coroutine closes the current Xiaozhi session.
    """
    suppress_cloud_uplink()
    intent_notified = await asyncio.to_thread(notify_intent)
    start_capture()
    await close_channel_for_local_photo(channel)
    return intent_notified
