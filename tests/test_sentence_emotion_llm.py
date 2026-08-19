"""LLM sentence-emotion fallback tier (DashScope qwen-flash) — tests.

The classifier must be total (never raise), time-bounded, and normalise its
output to the protocol emotion vocabulary. Network access itself is mocked:
only ``_http_chat_completion`` is patched, everything else is real code.
"""

from __future__ import annotations

import pytest

from yrobot import sentence_emotion_llm as mod


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.delenv("YROBOT_LLM_EMOTION", raising=False)


def test_classifies_valid_emotion(monkeypatch):
    monkeypatch.setattr(mod, "_http_chat_completion", lambda key, text: "surprised")
    assert mod.classify_sentence_llm("没想到结果居然是这样") == "surprised"


def test_normalises_whitespace_and_case(monkeypatch):
    monkeypatch.setattr(mod, "_http_chat_completion", lambda key, text: "  Sad \n")
    assert mod.classify_sentence_llm("很遗憾听到这个消息") == "sad"


def test_extra_words_in_reply_reduce_to_first_token(monkeypatch):
    monkeypatch.setattr(mod, "_http_chat_completion", lambda key, text: "happy。")
    assert mod.classify_sentence_llm("哈哈") == "happy"


def test_non_vocabulary_word_maps_to_none(monkeypatch):
    # Chinese replies (like the observed "快乐") must never leak through.
    monkeypatch.setattr(mod, "_http_chat_completion", lambda key, text: "快乐")
    assert mod.classify_sentence_llm("哈哈") is None


def test_http_failure_returns_none(monkeypatch):
    def boom(key, text):
        raise OSError("network down")

    monkeypatch.setattr(mod, "_http_chat_completion", boom)
    assert mod.classify_sentence_llm("随便一句") is None


def test_missing_api_key_returns_none(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    called = []
    monkeypatch.setattr(mod, "_http_chat_completion", lambda key, text: called.append(1))
    assert mod.classify_sentence_llm("随便一句") is None
    assert called == []


def test_env_switch_disables(monkeypatch):
    monkeypatch.setenv("YROBOT_LLM_EMOTION", "0")
    called = []
    monkeypatch.setattr(mod, "_http_chat_completion", lambda key, text: called.append(1))
    assert mod.classify_sentence_llm("随便一句") is None
    assert called == []


def test_empty_text_returns_none(monkeypatch):
    monkeypatch.setattr(mod, "_http_chat_completion", lambda key, text: "happy")
    assert mod.classify_sentence_llm("") is None
    assert mod.classify_sentence_llm("   ") is None
