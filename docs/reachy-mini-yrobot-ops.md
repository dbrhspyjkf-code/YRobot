# Reachy Mini YRobot Ops Notes

This document records the practical setup, changes, and failure modes for the
Reachy Mini YRobot deployment used in this project.

## Recent Changes (2026-08-08 / 2026-08-09) — Stability & Startup

### Startup guard chain

After a power cycle or daemon restart the Reachy daemon WebSocket handler
takes longer to initialise than its HTTP API. The old `wait-for-daemon.conf`
grep'd for `"running"` in the HTTP response — too early.

Fix:

1. `/home/pollen/wait_daemon_ready.py` — SDK-level probe that creates a
   `ReachyMini`, enables motors if disabled, and exits 0 on success.
2. `/etc/systemd/system/yrobot.service.d/wait-for-daemon.conf` —
   `ExecStartPre` calls this script, giving the daemon up to 30 s.

The script is deployed to Reachy but NOT tracked in the YRobot repo;
its content is identical to the one in the plan doc at
`docs/plans/2026-08-08-yrobot-stability-improvements.md`.

### Motor enable after goto_sleep / power cycle

After `goto_sleep()` or a hard power-cycle, the daemon boots with
`motor_control_mode: Disabled` and `backend_status.ready: false`. The
`ready` flag **never** becomes `true` without SDK intervention, so
waiting for it is pointless. The `wait_daemon_ready.py` script enables
motors before exiting; YRobot's own `enable_motors()` retry loop (3
tries × 1 s) handles the rest.

### Hardware: camera I2C bus lockup

**Symptom:** Reachy boots, head moves briefly, then hangs completely.
`dmesg` shows dozens of `dw9807 I2C write … fail ret = -5` errors.
The `dw9807` is the camera focus-motor chip; its I2C failures can lock
up the kernel bus, preventing the hardware watchdog from being pinged.
After 1 minute the BCM2835 watchdog hard-resets the board — previous-boot
journal is empty (no clean shutdown).

**Mitigation:** disconnect the camera flex cable from the CM4 board if
hang-on-boot is observed. Voice conversation (Xiaozhi) works without the
camera; only face tracking is lost.

### Stability improvements (commits 64916b7 … 42ecfd1)

- Slow-rise startup: 0.3 rad/s gaze for 8 s after motor enable, then
  restored to 2.5 rad/s. Reduces peak current draw.
- Motor enable failure → safe-mode (3 retries, 1 s apart).
- Startup failure counter: only cleared on successful `run()` return;
  consecutive failures persist across systemd restarts.
- VAD persistence: atomic per-key env update (`yrobot/env_store.py`),
  preserves HA token and other settings.
- Audio: bounded latest-frame queue (max 50), TTS watchdog covers
  startup-stall, mid-stream disconnect, and total-duration limits.
- aplay process handle tracked for clean terminate/kill on shutdown.
- Xiaozhi receive task supervised: premature exit triggers reconnect;
  cancel + await on close.
- Choreographer external inputs (mode, moves, gaze, stillness) now go
  through a command queue consumed by the motion thread.
- Dashboard `/api/status` exposes motion loop Hz, tick time,
  deadline misses, WS state, session id, reconnects, TTS packet
  timing, audio queue depth/drops.
- Health monitor checks Dashboard API, daemon API, and uses
  bounded subprocess timeouts.
- Obsolete MiniCPM-o/Hermes tests removed; 46 current tests pass.

### Xiaozhi configuration

Production configuration is read from environment variables, falling back
to the wired defaults:

```bash
XIAOZHI_CONV_URL  → default wss://api.tenclass.net/xiaozhi/v1/
XIAOZHI_TOKEN     → default test-token
XIAOZHI_DEVICE_ID → default wlan0 MAC
```

Set these in `/etc/systemd/system/yrobot.service.d/ha.conf` or
`/home/pollen/.config/yrobot/ha.env` for production tokens.


## Current Deployment

