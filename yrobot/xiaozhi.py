"""Xiaozhi 云对话客户端 — 接入 YRobot，替代 MiniCPM-o。

当 Settings.conversation_backend == "xiaozhi" 时，Conversation 使用本模块
连接 api.tenclass.net，走 xiaozhi hello/listen/tts 协议，获得更好的语音
对话质量。摄像头视觉通过 MCP analyze_image 工具提供。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Callable

import numpy as np
import opuslib

logger = logging.getLogger(__name__)

XIAOZHI_URL = os.environ.get(
    "XIAOZHI_CONV_URL", "wss://api.tenclass.net/xiaozhi/v1/"
)
XIAOZHI_TOKEN = os.environ.get("XIAOZHI_TOKEN", "test-token")

try:
    with open("/sys/class/net/wlan0/address") as f:
        XIAOZHI_DEVICE_ID = f.read().strip()
except Exception:
    XIAOZHI_DEVICE_ID = os.environ.get("XIAOZHI_DEVICE_ID", "")

CAPTURE_RATE = 16000
PLAYBACK_RATE = 24000
FRAME_MS = 60
CAPTURE_SAMPLES = CAPTURE_RATE * FRAME_MS // 1000  # 960
PLAYBACK_SAMPLES = PLAYBACK_RATE * FRAME_MS // 1000  # 1440
CHANNELS = 1

MAX_PLAYBACK_QUEUE = 6  # ~360 ms of TTS audio
PLAY_UNDERRUN_S = 0.04  # silence to insert when decoder starves


class _OpusEncoder:
    def __init__(self) -> None:
        self._enc = opuslib.Encoder(CAPTURE_RATE, CHANNELS, "voip")
        self._dec = opuslib.Decoder(PLAYBACK_RATE, CHANNELS)

    def encode(self, pcm16: bytes) -> bytes:
        return self._enc.encode(pcm16, CAPTURE_SAMPLES)

    def decode(self, opus_pkt: bytes) -> np.ndarray | None:
        try:
            raw = self._dec.decode(opus_pkt, PLAYBACK_SAMPLES)
            return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception:
            return None


class XiaozhiConversation:
    """Reachy ↔ Xiaozhi 云对话循环。

    音频管线：Microphone (PCM16) → Opus 编码 → WebSocket → Xiaozhi 云
            Xiaozhi 云 → TTS Opus → 解码 → Speaker
    协议：手动模式 — YRobot 控制聆听起止，Xiaozhi 云返回 stt/llm/tts。
    """

    def __init__(
        self,
        stop: threading.Event,
        *,
        read_mic: Callable[[], np.ndarray],
        play_speaker: Callable[[int, np.ndarray], None],
        mic_poller: Callable[[], Any] | None = None,
        speak_text_cb: Callable[[str], None] | None = None,
    ) -> None:
        self._stop = stop
        self._read_mic = read_mic
        self._play_speaker = play_speaker
        self._mic_poller = mic_poller
        self._speak_text_cb = speak_text_cb
        self._codec = _OpusEncoder()
        self._session_id = ""
        self._playback_queue: list[np.ndarray] = []

    # ── 公共入口 ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        """阻塞运行对话循环，直到 stop 被设置。"""
        asyncio.run(self._run())

    async def _run(self) -> None:
        import websockets

        headers = {
            "Authorization": f"Bearer {XIAOZHI_TOKEN}",
            "Device-Id": XIAOZHI_DEVICE_ID,
            "Protocol-Version": "1",
        }
        async with websockets.connect(
            XIAOZHI_URL,
            additional_headers=headers,
            open_timeout=10,
            ping_interval=30,
        ) as ws:
            if self._mic_poller:
                asyncio.create_task(self._mic_poller())
            await self._handshake(ws)
            await self._conversation_loop(ws)

    # ── 协议 ──────────────────────────────────────────────────────────────────

    async def _handshake(self, ws) -> None:
        hello = {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": CAPTURE_RATE,
                "channels": 1,
                "frame_duration": FRAME_MS,
            },
        }
        await ws.send(json.dumps(hello))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        data = json.loads(raw)
        if data.get("type") != "hello":
            raise RuntimeError(f"unexpected server hello: {data.get('type')}")
        self._session_id = data.get("session_id", "")
        logger.info(
            "xiaozhi connected session=%s server_rate=%s",
            self._session_id[:12],
            data.get("audio_params", {}).get("sample_rate", PLAYBACK_RATE),
        )

    async def _conversation_loop(self, ws) -> None:
        sid = self._session_id
        recv_task = asyncio.ensure_future(self._recv_loop(ws))
        try:
            while not self._stop.is_set():
                # 聆听 3 秒
                await ws.send(
                    json.dumps(
                        {
                            "session_id": sid,
                            "type": "listen",
                            "state": "start",
                            "mode": "manual",
                        }
                    )
                )
                for _ in range(83):  # 83 × 60ms ≈ 5s
                    if self._stop.is_set():
                        break
                    pcm = self._read_mic()
                    if pcm is None:
                        await asyncio.sleep(0.06)
                        continue
                    pkt = self._codec.encode(pcm[:CAPTURE_SAMPLES].tobytes())
                    await ws.send(pkt)
                    await asyncio.sleep(0)
                await ws.send(
                    json.dumps(
                        {
                            "session_id": sid,
                            "type": "listen",
                            "state": "stop",
                        }
                    )
                )
                # 等待服务器处理（stt → llm → tts），最长 8 秒
                for _ in range(30):
                    if self._stop.is_set():
                        break
                    if self._playback_queue:
                        await self._drain_playback()
                    await asyncio.sleep(0.2)
                    if self._playback_queue:
                        await self._drain_playback()
        finally:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass

    async def _recv_loop(self, ws) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if isinstance(raw, bytes):
                pcm = self._codec.decode(raw)
                if pcm is not None:
                    self._playback_queue.append(pcm)
                    if len(self._playback_queue) > MAX_PLAYBACK_QUEUE:
                        self._playback_queue.pop(0)
            else:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type", "")
                if mtype == "stt":
                    logger.info("xiaozhi stt: %s", data.get("text", ""))
                elif mtype == "llm":
                    text = data.get("text", "")
                    logger.info("xiaozhi llm: %s", text[:80])
                    if text and self._speak_text_cb:
                        self._speak_text_cb(text)
                elif mtype == "tts":
                    state = data.get("state", "")
                    logger.info("xiaozhi tts state=%s", state)
                    if state == "stop":
                        await self._drain_playback()
                else:
                    logger.debug("xiaozhi msg: %s", json.dumps(data, ensure_ascii=False)[:200])

    async def _drain_playback(self) -> None:
        while self._playback_queue:
            pcm = self._playback_queue.pop(0)
            if len(pcm) == 0:
                continue
            self._play_speaker(0, pcm)
            duration = len(pcm) / PLAYBACK_RATE
            if duration > 0.001:
                await asyncio.sleep(duration)
