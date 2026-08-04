from yrobot.wake import WakeGate


def test_wake_phrase_opens_window():
    gate = WakeGate(phrase="你好大白", window_s=10.0, enabled=True)

    assert gate.awake(100.0) is False
    assert gate.observe_text("你好大白", 100.0) is True
    assert gate.awake(109.9) is True
    assert gate.awake(110.1) is False


def test_wake_phrase_matches_without_spaces():
    gate = WakeGate(phrase="你好大白", window_s=10.0, enabled=True)

    assert gate.observe_text("你好，大白。", 100.0) is True


def test_non_wake_text_does_not_open_window():
    gate = WakeGate(phrase="你好大白", window_s=10.0, enabled=True)

    assert gate.observe_text("打开书台灯", 100.0) is False
    assert gate.awake(100.0) is False


def test_disabled_gate_allows_everything():
    gate = WakeGate(phrase="你好大白", window_s=10.0, enabled=False)

    assert gate.awake(100.0) is True
    assert gate.observe_text("旁边聊天", 100.0) is True
