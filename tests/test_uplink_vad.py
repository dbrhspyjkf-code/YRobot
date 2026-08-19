"""Regression tests for the xiaozhi uplink energy-hangover VAD.

The old uplink gate ended an utterance on the FIRST 60 ms frame below
rms 1000 once ``min_deadline`` (3 s) elapsed, so any inter-word pause or
breath cut the audio mid-sentence: cloud ASR received fragments and the
user's speech was truncated ("the cloud never knew what I said").
These tests pin the fixed behaviour:

- a short (< hangover) intra-utterance pause must NOT end the turn;
- only ~0.9 s of SUSTAINED silence ends it;
- utterances must survive well past the old 6 s hard cap (12 s now);
- a 1 s minimum keeps noise blips from chattering the gate;
- the preroll buffer preserves pre-speech frames (first syllables) and
  drops stale ones.
"""

from __future__ import annotations

from yrobot.uplink_vad import EnergyHangoverVAD, PrerollBuffer

FRAME_MS = 60.0
VOICE_RMS = 4000.0  # well above both start/stop thresholds
SILENCE_RMS = 100.0  # well below stop threshold


def _clock():
    t = {"now": 1000.0}
    def tick(step_s: float = FRAME_MS / 1000.0) -> float:
        t["now"] += step_s
        return t["now"]
    return tick


class TestEnergyHangoverVAD:
    def test_word_gap_pause_does_not_end_utterance(self):
        """A 600 ms inter-word pause (breath, comma) must not cut the turn."""
        vad = EnergyHangoverVAD()
        clock = _clock()
        vad.begin(clock())
        # 2 s of speech, then a 600 ms pause, then speech again.
        for _ in range(33):  # ~2 s
            assert vad.feed(VOICE_RMS, clock()) == "continue"
        for _ in range(10):  # 600 ms pause < 900 ms hangover
            assert vad.feed(SILENCE_RMS, clock()) == "continue"
        for _ in range(20):  # speech resumes
            assert vad.feed(VOICE_RMS, clock()) == "continue"

    def test_sustained_silence_ends_utterance(self):
        """~0.9 s of sustained silence ends the utterance (after the floor)."""
        vad = EnergyHangoverVAD()
        clock = _clock()
        vad.begin(clock())
        for _ in range(40):  # ~2.4 s of speech, well past the 1 s floor
            assert vad.feed(VOICE_RMS, clock()) == "continue"
        decisions = [vad.feed(SILENCE_RMS, clock()) for _ in range(15)]  # 900 ms
        assert decisions[-1] == "end"
        assert all(d == "continue" for d in decisions[:-1])

    def test_silence_counter_resets_on_voice(self):
        """Two sub-hangover silences separated by voice must not accumulate."""
        vad = EnergyHangoverVAD()
        clock = _clock()
        vad.begin(clock())
        for _ in range(20):
            vad.feed(VOICE_RMS, clock())
        for _ in range(10):  # 600 ms silence
            assert vad.feed(SILENCE_RMS, clock()) == "continue"
        assert vad.feed(VOICE_RMS, clock()) == "continue"  # resets the run
        for _ in range(10):  # another 600 ms silence, still < hangover
            assert vad.feed(SILENCE_RMS, clock()) == "continue"
        assert vad.feed(VOICE_RMS, clock()) == "continue"

    def test_long_utterance_survives_past_old_six_second_cap(self):
        """Continuous speech must stream past 6 s (old hard cap) to 12 s."""
        vad = EnergyHangoverVAD()
        clock = _clock()
        vad.begin(clock())
        for _ in range(150):  # 9 s of continuous speech
            assert vad.feed(VOICE_RMS, clock()) == "continue"
        # Reach the 12 s cap and confirm it ends (runaway-noise guard).
        saw_end = False
        for _ in range(60):  # up to 12.6 s
            if vad.feed(VOICE_RMS, clock()) == "end":
                saw_end = True
                break
        assert saw_end

    def test_min_utterance_ignores_early_silence(self):
        """A loud blip followed instantly by silence must not open+close."""
        vad = EnergyHangoverVAD()
        clock = _clock()
        vad.begin(clock())
        assert vad.feed(VOICE_RMS, clock()) == "continue"
        for _ in range(15):  # 900 ms of silence, still under the 1 s floor
            assert vad.feed(SILENCE_RMS, clock()) == "continue"
        # After the floor, continued silence ends the turn.
        assert vad.feed(SILENCE_RMS, clock()) == "end"


class TestPrerollBuffer:
    def test_drain_keeps_recent_pre_speech_frames(self):
        """The pre-speech tail survives a failed gate and is prepended next."""
        preroll = PrerollBuffer(seconds=0.5, frame_ms=FRAME_MS)
        tick = _clock()
        preroll.extend([f"f{i}" for i in range(20)], tick())  # 20 frames pushed
        kept = preroll.drain(tick())
        assert kept == [f"f{i}" for i in range(12, 20)]  # last 8 (0.5 s)

    def test_drain_drops_stale_frames(self):
        """Frames older than the max age are never prepended (stale audio)."""
        preroll = PrerollBuffer(seconds=0.5, frame_ms=FRAME_MS, max_age_s=2.0)
        preroll.extend(["old1", "old2"], now=1000.0)
        kept = preroll.drain(now=1010.0)  # 10 s later
        assert kept == []

    def test_drain_clears_buffer(self):
        preroll = PrerollBuffer(seconds=0.5, frame_ms=FRAME_MS)
        preroll.extend(["a", "b"], now=1000.0)
        assert preroll.drain(now=1000.5) == ["a", "b"]
        assert preroll.drain(now=1000.6) == []
