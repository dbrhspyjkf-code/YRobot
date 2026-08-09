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

from yrobot.config import Settings
from yrobot.audio import dashboard_mic_signal, get_vad_rms_min, set_vad_rms_min
from yrobot.env_store import update_env_value
from yrobot.state import RUNTIME_HEALTH

logger = logging.getLogger(__name__)

DEFAULT_ENV_PATH = Path.home() / ".config" / "yrobot" / "ha.env"
STARTED_AT = time.monotonic()

# Populated lazily for shared callers like ``build_status``; not coupled to
# any particular request handler so the volume singleton survives reloads.
_volume_controller_instance: VolumeController | None = None
_audio_input_controller_instance: AudioInputController | None = None
_motion_controller_instance: "MotionController | None" = None


def volume_controller_singleton() -> VolumeController:
    """Lazy module-level VolumeController for shared callers like build_status."""
    global _volume_controller_instance
    if _volume_controller_instance is None:
        _volume_controller_instance = VolumeController()
    return _volume_controller_instance


class MotionController:
    """Runtime-only bridge to the active Choreographer for one-shot moves.

    The Choreographer registers itself at startup (``motion_controller.set(choreo)``);
    until then the API answers gracefully instead of crashing.
    """

    def __init__(self) -> None:
        self._choreo: Any = None
        self._recorded_provider: Any = None
        self._lock = threading.Lock()

    def set(self, choreo: Any) -> None:
        with self._lock:
            self._choreo = choreo

    def set_recorded_provider(self, provider: Any) -> None:
        with self._lock:
            self._recorded_provider = provider

    def get(self) -> Any | None:
        with self._lock:
            return self._choreo

    def play(self, name: str) -> tuple[bool, str]:
        choreo = self.get()
        if choreo is None:
            return False, "机器人动作系统尚未就绪"
        if choreo.play_move(name):
            return True, f"动作 {name} 开始播放"
        # Official recorded-emotion library.
        provider = self._recorded_provider
        recorded = provider() if callable(provider) else None
        if recorded is not None and choreo.play_recorded(name, recorded):
            return True, f"情绪 {name} 开始播放"
        # Official dances library.
        if choreo.play_dance(name):
            return True, f"舞蹈 {name} 开始播放"
        return False, f"未知动作: {name}"

    def current(self) -> str | None:
        choreo = self.get()
        if choreo is None:
            return None
        return choreo.current_move() or choreo.current_recorded()

    def status(self) -> dict[str, Any]:
        choreo = self.get()
        if choreo is None:
            return {"ready": False}
        try:
            return {"ready": True, **choreo.get_status()}
        except Exception as exc:
            logger.debug("motion status unavailable: %s", exc)
            return {"ready": False, "error": str(exc)}

    def list_moves(self) -> list[str]:
        from yrobot.motion import MOVES
        names = list(MOVES)
        provider = self._recorded_provider
        recorded = provider() if callable(provider) else None
        if recorded is not None:
            try:
                names.extend(sorted(recorded.list_moves()))
            except Exception:
                pass
        try:
            from reachy_mini_dances_library.collection.dance import AVAILABLE_MOVES
            names.extend(sorted(AVAILABLE_MOVES.keys()))
        except Exception:
            pass
        return names


def motion_controller_singleton() -> MotionController:
    global _motion_controller_instance
    if _motion_controller_instance is None:
        _motion_controller_instance = MotionController()
    return _motion_controller_instance


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
SYSTEM_POWER_ACTIONS = {
    "reboot": {
        "command": ["sudo", "-n", "systemctl", "reboot"],
        "message": "正在重启 Reachy Mini…网络会短暂断开。",
    },
    "poweroff": {
        "command": ["sudo", "-n", "systemctl", "poweroff"],
        "message": "正在关闭 Reachy Mini…关机后需要手动按电源开机。",
    },
}


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

    @staticmethod
    def _dispatch_power(command: list[str], action: str) -> None:
        try:
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 — fire-and-forget
            logger.warning("system power %s failed: %s", action, exc)

    def power(self, action: str) -> dict[str, Any]:
        spec = SYSTEM_POWER_ACTIONS.get(action)
        if spec is None:
            raise ValueError("unsupported system power action")
        threading.Thread(
            target=self._dispatch_power,
            args=(spec["command"], action),
            name=f"yrobot-system-power-{action}",
            daemon=True,
        ).start()
        return {"ok": True, "action": action, "message": spec["message"]}


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

    def read(
        self,
        lines: int,
        min_level: str,
        *,
        filter_kind: str = "",
    ) -> tuple[list[dict[str, Any]], str | None]:
        lines = max(1, min(self.MAX_LINES, int(lines)))
        min_priority = self._priority_for(min_level)
        # "chat" filter: keep only Xiaozhi conversation lines (STT + TTS text).
        chat_markers = ("xz stt:", "xz tts text:")
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
            message = str(record.get("MESSAGE") or "")
            if filter_kind == "chat" and not any(m in message for m in chat_markers):
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
                    "message": message,
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


