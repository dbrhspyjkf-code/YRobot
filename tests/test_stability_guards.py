from pathlib import Path

from yrobot.audio_runtime import BoundedLatestQueue, TtsWatchdog
from yrobot.env_store import update_env_value


def test_update_env_value_preserves_other_settings(tmp_path: Path):
    path = tmp_path / "ha.env"
    path.write_text(
        "# managed settings\nYROBOT_HA_TOKEN=secret-token\nYROBOT_VAD_RMS_MIN=0.065\n",
        encoding="utf-8",
    )

    update_env_value(path, "YROBOT_VAD_RMS_MIN", "0.081")

    assert path.read_text(encoding="utf-8") == (
        "# managed settings\nYROBOT_HA_TOKEN=secret-token\nYROBOT_VAD_RMS_MIN=0.081\n"
    )


def test_update_env_value_appends_missing_key(tmp_path: Path):
    path = tmp_path / "settings.env"
    path.write_text("OTHER=value", encoding="utf-8")

    update_env_value(path, "YROBOT_VAD_RMS_MIN", "0.081")

    assert path.read_text(encoding="utf-8") == "OTHER=value\nYROBOT_VAD_RMS_MIN=0.081\n"


def test_bounded_latest_queue_drops_oldest_item():
    audio = BoundedLatestQueue[str](maxsize=2)
    audio.put_latest("first")
    audio.put_latest("second")
    audio.put_latest("latest")

    assert audio.qsize() == 2
    assert audio.get() == "second"
    assert audio.get() == "latest"
    assert audio.dropped == 1


def test_tts_watchdog_detects_startup_and_midstream_stalls():
    watchdog = TtsWatchdog(no_packet_timeout=3.0, packet_gap_timeout=2.0, max_duration=10.0)
    watchdog.start(now=100.0)
    assert not watchdog.stalled(now=102.9)
    assert watchdog.stalled(now=103.0)

    watchdog.start(now=200.0)
    watchdog.packet(now=201.0)
    assert not watchdog.stalled(now=202.9)
    assert watchdog.stalled(now=203.0)


def test_tts_watchdog_enforces_total_duration():
    watchdog = TtsWatchdog(no_packet_timeout=30.0, packet_gap_timeout=30.0, max_duration=5.0)
    watchdog.start(now=10.0)
    watchdog.packet(now=11.0)

    assert watchdog.stalled(now=15.0)
