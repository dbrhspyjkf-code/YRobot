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
from reachy_mini.apps.app import ReachyMiniApp
from reachy_mini.reachy_mini import ReachyMini

from yrobot.app_config import (
    AppConfig,
    _MediaHolder,
    audio_input_controller_singleton,
    register_settings_routes,
)
from yrobot.audio import Microphone, Speaker, UplinkGain, VoiceDetector, apply_audio_startup_config
from yrobot.barge import BargeConfig, BargeDecision, BargeDetector
from yrobot.config import Settings
from yrobot.hermes_tools import HermesToolsController
from yrobot.home_assistant import HomeAssistantController
from yrobot.local_info import LocalInfoController
from yrobot.motion import IDLE, LISTEN, SPEAK, Choreographer, SoundCompass, head_yaw_of
from yrobot.realtime import Delta, RealtimeClient, ThinkFilter
from yrobot.session import ConversationMemory, RotationPolicy
from yrobot.tts import synthesize_speech_24k
from yrobot.turn import TurnGate
from yrobot.vision import LatestCamera, VisionStats

logger = logging.getLogger(__name__)

FRAME_S = 0.02
AUDIO_PIPELINE_WARMUP_S = 1.0
ACTIVE_WINDOW_S = 10.0  # camera stays at 1 fps this long after the user spoke
# kv-cache burn rates measured on the live gateway (tokens/second, per frame).
# "Active chat ~85 tok/s" was measured WITH 1 fps vision; frames are counted
# separately here, so busy audio-only burn is ~28. Estimating 85 rotated
# sessions every ~85 s and wiped the model's memory mid-conversation
# (hardware log 2026-07-24).
KV_PER_S_IDLE, KV_PER_S_BUSY, KV_PER_FRAME = 13.0, 28.0, 64.0


@dataclass(frozen=True)
class UplinkPacket:
    """One captured audio unit waiting for the network sender."""

    audio: np.ndarray
    force_listen: bool
    captured_at: float
    input_id: str


