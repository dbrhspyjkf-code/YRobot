"""Small WebSocket client for Qwen3.5 Omni Realtime."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from threading import Event
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

from yrobot.config import Settings

logger = logging.getLogger(__name__)

MAX_TOOL_OUTPUT_BYTES = 4096
# 190 KB decoded image == ~253 KB base64. Warn before the server rejects.
_IMAGE_B64_WARN_BYTES = 200 * 1024
_MIN_AUDIO_APPENDS_BEFORE_IMAGE = 3
DEFAULT_INSTRUCTIONS = (
    "你是 Reachy Mini 桌面机器人。使用用户当前使用的语言自然、简短地回答；"
    "用户切换语言时立即跟随。只有工具实际返回成功时，才能确认操作成功。\n"
    "股票代码是六位数字。用户念代码时是逐位读的：听到中文数字必须逐字转换"
    "（幺/一→1，两→2，〇/零→0，其余按字面），绝不能按数值理解——例如“六八八零"
    "“幺八”逐位就是 688018。如果逐位转换后不足六位、工具参数拼不出合法代码、"
    "或查询失败，绝不要猜一个代码去查：先向用户复述你逐位听到的数字并请他一位"
    "一位重说，也可以先查自选股列表按代码或名称模糊匹配。\n"
    "调用工具时不要从无意义的字词里提取参数（例如把“什么”当成城市名）；"
    "参数不确定时先向用户确认。"
)
# Appended to the system prompt only when settings.send_video is on. The
# model buffers the most recent ~120 s of frames; this prompt tells it
# that the buffer is real camera input (not a single snapshot) and asks
# it to ground visual answers in what it actually sees rather than
# guessing.
VISION_POLICY = (
    "你正在通过用户的摄像头看到实时画面（约 1 fps 抽帧，模型保留最近约 120 秒）。"
    "当用户问及“看到什么/看见/描述/画面/前面/那边/这是什么/手里拿的”等视觉相关问题时，"
    "请基于你看到的真实画面内容回答，不要用“作为语言模型我看不到画面”等套话回避。"
    "如果图像看不清，诚实说看不清。"
)


def _ignore(*_args: Any) -> None:
    return None


def _model_url(base_url: str, model: str) -> str:
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["model"] = model
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class QwenRealtimeClient:
    """Translate YRobot PCM and callbacks to the official realtime events."""

    def __init__(
        self,
        settings: Settings,
        tools: Any,
        *,
        on_audio: Callable[[bytes], None] = _ignore,
        on_input_transcript: Callable[[str], None] = _ignore,
        on_output_transcript: Callable[[str], None] = _ignore,
        on_interrupt: Callable[[], None] = _ignore,
        on_user_speech: Callable[[], None] = _ignore,
        on_response_done: Callable[[], None] = _ignore,
        on_state: Callable[[str], None] = _ignore,
        on_error: Callable[[str], None] = _ignore,
    ) -> None:
        self.settings = settings
        self.tools = tools
        vision_prompt = f"\n\n{VISION_POLICY}" if settings.send_video else ""
        self.instructions = (
            f"{DEFAULT_INSTRUCTIONS}\n{settings.effective_system_prompt}{vision_prompt}"
        )
        self.url = _model_url(settings.qwen_url, settings.qwen_model)
        self.websocket_factory = websockets.connect
        self.websocket: Any | None = None
        self.ready = asyncio.Event()
        self._on_audio = on_audio
        self._on_input_transcript = on_input_transcript
        self._on_output_transcript = on_output_transcript
        self._on_interrupt = on_interrupt
        self._on_user_speech = on_user_speech
        self._on_response_done = on_response_done
        self._on_state = on_state
        self._on_error = on_error
        self._active_response_id: str | None = None
        self._cancelled_response_ids: set[str] = set()
        self._completed_call_ids: set[str] = set()
        self._pending_tool_response_create = False
        self._reported_error = False
        self._speech_started_at: float | None = None
        self._audio_appends_since_connect = 0

    def session_update(self) -> dict[str, Any]:
        return {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": self.settings.qwen_voice,
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "input_audio_transcription": {
                    "model": "qwen3-asr-flash-realtime",
                    "language": "zh",
                },
                "instructions": self.instructions,
                "turn_detection": None,
                "tools": self.tools.schemas(),
            },
        }

    async def run(self, stop_event: Event) -> None:
        if not self.settings.qwen_api_key:
            raise ValueError("DASHSCOPE_API_KEY is required for QWEN")
        self._on_state("connecting")
        try:
            async with self.websocket_factory(
                self.url,
                additional_headers={
                    "Authorization": f"Bearer {self.settings.qwen_api_key}",
                },
            ) as websocket:
                self.websocket = websocket
                self._audio_appends_since_connect = 0
                self._on_state("connected")
                while not stop_event.is_set():
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                    except TimeoutError:
                        continue
                    await self.handle_event(json.loads(message))
        except Exception as exc:
            if not self._reported_error:
                self._report_error(str(exc))
            raise
        else:
            self._on_state("disconnected")
        finally:
            self.websocket = None

    async def append_pcm(self, pcm: bytes) -> None:
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )
        self._audio_appends_since_connect += 1

    async def append_image(self, image_b64: str) -> None:
        """Append a base64 JPEG to the model's rolling visual buffer.

        DashScope Qwen-Omni-Flash-Realtime maintains a ~120s rolling buffer
        of recent frames (50 video turns); older frames are auto-discarded.
        Per the official Qwen-Omni-Realtime API:
          * the base64 string must contain no whitespace or newlines,
          * the decoded image should stay under 190 KB,
          * audio must already have been sent in this session at least once,
          * ~1 fps is the recommended cadence.
        The caller (main.py) is responsible for both the audio-first ordering
        and the 1 fps rate.
        """
        if self._audio_appends_since_connect < _MIN_AUDIO_APPENDS_BEFORE_IMAGE:
            logger.debug(
                "skip image before QWEN audio is established: audio_appends=%d",
                self._audio_appends_since_connect,
            )
            return
        if any(ch.isspace() for ch in image_b64):
            raise ValueError("image base64 must not contain whitespace or newlines")
        if len(image_b64) > _IMAGE_B64_WARN_BYTES:
            logger.warning(
                "input_image_buffer.append payload %d bytes; QWEN 190 KB target",
                len(image_b64),
            )
        await self._send(
            {
                "type": "input_image_buffer.append",
                "image": image_b64,
            }
        )

    async def commit_turn(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})

    async def resend_session_update(self) -> None:
        """Re-emit the current session.update payload.

        Used by main.py when the recognised speaker changes mid-session
        so the model can address the new person by name. QWEN
        Qwen-Omni-Flash-Realtime applies the new session config to
        the next response (an in-flight response is unaffected).
        """
        await self._send(self.session_update())

    async def resend_session_update_with(self, *, instructions_override: str) -> None:
        """Re-emit session.update with a caller-supplied instructions
        string. main.py uses this to splice in 'you are now talking
        to {name}' without rebuilding the rest of the session dict.
        """
        payload = self.session_update()
        payload["session"]["instructions"] = instructions_override
        await self._send(payload)

    async def request_response(self, *, cancel_active: bool = True) -> None:
        cancelled = False
        if cancel_active:
            cancelled = await self._cancel_active_response()
        if cancelled:
            await asyncio.sleep(0.4)
        await self._send({"type": "response.create"})

    async def cancel_and_inject(self, text: str, *, role: str = "user") -> None:
        """Cancel any in-flight response, inject a text message, then
        ask the model to respond. Used by main.py to surface the truth
        when the spoken-control path's trigger-word match fails (or the
        user stt is garbled) so the model does not fabricate a
        “好的，已 X” reply.

        Flow:
          1. response.cancel for any active response
          2. conversation.item.create {type: message, role, content}
          3. response.create

        Note: an earlier iteration also sent conversation.interrupt,
        but the QWEN qwen3.5-omni-flash-realtime endpoint rejects it
        as “Invalid value” which pushes the service into safe_mode,
        so we only use response.cancel.

        The injected text should be written from the system\'s POV
        (e.g. “[system] user input did not match any local tool”),
        not as a fake user utterance.
        """
        try:
            await self._cancel_active_response()
        except Exception:
            pass
        # QWEN needs a moment to actually finalize the cancel on its
        # side before we can issue a new response.create. Without this
        # sleep we hit a race where the cancel is still in flight and
        # QWEN rejects the new request with
        # 'Conversation already has an active response', which cascades
        # into the service going to safe_mode.
        import asyncio as _asyncio

        await _asyncio.sleep(0.4)
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self.request_response()

    async def set_turn_detection(self, mode: str | None) -> None:
        if mode not in {None, "semantic_vad"}:
            raise ValueError("turn detection must be None or 'semantic_vad'")
        turn_detection = None
        if mode == "semantic_vad":
            turn_detection = {
                "type": "semantic_vad",
                "threshold": 0.5,
                "silence_duration_ms": 1200,
            }
        logger.info("QWEN turn_detection: %s", turn_detection)
        await self._send(
            {
                "type": "session.update",
                "session": {"turn_detection": turn_detection},
            }
        )

    async def handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "session.created":
            await self._send(self.session_update())
        elif event_type == "session.updated":
            self.ready.set()
        elif event_type == "response.created":
            self._active_response_id = (event.get("response") or {}).get("id")
        elif event_type == "response.audio.delta":
            response_id = event.get("response_id")
            if response_id in self._cancelled_response_ids:
                return
            if response_id:
                self._active_response_id = response_id
            self._on_audio(base64.b64decode(event["delta"], validate=True))
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = str(event.get("transcript") or "")
            elapsed = None
            if self._speech_started_at is not None:
                elapsed = time.monotonic() - self._speech_started_at
            logger.info(
                "QWEN ASR completed: elapsed_s=%s transcript=%r",
                None if elapsed is None else round(elapsed, 3),
                transcript,
            )
            self._on_input_transcript(transcript)
        elif event_type == "response.audio_transcript.done":
            self._on_output_transcript(str(event.get("transcript") or ""))
        elif event_type == "input_audio_buffer.speech_started":
            self._speech_started_at = time.monotonic()
            logger.info("QWEN ASR speech_started")
            self._on_user_speech()
            self._on_interrupt()
            await self._cancel_active_response()
        elif event_type == "input_audio_buffer.speech_stopped":
            elapsed = None
            if self._speech_started_at is not None:
                elapsed = time.monotonic() - self._speech_started_at
            logger.info(
                "QWEN ASR speech_stopped: elapsed_s=%s",
                None if elapsed is None else round(elapsed, 3),
            )
        elif event_type == "response.function_call_arguments.done":
            await self._complete_tool_call(event)
        elif event_type == "response.done":
            response = event.get("response") or {}
            response_id = response.get("id")
            if response_id is None or response_id == self._active_response_id:
                self._active_response_id = None
            function_calls = [
                item
                for item in response.get("output") or []
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            if function_calls:
                for item in function_calls:
                    await self._complete_tool_call(item)
                return
            if self._pending_tool_response_create:
                self._pending_tool_response_create = False
                await self.request_response(cancel_active=False)
                return
            self._on_response_done()
        elif event_type == "error":
            error = event.get("error") or {}
            message = str(error.get("message") or event.get("message") or "QWEN realtime error")
            if "append image before append audio" in message.casefold():
                logger.warning("QWEN ignored recoverable vision ordering error: %s", message)
                return
            self._report_error(message)
            raise RuntimeError(message)

    async def _send(self, document: dict[str, Any]) -> None:
        if self.websocket is None:
            raise RuntimeError("QWEN WebSocket is not connected")
        await self.websocket.send(json.dumps(document, ensure_ascii=False, separators=(",", ":")))

    async def _cancel_active_response(self) -> bool:
        if self._active_response_id is None:
            return False
        response_id = self._active_response_id
        self._active_response_id = None
        self._cancelled_response_ids.add(response_id)
        try:
            await self._send({"type": "response.cancel"})
        except RuntimeError as exc:
            if "conversation has none active response" in str(exc).casefold():
                return True
            raise
        return True

    async def _complete_tool_call(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("call_id") or "")
        if call_id in self._completed_call_ids:
            return
        self._completed_call_ids.add(call_id)
        try:
            arguments = json.loads(event.get("arguments") or "")
            if not isinstance(arguments, dict):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            result = {"ok": False, "error": "invalid function arguments"}
        else:
            result = await asyncio.to_thread(
                self.tools.execute,
                str(event.get("name") or ""),
                arguments,
            )
        output = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(output.encode("utf-8")) > MAX_TOOL_OUTPUT_BYTES:
            output = json.dumps(
                {"ok": False, "error": "tool result too large"},
                separators=(",", ":"),
            )
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )
        try:
            await self.request_response(cancel_active=False)
        except RuntimeError as exc:
            if "conversation already has an active response" in str(exc).casefold():
                logger.info("QWEN tool follow-up deferred until response.done: %s", exc)
                self._pending_tool_response_create = True
                return
            raise

    def _report_error(self, message: str) -> None:
        self._reported_error = True
        self._on_state("error")
        self._on_error(message)
