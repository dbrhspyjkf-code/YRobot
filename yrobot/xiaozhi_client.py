"""Xiaozhi 协议客户端 — 实现与 RealtimeClient 相同的接口，供 Conversation 使用。

Conversation 通过 send_chunk(audio, jpeg, ...) 发送 PCM16 音频 +
可选的 JPEG 帧。本模块将 PCM16 编码为 Opus，通过 WebSocket 发送给
Xiaozhi 云，并接收 STT / LLM / TTS 消息转换为 Delta 对象。
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

from yrobot.realtime import Delta

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 60
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 960
CHANNELS = 1

XIAOZHI_URL = os.environ.get(
    "XIAOZHI_CONV_URL", "wss://api.tenclass.net/xiaozhi/v1/"
)
XIAOZHI_TOKEN = os.environ.get("XIAOZHI_TOKEN", "test-token")

try:
    with open("/sys/class/net/wlan0/address") as f:
        XIAOZHI_DEVICE_ID = f.read().strip()
except Exception:
    XIAOZHI_DEVICE_ID = os.environ.get("XIAOZHI_DEVICE_ID", "")


class XiaozhiClient:
    """Conversation 兼容的 Xiaozhi 云客户端。

    Usage:
        client = XiaozhiClient(on_delta, on_closed)
        client.open(timeout=30)
        while ...:
            client.send_chunk(pcm16_audio, None, False, "input_001")
        client.close()
    """

    def __init__(
        self,
        on_delta: Callable[[Delta], None],
        on_closed: Callable[[str], None],
        *,
        system_prompt: str | None = None,
    ) -> None:
        self._on_delta = on_delta
        self._on_closed = on_closed
        self._system_prompt = system_prompt
        self._encoder: opuslib.Encoder | None = None
        self._decoder: opuslib.Decoder | None = None
        self._ws = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._closed_once = threading.Event()
        self._audio_buf = bytearray()
        self.session_id = ""
        self._playback_pcm: list[np.ndarray] = []

    # ── RealtimeClient 兼容接口 ───────────────────────────────────────────

    def open(self, timeout: float = 30.0) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._thread = threading.Thread(
            target=self._loop.run_until_complete,
            args=(self._connect(timeout),),
            name="xz-client",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("Xiaozhi handshake timeout")

    def send_chunk(
        self,
        audio_16k: np.ndarray,
        jpeg: bytes | None,
        force_listen: bool,
        input_id: str,
    ) -> None:
        if self._loop is None or self._ws is None:
            return
        if self._encoder is None:
            self._encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, "voip")
        pcm = audio_16k.astype(np.int16).tobytes()
        try:
            pkt = self._encoder.encode(pcm, len(pcm) // 2)
        except Exception:
            return
        self._loop.call_soon_threadsafe(
            lambda p=pkt: asyncio.ensure_future(self._ws_send(p))
        )

    def close(self) -> None:
        if self._closed_once.is_set():
            return
        self._closed_once.set()
        self._stop.set()
        if self._loop and self._ws:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._ws_close())
            )
        if self._thread:
            self._thread.join(timeout=3)

    # ── Xiaozhi 协议 ──────────────────────────────────────────────────────

    async def _connect(self, timeout: float) -> None:
        import websockets

        headers = {
            "Authorization": f"Bearer {XIAOZHI_TOKEN}",
            "Device-Id": XIAOZHI_DEVICE_ID,
            "Protocol-Version": "1",
        }
        self._ws = await websockets.connect(
            XIAOZHI_URL,
            additional_headers=headers,
            open_timeout=timeout,
            ping_interval=30,
        )
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
        await self._ws.send(json.dumps(hello))
        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        data = json.loads(raw)
        self.session_id = data.get("session_id", "")
        logger.info("xiaozhi client ready session=%s", self.session_id[:12])
        self._ready.set()
        # 启动收发循环
        await self._run_loop()

    async def _run_loop(self) -> None:
        """主循环：发送 listen → 音频 → 接收 STT/LLM/TTS。"""
        sid = self.session_id
        recv_task = asyncio.ensure_future(self._recv_loop())
        buf = bytearray()  # 积累 Conversation 发来的 PCM16 片段
        try:
            while not self._stop.is_set():
                # 收集 3 秒音频
                await self._ws.send(
                    json.dumps(
                        {
                            "session_id": sid,
                            "type": "listen",
                            "state": "start",
                            "mode": "manual",
                        }
                    )
                )
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and not self._stop.is_set():
                    # 从音频队列取数据
                    if len(self._audio_buf) >= 960 * 2:  # 960 samples × 2 bytes
                        chunk = self._audio_buf[: 960 * 2]
                        self._audio_buf = self._audio_buf[960 * 2 :]
                        await self._ws.send(chunk)
                    else:
                        await asyncio.sleep(0.05)
                await self._ws.send(
                    json.dumps(
                        {
                            "session_id": sid,
                            "type": "listen",
                            "state": "stop",
                        }
                    )
                )
                # 等待回复
                for _ in range(40):
                    if self._stop.is_set():
                        break
                    await asyncio.sleep(0.2)
        finally:
            recv_task.cancel()

    async def _recv_loop(self) -> None:
        now = time.monotonic()
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            now = time.monotonic()
            if isinstance(raw, bytes):
                # TTS Opus → 解码为 PCM16 → 伪造 audio delta
                if self._decoder is None:
                    self._decoder = opuslib.Decoder(24000, 1)
                try:
                    pcm = self._decoder.decode(raw, 1440)
                    pcm_f32 = (
                        np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                        / 32768.0
                    )
                    self._on_delta(
                        Delta(
                            kind="audio",
                            received_at=now,
                            audio=pcm_f32,
                            response_id="xz",
                        )
                    )
                except Exception:
                    pass
            else:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type", "")
                if mtype == "stt":
                    text = data.get("text", "")
                    logger.info("xiaozhi stt: %s", text)
                elif mtype == "llm":
                    text = data.get("text", "")
                    if text:
                        self._on_delta(
                            Delta(
                                kind="text",
                                received_at=now,
                                text=text,
                                response_id="xz",
                            )
                        )
                elif mtype == "tts":
                    state = data.get("state", "")
                    if state == "start":
                        pass  # TTS 音频帧很快会来
                    elif state == "stop":
                        # 伪造 utterance_end
                        self._on_delta(
                            Delta(
                                kind="listen",
                                received_at=now,
                                input_id="xz",
                            )
                        )

    async def _ws_send(self, pkt: bytes) -> None:
        if self._ws:
            await self._ws.send(pkt)

    async def _ws_close(self) -> None:
        if self._ws:
            await self._ws.close()
