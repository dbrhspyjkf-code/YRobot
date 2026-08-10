"""Explicit, local-only Function Calling tools for Qwen Realtime."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yrobot.config import Settings

VERIFIED_HERMES_BASE = "http://192.168.1.200:8766"
MAX_RESULT_BYTES = 4096
ALLOWED_DOMAINS = frozenset({"light", "switch", "fan"})
ALLOWED_ACTIONS = frozenset({"turn_on", "turn_off", "toggle"})
BLOCKED_DOMAINS = frozenset({"lock", "cover", "alarm_control_panel", "climate", "water_heater"})
BLOCKED_TERMS = (
    "lock",
    "door",
    "garage",
    "alarm",
    "gas",
    "heater",
    "boiler",
    "stove",
    "oven",
    "锁",
    "门",
    "车库",
    "警报",
    "燃气",
    "煤气",
    "加热",
    "暖气",
    "热水器",
    "炉",
)


@dataclass(frozen=True)
class AllowedAction:
    name: str
    service: str
    entity_id: str
    service_data: dict[str, Any] | None = None


class ToolExecutor:
    """Resolve narrow model arguments through robot-local allowlists."""

    def __init__(
        self,
        settings: Settings,
        *,
        opener: Any = urllib.request.urlopen,
        timeout: float = 5.0,
    ) -> None:
        self.settings = settings
        self._opener = opener
        self._timeout = timeout
        self._actions: dict[tuple[str, str], AllowedAction] = {}
        self._devices: dict[str, AllowedAction] = {}
        self._phrases: dict[str, tuple[str, str]] = {}
        self._blocked_devices: set[str] = set()
        self._load_whitelist()

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "查询指定城市的实时天气。",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string", "description": "城市名"}},
                        "required": ["city"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_device_state",
                    "description": "读取机器人本地白名单内设备的当前状态。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "device": {"type": "string", "description": "白名单友好名称"}
                        },
                        "required": ["device"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "control_allowed_device",
                    "description": "控制机器人本地白名单内的低风险设备。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "device": {"type": "string", "description": "白名单友好名称"},
                            "action": {
                                "type": "string",
                                "enum": ["turn_on", "turn_off", "toggle"],
                            },
                        },
                        "required": ["device", "action"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "get_weather": self._get_weather,
            "get_device_state": self._get_device_state,
            "control_allowed_device": self._control_device,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            result = handler(arguments)
        except Exception as exc:
            message = str(exc)
            if self.settings.ha_token:
                message = message.replace(self.settings.ha_token, "[redacted]")
            result = {"ok": False, "error": message or type(exc).__name__}
        return self._bounded(result)

    def execute_spoken_control(self, transcript: str) -> dict[str, Any] | None:
        text = self._normalize_phrase(transcript)
        if not text:
            return None
        for phrase, (device, action) in sorted(
            self._phrases.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if phrase in text:
                return self.execute(
                    "control_allowed_device", {"device": device, "action": action}
                )
        return None

    def _load_whitelist(self) -> None:
        source = Path(self.settings.ha_whitelist_path).expanduser()
        if not source.exists():
            return
        document = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(document, list):
            raise ValueError("Home Assistant whitelist must be a list")
        for item in document:
            if not isinstance(item, dict):
                raise ValueError("Home Assistant whitelist entries must be objects")
            name = str(item.get("name") or "").strip()
            service = str(item.get("service") or "").strip()
            entity_id = str(item.get("entity_id") or "").strip()
            if not name or "." not in service or "." not in entity_id:
                continue
            domain, action = service.split(".", 1)
            entity_domain = entity_id.split(".", 1)[0]
            risk_text = f"{name} {service} {entity_id}".casefold()
            blocked = (
                domain in BLOCKED_DOMAINS
                or entity_domain in BLOCKED_DOMAINS
                or any(term in risk_text for term in BLOCKED_TERMS)
            )
            if blocked:
                self._blocked_devices.add(name)
                continue
            if domain not in ALLOWED_DOMAINS or entity_domain != domain:
                continue
            if action not in ALLOWED_ACTIONS:
                continue
            service_data = item.get("service_data")
            if service_data is not None and not isinstance(service_data, dict):
                continue
            allowed = AllowedAction(name, service, entity_id, service_data)
            self._actions[(name, action)] = allowed
            self._devices.setdefault(name, allowed)
            for phrase in self._control_phrases(item, name):
                self._phrases.setdefault(phrase, (name, action))

    def _control_phrases(self, item: dict[str, Any], name: str) -> list[str]:
        phrases = item.get("phrases")
        candidates = [name]
        if isinstance(phrases, list):
            candidates.extend(str(phrase) for phrase in phrases)
        return [
            phrase
            for phrase in (self._normalize_phrase(candidate) for candidate in candidates)
            if phrase
        ]

    @staticmethod
    def _normalize_phrase(value: str) -> str:
        return "".join(char for char in value.casefold() if char.isalnum())

    def _get_weather(self, arguments: dict[str, Any]) -> dict[str, Any]:
        city = str(arguments.get("city") or "").strip()
        if not city:
            return {"ok": False, "error": "city is required"}
        if self.settings.hermes_tools_url.rstrip("/") != VERIFIED_HERMES_BASE:
            return {"ok": False, "error": "Hermes REST base is not verified"}
        query = urllib.parse.urlencode({"city": city})
        request = urllib.request.Request(f"{VERIFIED_HERMES_BASE}/weather?{query}", method="GET")
        result = self._read_json(request)
        if not isinstance(result, dict):
            return {"ok": False, "error": "weather service returned invalid data"}
        return result

    def _get_device_state(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = str(arguments.get("device") or "").strip()
        blocked = self._blocked_or_missing(device)
        if blocked is not None:
            return blocked
        unavailable = self._ha_unavailable()
        if unavailable is not None:
            return unavailable
        action = self._devices[device]
        request = urllib.request.Request(
            f"{self.settings.ha_url}/api/states/{action.entity_id}",
            method="GET",
            headers=self._ha_headers(),
        )
        result = self._read_json(request)
        state = result.get("state") if isinstance(result, dict) else None
        return {"ok": True, "device": device, "state": state}

    def _control_device(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = str(arguments.get("device") or "").strip()
        requested_action = str(arguments.get("action") or "").strip()
        blocked = self._blocked_or_missing(device)
        if blocked is not None:
            return blocked
        if requested_action not in ALLOWED_ACTIONS:
            return {"ok": False, "error": "action is not allowed"}
        action = self._actions.get((device, requested_action))
        if action is None:
            return {"ok": False, "error": "action is not allowlisted for device"}
        unavailable = self._ha_unavailable()
        if unavailable is not None:
            return unavailable
        domain, service = action.service.split(".", 1)
        payload: dict[str, Any] = {"entity_id": action.entity_id}
        if action.service_data:
            payload.update(action.service_data)
        request = urllib.request.Request(
            f"{self.settings.ha_url}/api/services/{domain}/{service}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers=self._ha_headers(),
        )
        with self._opener(request, timeout=self._timeout) as response:
            response.read()
        return {"ok": True, "device": device, "action": requested_action}

    def _blocked_or_missing(self, device: str) -> dict[str, Any] | None:
        if device in self._blocked_devices:
            return {"ok": False, "error": "device class is blocked"}
        if device not in self._devices:
            return {"ok": False, "error": "device is not allowlisted"}
        return None

    def _ha_unavailable(self) -> dict[str, Any] | None:
        if not (
            self.settings.ha_enabled
            and self.settings.ha_url
            and self.settings.ha_token
        ):
            return {"ok": False, "error": "Home Assistant is not configured"}
        return None

    def _ha_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.ha_token}",
            "Content-Type": "application/json",
        }

    def _read_json(self, request: urllib.request.Request) -> Any:
        with self._opener(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _bounded(result: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) <= MAX_RESULT_BYTES:
            return result
        return {"ok": False, "error": "tool result too large"}
