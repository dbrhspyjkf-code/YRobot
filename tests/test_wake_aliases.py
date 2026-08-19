"""Wake-phrase ASR alias coverage."""

from yrobot.main import _wake_match


def test_canonical_phrase_wakes():
    assert _wake_match("你好小白") == "你好小白"
    assert _wake_match("你好小白，明天天气怎么样") == "你好小白"


def test_existing_asr_alias_wakes():
    assert _wake_match("你好小孩") == "你好小孩"


def test_swallowed_character_variant_wakes():
    """Field report 2026-08-19 23:08: ASR heard 你小白。 for 你好小白 — the
    wake was lost and the whole turn fell into the ambient (mute, unlogged)
    path. The swallowed-好 variant must wake too."""
    assert _wake_match("你小白。") == "你小白"
    assert _wake_match("你小白") == "你小白"


def test_longer_utterance_with_variant_does_not_wake():
    """Aliases are exact-match only: no 你好小白…-style prefix extension."""
    assert _wake_match("你小白帮我看看天气") is None
