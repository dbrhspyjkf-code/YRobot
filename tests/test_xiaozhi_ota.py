"""Tests for yrobot.xiaozhi_ota (OTA credential client for the MQTT transport)."""

from __future__ import annotations

import json
import stat
import unittest
from unittest import mock

from yrobot.xiaozhi_ota import (
    OtaCredentialCache,
    OTAError,
    fetch_mqtt_config,
    load_or_create_client_uuid,
)


def _http_response(payload: dict, status: int = 200) -> mock.Mock:
    resp = mock.Mock()
    resp.status = status
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    return resp


OTA_OK = {
    "server_time": {"timestamp": 1787142220340},
    "mqtt": {
        "endpoint": "api.tenclass.net",
        "client_id": "GID_test@@@88_a2_9e_78_ff_3c@@@04da4e1a-de51-40ee-a9da-0b18934c5873",
        "username": "eyJpcCI6IjE3Mi4xNi43LjU0IiwiYyI6MH0=",
        "password": "x" * 44,
        "publish_topic": "device-server",
        "subscribe_topic": None,
    },
    "websocket": {"url": "wss://api.tenclass.net/xiaozhi/v1/", "token": "test-token"},
}


class TestFetchMqttConfig(unittest.TestCase):
    def test_parses_mqtt_section(self) -> None:
        with mock.patch(
            "yrobot.xiaozhi_ota.urlopen", return_value=_http_response(OTA_OK)
        ) as urlopen:
            cfg = fetch_mqtt_config("88:a2:9e:78:ff:3c", "04da4e1a-de51-40ee-a9da-0b18934c5873")

        self.assertEqual(cfg.endpoint, "api.tenclass.net")
        self.assertEqual(cfg.publish_topic, "device-server")
        self.assertTrue(cfg.client_id.startswith("GID_test@@@88_a2_9e_78_ff_3c"))
        self.assertEqual(cfg.username, OTA_OK["mqtt"]["username"])
        self.assertEqual(cfg.password, "x" * 44)

        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.tenclass.net/xiaozhi/ota/")
        self.assertEqual(req.headers.get("Device-id"), "88:a2:9e:78:ff:3c")
        self.assertEqual(
            req.headers.get("Client-id"), "04da4e1a-de51-40ee-a9da-0b18934c5873"
        )
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["mac_address"], "88:a2:9e:78:ff:3c")

    def test_http_error_raises_ota_error(self) -> None:
        with mock.patch(
            "yrobot.xiaozhi_ota.urlopen", return_value=_http_response({}, status=400)
        ):
            with self.assertRaises(OTAError):
                fetch_mqtt_config("88:a2:9e:78:ff:3c", "04da4e1a-de51-40ee-a9da-0b18934c5873")

    def test_missing_mqtt_section_raises(self) -> None:
        resp = _http_response({"firmware": {}})
        with mock.patch("yrobot.xiaozhi_ota.urlopen", return_value=resp):
            with self.assertRaises(OTAError):
                fetch_mqtt_config("88:a2:9e:78:ff:3c", "04da4e1a-de51-40ee-a9da-0b18934c5873")

    def test_redacted_repr_hides_password(self) -> None:
        with mock.patch("yrobot.xiaozhi_ota.urlopen", return_value=_http_response(OTA_OK)):
            cfg = fetch_mqtt_config("88:a2:9e:78:ff:3c", "04da4e1a-de51-40ee-a9da-0b18934c5873")
        text = repr(cfg)
        self.assertNotIn(cfg.password, text)
        self.assertNotIn(cfg.username, text)
        self.assertIn("api.tenclass.net", text)  # non-secret fields stay visible


class TestCredentialCache(unittest.TestCase):
    def test_save_load_roundtrip_and_permissions(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cache = OtaCredentialCache(Path(td) / "ota.json")
            with mock.patch("yrobot.xiaozhi_ota.urlopen", return_value=_http_response(OTA_OK)):
                cfg = fetch_mqtt_config("88:a2:9e:78:ff:3c", "04da4e1a-0000-0000-0000-000000000000")
            cache.save(cfg)

            path = Path(td) / "ota.json"
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

            loaded = cache.load()
            assert loaded is not None
            self.assertEqual(loaded.client_id, cfg.client_id)
            self.assertEqual(loaded.password, cfg.password)

    def test_load_missing_returns_none(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cache = OtaCredentialCache(Path(td) / "absent.json")
            self.assertIsNone(cache.load())

    def test_load_corrupt_returns_none(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ota.json"
            path.write_text("{ not json")
            cache = OtaCredentialCache(path)
            self.assertIsNone(cache.load())


class TestClientUuid(unittest.TestCase):
    def test_creates_and_persists(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "client_uuid"
            u1 = load_or_create_client_uuid(path)
            u2 = load_or_create_client_uuid(path)
            self.assertEqual(u1, u2)
            # must be UUID-shaped: the OTA endpoint 400s on arbitrary strings
            import uuid as _uuid

            _uuid.UUID(u1)  # raises if malformed


if __name__ == "__main__":
    unittest.main()
