"""Session rotation and continuity-hint tests."""

from yrobot.session import ConversationMemory, RotationPolicy


def test_rotation_waits_for_quiet_boundary():
    policy = RotationPolicy(time_budget_s=280, kv_budget=7200)
    assert not policy.should_rotate(elapsed_s=281, kv_tokens=100, quiet=False)
    assert policy.should_rotate(elapsed_s=281, kv_tokens=100, quiet=True)
    assert policy.should_rotate(elapsed_s=311, kv_tokens=100, quiet=False)


def test_rotation_uses_kv_budget():
    policy = RotationPolicy(time_budget_s=280, kv_budget=7200)
    assert not policy.should_rotate(elapsed_s=100, kv_tokens=7201, quiet=False)
    assert policy.should_rotate(elapsed_s=100, kv_tokens=7201, quiet=True)


def test_kv_budget_gets_the_same_bounded_grace_as_time_budget():
    policy = RotationPolicy(time_budget_s=280, kv_budget=7200, grace_s=30)
    assert not policy.should_rotate(elapsed_s=100, kv_tokens=7201, quiet=False)
    assert not policy.should_rotate(elapsed_s=129, kv_tokens=8000, quiet=False)
    assert policy.should_rotate(elapsed_s=130, kv_tokens=8000, quiet=False)


def test_rotation_policy_resets_between_sessions():
    policy = RotationPolicy(time_budget_s=1, kv_budget=7200, grace_s=30)
    assert not policy.should_rotate(elapsed_s=2, kv_tokens=0, quiet=False)
    policy.reset()
    assert not policy.should_rotate(elapsed_s=2, kv_tokens=0, quiet=False)


def test_conversation_memory_is_bounded_and_transport_transparent():
    memory = ConversationMemory(max_chars=24)
    memory.append_assistant("第一段回答。")
    memory.append_assistant("第二段比较长的回答。")
    prompt = memory.prompt("base")
    assert prompt.startswith("base\n")
    assert "Continue naturally" in prompt
    assert "第二段" in prompt
    assert len(prompt.rsplit(": ", 1)[-1]) <= 24


def test_empty_memory_does_not_change_prompt():
    assert ConversationMemory().prompt("base") == "base"
