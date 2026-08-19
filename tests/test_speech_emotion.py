"""Keyword sentence-emotion classifier for spoken XIAOZHI replies."""

import pytest

from yrobot.speech_emotion import sentence_emotion


def test_happy_from_laughter():
    assert sentence_emotion("哈哈，这个太好笑了！") == "happy"


def test_surprised_from_exclamation():
    assert sentence_emotion("哇！居然是这样的") == "surprised"


def test_thinking_from_deliberation():
    assert sentence_emotion("嗯，让我想想看该怎么做") == "thinking"


def test_sad_from_apology():
    assert sentence_emotion("真的很抱歉，这次没能帮上忙") == "sad"


def test_grateful_from_thanks():
    assert sentence_emotion("谢谢你告诉我这些") == "grateful"


def test_plain_sentence_has_no_emotion():
    assert sentence_emotion("今天天气不错，适合出去走走") is None


def test_empty_text_is_none():
    assert sentence_emotion("") is None
    assert sentence_emotion("   ") is None


# --- Expanded vocabulary (2026-08-19): wider synonyms + two new groups. ---


@pytest.mark.parametrize(
    "text,expected",
    [
        # wider synonyms for existing groups
        ("嘿嘿，有意思", "happy"),
        ("真的假的？", "surprised"),
        ("什么情况这是", "surprised"),
        ("容我想想再回答你", "thinking"),
        ("这次实验失败了，有点失望", "sad"),
        ("烦死了，怎么又出错", "angry"),
        ("摸摸头，我最喜欢你了", "loving"),
        ("辛苦啦，多谢帮忙", "grateful"),
        ("吓坏了，刚才好险", "scared"),
        ("放心，稳稳的没问题", "confident"),
        ("困得不行，想眯一会", "sleepy"),
        ("太燃了，好激动", "excited"),
        # new group: confused
        ("抱歉，我没听懂你的意思", "confused"),
        ("什么意思？再说一遍", "confused"),
        # new group: welcoming
        ("欢迎回来，很高兴见到你", "welcoming"),
        # new group: laughing (distinct from mild happy)
        ("笑死我了，太好笑了", "laughing"),
    ],
)
def test_expanded_keywords(text, expected):
    assert sentence_emotion(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "好的，我知道了",
        "明白了，马上处理",
        "嗯嗯，你继续说",
        "现在是晚上八点",
    ],
)
def test_high_frequency_fillers_stay_silent(text):
    # Acknowledgements and neutral facts must NOT gesture (over-trigger guard).
    assert sentence_emotion(text) is None
