import json

from yrobot.config import Settings
from yrobot.hermes_tools import HermesToolsController


class FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def test_deepseek_balance_phrase_calls_http_endpoint():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "ok": True,
                "currency": "CNY",
                "total_balance": "57.28",
            }
        )

    settings = Settings(hermes_tools_enabled=True, hermes_tools_url="http://hermes.local:8766")
    controller = HermesToolsController.from_settings(settings, opener=opener)

    result = controller.handle_text("我帮你查一下DeepSeek余额。", "resp-1")

    assert result is not None
    assert result.ok is True
    assert result.name == "DeepSeek余额"
    assert result.message == "DeepSeek 余额：57.28 CNY"
    assert captured == {
        "url": "http://hermes.local:8766/api/deepseek/balance",
        "timeout": 5.0,
    }


def test_split_deepseek_balance_phrase_matches_after_accumulation():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return FakeResponse(
            {
                "ok": True,
                "currency": "CNY",
                "total_balance": "57.28",
            }
        )

    settings = Settings(hermes_tools_enabled=True, hermes_tools_url="http://hermes.local:8766")
    controller = HermesToolsController.from_settings(settings, opener=opener)

    assert controller.handle_text("Deep", "resp-1") is None
    result = controller.handle_text("Seek余额。", "resp-1")

    assert result is not None
    assert result.ok is True
    assert calls == ["http://hermes.local:8766/api/deepseek/balance"]


def test_duplicate_deepseek_balance_response_does_not_call_twice():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return FakeResponse(
            {
                "ok": True,
                "currency": "CNY",
                "total_balance": "57.28",
            }
        )

    settings = Settings(hermes_tools_enabled=True, hermes_tools_url="http://hermes.local:8766")
    controller = HermesToolsController.from_settings(settings, opener=opener)

    assert controller.handle_text("DeepSeek余额", "resp-1") is not None
    assert controller.handle_text("DeepSeek余额", "resp-1") is None
    assert calls == ["http://hermes.local:8766/api/deepseek/balance"]


def test_duplicate_deepseek_balance_is_debounced_across_responses():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return FakeResponse(
            {
                "ok": True,
                "currency": "CNY",
                "total_balance": "57.28",
            }
        )

    settings = Settings(hermes_tools_enabled=True, hermes_tools_url="http://hermes.local:8766")
    controller = HermesToolsController.from_settings(settings, opener=opener)

    assert controller.handle_text("DeepSeek余额", "resp-1") is not None
    assert controller.handle_text("DeepSeek余额", "resp-2") is None
    assert calls == ["http://hermes.local:8766/api/deepseek/balance"]


def test_disabled_hermes_tools_ignore_text():
    controller = HermesToolsController.from_settings(Settings(hermes_tools_enabled=False))

    assert controller.handle_text("DeepSeek余额", "resp-1") is None


def _extract(name):
    """Helper that imports the private extractor."""
    from yrobot.hermes_tools import _extract_stock_name
    return _extract_stock_name(name)


def test_extract_stock_name_supports_x_stock_format():
    """User says '平安股票怎么样' — must extract '平安', not ''."""
    assert _extract("平安股票怎么样") == "平安"
    assert _extract("平安股票价格多少") == "平安"
    assert _extract("平安股票分析") == "平安"
    assert _extract("平安股票") == "平安"


def test_extract_stock_name_strips_common_verbs():
    assert _extract("查询贵州茅台分析") == "贵州茅台"
    assert _extract("宁德时代现在多少钱") == "宁德时代"
    assert _extract("比亚迪怎么样") == "比亚迪"
    assert _extract("我想买比亚迪能买吗") == "比亚迪"


def test_extract_stock_name_accepts_six_digit_code():
    assert _extract("600519股价多少") == "600519"


def test_extract_stock_name_rejects_portfolio_queries():
    """'我的股票' is the portfolio path, must not extract a name."""
    assert _extract("我的股票") == ""
    assert _extract("自选股分析") == ""
    assert _extract("持仓怎么样") == ""
    assert _extract("股票") == ""
    assert _extract("股票怎么样") == ""


def test_extract_stock_name_rejects_bad_length():
    assert _extract("A") == ""
    assert _extract("abcdefghij") == ""


def test_stock_code_with_price_keyword_routes_to_stock_price():
    """User says '688018的价格' — must call /api/stocks/price, not portfolio."""
    captured = []

    def opener(request, **kwargs):
        from urllib.parse import urlsplit, parse_qs
        url = urlsplit(request.full_url)
        captured.append({"path": url.path, "params": dict(parse_qs(url.query))})
        body = (
            b'{"ok": true, "name": "688018", "price": 152.30, '
            b'"change_pct": 2.5}'
        )

        class FakeResp:
            def __init__(self, body): self._body = body

            def __enter__(self): return self

            def __exit__(self, *a): return False

            def read(self): return self._body

        return FakeResp(body)

    controller = HermesToolsController.from_settings(
        Settings(hermes_tools_enabled=True, hermes_tools_url="http://hermes.local:8766"),
        opener=opener,
    )

    cases = [
        "688018的价格",
        "688018股价多少",
        "688018价格",
        "688018行情",
        "平安股票价格多少",
        "比亚迪怎么样",
        "688018能买吗",
    ]
    for i, text in enumerate(cases):
        result = controller.handle_text(text, f"resp-stock-{i}")
        assert result is not None, f"{text!r} should fire a tool"
        assert result.name in ("stock_price", "stock_advice"), (
            f"{text!r} should fire stock_price or stock_advice, got {result.name}"
        )
        assert captured[-1]["path"] in ("/api/stocks/price", "/api/stocks/advice"), (
            f"{text!r} hit wrong endpoint: {captured[-1]['path']}"
        )
        # Reset cooldown between cases so each call is independent.
        controller._last_fired_at.clear()


def test_bare_stock_word_does_not_fire_portfolio():
    """Model says '想查询的股票' — must NOT fire portfolio.

    This was the root cause of the 688018 bug: model's clarification triggered
    the portfolio tool, hiding the single-stock query.
    """
    calls = []

    def opener(request, **kwargs):
        calls.append(request.full_url)

        class FakeResp:
            def __init__(self, body): self._body = b"{}"

            def __enter__(self): return self

            def __exit__(self, *a): return False

            def read(self): return self._body

        return FakeResp(None)

    controller = HermesToolsController.from_settings(
        Settings(hermes_tools_enabled=True, hermes_tools_url="http://hermes.local:8766"),
        opener=opener,
    )

    for text in ("好的，请告诉我您想查询的股票", "哪只股票", "股票", "股票行情"):
        # Use a fresh response_id so cooldown doesn't matter.
        result = controller.handle_text(text, f"resp-bare-{hash(text)}")
        assert result is None, f"{text!r} must not fire any tool, got {result.name}"


def test_portfolio_phrases_route_to_portfolio():
    portfolio_phrases = (
        "我的股票", "自选股怎么样", "股票怎么样",
        "持有的股票", "我关注的股票",
    )
    for text in portfolio_phrases:
        from yrobot.hermes_tools import _is_portfolio_intent
        assert _is_portfolio_intent(text), f"{text!r} should be portfolio intent"


def test_stock_advice_also_accepts_code_with_advice_keyword():
    from yrobot.hermes_tools import _has_stock_code
    text = "688018怎么样"
    assert _has_stock_code(text)
    assert any(kw in text for kw in ("怎么样", "建议", "分析", "能买"))
