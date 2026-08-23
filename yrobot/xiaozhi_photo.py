"""Small, dependency-free helpers for device-owned Xiaozhi photo commands."""

from __future__ import annotations

from typing import Protocol


class ClosableXiaozhiChannel(Protocol):
    """The minimal channel contract needed to interrupt a cloud turn."""

    async def close(self) -> None: ...


async def close_channel_for_local_photo(channel: ClosableXiaozhiChannel) -> None:
    """Close the active cloud channel before its model tool call can run."""
    await channel.close()
