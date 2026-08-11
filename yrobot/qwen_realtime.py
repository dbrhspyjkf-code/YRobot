"""Small WebSocket client for Qwen3.5 Omni Realtime."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from threading import Event
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

from yrobot.config import Settings

MAX_TOOL_OUTPUT_BYTES = 4096
DEFAULT_INSTRUCTIONS = (
    "你是 Reachy Mini 桌面机器人。使用用户当前使用的语言自然、简短地回答；"
    "用户切换语言时立即跟随。只有工具实际返回成功时，才能确认操作成功。"
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
        self.instructions = f"{DEFAULT_INSTRUCTIONS}\n{settings.effective_system_prompt}"
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
        self._reported_error = False

    def session_update(self) -> dict[str, Any]:
        return {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": self.settings.qwen_voice,
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
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

    async def commit_turn(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})

    async def request_response(self) -> None:
        await self._send({"type": "response.create"})

    async def cancel_and_inject(self, text: str, *, role: str = "user") -> None:
        """Cancel any in-flight response, inject a text message, then
        ask the model to respond. Used by main.py to surface the truth
        when the spoken-control path's trigger-word match fails (or the
        user stt is garbled) so the model does not fabricate a
        "好的，已 X" reply.

        Flow:
          1. conversation.interrupt (best-effort)
          2. response.cancel for any active response
          3. conversation.item.create {type: message, role, content}
          4. response.create

        The injected text should be written from the system's POV
        (e.g. '[系统] 用户的话没匹配到任何可执行工具。'), not as a
        fake user utterance.
        """
        try:
            await self._send({"type": "conversation.interrupt"})
        except Exception:
            pass
        try:
            await self._cancel_active_response()
        except Exception:
            pass
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": text}],
            },
        })
        await self.request_response()

    async def set_turn_detection(self, mode: str | None) -> None:
        if mode not in {None, "semantic_vad"}:
            raise ValueError("turn detection must be None or 'semantic_vad'")
        turn_detection = None if mode is None else {"type": mode}
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
            self._on_input_transcript(str(event.get("transcript") or ""))
        elif event_type == "response.audio_transcript.done":
            self._on_output_transcript(str(event.get("transcript") or ""))
        elif event_type == "input_audio_buffer.speech_started":
            self._on_user_speech()
            self._on_interrupt()
            await self._cancel_active_response()
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
            self._on_response_done()
        elif event_type == "error":
            error = event.get("error") or {}
            message = str(error.get("message") or event.get("message") or "QWEN realtime error")
            self._report_error(message)
            raise RuntimeError(message)

    async def _send(self, document: dict[str, Any]) -> None:
        if self.websocket is None:
            raise RuntimeError("QWEN WebSocket is not connected")
        await self.websocket.send(json.dumps(document, ensure_ascii=False, separators=(",", ":")))

    async def _cancel_active_response(self) -> None:
        if self._active_response_id is None:
            return
        response_id = self._active_response_id
        self._active_response_id = None
        self._cancelled_response_ids.add(response_id)
        await self._send({"type": "response.cancel"})

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
        await self.request_response()

    def _report_error(self, message: str) -> None:
        self._reported_error = True
        self._on_state("error")
        self._on_error(message)
