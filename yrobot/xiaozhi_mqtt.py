"""Xiaozhi MQTT + UDP transport (protocol v3, tenclass cloud).

Two channels, mirroring xiaozhi-esp32 main/protocols/mqtt_protocol.cc:

- MQTT control channel (TLS 8883): JSON messages with the same semantics
  as the legacy WebSocket protocol (hello / listen / stt / tts / llm /
  mcp / goodbye). Credentials come from the OTA endpoint (xiaozhi_ota).
- UDP audio channel: opus frames encrypted with AES-128-CTR. The first
  16 bytes of every datagram are the per-packet nonce AND the packet
  header (type|flags|payload_len|ssrc|timestamp|sequence).

The MQTT client runs on a paho network thread; inbound JSON is bridged
into the asyncio loop via call_soon_threadsafe (same pattern as the mic
reader and aplay writer threads in main.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import struct
import threading
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

_PACKET_HEADER_LEN = 16
_PACKET_TYPE_AUDIO = 0x01


class PacketFormatError(ValueError):
    """Malformed or non-audio UDP datagram."""


def is_xiaozhi_conversation_response(message: object) -> bool:
    """Whether an inbound item proves the pending audio uplink was handled.

    MQTT control messages such as MCP and LLM emotions can continue even when
    the cloud's UDP/STT path has silently stalled. Only an audio packet or a
    speech event is a valid liveness acknowledgement for an uplink burst.
    """
    if isinstance(message, bytes):
        return True
    return isinstance(message, dict) and message.get("type") in {"stt", "tts"}


def build_nonce(
    base: bytes, *, payload_len: int, timestamp: int, sequence: int
) -> bytes:
    """Per-packet AES-CTR nonce == packet header (mqtt_protocol.cc SendAudio)."""
    nonce = bytearray(base[:_PACKET_HEADER_LEN])
    struct.pack_into(">H", nonce, 2, payload_len)
    struct.pack_into(">I", nonce, 8, timestamp)
    struct.pack_into(">I", nonce, 12, sequence)
    return bytes(nonce)


class UdpAudioCrypto:
    """AES-128-CTR packet codec for the UDP audio channel."""

    def __init__(self, *, key_hex: str, nonce_hex: str) -> None:
        self._key = bytes.fromhex(key_hex)
        self._nonce_base = bytes.fromhex(nonce_hex)
        if len(self._key) != 16 or len(self._nonce_base) != 16:
            raise ValueError("key/nonce must be 16 bytes (32 hex chars)")

    def encrypt_packet(self, opus: bytes, *, timestamp: int, sequence: int) -> bytes:
        nonce = build_nonce(
            self._nonce_base,
            payload_len=len(opus),
            timestamp=timestamp,
            sequence=sequence,
        )
        enc = Cipher(algorithms.AES(self._key), modes.CTR(nonce)).encryptor()
        return nonce + enc.update(opus) + enc.finalize()

    def decrypt_packet(self, data: bytes) -> tuple[bytes, int, int]:
        """Return (opus, timestamp, sequence); raise PacketFormatError on junk."""
        if len(data) < _PACKET_HEADER_LEN + 1:
            raise PacketFormatError(f"packet too short: {len(data)}B")
        if data[0] != _PACKET_TYPE_AUDIO:
            raise PacketFormatError(f"unexpected packet type: {data[0]:#x}")
        timestamp = struct.unpack_from(">I", data, 8)[0]
        sequence = struct.unpack_from(">I", data, 12)[0]
        dec = Cipher(algorithms.AES(self._key), modes.CTR(data[:_PACKET_HEADER_LEN])).decryptor()
        opus = dec.update(data[_PACKET_HEADER_LEN:]) + dec.finalize()
        return opus, timestamp, sequence


class SequenceGuard:
    """Monotonic sequence check: replays dropped, gaps tolerated."""

    def __init__(self) -> None:
        self._last = 0

    def accept(self, sequence: int) -> bool:
        if sequence <= self._last:
            return False
        if sequence != self._last + 1 and sequence > self._last:
            logger.debug("udp seq gap: got %d after %d", sequence, self._last)
        self._last = sequence
        return True


@dataclass(frozen=True)
class UdpChannelInfo:
    server: str
    port: int
    key: str
    nonce: str

    def __repr__(self) -> str:  # noqa: D105 — key material never in logs
        return f"UdpChannelInfo(server={self.server!r}, port={self.port}, key=<hidden>)"

    @classmethod
    def from_hello(cls, hello: dict) -> UdpChannelInfo | None:
        udp = hello.get("udp")
        if not isinstance(udp, dict):
            return None
        try:
            return cls(
                server=str(udp["server"]),
                port=int(udp["port"]),
                key=str(udp["key"]),
                nonce=str(udp["nonce"]),
            )
        except (KeyError, ValueError) as exc:
            logger.warning("server hello udp section invalid: %s", exc)
            return None


def hello_request() -> str:
    """hello v3 asking for the UDP transport (matches GetHelloMessage())."""
    return json.dumps(
        {
            "type": "hello",
            "version": 3,
            "transport": "udp",
            "features": {"mcp": True},
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
    )


class XiaozhiUdpAudio:
    """UDP socket wrapper: decrypt inbound opus, encrypt outbound opus."""

    def __init__(self, info: UdpChannelInfo, *, on_packet, chunk: int = 4096) -> None:
        self._info = info
        self._crypto = UdpAudioCrypto(key_hex=info.key, nonce_hex=info.nonce)
        self._guard = SequenceGuard()
        self._on_packet = on_packet  # callable(opus: bytes, timestamp: int)
        self._sock: socket.socket | None = None
        self._send_seq = 0
        self._ts = 0
        self._reader: threading.Thread | None = None
        self._closed = threading.Event()
        self._chunk = chunk

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.5)
        self._sock.connect((self._info.server, self._info.port))
        self._reader = threading.Thread(
            target=self._read_loop, name="xz-udp-reader", daemon=True
        )
        self._reader.start()

    def send(self, opus: bytes, *, timestamp: int) -> None:
        sock = self._sock
        if sock is None or self._closed.is_set():
            return
        self._send_seq += 1
        pkt = self._crypto.encrypt_packet(
            opus, timestamp=timestamp, sequence=self._send_seq
        )
        sock.send(pkt)

    def _read_loop(self) -> None:
        while not self._closed.is_set():
            try:
                assert self._sock is not None
                data, _ = self._sock.recvfrom(self._chunk)
            except TimeoutError:
                continue
            except OSError:
                if not self._closed.is_set():
                    logger.warning("xz udp reader socket error", exc_info=True)
                break
            try:
                opus, ts, seq = self._crypto.decrypt_packet(data)
            except PacketFormatError as exc:
                logger.debug("xz udp drop: %s", exc)
                continue
            if not self._guard.accept(seq):
                continue
            try:
                self._on_packet(opus, ts)
            except Exception:  # noqa: BLE001 — reader must survive callbacks
                logger.exception("xz udp on_packet callback failed")

    def close(self) -> None:
        self._closed.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


class XiaozhiMqttTransport:
    """MQTT control channel bridged into asyncio (paho network thread)."""

    def __init__(self, config, *, loop: asyncio.AbstractEventLoop,
                 queue: asyncio.Queue | None = None) -> None:
        self._config = config  # MqttConfig from xiaozhi_ota
        self._loop = loop
        # Shared inbound queue: dicts from MQTT, bytes from the UDP reader —
        # the same type-split the legacy WS recv() loop already used.
        self._queue: asyncio.Queue = queue if queue is not None else asyncio.Queue()
        self._client = None
        self._connected = threading.Event()
        self._closed = threading.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and not self._closed.is_set()

    @property
    def queue(self) -> asyncio.Queue:
        """Shared inbound queue (dicts from MQTT, bytes from the UDP reader)."""
        return self._queue

    def start(self) -> None:
        import paho.mqtt.client as mqtt  # imported lazily: optional dep

        c = self._config
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=c.client_id,
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(c.username, c.password)
        client.tls_set_context(ssl.create_default_context())
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        host, _, port = c.endpoint.partition(":")
        client.connect(host, int(port or 8883), keepalive=60)
        client.loop_start()
        self._client = client

    def _on_connect(self, client, userdata, flags, rc, props=None) -> None:
        logger.info("xz mqtt connected rc=%s", rc)
        client.subscribe(self._config.publish_topic)
        self._connected.set()

    def _on_message(self, client, userdata, msg) -> None:
        try:
            data = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            logger.warning("xz mqtt non-json message on %s", msg.topic)
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, data)

    def wait_connected(self, timeout: float) -> bool:
        """Block (from a worker thread) until the broker accepts us."""
        return self._connected.wait(timeout)

    def publish_text(self, text: str) -> bool:
        client = self._client
        if client is None or not self.connected:
            return False
        info = client.publish(self._config.publish_topic, text)
        return info.rc == 0

    def _on_disconnect(self, client, userdata, *args) -> None:
        self._connected.clear()
        if not self._closed.is_set():
            logger.warning("xz mqtt disconnected")

    def publish_json(self, obj: dict) -> bool:
        client = self._client
        if client is None or not self.connected:
            return False
        info = client.publish(
            self._config.publish_topic, json.dumps(obj, ensure_ascii=False)
        )
        return info.rc == 0

    async def next_json(self, timeout: float | None = None) -> dict:
        if timeout is None:
            return await self._queue.get()
        return await asyncio.wait_for(self._queue.get(), timeout)

    def close(self) -> None:
        self._closed.set()
        self._connected.clear()
        client = self._client
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
