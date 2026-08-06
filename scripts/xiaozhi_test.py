#!/usr/bin/env python3
"""Xiaozhi 对话测试脚本 —— 独立运行，不动 YRobot。

用法（在 Reachy 上）：
  cd /home/pollen/YRobot
  sudo systemctl stop yrobot.service                # 停掉 YRobot 避免音频冲突
  .venv/bin/python scripts/xiaozhi_test.py          # 运行测试
  # 对着 Reachy 说话，听回复对话质量
  Ctrl+C 退出
  sudo systemctl start yrobot.service               # 恢复 YRobot
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import struct
import sys

# ── 音频依赖 ─────────────────────────────────────────────────────────────────
import opuslib
import sounddevice as sd

# ── WebSocket（尝试 websockets，失败用 websocket-client） ────────────────────
try:
    import websockets   # type: ignore[import-not-found]
    WS_AVAILABLE = "websockets"
except ImportError:
    import websocket    # type: ignore[import-not-found]
    WS_AVAILABLE = "websocket-client"


SAMPLE_RATE = 24000
CAPTURE_RATE = 16000
FRAME_MS = 60
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 1440
CAPTURE_SAMPLES = CAPTURE_RATE * FRAME_MS // 1000  # 960
CHANNELS = 1

# ── 小智云对话接入（经 OTA 激活后自动读取配置） ────────────────────────
_XIAOZHI_CONV_URL = os.environ.get("XIAOZHI_CONV_URL", "wss://api.tenclass.net/xiaozhi/v1/")
_XIAOZHI_TOKEN = os.environ.get("XIAOZHI_TOKEN", "test-token")
# 用网卡 MAC 做 Device-Id，和激活时一致
try:
    with open("/sys/class/net/wlan0/address") as f:
        _XIAOZHI_DEVICE_ID = f.read().strip()
except Exception:
    _XIAOZHI_DEVICE_ID = os.environ.get("XIAOZHI_DEVICE_ID", "")

_xiaozhi_url = _XIAOZHI_CONV_URL
if not _XIAOZHI_DEVICE_ID:
    print("❌ 无法读取网卡 MAC 地址")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("xiaozhi-test")

# ── 全局状态 ─────────────────────────────────────────────────────────────────
_quit = asyncio.Event()
_session_id = ""
_speaking = False
_t0 = 0.0


def elapsed() -> float:
    import time
    return time.monotonic() - _t0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Opus 编解码 + 播放
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_encoder: opuslib.Encoder | None = None
_decoder: opuslib.Decoder | None = None
_audio_in_queue: asyncio.Queue[bytes] | None = None  # 录音线程 → 主循环
_playback_packets: list[bytes] = []


def _handle_binary_audio(data: bytes) -> None:
    """收到一个 Opus 音频帧，立即解码并播放。"""
    try:
        pcm = _dec().decode(data, FRAME_SAMPLES)
        sd.play(pcm, SAMPLE_RATE)
    except Exception as exc:
        pass  # 丢弃损坏帧
    global _encoder
    if _encoder is None:
        _encoder = opuslib.Encoder(CAPTURE_RATE, CHANNELS, "voip")
    return _encoder


def _dec() -> opuslib.Decoder:
    global _decoder
    if _decoder is None:
        _decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
    return _decoder


def _record_thread(dev_index: int | None) -> None:
    """后台线程：持续读麦克风 → Opus 编码 → 入队。"""
    import time as _t
    queue = _audio_in_queue
    sd.default.samplerate = CAPTURE_RATE
    sd.default.channels = CHANNELS
    sd.default.dtype = "int16"
    sd.default.device = dev_index

    with sd.InputStream() as stream:
        while not _quit.is_set():
            buf, _overflowed = stream.read(CAPTURE_SAMPLES)
            pcm = buf.tobytes()
            try:
                opus_pkt = _enc().encode(pcm, CAPTURE_SAMPLES)
            except Exception:
                _t.sleep(0.01)
                continue
            if queue is not None:
                try:
                    queue.put_nowait(opus_pkt)
                except asyncio.QueueFull:
                    pass
    logger.info("录音线程已退出")




# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WebSocket 协议处理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _send(ws, msg: dict | bytes) -> None:
    if isinstance(msg, bytes):
        if WS_AVAILABLE == "websockets":
            await ws.send(msg)   # type: ignore[union-attr]
        else:
            ws.send(msg)          # type: ignore[union-attr]
    else:
        text = json.dumps(msg, ensure_ascii=False)
        if WS_AVAILABLE == "websockets":
            await ws.send(text)   # type: ignore[union-attr]
        else:
            ws.send(text)          # type: ignore[union-attr]


async def _handle_message(event: dict) -> None:
    """解析 Xiaozhi 协议消息。"""
    global _session_id, _speaking
    mtype = event.get("type", "")

    if mtype == "hello":
        _session_id = event.get("session_id", "")
        _speaking = False
        logger.info("✅ 服务器 hello, session=%s", _session_id)
        if "audio_params" in event:
            logger.info("   服务器音频: %s", event["audio_params"])

    elif mtype == "stt":
        text = event.get("text", "")
        logger.info("🎤 STT: %s", text)

    elif mtype == "llm":
        emotion = event.get("emotion", "")
        text = event.get("text", "")
        logger.info("🤖 LLM: %s %s", emotion, text)

    elif mtype == "tts":
        state = event.get("state", "")
        if state == "start":
            _speaking = True
            pass  # tts start: clear state
        elif state == "stop":
            _speaking = False
            pass  # tts stop: done
        elif state == "sentence_start":
            logger.info("🗣️ %s", event.get("text", ""))

    elif mtype == "goodbye":
        logger.info("👋 服务器 goodbye: %s", event.get("reason", ""))
        _quit.set()

    elif mtype == "mcp":
        logger.debug("MCP 消息（忽略）")


async def run_conversation(ws, mic_dev: int | None) -> None:
    """手动模式对话主循环。

    1. 发 hello → 等服务器 hello
    2. 发 listen start → 发音频帧 → 发 listen stop
    3. 收 stt/llm/tts → 播放 → 回到第 2 步
    4. 收到再见信号或 Ctrl+C → 退出
    """
    global _session_id, _speaking
    # ── 发 hello ──
    hello = {
        "type": "hello",
        "version": 1,
        "transport": "websocket",
        "audio_params": {
            "format": "opus",
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "frame_duration": FRAME_MS,
        },
    }
    await _send(ws, hello)
    logger.info("→ hello 已发送，等待服务器…")

    # ── 启动录音线程 ──
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)
    global _audio_in_queue
    _audio_in_queue = queue
    import threading as _th
    rec_thread = _th.Thread(target=_record_thread, args=(mic_dev,), daemon=True)
    rec_thread.start()

    # ── 对话循环 ──
    turn = 0
    while not _quit.is_set():
        await asyncio.sleep(0.5)
        if not _session_id:
            continue  # 等服务器 hello
        turn += 1
        logger.info("═══ 第 %d 轮对话 ═══", turn)

        # 开始录音
        await _send(ws, {
            "session_id": _session_id,
            "type": "listen",
            "state": "start",
            "mode": "manual",
        })
        # 清空队列里积压的旧音频
        while not queue.empty():
            queue.get_nowait()
        # 发送 3 秒音频
        for _ in range(50):  # 50 × 60ms = 3s
            if _quit.is_set():
                break
            try:
                pkt = await asyncio.wait_for(queue.get(), timeout=0.2)
                await _send(ws, pkt)
            except asyncio.TimeoutError:
                break
        # 停止录音
        await _send(ws, {
            "session_id": _session_id,
            "type": "listen",
            "state": "stop",
        })
        # 等待服务器处理（stt → llm → tts）
        await asyncio.sleep(3)

    rec_thread.join(timeout=1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _ws_client(url: str, mic_dev: int | None) -> None:
    """异步 WebSocket 客户端，带认证头。"""
    ws_headers = {
        "Authorization": f"Bearer {_XIAOZHI_TOKEN}",
        "Device-Id": _XIAOZHI_DEVICE_ID,
        "Protocol-Version": "1",
    }
    if WS_AVAILABLE == "websockets":
        async with websockets.connect(url, additional_headers=ws_headers) as ws:  # type: ignore[attr-defined]
            async with asyncio.TaskGroup() as tg:
                tg.create_task(run_conversation(ws, mic_dev))
                # 接收循环
                async for raw in ws:
                    if isinstance(raw, bytes):
                        _handle_binary_audio(raw)
                    else:
                        try:
                            msg = json.loads(raw)
                            await _handle_message(msg)
                        except json.JSONDecodeError:
                            pass
                    if _quit.is_set():
                        break
    else:
        # websocket-client 同步版本
        from concurrent.futures import ThreadPoolExecutor
        ws = websocket.create_connection(url)
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            # 接收 → 主线程
            def recv_loop():
                while not _quit.is_set():
                    try:
                        opcode, data = ws.recv_data()
                    except Exception:
                        break
                    if isinstance(data, bytes) and opcode == 2:
                        _handle_binary_audio(data)
                    else:
                        try:
                            msg = json.loads(data.decode())
                            asyncio.run_coroutine_threadsafe(
                                _handle_message(msg), loop
                            )
                        except Exception:
                            pass
            pool.submit(recv_loop)
            await run_conversation(ws, mic_dev)
        ws.close()


async def main() -> None:
    global _t0
    import time as _t
    _t0 = _t.monotonic()

    # 检查 Xiaozhi URL
    if not _xiaozhi_url.startswith(("ws://", "wss://")):
        print("❌ XIAOZHI_TEST_URL 不是有效的 WebSocket URL")
        sys.exit(1)

    # 选麦克风：Reachy Mini 的 PipeWire GStreamer 源
    mic_dev = None
    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            name = d.get("name", "")
            if "reachymini_audio_src" in name.lower() and d.get("max_input_channels", 0) > 0:
                mic_dev = i
                logger.info("使用设备 %d: %s", i, name)
                break
        if mic_dev is None:
            for i, d in enumerate(devices):
                if "reachy" in d.get("name", "").lower() and d.get("max_input_channels", 0) > 0:
                    mic_dev = i
                    logger.info("使用设备 %d: %s (fallback)", i, d["name"])
                    break
        if mic_dev is None:
            logger.info("未找到 Reachy 设备，使用默认")
    except Exception as exc:
        logger.warning("设备枚举失败: %s", exc)

    logger.info("连接 %s …", _xiaozhi_url[:60] + "…")
    await _ws_client(_xiaozhi_url, mic_dev)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户中断")
    finally:
        _quit.set()
        pass  # tts stop: done
