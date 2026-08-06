from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from yrobot.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HermesToolResult:
    name: str
    ok: bool
    message: str
    mute_model_audio: bool = True


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

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        request = urllib.request.Request(f"{self._base_url}{path}{query}", method="GET")
        with self._opener(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_deepseek_balance(self) -> dict[str, Any]:
        return self._get_json("/api/deepseek/balance")

    def get_weather(self, city: str = "广州") -> dict[str, Any]:
        return self._get_json("/weather", params={"city": city})

    def get_rate(self) -> dict[str, Any]:
        return self._get_json("/rate")

    def get_stocks_portfolio(self) -> dict[str, Any]:
        return self._get_json("/api/stocks/portfolio")

    def get_stock_price(self, name: str) -> dict[str, Any]:
        return self._get_json("/api/stocks/price", params={"name": name})

    def get_stock_advice(self, name: str | None = None) -> dict[str, Any]:
        params = {"name": name} if name else None
        return self._get_json("/api/stocks/advice", params=params)


# ---------------------------------------------------------------------------
# Tool registry: phrases that fire a tool, the call, and how to format the
# reply for TTS. Each tool fires at most once per gateway response_id.
# ---------------------------------------------------------------------------

WEATHER_DEFAULT_CITY = "广州"
WEATHER_KNOWN_CITIES = (
    "广州", "深圳", "北京", "上海", "成都", "杭州", "南京", "武汉",
    "西安", "重庆", "苏州", "天津", "青岛", "厦门", "福州", "济南",
    "郑州", "长沙", "合肥", "南昌",
)


def _extract_city(normalized: str) -> str:
    for city in WEATHER_KNOWN_CITIES:
        if city in normalized:
            return city
    return WEATHER_DEFAULT_CITY


def _format_weather(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        error = data.get("error") or "天气服务暂不可用"
        return f"天气查询失败：{error}"
    city = data.get("city") or "当地"
    condition = data.get("condition") or "未知"
    temp = data.get("temp_c") or "?"
    feels = data.get("feels_like_c")
    humidity = data.get("humidity")
    wind = data.get("wind_kmph")
    bits = [f"{city}当前{condition}，气温{temp}度"]
    if feels and feels != temp:
        bits.append(f"体感{feels}度")
    if humidity:
        bits.append(f"湿度{humidity}%")
    if wind:
        bits.append(f"风速每秒{wind}公里")
    return "，".join(bits) + "。"


def _format_rate(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        error = data.get("error") or "汇率服务暂不可用"
        return f"汇率查询失败：{error}"
    rates = data.get("rates") or {}
    base = data.get("base") or "USD"
    pieces: list[str] = []
    for code in ("CNY", "EUR", "JPY", "HKD", "GBP"):
        if code not in rates:
            continue
        try:
            v = float(rates[code])
        except (TypeError, ValueError):
            continue
        if base == "USD" and code == "USD":
            continue
        # If base is USD and target is CNY: 1 USD = v CNY
        # For non-USD target, invert to “1 <code> = <v_inverse> USD”
        if base == "USD":
            pieces.append(f"1{base}兑{code}{v:.2f}")
        else:
            inverse = v if code == base else (1.0 / v) if v else 0.0
            pieces.append(f"1{code}约{base}{inverse:.2f}")
    return f"汇率参考：{'；'.join(pieces)}。" if pieces else "汇率服务暂不可用。"


def _format_stocks(data: dict[str, Any]) -> str:
    items = data.get("items") or []
    if not items:
        return "股票服务暂时没有返回任何持仓。"
    by_pct = sorted(
        (it for it in items if isinstance(it.get("pchg"), (int, float))),
        key=lambda it: float(it["pchg"]),
    )
    worst = by_pct[0] if by_pct else None
    best = by_pct[-1] if by_pct else None
    up = sum(1 for it in items if float(it.get("pchg", 0)) > 0)
    down = sum(1 for it in items if float(it.get("pchg", 0)) < 0)
    parts = [f"你关注{len(items)}只股票，上涨{up}只，下跌{down}只。"]
    if best and float(best.get("pchg", 0)) > 0:
        parts.append(
            "涨幅最大是" + str(best.get('name', '?')) +
            "（" + str(best.get('code', '')) + "），" +
            f"+{float(best['pchg']):.2f}%。"
        )
    if worst and float(worst.get("pchg", 0)) < 0:
        parts.append(
            "跌幅最大是" + str(worst.get('name', '?')) +
            "（" + str(worst.get('code', '')) + "），" +
            f"{float(worst['pchg']):.2f}%。"
        )
    # Detailed: append one_sentence for a few highlighted stocks (keep TTS short).
    detail_parts: list[str] = []
    if best:
        detail_parts.append(
            f"{best.get('name', '')}：{best.get('one_sentence') or '暂无点评'}"
        )
    if worst:
        detail_parts.append(
            f"{worst.get('name', '')}：{worst.get('one_sentence') or '暂无点评'}"
        )
    # add 2-3 mid movers with sentences
    mids = [it for it in items if it not in (best, worst) and it.get("one_sentence")]
    for it in mids[:3]:
        detail_parts.append(f"{it.get('name', '')}：{it.get('one_sentence')}")
    if detail_parts:
        parts.append("")
        parts.extend(detail_parts)
    return "\n".join(parts)


def _format_stock_price(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        error = data.get("error") or "股票行情查询失败"
        return f"行情查询失败：{error}"
    text = data.get("text") or "暂无行情数据"
    return str(text).strip() or "暂无行情数据。"


def _format_stock_advice(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        error = data.get("error") or "操盘建议查询失败"
        return f"操盘建议查询失败：{error}"
    text = data.get("text") or "暂无建议"
    # keep only the summary line for TTS (text may contain many lines)
    first_line = str(text).strip().splitlines()[0] if text else "暂无建议"
    return first_line + "。"


def _has_stock_code(text: str) -> bool:
    """Return True if the text contains a 6-digit A-share stock code.

    Uses negative lookaround (no ASCII \\b which breaks at Chinese chars) so the
    code can sit anywhere in a Chinese sentence, e.g. '688018怎么样'.
    """
    import re as _re
    return _re.search(r"(?<!\d)\d{6}(?!\d)", text) is not None


def _is_portfolio_intent(text: str) -> bool:
    """True only when the text expresses 'show my holdings' intent.

    A bare mention of '股票' (the noun) is not portfolio intent — the model
    often writes "想查询的股票" or "哪只股票" while clarifying a single-stock
    query, and matching that fires the portfolio tool by mistake.
    """
    portfolio_phrases = (
        "我的股票", "我的持仓", "我的自选", "持仓", "自选股",
        "股票怎么样", "股票如何", "持有的股票", "全部股票",
        "我关注的股票", "关注的股票", "我买的股票",
    )
    return any(p in text for p in portfolio_phrases)


def _extract_stock_name(normalized: str) -> str:
    """Extract a stock name/code from the user's request text."""
    import re as _re
    # 6-digit code
    match = _re.search(r"(?<!\d)\d{6}(?!\d)", normalized)
    if match:
        return match.group(0)
    # Strip common verbs around a name. Longer phrases first so that e.g.
    # "看一下" wins over the standalone "一下".
    cleaned = _re.sub(
        r"查一下|查询|看看|看一下|帮忙|帮我|请帮我|请你|让我|我想|想买|能不能买|能买吗|怎么样|怎样|如何|多少钱|多少|股价|股票价格|价格|行情|分析|建议|操盘|点评|表现|现在|最近|这个|那|一下|查|呢|吗|的|了",
        "",
        normalized,
    )
    cleaned = cleaned.strip()
    # Strip the noun "股票" so "平安股票怎么样" → "平安".
    # Keep the prefix "我的" / "自选" so portfolio queries return "".
    cleaned = cleaned.replace("的股票", "").replace("股票", "")
    cleaned = cleaned.strip()
    # Strip leading verb remnants after the noun strip (e.g., "买比亚迪").
    if cleaned.startswith("买"):
        cleaned = cleaned[1:]
    cleaned = cleaned.strip()
    # Reject obvious non-names.
    if not cleaned:
        return ""
    if cleaned in ("我", "我的", "自选", "自选股", "持仓", "它", "这", "那"):
        return ""
    if 2 <= len(cleaned) <= 8:
        return cleaned
    return ""


def _format_deepseek(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        error = data.get("error") or "DeepSeek 账户查询失败"
        return f"DeepSeek 余额查询失败：{error}"
    balance = data.get("total_balance") or data.get("balance") or "?"
    currency = data.get("currency") or "CNY"
    return f"DeepSeek 余额：{balance} {currency}。"


@dataclass(frozen=True)
class ToolDef:
    name: str
    phrases: tuple[str, ...]
    call: Callable[[HermesToolsClient, str], dict[str, Any]]
    format: Callable[[dict[str, Any]], str]
    mute_model_audio: bool = True
    skip_when: Callable[[str], bool] | None = None
    cooldown_s: float = 30.0
    extra_matches: Callable[[str], bool] | None = None

    def matches(self, text: str) -> bool:
        if any(phrase in text for phrase in self.phrases):
            return True
        if self.extra_matches is not None and self.extra_matches(text):
            return True
        return False


TOOL_DEFS: tuple[ToolDef, ...] = (
    ToolDef(
        name="weather",
        phrases=("天气",),
        call=lambda client, text: client.get_weather(_extract_city(text)),
        format=_format_weather,
    ),
    ToolDef(
        name="stock_price",
        phrases=("多少钱", "股价", "行情", "价格", "价格多少", "股票价格", "多少钱一股", "现在多少"),
        call=lambda client, text: client.get_stock_price(_extract_stock_name(text) or ""),
        format=_format_stock_price,
        skip_when=lambda text: not _extract_stock_name(text),
        extra_matches=lambda text: _has_stock_code(text) and any(
            kw in text for kw in ("价格", "股价", "行情", "多少")
        ),
        cooldown_s=3.0,
    ),
    ToolDef(
        name="stock_advice",
        phrases=("怎么样", "怎样", "如何", "分析", "建议", "操盘", "点评", "表现", "能买吗", "能不能买"),
        call=lambda client, text: client.get_stock_advice(_extract_stock_name(text) or None),
        format=_format_stock_advice,
        skip_when=lambda text: not _extract_stock_name(text),
        extra_matches=lambda text: _has_stock_code(text) and any(
            kw in text for kw in ("怎么样", "建议", "分析", "能买")
        ),
        cooldown_s=3.0,
    ),
    ToolDef(
        name="stocks_advice_all",
        phrases=("股票建议", "操盘建议", "股票推荐", "建议汇总"),
        call=lambda client, text: client.get_stock_advice(),
        format=_format_stock_advice,
    ),
    ToolDef(
        name="stocks",
        phrases=(
            "我的股票", "我的持仓", "我的自选", "持仓", "自选股",
            "股票怎么样", "股票如何", "持有的股票", "我关注的股票",
            "我买的股票", "关注的股票", "全部股票",
        ),
        call=lambda client, text: client.get_stocks_portfolio(),
        format=_format_stocks,
        skip_when=lambda text: not _is_portfolio_intent(text),
    ),
    ToolDef(
        name="rate",
        phrases=("汇率", "美元换人民币", "汇率多少", "人民币汇率"),
        call=lambda client, text: client.get_rate(),
        format=_format_rate,
    ),
    ToolDef(
        name="deepseek_balance",
        phrases=("deepseek余额", "deepseek还有多少钱", "deepseek余额多少"),
        call=lambda client, text: client.get_deepseek_balance(),
        format=_format_deepseek,
    ),
)


def _validate_tool_defs(defs: tuple[ToolDef, ...]) -> None:
    """Fail fast on duplicate tool names or empty phrases."""
    seen_names: dict[str, str] = {}
    seen_phrases: dict[str, str] = {}
    for tool in defs:
        if tool.name in seen_names:
            raise RuntimeError(
                f"Duplicate Hermes tool name {tool.name!r}: also defined as "
                f"{seen_names[tool.name]!r}. Two ToolDef entries would silently "
                "share a cooldown/dedup slot."
            )
        seen_names[tool.name] = str(tool.phrases[0]) if tool.phrases else ""
        for phrase in tool.phrases:
            if not phrase:
                raise RuntimeError(
                    f"Hermes tool {tool.name!r} has an empty phrase."
                )
            if phrase in seen_phrases:
                logger.warning(
                    "Hermes tool phrase %r is shared by %s and %s; "
                    "only the first match wins per response_id.",
                    phrase, seen_phrases[phrase], tool.name,
                )
            else:
                seen_phrases[phrase] = tool.name


_validate_tool_defs(TOOL_DEFS)


class HermesToolsController:
    def __init__(self, client: HermesToolsClient, enabled: bool) -> None:
        self._client = client
        self._enabled = enabled
        self._fired: set[tuple[str, str]] = set()
        self._last_fired_at: dict[str, float] = {}
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
        for tool in TOOL_DEFS:
            if not tool.matches(normalized):
                continue
            if tool.skip_when and tool.skip_when(normalized):
                continue
            key = (response_id, tool.name)
            if key in self._fired:
                return None
            now = time.monotonic()
            last_fired_at = self._last_fired_at.get(tool.name)
            if last_fired_at is not None and now - last_fired_at < tool.cooldown_s:
                self._fired.add(key)
                return None
            self._fired.add(key)
            self._last_fired_at[tool.name] = now
            try:
                raw = tool.call(self._client, normalized)
                message = tool.format(raw)
            except Exception as exc:  # noqa: BLE001 — external tools must not stop conversation
                logger.warning("Hermes tool %s failed: %s", tool.name, exc)
                return HermesToolResult(tool.name, False, str(exc), tool.mute_model_audio)
            return HermesToolResult(tool.name, True, message, tool.mute_model_audio)
        return None
