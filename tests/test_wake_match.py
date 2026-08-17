"""Wake-word matching: ONLY 你好小白 may wake the robot (user directive)."""

from yrobot.main import _wake_match


def test_canonical_wake_phrase_matches():
    assert _wake_match("你好，小白。") is not None
    assert _wake_match("你好小白") is not None


def test_address_then_question_matches():
    assert _wake_match("你好小白，明天天气怎么样？") is not None
    assert _wake_match("你好小白今天几号") is not None


def test_everything_else_does_not_wake():
    assert _wake_match("小白。") is None
    assert _wake_match("我要小白。") is None
    assert _wake_match("小白猫很可爱。") is None
    assert _wake_match("阿皮。") is None
    assert _wake_match("reachy") is None
    assert _wake_match("hey reachy") is None
    assert _wake_match("hello reachy") is None
    assert _wake_match("嘿") is None
    assert _wake_match("嘿，你们看这个。") is None


def test_ambient_conversation_never_wakes():
    assert _wake_match("他冷藏，不是说他不知道是他冷藏太久。") is None
    assert _wake_match("太对，你继续再等他。") is None
    assert _wake_match("你好谁。") is None
    assert _wake_match("两小孩。") is None
    assert _wake_match("你好。") is None


def test_empty_is_none():
    assert _wake_match("") is None
    assert _wake_match("。。。") is None
