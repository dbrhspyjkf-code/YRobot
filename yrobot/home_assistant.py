from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yrobot.config import Settings

logger = logging.getLogger(__name__)

BLOCKED_DOMAINS = {"lock", "alarm_control_panel", "cover"}


@dataclass(frozen=True)
class HomeAssistantAction:
    name: str
    phrases: tuple[str, ...]
    service: str
    entity_id: str
    service_data: dict[str, Any] | None = None
    response: str | None = None


@dataclass(frozen=True)
class HomeAssistantResult:
    action: HomeAssistantAction
    ok: bool
    detail: str = ""


class HomeAssistantClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        opener: Callable = urllib.request.urlopen,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._opener = opener
        self._timeout = timeout

    def call(self, action: HomeAssistantAction) -> None:
        domain, service = action.service.split(".", 1)
        url = f"{self._base_url}/api/services/{domain}/{service}"
        payload: dict[str, Any] = {"entity_id": action.entity_id}
        if action.service_data:
            payload.update(action.service_data)
        # number.set_value + relative_step: read the current value and nudge
        # it by the delta, clamped to the entity's min/max. This lets a
        # whitelist entry say “音量 +10” without knowing the absolute level.
        if (
            domain == "number"
            and service == "set_value"
            and isinstance(action.service_data, dict)
            and action.service_data.get("relative_step") is not None
        ):
            step = float(action.service_data["relative_step"])
            current = self._read_number(action.entity_id)
            if current is not None:
                value, lo, hi = current
                payload["value"] = int(max(lo, min(hi, value + step)))
            payload.pop("relative_step", None)
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        with self._opener(request, timeout=self._timeout) as response:
            response.read()

    def _read_number(self, entity_id: str) -> tuple[int, float, float] | None:
        """Return (value, min, max) for a number entity, or None on failure."""
        url = f"{self._base_url}/api/states/{entity_id}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - best-effort read
            logger.warning("number read failed for %s: %s", entity_id, exc)
            return None
        try:
            attributes = data.get("attributes") or {}
            lo = float(attributes.get("min", -float("inf")))
            hi = float(attributes.get("max", float("inf")))
            return int(float(data.get("state"))), lo, hi
        except (TypeError, ValueError):
            logger.warning("number state parse failed for %s", entity_id)
            return None


def _normalize(text: str) -> str:
    return "".join(text.split()).lower()


def _load_actions(path: str) -> tuple[HomeAssistantAction, ...]:
    source = Path(path).expanduser()
    if not source.exists():
        return ()
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Home Assistant whitelist must be a list")

    actions = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Home Assistant whitelist entries must be objects")
        service = str(item["service"])
        domain = service.split(".", 1)[0]
        if domain in BLOCKED_DOMAINS:
            raise ValueError(f"Home Assistant domain is blocked: {domain}")
        phrases = tuple(str(p) for p in item["phrases"] if str(p).strip())
        if not phrases:
            raise ValueError("Home Assistant whitelist entry needs phrases")
        service_data = item.get("service_data")
        if service_data is not None and not isinstance(service_data, dict):
            raise ValueError("Home Assistant service_data must be an object")
        actions.append(
            HomeAssistantAction(
                name=str(item["name"]),
                phrases=phrases,
                service=service,
                entity_id=str(item["entity_id"]),
                service_data=service_data,
                response=str(item["response"]).strip() if item.get("response") else None,
            )
        )
    return tuple(actions)


class HomeAssistantController:
    def __init__(
        self,
        actions: tuple[HomeAssistantAction, ...],
        caller: Callable[[HomeAssistantAction], None],
        enabled: bool,
    ) -> None:
        self._actions = actions
        self._caller = caller
        self._enabled = enabled
        self._fired: set[tuple[str, str]] = set()
        self._last_fired: dict[str, float] = {}  # entity_id → timestamp
        self._cooldown_s = 30.0
        self._buffer_response_id = ""
        self._buffer_text = ""

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        caller: Callable[[HomeAssistantAction], None] | None = None,
    ) -> HomeAssistantController:
        enabled = bool(settings.ha_enabled and settings.ha_url and settings.ha_token)
        try:
            actions = _load_actions(settings.ha_whitelist_path) if enabled else ()
        except Exception as exc:  # noqa: BLE001 - invalid operator config disables HA only
            logger.warning("Home Assistant whitelist disabled: %s", exc)
            actions = ()
            enabled = False
        if caller is None and enabled:
            client = HomeAssistantClient(settings.ha_url or "", settings.ha_token or "")
            caller = client.call
        return cls(actions, caller or (lambda action: None), enabled)

    def handle_text(self, text: str, response_id: str) -> HomeAssistantResult | None:
        if not self._enabled:
            return None
        if response_id != self._buffer_response_id:
            self._buffer_response_id = response_id
            self._buffer_text = ""
        self._buffer_text += text
        normalized = _normalize(self._buffer_text)
        matches = [
            action
            for action in self._actions
            if any(_normalize(phrase) in normalized for phrase in action.phrases)
        ]
        if not matches:
            return None
        pending = []
        now = time.monotonic()
        for action in matches:
            key = (response_id, action.service, action.entity_id)
            if key in self._fired:
                continue
            # cooldown: don't fire the same entity again within cooldown window
            last = self._last_fired.get(action.entity_id, 0.0)
            if now - last < self._cooldown_s:
                logger.info(
                    "Home Assistant cooldown skipped %s/%s (%.0fs ago)",
                    action.name, action.service, now - last,
                )
                continue
            pending.append(action)
            self._fired.add(key)
            self._last_fired[action.entity_id] = now
        if not pending:
            return None
        try:
            for action in pending:
                self._caller(action)
        except Exception as exc:  # noqa: BLE001 - HA failures should not stop conversation
            return HomeAssistantResult(action, False, str(exc))
        return HomeAssistantResult(pending[0], True)
