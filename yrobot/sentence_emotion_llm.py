"""LLM sentence-emotion fallback tier via DashScope qwen-flash.

Keyword misses used to mean "no gesture" (3-day field data: 2/76 sentences
hit the keyword table, and every Xiaozhi ``llm``-event emotion tag was the
default "happy"). This module adds a semantic second tier: classify the
spoken sentence with a cheap cloud LLM so replies can gesture on meaning.

Hard requirements (v2-rollback lesson — external deps must never hurt the
conversation main path):
- total function: returns a protocol emotion string or ``None``, never raises
- strictly time-bounded (2 s HTTP timeout); the caller runs it on a worker
  thread (``asyncio.to_thread``), never on the WebSocket event loop
- output normalised against the emotion vocabulary that
  ``motion.EMOTION_TO_MOVES`` can map; anything else (including Chinese
  replies like the observed "快乐") maps to ``None``
- opt-out with ``YROBOT_LLM_EMOTION=0``; a missing ``DASHSCOPE_API_KEY``
  disables the tier entirely (no request is attempted)
"""

from __future__ import annotations

import json
import os
import urllib.request

_EMOTION_VOCAB = frozenset(
    {
        "happy",
        "laughing",
        "surprised",
        "thinking",
        "confused",
        "sad",
        "angry",
        "scared",
        "bored",
        "lonely",
        "embarrassed",
        "loving",
        "grateful",
        "excited",
        "confident",
        "sleepy",
        "welcoming",
    }
)

_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_MODEL = os.environ.get("YROBOT_LLM_EMOTION_MODEL", "qwen-flash")
_TIMEOUT_S = 2.0

_SYSTEM_PROMPT = (
    "You label one spoken sentence of a social robot with an emotion. "
    "Reply with EXACTLY ONE English word from this set: "
    "happy, laughing, surprised, thinking, confused, sad, angry, scared, "
    "bored, lonely, embarrassed, loving, grateful, excited, confident, "
    "sleepy, welcoming, none. "
    "Factual statements (time, weather, data, plans) are none even if "
    "mildly positive. Use none when the sentence carries no clear emotion. "
    "Never use synonyms (sorry/great/...) — only the exact words listed. "
    "No punctuation, no explanation, no Chinese."
)


def _http_chat_completion(api_key: str, text: str) -> str:
    """Blocking DashScope chat POST; raises on any transport/parse failure."""
    payload = json.dumps(
        {
            "model": _MODEL,
            "temperature": 0,
            "max_tokens": 6,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text[:200]},
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"] or ""


def classify_sentence_llm(text: str, *, api_key: str | None = None) -> str | None:
    """Semantically classify *text*; ``None`` on miss/disable/failure.

    Never raises and never blocks longer than the HTTP timeout.
    """
    compact = (text or "").replace(" ", "")
    if not compact:
        return None
    if os.environ.get("YROBOT_LLM_EMOTION", "1").strip() == "0":
        return None
    key = api_key or os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        return None
    try:
        raw = _http_chat_completion(key, compact)
    except Exception:  # noqa: BLE001 — total by contract
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    first = stripped.split()[0].strip(".,!?;:。！？，、").lower()
    return first if first in _EMOTION_VOCAB else None
