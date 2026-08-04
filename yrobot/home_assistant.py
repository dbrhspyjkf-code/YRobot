from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yrobot.config import Settings

logger = logging.getLogger(__name__)

BLOCKED_DOMAINS = {"lock", "alarm_control_panel", "cover"}


@dataclass(frozen=True)
class HomeAssistantAction:
    name: str
    phrases: tuple[str, ...]
    service: str
    entity_id: str


@dataclass(frozen=True)
class HomeAssistantResult:
    action: HomeAssistantAction
    ok: bool
    detail: str = ""


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
        actions.append(
            HomeAssistantAction(
                name=str(item["name"]),
                phrases=phrases,
                service=service,
                entity_id=str(item["entity_id"]),
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
        return cls(actions, caller or (lambda action: None), enabled)

    def handle_text(self, text: str, response_id: str) -> HomeAssistantResult | None:
        if not self._enabled:
            return None
        normalized = _normalize(text)
        matches = [
            action
            for action in self._actions
            if any(_normalize(phrase) in normalized for phrase in action.phrases)
        ]
        if len(matches) != 1:
            return None
        action = matches[0]
        key = (response_id, action.name)
        if key in self._fired:
            return None
        self._fired.add(key)
        try:
            self._caller(action)
        except Exception as exc:  # noqa: BLE001 - HA failures should not stop conversation
            return HomeAssistantResult(action, False, str(exc))
        return HomeAssistantResult(action, True)
