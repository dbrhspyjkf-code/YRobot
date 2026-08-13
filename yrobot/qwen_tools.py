"""Explicit, local-only Function Calling tools for Qwen Realtime."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yrobot.config import Settings

logger = logging.getLogger(__name__)

VERIFIED_HERMES_BASE = "http://192.168.1.200:8766"
VERIFIED_HERMES_IOS_API = "http://192.168.1.200:8900"   # hermes-mcp-xiaozhi iOS HTTP API
MAX_RESULT_BYTES = 4096
ALLOWED_DOMAINS = frozenset({"light", "switch", "fan", "media_player", "number"})
ALLOWED_ACTIONS = frozenset({"turn_on", "turn_off", "toggle", "oscillate", "media_play", "media_pause", "set_value"})
FOLLOW_UP_CLOSE_WINDOW_S = 30.0
BARE_CLOSE_PHRASES = frozenset({"关", "关闭", "关掉"})
VOLUME_STEP_PERCENT = 10
SONOS_VOLUME_MAX_PERCENT = 70
SONOS_HA_ENTITY_ID = "media_player.ke_ting"
ROBOT_VOLUME_TARGET_PHRASES = (
    "你的音量",
    "你音量",
    "你的声音",
    "你声音",
    "小白音量",
    "小白声音",
    "机器人音量",
    "机器人声音",
    "reachy音量",
    "reachy声音",
)
SONOS_VOLUME_TARGET_PHRASES = ("音响", "音箱", "sonos")
TV_VOLUME_TARGET_PHRASES = ("电视", "电视机")
DEFAULT_SPOKEN_WEATHER_CITY = "深圳"
VOLUME_UP_PHRASES = ("调大", "大一点", "大点", "加大", "加点", "提高", "高一点")
VOLUME_DOWN_PHRASES = ("调小", "小一点", "小点", "减小", "降低", "低一点")
VOLUME_SET_PHRASES = ("调到", "设到", "设置到", "到", "百分之")
BLOCKED_DOMAINS = frozenset({"lock", "cover", "alarm_control_panel", "climate", "water_heater"})
BLOCKED_TERMS = (
    "lock",
    "door",
    "garage",
    "alarm",
    "gas",
    "heater",
    "boiler",
    "stove",
    "oven",
    "锁",
    "门",
    "车库",
    "警报",
    "燃气",
    "煤气",
    "加热",
    "暖气",
    "热水器",
    "炉",
)


@dataclass(frozen=True)
class AllowedAction:
    name: str
    service: str
    entity_id: str
    service_data: dict[str, Any] | None = None


class ToolExecutor:
    """Resolve narrow model arguments through robot-local allowlists."""

    def __init__(
        self,
        settings: Settings,
        *,
        opener: Any = urllib.request.urlopen,
        timeout: float = 5.0,
        volume_controller: Any | None = None,
    ) -> None:
        self.settings = settings
        self._opener = opener
        self._timeout = timeout
        self._volume_controller = volume_controller
        self._actions: dict[tuple[str, str], AllowedAction] = {}
        self._devices: dict[str, AllowedAction] = {}
        self._phrases: dict[str, tuple[str, str]] = {}
        self._blocked_devices: set[str] = set()
        self._last_spoken_device: str | None = None
        self._last_spoken_at = 0.0
        self._load_whitelist()

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "查询指定城市的实时天气（通过 hermes-mcp-xiaozhi）。",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string", "description": "城市名"}},
                        "required": ["city"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_device_state",
                    "description": "读取机器人本地白名单内设备的当前状态。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "device": {"type": "string", "description": "白名单友好名称"}
                        },
                        "required": ["device"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "control_allowed_device",
                    "description": "控制机器人本地白名单内的低风险设备。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "device": {"type": "string", "description": "白名单友好名称"},
                            "action": {
                                "type": "string",
                                "enum": ["turn_on", "turn_off", "toggle"],
                            },
                        },
                        "required": ["device", "action"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_stock_price",
                    "description": "查询股票、ETF、指数的实时价格（A 股/港股/美股）。例如「看看茅台多少钱」「查询 600519」「上证指数」。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "股票名称或代码（如「贵州茅台」「600519」「tsla」），或完整查询语句"},
                        },
                        "required": ["prompt"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_stock_detail",
                    "description": "获取股票详细分析（财务数据/股东/资金流向/基本面）。例如「宁德时代基本面」「比亚迪股东情况」。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "股票名称或代码"},
                        },
                        "required": ["prompt"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_portfolio_stocks",
                    "description": "查询用户自选股列表的当前行情。",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_portfolio_stock",
                    "description": "把一只股票加入自选股列表（按中文名或代码）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "股票名或代码（如「宁德时代」或「300750」）"},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remove_portfolio_stock",
                    "description": "从自选股列表移除一只股票。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "股票名或代码"},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_stock_advice",
                    "description": "对指定股票做操盘建议（基于 AI 智能分析）。例如「茅台现在能买吗」「比亚迪的操盘建议」。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "股票名/代码 + 你的问题"},
                        },
                        "required": ["prompt"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "control_sonos",
                    "description": "控制客厅 Sonos 音响（播放/暂停/停止/下一首/上一首/音量加减/静音/取消静音/设置音量到具体值）。例如「客厅音响播放音乐」「Sonos 暂停」「下一首」「音量加大」「声音小一点」「静音」「客厅音响 60」等。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": "自然语言指令，如「客厅音响播放音乐」「Sonos 暂停」「下一首」「音量加大」「客厅音响 60」",
                            },
                        },
                        "required": ["prompt"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        logger.info("tool call: %s args=%r", name, arguments)
        handlers = {
            "get_weather": self._get_weather,
            "get_device_state": self._get_device_state,
            "control_allowed_device": self._control_device,
            "get_stock_price": self._get_stock_price,
            "get_stock_detail": self._get_stock_detail,
            "get_portfolio_stocks": self._get_portfolio_stocks,
            "add_portfolio_stock": self._add_portfolio_stock,
            "remove_portfolio_stock": self._remove_portfolio_stock,
            "get_stock_advice": self._get_stock_advice,
            "control_sonos": self._control_sonos,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            result = handler(arguments)
        except Exception as exc:
            message = str(exc)
            if self.settings.ha_token:
                message = message.replace(self.settings.ha_token, "[redacted]")
            result = {"ok": False, "error": message or type(exc).__name__}
        return self._bounded(result)

    def execute_spoken_control(
        self, transcript: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        text = self._normalize_phrase(transcript)
        if not text:
            return None
        timestamp = time.monotonic() if now is None else now
        logger.info("spoken control scan: text=%r", text)
        tv_result = self._execute_spoken_tv_volume_control(text)
        if tv_result is not None:
            return tv_result
        sonos_result = self._execute_spoken_sonos_control(text)
        if sonos_result is not None:
            return sonos_result
        stock_result = self._execute_spoken_stock_tool(text)
        if stock_result is not None:
            return stock_result
        weather_result = self._execute_spoken_weather_query(text)
        if weather_result is not None:
            return weather_result
        volume_result = self._execute_spoken_volume_control(text)
        if volume_result is not None:
            return volume_result
        for phrase, (device, action) in sorted(
            self._phrases.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if phrase in text:
                result = self.execute(
                    "control_allowed_device", {"device": device, "action": action}
                )
                if result.get("ok") is True:
                    self._last_spoken_device = device
                    self._last_spoken_at = timestamp
                return result
        if (
            text in BARE_CLOSE_PHRASES
            and self._last_spoken_device is not None
            and timestamp - self._last_spoken_at <= FOLLOW_UP_CLOSE_WINDOW_S
        ):
            result = self.execute(
                "control_allowed_device",
                {"device": self._last_spoken_device, "action": "turn_off"},
            )
            if result.get("ok") is True:
                self._last_spoken_at = timestamp
            return result
        return None


    def _execute_spoken_weather_query(self, text: str) -> dict[str, Any] | None:
        if "天气" not in text or not self.settings.hermes_tools_enabled:
            return None
        city = text
        for token in (
            "今天", "明天", "后天", "现在", "当前", "实时", "查询", "查一下",
            "帮我", "请", "看看", "问一下", "一下", "的", "天气预报", "天气",
            "怎么样", "如何", "好吗", "咋样", "啊", "呀", "呢", "吗", "吧",
        ):
            city = city.replace(token, "")
        city = city.strip()
        if not city:
            city = DEFAULT_SPOKEN_WEATHER_CITY
        result = self.execute("get_weather", {"city": city})
        if result.get("ok") is not True:
            return result
        return {"ok": True, "result": self._format_weather_result(result, city)}

    @staticmethod
    def _format_weather_result(result: dict[str, Any], fallback_city: str) -> str:
        city = str(result.get("city") or fallback_city).strip()
        condition = str(result.get("condition") or "").strip()

        def compact_number(value: Any) -> str:
            text = str(value or "").strip()
            if not text:
                return ""
            try:
                number = float(text)
            except ValueError:
                return text
            if number.is_integer():
                return str(int(number))
            return str(number)

        parts = []
        temp = compact_number(result.get("temp_c"))
        feels_like = compact_number(result.get("feels_like_c"))
        humidity = compact_number(result.get("humidity"))
        wind = compact_number(result.get("wind_kmph"))
        if condition:
            parts.append(condition)
        if temp:
            parts.append(f"{temp}度")
        if feels_like:
            parts.append(f"体感{feels_like}度")
        if humidity:
            parts.append(f"湿度{humidity}%")
        if wind:
            parts.append(f"风速{wind}公里每小时")
        if not parts:
            return f"{city}天气已查到。"
        return f"{city}天气：" + "，".join(parts) + "。"


    def _execute_spoken_stock_tool(self, text: str) -> dict[str, Any] | None:
        portfolio_result = self._execute_spoken_portfolio_stock(text)
        if portfolio_result is not None:
            return portfolio_result
        detail_result = self._execute_spoken_stock_detail(text)
        if detail_result is not None:
            return detail_result
        return self._execute_spoken_stock_price(text)

    def _execute_spoken_portfolio_stock(self, text: str) -> dict[str, Any] | None:
        if not any(word in text for word in ("自选股", "持仓", "我的股票", "股票列表")):
            return None
        if any(word in text for word in ("建议", "操盘", "能买", "能买吗", "能不能买", "可以买", "要不要买")):
            return self.execute("get_stock_advice", {"prompt": "自选股"})
        if any(word in text for word in ("加入", "添加", "加到", "放到")):
            name = self._extract_portfolio_stock_name(text, ("把", "加入", "添加", "加到", "放到", "自选股"))
            if not name:
                return None
            return self.execute("add_portfolio_stock", {"name": name})
        if any(word in text for word in ("删除", "移出", "去掉", "移除")):
            name = self._extract_portfolio_stock_name(text, ("从", "自选股", "删除", "移出", "去掉", "移除"))
            if not name:
                return None
            return self.execute("remove_portfolio_stock", {"name": name})
        return self.execute("get_portfolio_stocks", {})

    def _execute_spoken_stock_detail(self, text: str) -> dict[str, Any] | None:
        if not any(word in text for word in ("详情", "详细", "基本面", "财务", "股东", "资金流", "分析")):
            return None
        prompt = self._extract_stock_code(text) or text
        return self.execute("get_stock_detail", {"prompt": prompt})

    @staticmethod
    def _extract_portfolio_stock_name(text: str, stop_words: tuple[str, ...]) -> str:
        name = text
        for word in stop_words:
            name = name.replace(word, "")
        return name.strip(" ，。,.吗呢吧")

    def _execute_spoken_stock_price(self, text: str) -> dict[str, Any] | None:
        if not any(word in text for word in ("查询", "查", "价格", "股价", "股票")):
            return None
        code = self._extract_stock_code(text)
        if code is None:
            return None
        return self.execute("get_stock_price", {"prompt": code})

    @staticmethod
    def _extract_stock_code(text: str) -> str | None:
        match = re.search(r"\d{6}", text)
        if match:
            return match.group(0)
        digits = {
            "零": "0",
            "〇": "0",
            "幺": "1",
            "一": "1",
            "二": "2",
            "两": "2",
            "三": "3",
            "四": "4",
            "五": "5",
            "六": "6",
            "七": "7",
            "八": "8",
            "九": "9",
        }
        converted = "".join(digits.get(char, " ") for char in text).replace(" ", "")
        match = re.search(r"\d{6}", converted)
        if match:
            return match.group(0)
        match = re.search(r"688\d{2}", converted)
        if match:
            return f"6880{match.group(0)[3:]}"
        return None

    def _execute_spoken_tv_volume_control(self, text: str) -> dict[str, Any] | None:
        if not any(phrase in text for phrase in TV_VOLUME_TARGET_PHRASES):
            return None
        if not self._is_spoken_volume_command(text):
            return None
        return {
            "ok": False,
            "device": "电视音量",
            "error": "电视音量尚未接入本地控制",
        }

    def _execute_spoken_sonos_control(self, text: str) -> dict[str, Any] | None:
        if text == "音量大":
            return self._step_sonos_volume("volume_up", VOLUME_STEP_PERCENT)
        if text == "音量小":
            return self._step_sonos_volume("volume_down", -VOLUME_STEP_PERCENT)
        if not any(phrase in text for phrase in SONOS_VOLUME_TARGET_PHRASES):
            return None
        if not self._is_spoken_volume_command(text):
            return None
        if any(phrase in text for phrase in VOLUME_UP_PHRASES):
            return self._step_sonos_volume("volume_up", VOLUME_STEP_PERCENT)
        if any(phrase in text for phrase in VOLUME_DOWN_PHRASES):
            return self._step_sonos_volume("volume_down", -VOLUME_STEP_PERCENT)
        return self.execute("control_sonos", {"prompt": self._sonos_prompt(text)})

    def _step_sonos_volume(self, action: str, delta: int) -> dict[str, Any]:
        unavailable = self._ha_unavailable()
        if unavailable is not None:
            return unavailable
        state_request = urllib.request.Request(
            f"{self.settings.ha_url}/api/states/{SONOS_HA_ENTITY_ID}",
            method="GET",
            headers=self._ha_headers(),
        )
        state = self._read_json(state_request)
        current_level = ((state.get("attributes") or {}).get("volume_level") if isinstance(state, dict) else None)
        if not isinstance(current_level, int | float):
            return {"ok": False, "error": "Sonos volume state is unavailable"}
        current_percent = round(float(current_level) * 100)
        target_percent = max(0, min(SONOS_VOLUME_MAX_PERCENT, current_percent + delta))
        request = urllib.request.Request(
            f"{self.settings.ha_url}/api/services/media_player/volume_set",
            data=json.dumps(
                {"entity_id": SONOS_HA_ENTITY_ID, "volume_level": target_percent / 100}
            ).encode(),
            method="POST",
            headers=self._ha_headers(),
        )
        with self._opener(request, timeout=self._timeout) as response:
            response.read()
        return {
            "ok": True,
            "device": "音响音量",
            "action": action,
            "volume_percent": target_percent,
        }

    @staticmethod
    def _sonos_prompt(text: str) -> str:
        target = ToolExecutor._extract_spoken_volume_percent(text)
        if (
            target is not None
            and 1 <= target <= 9
            and re.search(r"(音响|音箱).*音量[零〇一二两三四五六七八九]$", text)
        ):
            target *= 10
        if target is None or any(phrase in text for phrase in VOLUME_UP_PHRASES + VOLUME_DOWN_PHRASES):
            return text
        return f"音响音量调到{target}"

    def _execute_spoken_volume_control(self, text: str) -> dict[str, Any] | None:
        if self._volume_controller is None:
            return None
        if any(phrase in text for phrase in TV_VOLUME_TARGET_PHRASES):
            return None
        if not any(phrase in text for phrase in ROBOT_VOLUME_TARGET_PHRASES):
            return None
        if any(phrase in text for phrase in VOLUME_UP_PHRASES):
            action = "volume_up"
            delta = VOLUME_STEP_PERCENT
        elif any(phrase in text for phrase in VOLUME_DOWN_PHRASES):
            action = "volume_down"
            delta = -VOLUME_STEP_PERCENT
        else:
            target = self._extract_spoken_volume_percent(text)
            if target is None:
                return None
            applied = int(self._volume_controller.write_percent(target))
            return {
                "ok": True,
                "device": "音量",
                "action": "volume_set",
                "volume_percent": applied,
            }
        current = int(self._volume_controller.read_percent())
        applied = int(self._volume_controller.write_percent(current + delta))
        return {
            "ok": True,
            "device": "音量",
            "action": action,
            "volume_percent": applied,
        }

    @staticmethod
    def _is_spoken_volume_command(text: str) -> bool:
        return (
            any(phrase in text for phrase in VOLUME_UP_PHRASES)
            or any(phrase in text for phrase in VOLUME_DOWN_PHRASES)
            or ToolExecutor._has_explicit_volume_set(text)
        )

    @staticmethod
    def _has_explicit_volume_set(text: str) -> bool:
        if any(phrase in text for phrase in VOLUME_SET_PHRASES):
            return True
        return re.search(r"(音响|音箱|sonos|你的|小白|机器人|reachy|电视).*音量\d{1,3}$", text) is not None

    @staticmethod
    def _extract_spoken_volume_percent(text: str) -> int | None:
        if not ToolExecutor._has_explicit_volume_set(text):
            return None
        match = re.search(r"(\d{1,3})", text)
        if match:
            value = int(match.group(1))
            return max(0, min(100, value))
        value = ToolExecutor._parse_small_chinese_number(text)
        if value is None:
            return None
        return max(0, min(100, value))

    @staticmethod
    def _parse_small_chinese_number(text: str) -> int | None:
        digits = {
            "零": 0,
            "〇": 0,
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
        }
        if "一百" in text or "百分百" in text or "百分之一百" in text:
            return 100
        match = re.search(r"([零〇一二两三四五六七八九]?十[零〇一二两三四五六七八九]?|[零〇一二两三四五六七八九])", text)
        if match is None:
            return None
        token = match.group(1)
        if "十" not in token:
            return digits.get(token)
        left, _, right = token.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones

    def _load_whitelist(self) -> None:
        source = Path(self.settings.ha_whitelist_path).expanduser()
        if not source.exists():
            return
        document = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(document, list):
            raise ValueError("Home Assistant whitelist must be a list")
        for item in document:
            if not isinstance(item, dict):
                raise ValueError("Home Assistant whitelist entries must be objects")
            name = str(item.get("name") or "").strip()
            service = str(item.get("service") or "").strip()
            entity_id = str(item.get("entity_id") or "").strip()
            if not name or "." not in service or "." not in entity_id:
                continue
            domain, action = service.split(".", 1)
            entity_domain = entity_id.split(".", 1)[0]
            risk_text = f"{name} {service} {entity_id}".casefold()
            blocked = (
                domain in BLOCKED_DOMAINS
                or entity_domain in BLOCKED_DOMAINS
                or any(term in risk_text for term in BLOCKED_TERMS)
            )
            if blocked:
                self._blocked_devices.add(name)
                continue
            if domain not in ALLOWED_DOMAINS or entity_domain != domain:
                continue
            if action not in ALLOWED_ACTIONS:
                continue
            service_data = item.get("service_data")
            if service_data is not None and not isinstance(service_data, dict):
                continue
            allowed = AllowedAction(name, service, entity_id, service_data)
            self._actions[(name, action)] = allowed
            self._devices.setdefault(name, allowed)
            for phrase in self._control_phrases(item, name):
                self._phrases.setdefault(phrase, (name, action))

    def _control_phrases(self, item: dict[str, Any], name: str) -> list[str]:
        phrases = item.get("phrases")
        candidates = [name]
        if isinstance(phrases, list):
            candidates.extend(str(phrase) for phrase in phrases)
        return [
            phrase
            for phrase in (self._normalize_phrase(candidate) for candidate in candidates)
            if phrase
        ]

    @staticmethod
    def _normalize_phrase(value: str) -> str:
        return "".join(char for char in value.casefold() if char.isalnum())

    def _call_hermes_tool(self, name: str, prompt: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """通用：调 hermes-mcp-xiaozhi iOS API (:8900) /api/tools/call.

        所有 hermes 工具都通过这个端点暴露 (port 8900)，body 格式：
          {"name": "工具名", "arguments": {"prompt": "...", **extra}}
        返回 {"ok": true, "result": "人类可读字符串"}.
        """
        if not self.settings.hermes_tools_enabled:
            return {"ok": False, "error": "Hermes tools are disabled"}
        ios_url = self.settings.hermes_ios_api_url.rstrip("/")
        if ios_url != VERIFIED_HERMES_IOS_API:
            return {"ok": False, "error": "Hermes iOS API base is not verified"}
        arguments = {"prompt": prompt}
        if extra:
            arguments.update(extra)
        payload = json.dumps({"name": name, "arguments": arguments}).encode("utf-8")
        logger.info("hermes tool call: %s prompt=%r extra=%s", name, prompt, extra)
        request = urllib.request.Request(
            f"{ios_url}/api/tools/call",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            result = self._read_json(request)
        except Exception as exc:
            logger.warning("hermes tool call %s transport error: %s", name, exc)
            raise
        if not isinstance(result, dict):
            logger.warning("hermes tool call %s returned non-dict: %r", name, result)
            return {"ok": False, "error": "Hermes tool call returned invalid data"}
        if not result.get("ok"):
            logger.warning("hermes tool call %s returned ok=false: %s", name, result.get("error"))
            return {"ok": False, "error": str(result.get("error") or "tool call failed")}
        text = str(result.get("result") or "")
        logger.info("hermes tool call %s -> %d chars", name, len(text))
        return {"ok": True, "result": text}

    def _get_weather(self, arguments: dict[str, Any]) -> dict[str, Any]:
        city = str(arguments.get("city") or "").strip()
        if not city:
            return {"ok": False, "error": "city is required"}
        if self.settings.hermes_tools_url.rstrip("/") != VERIFIED_HERMES_BASE:
            return {"ok": False, "error": "Hermes REST base is not verified"}
        query = urllib.parse.urlencode({"city": city})
        request = urllib.request.Request(f"{VERIFIED_HERMES_BASE}/weather?{query}", method="GET")
        result = self._read_json(request)
        if not isinstance(result, dict):
            return {"ok": False, "error": "weather service returned invalid data"}
        return result

    def _get_stock_price(self, arguments: dict[str, Any]) -> dict[str, Any]:
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt (stock name or code) is required"}
        return self._call_hermes_tool("get_stock_price", prompt)

    def _get_stock_detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        return self._call_hermes_tool("get_stock_detail", prompt)

    def _get_portfolio_stocks(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._call_hermes_tool("get_portfolio_stocks", "")

    def _add_portfolio_stock(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = str(arguments.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        return self._call_hermes_tool("add_portfolio_stock", name)

    def _remove_portfolio_stock(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = str(arguments.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        return self._call_hermes_tool("remove_portfolio_stock", name)

    def _get_stock_advice(self, arguments: dict[str, Any]) -> dict[str, Any]:
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        return self._call_hermes_tool("get_stock_advice", prompt)

    def _control_sonos(self, arguments: dict[str, Any]) -> dict[str, Any]:
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt (Sonos instruction) is required"}
        return self._call_hermes_tool("control_sonos", prompt)

    def _get_device_state(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = str(arguments.get("device") or "").strip()
        blocked = self._blocked_or_missing(device)
        if blocked is not None:
            return blocked
        unavailable = self._ha_unavailable()
        if unavailable is not None:
            return unavailable
        action = self._devices[device]
        request = urllib.request.Request(
            f"{self.settings.ha_url}/api/states/{action.entity_id}",
            method="GET",
            headers=self._ha_headers(),
        )
        result = self._read_json(request)
        state = result.get("state") if isinstance(result, dict) else None
        return {"ok": True, "device": device, "state": state}

    def _control_device(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = str(arguments.get("device") or "").strip()
        requested_action = str(arguments.get("action") or "").strip()
        blocked = self._blocked_or_missing(device)
        if blocked is not None:
            return blocked
        if requested_action not in ALLOWED_ACTIONS:
            return {"ok": False, "error": "action is not allowed"}
        action = self._actions.get((device, requested_action))
        if action is None:
            return {"ok": False, "error": "action is not allowlisted for device"}
        unavailable = self._ha_unavailable()
        if unavailable is not None:
            return unavailable
        domain, service = action.service.split(".", 1)
        payload: dict[str, Any] = {"entity_id": action.entity_id}
        if action.service_data:
            payload.update(action.service_data)
        request = urllib.request.Request(
            f"{self.settings.ha_url}/api/services/{domain}/{service}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers=self._ha_headers(),
        )
        with self._opener(request, timeout=self._timeout) as response:
            response.read()
        return {"ok": True, "device": device, "action": requested_action}

    def _blocked_or_missing(self, device: str) -> dict[str, Any] | None:
        if device in self._blocked_devices:
            return {"ok": False, "error": "device class is blocked"}
        if device not in self._devices:
            return {"ok": False, "error": "device is not allowlisted"}
        return None

    def _ha_unavailable(self) -> dict[str, Any] | None:
        if not (
            self.settings.ha_enabled
            and self.settings.ha_url
            and self.settings.ha_token
        ):
            return {"ok": False, "error": "Home Assistant is not configured"}
        return None

    def _ha_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.ha_token}",
            "Content-Type": "application/json",
        }

    def _read_json(self, request: urllib.request.Request) -> Any:
        with self._opener(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _bounded(result: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) <= MAX_RESULT_BYTES:
            return result
        return {"ok": False, "error": "tool result too large"}
