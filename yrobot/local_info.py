from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LocalInfoResult:
    name: str
    ok: bool
    message: str
    mute_model_audio: bool = True


def _normalize(text: str) -> str:
    ignored = set(" ，,。.!！?？、")
    return "".join(ch for ch in text if ch not in ignored).lower()


class LocalInfoController:
    def __init__(
        self,
        *,
        enabled: bool = True,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._enabled = enabled
        self._now = now
        self._fired: set[tuple[str, str]] = set()
        self._buffer_response_id = ""
        self._buffer_text = ""

    def handle_text(self, text: str, response_id: str) -> LocalInfoResult | None:
        if not self._enabled:
            return None
        if response_id != self._buffer_response_id:
            self._buffer_response_id = response_id
            self._buffer_text = ""
        self._buffer_text += text
        normalized = _normalize(self._buffer_text)

        if (
            "当前日期" in normalized
            or "今天日期" in normalized
            or "今天几号" in normalized
            or "今天星期" in normalized
            or "今天周" in normalized
            or re.search(r"今天是.+月.+[日号]", normalized)
            or re.search(r"今天是星期[一二三四五六日天]", normalized)
            or re.search(r"今天是周[一二三四五六日天]", normalized)
        ):
            return self._once(response_id, "date", self._date_result)
        if "当前时间" in normalized or "现在是" in normalized or "几点" in normalized:
            return self._once(response_id, "time", self._time_result)
        return None

    def _once(
        self,
        response_id: str,
        key_name: str,
        factory: Callable[[], LocalInfoResult],
    ) -> LocalInfoResult | None:
        key = (response_id, key_name)
        if key in self._fired:
            return None
        self._fired.add(key)
        return factory()

    def _date_result(self) -> LocalInfoResult:
        now = self._now()
        weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
        message = f"今天是{now.year}年{now.month}月{now.day}日，{weekdays[now.weekday()]}。"
        return LocalInfoResult("本地日期", True, message)

    def _time_result(self) -> LocalInfoResult:
        now = self._now()
        return LocalInfoResult("本地时间", True, f"现在是{now.hour}点{now.minute:02d}分。")
