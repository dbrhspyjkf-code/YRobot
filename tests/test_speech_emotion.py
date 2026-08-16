"""Keyword sentence-emotion classifier for spoken XIAOZHI replies."""

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
