"""Audio dashboard helpers and VAD configuration."""

from __future__ import annotations

import logging
import os
import threading
import time


logger = logging.getLogger(__name__)

FRAME_MS = 20
FRAME_SAMPLES = 16_000 * FRAME_MS // 1000  # 320
SILENT_DB = -120.0
# Tunable VAD threshold: set via environment or /api/audio/vad runtime.
_vad_rms_min = float(os.environ.get("YROBOT_VAD_RMS_MIN", "0.11"))
DASHBOARD_MIC_SILENT_DB = -60.0
DASHBOARD_MIC_LOUD_DB = 0.0
DASHBOARD_MIC_VOICED_RMS = 0.004
_dashboard_mic_lock = threading.Lock()
_dashboard_mic_signal: dict[str, float | bool] = {
    "level_db": DASHBOARD_MIC_SILENT_DB,
    "level_percent": 0.0,
    "rms": 0.0,
    "voiced": False,
    "updated_at": 0.0,
}


def _publish_dashboard_mic(rms: float) -> None:
    db = 20.0 * math.log10(rms + 1e-9)
    span = DASHBOARD_MIC_LOUD_DB - DASHBOARD_MIC_SILENT_DB
    pct = max(0.0, min(100.0, (db - DASHBOARD_MIC_SILENT_DB) / span * 100.0))
    with _dashboard_mic_lock:
        _dashboard_mic_signal.update(
            {
                "level_db": db,
                "level_percent": pct,
                "rms": rms,
                "voiced": rms > DASHBOARD_MIC_VOICED_RMS,
                "updated_at": time.monotonic(),
            }
        )


def dashboard_mic_signal() -> dict[str, float | bool]:
    """Return the latest mic level snapshot written by ``Microphone``.

    The dashboard reads this cheaply without contending with the realtime
    loop over the single GStreamer ``appsink`` reader.
    """
    with _dashboard_mic_lock:
        return dict(_dashboard_mic_signal)
WRITE_SETTLE_SECONDS = 0.1

# Pollen's Reachy Mini conversation app applies this exact profile before
# starting its realtime record/play loops.  In particular the hardware AGC
# and double-talk settings operate before our software VAD; outbound AGC is
# too late to recover a quiet interjection that VAD has already rejected.
AUDIO_STARTUP_CONFIG: tuple[tuple[str, tuple[float | int, ...]], ...] = (
    ("PP_AGCMAXGAIN", (10.0,)),
    ("PP_MIN_NS", (0.8,)),
    ("PP_MIN_NN", (0.8,)),
    ("PP_GAMMA_E", (0.5,)),
    ("PP_GAMMA_ETAIL", (0.5,)),
    ("PP_NLATTENONOFF", (0,)),
    ("PP_MGSCALE", (4.0, 1.0, 1.0)),
)


def get_vad_rms_min() -> float:
    return _vad_rms_min


def set_vad_rms_min(value: float) -> float:
    global _vad_rms_min
    _vad_rms_min = max(0.001, min(0.5, float(value)))
    return _vad_rms_min
