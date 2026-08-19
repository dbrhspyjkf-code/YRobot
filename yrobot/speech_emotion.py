"""Keyword sentence-emotion classification for XIAOZHI spoken replies.

Maps each spoken sentence (``tts sentence_start`` text) to a Xiaozhi-protocol
emotion so the robot can gesture while it talks, mirroring the official
Conversation App's per-turn expressiveness. Zero-cost, deterministic, and
deliberately conservative: unknown sentences return ``None`` (no move).

2026-08-19 expansion (3-day field data: keyword hits were 2/76 sentences):
wider synonyms per group plus new groups (welcoming / confused / laughing).
High-frequency acknowledgement fillers (好的/明白了/嗯嗯) stay excluded so
plain conversational turns never gesture.
"""

from __future__ import annotations

_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    # Greeting first so "很高兴见到" is not captured by the happy bucket.
    (("欢迎回来", "欢迎", "很高兴见到", "初次见面"), "welcoming"),
    # Specific groups next so they win over the broad buckets below.
    (
        ("没听懂", "没听清", "没明白", "没理解", "什么意思", "再说一遍"),
        "confused",
    ),
    (
        (
            "哈哈", "嘿嘿", "嘻嘻", "太好了", "好耶", "开心",
            "高兴", "太棒了", "讲个笑话", "有意思", "好玩",
        ),
        "happy",
    ),
    (
        (
            "哇，", "哇！", "天哪", "我的天", "惊讶", "竟然",
            "不会吧", "没想到", "不可思议", "真的假的", "什么情况",
            "啥情况", "居然", "惊了", "离谱",
        ),
        "surprised",
    ),
    (
        (
            "让我想想", "想一想", "我想想", "思考一下", "琢磨",
            "容我想想", "分析一下", "研究一下",
        ),
        "thinking",
    ),
    (
        ("对不起", "抱歉", "很遗憾", "可惜", "难过", "伤心", "不太好", "失败了", "失望", "没能"),
        "sad",
    ),
    (("生气", "讨厌", "气死", "太过分", "烦死", "真烦", "可恶", "气人"), "angry"),
    (
        ("爱你", "喜欢你", "抱抱", "么么", "亲亲", "想你", "摸摸头", "最喜欢你"),
        "loving",
    ),
    (
        ("谢谢", "感谢", "多谢", "辛苦了", "辛苦啦", "麻烦你了", "麻烦你啦"),
        "grateful",
    ),
    (("吓死", "吓我", "害怕", "可怕", "吓人", "吓坏", "好险"), "scared"),
    (
        ("没问题", "交给我", "包在我身上", "当然可以", "放心", "稳稳的", "保证", "妥妥的"),
        "confident",
    ),
    (
        ("好困", "困了", "想睡觉", "打个哈欠", "想眯一会", "困得不行", "犯困"),
        "sleepy",
    ),
    (
        ("恭喜", "成功了", "干得漂亮", "太燃了", "好激动", "期待", "庆祝"),
        "excited",
    ),
    # Laughing last among the expressive groups: sentences that carry both a
    # laughter keyword and 哈哈 stay "happy" (existing contract); pure
    # 笑死-type sentences still land here.
    (("笑死", "太好笑了", "笑死人", "肚子笑疼"), "laughing"),
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