def _robot_state_read() -> str:
    """Read the live robot state from yrobot.state (lazy import).

    State can be 'active' / 'sleeping' / 'deep_sleep' / 'safe_mode'.
    ``yrobot.main`` writes here, ``build_status`` reads. Lazy-imported so
    dashboard requests stay lightweight and don't pull in the Reachy stack
    on every poll.
    """
    try:
        from yrobot.state import ROBOT_STATE

        return ROBOT_STATE.current
    except Exception:  # noqa: BLE001 — dashboard must not crash on import glitch
        return "unknown"


def _read_system_metrics() -> dict[str, Any]:
    """Read lightweight system metrics (CPU, memory, disk, temperature)."""
    try:
        with open("/proc/loadavg") as f:
            load = f.read().split()
            cpu_pct = float(load[0]) / os.cpu_count() * 100 if os.cpu_count() else 0
    except Exception:
        cpu_pct = 0.0
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    mem["total_kb"] = int(line.split()[1])
                elif "MemAvailable" in line:
                    mem["available_kb"] = int(line.split()[1])
                if len(mem) == 2:
                    break
        mem_used_pct = (1 - mem.get("available_kb", 0) / max(mem.get("total_kb", 1), 1)) * 100
    except Exception:
        mem_used_pct = 0.0
    try:
        stat = os.statvfs("/")
        disk_pct = (1 - stat.f_bavail / max(stat.f_blocks, 1)) * 100
    except Exception:
        disk_pct = 0.0
    temp_c = 0.0
    try:
        for p in ["/sys/class/thermal/thermal_zone0/temp",
                  "/sys/class/thermal/thermal_zone1/temp"]:
            try:
                with open(p) as f:
                    temp_c = float(f.read().strip()) / 1000.0
                    break
            except Exception:
                continue
    except Exception:
        pass
    return {
        "cpu_percent": round(cpu_pct, 1),
        "memory_percent": round(mem_used_pct, 1),
        "disk_percent": round(disk_pct, 1),
        "temperature_c": round(temp_c, 1),
    }

def build_status(
    environ: Mapping[str, str],
    audio_input_controller: AudioInputController | None = None,
) -> dict[str, Any]:
    """Return safe runtime status for the dashboard."""
    settings = Settings.from_env(environ)
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
    xiaozhi_url = os.environ.get("XIAOZHI_CONV_URL", "wss://api.tenclass.net/xiaozhi/v1/")
    return {
        "service": {
            "name": "YRobot",
            "state": _robot_state_read(),
            "pid": os.getpid(),
            "uptime_s": max(0, int(time.monotonic() - STARTED_AT)),
        },
        "system": _read_system_metrics(),
        "motion": motion_controller_singleton().status(),
        "runtime": RUNTIME_HEALTH.snapshot(),
        "conversation": {
            "gateway_url": xiaozhi_url,
            "realtime_mode": "audio",
            "tls_verify": xiaozhi_url.startswith("wss://"),
            "video_enabled": False,
            "proactive_enabled": False,
            "backend": "xiaozhi",
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
            "path": "N/A",
            "environment_overrides": [],
        },
    }


def register_settings_routes(
    app: FastAPI,
    media_holder: _MediaHolder | None = None,
    audio_input_controller: AudioInputController | None = None,
    vad_env_path: Path = DEFAULT_ENV_PATH,
) -> None:
    """Attach dashboard API routes consumed by ``yrobot/static``."""

    camera = CameraStreamer(media_holder or _MediaHolder())
    audio_input = audio_input_controller or audio_input_controller_singleton()

    motion = motion_controller_singleton()

    @app.get("/api/motion")
    def get_motion() -> dict[str, Any]:
        return {"ok": True, "moves": motion.list_moves(), "current": motion.current()}

    @app.post("/api/motion")
    def post_motion(document: dict[str, Any]) -> dict[str, Any]:
        name = str(document.get("move") or document.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="missing 'move'")
        ok, msg = motion.play(name)
        if not ok:
            raise HTTPException(status_code=422, detail=msg)
        return {"ok": True, "message": msg, "current": motion.current()}

    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        return {
            "ok": True,
            "status": build_status(os.environ, audio_input_controller=audio_input),
        }

    volume_controller = VolumeController()

    system_controller = SystemController()

    @app.get("/api/system/state")
    def get_system_state() -> dict[str, Any]:
        return {"state": system_controller.state()}

    @app.post("/api/system/restart")
    def post_system_restart() -> dict[str, Any]:
        return system_controller.restart()

    @app.post("/api/system/power")
    def post_system_power(document: dict[str, Any]) -> dict[str, Any]:
        action = document.get("action")
        if action not in SYSTEM_POWER_ACTIONS:
            raise HTTPException(status_code=422, detail="action must be 'reboot' or 'poweroff'")
        return system_controller.power(str(action))

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
            update_env_value(vad_env_path, "YROBOT_VAD_RMS_MIN", f"{applied:.3f}")
        except (OSError, ValueError) as exc:
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
    def get_logs(
        lines: int = LogReader.DEFAULT_LINES,
        min_level: str = "info",
        filter: str = "",
    ) -> dict[str, Any]:
        if not available:
            raise HTTPException(status_code=503, detail=journal_error or "journal unavailable")
        entries, error = log_reader.read(lines, min_level, filter_kind=filter)
        if error is not None:
            raise HTTPException(status_code=503, detail=error)
        return {
            "unit": JOURNAL_UNIT,
            "level": min_level,
            "filter": filter,
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
