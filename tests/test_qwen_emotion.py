from yrobot.qwen_emotion import IdentityStabilizer, requested_dance, requested_emotion


def test_requested_emotion_matches_explicit_conversation_intent_only():
    assert requested_emotion("给我一个惊喜") == "surprised"
    assert requested_emotion("讲一个开心的笑话") == "happy"
    assert requested_emotion("这个问题你想一想再回答") == "thinking"
    assert requested_emotion("打开惊喜灯") is None


def test_requested_dance_uses_explicit_commands_only():
    assert requested_dance("小白跳个舞") == ("play", "simple_nod")
    assert requested_dance("小白跳开心舞") == ("play", "yeah_nod")
    assert requested_dance("停止跳舞") == ("stop", None)
    assert requested_dance("今天深圳天气怎么样") is None


def test_identity_stabilizer_announces_a_new_face_once_after_three_seconds():
    identities = IdentityStabilizer(stable_after_s=3.0)

    assert identities.observe("阿皮", now=10.0) is None
    assert identities.observe("阿皮", now=12.9) is None
    assert identities.observe("阿皮", now=13.1) == "阿皮"
    assert identities.observe("阿皮", now=20.0) is None
