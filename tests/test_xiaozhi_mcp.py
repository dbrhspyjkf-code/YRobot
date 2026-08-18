"""Tests for yrobot/xiaozhi_mcp.py — the device-side MCP server.

Red->green: these were written before the module existed. They cover the
full JSON-RPC surface (initialize / tools/list / tools/call), the volume
tool behaviour against a fake VolumeController, and error paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yrobot"))

from yrobot.xiaozhi_mcp import (  # noqa: E402
    TOOLS,
    XiaozhiMcpServer,
    handle_payload_with_tool_errors,
)


class FakeVolume:
    def __init__(self, percent: int = 50) -> None:
        self.percent = percent
        self.written: list[int] = []

    def read_percent(self) -> int:
        return self.percent

    def write_percent(self, value: int) -> int:
        value = max(0, min(100, int(value)))
        self.percent = value
        self.written.append(value)
        return value


def make(fake: FakeVolume | None = None) -> tuple[XiaozhiMcpServer, FakeVolume]:
    fake = fake or FakeVolume()
    return XiaozhiMcpServer(fake.read_percent, fake.write_percent), fake


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


def test_initialize_returns_protocol_and_capabilities():
    server, _ = make()
    reply = server.handle_payload({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert reply["id"] == 1
    result = reply["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "yrobot"


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


def test_tools_list_exposes_exactly_the_whitelist():
    server, _ = make()
    reply = server.handle_payload({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = reply["result"]["tools"]
    assert [t["name"] for t in tools] == [t["name"] for t in TOOLS]
    assert {t["name"] for t in tools} == {"set_volume", "volume_up", "volume_down"}
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


# ---------------------------------------------------------------------------
# tools/call — set_volume
# ---------------------------------------------------------------------------


def test_set_volume_writes_and_reports():
    server, fake = make(FakeVolume(20))
    reply = server.handle_payload(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "set_volume", "arguments": {"volume": 65}},
        }
    )
    assert fake.written == [65]
    text = reply["result"]["content"][0]["text"]
    assert "65" in text


def test_set_volume_clamps_out_of_range():
    server, fake = make()
    server.handle_payload(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "set_volume", "arguments": {"volume": 150}}}
    )
    assert fake.written == [100]


def test_set_volume_rejects_non_numeric():
    server, fake = make()
    reply = server.handle_payload(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "set_volume", "arguments": {"volume": "loud"}}}
    )
    assert fake.written == []
    assert "参数错误" in reply["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# tools/call — volume_up / volume_down
# ---------------------------------------------------------------------------


def test_volume_up_default_step():
    server, fake = make(FakeVolume(40))
    reply = server.handle_payload(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "volume_up", "arguments": {}}}
    )
    assert fake.written == [50]
    assert "调高" in reply["result"]["content"][0]["text"]


def test_volume_down_custom_step():
    server, fake = make(FakeVolume(40))
    reply = server.handle_payload(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "volume_down", "arguments": {"step": 25}}}
    )
    assert fake.written == [15]
    assert "调低" in reply["result"]["content"][0]["text"]


def test_volume_up_clamps_at_max():
    server, fake = make(FakeVolume(95))
    server.handle_payload(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "volume_up", "arguments": {}}}
    )
    assert fake.written == [100]


# ---------------------------------------------------------------------------
# Errors and notifications
# ---------------------------------------------------------------------------


def test_unknown_method_returns_32601():
    server, _ = make()
    reply = server.handle_payload({"jsonrpc": "2.0", "id": 9, "method": "prompts/list"})
    assert reply["error"]["code"] == -32601


def test_unknown_tool_maps_to_error_via_wrapper():
    server, _ = make()
    payload = {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
               "params": {"name": "reboot_planet", "arguments": {}}}
    reply = handle_payload_with_tool_errors(server, payload)
    assert reply["error"]["code"] == -32601


def test_notification_without_id_gets_no_reply():
    server, _ = make()
    assert server.handle_payload({"jsonrpc": "2.0", "method": "tools/list"}) is None
    assert server.handle_payload({"jsonrpc": "2.0"}) is None
    assert server.handle_payload("not-a-dict") is None  # type: ignore[arg-type]
