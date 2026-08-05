"""Persistent, non-secret settings for the Reachy Mini app dashboard.

The daemon environment remains authoritative. Dashboard values are stored in
the robot user's config directory and fill only variables that the daemon did
not provide, so managed deployments can keep controlling YRobot centrally.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Response

from yrobot.config import DEFAULT_PERSONA, Settings, normalize_url
from yrobot.audio import dashboard_mic_signal, get_vad_rms_min, set_vad_rms_min

logger = logging.getLogger(__name__)

CONFIG_PATH_ENV = "YROBOT_CONFIG_PATH"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "yrobot" / "settings.json"
DEFAULT_ENV_PATH = Path.home() / ".config" / "yrobot" / "ha.env"

FIELD_TO_ENV = {
    "gateway_url": "YROBOT_REALTIME_URL",
    "tls_verify": "YROBOT_TLS_VERIFY",
    "video_enabled": "YROBOT_SEND_VIDEO",
    "proactive_enabled": "YROBOT_PROACTIVE",
    "persona": "YROBOT_PERSONA",
}
REQUIRED_FIELDS = frozenset(FIELD_TO_ENV)
STARTED_AT = time.monotonic()

# Populated lazily for shared callers like ``build_status``; not coupled to
# any particular request handler so the volume singleton survives reloads.
_volume_controller_instance: VolumeController | None = None
_audio_input_controller_instance: AudioInputController | None = None


def volume_controller_singleton() -> VolumeController:
    """Lazy module-level VolumeController for shared callers like build_status."""
    global _volume_controller_instance
    if _volume_controller_instance is None:
        _volume_controller_instance = VolumeController()
    return _volume_controller_instance


class AudioInputController:
    """Runtime-only gate for uploading microphone audio to the model."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = bool(enabled)
        self._lock = threading.Lock()

    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._enabled = bool(enabled)
            return self._enabled

    def state(self) -> dict[str, Any]:
        return {"enabled": self.enabled()}


def audio_input_controller_singleton() -> AudioInputController:
    global _audio_input_controller_instance
    if _audio_input_controller_instance is None:
        _audio_input_controller_instance = AudioInputController()
    return _audio_input_controller_instance


