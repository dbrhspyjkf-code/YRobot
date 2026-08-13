from yrobot.app_config import CURRENT_PROCESS_LOG_PATH, LogReader


def test_log_reader_default_matches_detached_yrobot_log_path():
    assert CURRENT_PROCESS_LOG_PATH.as_posix() == "/tmp/yrobot-run/yrobot.log"


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


def test_log_reader_scans_past_high_frequency_camera_logs_for_chat(tmp_path):
    log_path = tmp_path / "yrobot.log"
    log_path.write_text(
        "2026-08-12 00:01:01,100 I __main__: qwen stt: 今天深圳天气怎么样？\n"
        "2026-08-12 00:01:02,200 I __main__: qwen response: 深圳天气晴。\n"
        + "INFO:     GET /api/camera/frame 200 OK\n" * 300,
        encoding="utf-8",
    )
    reader = LogReader(log_path=log_path)

    entries, error = reader.read(200, "info", filter_kind="chat")

    assert error is None
    assert [entry["message"] for entry in entries] == [
        "qwen stt: 今天深圳天气怎么样？",
        "qwen response: 深圳天气晴。",
    ]


def test_log_reader_gives_plain_process_lines_current_timestamps(tmp_path):
    log_path = tmp_path / "yrobot-main.log"
    log_path.write_text("INFO:     Application startup complete.\n", encoding="utf-8")
    reader = LogReader(log_path=log_path)

    entries, error = reader.read(20, "info")

    assert error is None
    assert entries[0]["timestamp_us"] > 1_700_000_000_000_000
