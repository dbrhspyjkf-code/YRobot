from pathlib import Path

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
