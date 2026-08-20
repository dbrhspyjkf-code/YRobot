"""Tests for yrobot.xiaozhi_mqtt (MQTT control channel + UDP AES-CTR audio).

Packet/nonce layout vectors mirror xiaozhi-esp32 main/protocols/mqtt_protocol.cc:
- nonce[2:4] = payload_len (network order)   nonce[8:12] = timestamp
- nonce[12:16] = sequence                     nonce[4:8] = server ssrc kept as-is
- the first 16 bytes of each UDP datagram ARE the AES-CTR nonce.
"""

from __future__ import annotations

import json
import struct
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from yrobot.xiaozhi_mqtt import (
    PacketFormatError,
    SequenceGuard,
    UdpAudioCrypto,
    UdpChannelInfo,
    build_nonce,
    hello_request,
    is_xiaozhi_conversation_response,
)

KEY = "0123456789ABCDEF0123456789ABCDEF"          # 32 hex chars = 16 bytes
NONCE = "01DCBA09876543210EDCBA0987654321"        # 32 hex chars; byte0=0x01
# (server-issued nonces carry type=0x01 in byte 0; the C client keeps
#  nonce[0:2] and nonce[4:8] as issued, so send-side packets echo them)


def _manual_ctr_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Textbook AES-CTR keystream: ECB(counter block) chained with a full
    128-bit big-endian increment — the semantics mbedtls, OpenSSL and
    cryptography all implement. Used as an independent cross-check vector."""
    ecb = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    out = bytearray()
    counter = int.from_bytes(nonce, "big")
    while len(out) < length:
        out += ecb.update(counter.to_bytes(16, "big"))
        counter = (counter + 1) % (1 << 128)
    return bytes(out[:length])


class TestBuildNonce(unittest.TestCase):
    def test_layout_matches_c_reference(self) -> None:
        base = bytes.fromhex(NONCE)
        n = build_nonce(base, payload_len=0x0123, timestamp=0xAABBCCDD, sequence=42)
        self.assertEqual(n[0:2], base[0:2])                       # type|flags untouched
        self.assertEqual(n[2:4], struct.pack(">H", 0x0123))       # payload_len
        self.assertEqual(n[4:8], base[4:8])                       # ssrc kept from server
        self.assertEqual(n[8:12], struct.pack(">I", 0xAABBCCDD))  # timestamp
        self.assertEqual(n[12:16], struct.pack(">I", 42))         # sequence
        self.assertEqual(len(n), 16)


class TestUdpAudioCrypto(unittest.TestCase):
    def setUp(self) -> None:
        self.crypto = UdpAudioCrypto(key_hex=KEY, nonce_hex=NONCE)

    def test_roundtrip_multiblock(self) -> None:
        # >16 bytes so the CTR counter must increment across blocks.
        opus = bytes(range(256))
        pkt = self.crypto.encrypt_packet(opus, timestamp=1000, sequence=7)
        self.assertEqual(pkt[0], 0x01)
        self.assertGreater(len(pkt), 16 + len(opus) - 1)
        got, ts, seq = self.crypto.decrypt_packet(pkt)
        self.assertEqual(got, opus)
        self.assertEqual((ts, seq), (1000, 7))

    def test_ciphertext_matches_manual_ctr_vector(self) -> None:
        opus = b"hello xiaozhi udp audio!!"       # 26 bytes, crosses one block
        ts, seq = 0x01020304, 9
        pkt = self.crypto.encrypt_packet(opus, timestamp=ts, sequence=seq)
        nonce = pkt[:16]
        ks = _manual_ctr_keystream(bytes.fromhex(KEY), nonce, len(opus))
        expected = bytes(a ^ b for a, b in zip(opus, ks, strict=True))
        self.assertEqual(pkt[16:], expected)

    def test_each_packet_gets_fresh_nonce(self) -> None:
        a = self.crypto.encrypt_packet(b"payload-a", timestamp=1, sequence=1)
        b = self.crypto.encrypt_packet(b"payload-a", timestamp=1, sequence=2)
        self.assertNotEqual(a, b)

    def test_bad_type_rejected(self) -> None:
        pkt = bytearray(self.crypto.encrypt_packet(b"x", timestamp=1, sequence=1))
        pkt[0] = 0x02
        with self.assertRaises(PacketFormatError):
            self.crypto.decrypt_packet(bytes(pkt))

    def test_truncated_packet_rejected(self) -> None:
        with self.assertRaises(PacketFormatError):
            self.crypto.decrypt_packet(b"\x01" * 10)


class TestSequenceGuard(unittest.TestCase):
    def test_in_order_accepted(self) -> None:
        g = SequenceGuard()
        self.assertTrue(g.accept(1))
        self.assertTrue(g.accept(2))

    def test_replay_dropped(self) -> None:
        g = SequenceGuard()
        g.accept(5)
        self.assertFalse(g.accept(5))
        self.assertFalse(g.accept(4))

    def test_gap_accepted(self) -> None:
        g = SequenceGuard()
        g.accept(1)
        self.assertTrue(g.accept(4))  # logged as warning upstream, still taken


class TestConversationResponseClassification(unittest.TestCase):
    def test_accepts_only_stt_tts_or_udp_audio(self) -> None:
        self.assertTrue(is_xiaozhi_conversation_response(b"opus-frame"))
        self.assertTrue(is_xiaozhi_conversation_response({"type": "stt", "text": "你好"}))
        self.assertTrue(is_xiaozhi_conversation_response({"type": "tts", "state": "start"}))
        self.assertFalse(is_xiaozhi_conversation_response({"type": "mcp", "payload": {}}))
        self.assertFalse(is_xiaozhi_conversation_response({"type": "llm", "emotion": "happy"}))
        self.assertFalse(is_xiaozhi_conversation_response({"type": "hello"}))


class TestUdpChannelInfo(unittest.TestCase):
    def test_from_server_hello(self) -> None:
        hello = {
            "type": "hello",
            "version": 3,
            "session_id": "d7fd0187ab",
            "transport": "udp",
            "audio_params": {"format": "opus", "sample_rate": 24000,
                             "channels": 1, "frame_duration": 60},
            "udp": {"server": "rtc.xiaozhi.me", "port": 8811,
                    "encryption": "aes-128-ctr", "key": KEY, "nonce": NONCE},
        }
        info = UdpChannelInfo.from_hello(hello)
        assert info is not None
        self.assertEqual(info.server, "rtc.xiaozhi.me")
        self.assertEqual(info.port, 8811)

    def test_missing_udp_section_returns_none(self) -> None:
        self.assertIsNone(UdpChannelInfo.from_hello({"type": "hello"}))

    def test_repr_hides_key_material(self) -> None:
        info = UdpChannelInfo(server="rtc.xiaozhi.me", port=8811, key=KEY, nonce=NONCE)
        text = repr(info)
        self.assertNotIn(KEY, text)
        self.assertNotIn(NONCE, text)


class TestHelloRequest(unittest.TestCase):
    def test_shape(self) -> None:
        msg = json.loads(hello_request())
        self.assertEqual(msg["type"], "hello")
        self.assertEqual(msg["version"], 3)
        self.assertEqual(msg["transport"], "udp")
        self.assertTrue(msg["features"]["mcp"])
        ap = msg["audio_params"]
        self.assertEqual((ap["format"], ap["sample_rate"], ap["channels"],
                          ap["frame_duration"]), ("opus", 16000, 1, 60))


if __name__ == "__main__":
    unittest.main()
