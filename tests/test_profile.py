"""Profile-driven tool whitelist and instructions."""

import os
import sys

from yrobot.hermes_tools import HermesToolsController, Settings
from yrobot.profile import (
    DEFAULT_PROFILE_NAME,
    PACKAGE_PROFILES_DIR,
    load_profile,
    list_profiles,
)


class FakeResp:
    def __init__(self, body): self._body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._body


def _opener(request, **kwargs):
    return FakeResp(b'{"ok": true, "holdings": []}')


def test_shipped_profiles_present():
    names = list_profiles()
    assert "default" in names
    assert "family_safe" in names
    assert "developer" in names


def test_default_profile_allows_all_hermes_tools():
    p = load_profile("default")
    for name in (
        "weather",
        "stock_price",
        "stock_advice",
        "stocks",
        "rate",
        "deepseek_balance",
    ):
        assert p.allows(name), f"default profile should allow {name}"


def test_family_safe_profile_excludes_sensitive_tools():
    p = load_profile("family_safe")
    assert p.allows("weather")
    assert p.allows("stock_price")
    assert p.allows("stock_advice")
    assert p.allows("rate")
    assert not p.allows("stocks"), "family_safe must not expose portfolio"
    assert not p.allows("deepseek_balance"), "family_safe must not expose balance"


def test_family_safe_profile_has_instructions():
    p = load_profile("family_safe")
    assert p.instructions, "family_safe profile must ship with instructions"
    assert "家庭" in p.instructions or "友好" in p.instructions


def test_profile_filters_tool_dispatch():
    p = load_profile("family_safe")
    c = HermesToolsController.from_settings(
        Settings(hermes_tools_enabled=True, hermes_tools_url="http://h.local:8766"),
        opener=_opener,
        profile=p,
    )
    # '我的股票' would normally hit stocks (portfolio), but family_safe bans it.
    assert c.handle_text("我的股票", "resp-1") is None
    # 'DeepSeek余额' similarly banned.
    assert c.handle_text("DeepSeek余额", "resp-2") is None
    # '天气' is allowed.
    r = c.handle_text("天气", "resp-3")
    assert r is not None and r.name == "weather"


def test_profile_does_not_filter_when_unset():
    c = HermesToolsController.from_settings(
        Settings(hermes_tools_enabled=True, hermes_tools_url="http://h.local:8766"),
        opener=_opener,
    )
    r = c.handle_text("DeepSeek余额", "resp-1")
    assert r is not None and r.name == "deepseek_balance"


def test_missing_profile_raises_helpful_error():
    try:
        load_profile("does_not_exist")
    except FileNotFoundError as exc:
        assert "does_not_exist" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_settings_load_profile_instructions_into_prompt():
    from yrobot.config import Settings

    s = Settings(profile_name="family_safe")
    assert "家庭" in s.effective_system_prompt or "友好" in s.effective_system_prompt


def test_env_var_profile_name():
    from yrobot.config import Settings
    from unittest.mock import patch
    with patch.dict(os.environ, {"YROBOT_PROFILE": "family_safe"}, clear=False):
        s = Settings.from_env()
    assert s.profile_name == "family_safe"
