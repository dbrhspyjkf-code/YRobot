"""Application wiring: Reachy Mini Xiaozhi cloud integration.

Thread map:
    asyncio       mic → Opus encode → WebSocket → TTS decode → aplay
    face-tracker  OpenCV Haar cascade (~2 fps) → visual gaze target
    yrobot-motion 50 Hz Choreographer (owns the robot pose)
    yrobot-doa    12 Hz SoundCompass → audio gaze target
"""

from __future__ import annotations

import base64
import logging
import math
import os
import random
import re
import threading
import time
import wave
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncio
import cv2

if TYPE_CHECKING:
    from yrobot.audio_runtime import WakeGate
import numpy as np
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from reachy_mini.apps.app import ReachyMiniApp
from reachy_mini.reachy_mini import ReachyMini

from yrobot.xiaozhi_mcp import XiaozhiMcpServer
from yrobot.app_config import (
    _MediaHolder,
    audio_input_controller_singleton,
    register_photo_routes,
    register_settings_routes,
    volume_controller_singleton,
)
from yrobot.audio import apply_audio_startup_config
from yrobot.audio_runtime import WAKE_TIMEOUT as _GATE_WAKE_TIMEOUT
from yrobot.command_recognizer import CommandRecognizer
from yrobot.config import Settings
from yrobot.conversation_resume import ConversationWakeLease
from yrobot.hermes_photo_intent import HermesPhotoIntentNotifier
from yrobot.photo_cloud_guard import LocalPhotoCloudQuarantine
from yrobot.photo_feedback import LocalPhotoFeedback, run_local_photo_flow
from yrobot.photos import (
    PhotoCommandController,
    PhotoLibrary,
    openssh_runner,
)
from yrobot.qwen_emotion import (
    IdentityStabilizer,
    WakeGreetingGate,
    requested_dance,
    requested_emotion,
)
from yrobot.faces import FaceDB
from yrobot.state import ROBOT_STATE, RUNTIME_HEALTH
from yrobot.uplink_vad import DECISION_END, EnergyHangoverVAD, PrerollBuffer
from yrobot.xiaozhi_mqtt import (
    UdpChannelInfo,
    XiaozhiMqttTransport,
    XiaozhiUdpAudio,
    hello_request,
    is_xiaozhi_conversation_response,
)
from yrobot.xiaozhi_photo import start_local_photo_flow
from yrobot.xiaozhi_ota import (
    OTAError,
    OtaCredentialCache,
    fetch_mqtt_config,
    load_or_create_client_uuid,
)

# ── Xiaozhi cloud device identity (read from network interface) ──────────────
try:
    with open("/sys/class/net/wlan0/address") as f:
        XIAOZHI_DEVICE_ID = f.read().strip()
except Exception:
    XIAOZHI_DEVICE_ID = ""
XIAOZHI_DEVICE_ID = os.environ.get("XIAOZHI_DEVICE_ID", XIAOZHI_DEVICE_ID)
XIAOZHI_CONV_URL = os.environ.get("XIAOZHI_CONV_URL", "wss://api.tenclass.net/xiaozhi/v1/")
XIAOZHI_TOKEN = os.environ.get("XIAOZHI_TOKEN", "test-token")

