from yrobot.app_config import LogReader


def test_log_reader_reads_current_process_log_file(tmp_path):
    log_path = tmp_path / "yrobot-main.log"
    log_path.write_text(
        "2026-08-12 00:01:01,100 I __main__: qwen stt: 音响音量调大一下\n"
        "2026-08-12 00:01:02,200 I __main__: qwen response: 好的\n",
        encoding="utf-8",
    )
    reader = LogReader(log_path=log_path)

    entries, error = reader.read(20, "info", filter_kind="chat")

    assert error is None
    assert [entry["message"] for entry in entries] == [
        "qwen stt: 音响音量调大一下",
        "qwen response: 好的",
    ]


def test_log_reader_skips_empty_chat_lines(tmp_path):
    log_path = tmp_path / "yrobot-main.log"
    log_path.write_text(
        "2026-08-12 00:01:01,100 I __main__: qwen stt: \n"
        "2026-08-12 00:01:02,200 I __main__: qwen response: 好的\n",
        encoding="utf-8",
    )
    reader = LogReader(log_path=log_path)

    entries, error = reader.read(20, "info", filter_kind="chat")

    assert error is None
    assert [entry["message"] for entry in entries] == ["qwen response: 好的"]


def test_log_reader_gives_plain_process_lines_current_timestamps(tmp_path):
    log_path = tmp_path / "yrobot-main.log"
    log_path.write_text("INFO:     Application startup complete.\n", encoding="utf-8")
    reader = LogReader(log_path=log_path)

    entries, error = reader.read(20, "info")

    assert error is None
    assert entries[0]["timestamp_us"] > 1_700_000_000_000_000
