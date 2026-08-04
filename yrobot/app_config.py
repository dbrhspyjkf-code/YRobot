"""Persistent, non-secret settings for the Reachy Mini app dashboard.

The daemon environment remains authoritative. Dashboard values are stored in
the robot user's config directory and fill only variables that the daemon did
not provide, so managed deployments can keep controlling YRobot centrally.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from yrobot.config import DEFAULT_PERSONA, Settings, normalize_url

logger = logging.getLogger(__name__)

CONFIG_PATH_ENV = "YROBOT_CONFIG_PATH"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "yrobot" / "settings.json"

FIELD_TO_ENV = {
    "gateway_url": "YROBOT_REALTIME_URL",
    "tls_verify": "YROBOT_TLS_VERIFY",
    "video_enabled": "YROBOT_SEND_VIDEO",
    "proactive_enabled": "YROBOT_PROACTIVE",
    "persona": "YROBOT_PERSONA",
}
REQUIRED_FIELDS = frozenset(FIELD_TO_ENV)
STARTED_AT = time.monotonic()


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


def build_status(store: AppConfig, environ: Mapping[str, str]) -> dict[str, Any]:
    """Return safe runtime status for the dashboard."""
    effective = store.effective_environment(environ)
    settings = Settings.from_env(effective)
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
            "audio_uploaded_to_gateway": True,
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
) -> None:
    """Attach the small settings API consumed by ``yrobot/static``."""

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return {"settings": store.view(get_environment())}

    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        return {"ok": True, "status": build_status(store, get_environment())}

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