- Robot SSH: `pollen@192.168.1.14`
- Robot project path: `/home/pollen/YRobot`
- Local mirror path: `/Users/leenzhou/Projects/YRobot-reachy-current`
- Git branch: `codex/home-assistant-control`
- YRobot service: `yrobot.service`
- Reachy daemon service: `reachy-mini-daemon.service`
- Dashboard: `http://192.168.1.14:8042`
- Reachy daemon API: `http://192.168.1.14:8000`
- Home Assistant: `http://192.168.1.133:8123`
- Hermes tools: `http://192.168.1.200:8766`

Do not print or commit secrets. The Home Assistant token is loaded from
`/home/pollen/.config/yrobot/ha.env`.

## Recent Changes (2026-08-05 / 2026-08-06)

### Stock query flow (Hermes)

YRobot routes stock-related queries to Hermes tools via regex matching on the
model's streamed text. The pipeline is:

1. `main.py: _on_delta` accumulates a `caption` from text fragments.
2. `hermes_tools.py: HermesToolsController.handle_text` matches each tool by:
   - `ToolDef.phrases` — substring check on the caption.
   - `ToolDef.extra_matches` — code-aware: 6-digit stock code + price keyword
     triggers `stock_price`; code + advice keyword triggers `stock_advice`.
3. `ToolDef.skip_when` blocks the tool when intent is wrong (e.g. portfolio
   intent missing for `stocks`).
4. `_extract_stock_name(normalized)` extracts the stock code or company name.
   Strategy: 6-digit code → return it directly. Otherwise strip a fixed
   stop-word vocabulary, find all `[\u4e00-\u9fff]{2,8}` runs, drop trailing
   `股票` noun and leading `买` verb, reject portfolio placeholders, return
   the longest remaining run.
5. `_chinese_digit_code(text)` converts Chinese digit readings
   (`六零零六零零` → `600600`) so hermes-mcp gets the exact code even when
   the gateway emits the spoken-form.

### Hermes tool routing rules

| User says | Tool | Notes |
|---|---|---|
| `688018的价格`、`六零零六零零`、`比亚迪怎么样` | `stock_price` / `stock_advice` | Code-aware match |
| `我的股票`、`自选股`、`持仓`、`持有的股票` | `stocks` (portfolio) | Requires portfolio intent |
| `天气`、`广州天气` | `weather` | Substring match |
| `汇率`、`美元换人民币` | `rate` | |
| `DeepSeek余额` | `deepseek_balance` | Exact phrase |

Bare `股票` does NOT fire any tool — the model often writes `想查询的股票`
while clarifying a single-stock query and matching that fires the wrong tool.
Portfolio must use `_is_portfolio_intent(text)` which checks for explicit
portfolio phrases.

### Per-tool cooldowns

- Hermes tools: `stock_price`/`stock_advice` 3 s (rapid multi-stock queries
  allowed); portfolio 30 s default (full list, expensive).
- Home Assistant: 5 s per `(entity_id, service)`. Earlier 30 s per `entity_id`
  blocked normal toggle (e.g. user opens a light then closes it 9 s later).

### HA self-loop defence

Two layers, both required:

1. **User-voice gate** in `main.py: _on_delta` — only run HA / Hermes /
   `local_info` handlers if `_last_user_onset_at` was within the last 15 s.
   This prevents the model's own words from triggering HA actions when wake
   was caused by environmental noise or echo.
2. **HA response suppression** — any successful HA action adds the current
   `response_id` to `_muted_response_ids` so subsequent model text for the
   same response is not spoken. This prevents the model from continuing to
   describe the action it already executed (which used to loop).

### Uplink silence gate

`main.py` now keeps audio uplink live only while conversation is active:

- Pause when 15 s of mutual silence (no user voice + no gateway audio).
- Instant resume on any `_confirmed_user_active(now)`.

Earlier wake-word gating required 2–3 s of sustained speech which was
unreliable; the silence gate removes that friction while still breaking the
echo loop. It does NOT stop a runaway speaker; for that, watch the journal
for `first accepted audio` gaps or manually restart the service.

