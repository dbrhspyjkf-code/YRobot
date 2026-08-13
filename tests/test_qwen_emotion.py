from yrobot.qwen_emotion import requested_emotion


def test_requested_emotion_matches_explicit_conversation_intent_only():
    assert requested_emotion("给我一个惊喜") == "surprised"
    assert requested_emotion("讲一个开心的笑话") == "happy"
    assert requested_emotion("这个问题你想一想再回答") == "thinking"
    assert requested_emotion("打开惊喜灯") is None