# Configure logging early so both uvicorn (`python -m yrobot.main`) and
# cli() (`python yrobot/main.py`) get a working root logger. The basicConfig
# inside cli() below only runs in the second path.
logging.basicConfig(
    level=os.environ.get("YROBOT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
)


logger = logging.getLogger(__name__)

# ── Safe-mode startup guard ────────────────────────────────────────────────────
# Persisted across systemd restarts so a deterministic boot loop counts toward
# the threshold. A successful run() clears the counter.
_STARTUP_FAIL_COUNTER_PATH = "/tmp/.yrobot_startup_failures"
_MAX_STARTUP_FAILURES = 3
_MOTION_SET_TARGET_RESTART_FAILURES = 1500  # ~30s at 50 Hz
_QWEN_RECONNECT_MESSAGES = (
    "no response was generated for 300 seconds",
    "session was closed",
    "internal service error",
    "conversation already has an active response",
    "conversation has none active response",
    "timed out during opening handshake",
)
_QWEN_ACTIVE_SILENCE_FRAMES = 16
_QWEN_PRE_WAKE_SILENCE_FRAMES = 24
_QWEN_MAX_TURN_FRAMES = 167
_QWEN_FACE_SPEAKER_STABLE_S = 3.0


class RecentTranscriptWindow:
    """Build short ASR-fragment candidates without widening device control."""

    def __init__(self, *, window_s: float = 3.0, max_items: int = 4) -> None:
        self.window_s = window_s
        self.max_items = max_items
        self._items: list[tuple[float, str]] = []

    def candidates(self, transcript: str, *, now: float | None = None) -> list[str]:
        text = transcript.strip()
        if not text:
            return []
        current = time.monotonic() if now is None else now
        self._items = [(at, value) for at, value in self._items if current - at <= self.window_s]
        self._items.append((current, text))
        self._items = self._items[-self.max_items :]
        values = [value for _, value in self._items]
        joined = "".join(values)
        return [text] if joined == text else [text, joined]


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


def _clear_startup_failure_counter() -> None:
    """Clear the persisted counter only after a complete run returns."""
    try:
        os.remove(_STARTUP_FAIL_COUNTER_PATH)
    except FileNotFoundError:
        pass


def _start_motion_connection_watchdog(choreo, stop_event: threading.Event) -> threading.Thread:
    """Restart YRobot when the SDK set_target connection is gone for ~30s."""

    def _watch() -> None:
        while not stop_event.wait(5.0):
            try:
                status = choreo.get_status()
            except Exception as exc:  # noqa: BLE001
                logger.debug("motion watchdog status failed: %s", exc)
                continue
            consecutive = int(status.get("set_target_consecutive_failures") or 0)
            if consecutive >= _MOTION_SET_TARGET_RESTART_FAILURES:
                logger.error(
                    "motion set_target connection lost for %d consecutive writes; restarting YRobot",
                    consecutive,
                )
                os._exit(70)

    thread = threading.Thread(target=_watch, name="motion-connection-watchdog", daemon=True)
    thread.start()
    return thread


def _qwen_should_reconnect(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(fragment in message for fragment in _QWEN_RECONNECT_MESSAGES)


def _qwen_spoken_control_result_text(result: dict[str, Any]) -> str | None:
    if not result.get("ok"):
        return None
    exact = str(result.get("result") or "").strip()
    return exact or None


def _qwen_spoken_control_result_feedback(result: dict[str, Any]) -> str:
    exact = str(result.get("result") or "").strip()
    if result.get("ok") and exact:
        return (
            "[system] Local tool returned the final answer. "
            "Read only the exact sentence below; do not change any number, code, or unit:\n"
            f"{exact}"
        )
    if result.get("ok"):
        device = str(result.get("device") or "").strip()
        action = str(result.get("action") or "").strip()
        outcome = f"已成功：{device} {action}".strip() if (device or action) else "已成功"
    else:
        error_message = result.get("error", "unknown")
        outcome = f"执行失败：{error_message}"
    return (
        f"[系统] 本地工具刚刚执行了一次设备控制请求，真实结果是：{outcome}。"
        f"请基于这个事实向用户简短说明，不要再说我交给本地控制之类的中间话术。"
    )


def _qwen_unmatched_spoken_control_feedback(candidates: list[str]) -> str | None:
    normalized = [_qwen_normalize_for_control(candidate) for candidate in candidates]
    joined = "".join(normalized)
    if not joined:
        return None
    has_volume = "音量" in joined or "声音" in joined
    has_sonos = any(fragment in joined for fragment in ("音响", "音箱", "sonos"))
    if has_sonos and has_volume:
        return (
            "[系统] 用户刚才像是在说音响音量，但没有听到明确方向或数值；"
            "本地没有执行任何音响控制。请只追问：调大、调小，还是调到多少？"
        )
    if has_volume:
        return (
            "[系统] 用户刚才像是在说音量，但没有听到明确对象和动作；"
            "本地没有执行任何音量控制。请只追问：是音响、电视，还是你的音量？要调大还是调小？"
        )
    return None


def _qwen_normalize_for_control(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _qwen_sonos_fragment(value: str) -> bool:
    return _qwen_normalize_for_control(value) in {
        "音响",
        "音响音",
        "音响音量",
        "音箱",
        "音箱音",
        "音箱音量",
    }


def _qwen_step_direction(value: str) -> str | None:
    text = _qwen_normalize_for_control(value)
    # In the Sonos follow-up flow QWEN often hears the short answer
    # “调大” as “搅拌/交大”.  This helper is only used after an explicit
    # recent Sonos-volume fragment, so mapping those narrow homophones
    # is safer than letting the model claim it adjusted volume without
    # a local tool result.
    if any(
        phrase in text
        for phrase in ("调大", "搅拌", "交大", "大一点", "大点", "声音大", "加大", "加点", "提高")
    ):
        return "调大"
    if any(phrase in text for phrase in ("调小", "小一点", "小点", "声音小", "减小", "降低")):
        return "调小"
    return None


def _qwen_assistant_sonos_step_command(last_user_text: str, assistant_text: str) -> str | None:
    return None


def _qwen_contextual_sonos_step_command(last_user_text: str, current_user_text: str) -> str | None:
    if not _qwen_sonos_fragment(last_user_text):
        return None
    direction = _qwen_step_direction(current_user_text)
    return f"音响音量{direction}" if direction else None


def _qwen_should_request_response_after_local_control(
    local_matched: bool, command_recognizer_matched: bool
) -> bool:
    return not local_matched and not command_recognizer_matched


_qwen_photo_controller = PhotoCommandController()


async def _qwen_dispatch_photo_command(
    candidates: list[str],
    photo_library: PhotoLibrary,
    client: Any,
) -> bool | None:
    """Dispatch a photo capture from the most recent matching STT fragment.

    Returns ``True`` when the photo path answered (so the model should NOT
    speak), ``None`` when no candidate contained a photo trigger.
    """
    for candidate in reversed(candidates):
        if not _qwen_photo_controller.observe(candidate):
            continue
        outcome = await asyncio.to_thread(
            photo_library.capture_from_voice, source="voice-qwen"
        )
        if outcome.accepted:
            short_id = outcome.photo_id[:12]
            message = (
                f"[系统] 本地工具刚刚拍摄一张照片，photo_id={short_id}。"
                "请用一句自然语言告知用户已拍照，正在同步到相册，"
                "不要杜撰上传结果。"
            )
            logger.info(
                "qwen photo accepted id=%s source=voice-qwen", outcome.photo_id
            )
        else:
            message = (
                f"[系统] 本地拍照请求未执行，原因：{outcome.message}。"
                "请如实告诉用户发生了什么，不要编造已拍摄或上传。"
            )
            logger.info("qwen photo rejected reason=%s", outcome.message)
        await client.cancel_and_inject(message)
        return True
    return None


def _qwen_should_resume_wake_after_reconnect(gate: WakeGate) -> bool:
    return gate.active


_QWEN_DEVICE_INTENT_ACTIONS = (
    "打开",
    "关闭",
    "关掉",
    "关上",
    "关一下",
    "开一下",
    "开灯",
    "关灯",
    "调高",
    "调低",
    "调到",
    "切换",
)


def _qwen_device_intent(transcript: str) -> bool:
    """Detect a device-control attempt in a transcript.

    Used after the local spoken-control parser fails to match: those turns
    must inject the truth (nothing was executed) instead of letting the
    model fabricate a “好的，已关闭” reply for an unmatched device name.
    """
    text = transcript.replace(" ", "")
    return any(action in text for action in _QWEN_DEVICE_INTENT_ACTIONS)


_QWEN_BACKCHANNEL_TEXTS = frozenset(
    {
        "好",
        "好的",
        "好呀",
        "好啦",
        "好的呀",
        "行",
        "行吧",
        "嗯",
        "嗯嗯",
        "哦",
        "噢",
        "对",
        "是的",
        "没事",
        "没事了",
        "算了",
        "ok",
        "okay",
    }
)


def _qwen_is_bare_wake_utterance(transcript: str) -> bool:
    """True when the whole utterance is just the wake word/alias.

    The wake path already activated the session and requested the
    greeting response. When the cloud ASR transcript of that same phrase
    comes back (always ~1-2 s after a KWS hit), it must NOT trigger
    another response.create: DashScope rejects it mid-stream ("Conversation
    has none active response") and closes the session. Utterances like
    "你好小白，查一下天气" still carry a request and stay eligible.
    """
    norm = _WAKE_STRIP_RE.sub("", transcript).casefold().strip()
    if not norm:
        return False
    wake_norms = {
        _WAKE_STRIP_RE.sub("", word).casefold().strip()
        for word in (*WAKE_WORDS, *_WAKE_ASR_ALIASES)
    }
    return norm in wake_norms


def _qwen_is_pure_backchannel(transcript: str) -> bool:
    """True when the whole utterance is just an acknowledgement.

    Replying “我在呢，有什么事吗？” to the user's own “好。” after a
    completed device action is noise: these turns get a nod instead of a
    model response.
    """
    text = "".join(ch for ch in transcript.casefold() if ch.isalnum())
    return text in _QWEN_BACKCHANNEL_TEXTS


def _qwen_wants_visual_snapshot(transcript: str) -> bool:
    text = transcript.replace(" ", "")
    return any(
        phrase in text
        for phrase in ("看到", "看见", "看一下", "看看", "看这个", "这是什么", "描述", "画面", "前面", "手里")
    )


class _XiaozhiReconnect(Exception):
    """Expected Xiaozhi session close that should reconnect without traceback."""


class _XiaozhiPaused(Exception):
    """Local mic gate paused Xiaozhi; reconnect after input is enabled."""


def _xiaozhi_should_pause_for_mic(input_enabled: bool) -> bool:
    return not input_enabled


def _websocket_close_code(exc: BaseException) -> int | None:
    for attr in ("code",):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    for attr in ("rcvd", "sent"):
        frame = getattr(exc, attr, None)
        value = getattr(frame, "code", None)
        if isinstance(value, int):
            return value
    return None


def _is_expected_xiaozhi_disconnect(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if current.__class__.__name__ == "ConnectionClosedOK":
            return True
        code = _websocket_close_code(current)
        if code in {1000, 1001, 1005}:
            return True
        current = current.__cause__
    return False


_IDLE_SHOW_ENABLED = os.environ.get("YROBOT_IDLE_SHOW", "1").strip() != "0"
_IDLE_SHOW_INTERVAL_S = max(
    30.0, float(os.environ.get("YROBOT_IDLE_SHOW_S", "180"))
)


def _play_idle_show(choreo: Any, rec_provider: Any = None) -> str | None:
    """Weighted idle performance using the official app idle policy weights.

    60% stillness / 16% recorded emotion / 16% dance / 8% head tilt.
    """
    from yrobot.motion import recorded_move_for

    roll = random.random()
    if roll < 0.60:
        return None
    if roll < 0.76:
        emotion = random.choice(("happy", "surprised", "thinking", "grateful", "loving"))
        name = recorded_move_for(emotion)
        rec = rec_provider() if callable(rec_provider) and name else None
        if rec is not None and name and choreo.play_recorded(name, rec):
            return f"recorded:{name}"
        return None
    if roll < 0.92 and choreo.play_dance("simple_nod"):
        return "dance:simple_nod"
    return "tilt" if choreo.play_move("tilt") else None


# Xiaozhi's LLM emits emotion=happy on nearly every reply as its default
# expression; it carries no content signal and must not gesture on its own.
# Informative emotions still gesture; happy only via sentence keywords.
_LLM_EMOTION_NOISE = frozenset({"", "happy", "neutral", "none", "ok", "normal"})
# Minimum gap between any two emotion-triggered moves, so multi-sentence
# replies do not machine-gun gestures.
_EMOTION_MOVE_GLOBAL_COOLDOWN_S = 12.0


# Wake phrase for the xiaozhi session (user directive 2026-08-17: ONLY
# 你好小白). Strict matching (_wake_match): the whole utterance must equal
# the phrase (after punctuation stripping), or start with 你好小白 followed
# by an actual request. Ambient conversation must never wake the robot.
WAKE_WORDS = (
    "你好小白",
)
# Observed ASR mis-hearings of the wake phrase (cloud ASR often returns
# 你好小孩 for 你好小白). Exact-match aliases: they wake the robot but do
# NOT get the startswith(...) extension.
_WAKE_ASR_ALIASES = (
    "你好小孩",
    # 2026-08-19 field report: ASR swallowed the 好 (你小白。). Exact-match
    # only — no prefix extension, so ambient speech stays gated.
    "你小白",
)

_WAKE_STRIP_RE = re.compile(r"[，。！？：；!?,.:;\s、～~]")


def _wake_match(text: str) -> str | None:
    """Strict wake-phrase match that ignores ambient conversation.

    Ambient speech must never wake the robot just because it happens to
    contain a wake-word substring (field report: "我要小白。" in background
    conversation woke it). After stripping punctuation/whitespace the whole
    utterance must equal a wake phrase, or start with the canonical address
    "你好小白…" followed by an actual request.
    """
    norm = _WAKE_STRIP_RE.sub("", text).casefold().strip()
    if not norm:
        return None
    for alias in _WAKE_ASR_ALIASES:
        if norm == _WAKE_STRIP_RE.sub("", alias).casefold().strip():
            return alias
    for w in WAKE_WORDS:
        wn = _WAKE_STRIP_RE.sub("", w).casefold().strip()
        if not wn:
            continue
        if norm == wn:
            return w
        if wn == "你好小白" and norm.startswith(wn):
            return w
    return None


_RECORDED_MOVES: list[Any] = [None]


def _get_recorded() -> Any:
    """Lazy singleton for the official emotion library (85 recorded moves).

    Shared by both backends; loads on first use so slow first loads do not
    block startup.
    """
    if _RECORDED_MOVES[0] is None:
        try:
            from reachy_mini.motion.recorded_move import RecordedMoves

            _RECORDED_MOVES[0] = RecordedMoves(
                "pollen-robotics/reachy-mini-emotions-library"
            )
        except Exception as exc:
            logger.warning("emotion library unavailable: %s", exc)
    return _RECORDED_MOVES[0]


def _play_emotion_move(
    choreo: Any,
    emo: str,
    last: dict[str, float],
    rec_provider: Any = None,
    *,
    prefer_recorded: bool = False,
    source: str = "llm",
    tag: str = "xz",
) -> bool:
    """Map an emotion to a recorded move with programmatic fallback.

    Shared by both backends. Recorded moves come from the official curated
    whitelist and rotate per emotion; if the library is unavailable or the
    move is rejected, the safe programmatic move is used instead. A 12 s
    global cooldown keeps any session from emoting on every reply.
    """
    from yrobot.motion import EMOTION_FALLBACK_MOVE, recorded_move_for

    if source == "llm":
        logger.info("%s emotion %s ignored (autonomous gestures disabled)", tag, emo or "?")
        return False

    rec_name = recorded_move_for(emo) if prefer_recorded else None
    fb_name = EMOTION_FALLBACK_MOVE.get(emo)
    target = rec_name if prefer_recorded and rec_name else fb_name
    now = time.monotonic()
    if not target:
        logger.info("%s emotion %s (no safe move)", tag, emo or "?")
        return False
    if now - last.get("__any__", -1e9) < _EMOTION_MOVE_GLOBAL_COOLDOWN_S:
        logger.info("%s emotion %s -> %s (global cooldown)", tag, emo, target)
        return False
    if now - last.get(target, -1e9) < 5.0:
        logger.info("%s emotion %s -> %s (cooldown)", tag, emo, target)
        return False
    last["__any__"] = now
    last[target] = now
    if prefer_recorded and rec_name:
        rec = rec_provider() if callable(rec_provider) else None
        if rec is not None and choreo.play_recorded(rec_name, rec):
            logger.info("%s emotion %s -> recorded %s", tag, emo, rec_name)
            return True
    if fb_name and choreo.play_move(fb_name):
        logger.info("%s emotion %s -> safe move %s", tag, emo, fb_name)
        return True
    return False


def _handle_xiaozhi_emotion(
    choreo: Any,
    emo: str,
    last: dict[str, float],
    rec_provider: Any = None,
    *,
    prefer_recorded: bool = False,
    source: str = "llm",
) -> None:
    """Xiaozhi-backend wrapper around the shared emotion-move policy.

    The LLM's default 'happy' emotion is treated as noise (source="llm")
    because Xiaozhi emits it on almost every reply; content-corroborated
    happy arrives via source="sentence".
    """
    _play_emotion_move(
        choreo,
        emo,
        last,
        rec_provider,
        prefer_recorded=prefer_recorded,
        source=source,
        tag="xz",
    )


def _fuse_speaker_gaze(
    audio_yaw: float,
    visual_yaw: float | None,
    *,
    max_visual_audio_delta: float = math.radians(70.0),
    visual_weight: float = 0.7,
) -> tuple[float, str]:
    """Use visual gaze only when it agrees with the audio speaker direction."""
    if visual_yaw is None:
        return audio_yaw, "audio"
    delta = abs((visual_yaw - audio_yaw + math.pi) % (2 * math.pi) - math.pi)
    if delta > max_visual_audio_delta:
        return audio_yaw, "audio"
    weight = max(0.0, min(1.0, visual_weight))
    fused = audio_yaw + ((visual_yaw - audio_yaw + math.pi) % (2 * math.pi) - math.pi) * weight
    return fused, "audio+visual"


logger = logging.getLogger(__name__)


class Yrobot(ReachyMiniApp):
    """Reachy Mini app entry point (``reachy_mini_apps`` group)."""

    custom_app_url: str | None = "http://0.0.0.0:8042"

    def __init__(self, running_on_wireless: bool = False) -> None:
        load_dotenv()
        super().__init__(running_on_wireless=running_on_wireless)
        self._media_holder = _MediaHolder()
        assert self.settings_app is not None
        # Übersicht widgets are rendered by a local WebKit origin and must
        # preflight cross-origin JSON PUT/POST controls before reaching this
        # Dashboard API. Keep browser dashboard behavior unchanged while
        # permitting the desktop controller to use the existing safe routes.
        self.settings_app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["GET", "PUT", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
        )
        self._camera_streamer = register_settings_routes(
            self.settings_app, media_holder=self._media_holder
        )
        self._photo_voice_controller = PhotoCommandController()
        photo_settings = Settings.from_env()
        self._photo_intent_notifier = HermesPhotoIntentNotifier(
            photo_settings.hermes_photo_intent_url,
            photo_settings.hermes_photo_intent_secret or "",
        )
        self._local_photo_cloud_quarantine = LocalPhotoCloudQuarantine()
        self._xiaozhi_wake_lease = ConversationWakeLease(
            timeout_s=_GATE_WAKE_TIMEOUT
        )
        self._photo_feedback = LocalPhotoFeedback()
        self._photo_library = self._build_photo_library(photo_settings)
        register_photo_routes(self.settings_app, self._photo_library)

    def _run_xiaozhi_photo_flow(self, transcript: str) -> None:
        """Run the local-only capture sequence without a cloud model reply."""
        outcome = run_local_photo_flow(
            lambda: self._photo_library.capture_from_voice(source="voice-xz"),
            self._photo_feedback,
        )
        if outcome.accepted:
            logger.info(
                "xz photo accepted id=%s transcript=%r",
                outcome.photo_id,
                transcript,
            )
        else:
            logger.info(
                "xz photo rejected reason=%s transcript=%r",
                outcome.message,
                transcript,
            )

    def _schedule_xiaozhi_photo(self, transcript: str) -> None:
        # This thread must outlive the deliberately closed cloud session. An
        # asyncio task would be cancelled when that session's event loop exits.
        threading.Thread(
            target=self._run_xiaozhi_photo_flow,
            args=(transcript,),
            name="yrobot-xz-photo-capture",
            daemon=True,
        ).start()

    def _build_photo_library(self, settings: Settings | None = None) -> PhotoLibrary:
        settings = settings or Settings.from_env()
        runner = openssh_runner(settings) if settings.photo_upload_enabled else None
        return PhotoLibrary(
            settings=settings,
            camera=self._camera_streamer,
            media_holder=self._media_holder,
            sftp=runner,
        )

    @property
    def photo_library(self) -> PhotoLibrary:
        return self._photo_library

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        """Run exactly one configured conversation backend."""
        try:
            self._media_holder.media = reachy_mini.media
            self._photo_library.start()
            settings = Settings.from_env()
            RUNTIME_HEALTH.update(
                backend=settings.conversation_backend,
                last_error=None,
            )
            try:
                if settings.conversation_backend == "qwen":
                    self._run_qwen(reachy_mini, stop_event, settings)
                else:
                    self._run_xiaozhi(reachy_mini, stop_event, settings)
                _clear_startup_failure_counter()
            finally:
                self._photo_library.stop()
        except Exception as exc:
            logger.exception("YRobot startup failed: %s", exc)
            RUNTIME_HEALTH.update(ws_state="error", last_error=str(exc))
            ROBOT_STATE.set("safe_mode")
            self._photo_library.stop()
            _enter_safe_mode(self._media_holder, exc, stop_event)

    def _run_qwen(
        self,
        reachy_mini: ReachyMini,
        stop_event: threading.Event,
        settings: Settings,
    ) -> None:
        """Run wake-gated Qwen PCM audio without touching the official daemon."""
        if not settings.qwen_api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for QWEN")

        import asyncio

        import numpy as np
        import sounddevice as sd

        from yrobot.app_config import motion_controller_singleton
        from yrobot.audio import _publish_dashboard_mic, get_vad_rms_min
        from yrobot.audio_runtime import PcmPlayback, WakeGate
        from yrobot.motion import IDLE, LISTEN, SPEAK, Choreographer
        from yrobot.qwen_realtime import (
            DEFAULT_INSTRUCTIONS,
            VISION_POLICY,
            QwenRealtimeClient,
            _model_url,
        )
        from yrobot.qwen_tools import ToolExecutor
        from yrobot.kws import KeywordWakeDetector

        camera_streamer = self._camera_streamer

        startup_head_pose = None
        startup_antennas = None
        try:
            startup_head_pose = reachy_mini.get_current_head_pose()
            _, antenna_joints = reachy_mini.get_current_joint_positions()
            startup_antennas = (float(antenna_joints[0]), float(antenna_joints[1]))
        except Exception as exc:
            logger.warning("could not capture QWEN startup pose: %s", exc)

        motor_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                reachy_mini.enable_motors()
                motor_error = None
                break
            except Exception as exc:
                motor_error = exc
                logger.warning("QWEN motor enable failed (attempt %d/3): %s", attempt, exc)
                if attempt < 3:
                    stop_event.wait(1.0)
        if motor_error is not None:
            RUNTIME_HEALTH.update(motor_ready=False)
            raise RuntimeError("Reachy motors could not be enabled") from motor_error

        RUNTIME_HEALTH.update(motor_ready=True)
        choreo = Choreographer(
            reachy_mini,
            startup_head_pose=startup_head_pose,
            startup_antennas=startup_antennas,
            startup_blend_duration=4.0,
        )
        motion_controller_singleton().set(choreo)
        choreo.start()
        _start_motion_connection_watchdog(choreo, stop_event)

        # ── SpeakerTracker: audio DoA + visual face (shared with XIAOZHI) ──
        from yrobot.tracking import SpeakerTracker

        tracker = SpeakerTracker(
            reachy_mini,
            choreo,
            head_tracking_weight=settings.head_tracking_weight,
            camera_streamer=camera_streamer,
        )
        tracker.start()

        try:
            reachy_mini.media.stop_playing()
        except Exception as exc:
            logger.warning("could not release SDK speaker for QWEN: %s", exc)
        try:
            applied_audio_profile = apply_audio_startup_config(reachy_mini.media)
            logger.info("QWEN audio startup profile applied: %s", applied_audio_profile)
        except Exception as exc:  # noqa: BLE001
            logger.warning("QWEN audio startup profile failed: %s", exc)

        # Local keyword wake detector (sherpa-onnx KWS). When enabled,
        # the detector listens on the same mic frames the loop already
        # reads; a hit on the configured keyword ("你好小白") opens the
        # WakeGate window via observe_transcript so the audio uplink
        # starts forwarding to QWEN. This is the local replacement for
        # the cloud ASR wake match, which is unreliable on short Chinese
        # phrases.
        kws_detector: KeywordWakeDetector | None = None
        if settings.wake_enabled:
            try:
                kws_detector = KeywordWakeDetector(model_dir=Path(settings.kws_model_dir))
                logger.info("KWS wake armed: phrase=%r", settings.wake_phrase)
            except Exception as exc:  # noqa: BLE001
                logger.warning("KWS wake init failed (falling back to cloud ASR): %s", exc)
        # Escape hatch: YROBOT_IDLE_UPLINK=1 forces the legacy cloud-ASR
        # idle uplink even when local KWS is armed (e.g. if field wake
        # misses make the local engine unreliable).
        _idle_uplink_forced = os.environ.get("YROBOT_IDLE_UPLINK", "").strip() == "1"
        # With local KWS armed the cloud hears NOTHING while idle: zero
        # upload, zero ambient transcripts, zero server-side conversation
        # records until the wake phrase is detected locally.
        _idle_uplink = kws_detector is None or _idle_uplink_forced
        logger.info(
            "QWEN idle uplink: %s (kws=%s, forced=%s)",
            "on" if _idle_uplink else "off",
            kws_detector is not None,
            _idle_uplink_forced,
        )

        mic_stream = sd.InputStream(
            device="reachymini_audio_src",
            samplerate=16000,
            channels=1,
            dtype="int16",
            blocksize=960,
        )
        playback = PcmPlayback()
        gate = WakeGate()
        wake_greetings = WakeGreetingGate()
        transcript_window = RecentTranscriptWindow()
        mic_stream.start()
        playback.start()

        async def run_qwen() -> None:
            response_started = False
            pending_qwen_emotion: list[str | None] = [None]
            client: QwenRealtimeClient
            last_sonos_fragment = ""
            last_sonos_fragment_at = 0.0
            asr_preroll: deque[bytes] = deque(maxlen=34)
            asr_capture = bytearray()
            asr_capturing = False
            asr_capture_started_at = 0.0
            asr_debug_dir = Path("/tmp/yrobot-asr-debug")
            asr_debug_max_bytes = 16000 * 2 * 8  # 8 seconds of 16 kHz int16 mono

            # ── Face recognition ─────────────────────────────────────────
            # FaceDB is opened once per QWEN session; on-disk state
            # survives restarts. Recognition runs after every audio
            # chunk that has a fresh JPEG cached, and a speaker
            # change triggers a session.update re-emit so the model
            # can address the new person by name.
            face_db = FaceDB()
            speaker_identity = IdentityStabilizer(
                stable_after_s=_QWEN_FACE_SPEAKER_STABLE_S
            )

            async def _drain_face() -> None:
                jpeg = camera_streamer.latest()
                if jpeg is None:
                    return
                arr = np.frombuffer(jpeg, np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    return
                name, face_score = face_db.recognize_with_score(frame)
                name = name or ""
                RUNTIME_HEALTH.update(
                    face_recognition_name=name or None,
                    face_recognition_score=round(face_score, 3),
                    face_recognition_at=time.time(),
                )
                now = time.monotonic()
                confirmed_name = speaker_identity.observe(name, now=now)
                if confirmed_name is None:
                    return
                speaker_prompt = f"\n\n你正在跟 {confirmed_name} 说话。\n"
                try:
                    extra = (
                        f"{DEFAULT_INSTRUCTIONS}\n{speaker_prompt}"
                        f"{settings.effective_system_prompt}"
                    )
                    if settings.send_video:
                        extra = f"{extra}\n\n{VISION_POLICY}"
                    await client.resend_session_update_with(instructions_override=extra)
                    logger.info("QWEN local face confirmed: speaker=%r", confirmed_name)
                    if not response_started and wake_greetings.claim(confirmed_name):
                        asyncio.create_task(speak_exact_text(f"{confirmed_name}，你好！"))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("session.update on speaker change failed: %s", exc)

            async def monitor_face_identity() -> None:
                while not stop_event.is_set():
                    if gate.active:
                        await _drain_face()
                    await asyncio.sleep(1.0)

            async def _append_visual_snapshot(transcript: str) -> None:
                if not settings.send_video or not _qwen_wants_visual_snapshot(transcript):
                    return
                jpeg = camera_streamer.latest()
                if jpeg is None:
                    logger.info("QWEN visual snapshot skipped: no cached camera frame")
                    return
                try:
                    await client.append_image(base64.b64encode(jpeg).decode("ascii"))
                    logger.info("QWEN visual snapshot appended after transcript")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("QWEN visual snapshot append failed: %s", exc)

            def save_asr_debug_wav(transcript: str) -> None:
                nonlocal asr_capture, asr_capturing
                if not asr_capture:
                    asr_capturing = False
                    return
                try:
                    asr_debug_dir.mkdir(parents=True, exist_ok=True)
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    path = asr_debug_dir / f"asr-{stamp}.wav"
                    with wave.open(str(path), "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(16000)
                        wav.writeframes(bytes(asr_capture))
                    files = sorted(asr_debug_dir.glob("asr-*.wav"), key=lambda x: x.stat().st_mtime)
                    for old in files[:-8]:
                        old.unlink(missing_ok=True)
                    logger.info(
                        "QWEN ASR debug wav: path=%s bytes=%d transcript=%r",
                        path,
                        len(asr_capture),
                        transcript[:120],
                    )
                except Exception as exc:
                    logger.warning("could not save QWEN ASR debug wav: %s", exc)
                finally:
                    asr_capture = bytearray()
                    asr_capturing = False

            def on_audio(pcm: bytes) -> None:
                nonlocal response_started
                if not response_started:
                    response_started = True
                    choreo.set_mode(SPEAK)
                    choreo.release_still()
                    tracker.set_robot_speaking(True)  # echo guard
                    if pending_qwen_emotion[0] is not None:
                        play_qwen_requested_emotion(pending_qwen_emotion[0])
                        pending_qwen_emotion[0] = None
                    RUNTIME_HEALTH.update(tts_active=True)
                playback.put(pcm)
                RUNTIME_HEALTH.update(
                    audio_queue=playback.pending,
                    audio_dropped=playback.dropped,
                    last_tts_packet_at=time.time(),
                )

            async def speak_exact_text(text: str) -> bool:
                # Stock quotes and other numeric local-tool results must not be
                # re-generated by the main conversational model. Use an isolated
                # short realtime session only as TTS, so stale conversation
                # context cannot change prices/codes.
                import base64 as _base64
                import json as _json

                import websockets as _websockets

                exact = text.strip()
                if not exact:
                    return False
                try:
                    try:
                        await client._cancel_active_response()
                    except Exception:
                        pass
                    session_payload = {
                        "type": "session.update",
                        "session": {
                            "modalities": ["text", "audio"],
                            "voice": settings.qwen_voice,
                            "input_audio_format": "pcm",
                            "output_audio_format": "pcm",
                            "turn_detection": None,
                            "temperature": 0.0,
                            "top_p": 0.01,
                            "instructions": (
                                "你是一个文字转语音引擎。只朗读用户给出的原文，"
                                "不要回答问题，不要解释，不要补充，不要修改任何数字、代码或单位。"
                            ),
                        },
                    }
                    ws_url = _model_url(settings.qwen_url, settings.qwen_model)
                    async with _websockets.connect(
                        ws_url,
                        additional_headers={"Authorization": f"Bearer {settings.qwen_api_key}"},
                        max_size=10_000_000,
                    ) as ws:
                        await ws.send(_json.dumps(session_payload, ensure_ascii=False))
                        await ws.send(
                            _json.dumps(
                                {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "message",
                                        "role": "user",
                                        "content": [
                                            {
                                                "type": "input_text",
                                                "text": "朗读以下原文，不要改写："
                                                + chr(10)
                                                + exact,
                                            }
                                        ],
                                    },
                                },
                                ensure_ascii=False,
                            )
                        )
                        await ws.send(_json.dumps({"type": "response.create"}))
                        while True:
                            msg = _json.loads(await ws.recv())
                            event_type = msg.get("type")
                            if event_type == "response.audio.delta":
                                delta = msg.get("delta") or ""
                                if delta:
                                    on_audio(_base64.b64decode(delta))
                            elif event_type == "response.audio_transcript.done":
                                logger.info(
                                    "qwen exact tts transcript: %s",
                                    str(msg.get("transcript") or "")[:160],
                                )
                            elif event_type == "response.done":
                                on_response_done()
                                return True
                            elif event_type == "error":
                                error = msg.get("error") or {}
                                raise RuntimeError(error.get("message") or error or msg)
                except Exception as exc:
                    logger.warning("qwen exact tts failed: %s", exc)
                    on_response_done()
                    return False

            def on_interrupt() -> None:
                nonlocal response_started
                response_started = False
                playback.flush()
                choreo.set_mode(LISTEN)
                tracker.set_robot_speaking(False)
                RUNTIME_HEALTH.update(tts_active=False, audio_queue=0)

            def on_user_speech() -> None:
                nonlocal asr_capture, asr_capturing, asr_capture_started_at
                asr_capture = bytearray().join(asr_preroll)
                asr_capturing = True
                asr_capture_started_at = time.monotonic()
                gate.note_speech()
                choreo.set_mode(LISTEN)
                tracker.note_speech()  # pulse DoA window for head tracking

            def on_response_done() -> None:
                nonlocal response_started
                response_started = False
                choreo.set_mode(IDLE)
                tracker.set_robot_speaking(False)
                RUNTIME_HEALTH.update(tts_active=False)

            async def activate_from_wake() -> None:
                # Keep QWEN in manual turn mode after wake. In server VAD
                # mode QWEN may start speaking before the local deterministic
                # home-control parser has decided whether a command was
                # actually executed, which lets it say things like “已调低音量”
                # without any tool result.
                await client.set_turn_detection(None)
                await client.request_response()

            async def execute_local_spoken_control(candidates: list[str]) -> bool:
                # Walk the recent ASR candidates (most recent last). Pick
                # the first that matches a trigger word and try it. If a
                # tool fires, the result is honest (tool either succeeded
                # or returned ok=False) and we tell the model what really
                # happened. No match is silent: QWEN often streams ASR
                # fragments, and the next fragment may complete the command.
                photo_dispatch = await _qwen_dispatch_photo_command(
                    candidates, self._photo_library, client
                )
                if photo_dispatch is not None:
                    return photo_dispatch
                for candidate in candidates:
                    result = await asyncio.to_thread(
                        tool_executor.execute_spoken_control, candidate
                    )
                    if result is not None:
                        logger.info(
                            "qwen local spoken control: %s transcript=%r",
                            result,
                            candidate,
                        )
                        exact_result = _qwen_spoken_control_result_text(result)
                        if exact_result:
                            if await speak_exact_text(exact_result):
                                return True
                            await client.cancel_and_inject(
                                "[系统] 本地工具已经得到查询结果，但精确语音播报失败。"
                                "请只告诉用户：行情已查到，但语音播报失败，请查看日志或 Dashboard；"
                                "不要提供任何股票代码、价格、涨跌幅或其他数字。"
                            )
                            return True
                        await client.cancel_and_inject(_qwen_spoken_control_result_feedback(result))
                        return True
                feedback = _qwen_unmatched_spoken_control_feedback(candidates)
                if feedback:
                    await client.cancel_and_inject(feedback)
                    return True
                logger.info("qwen local spoken control: no match candidates=%r", candidates)
                return False


            async def execute_command_recognizer(wav_bytes: bytes) -> bool:
                command = await asyncio.to_thread(command_recognizer.recognize, wav_bytes)
                if not command:
                    return False
                logger.info("command recognizer matched: %s", command)
                return await execute_local_spoken_control([command])

            def on_input_transcript(transcript: str) -> None:
                nonlocal last_sonos_fragment, last_sonos_fragment_at
                wav_bytes = bytes(asr_capture) if gate.active and asr_capture else b""
                if gate.active:
                    # Chat-marker log only for in-session turns: ambient
                    # conversation must not appear in the dashboard panel.
                    logger.info("qwen stt: %s", transcript[:120])
                    save_asr_debug_wav(transcript)
                normalized = _qwen_normalize_for_control(transcript)
                now = time.monotonic()
                if _qwen_sonos_fragment(transcript):
                    last_sonos_fragment = transcript
                    last_sonos_fragment_at = now
                candidates = transcript_window.candidates(transcript)
                contextual_command = _qwen_contextual_sonos_step_command(
                    last_sonos_fragment, transcript
                )
                if contextual_command and now - last_sonos_fragment_at <= 15.0:
                    candidates = [*candidates, contextual_command]
                if gate.observe_transcript(transcript):
                    logger.info("QWEN wake word detected")
                    logger.info("qwen stt: %s", transcript[:120])
                    wake_greetings.begin_wake()
                    tracker.set_conversation_active(True)
                    asyncio.create_task(activate_from_wake())
                if gate.active:
                    pending_qwen_emotion[0] = requested_emotion(transcript)
                    dance_request = requested_dance(transcript)
                    if dance_request is not None:
                        action, dance = dance_request
                        if action == "stop":
                            choreo.stop_recorded()
                            logger.info("qwen dance stopped")
                        elif dance is not None and choreo.play_dance(dance):
                            logger.info("qwen dance -> %s", dance)
                if gate.active and candidates:

                    async def _run_spoken_control() -> None:
                        await _drain_face()
                        await _append_visual_snapshot(transcript)
                        matched = await execute_local_spoken_control(candidates)
                        command_matched = False
                        if not matched and wav_bytes:
                            command_matched = await execute_command_recognizer(wav_bytes)
                        if _qwen_is_pure_backchannel(transcript):
                            # Pure acknowledgement (好/行/嗯): no model
                            # response, just a small nod.
                            choreo.play_move("nod")
                        elif _qwen_is_bare_wake_utterance(transcript):
                            # The wake path (KWS hit or cloud transcript
                            # match) already activated the session, and its
                            # response.create is streaming the greeting. A
                            # second response.create for the bare wake-word
                            # echo makes DashScope close the session
                            # ("Conversation has none active response").
                            pass
                        elif _qwen_should_request_response_after_local_control(
                            matched, command_matched
                        ):
                            if _qwen_device_intent(transcript):
                                # The local deterministic parser found nothing:
                                # nothing was executed. Inject the truth so the
                                # model cannot fabricate a success reply, and
                                # point it at its own function tool for fuzzy
                                # device names (e.g. 省灯 -> 吸顶灯).
                                await client.cancel_and_inject(
                                    "[system] 用户的这句话没有匹配到任何本地设备"
                                    "指令，没有任何设备操作被执行过。禁止声称已"
                                    "执行。如果你想帮用户控制设备，请调用 "
                                    "control_allowed_device 工具（可以自行推断"
                                    "用户想说的设备名）；否则请如实说明没听清。"
                                )
                            else:
                                await client.request_response()

                    asyncio.create_task(_run_spoken_control())

            def on_output_transcript(transcript: str) -> None:
                logger.info("qwen response: %s", transcript[:160])

            def on_state(state: str) -> None:
                if state == "connected":
                    RUNTIME_HEALTH.update(ws_state=state, last_error=None)
                else:
                    RUNTIME_HEALTH.update(ws_state=state)

            def on_error(message: str) -> None:
                RUNTIME_HEALTH.update(last_error=message)

            last_qwen_requested_emotion: dict[str, float] = {}

            def play_qwen_requested_emotion(emotion: str) -> bool:
                """Play a bounded gesture only for an explicit user request."""
                if choreo.current_move() is not None or choreo.current_recorded() is not None:
                    logger.info("qwen requested emotion %s skipped: move in progress", emotion)
                    return False
                return _play_emotion_move(
                    choreo,
                    emotion,
                    last_qwen_requested_emotion,
                    _get_recorded,
                    prefer_recorded=True,
                    source="explicit_request",
                    tag="qwen",
                )

            tool_executor = ToolExecutor(
                settings,
                volume_controller=volume_controller_singleton(),
            )
            command_recognizer = CommandRecognizer(settings)
            client = QwenRealtimeClient(
                settings,
                tool_executor,
                on_audio=on_audio,
                on_input_transcript=on_input_transcript,
                on_output_transcript=on_output_transcript,
                on_interrupt=on_interrupt,
                on_user_speech=on_user_speech,
                on_response_done=on_response_done,
                on_state=on_state,
                on_error=on_error,
            )
            cloud_task = asyncio.create_task(client.run(stop_event))
            ready_task = asyncio.create_task(client.ready.wait())
            face_task: asyncio.Task[None] | None = None
            try:
                done, _ = await asyncio.wait(
                    {cloud_task, ready_task},
                    timeout=15.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cloud_task in done:
                    await cloud_task
                if ready_task not in done:
                    raise TimeoutError("QWEN session setup timed out")

                face_task = asyncio.create_task(monitor_face_identity())

                if _qwen_should_resume_wake_after_reconnect(gate):
                    logger.info("QWEN wake still active after reconnect; resuming")
                    await activate_from_wake()

                threshold = max(500.0, get_vad_rms_min() * 32768.0)
                manual_speaking = False
                silence_frames = 0
                turn_frames = 0
                while not stop_event.is_set():
                    if cloud_task.done():
                        await cloud_task
                    frame, _ = await asyncio.to_thread(mic_stream.read, 960)
                    pcm_int16 = frame.astype("<i2", copy=False)
                    pcm = pcm_int16.tobytes()
                    # Local KWS wake: drain the sherpa-onnx stream on the
                    # same mic chunk. On a hit, open the WakeGate window
                    # (observe_transcript matches the WAKE_WORDS list) so
                    # the audio uplink below starts forwarding to QWEN.
                    kws_chunks_fed = 0
                    if kws_detector is not None and not gate.active:
                        try:
                            kws_chunks_fed += 1
                            hit = kws_detector.feed(pcm_int16)
                            if hit:
                                gate.observe_transcript(hit)
                                wake_greetings.begin_wake()
                                RUNTIME_HEALTH.update(wake_active=True)
                                # Mirror the transcript-wake path exactly:
                                # head tracking and session activation (manual turn
                                # mode + response request) so conversation starts
                                # immediately, without a gesture clip.
                                tracker.set_conversation_active(True)
                                asyncio.create_task(activate_from_wake())
                                logger.info("KWS wake detected: %r", hit)
                            elif kws_chunks_fed % 1000 == 0:
                                # Heartbeat: proves the feed loop is alive
                                # even while the detector stays silent.
                                logger.info(
                                    "KWS alive: %d chunks fed (~%.0fs of audio)",
                                    kws_chunks_fed,
                                    kws_chunks_fed * 0.06,
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("kws wake feed failed: %s", exc)
                    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
                    rms = float(np.sqrt(np.mean(np.square(samples))))
                    _publish_dashboard_mic(rms / 32768.0)
                    if not audio_input_controller_singleton().enabled():
                        await asyncio.sleep(0)
                        continue

                    if gate.active:
                        asr_preroll.append(pcm)
                        if rms >= threshold:
                            if not manual_speaking:
                                manual_speaking = True
                                silence_frames = 0
                                turn_frames = 0
                                asr_capture = bytearray().join(asr_preroll)
                                asr_capturing = True
                                asr_capture_started_at = time.monotonic()
                                gate.note_speech()
                                choreo.set_mode(LISTEN)
                                tracker.note_speech()
                            silence_frames = 0
                        elif manual_speaking:
                            silence_frames += 1
                        else:
                            if gate.expire():
                                RUNTIME_HEALTH.update(wake_active=False)
                                await client.set_turn_detection(None)
                                playback.flush()
                                choreo.set_mode(IDLE)
                                tracker.set_conversation_active(False)
                                logger.info(
                                    "QWEN wake expired (%.0fs timeout)", gate.timeout
                                )
                            continue

                        if asr_capturing and len(asr_capture) < asr_debug_max_bytes:
                            asr_capture.extend(pcm)
                        await client.append_pcm(pcm)
                        turn_frames += 1
                        if (
                            silence_frames >= _QWEN_ACTIVE_SILENCE_FRAMES
                            or turn_frames >= _QWEN_MAX_TURN_FRAMES
                        ):
                            await client.commit_turn()
                            manual_speaking = False
                            silence_frames = 0
                            turn_frames = 0
                        if gate.expire():
                            await client.set_turn_detection(None)
                            playback.flush()
                            choreo.set_mode(IDLE)
                            tracker.set_conversation_active(False)
                            logger.info(
                                "QWEN wake expired (%.0fs timeout)", gate.timeout
                            )
                        continue

                    if not _idle_uplink:
                        # Local KWS owns wake: no audio leaves the robot
                        # until the session is active.
                        continue

                    if rms >= threshold:
                        manual_speaking = True
                        silence_frames = 0
                    elif manual_speaking:
                        silence_frames += 1
                    else:
                        continue

                    await client.append_pcm(pcm)
                    turn_frames += 1
                    if (
                        silence_frames >= _QWEN_PRE_WAKE_SILENCE_FRAMES
                        or turn_frames >= _QWEN_MAX_TURN_FRAMES
                    ):
                        await client.commit_turn()
                        manual_speaking = False
                        silence_frames = 0
                        turn_frames = 0
            finally:
                ready_task.cancel()
                cloud_task.cancel()
                if face_task is not None:
                    face_task.cancel()
                for task in (ready_task, cloud_task, face_task):
                    if task is None:
                        continue
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        try:
            while not stop_event.is_set():
                try:
                    asyncio.run(run_qwen())
                except Exception as exc:
                    if not _qwen_should_reconnect(exc):
                        raise
                    logger.warning("QWEN session closed, reconnecting: %s", exc)
                    RUNTIME_HEALTH.increment("reconnects")
                    RUNTIME_HEALTH.update(ws_state="reconnecting", last_error=str(exc))
                    playback.flush()
                    choreo.set_mode(IDLE)
                    stop_event.wait(2.0)
        finally:
            RUNTIME_HEALTH.update(ws_state="stopped", tts_active=False, audio_queue=0)
            playback.close()
            mic_stream.stop()
            mic_stream.close()
            tracker.stop()
            choreo.close()
            choreo.join(timeout=2.0)

    def _run_xiaozhi(
        self,
        reachy_mini: ReachyMini,
        stop_event: threading.Event,
        settings: Settings,
    ) -> None:
        """Xiaozhi — sounddevice mic + OutputStream TTS."""
        import asyncio as _a
        import json as _j
        import subprocess as _sp
        import time as _sleep

        import opuslib
        import sounddevice as _sd
        import websockets as _ws

        from yrobot.app_config import audio_input_controller_singleton
        from yrobot.kws import KeywordWakeDetector
        from yrobot.audio import _publish_dashboard_mic
        from yrobot.audio_runtime import (
            BoundedLatestQueue,
            TtsWatchdog,
            UplinkResponseWatchdog,
            xiaozhi_aplay_command,
        )
        from yrobot.motion import IDLE, LISTEN, SPEAK, Choreographer

        settings = Settings.from_env()
        camera_streamer = self._camera_streamer

        # ── Safe motor startup with slow Choreographer rise ───────
        # Snapshot the real pose before our 50 Hz writer starts. The first
        # Choreographer targets blend from this pose so startup never snaps
        # from sleep directly into the internal idle pose.
        startup_head_pose = None
        startup_antennas = None
        try:
            startup_head_pose = reachy_mini.get_current_head_pose()
            _, antenna_joints = reachy_mini.get_current_joint_positions()
            startup_antennas = (float(antenna_joints[0]), float(antenna_joints[1]))
            logger.info("captured startup pose for smooth motor handoff")
        except Exception as exc:
            logger.warning("could not capture startup pose before motor enable: %s", exc)

        # Avoid goto_target (defaults to a short snap). Enable the motors
        # before starting the pose writer; a failed enable is not recoverable
        # by repeatedly sending set_target commands.
        motor_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                reachy_mini.enable_motors()
                logger.info("motors enabled (attempt %d)", attempt)
                motor_error = None
                break
            except Exception as exc:
                motor_error = exc
                logger.warning("motor enable failed (attempt %d/3): %s", attempt, exc)
                if attempt < 3:
                    stop_event.wait(1.0)
        if motor_error is not None:
            RUNTIME_HEALTH.update(motor_ready=False)
            raise RuntimeError("Reachy motors could not be enabled") from motor_error
        RUNTIME_HEALTH.update(motor_ready=True)

        choreo = Choreographer(
            reachy_mini,
            startup_head_pose=startup_head_pose,
            startup_antennas=startup_antennas,
            startup_blend_duration=4.0,
        )
        # Keep the low-speed parameters active during the complete startup
        # rise. Restore normal tracking only after the worker has run for 8s.
        choreo._gaze._max_vel = 0.3
        choreo._gaze._omega = 2.0
        from yrobot.app_config import motion_controller_singleton

        motion_controller_singleton().set(choreo)
        motion_controller_singleton().set_recorded_provider(_get_recorded)
        choreo.start()
        _start_motion_connection_watchdog(choreo, stop_event)
        # Wait for the slow initial rise unless shutdown was requested.
        if not stop_event.wait(timeout=8.0):
            choreo._gaze._max_vel = 2.5
            choreo._gaze._omega = 6.0
            logger.info("head rise complete, gaze speed restored")

        # ── SpeakerTracker: audio DoA + visual face (shared with QWEN) ───
        from yrobot.tracking import SpeakerTracker

        tracker = SpeakerTracker(
            reachy_mini,
            choreo,
            head_tracking_weight=settings.head_tracking_weight,
            camera_streamer=camera_streamer,
        )
        tracker.start()
        # Backward-compat alias so the existing _user_speaking[0] = True/False
        # assignments in the XIAOZHI VAD path below still update the same list
        # that SoundCompass.user_active reads.
        _user_speaking = tracker._user_speaking
        _last_emotion_move: dict[str, float] = {}

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

        _sleep.sleep(1.0)

        # Explicit stream params matter: the device-default open produced
        # float32 frames that were fed to opus as raw bytes (garbled audio
        # the cloud ASR could only half understand) and starved KWS.
        mic_stream = _sd.InputStream(
            device="reachymini_audio_src",
            samplerate=16000,
            channels=1,
            dtype="float32",
        )
        mic_stream.start()

        # Dedicated mic reader thread: the event loop's to_thread scheduling
        # jittered past the 60ms frame budget (~75% frames dropped — speech
        # came out as 2-4 garbled syllables and KWS never matched). A
        # blocking read on its own thread plus a queue decouples consumers.
        import queue as _q

        mic_q: "_q.Queue[np.ndarray]" = _q.Queue(maxsize=512)
        _mic_reader_stop = threading.Event()

        def _mic_reader():
            while not _mic_reader_stop.is_set():
                try:
                    raw, _ = mic_stream.read(960)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("mic reader error: %s", exc)
                    _mic_reader_stop.wait(0.1)
                    continue
                src = raw[:, 0] if getattr(raw, "ndim", 1) > 1 else raw
                pcm = (src * 32767.0).astype(np.int16)
                try:
                    mic_q.put_nowait(pcm)
                except _q.Full:
                    try:
                        mic_q.get_nowait()  # drop oldest
                        mic_q.put_nowait(pcm)
                    except _q.Empty:
                        pass

        _mic_thread = threading.Thread(target=_mic_reader, daemon=True, name="xz-mic-reader")
        _mic_thread.start()

        # Playback is intentionally isolated from the WebSocket event loop.
        # aplay writes can block on ALSA; the receive coroutine must never wait
        # on that pipe or it will stall incoming TTS packets.
        import queue as _pq
        import threading as _th

        _audio_q = BoundedLatestQueue[bytes | None](maxsize=50)
        _writer_stop = _th.Event()
        _audio_proc_lock = _th.Lock()
        _audio_proc = [None]
        _audio_stats = {"enqueued": 0, "written": 0, "dropped": 0, "restarts": 0}

        def _open_aplay():
            return _sp.Popen(
                list(xiaozhi_aplay_command()),
                stdin=_sp.PIPE,
                stderr=_sp.DEVNULL,
            )

        def _audio_writer():
            # The official daemon occasionally re-claims the speaker
            # (pcmC0D0p, mmap) after startup — every aplay then dies on
            # Broken-pipe within milliseconds. Three deaths inside 3 s
            # means the card is gone again: ask the SDK to stop_playing()
            # (same release the startup path does) before respawning.
            _aplay_deaths: list[float] = []
            proc = None
            try:
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
                                _now = time.monotonic()
                                _aplay_deaths = [
                                    t for t in _aplay_deaths if _now - t < 3.0
                                ] + [_now]
                                if len(_aplay_deaths) >= 3:
                                    _aplay_deaths.clear()
                                    # media.stop_playing() only stops the
                                    # SDK client stream — it does NOT free
                                    # pcmC0D0p when the daemon itself holds
                                    # it. The daemon's /api/media/stop_sound
                                    # does (validated 2026-08-20).
                                    try:
                                        reachy_mini.media.stop_playing()
                                    except Exception:  # noqa: BLE001
                                        pass
                                    try:
                                        import urllib.request as _urq

                                        _urq.urlopen(
                                            _urq.Request(
                                                "http://127.0.0.1:8000/api/media/stop_sound",
                                                method="POST",
                                            ),
                                            timeout=3.0,
                                        ).read()
                                        logger.warning(
                                            "audio-out: daemon re-claimed the speaker; stop_sound released it"
                                        )
                                    except Exception:  # noqa: BLE001
                                        logger.warning(
                                            "audio-out: stop_sound failed", exc_info=True
                                        )
                                proc = _open_aplay()
                                with _audio_proc_lock:
                                    _audio_proc[0] = proc
                                _audio_stats["restarts"] += 1
                                logger.info(
                                    "audio-out: started aplay (%d)", _audio_stats["restarts"]
                                )
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
                            with _audio_proc_lock:
                                _audio_proc[0] = None
                            proc = None
                    else:
                        _audio_stats["dropped"] += 1
                        logger.error("audio-out: dropped chunk after aplay restart failure")
            finally:
                if proc is not None:
                    try:
                        if proc.stdin is not None:
                            proc.stdin.close()
                    except OSError:
                        pass
                    try:
                        proc.terminate()
                        proc.wait(timeout=1.0)
                    except (OSError, _sp.TimeoutExpired):
                        try:
                            proc.kill()
                        except OSError:
                            pass
                with _audio_proc_lock:
                    _audio_proc[0] = None

        _audio_thread = _th.Thread(target=_audio_writer, name="aplay-writer", daemon=True)
        _audio_thread.start()

        def _aplay_add(stereo_f32):
            pcm = (np.clip(stereo_f32, -1, 1) * 32767).astype("<i2").tobytes()
            before = _audio_q.dropped
            _audio_q.put_latest(pcm)
            _audio_stats["enqueued"] += 1
            _audio_stats["dropped"] += _audio_q.dropped - before
            RUNTIME_HEALTH.update(
                audio_queue=_audio_q.qsize(),
                audio_dropped=_audio_stats["dropped"],
            )

        def _aplay_flush():
            # Drain stale audio from queue to prevent backlog.
            _audio_stats["dropped"] += _audio_q.flush()
            RUNTIME_HEALTH.update(
                audio_queue=_audio_q.qsize(),
                audio_dropped=_audio_stats["dropped"],
            )

        def _stop_audio_process() -> None:
            """Interrupt a blocked aplay write during shutdown."""
            with _audio_proc_lock:
                proc = _audio_proc[0]
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except (_sp.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

        # Local keyword wake detector (sherpa-onnx KWS), sharing the same
        # settings.wake_enabled switch as the QWEN path. A hit runs the
        # exact same actions as the cloud-transcript wake match (_waked
        # gate + nod), so wake no longer depends on tenclass streaming
        # ambient stt (which has intermittent gaps).
        _xz_kws: KeywordWakeDetector | None = None
        if settings.wake_enabled:
            try:
                _xz_kws = KeywordWakeDetector(model_dir=Path(settings.kws_model_dir))
                logger.info("XZ KWS wake armed: phrase=%r", settings.wake_phrase)
            except Exception as exc:  # noqa: BLE001
                logger.warning("XZ KWS init failed (falling back to cloud stt match): %s", exc)

        _xz_vol_ctx = {"last_at": 0.0}

        def _xz_local_volume_control(text: str) -> dict | None:
            # v1 protocol: the cloud streams ambient stt and YRobot matches
            # the wake word locally. The cloud LLM may answer “好的” without
            # any tool actually existing cloud-side, so the robot volume
            # executes deterministically on-device. Mirrors qwen_tools
            # vocabulary plus conversational continuation: within
            # VOLUME_CONTEXT_WINDOW_S after a robot-volume command, bare
            # direction words (再小一点/更大一点/搞小) keep adjusting.
            from yrobot.qwen_tools import (
                ROBOT_VOLUME_TARGET_PHRASES,
                TV_VOLUME_TARGET_PHRASES,
                SONOS_VOLUME_TARGET_PHRASES,
                VOLUME_STEP_PERCENT,
            )

            norm = text.lower().replace(" ", "")
            if any(phrase in norm for phrase in TV_VOLUME_TARGET_PHRASES):
                return None
            if any(phrase in norm for phrase in SONOS_VOLUME_TARGET_PHRASES):
                return None

            has_target = any(phrase in norm for phrase in ROBOT_VOLUME_TARGET_PHRASES)
            now = time.time()
            in_context = now - _xz_vol_ctx["last_at"] <= 15.0
            if not has_target and not in_context:
                return None

            # Explicit percentage: 音量调到50 / 调到百分之三十 (ASR emits digits)
            m = re.search(r"(?:调到|设到|设置为?|设为)百?分?之?([0-9]{1,3})", norm)
            vc = volume_controller_singleton()
            if m:
                target = max(0, min(100, int(m.group(1))))
                applied = int(vc.write_percent(target))
                _xz_vol_ctx["last_at"] = now
                return {"action": "volume_set", "volume_percent": applied}
            # Mute: 静音 / 关掉声音 / 别说话
            if "静音" in norm or "关闭声音" in norm or "关掉声音" in norm:
                applied = int(vc.write_percent(0))
                _xz_vol_ctx["last_at"] = now
                return {"action": "volume_mute", "volume_percent": applied}

            # Direction words (base + colloquial continuations)
            up_words = ("调大", "大一点", "大点", "加大", "加点", "提高", "高一点",
                        "再大", "更大", "更强", "响一点", "大声点", "搞大", "弄大")
            down_words = ("调小", "小一点", "小点", "减小", "降低", "低一点",
                          "再小", "更小", "小声点", "轻一点", "搞小", "弄小")
            if any(w in norm for w in up_words):
                action, delta = "volume_up", VOLUME_STEP_PERCENT
            elif any(w in norm for w in down_words):
                action, delta = "volume_down", -VOLUME_STEP_PERCENT
            else:
                return None
            current = int(vc.read_percent())
            applied = int(vc.write_percent(current + delta))
            _xz_vol_ctx["last_at"] = now
            return {"action": action, "volume_percent": applied}

        # ── Session channel: MQTT+UDP (protocol v3) or legacy WS ─────────
        # The tenclass cloud stopped accepting home-broadband WebSocket
        # handshakes on 2026-08-19 (HTTP 426 for every request shape), so
        # MQTT is the default transport. Both adapters expose the same
        # recv/send shape: inbound dicts (MQTT JSON) and bytes (UDP opus)
        # share one asyncio.Queue — the exact type-split the legacy WS
        # recv() loop already handled, so the session logic below stays
        # transport-agnostic.
        _ota_cache = OtaCredentialCache()
        _client_uuid = load_or_create_client_uuid()

        class _MqttChannel:
            def __init__(self, transport, udp, sid, tts_rate, tts_frame_ms):
                self._tp = transport
                self._udp = udp
                self.session_id = sid
                self.tts_rate = tts_rate
                self.tts_frame_ms = tts_frame_ms
                self._ts = 0

            async def recv(self):
                while not stop_event.is_set():
                    try:
                        return await _a.wait_for(self._tp.queue.get(), timeout=15.0)
                    except TimeoutError:
                        if not self._tp.connected:
                            raise _XiaozhiReconnect("mqtt broker disconnected") from None
                        continue
                raise _XiaozhiReconnect("stopping")

            async def send_json(self, obj):
                if not self._tp.publish_json(obj):
                    raise _XiaozhiReconnect("mqtt publish failed")

            async def send_audio(self, opus):
                self._ts += 960  # 16 kHz mono, 60 ms frames
                await _a.to_thread(self._udp.send, opus, timestamp=self._ts)

            async def close(self):
                try:
                    self._tp.publish_json(
                        {"session_id": self.session_id, "type": "goodbye"}
                    )
                except Exception:  # noqa: BLE001 — best-effort farewell
                    pass
                self._udp.close()
                self._tp.close()

        class _WsChannel:
            def __init__(self, ws, sid, tts_rate, tts_frame_ms):
                self._ws = ws
                self.session_id = sid
                self.tts_rate = tts_rate
                self.tts_frame_ms = tts_frame_ms

            async def recv(self):
                return await self._ws.recv()

            async def send_json(self, obj):
                await self._ws.send(_j.dumps(obj, ensure_ascii=False))

            async def send_audio(self, opus):
                await self._ws.send(opus)

            async def close(self):
                await self._ws.close()

        async def _open_ws_channel():
            hdrs = {
                "Authorization": f"Bearer {XIAOZHI_TOKEN}",
                "Device-Id": XIAOZHI_DEVICE_ID,
                "Protocol-Version": "1",
            }
            ws = await _ws.connect(
                XIAOZHI_CONV_URL,
                additional_headers=hdrs,
                open_timeout=12,
                ping_interval=20,
                ping_timeout=10,
            )
            await ws.send(
                _j.dumps(
                    {
                        "type": "hello",
                        "version": 1,
                        "transport": "websocket",
                        "audio_params": {
                            "format": "opus",
                            "sample_rate": 16000,
                            "channels": 1,
                            "frame_duration": 60,
                        },
                    }
                )
            )
            data = _j.loads(await _a.wait_for(ws.recv(), timeout=10))
            params = data.get("audio_params", {})
            return _WsChannel(
                ws,
                data.get("session_id", ""),
                int(params.get("sample_rate", 24000)),
                int(params.get("frame_duration", 60)),
            )

        async def _open_mqtt_channel():
            loop = _a.get_running_loop()
            inq: _a.Queue = _a.Queue()
            cfg = _ota_cache.load()
            transport: XiaozhiMqttTransport | None = None
            last_exc: Exception | None = None
            for _attempt in range(2):
                if cfg is None:
                    cfg = await _a.to_thread(
                        fetch_mqtt_config, XIAOZHI_DEVICE_ID, _client_uuid
                    )
                    _ota_cache.save(cfg)
                transport = XiaozhiMqttTransport(cfg, loop=loop, queue=inq)
                try:
                    await _a.to_thread(transport.start)
                    if not await _a.to_thread(transport.wait_connected, 10.0):
                        raise OSError("mqtt connect timeout")
                    break
                except Exception as exc:  # noqa: BLE001 — retry once with fresh creds
                    last_exc = exc
                    logger.warning(
                        "xz mqtt connect failed (attempt %d): %s", _attempt + 1, exc
                    )
                    transport.close()
                    transport = None
                    _ota_cache.path.unlink(missing_ok=True)
                    cfg = None
            if transport is None:
                raise OTAError(f"mqtt channel unavailable: {last_exc}")
            if not transport.publish_text(hello_request()):
                transport.close()
                raise OTAError("mqtt hello publish failed")
            # The broker may push mcp chatter before the hello reply; park
            # anything else and replay it once the session is up.
            deferred = []
            hello = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    msg = await _a.wait_for(
                        inq.get(), timeout=max(0.1, deadline - time.monotonic())
                    )
                except TimeoutError:
                    break
                if isinstance(msg, dict) and msg.get("type") == "hello":
                    hello = msg
                    break
                deferred.append(msg)
            if hello is None:
                transport.close()
                raise OTAError("mqtt server hello timeout")
            info = UdpChannelInfo.from_hello(hello)
            if info is None:
                transport.close()
                raise OTAError("mqtt server hello lacks udp section")
            sid = str(hello.get("session_id", ""))
            ap = hello.get("audio_params") or {}
            tts_rate = int(ap.get("sample_rate", 24000))
            tts_ms = int(ap.get("frame_duration", 60))
            udp = XiaozhiUdpAudio(
                info,
                on_packet=lambda opus, ts: loop.call_soon_threadsafe(
                    inq.put_nowait, opus
                ),
            )
            udp.start()
            channel = _MqttChannel(transport, udp, sid, tts_rate, tts_ms)
            for m in deferred:
                inq.put_nowait(m)
            logger.info(
                "xz mqtt ready sid=%s audio=%dHz/%dms udp=%s:%d",
                sid[:12],
                tts_rate,
                tts_ms,
                info.server,
                info.port,
            )
            return channel

        async def _open_xz_channel():
            if settings.xz_transport == "ws":
                return await _open_ws_channel()
            try:
                return await _open_mqtt_channel()
            except OTAError as exc:
                logger.warning("xz mqtt unavailable (%s); falling back to ws", exc)
                return await _open_ws_channel()

        async def run():
            enc = opuslib.Encoder(16000, 1, "voip")
            # Device-side MCP server: fixed whitelist (volume tools only,
            # immutable decision #9 — no generic MCP discovery).
            _vc = volume_controller_singleton()
            _xz_mcp = XiaozhiMcpServer(
                volume_read=_vc.read_percent,
                volume_write=_vc.write_percent,
            )
            RUNTIME_HEALTH.update(ws_state="connecting")
            chan = await _open_xz_channel()
            try:
                sid = chan.session_id
                tts_rate = chan.tts_rate
                tts_duration = chan.tts_frame_ms
                tts_frame_size = tts_rate * tts_duration // 1000
                dec = opuslib.Decoder(tts_rate, 1)
                tts_packets = 0
                tts_decode_errors = 0
                _tts_start_at = 0.0
                logger.info(
                    "xiaozhi ready sid=%s audio=%dHz/%dms", sid[:12], tts_rate, tts_duration
                )
                RUNTIME_HEALTH.update(
                    ws_state="connected",
                    session_id=sid,
                    tts_active=False,
                    tts_packets=0,
                    last_rx_at=time.time(),
                )
                tts_active = False
                tts_watchdog = TtsWatchdog()
                # ── Wake word state ──────────────────────────────────
                # A short transport reconnect must not make a user repeat the
                # wake word mid-conversation. A local-photo quarantine still
                # wins: it defers this restore until residual audio is gone.
                _waked = (
                    not self._local_photo_cloud_quarantine.active()
                    and self._xiaozhi_wake_lease.active()
                )
                _wake_deadline = (
                    time.time() + self._xiaozhi_wake_lease.remaining_s()
                    if _waked
                    else 0.0
                )
                if _waked:
                    logger.info("xz conversation restored after transport reconnect")
                # Session liveness (monotonic): every uplink burst must earn
                # at least one inbound message (the cloud answers each
                # burst with stt within ~3 s). The tenclass broker can
                # silently drop a session server-side while the MQTT TCP
                # link stays alive — detect and rebuild (2026-08-20).
                uplink_response_watchdog = UplinkResponseWatchdog(timeout_s=12.0)
                idle_show_last_activity = [time.monotonic()]
                idle_show_last_fire = [time.monotonic()]
                WAKE_TIMEOUT = _GATE_WAKE_TIMEOUT  # env YROBOT_WAKE_TIMEOUT_S

                async def recv():
                    nonlocal tts_active, _tts_start_at, tts_packets, tts_decode_errors
                    nonlocal _waked, _wake_deadline
                    while not stop_event.is_set():
                        try:
                            raw = await chan.recv()
                        except TimeoutError:
                            continue
                        uplink_response_watchdog.inbound_received(
                            is_conversation_response=is_xiaozhi_conversation_response(raw)
                        )
                        if isinstance(raw, bytes):
                            if not _waked:
                                continue
                            tts_packets += 1
                            tts_watchdog.packet()
                            RUNTIME_HEALTH.update(
                                last_rx_at=time.time(),
                                tts_packets=tts_packets,
                                last_tts_packet_at=time.time() if tts_watchdog.active else None,
                            )
                            if not hasattr(_aplay_add, "_count"):
                                _aplay_add._count = 0
                            _aplay_add._count += 1
                            try:
                                pcm = dec.decode(raw, tts_frame_size)
                                pcm_f32 = (
                                    np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
                                )
                                ratio = tts_rate / 16000
                                idx = np.arange(0, len(pcm_f32), ratio).astype(int)
                                pcm_16k = pcm_f32[idx[: min(len(idx), len(pcm_f32))]]
                                stereo = np.column_stack([pcm_16k, pcm_16k])
                                _aplay_add(stereo)
                                if tts_packets % 10 == 0:
                                    logger.info(
                                        "xz audio packets=%d latest=%dB", tts_packets, len(raw)
                                    )
                            except Exception as exc:
                                tts_decode_errors += 1
                                logger.warning(
                                    "xz opus decode failed packet=%d bytes=%d error=%s",
                                    tts_packets,
                                    len(raw),
                                    exc,
                                )
                        else:
                            RUNTIME_HEALTH.update(last_rx_at=time.time())
                            d = raw if isinstance(raw, dict) else _j.loads(raw)
                            t = d.get("type", "")
                            if t == "mcp":
                                # Xiaozhi MCP (the channel control_smart_home
                                # uses). Handle in a worker thread and reply
                                # with the matching JSON-RPC id.
                                mcp_payload = d.get("payload") or {}
                                mcp_reply = await _a.to_thread(
                                    _xz_mcp.handle_payload, mcp_payload
                                )
                                if mcp_reply is not None:
                                    await chan.send_json(
                                        {
                                            "session_id": sid,
                                            "type": "mcp",
                                            "payload": mcp_reply,
                                        }
                                    )
                                    logger.info("xz mcp -> %.200s", mcp_reply)
                                else:
                                    logger.info("xz mcp notification ignored")
                            if t == "llm":
                                # Gateway emotion metadata is conversational analysis,
                                # not a motion command. Keep it observable without
                                # letting a model-driven label interrupt dialogue.
                                emo = (d.get("emotion") or "").strip().lower()
                                logger.info("xz emotion %s ignored (autonomous gestures disabled)", emo)
                            if t == "stt":
                                text = d.get("text", "")
                                # Wake word gate (skip if force-wake flag set).
                                _force = False
                                try:
                                    _force = open("/tmp/yrobot_force_wake").read().strip() == "1"
                                except Exception:
                                    pass
                                if _force or _wake_match(text):
                                    # Chat-marker log only for turns that are
                                    # part of the conversation: the wake call
                                    # itself and everything after it.
                                    logger.info("xz stt: %s", text)
                                    _waked = True
                                    _wake_deadline = time.time() + WAKE_TIMEOUT
                                    self._xiaozhi_wake_lease.activate()
                                    logger.info("wake word detected: %.60s", text)
                                    idle_show_last_activity[0] = time.monotonic()
                                if not _waked:
                                    # No chat marker here: ambient conversation
                                    # must not appear in the dashboard chat panel.
                                    logger.info("xz ambient stt ignored: %.60s", text)
                                    continue
                                if not (_force or _wake_match(text)):
                                    logger.info("xz stt: %s", text)
                                if _waked:
                                    vol = _xz_local_volume_control(text)
                                    if vol is not None:
                                        logger.info(
                                            "xz local volume control: %s transcript=%r",
                                            vol,
                                            text,
                                        )
                                if _waked and self._photo_voice_controller.observe(text):
                                    intent_notified = await start_local_photo_flow(
                                        chan,
                                        suppress_cloud_uplink=self._local_photo_cloud_quarantine.arm,
                                        notify_intent=self._photo_intent_notifier.notify,
                                        start_capture=lambda: self._schedule_xiaozhi_photo(text),
                                    )
                                    if intent_notified:
                                        logger.info(
                                            "xz local photo intent notified; aborting cloud turn"
                                        )
                                    else:
                                        logger.warning(
                                            "xz local photo intent unavailable; aborting cloud turn"
                                        )
                                    # Fixed local audio clips provide both prompts; the
                                    # closed cloud turn cannot add commentary or ask a
                                    # follow-up question.
                                    raise _XiaozhiReconnect(
                                        "local photo command handled on device"
                                    )
                                choreo.set_mode(LISTEN)
                            elif t == "tts" and d.get("state") == "start":
                                if not _waked:
                                    continue
                                # The server's reply to the wake phrase (its
                                # greeting / 我在 acknowledgement) must PLAY:
                                # silence after a successful wake reads as a
                                # dead mic. Ambient-conversation protection is
                                # the not-waked gate above.
                                logger.info("xz tts start")
                                tts_active = True
                                tracker.set_robot_speaking(True)  # echo guard
                                idle_show_last_activity[0] = time.monotonic()
                                _tts_start_at = time.time()
                                tts_watchdog.start()
                                RUNTIME_HEALTH.update(
                                    tts_active=True,
                                    tts_packets=0,
                                    last_tts_packet_at=None,
                                )
                                _user_speaking[0] = False
                                tts_packets = 0
                                tts_decode_errors = 0
                                _aplay_add._count = 0
                                choreo.set_mode(SPEAK)
                                choreo.release_still()
                            elif t == "tts" and d.get("state") == "sentence_start":
                                sentence_text = d.get("text", "")
                                # Keep the transcript for the conversation panel, but
                                # never classify reply semantics into body language.
                                logger.info("xz tts text: %s", sentence_text[:80])
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
                                tts_watchdog.stop()
                                RUNTIME_HEALTH.update(tts_active=False, tts_packets=tts_packets)
                                logger.info(
                                    "xz tts stop packets=%d audio(enqueued=%d written=%d pending=%d)",
                                    getattr(_aplay_add, "_count", 0),
                                    _audio_stats["enqueued"],
                                    _audio_stats["written"],
                                    _audio_q.qsize(),
                                )
                                logger.info(
                                    "xz audio summary packets=%d decode_errors=%d",
                                    tts_packets,
                                    tts_decode_errors,
                                )
                                _aplay_flush()
                                choreo.set_mode(IDLE)
                                tracker.set_robot_speaking(False)
                                idle_show_last_activity[0] = time.monotonic()

                rt = _a.ensure_future(recv())
                try:
                    # Silence floor for the Xiaozhi uplink gate.  Higher =
                    # louder speech required to trigger; normalized 0..1 value
                    # from the shared config (default 2000/32768 ≈ 0.061).
                    from yrobot.audio import get_vad_rms_min as _get_vad_min

                    SILENCE_RMS = max(500, int(_get_vad_min() * 32768))
                    # Energy-hangover VAD (2026-08-19): replaces the inline
                    # silence_run counter and fixes the extend-loop bug that
                    # re-sent the LAST collect frame (f16) instead of the
                    # freshly read buf, so the cloud received one repeated
                    # frame for the whole utterance tail and could not tell
                    # what the user said. Speech now streams until sustained
                    # silence (default 1.5 s) or the 12 s cap; a preroll
                    # buffer keeps the first syllables of quiet onsets.
                    uplink_vad = EnergyHangoverVAD(
                        stop_rms=1000.0,
                        hangover_s=settings.utterance_hangover_s,
                        max_utterance_s=settings.utterance_max_s,
                    )
                    uplink_preroll = PrerollBuffer(seconds=0.5)
                    logger.info(
                        "xz silence floor rms=%.0f hangover=%.1fs max=%.0fs",
                        SILENCE_RMS,
                        settings.utterance_hangover_s,
                        settings.utterance_max_s,
                    )
                    _photo_quarantine_logged = False
                    while not stop_event.is_set():
                        if rt.done():
                            if rt.cancelled():
                                raise RuntimeError("xiaozhi receive task was cancelled")
                            recv_error = rt.exception()
                            if recv_error is not None:
                                if isinstance(recv_error, _XiaozhiReconnect):
                                    raise recv_error
                                if _is_expected_xiaozhi_disconnect(recv_error):
                                    raise _XiaozhiReconnect(str(recv_error)) from recv_error
                                raise RuntimeError("xiaozhi receive task failed") from recv_error
                            raise RuntimeError("xiaozhi receive task ended unexpectedly")
                        if self._local_photo_cloud_quarantine.active():
                            dropped = 0
                            while dropped < 64:
                                try:
                                    mic_q.get_nowait()
                                except _q.Empty:
                                    break
                                dropped += 1
                            if not _photo_quarantine_logged:
                                logger.info(
                                    "xz local-photo cloud uplink suppressed remaining=%.1fs",
                                    self._local_photo_cloud_quarantine.remaining_s(),
                                )
                                _photo_quarantine_logged = True
                            await _a.sleep(0.02)
                            continue
                        if self._local_photo_cloud_quarantine.take_conversation_resume():
                            _waked = True
                            _wake_deadline = time.time() + WAKE_TIMEOUT
                            self._xiaozhi_wake_lease.activate()
                            logger.info(
                                "xz local-photo conversation resumed after uplink quarantine"
                            )
                        # Dead-session watchdog: an uplink burst went out
                        # >12 s ago and nothing has arrived since — the
                        # session is a zombie (server dropped it silently).
                        if uplink_response_watchdog.expired():
                            raise _XiaozhiReconnect(
                                "no server response 12s after uplink burst"
                            )
                        # Auto-expire wake after conversation timeout; the
                        # robot speaking keeps the session alive (deadline
                        # refreshed while TTS plays so long replies are never
                        # cut off mid-sentence).
                        if _waked and tts_active:
                            _wake_deadline = time.time() + WAKE_TIMEOUT
                            self._xiaozhi_wake_lease.activate()
                        if _waked and _wake_deadline > 0 and time.time() > _wake_deadline:
                            _waked = False
                            self._xiaozhi_wake_lease.clear()
                            logger.info("wake expired (%.0fs timeout)", WAKE_TIMEOUT)
                        # Publish conversation state to the tracker: while a
                        # conversation is active, a locked face may steer the
                        # head directly (official-app face-anchor parity).
                        tracker.set_conversation_active(_waked)
                        # Idle self-performance (official app pattern): after a
                        # long quiet stretch, occasionally play a small recorded
                        # emotion or dance while nobody is interacting.
                        _idle_now = time.monotonic()
                        if (
                            _IDLE_SHOW_ENABLED
                            and not _waked
                            and not tts_active
                            and _idle_now - idle_show_last_activity[0] >= _IDLE_SHOW_INTERVAL_S
                            and _idle_now - idle_show_last_fire[0] >= _IDLE_SHOW_INTERVAL_S
                        ):
                            idle_show_last_fire[0] = _idle_now
                            _idle_what = _play_idle_show(choreo, _get_recorded)
                            if _idle_what:
                                logger.info("idle show: %s", _idle_what)
                        if tts_active:
                            # Recover both startup stalls and mid-stream
                            # disconnects so one missing tts.stop cannot make
                            # the robot deaf forever.
                            if tts_watchdog.stalled():
                                tts_active = False
                                tts_watchdog.stop()
                                logger.warning(
                                    "tts stall: packets=%d age=%.1fs, forcing idle",
                                    tts_watchdog.packets,
                                    time.monotonic()
                                    - max(
                                        tts_watchdog.last_packet_at or tts_watchdog.started_at,
                                        0.0,
                                    ),
                                )
                                _aplay_flush()
                                choreo.set_mode(IDLE)
                            try:
                                mic_q.get_nowait()
                            except _q.Empty:
                                pass
                            await _a.sleep(0)
                            continue
                        if _xiaozhi_should_pause_for_mic(
                            audio_input_controller_singleton().enabled()
                        ):
                            raise _XiaozhiPaused("mic input disabled")
                        frames = []
                        rms_max = 0
                        for _ in range(16):
                            pcm16 = await _a.to_thread(mic_q.get, True, 5.0)
                            rms = float(
                                np.sqrt(
                                    np.mean(
                                        np.square(
                                            pcm16.astype(np.float64)
                                        )
                                    )
                                )
                            )
                            _publish_dashboard_mic(float(rms) / 32768.0)
                            if rms > rms_max:
                                rms_max = rms
                            frames.append(pcm16)
                            # Feed every mic frame to the local KWS while
                            # idle; on a hit mirror the transcript-wake
                            # state transition (_waked + deadline), without a nod.
                            if _xz_kws is not None and not _waked:
                                try:
                                    _hit = _xz_kws.feed(pcm16)
                                    if _hit:
                                        _waked = True
                                        _wake_deadline = time.time() + WAKE_TIMEOUT
                                        self._xiaozhi_wake_lease.activate()
                                        logger.info(
                                            "wake word detected (local KWS): %s", _hit
                                        )
                                        idle_show_last_activity[0] = time.monotonic()
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning("xz kws feed error: %s", exc)
                        if rms_max < SILENCE_RMS:
                            # Keep the window's tail for the next gate pass so
                            # a quiet onset does not lose its first syllables
                            # (frames that failed the gate are gone otherwise).
                            uplink_preroll.extend(frames)
                            continue
                        if self._local_photo_cloud_quarantine.active():
                            # The receive task may have recognised a local photo
                            # command while this VAD collection window was open.
                            # Never replay that residual window after reconnect.
                            continue
                        # Refresh wake deadline on every speech burst.
                        if _waked:
                            _wake_deadline = time.time() + WAKE_TIMEOUT
                            self._xiaozhi_wake_lease.activate()
                        _user_speaking[0] = True
                        await chan.send_json(
                            {
                                "session_id": sid,
                                "type": "listen",
                                "state": "start",
                                "mode": "manual",
                            }
                        )
                        logger.info("xz listen start (rms_max=%.0f)", rms_max)
                        # Prepend the fresh preroll tail (≤0.5 s before the
                        # onset) ahead of the gate window's own frames, then
                        # let the hangover VAD own the turn-end decision.
                        uplink_frames = uplink_preroll.drain() + frames
                        uplink_vad.begin()
                        sent = 0
                        for f16 in uplink_frames:
                            if self._local_photo_cloud_quarantine.active():
                                break
                            try:
                                await _a.wait_for(
                                    chan.send_audio(enc.encode(f16.tobytes(), 960)), timeout=3
                                )
                                sent += 1
                                await _a.sleep(0)
                            except Exception as exc:
                                logger.warning("xz uplink send failed: %s", exc)
                                break
                        while (
                            not stop_event.is_set()
                            and not self._local_photo_cloud_quarantine.active()
                        ):
                            buf = await _a.to_thread(mic_q.get, True, 5.0)
                            if self._local_photo_cloud_quarantine.active():
                                break
                            # Feed KWS during the uplink extension too — a
                            # wake word spanning the collect/extend boundary
                            # used to lose its tail (你好小白 -> 你好小f).
                            if _xz_kws is not None and not _waked:
                                try:
                                    _hit = _xz_kws.feed(buf)
                                    if _hit:
                                        _waked = True
                                        _wake_deadline = time.time() + WAKE_TIMEOUT
                                        self._xiaozhi_wake_lease.activate()
                                        logger.info(
                                            "wake word detected (local KWS): %s", _hit
                                        )
                                        idle_show_last_activity[0] = time.monotonic()
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning("xz kws feed error: %s", exc)
                            rms = float(
                                np.sqrt(
                                    np.mean(
                                        np.square(
                                            buf.astype(np.float64)
                                        )
                                    )
                                )
                            )
                            _publish_dashboard_mic(float(rms) / 32768.0)
                            try:
                                # Send the frame just read (buf). An earlier
                                # revision sent f16 (the leftover collect-loop
                                # variable) here, so the whole utterance tail
                                # was one repeated frame and the cloud could
                                # not tell what the user said.
                                await _a.wait_for(
                                    chan.send_audio(enc.encode(buf.tobytes(), 960)), timeout=3
                                )
                                sent += 1
                                await _a.sleep(0)
                            except Exception as exc:
                                logger.warning("xz uplink send failed: %s", exc)
                                break
                            # Hangover VAD ends the turn only on sustained
                            # silence (default 1.5 s) or the 12 s cap; single
                            # inter-word pauses no longer cut speech.
                            if uplink_vad.feed(rms) == DECISION_END:
                                break
                        if not self._local_photo_cloud_quarantine.active():
                            await chan.send_json(
                                {"session_id": sid, "type": "listen", "state": "stop"}
                            )
                        _user_speaking[0] = False
                        if sent and not self._local_photo_cloud_quarantine.active():
                            logger.info("xz sent %d frames (rms=%.0f)", sent, rms_max)
                            uplink_response_watchdog.uplink_sent()
                        # No post-burst cooldown: the old 4 s blind window ate
                        # the start of a follow-up sentence. The gate loop
                        # resumes immediately and stays armed via preroll.
                finally:
                    if not rt.done():
                        rt.cancel()
                    try:
                        await rt
                    except _a.CancelledError:
                        pass
                    except Exception as exc:
                        if isinstance(exc, _XiaozhiReconnect):
                            logger.info("xiaozhi receive task requested reconnect: %s", exc)
                        elif _is_expected_xiaozhi_disconnect(exc):
                            logger.warning("xiaozhi receive task closed, reconnecting: %s", exc)
                        else:
                            logger.warning("xiaozhi receive task closed with error: %s", exc)
            finally:
                try:
                    await chan.close()
                except Exception:  # noqa: BLE001 — teardown best effort
                    pass

        try:
            while not stop_event.is_set():
                if _xiaozhi_should_pause_for_mic(audio_input_controller_singleton().enabled()):
                    RUNTIME_HEALTH.update(ws_state="paused", session_id=None, tts_active=False)
                    logger.info("xiaozhi paused while mic input disabled")
                    while not stop_event.is_set() and _xiaozhi_should_pause_for_mic(
                        audio_input_controller_singleton().enabled()
                    ):
                        stop_event.wait(0.5)
                    if stop_event.is_set():
                        break
                    logger.info("xiaozhi resuming after mic input enabled")
                try:
                    _a.run(run())
                except _XiaozhiPaused as e:
                    RUNTIME_HEALTH.update(ws_state="paused", session_id=None, tts_active=False)
                    logger.info("xiaozhi paused: %s", e)
                    continue
                except _XiaozhiReconnect as e:
                    RUNTIME_HEALTH.update(
                        ws_state="reconnecting", session_id=None, tts_active=False
                    )
                    logger.warning("xiaozhi session closed, reconnecting: %s", e)
                except Exception as e:
                    RUNTIME_HEALTH.update(ws_state="error", session_id=None, tts_active=False)
                    logger.exception("xiaozhi ended: %s", e)
                if not stop_event.is_set():
                    reconnects = RUNTIME_HEALTH.increment("reconnects")
                    RUNTIME_HEALTH.update(ws_state="reconnecting")
                    logger.info("xiaozhi reconnecting in 3s (attempt %d)...", reconnects)
                    stop_event.wait(3)
        except Exception as e:
            logger.info("xiaozhi ended: %s", e)
        finally:
            RUNTIME_HEALTH.update(ws_state="stopped", session_id=None, tts_active=False)
            _writer_stop.set()
            _audio_q.put_latest(None)
            _stop_audio_process()
            _audio_thread.join(timeout=2)
            _stop_audio_process()
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
        fails,
        _MAX_STARTUP_FAILURES,
        exc,
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
    except Exception:
        logger.exception("YRobot process crashed during startup/runtime")
        app.stop()
        logging.shutdown()
        os._exit(1)


if __name__ == "__main__":
    cli()
