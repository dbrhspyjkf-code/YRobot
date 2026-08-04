from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from yrobot.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HermesToolResult:
    name: str
    ok: bool
    message: str


def _normalize(text: str) -> str:
    return "".join(text.split()).lower()


class HermesToolsClient:
    def __init__(
        self,
        base_url: str,
        *,
        opener: Callable = urllib.request.urlopen,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._opener = opener
        self._timeout = timeout

    def get_deepseek_balance(self) -> str:
        request = urllib.request.Request(f"{self._base_url}/api/deepseek/balance", method="GET")
        with self._opener(request, timeout=self._timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(str(data.get("error") or "DeepSeek balance query failed"))
        balance = data.get("total_balance") or data.get("balance") or "?"
        currency = data.get("currency") or "CNY"
        return f"DeepSeek 余额：{balance} {currency}"


class HermesToolsController:
    def __init__(self, client: HermesToolsClient, enabled: bool) -> None:
        self._client = client
        self._enabled = enabled
        self._fired: set[tuple[str, str]] = set()
        self._buffer_response_id = ""
        self._buffer_text = ""

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        opener: Callable = urllib.request.urlopen,
    ) -> HermesToolsController:
        client = HermesToolsClient(settings.hermes_tools_url, opener=opener)
        return cls(client, bool(settings.hermes_tools_enabled))

    def handle_text(self, text: str, response_id: str) -> HermesToolResult | None:
        if not self._enabled:
            return None
        if response_id != self._buffer_response_id:
            self._buffer_response_id = response_id
            self._buffer_text = ""
        self._buffer_text += text
        normalized = _normalize(self._buffer_text)
        if not any(phrase in normalized for phrase in ("deepseek余额", "deepseek还有多少钱")):
            return None

        key = (response_id, "deepseek_balance")
        if key in self._fired:
            return None
        self._fired.add(key)
        try:
            message = self._client.get_deepseek_balance()
        except Exception as exc:  # noqa: BLE001 - external tools should not stop conversation
            return HermesToolResult("DeepSeek余额", False, str(exc))
        return HermesToolResult("DeepSeek余额", True, message)
