"""Xiaozhi device-side MCP server (JSON-RPC over the xiaozhi WebSocket).

The tenclass cloud drives device tools through the xiaozhi MCP protocol —
the same channel ``control_smart_home`` uses for smart-home entities. This
module implements the device side for YRobot.

House rules (immutable decision #9): only a fixed, whitelisted tool set is
exposed. There is no generic MCP discovery — the tool registry below is the
complete surface.

Envelope (xiaozhi WebSocket frame)::

    {"session_id": "...", "type": "mcp", "payload": <JSON-RPC 2.0>}

Cloud -> device payloads are requests (``initialize`` / ``tools/list`` /
``tools/call``); the device answers with a matching-id result/error payload
that the caller wraps back into the same envelope.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "yrobot", "version": "1.0"}
DEFAULT_STEP_PERCENT = 10
MIN_VOLUME = 0
MAX_VOLUME = 100

# Fixed tool registry — the complete MCP surface exposed to the cloud.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "set_volume",
        "description": "设置机器人扬声器的音量（0-100 百分比）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "volume": {
                    "type": "integer",
                    "minimum": MIN_VOLUME,
                    "maximum": MAX_VOLUME,
                    "description": "目标音量百分比",
                },
            },
            "required": ["volume"],
        },
    },
    {
        "name": "volume_up",
        "description": "调高机器人扬声器音量。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "step": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "description": f"步进百分比（默认 {DEFAULT_STEP_PERCENT}）",
                },
            },
        },
    },
    {
        "name": "volume_down",
        "description": "调低机器人扬声器音量。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "step": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "description": f"步进百分比（默认 {DEFAULT_STEP_PERCENT}）",
                },
            },
        },
    },
]


def _clamp(value: int) -> int:
    return max(MIN_VOLUME, min(MAX_VOLUME, int(value)))


class XiaozhiMcpServer:
    """Handles JSON-RPC payloads from the xiaozhi cloud.

    ``volume_read`` / ``volume_write`` mirror ``VolumeController``:
    ``read_percent() -> int`` and ``write_percent(int) -> int`` (returns the
    applied value). Both are called from worker threads by the caller.
    """

    def __init__(
        self,
        volume_read: Callable[[], int],
        volume_write: Callable[[int], int],
    ) -> None:
        self._volume_read = volume_read
        self._volume_write = volume_write

    # ------------------------------------------------------------------
    # JSON-RPC entry point
    # ------------------------------------------------------------------

    def handle_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Process one cloud payload; return the reply payload or None.

        None is returned for notifications (no ``id``) and malformed
        payloads — the xiaozhi wire has no reply channel for those.
        """
        if not isinstance(payload, dict):
            return None
        request_id = payload.get("id")
        method = payload.get("method")
        if request_id is None or not method:
            return None

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._tools_call(payload.get("params") or {})
            else:
                return self._error(request_id, -32601, f"method not found: {method}")
        except _ToolNotFound as exc:
            return self._error(request_id, -32601, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("xiaozhi mcp: error handling %s", method)
            return self._error(request_id, -32603, f"internal error: {exc}")

        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            # JSON-RPC level error so the cloud model sees the miss.
            raise _ToolNotFound(name)

        if name == "set_volume":
            volume = arguments.get("volume")
            if not isinstance(volume, (int, float)) or isinstance(volume, bool):
                return _text_result("参数错误：volume 需要是 0-100 的整数。")
            applied = int(self._volume_write(_clamp(volume)))
            return _text_result(f"音量已设置为 {applied}%。")

        if name in ("volume_up", "volume_down"):
            step = arguments.get("step", DEFAULT_STEP_PERCENT)
            if not isinstance(step, (int, float)) or isinstance(step, bool):
                step = DEFAULT_STEP_PERCENT
            delta = _clamp(int(step)) if name == "volume_up" else -_clamp(int(step))
            current = int(self._volume_read())
            applied = int(self._volume_write(_clamp(current + delta)))
            arrow = "调高" if name == "volume_up" else "调低"
            return _text_result(f"音量已{arrow}到 {applied}%。")

        raise _ToolNotFound(name)  # pragma: no cover - registry drift guard

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


class _ToolNotFound(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"unknown tool: {name}")
        self.name = name


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def handle_payload_with_tool_errors(server: XiaozhiMcpServer, payload: dict[str, Any]):
    """Same as handle_payload but maps tool-not-found to a JSON-RPC error.

    Kept separate so handle_payload stays trivially testable.
    """
    try:
        return server.handle_payload(payload)
    except _ToolNotFound as exc:
        request_id = payload.get("id")
        if request_id is None:
            return None
        return XiaozhiMcpServer._error(request_id, -32601, str(exc))
