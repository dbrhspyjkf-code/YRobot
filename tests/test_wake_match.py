"""Wake-word matching must not fire on ambient conversation substrings."""

from yrobot.main import _wake_match


def test_canonical_wake_phrase_matches():
    assert _wake_match("你好，小白。") is not None
    assert _wake_match("你好小白") is not None
    assert _wake_match("小白。") is not None


def test_address_then_question_matches():
    assert _wake_match("你好小白，明天天气怎么样？") is not None


def test_ambient_substring_does_not_wake():
    assert _wake_match("我要小白。") is None
    assert _wake_match("小白猫很可爱。") is None
    assert _wake_match("我家小狗叫小白呢") is None


def test_single_interjection_never_wakes():
    assert _wake_match("嘿") is None
    assert _wake_match("嘿，你们看这个。") is None


def test_ambient_conversation_never_wakes():
    assert _wake_match("他冷藏，不是说他不知道是他冷藏太久。") is None
    assert _wake_match("太对，你继续再等他。") is None
    assert _wake_match("你好谁。") is None
    assert _wake_match("两小孩。") is None


def test_english_wake_phrases_exact_only():
    assert _wake_match("hey reachy") is not None
    assert _wake_match("Hey Reachy!") is not None
    assert _wake_match("reachy") is not None
    assert _wake_match("I read a reachy article yesterday") is None
