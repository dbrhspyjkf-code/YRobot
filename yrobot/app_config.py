"""Persistent, non-secret settings for the Reachy Mini app dashboard.

The daemon environment remains authoritative. Dashboard values are stored in
the robot user's config directory and fill only variables that the daemon did
not provide, so managed deployments can keep controlling YRobot centrally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Response

from yrobot.audio import dashboard_mic_signal, get_vad_rms_min, set_vad_rms_min
from yrobot.config import QWEN_VOICES, SUPPORTED_CONVERSATION_BACKENDS, Settings
from yrobot.env_store import update_env_value
from yrobot.qwen_realtime import _model_url
from yrobot.state import RUNTIME_HEALTH

logger = logging.getLogger(__name__)

DEFAULT_ENV_PATH = Path.home() / ".config" / "yrobot" / "ha.env"
STARTED_AT = time.monotonic()

# Populated lazily for shared callers like ``build_status``; not coupled to
# any particular request handler so the volume singleton survives reloads.
_volume_controller_instance: VolumeController | None = None
_audio_input_controller_instance: AudioInputController | None = None
_motion_controller_instance: MotionController | None = None


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

    def is_manual_active(self) -> bool:
        choreo = self.get()
        if choreo is None:
            return False
        try:
            return bool(choreo.is_manual_active())
        except Exception:
            return False

    def manual_status(self) -> dict[str, Any]:
        choreo = self.get()
        if choreo is None:
            return {"active": False, "remaining_ms": 0, "limits": {}}
        try:
            return dict(choreo.manual_status() or {})
        except Exception:
            return {"active": False, "remaining_ms": 0, "limits": {}}

    def begin_manual(self, document: dict[str, Any]) -> str:
        choreo = self.get()
        if choreo is None:
            raise HTTPException(status_code=503, detail="机器人动作系统尚未就绪")
        return choreo.begin_manual(document)

    def update_manual(self, document: dict[str, Any], *, session_id: str) -> None:
        choreo = self.get()
        if choreo is None:
            raise HTTPException(status_code=503, detail="机器人动作系统尚未就绪")
        choreo.update_manual(document, session_id=session_id)

    def renew_manual(self, session_id: str) -> None:
        choreo = self.get()
        if choreo is None:
            raise HTTPException(status_code=503, detail="机器人动作系统尚未就绪")
        choreo.renew_manual(session_id)

    def end_manual(self, session_id: str) -> None:
        choreo = self.get()
        if choreo is None:
            return
        choreo.end_manual(session_id)

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
            snapshot = choreo.get_status()
        except Exception as exc:
            logger.debug("motion status unavailable: %s", exc)
            return {"ready": False, "error": str(exc)}
        manual = snapshot.pop("manual_control", None) or {}
        manual_active = bool(snapshot.pop("manual_active", False))
        snapshot["control_owner"] = "manual" if manual_active else "autonomous"
        snapshot["manual_remaining_ms"] = int(manual.get("remaining_ms", 0))
        return {"ready": True, **snapshot}

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
            raise RuntimeError(f"amixer sget failed (rc={proc.returncode}): {proc.stderr.strip()}")
        match = re.search(r"Limits:\s*Playback\s+(\d+)\s*-\s*(\d+)", proc.stdout)
        if match is None:
            raise RuntimeError(f"could not parse amixer limits from output:\n{proc.stdout}")
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
                raise RuntimeError(f"could not parse amixer level from output:\n{proc.stdout}")
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
REACHY_DAEMON_BASE_URL = "http://127.0.0.1:8000"
REACHY_DAEMON_ENDPOINTS = (
    "/api/daemon/status",
    "/api/daemon/robot-app-lock-status",
    "/api/state/doa",
)
REACHY_DAEMON_ACTIONS = {"wake", "sleep", "restart"}
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


def _fetch_reachy_daemon_json(base_url: str, path: str) -> tuple[Any | None, str | None]:
    url = f"{base_url.rstrip('/')}{path}"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except Exception as exc:  # noqa: BLE001 — dashboard status is best-effort
        return None, str(exc)


def _derive_reachy_awake(motor_mode: str | None, daemon_state: str | None) -> bool | None:
    if daemon_state is not None and daemon_state != "running":
        return False
    if motor_mode is None:
        return None
    return motor_mode in {"enabled", "gravity_compensation"}


def _derive_reachy_app_slot(state: str | None, holder: str | None) -> dict[str, Any]:
    if state == "local_app":
        return {
            "active_app": holder,
            "active_app_transport": "local",
            "remote_session_active": False,
        }
    if state == "remote_session":
        return {
            "active_app": holder,
            "active_app_transport": "webrtc",
            "remote_session_active": True,
        }
    if state == "free":
        return {
            "active_app": None,
            "active_app_transport": None,
            "remote_session_active": False,
        }
    return {
        "active_app": None,
        "active_app_transport": None,
        "remote_session_active": None,
    }


def _read_reachy_daemon_status(
    base_url: str = REACHY_DAEMON_BASE_URL,
    fetch: Callable[[str, str], tuple[Any | None, str | None]] = _fetch_reachy_daemon_json,
) -> dict[str, Any]:
    results = {path: fetch(base_url, path) for path in REACHY_DAEMON_ENDPOINTS}
    status, _ = results["/api/daemon/status"]
    app_lock, _ = results["/api/daemon/robot-app-lock-status"]
    doa, _ = results["/api/state/doa"]
    errors = {path: err for path, (_, err) in results.items() if err}
    daemon: dict[str, Any] = {
        "available": any(payload is not None for payload, _ in results.values()),
        "base_url": base_url,
        "firmware_version": None,
        "hardware_id": None,
        "robot_name": None,
        "daemon_state": None,
        "motor_mode": None,
        "awake": None,
        "app_lock_state": None,
        "active_app": None,
        "active_app_transport": None,
        "remote_session_active": None,
        "doa_angle_rad": None,
        "doa_speech_detected": None,
        "errors": errors,
    }
    if isinstance(status, dict):
        daemon_state = status.get("state")
        backend = status.get("backend_status") or {}
        motor_mode = backend.get("motor_control_mode")
        daemon.update(
            {
                "firmware_version": status.get("version"),
                "hardware_id": status.get("hardware_id"),
                "robot_name": status.get("robot_name"),
                "daemon_state": daemon_state,
                "motor_mode": motor_mode,
                "awake": _derive_reachy_awake(motor_mode, daemon_state),
            }
        )
    if isinstance(app_lock, dict):
        daemon["app_lock_state"] = app_lock.get("state")
        daemon.update(_derive_reachy_app_slot(app_lock.get("state"), app_lock.get("holder_name")))
    if isinstance(doa, dict):
        daemon["doa_angle_rad"] = doa.get("angle")
        daemon["doa_speech_detected"] = doa.get("speech_detected")
    return daemon


class ReachyDaemonController:
    """Small wrapper around the official Reachy Mini daemon lifecycle endpoints."""

    def __init__(
        self,
        base_url: str = REACHY_DAEMON_BASE_URL,
        status_reader: Callable[[], dict[str, Any]] | None = None,
        post: Callable[[str], None] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._status_reader = status_reader or (lambda: _read_reachy_daemon_status(self._base_url))
        self._post = post or self._post_path

    def _post_path(self, path: str) -> None:
        url = f"{self._base_url}{path}"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=5.0):
            pass

    def action(self, action: str) -> dict[str, Any]:
        if action not in REACHY_DAEMON_ACTIONS:
            raise ValueError("unsupported reachy daemon action")
        if action == "wake":
            if self._status_reader().get("daemon_state") == "running":
                self._post("/api/motors/set_mode/enabled")
                self._post("/api/move/play/wake_up")
            else:
                self._post("/api/daemon/start?wake_up=true")
        elif action == "sleep":
            self._post("/api/daemon/stop?goto_sleep=true")
        else:
            self._post("/api/daemon/restart")
        return {"ok": True, "action": action}


def _request_reachy_sleep_before_power() -> None:
    req = urllib.request.Request(
        f"{REACHY_DAEMON_BASE_URL}/api/daemon/stop?goto_sleep=true",
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10.0):
        pass
    time.sleep(1.0)


class SystemController:
    """Operate the systemd unit that runs this dashboard.

    The dashboard itself runs inside the unit it controls, so a stop request
    tears the dashboard down with it — callers must communicate that to the
    user before issuing the request. The action is dispatched on a daemon
    thread so the FastAPI response can be flushed before systemd sends the
    process SIGTERM; otherwise the browser sees a 503 / empty reply.
    """

    def __init__(
        self,
        service: str = SYSTEM_SERVICE_NAME,
        before_power_action: Callable[[], None] | None = _request_reachy_sleep_before_power,
    ) -> None:
        self._service = service
        self._before_power_action = before_power_action

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
    @staticmethod
    def _dispatch_power(
        command: list[str],
        action: str,
        before_power_action: Callable[[], None] | None,
    ) -> None:
        try:
            if before_power_action is not None:
                try:
                    before_power_action()
                except Exception as exc:  # noqa: BLE001 — shutdown must continue
                    logger.warning("pre-power sleep failed before %s: %s", action, exc)
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
            args=(spec["command"], action, self._before_power_action),
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
CURRENT_PROCESS_LOG_PATH = Path("/tmp/yrobot-run/yrobot.log")
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
    return datetime.fromtimestamp(seconds, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


class LogReader:
    """Read recent entries for the YRobot dashboard.

    Prefer the current detached process log when present; fall back to
    ``systemd-journal`` for service-managed deployments.
    """

    DEFAULT_LINES = 200
    MAX_LINES = 2000

    def __init__(
        self,
        unit: str = JOURNAL_UNIT,
        timeout: float = 5.0,
        log_path: Path = CURRENT_PROCESS_LOG_PATH,
    ) -> None:
        self._unit = unit
        self._timeout = timeout
        self._log_path = log_path

    def available(self) -> tuple[bool, str | None]:
        if self._log_path.exists():
            return True, None
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
        if filter_kind == "chat":
            lines = self.MAX_LINES
        min_priority = self._priority_for(min_level)
        # "chat" filter: keep only user STT and bot reply lines, for any backend.
        # Each backend writes its own logger lines; we list their markers here.
        #   XIAOZHI:  "xz stt: <text>"          (user speech-to-text)
        #             "xz tts text: <text>"     (bot reply text, before audio)
        #   QWEN:     "qwen stt: <text>"        (user speech-to-text)
        #             "qwen response: <text>"   (bot reply text)
        # Connection / audio meta lines ("xz audio packets=", "xz tts start",
        # "xiaozhi ready", "QWEN wake word detected", etc.) are intentionally
        # excluded - they describe transport, not dialogue. To add a new
        # backend, follow the "<name> stt:" / "<name> response:" or
        # "<name> tts text:" convention and append its markers here.
        chat_markers = ("qwen stt:", "qwen response:", "xz stt:", "xz tts text:")
        if self._log_path.exists():
            return self._read_file(lines, min_priority, filter_kind, chat_markers)
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

    def _read_file(
        self,
        lines: int,
        min_priority: int,
        filter_kind: str,
        chat_markers: tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], str | None]:
        try:
            recent = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()[
                -lines:
            ]
        except OSError as exc:
            return [], str(exc)
        entries: list[dict[str, Any]] = []
        for index, line in enumerate(recent):
            message = line.strip()
            if not message:
                continue
            timestamp_us = 0
            match = re.match(
                r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})\s+([A-Z])\s+([^:]+):\s+(.*)$",
                message,
            )
            if match:
                stamp, millis, level_letter, logger_name, message = match.groups()
                try:
                    timestamp_us = (
                        int(datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp() * 1_000_000)
                        + int(millis) * 1000
                    )
                except ValueError:
                    timestamp_us = index
                logger_value = logger_name
                priority = {"E": 3, "W": 4, "I": 6, "D": 7}.get(level_letter, 6)
            else:
                logger_value = "python"
                priority = 6
                timestamp_us = int(time.time() * 1_000_000) + index
            if filter_kind == "chat":
                marker = next((value for value in chat_markers if value in message), "")
                if not marker or not message.split(marker, 1)[1].strip():
                    continue
            if priority > min_priority:
                continue
            entries.append(
                {
                    "timestamp_us": timestamp_us,
                    "level": _JOURNAL_PRIORITY_NAMES.get(priority, "info"),
                    "logger": logger_value,
                    "pid": os.getpid(),
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
        for p in ["/sys/class/thermal/thermal_zone0/temp", "/sys/class/thermal/thermal_zone1/temp"]:
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
        "power": _read_pi_power_state(),
    }


def _read_pi_power_state() -> dict[str, Any]:
    """Read Raspberry Pi throttling flags from vcgencmd when available."""
    state: dict[str, Any] = {
        "available": False,
        "raw": None,
        "under_voltage": False,
        "under_voltage_seen": False,
        "throttled": False,
        "throttled_seen": False,
        "frequency_capped": False,
        "frequency_capped_seen": False,
    }
    try:
        proc = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.0,
        )
    except Exception:
        return state
    if proc.returncode != 0:
        return state
    raw = proc.stdout.strip()
    match = re.search(r"throttled=(0x[0-9a-fA-F]+|\d+)", raw)
    if match is None:
        return state
    flags = int(match.group(1), 0)
    state.update(
        {
            "available": True,
            "raw": match.group(1),
            "under_voltage": bool(flags & (1 << 0)),
            "frequency_capped": bool(flags & (1 << 1)),
            "throttled": bool(flags & (1 << 2)),
            "under_voltage_seen": bool(flags & (1 << 16)),
            "frequency_capped_seen": bool(flags & (1 << 17)),
            "throttled_seen": bool(flags & (1 << 18)),
        }
    )
    return state


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
    runtime = RUNTIME_HEALTH.snapshot()
    # The dashboard also surfaces the wake-word config so the
    # operator can confirm at a glance whether the local gate is
    # armed and what phrase the user is supposed to say. The
    # `wake_active` flag itself is updated by main.py via
    # RUNTIME_HEALTH.update(wake_active=...) on every detection.
    settings_for_runtime = Settings.from_env(environ)
    runtime.setdefault("wake_enabled", settings_for_runtime.wake_enabled)
    runtime.setdefault("wake_phrase", settings_for_runtime.wake_phrase)
    runtime.setdefault("wake_active", False)
    configured_backend = Settings.from_env(environ).conversation_backend
    running_backend = runtime.get("backend", configured_backend)
    if running_backend == "qwen":
        qwen_url = os.environ.get(
            "YROBOT_QWEN_URL",
            os.environ.get("QWEN_URL", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"),
        )
        gateway_url = qwen_url
        tls_verify = qwen_url.startswith("wss://")
    else:
        gateway_url = os.environ.get("XIAOZHI_CONV_URL", "wss://api.tenclass.net/xiaozhi/v1/")
        tls_verify = gateway_url.startswith("wss://")
    return {
        "service": {
            "name": "YRobot",
            "state": _robot_state_read(),
            "pid": os.getpid(),
            "uptime_s": max(0, int(time.monotonic() - STARTED_AT)),
        },
        "system": _read_system_metrics(),
        "daemon": _read_reachy_daemon_status(),
        "motion": motion_controller_singleton().status(),
        "runtime": runtime,
        "conversation": {
            "gateway_url": gateway_url,
            "realtime_mode": settings.realtime_mode,
            "tls_verify": tls_verify,
            "video_enabled": settings.send_video,
            "video_interval_ms": (
                int(settings.frame_period_active_s * 1000)
                if settings.send_video and settings.frame_period_active_s > 0
                else None
            ),
            "video_fps": (
                round(1.0 / settings.frame_period_active_s, 2)
                if settings.send_video and settings.frame_period_active_s > 0
                else None
            ),
            "proactive_enabled": settings.proactive_enabled,
            "configured_backend": configured_backend,
            "backend": running_backend,
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
) -> CameraStreamer:
    """Attach dashboard API routes consumed by ``yrobot/static``."""

    camera = CameraStreamer(media_holder or _MediaHolder())
    audio_input = audio_input_controller or audio_input_controller_singleton()

    motion = motion_controller_singleton()
    configured_backend = Settings.from_env(os.environ).conversation_backend
    configured_voice = Settings.from_env(os.environ).qwen_voice

    @app.get("/api/conversation/backend")
    def get_conversation_backend() -> dict[str, Any]:
        runtime = RUNTIME_HEALTH.snapshot()
        return {
            "configured_backend": configured_backend,
            "running_backend": runtime.get("backend", "xiaozhi"),
            "connection_state": runtime.get("ws_state", "not_started"),
            "error": runtime.get("last_error"),
        }

    @app.put("/api/conversation/backend")
    def put_conversation_backend(document: dict[str, Any]) -> dict[str, Any]:
        nonlocal configured_backend
        backend = document.get("backend")
        if not isinstance(backend, str) or backend not in SUPPORTED_CONVERSATION_BACKENDS:
            raise HTTPException(status_code=422, detail="backend must be 'xiaozhi' or 'qwen'")
        try:
            update_env_value(vad_env_path, "YROBOT_CONVERSATION_BACKEND", backend)
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=503, detail=f"could not save backend env: {exc}"
            ) from exc
        configured_backend = backend
        return {"configured_backend": backend, "restart_required": True}

    @app.get("/api/conversation/voice")
    def get_conversation_voice() -> dict[str, Any]:
        """QWEN voice picker: returns the configured voice and the available list."""
        return {
            "configured_voice": configured_voice,
            "available_voices": list(QWEN_VOICES),
        }

    @app.put("/api/conversation/voice")
    def put_conversation_voice(document: dict[str, Any]) -> dict[str, Any]:
        nonlocal configured_voice
        voice = document.get("voice")
        if not isinstance(voice, str) or voice not in QWEN_VOICES:
            raise HTTPException(
                status_code=422,
                detail=f"voice must be one of {', '.join(QWEN_VOICES)}",
            )
        try:
            update_env_value(vad_env_path, "YROBOT_QWEN_VOICE", voice)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=f"could not save voice env: {exc}") from exc
        configured_voice = voice
        return {"configured_voice": voice, "restart_required": True}

    @app.get("/api/conversation/video")
    def get_conversation_video() -> dict[str, Any]:
        video_settings = Settings.from_env(os.environ)
        return {
            "video_enabled": video_settings.send_video,
            "frame_period_active_s": video_settings.frame_period_active_s,
            "frame_period_idle_s": video_settings.frame_period_idle_s,
            "scene_change_threshold": video_settings.scene_change_threshold,
        }

    @app.put("/api/conversation/video")
    def put_conversation_video(document: dict[str, Any]) -> dict[str, Any]:
        enabled = document.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=422, detail="'enabled' must be a boolean")
        try:
            update_env_value(vad_env_path, "YROBOT_SEND_VIDEO", "1" if enabled else "0")
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=f"could not save video env: {exc}") from exc
        return {"video_enabled": enabled, "restart_required": True}

    # ── Face registry routes ─────────────────────────────────────────
    # We deliberately create a single FaceDB per process so the
    # YuNet model is loaded at most once. The DB itself persists
    # to ~/.config/yrobot/faces.json so registrations survive
    # restarts.
    from yrobot.faces import FaceDB

    face_db = FaceDB()

    @app.get("/api/face")
    def get_faces() -> dict[str, Any]:
        runtime = RUNTIME_HEALTH.snapshot()
        return {
            "faces": face_db.list_profiles(),
            "recognition": {
                "name": runtime.get("face_recognition_name"),
                "score": runtime.get("face_recognition_score"),
                "at": runtime.get("face_recognition_at"),
            },
        }

    @app.post("/api/face")
    async def post_face(document: dict[str, Any]) -> dict[str, Any]:
        name = (document or {}).get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=422, detail="'name' must be a non-empty string")
        camera.set_running(True)
        # Capture 10 frames spaced 200 ms apart so the user can
        # slowly turn their head and we get enough variety for
        # template matching.
        frames: list[np.ndarray] = []
        for _ in range(10):
            jpeg = camera.latest()
            if jpeg is not None:
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    frames.append(frame)
            await asyncio.sleep(0.2)
        if not frames:
            raise HTTPException(
                status_code=422,
                detail="camera returned no frames during the capture window",
            )
        try:
            sample_count = face_db.register(name, frames)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"name": name, "samples": sample_count}

    @app.delete("/api/face/{name}")
    def delete_face(name: str) -> dict[str, Any]:
        deleted = face_db.delete(name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"no face profile named {name!r}")
        return {"deleted": True, "name": name}

    @app.post("/api/conversation/voice/preview")
    async def post_voice_preview(voice: str) -> dict[str, Any]:
        """Generate a short audio sample for the given voice (no restart needed).

        Connects a short-lived DashScope realtime WebSocket session using the
        configured model, asks the model to speak a single greeting in the
        requested voice, and returns the collected PCM deltas as base64 so the
        dashboard can play them in-browser without restarting YRobot.
        """
        if voice not in QWEN_VOICES:
            raise HTTPException(
                status_code=422,
                detail=f"voice must be one of {', '.join(QWEN_VOICES)}",
            )
        settings = Settings.from_env(os.environ)
        if not settings.qwen_api_key:
            raise HTTPException(status_code=503, detail="DASHSCOPE_API_KEY not configured")
        try:
            import base64 as _b64

            import websockets  # type: ignore[import-not-found]
        except ImportError as exc:
            raise HTTPException(status_code=503, detail=f"websockets not installed: {exc}") from exc
        sample_text = "你好，这是一段试听。"
        ws_url = _model_url(settings.qwen_url, settings.qwen_model)
        session_payload = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": voice,
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "input_audio_transcription": {
                    "model": "qwen3-asr-flash-realtime",
                    "language": "zh",
                },
                "turn_detection": None,
            },
        }
        try:
            async with websockets.connect(
                ws_url,
                additional_headers={"Authorization": f"Bearer {settings.qwen_api_key}"},
                max_size=10_000_000,
            ) as ws:
                await ws.send(json.dumps(session_payload))
                await ws.send(
                    json.dumps(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": sample_text}],
                            },
                        }
                    )
                )
                await ws.send(json.dumps({"type": "response.create"}))
                audio_chunks: list[bytes] = []
                while True:
                    raw_msg = await ws.recv()
                    try:
                        msg = json.loads(raw_msg)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    evt = msg.get("type", "")
                    if evt == "response.audio.delta":
                        b64 = msg.get("delta") or ""
                        if b64:
                            audio_chunks.append(_b64.b64decode(b64))
                    elif evt == "response.done":
                        break
                    elif evt == "error":
                        err = msg.get("error") or {}
                        raise HTTPException(
                            status_code=503,
                            detail=f"DashScope error: {err.get('message', err)}",
                        )
                pcm_bytes = b"".join(audio_chunks)
                sample_rate = 24000
                return {
                    "pcm_base64": _b64.b64encode(pcm_bytes).decode("ascii"),
                    "sample_rate": sample_rate,
                    "duration_s": len(pcm_bytes) / 2 / sample_rate,
                    "voice": voice,
                    "text": sample_text,
                }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"preview failed: {type(exc).__name__}: {exc}"
            ) from exc

    @app.get("/api/motion")
    def get_motion() -> dict[str, Any]:
        return {"ok": True, "moves": motion.list_moves(), "current": motion.current()}

    @app.post("/api/motion")
    def post_motion(document: dict[str, Any]) -> dict[str, Any]:
        name = str(document.get("move") or document.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="missing 'move'")
        if motion.is_manual_active():
            raise HTTPException(
                status_code=409,
                detail="手动控制进行中，请先释放控制权 (release manual lease)",
            )
        ok, msg = motion.play(name)
        if not ok:
            raise HTTPException(status_code=422, detail=msg)
        return {"ok": True, "message": msg, "current": motion.current()}

    # ---- manual control lease (plan Task 12) ----------------------

    @app.get("/api/manual-control/session")
    def get_manual_control_session() -> dict[str, Any]:
        return {"ok": True, **motion.manual_status()}

    @app.post("/api/manual-control/session")
    def post_manual_control_session(document: dict[str, Any]) -> dict[str, Any]:
        try:
            session_id = motion.begin_manual(document or {})
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"ok": True, "session_id": session_id, **motion.manual_status()}

    @app.put("/api/manual-control/session/{session_id}/target")
    def put_manual_control_target(session_id: str, document: dict[str, Any]) -> dict[str, Any]:
        try:
            motion.update_manual(document or {}, session_id=session_id)
        except PermissionError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"ok": True, **motion.manual_status()}

    @app.put("/api/manual-control/session/{session_id}/heartbeat")
    def put_manual_control_heartbeat(session_id: str) -> dict[str, Any]:
        try:
            motion.renew_manual(session_id)
        except PermissionError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True, **motion.manual_status()}

    @app.delete("/api/manual-control/session/{session_id}")
    def delete_manual_control_session(session_id: str) -> dict[str, Any]:
        motion.end_manual(session_id)
        return {"ok": True, **motion.manual_status()}

    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        return {
            "ok": True,
            "status": build_status(os.environ, audio_input_controller=audio_input),
        }

    volume_controller = VolumeController()

    system_controller = SystemController()
    reachy_daemon_controller = ReachyDaemonController()

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

    @app.post("/api/reachy-daemon/action")
    def post_reachy_daemon_action(document: dict[str, Any]) -> dict[str, Any]:
        action = str(document.get("action") or "")
        if action not in REACHY_DAEMON_ACTIONS:
            raise HTTPException(
                status_code=422, detail="action must be 'wake', 'sleep', or 'restart'"
            )
        try:
            return reachy_daemon_controller.action(action)
        except Exception as exc:  # noqa: BLE001 — surface daemon action failures
            raise HTTPException(status_code=503, detail=str(exc)) from exc

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

    return camera