class Conversation:
    """One full-duplex conversation across rotating gateway sessions."""

    def __init__(self, settings: Settings, mini: ReachyMini, stop: threading.Event) -> None:
        self._s = settings
        self._mini = mini
        self._stop = stop
        self._mic = Microphone(mini.media)
        self._detector = VoiceDetector(settings.vad_aggressiveness)
        self._speaker = Speaker(mini.media)
        self._gate = TurnGate()
        self._turn_lock = threading.Lock()
        self._agc = UplinkGain()
        self._home_assistant = HomeAssistantController.from_settings(settings)
        self._hermes_tools = HermesToolsController.from_settings(settings)
        self._local_info = LocalInfoController(enabled=settings.local_info_enabled)
        self._audio_input_enabled = audio_input_controller_singleton().enabled
        self._muted_response_ids: set[str] = set()
        self._suppressed_response_ids: set[str] = set()
        self._barge = BargeDetector(
            BargeConfig(
                echo_similarity=settings.barge_echo_similarity,
                unexplained_db=settings.barge_unexplained_db,
                confirm_ms=settings.barge_confirm_ms,
                fast_confirm_ms=settings.barge_fast_confirm_ms,
                fast_echo_similarity=settings.barge_fast_echo_similarity,
                fast_unexplained_db=settings.barge_fast_unexplained_db,
            )
        )
        self._rotation = RotationPolicy(settings.session_budget_s, settings.kv_budget_tokens)
        self._memory = ConversationMemory()
        self._choreo = Choreographer(mini)
        self._compass = SoundCompass(
            mini.media,
            current_head_yaw=self._current_head_yaw,
            user_active=self._confirmed_user_active,
            on_target=self._choreo.set_gaze_target,
        )
        self._captions = ThinkFilter()
        self._session_dead = threading.Event()
        # Lightweight silence gate: keep audio uplink live only while conversation is active.
        self._uplink_live = True
        self._last_delta_at = 0.0
        self._last_voice_at = -1e9
        self._last_user_onset_at = -1e9
        self._confirmed_voice_until = -1e9
        self._server_kv: float | None = None
        self._video_kv_est = 0.0
        self._last_logged_audio_onset_at = -1e9
        self._input_sequence = 0
        self._session_sequence = 0
        self._last_session_ended_at: float | None = None
        self._camera = (
            LatestCamera(
                mini.media,
                active=lambda now: (
                    now - self._last_voice_at < ACTIVE_WINDOW_S or self._speaker.audible(now)
                ),
                capture_period_s=settings.frame_period_active_s,
                idle_heartbeat_s=settings.frame_period_idle_s,
                scene_threshold=settings.scene_change_threshold,
            )
            if settings.send_video
            else None
        )

    def run(self) -> None:
        self._mini.media.start_recording()
        self._mini.media.start_playing()
        # Pollen's reference waits for the GStreamer pipelines to materialize
        # before writing/reading back XVF controls.
        self._stop.wait(AUDIO_PIPELINE_WARMUP_S)
        apply_audio_startup_config(self._mini.media)
        self._mini.enable_wobbling()
        if self._s.head_tracking_weight > 0:
            self._mini.start_head_tracking(weight=self._s.head_tracking_weight)
        self._speaker.start()
        self._choreo.start()
        self._compass.start()
        if self._camera is not None:
            self._camera.start()
        try:
            while not self._stop.is_set():
                self._one_session()
                self._stop.wait(self._s.reconnect_delay_s)
        finally:
            self._compass.close()
            self._choreo.close()
            self._speaker.close()
            if self._camera is not None:
                self._camera.close()
            self._compass.join(timeout=2)
            self._choreo.join(timeout=2)
            self._speaker.join(timeout=2)
            if self._camera is not None:
                self._camera.join(timeout=2)
                self._log_vision_stats("total", self._camera.stats())
            self._mini.media.stop_recording()

    # -- session ------------------------------------------------------------

    def _one_session(self) -> None:
        self._session_dead.clear()
        self._captions = ThinkFilter()
        self._server_kv = None
        self._video_kv_est = 0.0
        self._last_logged_audio_onset_at = -1e9
        self._input_sequence = 0
        self._barge.reset()
        self._rotation.reset()
        self._session_sequence += 1
        session_sequence = self._session_sequence
        with self._turn_lock:
            self._gate = TurnGate()
        client = RealtimeClient(
            self._s,
            on_delta=lambda delta: self._on_delta(delta, session_sequence),
            on_closed=lambda reason: self._on_closed(reason, session_sequence),
            system_prompt=self._memory.prompt(
                self._s.effective_system_prompt
            ),
        )
        try:
            client.open()
        except Exception as exc:  # noqa: BLE001 — queue/backend failures are routine
            logger.warning("session open failed: %s", exc)
            client.close()
            return
        if self._last_session_ended_at is not None:
            logger.info(
                "session %d handoff gap %.0f ms",
                session_sequence,
                (time.monotonic() - self._last_session_ended_at) * 1000,
            )
        try:
            self._uplink_loop(client)
        finally:
            self._last_session_ended_at = time.monotonic()
            # A transport/session boundary is also a playback boundary.
            # Never let buffered deltas from a dead session leak into the
            # reconnecting one.
            with self._turn_lock:
                final_epoch = self._speaker.interrupt()
            client.close(reason="rollover" if not self._stop.is_set() else "user_stop")
            self._speaker.wait_flushed(final_epoch, timeout=0.5)

    def _uplink_loop(self, client: RealtimeClient) -> None:
        chunk_frames = self._s.chunk_ms // 20
        frames: list[np.ndarray] = []
        packets: queue.Queue[UplinkPacket] = queue.Queue(maxsize=4)
        sender_halt = threading.Event()
        camera = self._camera
        vision_start = camera.stats() if camera is not None else None
        sender = threading.Thread(
            target=self._send_loop,
            args=(client, packets, sender_halt, camera),
            name="yrobot-uplink",
            daemon=True,
        )
        sender.start()
        while self._mic.read_frames():  # drop audio captured during session setup
            pass
        t0 = time.monotonic()
        kv_est = 0.0
        last_poll = time.monotonic()
        last_gap_log = -1e9
        try:
            while not self._stop.is_set() and not self._session_dead.is_set():
                poll_at = time.monotonic()
                capture_gap = poll_at - last_poll
                last_poll = poll_at
                if capture_gap > 0.06 and poll_at - last_gap_log > 2.0:
                    logger.warning("microphone loop gap %.0f ms", capture_gap * 1000)
                    last_gap_log = poll_at
                frames.extend(self._process_mic())
                now = time.monotonic()
                with self._turn_lock:
                    timed_out = self._gate.timed_out(now)
                if timed_out:
                    logger.error("barge-in boundary timed out; reconnecting instead of replaying")
                    return
                # MiniCPM-o 4.5 only advances on complete one-second units.
                # A partial force packet can produce a synthetic listen while
                # never executing the force override, so never flush early.
                if len(frames) < chunk_frames:
                    continue
                raw_chunk = np.concatenate(frames[:chunk_frames])
                del frames[:chunk_frames]
                if not self._audio_input_enabled():
                    continue
                # ---------- silence gate ----------
                # Activate instantly on any user voice; suspend after
                # 15 s of mutual silence to prevent echo loops.
                if not self._uplink_live:
                    if self._confirmed_user_active(now):
                        self._uplink_live = True
                        self._last_delta_at = now
                        logger.info("silence gate: uplink resumed (user voice)")
                    else:
                        continue
                elif (
                    now - self._last_delta_at > 15.0
                    and not self._speaker.audible(now)
                    and not self._confirmed_user_active(now)
                ):
                    self._uplink_live = False
                    logger.info(
                        "silence gate: uplink paused (%.0f s of mutual silence)",
                        now - self._last_delta_at,
                    )
                    continue
                # ---------- end silence gate ----------
                chunk = self._agc.process(
                    raw_chunk,
                    playback_active=self._speaker.playing(now),
                    confirmed_user_voice=self._confirmed_user_active(now),
                )
                self._input_sequence += 1
                input_id = f"input_{self._input_sequence:08d}"
                with self._turn_lock:
                    force_listen = self._gate.chunk_force_listen(now)
                self._enqueue_packet(
                    packets,
                    UplinkPacket(
                        chunk,
                        force_listen=force_listen,
                        captured_at=now,
                        input_id=input_id,
                    ),
                    # Prioritize the first causally valid force unit over stale
                    # queued silence/echo on a congested wireless uplink.
                    flush_backlog=force_listen,
                )
                busy = self._speaker.audible(now) or self._confirmed_user_active(now)
                kv_est += (KV_PER_S_BUSY if busy else KV_PER_S_IDLE) * len(raw_chunk) / 16_000
                rotation_kv = self._server_kv
                if rotation_kv is None:
                    rotation_kv = kv_est + self._video_kv_est
                if self._should_rotate(now - t0, rotation_kv, now):
                    logger.info(
                        "rotating session (%.0f s, %.0f kv tokens%s)",
                        now - t0,
                        rotation_kv,
                        " server" if self._server_kv is not None else " estimated",
                    )
                    return
        finally:
            sender_halt.set()
            sender.join(timeout=2)
            if camera is not None:
                assert vision_start is not None
                self._log_vision_stats("session", _stats_delta(camera.stats(), vision_start))

    def _speak_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return

        def run() -> None:
            try:
                pcm = synthesize_speech_24k(text)
            except Exception as exc:  # noqa: BLE001 - status TTS should not stop conversation
                logger.warning("status TTS failed: %s", exc)
                return
            epoch = self._speaker.epoch
            self._speaker.play(epoch, pcm)
            self._speaker.utterance_end()

        threading.Thread(target=run, name="yrobot-status-tts", daemon=True).start()

    def _send_loop(
        self,
        client: RealtimeClient,
        packets: queue.Queue[UplinkPacket],
        halt: threading.Event,
        camera: LatestCamera | None,
    ) -> None:
        """Serialize network writes without ever blocking mic capture."""
        while not halt.is_set() and not self._session_dead.is_set():
            try:
                packet = packets.get(timeout=0.05)
            except queue.Empty:
                continue
            jpeg = camera.take_latest() if camera is not None else None
            started = time.monotonic()
            queue_ms = (started - packet.captured_at) * 1000
            try:
                client.send_chunk(
                    packet.audio,
                    jpeg,
                    packet.force_listen,
                    packet.input_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("uplink ended: %s", exc)
                self._session_dead.set()
                return
            sent_at = time.monotonic()
            send_ms = (sent_at - started) * 1000
            if packet.force_listen:
                with self._turn_lock:
                    self._gate.force_sent(packet.input_id, sent_at)
                logger.info(
                    "force_listen sent: %.0f ms from user onset, %.0f ms queue age, "
                    "%.0f ms websocket",
                    (sent_at - self._last_user_onset_at) * 1000,
                    queue_ms,
                    send_ms,
                )
            if jpeg is not None:
                self._video_kv_est += KV_PER_FRAME
                if camera is not None:
                    camera.mark_sent()
            if send_ms > 300 or queue_ms > 150:
                logger.warning(
                    "slow uplink: websocket %.0f ms, queue age %.0f ms, depth %d, video=%s",
                    send_ms,
                    queue_ms,
                    packets.qsize(),
                    jpeg is not None,
                )

    @staticmethod
    def _enqueue_packet(
        packets: queue.Queue[UplinkPacket],
        packet: UplinkPacket,
        *,
        flush_backlog: bool = False,
    ) -> None:
        """Bound realtime backlog; retain the freshest audio under overload."""
        if flush_backlog:
            discarded = 0
            while True:
                try:
                    packets.get_nowait()
                    discarded += 1
                except queue.Empty:
                    break
            if discarded:
                logger.info("barge-in dropped %d stale queued uplink chunks", discarded)
        try:
            packets.put_nowait(packet)
            return
        except queue.Full:
            pass
        try:
            packets.get_nowait()
        except queue.Empty:
            pass
        packets.put_nowait(packet)
        logger.error("uplink backlog full: dropped oldest audio chunk")

    def _process_mic(self) -> list[np.ndarray]:
        """Read mic frames; run VAD, barge-in and posture per 20 ms frame.

        The XVF3800 profile conditions double-talk before WebRTC VAD. Clear,
        speech-shaped near-end evidence takes the fast path; ambiguous sound
        keeps the longer echo-aware confirmation that rejects motor knocks.
        """
        out = self._mic.read_frames()
        # A device read may return several frames. Approximate their capture
        # times instead of assigning the oldest frame a timestamp in the
        # future after a delayed device read.
        now = time.monotonic() - FRAME_S * max(0, len(out) - 1)
        for frame in out:
            robot_sounding = self._speaker.sounding(now)
            robot_turn_live = self._speaker.audible(now)
            voiced = self._detector.process(frame, now, floor_frozen=robot_sounding)
            if robot_turn_live and not self._gate_latched():
                decision = self._barge.process(
                    frame,
                    voiced=voiced,
                    raw_streak=self._detector.streak,
                    now=now,
                    echo_match=self._speaker.echo_match,
                )
                if decision is not None:
                    self._begin_barge(now, decision=decision)
            else:
                self._barge.reset()
            # Once a barge is latched, every voiced frame keeps force sticky.
            # While the robot is silent, ordinary user speech still drives
            # DoA/camera activity without creating an interruption.
            if voiced and (not robot_sounding or self._gate_latched()):
                self._mark_user_voice(now)
                with self._turn_lock:
                    self._gate.user_frame(True, False, now)
            now += FRAME_S
        now = time.monotonic()
        if self._speaker.playing(now):
            self._choreo.set_mode(SPEAK)
        elif self._confirmed_user_active(now) or self._gate_latched():
            self._choreo.set_mode(LISTEN)
        else:
            self._choreo.set_mode(IDLE)
        return out

    def _begin_barge(
        self,
        now: float,
        *,
        decision: BargeDecision | None = None,
    ) -> None:
        """Atomically suppress output and hard-stop the interrupted local turn."""
        with self._turn_lock:
            started = self._gate.user_frame(True, True, now)
            if started:
                self._captions = ThinkFilter()
                epoch = self._speaker.interrupt()
        if started:
            onset_at = decision.onset_at if decision is not None else now
            self._mark_user_voice(onset_at)
            if decision is None:
                logger.info(
                    "barge-in: local playback discarded at epoch %d (mic %.1f dB); "
                    "force latched for next complete unit",
                    epoch,
                    self._detector.last_db,
                )
            else:
                match = decision.match
                logger.info(
                    "barge-in %s path: playback discarded at epoch %d after %.0f ms "
                    "(mic %.1f dB, similarity %.2f, unexplained %.1f dB, lag %.0f ms); "
                    "force latched for next complete unit",
                    decision.path,
                    epoch,
                    decision.evidence_ms,
                    self._detector.last_db,
                    match.similarity,
                    match.unexplained_db,
                    match.lag_ms,
                )

    def _mark_user_voice(self, now: float) -> None:
        if now - self._last_voice_at >= VoiceDetector.HANGOVER_S:
            self._last_user_onset_at = now
        self._last_voice_at = now
        self._confirmed_voice_until = max(
            self._confirmed_voice_until,
            now + VoiceDetector.HANGOVER_S,
        )

    def _confirmed_user_active(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return now < self._confirmed_voice_until

    def _gate_latched(self) -> bool:
        with self._turn_lock:
            return self._gate.latched

    def _current_head_yaw(self) -> float:
        """Read the daemon's cached physical pose; fall back during startup."""
        try:
            return head_yaw_of(np.asarray(self._mini.get_current_head_pose()))
        except Exception:  # noqa: BLE001
            return self._choreo.current_yaw()

    def _should_rotate(self, elapsed: float, kv_est: float, now: float) -> bool:
        quiet = (
            not self._gate_latched()
            and not self._speaker.audible(now)
            and not self._confirmed_user_active(now)
        )
        return self._rotation.should_rotate(elapsed_s=elapsed, kv_tokens=kv_est, quiet=quiet)

    # -- gateway callbacks (yrobot-recv thread) -------------------------------

    def _on_delta(self, delta: Delta, session_sequence: int | None = None) -> None:
        if session_sequence is not None and session_sequence != self._session_sequence:
            logger.debug("ignored delta from stale session %d", session_sequence)
            return
        now = delta.received_at
        self._last_delta_at = now
        kv = delta.metrics.get("kv_cache_length")
        if isinstance(kv, int | float):
            self._server_kv = float(kv)
        if delta.kind == "listen":
            with self._turn_lock:
                was_latched = self._gate.latched
                acknowledged = self._gate.model_listen(now, delta.input_id)
                if not self._gate.latched:
                    self._speaker.utterance_end()
            if acknowledged:
                logger.info(
                    "force_listen acknowledged by %s: waiting for user turn end",
                    delta.input_id or "causal fallback",
                )
            elif was_latched:
                logger.info(
                    "ignored unmatched listen from %s; interrupted turn remains suppressed",
                    delta.input_id or "missing input_id",
                )
        elif delta.kind == "audio":
            with self._turn_lock:
                was_latched = self._gate.latched
                allowed = self._gate.model_audio(now, delta.response_id)
                allowed = (
                    allowed
                    and delta.response_id not in self._muted_response_ids
                    and delta.response_id not in self._suppressed_response_ids
                )
                if allowed:
                    epoch = self._speaker.epoch
                    self._speaker.play(epoch, delta.audio)
            if was_latched and allowed:
                logger.info("barge-in boundary complete: accepting new model response")
            if (
                allowed
                and self._last_user_onset_at > self._last_logged_audio_onset_at
                and now - self._last_user_onset_at < 30.0
            ):
                self._last_logged_audio_onset_at = self._last_user_onset_at
                logger.info(
                    "first accepted audio %.0f ms after confirmed voice onset (response %s)",
                    (now - self._last_user_onset_at) * 1000,
                    delta.response_id or "unknown",
                )
        elif delta.kind == "text":
            if delta.response_id in self._suppressed_response_ids:
                return
            with self._turn_lock:
                was_latched = self._gate.latched
                allowed = self._gate.model_text(now, delta.response_id)
                fragment = self._captions.feed(delta.text) if allowed else ""
                caption = fragment.strip()
            if was_latched and allowed:
                logger.info("barge-in boundary complete: accepting new model response")
            if caption:
                logger.info("robot: %s", caption)
                # Only process commands when user actually spoke recently.
                # This prevents the model's own words from triggering HA/Hermes
                # when wake was caused by noise/echo rather than user intent.
                user_voice_gap = now - self._last_user_onset_at
                if user_voice_gap < 15.0:
                    info_result = self._local_info.handle_text(caption, delta.response_id or "")
                    if info_result is not None:
                        if info_result.mute_model_audio and delta.response_id:
                            self._suppress_response(delta.response_id)
                        if info_result.ok:
                            logger.info("Local info result: %s", info_result.message)
                            self._speak_text(info_result.message)
                        else:
                            logger.warning(
                                "Local info failed: %s: %s",
                                info_result.name,
                                info_result.message,
                            )
                    result = self._home_assistant.handle_text(caption, delta.response_id or "")
                    if result is not None:
                        if result.ok:
                            logger.info("Home Assistant action succeeded: %s", result.action.name)
                            if delta.response_id:
                                self._suppress_response(delta.response_id)
                            if result.action.response:
                                self._speak_text(result.action.response)
                        else:
                            logger.warning(
                                "Home Assistant action failed: %s: %s",
                                result.action.name,
                                result.detail,
                            )
                    tool_result = self._hermes_tools.handle_text(caption, delta.response_id or "")
                    if tool_result is not None:
                        if tool_result.mute_model_audio and delta.response_id:
                            self._suppress_response(delta.response_id)
                        if tool_result.ok:
                            logger.info("Hermes tool result: %s", tool_result.message)
                            self._speak_text(tool_result.message)
                        else:
                            logger.warning(
                                "Hermes tool failed: %s: %s",
                                tool_result.name,
                                tool_result.message,
                            )
                else:
                    logger.debug(
                        "skipped command handlers (no user voice for %.0f s)",
                        user_voice_gap,
                    )
                if delta.response_id not in self._suppressed_response_ids:
                    self._memory.append_assistant(fragment)

    def _suppress_response(self, response_id: str) -> None:
        if not response_id:
            return
        self._muted_response_ids.add(response_id)
        self._suppressed_response_ids.add(response_id)
        self._captions = ThinkFilter()
        self._speaker.interrupt()

    def _on_closed(self, reason: str, session_sequence: int | None = None) -> None:
        if session_sequence is not None and session_sequence != self._session_sequence:
            logger.debug("ignored close from stale session %d: %s", session_sequence, reason)
            return
        logger.info("session closed: %s", reason)
        self._session_dead.set()

    @staticmethod
    def _log_vision_stats(scope: str, stats: VisionStats) -> None:
        logger.info(
            "vision %s stats: captured=%d changed=%d published=%d selected=%d sent=%d failures=%d",
            scope,
            stats.captured,
            stats.changed,
            stats.published,
            stats.selected,
            stats.sent,
            stats.failures,
        )


def _stats_delta(after: VisionStats, before: VisionStats) -> VisionStats:
    """Return per-session counters from a camera shared across sessions."""
    return VisionStats(
        **{
            name: getattr(after, name) - getattr(before, name)
            for name in VisionStats.__dataclass_fields__
        }
    )


class Yrobot(ReachyMiniApp):
    """Reachy Mini app entry point (``reachy_mini_apps`` group)."""

    custom_app_url: str | None = "http://0.0.0.0:8042"

    def __init__(self, running_on_wireless: bool = False) -> None:
        load_dotenv()
        super().__init__(running_on_wireless=running_on_wireless)
        self._config = AppConfig()
        self._media_holder = _MediaHolder()
        assert self.settings_app is not None
        register_settings_routes(
            self.settings_app,
            self._config,
            media_holder=self._media_holder,
        )

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        self._media_holder.media = reachy_mini.media
        self._wake_up_if_needed(reachy_mini)
        environment = self._config.effective_environment(os.environ)
        Conversation(Settings.from_env(environment), reachy_mini, stop_event).run()

    @staticmethod
    def _wake_up_if_needed(reachy_mini: ReachyMini) -> None:
        """Wake the robot head if it is still in the sleep pose at startup.

        Mirrors the official conversation app lifecycle: after a cold boot the
        head may stay lowered in the sleep pose, and the choreographer then
        fights a dead/depowered pose. A short wake_up() brings it back to the
        neutral position so speech/look animations have a valid baseline.
        """
        try:
            from reachy_mini.reachy_mini import SLEEP_HEAD_POSE
            from reachy_mini.utils.interpolation import distance_between_poses

            try:
                reachy_mini.enable_motors()
                logger.info("motors enabled for startup wake-up")
            except Exception as exc:  # noqa: BLE001 - wake-up remains best effort
                logger.warning("motor enable failed before wake-up: %s", exc)
            pose = reachy_mini.get_current_head_pose()
            if pose is None:
                logger.info("head pose unavailable; skipping wake-up check")
                return
            pose = np.asarray(pose, dtype=np.float64)
            if pose.shape != (4, 4):
                logger.warning("unexpected head pose shape %s; skipping wake-up", pose.shape)
                return
            t_dist, r_dist, _ = distance_between_poses(pose, SLEEP_HEAD_POSE)
            if t_dist <= 0.05 and r_dist <= 0.35:
                logger.info("head in sleep pose; running wake-up movement")
                reachy_mini.wake_up()
            else:
                logger.info("head not in sleep pose; skipping wake-up")
        except Exception as exc:  # noqa: BLE001 - startup wake-up is best effort
            logger.warning("wake-up check failed: %s", exc)


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