def persist_env_value(path: Path, key: str, value: str) -> None:
    path = path.expanduser()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    updated = False
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            out.append(line)
            continue
        current_key = line.split("=", 1)[0].strip()
        if current_key == key:
            out.append(f"{key}={value}")
            updated = True
        else:
            out.append(line)
    if not updated:
        out.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write("\n".join(out) + "\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def _require_bool(document: Mapping[str, Any], name: str) -> bool:
    value = document.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def validate_document(document: Mapping[str, Any]) -> dict[str, str | bool]:
    """Normalize one complete settings form into its persisted representation."""
    if set(document) != REQUIRED_FIELDS:
        missing = sorted(REQUIRED_FIELDS - set(document))
        unknown = sorted(set(document) - REQUIRED_FIELDS)
        detail = []
        if missing:
            detail.append(f"missing: {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown: {', '.join(unknown)}")
        raise ValueError("invalid settings fields (" + "; ".join(detail) + ")")

    raw_url = document.get("gateway_url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("gateway_url must be a non-empty string")
    video = _require_bool(document, "video_enabled")
    tls_verify = _require_bool(document, "tls_verify")
    proactive = _require_bool(document, "proactive_enabled")

    persona = document.get("persona")
    if not isinstance(persona, str):
        raise ValueError("persona must be a string")
    persona = persona.strip()
    if "\n" in persona or "\r" in persona:
        raise ValueError("persona must be a single line")
    if len(persona) > 240:
        raise ValueError("persona must be at most 240 characters")

    url = normalize_url(raw_url.strip(), mode="video" if video else "audio")
    settings_env = {
        "YROBOT_REALTIME_URL": url,
        "YROBOT_TLS_VERIFY": "1" if tls_verify else "0",
        "YROBOT_SEND_VIDEO": "1" if video else "0",
        "YROBOT_PROACTIVE": "1" if proactive and video else "0",
        "YROBOT_PERSONA": persona,
    }
    # Exercise the same cross-field validation used by the actual app.
    Settings.from_env(settings_env)
    return {
        "gateway_url": url,
        "tls_verify": tls_verify,
        "video_enabled": video,
        "proactive_enabled": proactive and video,
        "persona": persona,
    }


class AppConfig:
    """Read and atomically persist dashboard-editable YRobot settings."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get(CONFIG_PATH_ENV)
        self.path = Path(configured).expanduser() if path is None and configured else path
        if self.path is None:
            self.path = DEFAULT_CONFIG_PATH

    def load(self) -> dict[str, str | bool]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings root must be an object")
            return validate_document(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("ignoring invalid settings file %s: %s", self.path, exc)
            return {}

    @staticmethod
    def as_environment(document: Mapping[str, str | bool]) -> dict[str, str]:
        if not document:
            return {}
        return {
            "YROBOT_REALTIME_URL": str(document["gateway_url"]),
            "YROBOT_TLS_VERIFY": "1" if document["tls_verify"] else "0",
            "YROBOT_SEND_VIDEO": "1" if document["video_enabled"] else "0",
            "YROBOT_PROACTIVE": "1" if document["proactive_enabled"] else "0",
            "YROBOT_PERSONA": str(document["persona"]),
        }

    def effective_environment(self, environ: Mapping[str, str]) -> dict[str, str]:
        """Merge persisted values below the daemon/process environment."""
        merged = self.as_environment(self.load())
        merged.update(environ)
        return merged

    def view(self, environ: Mapping[str, str]) -> dict[str, Any]:
        effective = self.effective_environment(environ)
        settings = Settings.from_env(effective)
        persona = effective.get("YROBOT_PERSONA", DEFAULT_PERSONA).strip()
        overrides = [field for field, env_name in FIELD_TO_ENV.items() if env_name in environ]
        return {
            "gateway_url": settings.url,
            "tls_verify": settings.tls_verify,
            "video_enabled": settings.send_video,
            "proactive_enabled": settings.proactive_enabled,
            "persona": persona,
            "environment_overrides": overrides,
            "config_path": str(self.path),
        }

    def save(self, document: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
        normalized = validate_document(document)
        # Validate the saved values on their own even when daemon variables
        # will take precedence at the next launch.
        validation_env = dict(environ)
        validation_env.update(self.as_environment(normalized))
        Settings.from_env(validation_env)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            delete=False,
        ) as handle:
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(self.path)
        return self.view(environ)


class VolumeController:
    """Read/write the Reachy Mini speaker volume via ALSA ``amixer``.

    Volume is exposed to the dashboard as percent (0–100); ``amixer`` reports
    and accepts percent natively so no manual range mapping is needed.
    Adjustments are runtime-only — they do not persist across restarts so
    daemon-managed deployments keep their baseline level.
    """

    PCM_CONTROL = "PCM"
    MIN_PERCENT = 0
    MAX_PERCENT = 100

    def __init__(self, card: int = 0, control: str = PCM_CONTROL) -> None:
        self._card = card
        self._control = control
        self._range: tuple[int, int] | None = None
        self._lock = threading.Lock()

    @property
    def range(self) -> tuple[int, int]:
        if self._range is None:
            self._range = self._query_range()
        return self._range

    def _query_range(self) -> tuple[int, int]:
        proc = subprocess.run(
            ["amixer", "-c", str(self._card), "sget", self._control],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"amixer sget failed (rc={proc.returncode}): {proc.stderr.strip()}"
            )
        match = re.search(r"Limits:\s*Playback\s+(\d+)\s*-\s*(\d+)", proc.stdout)
        if match is None:
            raise RuntimeError(
                f"could not parse amixer limits from output:\n{proc.stdout}"
            )
        return int(match.group(1)), int(match.group(2))

    def _clamp(self, percent: int) -> int:
        return max(self.MIN_PERCENT, min(self.MAX_PERCENT, int(percent)))

    def read_percent(self) -> int:
        with self._lock:
            proc = subprocess.run(
                ["amixer", "-c", str(self._card), "sget", self._control],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"amixer sget failed (rc={proc.returncode}): {proc.stderr.strip()}"
                )
            # ``Playback <raw> [<percent>%]`` carries the current level; the
            # ``Limits: Playback <low> - <high>`` header would otherwise be
            # matched by a naive ``Playback <digits>`` search.
            match = re.search(r"Playback\s+\d+\s+\[(\d+)%\]", proc.stdout)
            if match is None:
                raise RuntimeError(
                    f"could not parse amixer level from output:\n{proc.stdout}"
                )
            return int(match.group(1))

    def write_percent(self, percent: int) -> int:
        target = self._clamp(percent)
        with self._lock:
            proc = subprocess.run(
                ["amixer", "-c", str(self._card), "sset", self._control, f"{target}%"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"amixer sset failed (rc={proc.returncode}): {proc.stderr.strip()}"
                )
        return self.read_percent()


CAMERA_LONG_EDGE = 640
CAMERA_JPEG_QUALITY = 70
CAMERA_INTERVAL_S = 0.5
SYSTEM_SERVICE_NAME = "yrobot.service"


class SystemController:
    """Operate the systemd unit that runs this dashboard.

    The dashboard itself runs inside the unit it controls, so a stop request
    tears the dashboard down with it — callers must communicate that to the
    user before issuing the request. The action is dispatched on a daemon
    thread so the FastAPI response can be flushed before systemd sends the
    process SIGTERM; otherwise the browser sees a 503 / empty reply.
    """

    def __init__(self, service: str = SYSTEM_SERVICE_NAME) -> None:
        self._service = service

    def state(self) -> dict[str, Any]:
        return {
            "service": self._service,
            "running": True,
            "pid": os.getpid(),
            "uptime_s": max(0, int(time.monotonic() - STARTED_AT)),
        }

    @staticmethod
    def _dispatch(action: str, service: str) -> None:
        """Run ``sudo systemctl <action> <service>`` on a daemon thread."""
        try:
            subprocess.run(
                ["sudo", "-n", "systemctl", action, service],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 — fire-and-forget
            logger.warning("systemctl %s %s failed: %s", action, service, exc)

    def _schedule(self, action: str) -> None:
        threading.Thread(
            target=self._dispatch,
            args=(action, self._service),
            name=f"yrobot-systemctl-{action}",
            daemon=True,
        ).start()

    def restart(self) -> dict[str, Any]:
        self._schedule("restart")
        return {
            "ok": True,
            "action": "restart",
            "message": "正在重启 YRobot…几秒后页面会自动重新连接。",
        }


class _MediaHolder:
    """Late-binding reference to the Reachy Mini media object.

    The media instance is created inside ``ReachyMiniApp.run``, so the
    dashboard router needs an indirection that ``Yrobot`` can fill in once
    the robot is connected.
    """

    def __init__(self) -> None:
        self.media: Any = None


class CameraStreamer:
    """On-demand, latest-only camera capture for the dashboard.

    The worker thread only runs while ``running`` is true, so toggling the
    preview off fully releases CPU and stops the camera from being polled.
    The latest JPEG is cached and returned by ``/api/camera/frame``; nothing
    is buffered, so an idle dashboard never accumulates backpressure.
    """

    def __init__(self, media_holder: _MediaHolder) -> None:
        self._media_holder = media_holder
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: bytes | None = None
        self._stats = {
            "captured": 0,
            "failures": 0,
            "last_frame_at": None,
            "started_at": None,
        }

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def state(self) -> dict[str, Any]:
        with self._lock:
            stats = dict(self._stats)
        return {
            "running": self.running,
            "interval_s": CAMERA_INTERVAL_S,
            "long_edge": CAMERA_LONG_EDGE,
            "jpeg_quality": CAMERA_JPEG_QUALITY,
            "frame_bytes": len(self._latest) if self._latest else 0,
            **stats,
        }

    def set_running(self, running: bool) -> bool:
        if running:
            return self._start()
        self._stop()
        return False

    def _start(self) -> bool:
        if self.running:
            return True
        self._stop_event.clear()
        with self._lock:
            self._stats["started_at"] = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name="yrobot-dashboard-camera",
            daemon=True,
        )
        self._thread.start()
        return True

    def _stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        with self._lock:
            self._latest = None
            self._stats["last_frame_at"] = None

    def latest(self) -> bytes | None:
        with self._lock:
            return self._latest

    def _encode(self, bgr_frame: np.ndarray) -> bytes | None:
        height, width = bgr_frame.shape[:2]
        long_edge = max(height, width)
        if long_edge > CAMERA_LONG_EDGE:
            scale = CAMERA_LONG_EDGE / long_edge
            bgr_frame = cv2.resize(
                bgr_frame,
                (max(1, round(width * scale)), max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        ok, encoded = cv2.imencode(
            ".jpg",
            bgr_frame,
            [cv2.IMWRITE_JPEG_QUALITY, CAMERA_JPEG_QUALITY],
        )
        return encoded.tobytes() if ok else None

    def _run(self) -> None:
        next_attempt = 0.0
        while not self._stop_event.wait(0.05):
            now = time.monotonic()
            if now < next_attempt:
                continue
            next_attempt = now + CAMERA_INTERVAL_S
            media = self._media_holder.media
            if media is None:
                continue
            try:
                frame = media.get_frame()
            except Exception as exc:  # noqa: BLE001 — capture is best effort
                logger.debug("dashboard camera capture raised: %s", exc)
                with self._lock:
                    self._stats["failures"] += 1
                continue
            if frame is None:
                with self._lock:
                    self._stats["failures"] += 1
                continue
            jpeg = self._encode(frame)
            if jpeg is None:
                with self._lock:
                    self._stats["failures"] += 1
                continue
            with self._lock:
                self._latest = jpeg
                self._stats["captured"] += 1
                self._stats["last_frame_at"] = time.monotonic()


JOURNAL_UNIT = "yrobot.service"
JOURNAL_LEVELS: tuple[tuple[str, int], ...] = (
    ("emerg", 0),
    ("alert", 1),
    ("crit", 2),
    ("error", 3),
    ("warning", 4),
    ("notice", 5),
    ("info", 6),
    ("debug", 7),
)
_JOURNAL_PRIORITY_NAMES = {priority: name for name, priority in JOURNAL_LEVELS}


def _format_timestamp(timestamp_us: int) -> str:
    if timestamp_us <= 0:
        return ""
    seconds = timestamp_us / 1_000_000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


class LogReader:
    """Read recent entries from ``systemd-journal`` for the YRobot unit.

    The dashboard polls this on a short interval; no caching or streaming is
    performed here because the journal itself already buffers recent entries.
    """

    DEFAULT_LINES = 200
    MAX_LINES = 2000

    def __init__(self, unit: str = JOURNAL_UNIT, timeout: float = 5.0) -> None:
        self._unit = unit
        self._timeout = timeout

    def available(self) -> tuple[bool, str | None]:
        try:
            proc = subprocess.run(
                ["journalctl", "-u", self._unit, "--no-pager", "-n", "1"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except FileNotFoundError:
            return False, "journalctl not found on PATH"
        except subprocess.TimeoutExpired:
            return False, "journalctl timed out"
        except Exception as exc:  # noqa: BLE001 — surface unexpected journal errors
            return False, str(exc)
        if proc.returncode != 0:
            return False, (proc.stderr.strip() or f"journalctl rc={proc.returncode}")
        return True, None

    def read(self, lines: int, min_level: str) -> tuple[list[dict[str, Any]], str | None]:
        lines = max(1, min(self.MAX_LINES, int(lines)))
        min_priority = self._priority_for(min_level)
        try:
            proc = subprocess.run(
                [
                    "journalctl",
                    "-u",
                    self._unit,
                    "--no-pager",
                    "-n",
                    str(lines),
                    "--output=json",
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except FileNotFoundError:
            return [], "journalctl not found on PATH"
        except subprocess.TimeoutExpired:
            return [], "journalctl timed out"
        except Exception as exc:  # noqa: BLE001 — surface unexpected journal errors
            return [], str(exc)
        if proc.returncode != 0:
            return [], (proc.stderr.strip() or f"journalctl rc={proc.returncode}")

        entries: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            priority = self._coerce_priority(record.get("PRIORITY"))
            if priority > min_priority:
                continue
            timestamp_us = self._coerce_int(record.get("__REALTIME_TIMESTAMP"))
            entries.append(
                {
                    "timestamp_us": timestamp_us,
                    "level": _JOURNAL_PRIORITY_NAMES.get(priority, "info"),
                    "logger": str(record.get("SYSLOG_IDENTIFIER") or ""),
                    "pid": self._coerce_int(record.get("_PID")),
                    "message": str(record.get("MESSAGE") or ""),
                }
            )
        entries.sort(key=lambda entry: entry["timestamp_us"])
        return entries, None

    @staticmethod
    def _priority_for(level: str) -> int:
        name = (level or "info").lower()
        for key, priority in JOURNAL_LEVELS:
            if key == name:
                return priority
        # unknown -> accept everything at or below INFO
        return 6

    @staticmethod
    def _coerce_priority(value: Any) -> int:
        try:
            return max(0, min(7, int(value)))
        except (TypeError, ValueError):
            return 6

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


def build_status(
    store: AppConfig,
    environ: Mapping[str, str],
    audio_input_controller: AudioInputController | None = None,
) -> dict[str, Any]:
    """Return safe runtime status for the dashboard."""
    effective = store.effective_environment(environ)
    settings = Settings.from_env(effective)
    audio_input = audio_input_controller or audio_input_controller_singleton()
    input_enabled = audio_input.enabled()
    volume_percent: int | None = None
    volume_error: str | None = None
    try:
        volume_percent = volume_controller_singleton().read_percent()
    except Exception as exc:  # noqa: BLE001 — volume is best-effort status
        volume_error = str(exc)
    mic_state = dashboard_mic_signal()
    mic_state["available"] = mic_state.get("updated_at", 0.0) > 0.0
    return {
        "service": {
            "name": "YRobot",
            "state": "running",
            "pid": os.getpid(),
            "uptime_s": max(0, int(time.monotonic() - STARTED_AT)),
        },
        "conversation": {
            "gateway_url": settings.url,
            "realtime_mode": settings.realtime_mode,
            "tls_verify": settings.tls_verify,
            "video_enabled": settings.send_video,
            "proactive_enabled": settings.proactive_enabled,
        },
        "audio": {
            "volume_percent": volume_percent,
            "control": VolumeController.PCM_CONTROL,
            "range": list(volume_controller_singleton().range),
            "volume_error": volume_error,
            "mic": mic_state,
            "input_enabled": input_enabled,
        },
        "integrations": {
            "home_assistant": {
                "enabled": settings.ha_enabled,
                "configured": bool(settings.ha_url and settings.ha_token),
                "url": settings.ha_url,
                "whitelist_path": settings.ha_whitelist_path,
            },
            "hermes_tools": {
                "enabled": settings.hermes_tools_enabled,
                "url": settings.hermes_tools_url,
            },
            "local_info": {
                "enabled": settings.local_info_enabled,
            },
        },
        "privacy": {
            "audio_uploaded_to_gateway": input_enabled,
            "video_uploaded_to_gateway": settings.send_video,
            "local_media_recording": False,
        },
        "config": {
            "path": str(store.path),
            "environment_overrides": [
                field for field, env_name in FIELD_TO_ENV.items() if env_name in environ
            ],
        },
    }


def register_settings_routes(
    app: FastAPI,
    store: AppConfig,
    get_environment: Callable[[], Mapping[str, str]] = lambda: os.environ,
    media_holder: _MediaHolder | None = None,
    audio_input_controller: AudioInputController | None = None,
    vad_env_path: Path = DEFAULT_ENV_PATH,
) -> None:
    """Attach the small settings API consumed by ``yrobot/static``."""

    camera = CameraStreamer(media_holder or _MediaHolder())
    audio_input = audio_input_controller or audio_input_controller_singleton()

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return {"settings": store.view(get_environment())}

    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        return {
            "ok": True,
            "status": build_status(store, get_environment(), audio_input_controller=audio_input),
        }

    @app.put("/api/settings")
    def put_settings(document: dict[str, Any]) -> dict[str, Any]:
        try:
            settings = store.save(document, get_environment())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "settings": settings,
            "restart_required": True,
            "message": "Settings saved. Restart YRobot to apply them.",
        }

    volume_controller = VolumeController()

    system_controller = SystemController()

    @app.get("/api/system/state")
    def get_system_state() -> dict[str, Any]:
        return {"state": system_controller.state()}

    @app.post("/api/system/restart")
    def post_system_restart() -> dict[str, Any]:
        return system_controller.restart()

    @app.get("/api/volume")
    def get_volume() -> dict[str, Any]:
        try:
            percent = volume_controller.read_percent()
        except Exception as exc:  # noqa: BLE001 — surface amixer errors to UI
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        low, high = volume_controller.range
        return {
            "volume": {
                "percent": percent,
                "control": volume_controller.PCM_CONTROL,
                "range": [low, high],
                "min_percent": volume_controller.MIN_PERCENT,
                "max_percent": volume_controller.MAX_PERCENT,
            }
        }

    @app.put("/api/volume")
    def put_volume(document: dict[str, Any]) -> dict[str, Any]:
        percent = document.get("percent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            raise HTTPException(status_code=422, detail="percent must be a number")
        try:
            applied = volume_controller.write_percent(int(percent))
        except Exception as exc:  # noqa: BLE001 — surface amixer errors to UI
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        low, high = volume_controller.range
        return {
            "volume": {
                "percent": applied,
                "control": volume_controller.PCM_CONTROL,
                "range": [low, high],
                "min_percent": volume_controller.MIN_PERCENT,
                "max_percent": volume_controller.MAX_PERCENT,
            }
        }

    @app.get("/api/audio/vad")
    def get_vad() -> dict[str, Any]:
        return {
            "vad": {
                "rms_min": get_vad_rms_min(),
                "min": 0.001,
                "max": 0.5,
                "step": 0.005,
                "unit": "RMS",
            }
        }

    @app.put("/api/audio/vad")
    def put_vad(document: dict[str, Any]) -> dict[str, Any]:
        value = document.get("rms_min")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise HTTPException(status_code=422, detail="rms_min must be a number")
        applied = set_vad_rms_min(float(value))
        try:
            persist_env_value(vad_env_path, "YROBOT_VAD_RMS_MIN", f"{applied:.3f}")
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"could not save VAD env: {exc}") from exc
        return {
            "vad": {
                "rms_min": applied,
                "min": 0.001,
                "max": 0.5,
                "step": 0.005,
                "unit": "RMS",
            }
        }

    @app.get("/api/audio/input")
    def get_audio_input() -> dict[str, Any]:
        return {"audio_input": audio_input.state()}

    @app.put("/api/audio/input")
    def put_audio_input(document: dict[str, Any]) -> dict[str, Any]:
        enabled = document.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=422, detail="enabled must be a boolean")
        audio_input.set_enabled(enabled)
        return {"audio_input": audio_input.state()}

    @app.get("/api/camera/state")
    def get_camera_state() -> dict[str, Any]:
        return {"state": camera.state()}

    @app.put("/api/camera/state")
    def put_camera_state(document: dict[str, Any]) -> dict[str, Any]:
        running = document.get("running")
        if not isinstance(running, bool):
            raise HTTPException(status_code=422, detail="running must be a boolean")
        camera.set_running(running)
        return {"state": camera.state()}

    @app.get("/api/camera/frame")
    def get_camera_frame() -> Response:
        frame = camera.latest()
        if frame is None:
            raise HTTPException(
                status_code=404,
                detail="camera preview is off or no frame yet",
            )
        return Response(
            content=frame,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    log_reader = LogReader()
    available, journal_error = log_reader.available()

    @app.get("/api/logs")
    def get_logs(lines: int = LogReader.DEFAULT_LINES, min_level: str = "info") -> dict[str, Any]:
        if not available:
            raise HTTPException(status_code=503, detail=journal_error or "journal unavailable")
        entries, error = log_reader.read(lines, min_level)
        if error is not None:
            raise HTTPException(status_code=503, detail=error)
        return {
            "unit": JOURNAL_UNIT,
            "level": min_level,
            "lines": len(entries),
            "logs": [
                {
                    "timestamp_us": entry["timestamp_us"],
                    "timestamp": _format_timestamp(entry["timestamp_us"]),
                    "level": entry["level"],
                    "logger": entry["logger"],
                    "pid": entry["pid"],
                    "message": entry["message"],
                }
                for entry in entries
            ],
        }
