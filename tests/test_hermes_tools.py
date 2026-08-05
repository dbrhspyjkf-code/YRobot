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
