from yrobot.audio import AUDIO_STARTUP_CONFIG, apply_audio_startup_config


class ConfigurableAudio:
    def __init__(self):
        self.calls = []

    def apply_audio_config(self, config, *, verify, write_settle_seconds):
        self.calls.append((config, verify, write_settle_seconds))
        return True


class Media:
    def __init__(self, audio):
        self.audio = audio


def test_applies_reachy_audio_startup_profile():
    audio = ConfigurableAudio()

    assert apply_audio_startup_config(Media(audio), write_settle_seconds=0) is True
    assert audio.calls == [(AUDIO_STARTUP_CONFIG, True, 0)]


def test_audio_startup_profile_is_best_effort_without_sdk_api():
    assert apply_audio_startup_config(Media(object())) is False
