"""Xiaozhi OTA credential client (MQTT transport).

The tenclass cloud issues short-lived MQTT credentials per device via the
OTA endpoint — the same flow the xiaozhi-esp32 firmware uses (ota.cc).
Because the password is dynamically signed (it embeds a timestamp and a
counter), credentials MUST be fetched fresh on demand and never hard-coded.

Security rules (immutable decision #7): credentials stay on the robot at
mode 0600, are never logged, and never reach Git/dashboard/progress docs.
The dataclass repr is redacted on purpose.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

logger = logging.getLogger(__name__)

DEFAULT_OTA_URL = os.environ.get(
    "XIAOZHI_OTA_URL", "https://api.tenclass.net/xiaozhi/ota/"
)
DEFAULT_STATE_DIR = Path(
    os.environ.get("YROBOT_STATE_DIR", "/home/pollen/.local/state/yrobot")
)


class OTAError(RuntimeError):
    """Raised when the OTA endpoint cannot be reached or returns no MQTT config."""


@dataclass(frozen=True)
class MqttConfig:
    endpoint: str
    client_id: str
    username: str
    password: str
    publish_topic: str

    def __repr__(self) -> str:  # noqa: D105 — redact secrets everywhere
        return (
            f"MqttConfig(endpoint={self.endpoint!r}, "
            f"client_id={self.client_id.split('@@@')[0]}@@@…, "
            f"username=<hidden>, password=<hidden>, "
            f"publish_topic={self.publish_topic!r})"
        )


def fetch_mqtt_config(
    device_id: str,
    client_uuid: str,
    *,
    url: str = DEFAULT_OTA_URL,
    timeout: float = 15.0,
) -> MqttConfig:
    """POST the OTA check-version request and return the MQTT credentials.

    Mirrors xiaozhi-esp32 Ota::CheckVersion: Device-Id + Client-Id headers
    (Client-Id must be UUID-shaped or the server answers 400 Invalid client
    ID) and a system-info JSON body.
    """
    body = json.dumps(
        {
            "version": 2,
            "language": "zh",
            "mac_address": device_id,
            "uuid": client_uuid,
            "chip_model_name": "esp32s3",
            "application": {
                "name": "yrobot",
                "version": "1.0.0",
                "compile_time": "2026-08-19T00:00:00Z",
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Device-Id": device_id,
            "Client-Id": client_uuid,
            "User-Agent": "yrobot/1.0.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise OTAError(f"OTA HTTP {resp.status}")
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OTAError(f"OTA HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OTAError(f"OTA unreachable: {exc}") from exc

    mqtt = payload.get("mqtt")
    if not isinstance(mqtt, dict) or not mqtt.get("endpoint"):
        raise OTAError("OTA response has no usable mqtt section")
    try:
        cfg = MqttConfig(
            endpoint=str(mqtt["endpoint"]),
            client_id=str(mqtt["client_id"]),
            username=str(mqtt["username"]),
            password=str(mqtt["password"]),
            publish_topic=str(mqtt.get("publish_topic") or "device-server"),
        )
    except KeyError as exc:
        raise OTAError(f"OTA mqtt section missing field: {exc}") from exc
    logger.info(
        "OTA ok endpoint=%s publish_topic=%s", cfg.endpoint, cfg.publish_topic
    )
    return cfg


class OtaCredentialCache:
    """Persist the last credentials so quick restarts skip one OTA round trip.

    The cache is a plain JSON file at mode 0600 under the yrobot state dir.
    Stale credentials are simply discarded by the caller (reconnect path
    re-fetches); there is no TTL guessing here.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (DEFAULT_STATE_DIR / "xiaozhi_ota.json")

    def save(self, cfg: MqttConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(
            {
                "endpoint": cfg.endpoint,
                "client_id": cfg.client_id,
                "username": cfg.username,
                "password": cfg.password,
                "publish_topic": cfg.publish_topic,
            }
        )
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(self.path, 0o600)  # cover pre-existing files
        except OSError:
            pass

    def load(self) -> MqttConfig | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return MqttConfig(
                endpoint=str(data["endpoint"]),
                client_id=str(data["client_id"]),
                username=str(data["username"]),
                password=str(data["password"]),
                publish_topic=str(data.get("publish_topic") or "device-server"),
            )
        except (OSError, ValueError, KeyError):
            return None


def load_or_create_client_uuid(path: Path | None = None) -> str:
    """Stable per-install UUID used as the OTA Client-Id header."""
    p = path or (DEFAULT_STATE_DIR / "xiaozhi_client_uuid")
    try:
        value = p.read_text(encoding="utf-8").strip()
        if value:
            _uuid.UUID(value)  # validate shape
            return value
    except (OSError, ValueError):
        pass
    value = str(_uuid.uuid4())
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
    finally:
        os.close(fd)
    return value