### Profiles (P1)

Tool whitelist and instructions override are now profile-driven.

- Profiles ship in `yrobot/profiles/`: `default`, `family_safe`, `developer`.
- `tools.txt` — allow-list, one tool name per line; `#` for comments; empty
  file = allow all.
- `instructions.txt` — text appended to the system prompt.
- `YROBOT_PROFILE=name` selects a profile (default `default`).
- `YROBOT_PROFILE_DIR=/path/to/profiles` overrides the shipped location.
- User-level override dir takes precedence over the shipped one.

Resolution helper: `yrobot.profile.load_profile(name, override_dir=...)`.
Tests: `tests/test_profile.py` (9 cases).

### Hermes dry-run probe (P2)

`yrobot.hermes_tools.validate_tools(client, enabled_tools, timeout)` validates
Hermes tools at startup. It prefers the real `/api/discover` endpoint and falls
back to dry-run probes:

1. Fetch `GET /api/discover` (server's self-reported tool list).
2. For each `ToolDef` with a `discover_name`, cross-check it appears in the
   discover index — missing entry ⇒ the client expects an endpoint the server
   does not register (typo / version drift) ⇒ `FAIL` with a clear message.
3. Tools without `discover_name` (or when discover is unavailable) fall back
   to dry-run probes against hardcoded sentinel URLs.

Called from `Yrobot.__init__` via `_probe_hermes_tools`; per-tool result logged
at INFO or WARNING. Failures do NOT block startup — they're for visibility only.

`/api/discover` is served by **hermes-mcp-xiaozhi** on `192.168.1.200:8766`
(`hermes_mcp_server/main.py`, `handle_discover`). It lists 7 tools:
`weather`, `rate`, `stocks_portfolio`, `stock_price`, `stock_advice`,
`deepseek_balance`, `health`. **When adding a new tool to hermes-mcp, remember
to add it to the discover payload too** — YRobot uses it to cross-validate.

The hermes-mcp local checkout at `/Users/leenzhou/hermes-mcp-xiaozhi` is NOT a
git repo on the 200 machine (deployed via copy). The deployed copy is newer
and has `handle_stocks_price` / `handle_stocks_advice` that local once lacked;
sync from deployed → local before editing (`scp root@192.168.1.200:...`, SSH
password `orangepi`).

### Model behavior contracts (system prompt)

The system prompt now lists every Hermes tool with the trigger phrase it
expects, e.g.:

- 股票价格 — must contain `股价` / `价格` / `行情`
- 股票分析建议 — must contain `怎么样` / `建议`
- 6 位代码 — repeat verbatim (Arabic digits, NOT `六零零六零零` Chinese reading)
- HA 控制 — only when user explicitly asks; do not echo `好的，X已关闭`

### Dashboard additions

- VAD RMS threshold slider (`/api/audio/vad`) — live updates `YROBOT_VAD_RMS_MIN`
  in `ha.env`. Persisted value at last deployment: `0.081`.
- Mic input enable/disable — runtime-only (defaults to enabled after restart).
- Volume slider / mute / mic level meter.
- Log panel with auto-scroll, pause, level filter, jump-to-bottom.
- Camera preview toggle (default off — saves resources).
- Profile selector in the settings form (03B / PROFILE panel).

### Robot states (P1 observability + deep sleep)

`yrobot.state.ROBOT_STATE` is a thread-safe singleton read by `/api/status`:

- `active` — uplink live, head moving, speaker ready.
- `sleeping` — silence gate paused (15 s mutual silence); presence detector
  runs at 1 Hz.
- `deep_sleep` — after 30 s of *confirmed absence* the head freezes
  (`choreo.set_still(now+3600)`); dashboard shows the state.
- `safe_mode` — startup failed ≥3 times; dashboard stays up, conversation
  does not run.

Wake paths from sleeping/deep_sleep: user voice, wake word, or a detected
face (Haar cascade) → `release_still()` → `active`.

Caveat: Haar cascade recognizes frontal faces only. If the robot faces a
person's back/side, presence may not trigger — a motion-delta fallback could
be added later.

### Fail-safe startup (P0)

- systemd drop-in `startup-guard.conf`: `StartLimitBurst=5` /
  `StartLimitIntervalSec=120` caps restart storms.
- `Yrobot.run()` wraps critical init; on failure increments a persisted
  counter in `/tmp/.yrobot_startup_failures`. After 3 consecutive failures it
  enters `_enter_safe_mode` (dashboard up, conversation not started).
- A successful start clears the counter, so a single transient crash won't
  lock the robot into safe mode.

### Presence detector

`yrobot/presence.py` — `PresenceDetector` polls a frame provider (wired to
`LatestCamera.take_latest`) at 1 Hz, runs OpenCV's bundled Haar cascade on
320×240 grayscale, exposes hysteresis state (3 frames). Starts lazily the
first time the silence gate goes to sleep; the moment a face is seen the
uplink resumes.

### Stock name extraction (2026-08-06 afternoon)

**Root fix: match known stock names first.** Model paraphrases are infinite
("你问比亚迪怎么样", "查询比亚迪的最新行情信息。目前"), so enumerating stop
words never keeps up. `_extract_stock_name` now:

1. 6-digit code (Arabic or Chinese-digit `六零零六零零` → `600600`).
2. Longest name found in `KNOWN_STOCK_NAMES` — the user's 11-stock portfolio
   + ~50 famous A-shares (hermes `_resolve_secid.known` + large caps).
3. Stop-word strip + longest-Han-run fallback.

To add a stock the user may ask about, append one name to
`KNOWN_STOCK_NAMES` in `yrobot/hermes_tools.py` — no new stop words needed.

### Generic Hermes tools via the 8900 ios-api bridge

hermes-mcp exposes 24 MCP tools; YRobot previously used only the 7 HTTP REST
endpoints on **8766**. The remaining tools are dispatched by name through the
**8900 ios-api bridge** (`POST /api/tools/call {name, arguments:{prompt}}`):

- hermes-mcp `_ios_api.py` `_dispatch` was backfilled to cover all 24 tools;
  `GET /api/tools` lists them (used for discovery/validation).
- YRobot `HermesToolsClient.call_tool(name, prompt)` POSTs to
  `ios_api_url` (default `http://192.168.1.200:8900`, env `YROBOT_IOS_API_URL`).
- `ToolDef.discover_source` distinguishes `hermes` (8766 discover) from
  `ios_api` (8900 `/api/tools`).
- Reachy enables 12 tools: weather, stock_price, stock_advice,
  stocks_advice_all, stocks, rate, deepseek_balance, cctv_news, web_search,
  stock_detail, margin_data, ipo_info.
- Deliberately excluded: add_note, add_reminder, send_email, taobao_orders,
  chat_history (user decision).

### Routing rules that matter

- `stock_advice` fires only on explicit advice intent (建议/操盘/点评/能买).
  Vague "怎么样/如何/分析" routes to `stock_detail` (covers non-portfolio
  stocks like 比亚迪).
- `margin_data` requires a stock code/name in the caption — a bare
  "融资融券" echo does not fire (hermes would return encyclopedia text).
- `stock_detail` / `stock_advice` / `margin_data` pass the *extracted* stock
  name/code to hermes, not the raw caption.

### Rate formatting (2026-08-06 afternoon)

`_format_rate` filters by the currencies the user asked about:

- 美元兑人民币 → only CNY
- 美元兑日元 → only JPY
- 汇率 (no currency named) → all five rates

`ToolDef.format` now receives the normalized user text as a second argument;
all format functions accept an optional `_text`.

### Margin data precise query (2026-08-06 afternoon, hermes side)

hermes `get_margin_data` previously used MX news search → broad sector lists
("科创板股融资融券余额每日变动") burying the target stock. It now queries
EastMoney datacenter `RPTA_WEB_RZRQ_GGMX` first when a 6-digit code is
present:

> 乐鑫科技（688018）2026-08-05融资融券，融资余额7.28亿，融券余额588.42万，融资融券合计7.34亿。

Falls back to MX search on failure.

### hermes-mcp sync reminder

- Local dev repo: `/Users/leenzhou/hermes-mcp-xiaozhi` (git).
- Deployed copy: `/home/orangepi/hermes-mcp-xiaozhi` on 192.168.1.200 (NOT a
  git repo — deployed by `scp`).
- The deployed copy was once NEWER than local (had `handle_stocks_price` /
  `handle_stocks_advice`); sync deployed → local before editing, then scp back.
- SSH to 200: `sshpass -p orangepi ssh root@192.168.1.200`.
- Restart: `systemctl restart hermes-mcp-xiaozhi.service`.
- After editing hermes, re-verify both ports: `8766 /api/discover` and
  `8900 /api/tools`.

### Xiaozhi 云对话接入（2026-08-06 下午，WIP）

**目的**：让 Reachy 用小智云的 ASR/LLM/TTS 获得更好的对话质量。

**激活流程**（scripts/xiaozhi_activate.py）：

1. POST https://api.tenclass.net/xiaozhi/ota/ 带 Device-Id (MAC 地址) +
   ESP32 风格 system-info JSON body。
2. 服务器返回 6 位激活码 → 用户去 xiaozhi.me 输入绑定。
3. 绑定后轮询 OTA 直到拿到真实 wsURL (wss://api.tenclass.net/xiaozhi/v1/)。

**WebSocket 连接**：
- URL: `wss://api.tenclass.net/xiaozhi/v1/`
- 认证头: `Authorization: Bearer test-token`, `Device-Id: {wlan0 MAC}`,
  `Protocol-Version: 1`
- 协议: xiaozhi hello/listen/stt/llm/tts, 录制 16kHz→Opus, 播放 24kHz Opus

**脚本**: scripts/xiaozhi_test.py — 独立测试，先停 YRobot 再跑。

**当前状态**：WebSocket 连接、协议交换、STT、LLM 回复和长句 TTS 播放均已验证。

**TTS 实现与排查（2026-08-07）**：
- 下行 Opus 参数必须采用 server hello 协商的 `sample_rate` 和 `frame_duration`。
- aplay 写入必须在独立线程中进行，不能阻塞 WebSocket 接收协程。
- 收到 `tts.start` 后禁止新的 `listen.start` 和上行音频；仅 `tts.stop` 才结束 speaking。`sentence_end` 是句间事件，不能视为整段 TTS 结束。
- 麦克风读取使用 `asyncio.to_thread()`，每个上行音频包发送后让出事件循环；否则下行音频会积压，长句可能中断，TCP 会表现为 `CLOSE-WAIT`。
- 验证日志：在 `xz tts start` 和 `xz tts stop` 之间应持续出现 `xz audio packets=...`，期间不应有 `xz sent ...`。

小智云不自带视觉；视觉通过 MCP 工具 analyze_image 调外部 VL 模型
(Qwen-VL/GPT-4o/MiniCPM-o)。待用户选定 VL 模型后在 hermes-mcp 加该工具。

## Architecture

YRobot runs as a Reachy Mini app and talks to three external systems:

- MiniCPM-o realtime gateway for voice/video conversation.
- Home Assistant for local device control through a whitelist.
- Hermes HTTP tools for local utility calls such as DeepSeek balance.

The Reachy Mini daemon owns the SDK WebSocket, motors, camera, audio backend, and
media pipeline. YRobot sits above it and owns conversation lifecycle, VAD, tool
routing, Dashboard controls, speaker interruption, and session rotation.

## Runtime Configuration

Important config files on the robot:

- `/home/pollen/.config/yrobot/ha.env`
- `/home/pollen/.config/yrobot/settings.json`
- `/home/pollen/.config/yrobot/home_assistant_whitelist.json`
- `/home/pollen/.config/yrobot/memory.json`

Important environment values:

```bash
YROBOT_VAD_AGGRESSIVENESS=3
YROBOT_VAD_RMS_MIN=0.081
YROBOT_HA_ENABLED=1
YROBOT_HERMES_TOOLS_ENABLED=1
```

Dashboard settings in `settings.json` are lower priority than daemon/process
environment variables. If a setting appears in `environment_overrides`, changing
it on the Dashboard will not survive a restart unless the env file is changed.

## Common Checks

Robot services:

```bash
systemctl is-active reachy-mini-daemon.service
systemctl is-active yrobot.service
systemctl --no-pager --full status yrobot.service
```

Dashboard status:

```bash
curl http://127.0.0.1:8042/api/status | python3 -m json.tool
curl http://127.0.0.1:8042/api/audio/vad | python3 -m json.tool
curl http://127.0.0.1:8042/api/audio/input | python3 -m json.tool
```

Motors and head pose:

```bash
curl http://127.0.0.1:8000/api/motors/status | python3 -m json.tool
curl http://127.0.0.1:8000/api/state/present_head_pose | python3 -m json.tool
```

Recent logs:

```bash
journalctl -u yrobot.service --since "10 minutes ago" --no-pager
journalctl -u reachy-mini-daemon.service --since "10 minutes ago" --no-pager
```

Useful filtered log view:

```bash
journalctl -u yrobot.service --since "10 minutes ago" --no-pager \
  | grep -E "robot:|Home Assistant action|first accepted audio|session .* ready|silence gate|wake|motor|Traceback|ERROR|Exception"
```

## Changes Made

### Dashboard Audio Controls

The Dashboard now exposes:

- Speaker volume control.
- Mic level meter.
- Mic input enable/disable button.
- VAD RMS threshold slider.

Mic disable is runtime-only. It keeps the mic meter visible but stops microphone
audio from being uploaded to the realtime model. After a restart it defaults to
enabled so the robot does not stay permanently deaf by accident.

### VAD Sensitivity

The VAD slider controls `YROBOT_VAD_RMS_MIN`.

Higher value means less sensitive. Lower value means more sensitive.

Suggested ranges:

- Quiet room: `0.035` to `0.050`
- Normal home: `0.055` to `0.075`
- TV, people talking, or noise: `0.080` to `0.120`

The current persisted value is `0.081`. Dashboard changes are written back to
`/home/pollen/.config/yrobot/ha.env` and also affect the running detector.

### Home Assistant Control

Home Assistant actions are whitelist-based. Do not expose arbitrary HA service
calls to the model.

Important rules:

- Whitelist exact phrases for each safe action.
- Keep locks, alarm panels, garage doors, gas, heaters, and other high-risk
  devices out of the first version.
- Device actions such as "关灯睡觉" should execute silently or with one short
  response, not a device-by-device speech report.
- For the Xiaomi TV switch, remember the logic is inverted:
  `switch.xiaomi_esprh1_0bc4_is_on` on means TV off, off means TV on.

### HA Self-Loop Fix

Observed failure:

- The model said variants of "好的，我明白。厨房灯已关闭。关掉厨房灯..." for a long
  time without new user speech.
- Logs showed one Home Assistant action succeeded, then the same model response
  kept producing text.
- The text itself matched the HA command phrase again, creating a loop.

Fix:

- Any successful Home Assistant action now suppresses the current model response.
- Suppression does not depend on whether the whitelist action has a `response`.
- Suppressed model text is not added to conversation memory.
- Speaker output is interrupted for the suppressed response.

When this bug appears again, look for:

```text
robot: ...
Home Assistant action succeeded: ...
robot: ...
robot: ...
```

If there are repeated `robot:` lines without new `first accepted audio`, it is a
model/tool loop, not a user speaking repeatedly.

### Hermes Tools

Hermes runs separately on `192.168.1.200:8766`.

Example:

```bash
curl http://192.168.1.200:8766/api/deepseek/balance
```

YRobot has a Hermes controller that can trigger local tools such as DeepSeek
balance. Tool-triggered model responses are suppressed so the model does not
continue speaking or write tool prompt text into local memory.

### Local Memory

Local memory is stored in:

```bash
/home/pollen/.config/yrobot/memory.json
```

Memory acknowledgements should be handled locally. Tool-triggered or suppressed
model text must not be appended to memory, because it can be re-injected after
session rotation and cause repeated speech.

### Startup Motors and Head Wake-Up

After a power-button shutdown and boot, the Reachy daemon may detect all motors
but leave motor mode disabled:

```json
{"mode": "disabled"}
```

Symptoms:

- Head stays down in the sleep/off pose.
- `wake_up()` or `goto_target()` may return without actually moving the head.

Fix:

- YRobot startup now calls `enable_motors()` before checking sleep pose or
  running `wake_up()`.

Manual recovery:

```bash
curl -X POST http://127.0.0.1:8000/api/motors/set_mode/enabled
curl http://127.0.0.1:8000/api/motors/status | python3 -m json.tool
```

Then restart YRobot if needed:

```bash
sudo systemctl restart yrobot.service
```

Expected YRobot startup log:

```text
motors enabled for startup wake-up
head not in sleep pose; skipping wake-up
```

or, if the head is down:

```text
motors enabled for startup wake-up
head in sleep pose; running wake-up movement
```

### Power Button

Short-press power shutdown is acceptable for normal use. Avoid long-press forced
power cuts while writing configuration, updating code, committing, pushing, or
while services are still starting.

After reboot, verify:

```bash
systemctl is-active reachy-mini-daemon.service
systemctl is-active yrobot.service
curl http://127.0.0.1:8000/api/motors/status | python3 -m json.tool
```

## MCP Notes

The Hugging Face article about adding MCP tools targets the official
`reachy-mini-conversation-app` profile/tool-spaces mechanism.

This YRobot deployment does not directly use that official profile system. It
already has its own Home Assistant, Hermes, local-info, and memory controllers.

Recommended path:

- Use MCP for low-risk information tools such as weather, search, news, exchange
  rates, or status lookup.
- Keep Home Assistant actions local and whitelist-based.
- Add a YRobot MCP bridge/controller if MCP tools are needed.
- Do not hand arbitrary appliance control to a public remote MCP tool.

## Git Workflow

Main local repo:

```bash
cd /Users/leenzhou/Projects/YRobot-reachy-current
git status --short --branch
git log --oneline -5
```

Robot repo:

```bash
ssh pollen@192.168.1.14
cd /home/pollen/YRobot
git status --short
git log --oneline -5
```

The robot currently has two untracked files that were intentionally not committed:

- `yrobot/audio.py.bak`
- `yrobot/wakeword.py`

Do not add these unless wake-word work is intentionally restarted.

## Validation Before Claiming a Fix

For code changes, run focused checks first:

```bash
.venv/bin/python -m py_compile \
    yrobot/main.py yrobot/audio.py yrobot/app_config.py \
    yrobot/hermes_tools.py yrobot/home_assistant.py \
    yrobot/profile.py yrobot/config.py
.venv/bin/python -m pytest tests/test_main.py::test_home_assistant_action_without_response_suppresses_model_loop -q
.venv/bin/python -m pytest tests/test_main.py::test_startup_wake_up_enables_motors_before_movement -q
.venv/bin/python -m pytest tests/test_hermes_tools.py -k "extract_stock_name or stock_code or bare_stock or portfolio or stock_advice_also or validate_tools or model_paraphrase" -q
.venv/bin/python -m pytest tests/test_profile.py -q
```

Smoke-test the Hermes probe against the live server:

```bash
ssh pollen@192.168.1.14
journalctl -u yrobot.service --since "1 minute ago" --no-pager \
    | grep -E "Hermes tool|UNREACHABLE"
```

Every `Hermes tool <name> reachable` line means the probe passed for that
tool. `UNREACHABLE` lines indicate the endpoint URL or the server is broken.

Full `pytest` and `ruff check .` have had unrelated failures from in-progress
local changes. Treat focused hardware-relevant checks as the immediate gate, and
only clean full-suite issues when intentionally doing repo cleanup.

