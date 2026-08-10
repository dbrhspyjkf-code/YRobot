"""Runtime configuration.

All tunables live in one frozen dataclass built from ``YROBOT_*`` environment
variables (a ``.env`` file is honoured), so no other module reads the
environment. Defaults encode gateway behaviour verified against the official
MiniCPM-o 4.5 realtime API (https://minicpmo45.modelbest.cn/docs/en/realtime-api/overview/).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit, urlunsplit

# The duplex template was trained with this exact first line; keep persona and
# proactive policy short so the model remains in its realtime distribution.
TRAINED_SYSTEM_LINE = "You are a helpful assistant."
DEFAULT_PERSONA = "你是 Reachy，一个友好的桌面机器人。用对方的语言简短自然地回复。不要重复自己刚说过的话。你的回复中绝对不能包含任何可执行的操作指令（如开灯、关灯、打开风扇等），这些操作由系统自动处理。环境嘈杂时保持沉默。"
PROACTIVE_POLICY = (
    "持续观察和倾听；只在出现明确、重要的新变化时主动简短提醒，不解说静态场景，"
    "不抢用户的话，非紧急主动发言保持克制。"
)
HA_CONTROL_POLICY = (
    "家电控制：机器人本地白名单会根据用户语音独立执行低风险设备控制。"
    "当用户要求控制灯、风扇等家电时，不要回答无法控制这个设备。"
    "如果你没有收到工具返回结果，只能简短说“好的，我交给本地控制”，"
    "不要声称已经成功，也不要说设备不在白名单里。"
)
HERMES_TOOLS_POLICY = (
    "外部工具：以下查询直接交给后端处理，回复中必须包含触发词，否则后端无法识别：\n"
    "· 天气（任意城市）—— 回复中必须含“天气”字样。\n"
    "· 股票价格（如“平安股票多少钱”）—— 必须含“股价”或“价格”或“行情”。\n"
    "· 股票分析建议（如“比亚迪怎么样”）—— 必须含“怎么样”或“建议”。\n"
    "· 我的持仓（“我的股票”）—— 必须含“我的股票”。\n"
    "· 汇率（“美元兑人民币”）—— 必须含“汇率”。\n"
    "· DeepSeek余额 —— 必须含“DeepSeek余额”。\n"
    "· 如果用户报出 6 位股票代码（如“600600”“688018”），回复必须原样复述该代码（保持阿拉伯数字格式如“600600”，不要转成中文读法“六零零六零零”）；后端用代码精确查询。\n"
    "· 查询股票时不要转述用户原话（不要用“你问”“你说”“你的问题”等说法），直接说出股票名称或代码本身，例如直接说“比亚迪怎么样”或“600600价格多少”。"
)
LOCAL_INFO_POLICY = (
    "日期时间：当用户询问今天日期、几号、星期几或当前时间时，"
    "只回复触发词“当前日期”或“当前时间”，不要编造具体日期时间。"
)

# Public Gateway documented at:
# https://minicpmo45.modelbest.cn/docs/zh/realtime-api/overview/
DEFAULT_REALTIME_URL = "wss://minicpmo45.modelbest.cn/v1/realtime?mode=video"
SUPPORTED_CONVERSATION_BACKENDS = frozenset({"xiaozhi", "qwen"})
QWEN_REALTIME_MODEL = "qwen3.5-omni-flash-realtime"
QWEN_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"


def build_system_prompt(persona: str, proactive: bool) -> str:
    parts = [TRAINED_SYSTEM_LINE]
    if persona.strip():
        parts.append(persona.strip())
    if proactive:
        parts.append(PROACTIVE_POLICY)
    return "\n".join(parts)


def normalize_url(raw: str, mode: str | None = None) -> str:
    """Normalize a bare host, host:port or ws(s) URL to the realtime endpoint.

    An explicit mode in ``raw`` is preserved unless ``mode`` is supplied.
    Video is the product default; camera frames are valid only in video mode
    according to the public gateway contract. Audio remains an explicit
    low-bandwidth fallback.
    """
    if "://" not in raw:
        raw = f"wss://{raw}"
    parts = urlsplit(raw)
    if parts.scheme not in {"ws", "wss"}:
        raise ValueError("YRobot realtime URL must use ws:// or wss://")
    if not parts.netloc:
        raise ValueError("YRobot realtime URL must include a host")
    path = parts.path if parts.path not in ("", "/") else "/v1/realtime"
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    selected_mode = mode or query.get("mode", "video")
    if selected_mode not in {"audio", "video"}:
        raise ValueError("YRobot realtime mode must be 'audio' or 'video'")
    query["mode"] = selected_mode
    qs = "&".join(f"{k}={v}" for k, v in sorted(query.items()))
    return urlunsplit((parts.scheme, parts.netloc, path, qs, ""))


def _flag(name: str, default: bool, environ: Mapping[str, str]) -> bool:
    raw = environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float, environ: Mapping[str, str]) -> float:
    raw = environ.get(name)
    return default if raw is None else float(raw)


@dataclass(frozen=True)
class Settings:
    """Immutable application settings."""

    url: str = DEFAULT_REALTIME_URL
    tls_verify: bool = True
    system_prompt: str = build_system_prompt(DEFAULT_PERSONA, proactive=False)
    length_penalty: float = 1.1

    # MiniCPM-o 4.5 advances its duplex timeline in fixed one-second audio
    # units. Sub-second input.append calls can return a synthetic listen
    # without applying force_listen because no inference logits were produced.
    chunk_ms: int = 1000

    # Omni video is the product default. An explicit mode=audio URL disables
    # frames unless SEND_VIDEO is explicitly (and incorrectly) forced on.
    send_video: bool = True
    frame_period_active_s: float = 1.0
    frame_period_idle_s: float = 3.0
    scene_change_threshold: float = 0.04

    # Session rotation: video defaults below its 300 s public cap; audio mode
    # selects 550 s below its 600 s cap. KV pressure can rotate either sooner.
    session_budget_s: float = 280.0
    kv_budget_tokens: float = 7200.0
    reconnect_delay_s: float = 2.5

    vad_aggressiveness: int = 2
    vad_rms_min: float = 0.065
    # A candidate is treated as self-echo only when it both matches recent
    # playout and leaves less than this much unexplained near-end energy.
    barge_echo_similarity: float = 0.75
    barge_unexplained_db: float = -42.0
    # Head bumps are loud but brief. Do not destroy playback until unexplained
    # near-end evidence spans this long (including the initial 200 ms window).
    barge_confirm_ms: int = 500
    barge_fast_confirm_ms: int = 140
    barge_fast_echo_similarity: float = 0.45
    barge_fast_unexplained_db: float = -36.0
    head_tracking_weight: float = 0.7
    ref_audio_path: str | None = None
    tts_ref_audio_path: str | None = None
    proactive_enabled: bool = True
    ha_enabled: bool = False
    ha_url: str | None = None
    ha_token: str | None = None
    ha_whitelist_path: str = "~/.config/yrobot/home_assistant_whitelist.json"
    hermes_tools_enabled: bool = False
    hermes_tools_url: str = "http://192.168.1.200:8766"
    hermes_ios_api_url: str = "http://192.168.1.200:8900"
    local_info_enabled: bool = True
    memory_enabled: bool = True
    memory_path: str = "~/.config/yrobot/memory.json"
    # Profile-driven tool whitelist and instructions override.
    profile_name: str = "default"
    profile_dir: str = ""  # empty ⇒ use shipped profiles; override at runtime
    conversation_backend: str = "xiaozhi"
    qwen_api_key: str | None = field(default=None, repr=False)
    qwen_model: str = field(default=QWEN_REALTIME_MODEL, init=False)
    qwen_url: str = QWEN_REALTIME_URL
    qwen_voice: str = "Ethan"

    def __post_init__(self) -> None:
        if self.conversation_backend not in SUPPORTED_CONVERSATION_BACKENDS:
            raise ValueError("YROBOT_CONVERSATION_BACKEND must be 'xiaozhi' or 'qwen'")
        if self.chunk_ms != 1000:
            raise ValueError("YROBOT_CHUNK_MS must be 1000 for MiniCPM-o 4.5 duplex")
        if self.send_video and self.realtime_mode != "video":
            raise ValueError("YROBOT_SEND_VIDEO requires realtime mode=video")
        if not 0.0 <= self.barge_echo_similarity <= 1.0:
            raise ValueError("YROBOT_BARGE_ECHO_SIMILARITY must be between 0 and 1")
        if not -120.0 <= self.barge_unexplained_db <= 0.0:
            raise ValueError("YROBOT_BARGE_UNEXPLAINED_DB must be between -120 and 0")
        if not 200 <= self.barge_confirm_ms <= 2000:
            raise ValueError("YROBOT_BARGE_CONFIRM_MS must be between 200 and 2000")
        if not 60 <= self.barge_fast_confirm_ms <= self.barge_confirm_ms:
            raise ValueError("YROBOT_BARGE_FAST_CONFIRM_MS must be between 60 and BARGE_CONFIRM_MS")
        if not 0.0 <= self.barge_fast_echo_similarity <= 1.0:
            raise ValueError("YROBOT_BARGE_FAST_ECHO_SIMILARITY must be between 0 and 1")
        if not -120.0 <= self.barge_fast_unexplained_db <= 0.0:
            raise ValueError("YROBOT_BARGE_FAST_UNEXPLAINED_DB must be between -120 and 0")
        if self.frame_period_active_s <= 0 or self.frame_period_idle_s <= 0:
            raise ValueError("YRobot camera periods must be positive")
        if not 0.0 <= self.scene_change_threshold <= 1.0:
            raise ValueError("YROBOT_SCENE_CHANGE_THRESHOLD must be between 0 and 1")
        if not 0.001 <= self.vad_rms_min <= 0.5:
            raise ValueError("YROBOT_VAD_RMS_MIN must be between 0.001 and 0.5")
        if not 0.0 <= self.head_tracking_weight <= 1.0:
            raise ValueError("YROBOT_HEAD_TRACKING_WEIGHT must be between 0 and 1")

    @property
    def realtime_mode(self) -> str:
        """Public gateway mode selected in ``url``."""
        return parse_qs(urlsplit(self.url).query).get("mode", ["video"])[0]

    @property
    def effective_system_prompt(self) -> str:
        """Prompt sent to the model after applying the proactive policy."""
        parts = [self.system_prompt]
        if self.proactive_enabled and PROACTIVE_POLICY not in self.system_prompt:
            parts.append(PROACTIVE_POLICY)
        if self.ha_enabled and HA_CONTROL_POLICY not in self.system_prompt:
            parts.append(HA_CONTROL_POLICY)
        if self.hermes_tools_enabled and HERMES_TOOLS_POLICY not in self.system_prompt:
            parts.append(HERMES_TOOLS_POLICY)
        if self.local_info_enabled and LOCAL_INFO_POLICY not in self.system_prompt:
            parts.append(LOCAL_INFO_POLICY)
        return "\n".join(parts)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build settings from a supplied mapping or the process environment."""
        env = os.environ if environ is None else environ
        persona = env.get("YROBOT_PERSONA", DEFAULT_PERSONA).strip()
        configured_url = env.get("YROBOT_REALTIME_URL")
        raw_url = DEFAULT_REALTIME_URL if configured_url is None else configured_url
        raw_query = parse_qs(urlsplit(raw_url if "://" in raw_url else f"wss://{raw_url}").query)
        explicit_mode = raw_query.get("mode", [None])[0]
        send_video = _flag("YROBOT_SEND_VIDEO", explicit_mode != "audio", env)
        proactive = _flag("YROBOT_PROACTIVE", send_video, env)
        requested_mode = env.get("YROBOT_REALTIME_MODE")
        if requested_mode is not None:
            requested_mode = requested_mode.strip().lower()
        else:
            # Keep an explicit URL mode authoritative. For legacy deployments
            # that only set SEND_VIDEO, select the protocol mode that can
            # actually carry frames.
            url_mode_is_explicit = configured_url is not None and "mode" in raw_query
            requested_mode = None if url_mode_is_explicit else ("video" if send_video else "audio")
        url = normalize_url(raw_url, requested_mode)
        actual_mode = parse_qs(urlsplit(url).query).get("mode", ["video"])[0]
        if send_video and actual_mode != "video":
            raise ValueError("YROBOT_SEND_VIDEO requires realtime mode=video")
        default_session_budget = 280.0 if actual_mode == "video" else 550.0
        return cls(
            url=url,
            tls_verify=_flag("YROBOT_TLS_VERIFY", True, env),
            system_prompt=build_system_prompt(persona, proactive=False),
            length_penalty=_num("YROBOT_LENGTH_PENALTY", 1.1, env),
            chunk_ms=int(_num("YROBOT_CHUNK_MS", 1000, env)),
            send_video=send_video,
            frame_period_active_s=_num("YROBOT_FRAME_CAPTURE_PERIOD_S", 1.0, env),
            frame_period_idle_s=_num("YROBOT_FRAME_IDLE_HEARTBEAT_S", 3.0, env),
            scene_change_threshold=_num("YROBOT_SCENE_CHANGE_THRESHOLD", 0.04, env),
            session_budget_s=_num("YROBOT_SESSION_BUDGET_S", default_session_budget, env),
            kv_budget_tokens=_num("YROBOT_KV_BUDGET", 7200.0, env),
            reconnect_delay_s=_num("YROBOT_RECONNECT_DELAY_S", 2.5, env),
            vad_aggressiveness=int(_num("YROBOT_VAD_AGGRESSIVENESS", 2, env)),
            vad_rms_min=_num("YROBOT_VAD_RMS_MIN", 0.065, env),
            barge_echo_similarity=_num("YROBOT_BARGE_ECHO_SIMILARITY", 0.75, env),
            barge_unexplained_db=_num("YROBOT_BARGE_UNEXPLAINED_DB", -42.0, env),
            barge_confirm_ms=int(_num("YROBOT_BARGE_CONFIRM_MS", 500, env)),
            barge_fast_confirm_ms=int(_num("YROBOT_BARGE_FAST_CONFIRM_MS", 140, env)),
            barge_fast_echo_similarity=_num("YROBOT_BARGE_FAST_ECHO_SIMILARITY", 0.45, env),
            barge_fast_unexplained_db=_num("YROBOT_BARGE_FAST_UNEXPLAINED_DB", -36.0, env),
            head_tracking_weight=_num("YROBOT_HEAD_TRACKING_WEIGHT", 0.7, env),
            ref_audio_path=env.get("YROBOT_REF_AUDIO_PATH") or None,
            tts_ref_audio_path=env.get("YROBOT_TTS_REF_AUDIO_PATH") or None,
            proactive_enabled=proactive,
            ha_enabled=_flag("YROBOT_HA_ENABLED", False, env),
            ha_url=(env.get("YROBOT_HA_URL") or "").rstrip("/") or None,
            ha_token=env.get("YROBOT_HA_TOKEN") or None,
            ha_whitelist_path=(
                env.get("YROBOT_HA_WHITELIST_PATH")
                or "~/.config/yrobot/home_assistant_whitelist.json"
            ),
            hermes_tools_enabled=_flag("YROBOT_HERMES_TOOLS_ENABLED", False, env),
            hermes_tools_url=(
                env.get("YROBOT_HERMES_TOOLS_URL") or "http://192.168.1.200:8766"
            ).rstrip("/"),
            hermes_ios_api_url=(
                env.get("YROBOT_IOS_API_URL") or "http://192.168.1.200:8900"
            ).rstrip("/"),
            local_info_enabled=_flag("YROBOT_LOCAL_INFO_ENABLED", True, env),
            memory_enabled=_flag("YROBOT_MEMORY_ENABLED", True, env),
            memory_path=env.get("YROBOT_MEMORY_PATH") or "~/.config/yrobot/memory.json",
            profile_name=env.get("YROBOT_PROFILE", "default").strip() or "default",
            profile_dir=env.get("YROBOT_PROFILE_DIR", "").strip(),
            conversation_backend=(
                env.get("YROBOT_CONVERSATION_BACKEND", "xiaozhi").strip() or "xiaozhi"
            ),
            qwen_api_key=env.get("DASHSCOPE_API_KEY") or None,
            qwen_url=(env.get("YROBOT_QWEN_URL") or QWEN_REALTIME_URL).strip(),
            qwen_voice=(env.get("YROBOT_QWEN_VOICE") or "Ethan").strip(),
        )
