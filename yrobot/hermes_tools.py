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
from yrobot.profile import Profile, load_profile as _load_profile_default

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
        ios_api_url: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._ios_api_url = (ios_api_url or "").rstrip("/")
        self._opener = opener
        self._timeout = timeout

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        request = urllib.request.Request(f"{self._base_url}{path}{query}", method="GET")
        with self._opener(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self._opener(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def call_tool(self, name: str, prompt: str) -> dict[str, Any]:
        """Call an arbitrary Hermes tool by name via /api/tools/call.

        All hermes-mcp tools accept a single natural-language ``prompt``
        argument (see ``create_server``). Generic tools are dispatched through
        the ios-api bridge (8900) — not the 8766 REST server — so this method
        posts to ``ios_api_url`` when configured.

        Returns the raw dispatch response ``{"ok": bool, "result": str}``.
        """
        base = self._ios_api_url or self._base_url
        payload = json.dumps(
            {"name": name, "arguments": {"prompt": prompt}}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{base}/api/tools/call",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
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

    def ping(self) -> dict[str, Any]:
        """Lightweight server reachability probe (expects any 200 + JSON).

        Many servers do not expose a dedicated health endpoint, so we just
        hit the portfolio path which is cheap. Returns the parsed JSON so
        callers can also validate the response shape.
        """
        return self._get_json("/api/stocks/portfolio")

    def discover(self) -> dict[str, Any]:
        """Fetch /api/discover — the server's self-reported tool list.

        Returns the raw discover payload (``{"tools": [...]}``). Raises
        on connection/parse failure; callers decide how to degrade.
        """
        return self._get_json("/api/discover")


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
    Recognises both Arabic digits ('600600') and Chinese digit readings
    ('六零零六零零') because the gateway sometimes emits the latter.
    """
    import re as _re
    if _re.search(r"(?<!\d)\d{6}(?!\d)", text) is not None:
        return True
    return _chinese_digit_code(text) is not None


_CHINESE_DIGITS = "零一二三四五六七八九"
_CHINESE_DIGIT_CODE_RE = None  # compiled lazily


_NOISE_RUNS = {
    "我们", "你们", "他们", "好的", "这个", "那个", "什么", "怎么", "为什么",
    "我的", "我的股票", "自选", "自选股", "持仓", "它", "这", "那",
    "股票", "请稍等", "我来", "帮我", "你问", "帮你", "我们", "来看", "去帮",
    "一下", "那来", "们来", "方案", "方案目", "目",
}


def _looks_like_stock_name(candidate: str) -> bool:
    """Best-effort check that a candidate run is a plausible stock name.

    Rejects runs that are pure stop-word residues or end with a verb leftover.
    """
    if not candidate or not (2 <= len(candidate) <= 8):
        return False
    if candidate in _NOISE_RUNS:
        return False
    # Ends with a leading-verb leftover like 来/帮/查/看/买.
    if candidate.endswith(("来", "帮", "查", "看", "买", "我", "你")) and len(candidate) > 3:
        return False
    return True


def _chinese_digit_code(text: str) -> str | None:
    """Convert a 6-character Chinese digit reading to its Arabic form.

    Examples: '六零零六零零' -> '600600', '六八八零一八' -> '688018'.
    Returns None if no valid 6-character Chinese-digit run is present.
    """
    import re as _re
    global _CHINESE_DIGIT_CODE_RE
    if _CHINESE_DIGIT_CODE_RE is None:
        _CHINESE_DIGIT_CODE_RE = _re.compile(r"[零一二三四五六七八九]{6}")
    m = _CHINESE_DIGIT_CODE_RE.search(text)
    if m is None:
        return None
    mapping = {c: str(i) for i, c in enumerate(_CHINESE_DIGITS)}
    return "".join(mapping[c] for c in m.group(0))


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


# Known A-share stock names: hermes-mcp _resolve_secid.known + user portfolio.
# Matching against this list first is far more robust than stripping arbitrary
# stop words from free-form model paraphrases.
KNOWN_STOCK_NAMES = frozenset({
    "茅台", "贵州茅台", "腾讯", "腾讯控股", "比亚迪", "宁德时代",
    "胜宏科技", "中际旭创", "乐鑫科技", "万华化学", "中国核电",
    "福耀玻璃", "三花智控", "豪威集团", "芒果超媒", "宝钛股份", "迈瑞医疗",
    "平安", "中国平安", "平安银行", "工商银行", "建设银行", "农业银行",
    "招商银行", "万科A", "万科", "中国中免", "隆基绿能", "阳光电源",
    "药明康德", "恒瑞医药", "京东方A", "中兴通讯", "海康威视",
    "紫金矿业", "中国神华", "长江电力", "美的集团", "格力电器",
    "立讯精密", "潍柴动力", "三一重工", "中远海控", "东方财富",
    "同花顺", "中国移动", "中国电信", "中国联通", "工业富联",
    "科大讯飞", "金山办公", "信息发展", "华信新材", "兆易创新", "北方华创", "中芯国际",
})


def _match_known_stock_name(text: str) -> str | None:
    """Return the longest known stock name found in ``text``."""
    best = None
    for name in KNOWN_STOCK_NAMES:
        if name in text and (best is None or len(name) > len(best)):
            best = name
    return best


def _margin_prompt(text: str) -> str:
    """Build a hermes-friendly margin query.

    hermes get_margin_data extracts a 6-digit code via regex; it does NOT
    understand Chinese-digit readings (六八八零一八). Normalise any Chinese
    digit code to Arabic and prefer the code so the search query is exact.
    """
    import re as _re
    code = _re.search(r"(?<!\d)\d{6}(?!\d)", text)
    if code:
        return f"查询{code.group(0)}融资融券"
    chinese_code = _chinese_digit_code(text)
    if chinese_code is not None:
        return f"查询{chinese_code}融资融券"
    return text


def _extract_stock_name(normalized: str) -> str:
    """Extract a stock name/code from the user's request text.

    Strategy:
    1. If a 6-digit code is present anywhere, return it.
    2. Chinese-digit code (六零零六零零 → 600600).
    3. Match against KNOWN_STOCK_NAMES (portfolio + famous A-shares). This
       is the robust path: model paraphrases are infinite, but the set of
       stocks the user actually asks about is small and mostly known.
    4. Stop-word strip + longest-Han-run fallback.
    """
    import re as _re
    code_match = _re.search(r"(?<!\d)\d{6}(?!\d)", normalized)
    if code_match:
        return code_match.group(0)
    chinese_code = _chinese_digit_code(normalized)
    if chinese_code is not None:
        return chinese_code
    known = _match_known_stock_name(normalized)
    if known is not None:
        return known

    # --- Stop-word strip + longest Han run (primary fallback) ---
    # Strip stop words (longest alternatives first so e.g. 多少钱 wins over 多少).
    cleaned = _re.sub(
        r"查一下|查询|看看|看一下|帮忙|帮我|请帮我|请你|让我|我想|想买|能不能买|能买吗|"
        r"怎么样|怎样|如何|多少钱|多少|股价|股票价格|价格|行情|分析|建议|操盘|点评|表现|"
        r"现在|最近|这个|那|一下|查|呢|吗|的|了|好的|我来|我帮您|我来帮您|帮您|您|为|"
        r"正在|马上|稍等|请稍等|稍等一下|等待|在|受|好的|是|我|呀|啊|哈|嗯|那个|这个|"
        r"你问|你说|帮我|帮你|我们|我来|来看|去帮|们来|的方案|这个方案|方案|"
        r"随便|一个|最新|目前|目|行情信息|最新信息|"
        r"。|，|、|！|？|~|·|;",
        "",
        normalized,
    )
    # Strip greetings / family address words that the model often prepends.
    cleaned = _re.sub(
        r"嘿|喂|哎呀|哎|诶|好的呀|嗯|哈哈|爸爸|妈妈|爷爷|奶奶|朋友|哥们|你好|您好|好|呀",
        "",
        cleaned,
    )

    # --- Trigger-phrase cut on the cleaned text (covers short utterances) ---
    # After stop words are removed, a stock-intent keyword directly follows the
    # name (“比亚迪怎么样” → cleaned “比亚迪怎么样” → take chars before keyword).
    trigger_match = _re.search(
        r"(?P<name>[\u4e00-\u9fff]{2,4})[，,、\s]*(?:怎么样|怎样|如何|分析|建议|操盘|点评|表现|能买吗|能不能买|多少钱|价格多少|股价|行情|价格)",
        cleaned,
    )
    if trigger_match:
        raw_name = trigger_match.group("name")
        raw_name = (
            raw_name[:-2]
            if raw_name.endswith("股票") and len(raw_name) > 2
            else raw_name
        )
        if _looks_like_stock_name(raw_name):
            return raw_name
    # Find the longest run of Chinese characters (Han script) of length 2-8.
    runs = _re.findall(r"[\u4e00-\u9fff]{2,8}", cleaned)
    # Remove trailing 股票 noun from each run.
    runs = [r[:-2] if r.endswith("股票") and len(r) > 2 else r for r in runs]
    # Strip a leading "买" (buy) verb remnant after the noun strip.
    runs = [r[1:] if r.startswith("买") and len(r) > 3 else r for r in runs]
    # Reject runs that look like stop-word residues or portfolio placeholders.
    REJECT_RUNS = {
        "我们", "你们", "他们", "好的", "这个", "那个", "什么", "怎么", "为什么",
        "我的", "我的股票", "自选", "自选股", "持仓", "它", "这", "那",
        "股票", "请稍等",
    }
    candidates = [r for r in runs if r and r not in REJECT_RUNS]
    if not candidates:
        return ""
    # Prefer the longest; ties → earliest in text.
    candidates.sort(key=lambda r: (-len(r), cleaned.index(r)))
    return candidates[0]


def _format_deepseek(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        error = data.get("error") or "DeepSeek 账户查询失败"
        return f"DeepSeek 余额查询失败：{error}"
    balance = data.get("total_balance") or data.get("balance") or "?"
    currency = data.get("currency") or "CNY"
    return f"DeepSeek 余额：{balance} {currency}。"


def _format_generic_tool(data: dict[str, Any]) -> str:
    """Format the generic {ok, result} response from /api/tools/call."""
    if not data.get("ok"):
        error = data.get("error") or "工具调用失败"
        return f"工具调用失败：{error}"
    result = data.get("result")
    if not result:
        return "完成。"
    text = str(result).strip()
    # Keep the first few lines for TTS (many tools return multi-line tables).
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "完成。"
    # Some tools return many lines; speak the first 1-3 meaningful lines.
    summary = "，".join(lines[:3])
    return summary[:300] + ("。" if not summary.endswith(("。", "！", "？", "。", "！", "？")) else "")


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
    # Optional mapping to the server-side discover name (GET /api/discover
    # on 8766, or GET /api/tools on the ios-api 8900 bridge when
    # discover_source="ios_api"). When set, validate_tools cross-checks this
    # tool against the matching discover payload; when absent, validate_tools
    # falls back to the dry-run probe.
    discover_name: str | None = None
    discover_source: str = "hermes"  # "hermes" (8766) or "ios_api" (8900)

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
        discover_name="weather",
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
        discover_name="stock_price",
    ),
    ToolDef(
        name="stock_advice",
        phrases=("建议", "操盘", "点评", "能买吗", "能不能买", "推荐买"),
        call=lambda client, text: client.call_tool(
            "get_stock_advice", f"查询{_extract_stock_name(text)}的操盘建议"
        ),
        format=_format_generic_tool,
        skip_when=lambda text: (
            not _extract_stock_name(text)
            or any(kw in text for kw in ("详细分析", "资金流向", "十大股东", "财务数据", "基本面分析"))
        ),
        extra_matches=lambda text: _has_stock_code(text) and any(
            kw in text for kw in ("建议", "能买")
        ),
        cooldown_s=3.0,
        discover_name="stock_advice",
    ),
    ToolDef(
        name="stocks_advice_all",
        phrases=("股票建议", "操盘建议", "股票推荐", "建议汇总"),
        call=lambda client, text: client.get_stock_advice(),
        format=_format_stock_advice,
        discover_name="stock_advice",
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
        discover_name="stocks_portfolio",
    ),
    ToolDef(
        name="rate",
        phrases=("汇率", "美元换人民币", "汇率多少", "人民币汇率"),
        call=lambda client, text: client.get_rate(),
        format=_format_rate,
        discover_name="rate",
    ),
    ToolDef(
        name="deepseek_balance",
        phrases=("deepseek余额", "deepseek还有多少钱", "deepseek余额多少"),
        call=lambda client, text: client.get_deepseek_balance(),
        format=_format_deepseek,
        discover_name="deepseek_balance",
    ),
    # --- Generic tools: dispatch by name to hermes-mcp /api/tools/call ---
    # Each receives the full caption as its natural-language prompt.
    ToolDef(
        name="cctv_news",
        phrases=("新闻联播", "今日新闻", "新闻摘要", "联播摘要"),
        call=lambda client, text: client.call_tool("get_cctv_news", text),
        format=_format_generic_tool,
        discover_name="get_cctv_news",
        discover_source="ios_api",
    ),
    ToolDef(
        name="web_search",
        phrases=("搜索一下", "帮我搜", "网上查", "搜索", "查一下新闻", "最新消息"),
        call=lambda client, text: client.call_tool("web_search", text),
        format=_format_generic_tool,
        discover_name="web_search",
        discover_source="ios_api",
        cooldown_s=10.0,
    ),
    ToolDef(
        name="stock_detail",
        phrases=("详细分析", "资金流向", "十大股东", "财务数据", "基本面分析", "怎么样", "怎样", "如何", "分析一下", "研究一下"),
        call=lambda client, text: client.call_tool(
            "get_stock_detail", f"查询{_extract_stock_name(text)}的详细分析"
        ),
        format=_format_generic_tool,
        skip_when=lambda text: not _extract_stock_name(text),
        cooldown_s=5.0,
        discover_name="get_stock_detail",
        discover_source="ios_api",
    ),
    ToolDef(
        name="margin_data",
        phrases=("两融数据", "融资融券", "融资余额", "融券余额", "融资", "融券"),
        call=lambda client, text: client.call_tool(
            "get_margin_data", _margin_prompt(text)
        ),
        format=_format_generic_tool,
        extra_matches=lambda text: _has_stock_code(text) and any(
            kw in text for kw in ("融资", "融券", "两融")
        ),
        discover_name="get_margin_data",
        discover_source="ios_api",
    ),
    ToolDef(
        name="ipo_info",
        phrases=("新股申购", "新股信息", "打新"),
        call=lambda client, text: client.call_tool("get_ipo_info", text),
        format=_format_generic_tool,
        discover_name="get_ipo_info",
        discover_source="ios_api",
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
    def __init__(
        self,
        client: HermesToolsClient,
        enabled: bool,
        *,
        profile: Profile | None = None,
    ) -> None:
        self._client = client
        self._enabled = enabled
        self._profile = profile
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
        profile: Profile | None = None,
    ) -> HermesToolsController:
        client = HermesToolsClient(
            settings.hermes_tools_url,
            opener=opener,
            ios_api_url=settings.hermes_ios_api_url,
        )
        return cls(
            client,
            bool(settings.hermes_tools_enabled),
            profile=profile,
        )

    def handle_text(self, text: str, response_id: str) -> HermesToolResult | None:
        if not self._enabled:
            return None
        if response_id != self._buffer_response_id:
            self._buffer_response_id = response_id
            self._buffer_text = ""
        self._buffer_text += text
        normalized = _normalize(self._buffer_text)
        for tool in TOOL_DEFS:
            if self._profile is not None and not self._profile.allows(tool.name):
                continue
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


# ---------------------------------------------------------------------------
# Startup validation: dry-run probe each enabled tool to catch config errors
# before the first user request. Failures are surfaced as warnings rather
# than exceptions so a single broken tool does not block startup.
# ---------------------------------------------------------------------------

# Dry-run probes use a safe sentinel that hermes-mcp resolves as a real
# stock so we exercise the same JSON shape without side-effects.
_DRY_RUN_PROBES: dict[str, tuple[str, dict[str, str]]] = {
    "stock_price": ("/api/stocks/price", {"name": "600600"}),
    "stock_advice": ("/api/stocks/advice", {}),  # no name = aggregated advice
    "stocks_advice_all": ("/api/stocks/advice", {}),
    "stocks": ("/api/stocks/portfolio", {}),
    "rate": ("/rate", {}),
    "weather": ("/weather", {"city": "广州"}),
    "deepseek_balance": ("/api/deepseek/balance", {}),
}


@dataclass(frozen=True)
class ToolHealth:
    name: str
    ok: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.name}: {'OK' if self.ok else 'FAIL'} ({self.detail})"


def _looks_like_response(raw: object) -> bool:
    """Loose response-shape check for Hermes endpoints.

    Endpoints are not required to return {"ok": bool}; portfolio returns
    {"count", "items"}, others may return {"name", "text"}. We only require
    a JSON object that has at least one of the common keys so a 404 page
    or non-JSON response is still flagged as broken.
    """
    if not isinstance(raw, dict):
        return False
    return any(k in raw for k in ("ok", "count", "items", "name", "text", "balance"))


def _probe_generic_tool(
    client: HermesToolsClient,
    tool: ToolDef,
    timeout: float | None,
) -> bool:
    """Dry-run a generic (ios_api) tool by calling it with a sentinel prompt.

    Uses a no-op prompt that should be safe for read-only tools; write tools
    (add_note/add_reminder/send_email) are NOT probed here to avoid side
    effects — they're only validated via the ios /api/tools list.
    """
    if tool.name in ("add_note", "add_reminder", "send_email"):
        return True  # skip side-effecting probes
    saved_timeout = client._timeout
    if timeout is not None:
        client._timeout = timeout
    try:
        raw = client.call_tool(tool.discover_name or tool.name, "ping")
        return bool(raw.get("ok"))
    except Exception:  # noqa: BLE001
        return False
    finally:
        client._timeout = saved_timeout


def validate_tools(
    client: HermesToolsClient,
    *,
    enabled_tools: tuple[ToolDef, ...],
    timeout: float | None = None,
    ios_api_url: str | None = None,
) -> list[ToolHealth]:
    """Validate each enabled tool, prefer discover cross-check.

    Strategy:
      1. Fetch ``GET /api/discover`` (8766) for tools whose
         ``discover_source == "hermes"``. If it succeeds, build an index of
         the server's self-reported tools and cross-check every such ToolDef.
      2. For tools with ``discover_source == "ios_api"`` (dispatched via the
         generic 8900 bridge), fetch ``GET /api/tools`` on ``ios_api_url`` and
         cross-check the name is present.
      3. Tools without a discover_name (or when discover is unavailable) fall
         back to the dry-run probe.

    Failures do not raise; they are returned as ``ToolHealth(ok=False, ...)``.
    """
    results: list[ToolHealth] = []
    discover_index: dict[str, dict[str, Any]] = {}
    discover_ok = False
    try:
        discovered = client.discover()
        tools = discovered.get("tools")
        if isinstance(tools, list):
            discover_index = {
                str(t.get("name")): t for t in tools if isinstance(t, dict) and t.get("name")
            }
            discover_ok = True
    except Exception as exc:  # noqa: BLE001 — degrade to probe-only
        results.append(ToolHealth("discover", False, f"{type(exc).__name__}: {exc}"))

    # ios_api (8900) tool list for the generic dispatch bridge.
    ios_tools: set[str] = set()
    ios_ok = False
    if ios_api_url:
        try:
            ios_client = HermesToolsClient(ios_api_url, opener=client._opener)
            raw = ios_client._get_json("/api/tools")
            tools_list = raw.get("tools")
            if isinstance(tools_list, list):
                ios_tools = {str(t) for t in tools_list}
                ios_ok = True
        except Exception as exc:  # noqa: BLE001 — degrade to probe-only
            results.append(ToolHealth("ios_api", False, f"{type(exc).__name__}: {exc}"))

    for tool in enabled_tools:
        # ios_api tools: cross-check against the 8900 /api/tools list.
        if tool.discover_source == "ios_api":
            if ios_ok and tool.discover_name:
                if tool.discover_name not in ios_tools:
                    results.append(
                        ToolHealth(
                            tool.name,
                            False,
                            f"{tool.discover_name!r} missing from ios /api/tools",
                        )
                    )
                    continue
                results.append(ToolHealth(tool.name, True, f"ios_api:{tool.discover_name}"))
                continue
            # ios discover unavailable: probe dry-run via call_tool.
            probe_ok = _probe_generic_tool(client, tool, timeout)
            results.append(
                ToolHealth(tool.name, probe_ok, "ios probe" if probe_ok else "ios probe failed")
            )
            continue

        # Cross-check against the 8766 discover payload when we have one
        # and the tool declares a discover_name.
        if discover_ok and tool.discover_name:
            if tool.discover_name not in discover_index:
                results.append(
                    ToolHealth(
                        tool.name,
                        False,
                        f"discover_name {tool.discover_name!r} missing from /api/discover",
                    )
                )
                continue
            server_tool = discover_index[tool.discover_name]
            endpoint = str(server_tool.get("endpoint", ""))
            results.append(ToolHealth(tool.name, True, f"discover:{endpoint}"))
            continue

        # Dry-run probe fallback (also covers tools without discover_name).
        probe = _DRY_RUN_PROBES.get(tool.name)
        if probe is None:
            results.append(ToolHealth(tool.name, True, "no probe defined"))
            continue
        path, params = probe
        saved_timeout = client._timeout
        if timeout is not None:
            client._timeout = timeout
        try:
            raw = client._get_json(path, params=params)
        except Exception as exc:  # noqa: BLE001 — surface any probe failure
            results.append(ToolHealth(tool.name, False, f"{type(exc).__name__}: {exc}"))
            continue
        finally:
            client._timeout = saved_timeout
        if not _looks_like_response(raw):
            results.append(ToolHealth(tool.name, False, f"unexpected shape: {raw!r:.80}"))
            continue
        results.append(ToolHealth(tool.name, True))
    return results
