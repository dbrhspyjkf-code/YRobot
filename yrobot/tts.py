from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import wave

import numpy as np

EDGE_TTS_VOICE = "zh-CN-XiaoxiaoNeural"


def synthesize_speech_24k(text: str) -> np.ndarray:
    """Return 24 kHz mono float32 PCM for short spoken status messages."""
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError("edge-tts is not installed") from exc

    async def save(path: str) -> None:
        communicate = edge_tts.Communicate(text, EDGE_TTS_VOICE)
        await communicate.save(path)

    with tempfile.TemporaryDirectory(prefix="yrobot-tts-") as tmpdir:
        mp3_path = os.path.join(tmpdir, "speech.mp3")
        wav_path = os.path.join(tmpdir, "speech.wav")
        asyncio.run(save(mp3_path))
        subprocess.run(
            [
                "gst-launch-1.0",
                "-q",
                "filesrc",
                f"location={mp3_path}",
                "!",
                "decodebin",
                "!",
                "audioconvert",
                "!",
                "audioresample",
                "!",
                "audio/x-raw,format=S16LE,channels=1,rate=24000",
                "!",
                "wavenc",
                "!",
                "filesink",
                f"location={wav_path}",
            ],
            check=True,
            timeout=15,
        )
        with wave.open(wav_path, "rb") as wav:
            if wav.getnchannels() != 1 or wav.getframerate() != 24_000 or wav.getsampwidth() != 2:
                raise RuntimeError("unexpected decoded TTS format")
            raw = wav.readframes(wav.getnframes())
    if not raw:
        raise RuntimeError("TTS returned no audio")
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
