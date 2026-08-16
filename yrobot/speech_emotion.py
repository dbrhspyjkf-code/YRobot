"""Keyword sentence-emotion classification for XIAOZHI spoken replies.

Maps each spoken sentence (``tts sentence_start`` text) to a Xiaozhi-protocol
emotion so the robot can gesture while it talks, mirroring the official
Conversation App's per-turn expressiveness. Zero-cost, deterministic, and
deliberately conservative: unknown sentences return ``None`` (no move).
"""

from __future__ import annotations

_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("哈哈", "太好了", "好耶", "开心", "高兴", "太棒了", "讲个笑话"), "happy"),
    (
        ("哇，", "哇！", "天哪", "我的天", "惊讶", "竟然", "不会吧", "没想到", "不可思议"),
        "surprised",
    ),
    (("让我想想", "想一想", "我想想", "思考一下", "琢磨"), "thinking"),
    (("对不起", "抱歉", "很遗憾", "可惜", "难过", "伤心", "不太好"), "sad"),
    (("生气", "讨厌", "气死", "太过分"), "angry"),
    (("爱你", "喜欢你", "抱抱", "么么", "亲亲"), "loving"),
    (("谢谢", "感谢", "多谢"), "grateful"),
    (("吓死", "吓我", "害怕", "可怕", "吓人"), "scared"),
    (("没问题", "交给我", "包在我身上", "当然可以", "放心"), "confident"),
    (("好困", "困了", "想睡觉", "打个哈欠"), "sleepy"),
    (("恭喜", "成功了", "干得漂亮"), "excited"),
)


def sentence_emotion(text: str) -> str | None:
    """Return a Xiaozhi emotion keyword-matched in *text*, else ``None``."""
    compact = (text or "").replace(" ", "")
    if not compact:
        return None
    for keywords, emotion in _RULES:
        if any(keyword in compact for keyword in keywords):
            return emotion
    return None
