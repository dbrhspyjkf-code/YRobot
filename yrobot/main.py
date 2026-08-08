"""Application wiring: Reachy Mini app, session lifecycle and CLI.

Thread map (all communication is immutable data + atomic flags):

    main loop      microphone → VAD → turn controller → bounded audio queue
    yrobot-uplink  audio queue + latest JPEG → websocket (may block safely)
    yrobot-camera  camera → resize/JPEG → replaceable latest-frame slot
    yrobot-recv    gateway deltas → gate check → speaker queue / captions
    yrobot-speaker paced, interruptible playback (owns the audio pipeline)
    yrobot-motion  50 Hz choreographer (owns the robot pose)
    yrobot-doa     12 Hz sound compass → gaze targets
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np
from dotenv import load_dotenv
import opuslib
from reachy_mini.apps.app import ReachyMiniApp
from reachy_mini.reachy_mini import ReachyMini

from yrobot.app_config import (
    _MediaHolder,
    audio_input_controller_singleton,
    register_settings_routes,
)
from yrobot.config import Settings
from yrobot.state import ROBOT_STATE

# ── Xiaozhi cloud device identity (read from network interface) ──────────────
try:
    with open("/sys/class/net/wlan0/address") as f:
        XIAOZHI_DEVICE_ID = f.read().strip()
except Exception:
    XIAOZHI_DEVICE_ID = ""

logger = logging.getLogger(__name__)

# ── Safe-mode startup guard ────────────────────────────────────────────────────
# Persisted across systemd restarts so a deterministic boot loop counts toward
# the threshold. A successful run() clears the counter.
_STARTUP_FAIL_COUNTER_PATH = "/tmp/.yrobot_startup_failures"
_MAX_STARTUP_FAILURES = 3


def _record_startup_failure() -> int:
    """Increment the persisted failure counter and return the new value."""
    try:
        with open(_STARTUP_FAIL_COUNTER_PATH, encoding="utf-8") as f:
            count = int((f.read() or "0").strip() or "0")
    except (FileNotFoundError, ValueError):
        count = 0
    count += 1
    try:
        with open(_STARTUP_FAIL_COUNTER_PATH, "w", encoding="utf-8") as f:
            f.write(str(count))
    except OSError:
        pass
    return count

logger = logging.getLogger(__name__)
class Yrobot(ReachyMiniApp):
    """Reachy Mini app entry point (``reachy_mini_apps`` group)."""

    custom_app_url: str | None = "http://0.0.0.0:8042"

    def __init__(self, running_on_wireless: bool = False) -> None:
        load_dotenv()
        super().__init__(running_on_wireless=running_on_wireless)
        self._media_holder = _MediaHolder()
        assert self.settings_app is not None
        register_settings_routes(self.settings_app, media_holder=self._media_holder)

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        """Run YRobot with Xiaozhi cloud backend."""
        # Reset the failure counter from previous sessions.
        from yrobot.main import _STARTUP_FAIL_COUNTER_PATH as _fail_path
        try:
            os.remove(_fail_path)
        except FileNotFoundError:
            pass
        try:
            self._media_holder.media = reachy_mini.media
            self._run_xiaozhi(reachy_mini, stop_event)
        except Exception as exc:
            logger.exception("YRobot startup failed: %s", exc)
            ROBOT_STATE.set("safe_mode")
            _enter_safe_mode(self._media_holder, exc, stop_event)

    def _run_xiaozhi(
        self,
        reachy_mini: ReachyMini,
        stop_event: threading.Event,
    ) -> None:
        """Xiaozhi — sounddevice mic + OutputStream TTS."""
        import asyncio as _a
        import json as _j
        import sounddevice as _sd
        import subprocess as _sp
        import websockets as _ws
        import cv2
        from yrobot.motion import IDLE, LISTEN, SPEAK, Choreographer, SoundCompass, head_yaw_of
        from yrobot.app_config import audio_input_controller_singleton
        from yrobot.audio import _publish_dashboard_mic

        choreo = Choreographer(reachy_mini)
        # Smooth but responsive gaze: fast enough to track a speaker, bounded
        # enough to never snap (the body turn carries the large motions).
        choreo._gaze._max_vel = 2.5   # rad/s
        choreo._gaze._omega = 6.0     # spring stiffness
        from yrobot.app_config import motion_controller_singleton
        motion_controller_singleton().set(choreo)
        # Official emotion library (85 recorded moves) — lazy singleton so
        # playback works even if the library is slow to load on first use.
        _recorded_moves = [None]
        def _get_recorded():
            if _recorded_moves[0] is None:
                try:
                    from reachy_mini.motion.recorded_move import RecordedMoves
                    _recorded_moves[0] = RecordedMoves(
                        "pollen-robotics/reachy-mini-emotions-library")
                except Exception as exc:
                    logger.warning("emotion library unavailable: %s", exc)
            return _recorded_moves[0]
        motion_controller_singleton().set_recorded_provider(_get_recorded)
        choreo.start()

        # SoundCompass: track speaker direction via XVF3800 DoA
        _user_speaking = [False]
        def _current_head_yaw():
            try:
                import numpy as np
                return head_yaw_of(np.asarray(reachy_mini.get_current_head_pose()))
            except Exception:
                return choreo.current_yaw()
        SoundCompass.WINDOW_S = 2.0       # 2s smoothing window (was 1.0)
        SoundCompass.MIN_CONFIDENCE = 6.0  # need 6+ confidence (was 3.0)
        SoundCompass.DEADBAND_RAD = 0.20   # ~11° deadband (was 0.12)

        # ── PersonTracker: fuse camera face detection with audio DoA ──────
        # Runs at ~5 fps; when a face is confidently detected the visual
        # yaw overrides the audio-only DoA estimate.
        import threading as _th_face
        _face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        _visual_gaze = [None]  # latest (world_yaw, timestamp) or None

        # Fusion arbitration: while the face tracker has a confirmed face in
        # view (_visual_gaze is not None), SoundCompass backs off.  As soon
        # as the face is lost the sound compass immediately takes over.
        compass = SoundCompass(
            reachy_mini.media,
            current_head_yaw=_current_head_yaw,
            user_active=lambda: _visual_gaze[0] is None,
            on_target=choreo.set_gaze_target,
        )
        compass.start()
        _vis_frames = 0  # consecutive face detections (2 required to override DoA)
        _last_emotion_move: dict[str, float] = {}  # move name -> last fire time
        _vis_stop = _th_face.Event()

        def _handle_emotion(choreo: Any, emo: str, last: dict[str, float], rec_provider: Any) -> None:
            """Map a Xiaozhi emotion to a move with per-move cooldown."""
            from yrobot.motion import (
                EMOTION_FALLBACK_MOVE, EMOTION_TO_MOVE)
            rec_name = EMOTION_TO_MOVE.get(emo)
            fb_name = EMOTION_FALLBACK_MOVE.get(emo)
            target = rec_name or fb_name
            now = time.monotonic()
            if not target:
                logger.info("xz emotion %s (no move)", emo or "?")
                return
            if now - last.get(target, -1e9) < 5.0:
                logger.info("xz emotion %s -> %s (cooldown)", emo, target)
                return
            last[target] = now
            if rec_name:
                rec = rec_provider()
                if rec is not None and choreo.play_recorded(rec_name, rec):
                    logger.info("xz emotion %s -> recorded %s", emo, rec_name)
                    return
            if fb_name:
                choreo.play_move(fb_name)
                logger.info("xz emotion %s -> move %s", emo, fb_name)


        def _face_tracker():
            nonlocal _vis_frames
            import json as _json, urllib.request as _ur
            frame_url = "http://127.0.0.1:8042/api/camera/frame"
            state_url = "http://127.0.0.1:8042/api/camera/state"
            _last_cam_check = 0.0
            def _ensure_camera():
                """Re-enable camera via HTTP; called at startup then every 30s."""
                nonlocal _last_cam_check
                now = time.time()
                if now - _last_cam_check < 30:
                    return
                _last_cam_check = now
                try:
                    _r = _ur.Request(state_url, method="PUT",
                        data=_json.dumps({"running": True}).encode(),
                        headers={"Content-Type": "application/json"})
                    _ur.urlopen(_r, timeout=3)
                except Exception:
                    pass
            while not _vis_stop.is_set():
                _ensure_camera()
                try:
                    req = _ur.Request(frame_url)
                    with _ur.urlopen(req, timeout=2) as resp:
                        jpeg = resp.read()
                    arr = np.frombuffer(jpeg, dtype=np.uint8)
                    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if bgr is None:
                        _vis_stop.wait(0.5)
                        continue
                    # Downscale to 320px wide: Haar detection cost scales with
                    # pixels; 320 keeps ~2-3 fps on the low-power board while
                    # still tracking a face across the ~80° FOV.
                    scale = bgr.shape[1] / 320.0
                    if scale > 1.0:
                        small = cv2.resize(bgr, (320, int(bgr.shape[0] / scale)))
                    else:
                        small = bgr
                    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                    faces = _face_cascade.detectMultiScale(
                        gray, scaleFactor=1.15, minNeighbors=4,
                        minSize=(24, 24),
                    )
                    if len(faces) == 0:
                        # No face seen this frame; let audio DoA dominate.
                        _visual_gaze[0] = None
                        _vis_frames = 0
                        _vis_stop.wait(0.5)
                        continue
                    # Require 2 consecutive detections before the visual gaze
                    # overrides the audio DoA: a single spurious Haar hit at
                    # the frame edge would otherwise yank the head around.
                    _vis_frames += 1
                    if _vis_frames < 2:
                        _vis_stop.wait(0.5)
                        continue
                    # Use the largest face.
                    x, y, w, h = max(faces, key=lambda r: r[2] * r[3])
                    cx = (x + w / 2) * scale
                    # Camera-relative angle via pinhole model: atan2 of the
                    # pixel offset divided by focal length.  This is exact
                    # and uses the real camera calibration, no FOV guess.
                    try:
                        K = reachy_mini.media.camera.K
                        fx = float(K[0, 0])
                        cx_princ = float(K[0, 2])
                        cam_rad = math.atan2(cx - cx_princ, fx)
                    except Exception:
                        cam_rad = math.radians(
                            (cx - bgr.shape[1] / 2) * (80.0 / bgr.shape[1]))
                    # Convert to world yaw using head pose.
                    try:
                        head_yaw = _current_head_yaw()
                    except Exception:
                        head_yaw = choreo.current_yaw()
                    _visual_gaze[0] = (head_yaw + cam_rad, time.time())
                    _vis_stop.wait(0.5)   # ~2 fps, keep CPU low
                except Exception:
                    _vis_stop.wait(0.5)
        _vis_thread = _th_face.Thread(target=_face_tracker, name="face-tracker", daemon=True)
        _vis_thread.start()

        # Reduce SoundCompass jitter: log raw angles, use longer window
        _compass_log = [0.0]  # last logged angle to avoid spam

        _sd.default.samplerate = 16000
        _sd.default.channels = 1
        _sd.default.dtype = "int16"
        # Release the Reachy SDK's output stream so aplay can open
        # /dev/snd/pcmC0D0p (the SDK held it via Speaker class).
        try:
            reachy_mini.media.stop_playing()
            logger.info("released SDK speaker for aplay")
        except Exception as e:
            logger.warning("stop_playing failed: %s", e)

        mic_stream = _sd.InputStream(device="reachymini_audio_src")
        mic_stream.start()


        # Playback is intentionally isolated from the WebSocket event loop.
        # aplay writes can block on ALSA; the receive coroutine must never wait
        # on that pipe or it will stall incoming TTS packets.
        import queue as _pq
        import threading as _th
        _audio_q = _pq.Queue()
        _writer_stop = _th.Event()
        _audio_stats = {"enqueued": 0, "written": 0, "restarts": 0}

        def _open_aplay():
            return _sp.Popen(
                ["/usr/bin/aplay", "-r", "16000", "-f", "S16_LE", "-c", "2", "-q"],
                stdin=_sp.PIPE,
                stderr=_sp.DEVNULL,
            )

        def _audio_writer():
            proc = None
            while not _writer_stop.is_set():
                try:
                    chunk = _audio_q.get(timeout=0.5)
                except _pq.Empty:
                    continue
                if chunk is None:
                    break
                for attempt in range(2):
                    try:
                        if proc is None or proc.poll() is not None:
                            proc = _open_aplay()
                            _audio_stats["restarts"] += 1
                            logger.info("audio-out: started aplay (%d)", _audio_stats["restarts"])
                        proc.stdin.write(chunk)
                        _audio_stats["written"] += 1
                        break
                    except (BrokenPipeError, OSError) as exc:
                        logger.warning("audio-out: aplay write failed: %s", exc)
                        if proc is not None:
                            try:
                                proc.kill()
                            except OSError:
                                pass
                        proc = None
                else:
                    logger.error("audio-out: dropped chunk after aplay restart failure")
            if proc is not None:
                try:
                    proc.stdin.close()
                    proc.terminate()
                except OSError:
                    pass

        _audio_thread = _th.Thread(target=_audio_writer, name="aplay-writer", daemon=True)
        _audio_thread.start()

        def _aplay_add(stereo_f32):
            pcm = (np.clip(stereo_f32, -1, 1) * 32767).astype("<i2").tobytes()
            _audio_q.put_nowait(pcm)
            _audio_stats["enqueued"] += 1

        def _aplay_flush():
            pass

        async def run():
            enc = opuslib.Encoder(16000, 1, "voip")
            hdrs = {"Authorization": "Bearer test-token", "Device-Id": XIAOZHI_DEVICE_ID, "Protocol-Version": "1"}
            async with _ws.connect("wss://api.tenclass.net/xiaozhi/v1/", additional_headers=hdrs, open_timeout=12, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(_j.dumps({"type":"hello","version":1,"transport":"websocket",
                    "audio_params":{"format":"opus","sample_rate":16000,"channels":1,"frame_duration":60}}))
                data = _j.loads(await _a.wait_for(ws.recv(), timeout=10))
                sid = data.get("session_id","")
                params = data.get("audio_params", {})
                tts_rate = int(params.get("sample_rate", 24000))
                tts_duration = int(params.get("frame_duration", 60))
                tts_frame_size = tts_rate * tts_duration // 1000
                dec = opuslib.Decoder(tts_rate, 1)
                tts_packets = 0
                tts_decode_errors = 0
                _tts_start_at = 0.0
                logger.info("xiaozhi ready sid=%s audio=%dHz/%dms", sid[:12], tts_rate, tts_duration)
                tts_active = False

                async def recv():
                    nonlocal tts_active, _tts_start_at, tts_packets, tts_decode_errors
                    while not stop_event.is_set():
                        try:
                            raw = await ws.recv()
                        except _a.TimeoutError:
                            continue
                        if isinstance(raw, bytes):
                            tts_packets += 1
                            if not hasattr(_aplay_add, "_count"):
                                _aplay_add._count = 0
                            _aplay_add._count += 1
                            try:
                                pcm = dec.decode(raw, tts_frame_size)
                                pcm_f32 = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
                                ratio = tts_rate / 16000
                                idx = np.arange(0, len(pcm_f32), ratio).astype(int)
                                pcm_16k = pcm_f32[idx[:min(len(idx), len(pcm_f32))]]
                                stereo = np.column_stack([pcm_16k, pcm_16k])
                                _aplay_add(stereo)
                                if tts_packets % 10 == 0:
                                    logger.info("xz audio packets=%d latest=%dB", tts_packets, len(raw))
                            except Exception as exc:
                                tts_decode_errors += 1
                                logger.warning(
                                    "xz opus decode failed packet=%d bytes=%d error=%s",
                                    tts_packets, len(raw), exc,
                                )
                        else:
                            d = _j.loads(raw)
                            t = d.get("type","")
                            if t == "llm":
                                # Xiaozhi sends the model's emotion/expression here
                                # (e.g. {"type":"llm","emotion":"happy","text":"😀"});
                                # Prefer the official recorded emotion; fall back to a
                                # programmatic move.  Per-move cooldown prevents the
                                # default 'happy' emotion from firing on every reply.
                                # If a manual/MCP move is playing, emotions are ignored
                                # so a tool-triggered dance is never cut short.
                                emo = (d.get("emotion") or "").strip().lower()
                                if choreo.current_move() is not None or choreo.current_recorded() is not None:
                                    logger.info("xz emotion %s ignored (move in progress)", emo)
                                else:
                                    _handle_emotion(choreo, emo, _last_emotion_move, _get_recorded)
                            if t == "stt":
                                logger.info("xz stt: %s", d.get("text",""))
                                choreo.set_mode(LISTEN)
                            elif t == "tts" and d.get("state")=="start":
                                logger.info("xz tts start")
                                tts_active = True
                                _tts_start_at = time.time()
                                _user_speaking[0] = False
                                tts_packets = 0
                                tts_decode_errors = 0
                                _aplay_add._count = 0
                                choreo.set_mode(SPEAK)
                                choreo.release_still()
                            elif t == "tts" and d.get("state")=="sentence_start":
                                logger.info("xz tts text: %s", d.get("text","")[:80])
                            elif t == "tts" and d.get("state") == "sentence_end":
                                logger.info(
                                    "xz tts sentence_end packets=%d audio(enqueued=%d written=%d pending=%d)",
                                    getattr(_aplay_add, "_count", 0),
                                    _audio_stats["enqueued"],
                                    _audio_stats["written"],
                                    _audio_q.qsize(),
                                )
                            elif t == "tts" and d.get("state") == "stop":
                                tts_active = False
                                logger.info(
                                    "xz tts stop packets=%d audio(enqueued=%d written=%d pending=%d)",
                                    getattr(_aplay_add, "_count", 0),
                                    _audio_stats["enqueued"],
                                    _audio_stats["written"],
                                    _audio_q.qsize(),
                                )
                                logger.info("xz audio summary packets=%d decode_errors=%d", tts_packets, tts_decode_errors)
                                _aplay_flush()
                                choreo.set_mode(IDLE)

                rt = _a.ensure_future(recv())
                try:
                    # Silence floor for the Xiaozhi uplink gate.  Higher =
                    # louder speech required to trigger; normalized 0..1 value
                    # from the shared config (default 2000/32768 ≈ 0.061).
                    from yrobot.audio import get_vad_rms_min as _get_vad_min
                    SILENCE_RMS = max(500, int(_get_vad_min() * 32768))
                    logger.info("xz silence floor rms=%.0f", SILENCE_RMS)
                    while not stop_event.is_set():
                        if tts_active:
                            # Safety: if the server sent tts/start but no audio
                            # ever arrives (cloud hiccup / lost stop), recover
                            # listening after TTS_STALL_TIMEOUT so the robot is
                            # not deaf forever.
                            if tts_packets == 0 and time.time() - _tts_start_at > 30.0:
                                tts_active = False
                                logger.warning("tts stall: no audio for 30s, forcing listen")
                                _aplay_flush()
                                choreo.set_mode(IDLE)
                            await _a.to_thread(mic_stream.read, 960)
                            await _a.sleep(0)
                            continue
                        if not audio_input_controller_singleton().enabled():
                            await _a.to_thread(mic_stream.read, 960)
                            await _a.sleep(0.1)
                            continue
                        # Feed latest visual gaze if available (rate-limited)
                        if _visual_gaze[0] is not None:
                            vy, vt = _visual_gaze[0]
                            _vg = getattr(choreo, "_last_vis_gaze_at", 0)
                            if time.time() - vt < 0.5 and time.time() - _vg > 0.5:
                                choreo.set_gaze_target(vy)
                                choreo._last_vis_gaze_at = time.time()
                        frames = []
                        rms_max = 0
                        for _ in range(16):
                            buf, _ = await _a.to_thread(mic_stream.read, 960)
                            rms = float(np.sqrt(np.mean(np.square(np.frombuffer(buf, dtype=np.int16).astype(np.float64)))))
                            _publish_dashboard_mic(float(rms) / 32768.0)
                            if rms > rms_max: rms_max = rms
                            frames.append(buf)
                        if rms_max < SILENCE_RMS:
                            continue
                        _user_speaking[0] = True
                        await ws.send(_j.dumps({"session_id":sid,"type":"listen","state":"start","mode":"manual"}))
                        sent = 0
                        for buf in frames:
                            try:
                                await ws.send(enc.encode(buf.tobytes(), 960))
                                sent += 1
                                await _a.sleep(0)
                            except Exception: pass
                        deadline = time.monotonic() + 6.0
                        min_deadline = time.monotonic() + 3.0
                        while time.monotonic() < deadline and not stop_event.is_set():
                            buf, _ = await _a.to_thread(mic_stream.read, 960)
                            rms = float(np.sqrt(np.mean(np.square(np.frombuffer(buf, dtype=np.int16).astype(np.float64)))))
                            _publish_dashboard_mic(float(rms) / 32768.0)
                            try:
                                await ws.send(enc.encode(buf.tobytes(), 960))
                                sent += 1
                                await _a.sleep(0)
                            except Exception: pass
                            if time.monotonic() > min_deadline and rms < 1000:
                                break
                        await ws.send(_j.dumps({"session_id":sid,"type":"listen","state":"stop"}))
                        _user_speaking[0] = False
                        if sent:
                            logger.info("xz sent %d frames (rms=%.0f)", sent, rms_max)
                        for _ in range(20):
                            if stop_event.is_set(): break
                            await _a.sleep(0.2)
                finally:
                    rt.cancel()

        try:
            while not stop_event.is_set():
                try:
                    _a.run(run())
                except Exception as e:
                    logger.info("xiaozhi ended: %s", e)
                if not stop_event.is_set():
                    logger.info("xiaozhi reconnecting in 3s...")
                    stop_event.wait(3)
        except Exception as e:
            logger.info("xiaozhi ended: %s", e)
        finally:
            _writer_stop.set()
            _audio_q.put(None)
            _audio_thread.join(timeout=2)
            mic_stream.stop()
            mic_stream.close()
            _vis_stop.set()
            _vis_thread.join(timeout=2)
            compass.close()
            compass.join(timeout=2)
            choreo.close()
            choreo.join(timeout=2)


def _enter_safe_mode(
    media_holder: _MediaHolder,
    exc: Exception,
    stop_event: threading.Event,
) -> None:
    """Dashboard-only mode when Xiaozhi fails to start."""
    fails = _record_startup_failure()
    logger.error(
        "YRobot safe mode: %d/%d consecutive startup failures (last: %s)",
        fails, _MAX_STARTUP_FAILURES, exc,
    )
    if fails >= _MAX_STARTUP_FAILURES:
        logger.error(
            "YRobot safe mode: threshold reached; dashboard up, "
            "refusing to retry until service restart."
        )
    while not stop_event.is_set():
        stop_event.wait(timeout=1.0)


def cli() -> None:
    """Run YRobot from a terminal (Ctrl-C to stop)."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname).1s %(name)s: %(message)s"
    )
    app = Yrobot()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    cli()
