from yrobot.persistent_memory import PersistentMemory


def test_remember_command_persists_memory(tmp_path):
    memory = PersistentMemory(tmp_path / "memory.json")

    result = memory.handle_text("记住：我喜欢喝拿铁。", "resp-1")

    assert result is not None
    assert result.ok is True
    assert result.message == "我记住了：我喜欢喝拿铁。"
    assert memory.items() == ["我喜欢喝拿铁。"]


def test_forget_command_removes_matching_memory(tmp_path):
    memory = PersistentMemory(tmp_path / "memory.json")
    memory.add("我喜欢喝拿铁。")
    memory.add("书房灯叫书台灯。")

    result = memory.handle_text("忘掉：拿铁", "resp-2")

    assert result is not None
    assert result.ok is True
    assert result.message == "已忘掉 1 条记忆。"
    assert memory.items() == ["书房灯叫书台灯。"]


def test_recall_command_lists_memories(tmp_path):
    memory = PersistentMemory(tmp_path / "memory.json")
    memory.add("我喜欢喝拿铁。")
    memory.add("书房灯叫书台灯。")

    result = memory.handle_text("我记得什么？", "resp-3")

    assert result is not None
    assert result.ok is True
    assert result.message == "我记得：我喜欢喝拿铁；书房灯叫书台灯。"


def test_duplicate_response_does_not_run_twice(tmp_path):
    memory = PersistentMemory(tmp_path / "memory.json")

    assert memory.handle_text("记住：我喜欢喝拿铁。", "resp-4") is not None
    assert memory.handle_text("记住：我喜欢喝拿铁。", "resp-4") is None
    assert memory.items() == ["我喜欢喝拿铁。"]


def test_model_acknowledgement_marker_persists_memory_after_split_text(tmp_path):
    memory = PersistentMemory(tmp_path / "memory.json")

    assert memory.handle_text("好的，记", "resp-5") is None
    assert memory.handle_text("住了：您喜", "resp-5") is None
    result = memory.handle_text("欢喝冰美式。", "resp-5")

    assert result is not None
    assert result.message == "我记住了：您喜欢喝冰美式。"
    assert memory.items() == ["您喜欢喝冰美式。"]


def test_prompt_context_contains_bounded_memories(tmp_path):
    memory = PersistentMemory(tmp_path / "memory.json")
    memory.add("我喜欢喝拿铁。")

    prompt = memory.prompt_context()

    assert "Long-term local memory" in prompt
    assert "我喜欢喝拿铁。" in prompt
