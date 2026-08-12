import json

from yrobot.command_recognizer import CommandRecognizer
from yrobot.config import Settings
from yrobot.qwen_tools import ToolExecutor
from tests.test_qwen_tools import FakeResponse, RecordingOpener, make_settings_with_hermes


class CommandOpener:
    def __init__(self, document):
        self.document = document
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        return FakeResponse(self.document)


def test_command_recognizer_disabled_returns_none(tmp_path):
    recognizer = CommandRecognizer(Settings())

    assert recognizer.recognize(b"wav") is None


def test_command_recognizer_posts_wav_and_returns_text_command():
    opener = CommandOpener({"ok": True, "command_text": "音响音量调大"})
    settings = Settings(command_recognizer_enabled=True, command_recognizer_url="http://asr.local/recognize")
    recognizer = CommandRecognizer(settings, opener=opener)

    assert recognizer.recognize(b"wav-bytes") == "音响音量调大"
    request, timeout = opener.calls[0]
    assert request.full_url == "http://asr.local/recognize"
    assert request.get_method() == "POST"
    assert request.data == b"wav-bytes"
    assert request.headers["Content-type"] == "audio/wav"
    assert timeout == 3.0


def test_command_recognizer_rejects_non_allowlisted_response():
    opener = CommandOpener({"ok": True, "command_text": "打开大门"})
    settings = Settings(command_recognizer_enabled=True, command_recognizer_url="http://asr.local/recognize")
    recognizer = CommandRecognizer(settings, opener=opener)

    assert recognizer.recognize(b"wav") is None


def test_command_recognizer_command_executes_existing_tool_path(tmp_path):
    asr = CommandOpener({"ok": True, "command_text": "音响音量调大"})
    settings = make_settings_with_hermes(tmp_path)
    settings = Settings(
        conversation_backend="qwen",
        ha_enabled=True,
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
        ha_whitelist_path=settings.ha_whitelist_path,
        hermes_tools_enabled=True,
        command_recognizer_enabled=True,
        command_recognizer_url="http://asr.local/recognize",
    )
    recognizer = CommandRecognizer(settings, opener=asr)
    command = recognizer.recognize(b"wav")

    assert command == "音响音量调大"
