"""Unit tests for environment-driven settings."""

import pytest

from yrobot.config import DEFAULT_REALTIME_URL, TRAINED_SYSTEM_LINE, Settings, normalize_url


def test_defaults_target_official_gateway():
    s = Settings()
    assert s.url == DEFAULT_REALTIME_URL
    assert s.chunk_ms == 1000
    assert s.send_video is True
    assert s.realtime_mode == "video"
    assert s.proactive_enabled is True
    assert s.system_prompt.startswith(TRAINED_SYSTEM_LINE + "\n")
    assert len(s.effective_system_prompt) > len(s.system_prompt)


def test_home_assistant_defaults_disabled():
    settings = Settings()
    assert settings.ha_enabled is False
    assert settings.ha_url is None
    assert settings.ha_token is None
    assert settings.ha_whitelist_path == "~/.config/yrobot/home_assistant_whitelist.json"


def test_application_env_defaults_target_official_gateway(monkeypatch):
    monkeypatch.delenv("YROBOT_REALTIME_URL", raising=False)
    monkeypatch.delenv("YROBOT_REALTIME_MODE", raising=False)
    assert Settings.from_env().url == DEFAULT_REALTIME_URL


def test_from_env_accepts_an_explicit_mapping():
    settings = Settings.from_env(
        {
            "YROBOT_REALTIME_URL": "gateway.example.test",
            "YROBOT_SEND_VIDEO": "0",
            "YROBOT_PERSONA": "Be concise.",
        }
    )
    assert settings.url == "wss://gateway.example.test/v1/realtime?mode=audio"
    assert settings.send_video is False
    assert settings.system_prompt.endswith("Be concise.")


def test_home_assistant_env_overrides(monkeypatch):
    monkeypatch.setenv("YROBOT_HA_ENABLED", "1")
    monkeypatch.setenv("YROBOT_HA_URL", "http://192.168.1.133:8123/")
    monkeypatch.setenv("YROBOT_HA_TOKEN", "secret-token")
    monkeypatch.setenv("YROBOT_HA_WHITELIST_PATH", "/home/pollen/ha.json")

    settings = Settings.from_env()

    assert settings.ha_enabled is True
    assert settings.ha_url == "http://192.168.1.133:8123"
    assert settings.ha_token == "secret-token"
    assert settings.ha_whitelist_path == "/home/pollen/ha.json"


def test_home_assistant_prompt_requires_exact_device_action(monkeypatch):
    monkeypatch.setenv("YROBOT_HA_ENABLED", "1")
    monkeypatch.setenv("YROBOT_HA_URL", "http://192.168.1.133:8123")
    monkeypatch.setenv("YROBOT_HA_TOKEN", "secret-token")

    prompt = Settings.from_env().effective_system_prompt

    assert "家电控制" in prompt
    assert "完整设备名" in prompt
    assert "关闭书台灯" in prompt


@pytest.mark.parametrize("url", ["https://example.com", "file:///tmp/socket", "wss:///missing"])
def test_realtime_url_requires_a_websocket_host(url):
    with pytest.raises(ValueError, match="ws://|host"):
        normalize_url(url)


def test_legacy_send_video_flag_can_select_audio_without_url(monkeypatch):
    monkeypatch.delenv("YROBOT_REALTIME_URL", raising=False)
    monkeypatch.delenv("YROBOT_REALTIME_MODE", raising=False)
    monkeypatch.setenv("YROBOT_SEND_VIDEO", "false")
    assert Settings.from_env().url == DEFAULT_REALTIME_URL.replace("mode=video", "mode=audio")


def test_from_env_overrides(monkeypatch):
    monkeypatch.setenv("YROBOT_REALTIME_URL", "10.0.16.184:8006")
    monkeypatch.setenv("YROBOT_TLS_VERIFY", "0")
    monkeypatch.setenv("YROBOT_CHUNK_MS", "1000")
    monkeypatch.setenv("YROBOT_PERSONA", "只说中文。")
    monkeypatch.setenv("YROBOT_SEND_VIDEO", "false")
    monkeypatch.setenv("YROBOT_BARGE_ECHO_SIMILARITY", "0.8")
    monkeypatch.setenv("YROBOT_BARGE_UNEXPLAINED_DB", "-44")
    monkeypatch.setenv("YROBOT_BARGE_CONFIRM_MS", "600")
    s = Settings.from_env()
    assert s.url == "wss://10.0.16.184:8006/v1/realtime?mode=audio"
    assert s.tls_verify is False
    assert s.send_video is False
    assert s.barge_echo_similarity == 0.8
    assert s.barge_unexplained_db == -44.0
    assert s.barge_confirm_ms == 600
    assert s.system_prompt == f"{TRAINED_SYSTEM_LINE}\n只说中文。"


def test_empty_persona_keeps_trained_line_only(monkeypatch):
    monkeypatch.setenv("YROBOT_PERSONA", "  ")
    monkeypatch.setenv("YROBOT_PROACTIVE", "false")
    assert Settings.from_env().system_prompt == TRAINED_SYSTEM_LINE


def test_send_video_selects_video_mode_for_legacy_bare_url(monkeypatch):
    monkeypatch.setenv("YROBOT_REALTIME_URL", "10.0.16.184:8006")
    monkeypatch.setenv("YROBOT_SEND_VIDEO", "true")
    s = Settings.from_env()
    assert s.realtime_mode == "video"
    assert s.session_budget_s == 280.0


def test_explicit_audio_mode_rejects_video_frames(monkeypatch):
    monkeypatch.setenv(
        "YROBOT_REALTIME_URL",
        "wss://10.0.16.184:8006/v1/realtime?mode=audio",
    )
    monkeypatch.setenv("YROBOT_SEND_VIDEO", "true")
    with pytest.raises(ValueError, match="mode=video"):
        Settings.from_env()


def test_explicit_audio_mode_is_a_clean_fallback(monkeypatch):
    monkeypatch.setenv(
        "YROBOT_REALTIME_URL",
        "wss://10.0.16.184:8006/v1/realtime?mode=audio",
    )
    settings = Settings.from_env()
    assert settings.realtime_mode == "audio"
    assert settings.send_video is False
    assert settings.proactive_enabled is False


@pytest.mark.parametrize("chunk_ms", [20, 500, 2000])
def test_chunk_size_must_match_model_inference_unit(chunk_ms):
    with pytest.raises(ValueError, match="must be 1000"):
        Settings(chunk_ms=chunk_ms)


@pytest.mark.parametrize("similarity", [-0.1, 1.1])
def test_echo_similarity_must_be_normalized(similarity):
    with pytest.raises(ValueError, match="between 0 and 1"):
        Settings(barge_echo_similarity=similarity)


@pytest.mark.parametrize("unexplained_db", [-121.0, 0.1])
def test_unexplained_energy_threshold_must_be_decibels(unexplained_db):
    with pytest.raises(ValueError, match="between -120 and 0"):
        Settings(barge_unexplained_db=unexplained_db)


@pytest.mark.parametrize("confirm_ms", [199, 2001])
def test_barge_confirmation_window_is_bounded(confirm_ms):
    with pytest.raises(ValueError, match="between 200 and 2000"):
        Settings(barge_confirm_ms=confirm_ms)


@pytest.mark.parametrize("confirm_ms", [59, 501])
def test_fast_barge_confirmation_must_precede_safe_path(confirm_ms):
    with pytest.raises(ValueError, match="FAST_CONFIRM"):
        Settings(barge_fast_confirm_ms=confirm_ms, barge_confirm_ms=500)
