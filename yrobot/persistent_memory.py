from __future__ import annotations

import json
import logging
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_PATH = Path.home() / ".config" / "yrobot" / "memory.json"


@dataclass(frozen=True)
class MemoryResult:
    name: str
    ok: bool
    message: str
    mute_model_audio: bool = True


def _normalize(text: str) -> str:
    ignored = set(" ，,。.!！?？、")
    return "".join(ch for ch in text if ch not in ignored).lower()


def _clean_memory(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip(" \t\r\n:：，,")
    if len(clean) > 160:
        clean = clean[:160].rstrip()
    return clean


class PersistentMemory:
    """Small explicit long-term memory stored on the robot."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        enabled: bool = True,
        max_items: int = 40,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else DEFAULT_MEMORY_PATH
        self._enabled = enabled
        self._max_items = max_items
        self._fired: set[tuple[str, str]] = set()
        self._buffer_response_id = ""
        self._buffer_text = ""

    def items(self) -> list[str]:
        document = self._load()
        items = document.get("items", [])
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, str) and item.strip()]

    def add(self, text: str) -> str | None:
        item = _clean_memory(text)
        if not item:
            return None
        items = [existing for existing in self.items() if existing != item]
        items.append(item)
        self._save(items[-self._max_items :])
        return item

    def forget(self, needle: str) -> int:
        query = _clean_memory(needle)
        if not query:
            return 0
        normalized_query = _normalize(query)
        kept = [item for item in self.items() if normalized_query not in _normalize(item)]
        removed = len(self.items()) - len(kept)
        if removed:
            self._save(kept)
        return removed

    def prompt_context(self) -> str:
        items = self.items()
        if not self._enabled or not items:
            return ""
        body = "\n".join(f"- {item}" for item in items[-12:])
        return (
            "Long-term local memory saved on this Reachy Mini. "
            "Use it quietly when relevant; do not mention memory unless asked.\n"
            f"{body}"
        )

    def view(self) -> dict[str, Any]:
        items = self.items()
        return {
            "enabled": self._enabled,
            "path": str(self.path),
            "count": len(items),
            "items": items,
        }

    def handle_text(self, text: str, response_id: str) -> MemoryResult | None:
        if not self._enabled:
            return None
        if response_id != self._buffer_response_id:
            self._buffer_response_id = response_id
            self._buffer_text = ""
        self._buffer_text += text
        normalized = _normalize(self._buffer_text)

        if "我记得什么" in normalized or "你记得什么" in normalized:
            return self._once(response_id, "recall", self._recall_result)

        remember = self._extract_after_marker(
            self._buffer_text,
            ("记住：", "记住:", "请记住：", "请记住:"),
        )
        if remember is not None:
            return self._once(response_id, "remember", lambda: self._remember_result(remember))

        forget = self._extract_after_marker(
            self._buffer_text,
            ("忘掉：", "忘掉:", "忘记：", "忘记:"),
        )
        if forget is not None:
            return self._once(response_id, "forget", lambda: self._forget_result(forget))

        return None

    def _once(
        self,
        response_id: str,
        key_name: str,
        factory,
    ) -> MemoryResult | None:
        key = (response_id, key_name)
        if key in self._fired:
            return None
        self._fired.add(key)
        return factory()

    def _remember_result(self, text: str) -> MemoryResult:
        item = self.add(text)
        if item is None:
            return MemoryResult("本地记忆", False, "没有可保存的记忆。")
        return MemoryResult("本地记忆", True, f"我记住了：{item}")

    def _forget_result(self, text: str) -> MemoryResult:
        removed = self.forget(text)
        if removed == 0:
            return MemoryResult("本地记忆", True, "没有找到匹配的记忆。")
        return MemoryResult("本地记忆", True, f"已忘掉 {removed} 条记忆。")

    def _recall_result(self) -> MemoryResult:
        items = self.items()
        if not items:
            return MemoryResult("本地记忆", True, "我还没有保存任何记忆。")
        spoken = "；".join(item.rstrip("。") for item in items)
        return MemoryResult("本地记忆", True, f"我记得：{spoken}。")

    @staticmethod
    def _extract_after_marker(text: str, markers: tuple[str, ...]) -> str | None:
        for marker in markers:
            if marker in text:
                return _clean_memory(text.split(marker, 1)[1])
        return None

    def _load(self) -> Mapping[str, Any]:
        if not self.path.exists():
            return {"items": []}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("ignoring invalid memory file %s: %s", self.path, exc)
        return {"items": []}

    def _save(self, items: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            delete=False,
        ) as handle:
            json.dump({"items": items}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(self.path)
