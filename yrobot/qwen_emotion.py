"""Deterministic, local matching for explicit conversational gestures."""

from __future__ import annotations


def requested_emotion(transcript: str) -> str | None:
    """Map explicit conversational requests to one bounded gesture."""
    text = transcript.casefold().replace(" ", "")
    if any(word in text for word in ("打开", "关闭", "查询", "价格", "音量", "天气", "灯")):
        return None
    if "惊喜" in text or "惊讶" in text:
        return "surprised"
    if "开心" in text or "笑话" in text or "高兴" in text:
        return "happy"
    if "想一想" in text or "思考" in text or "想想" in text:
        return "thinking"
    if "难过" in text or "伤心" in text:
        return "sad"
    return None
