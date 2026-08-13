# Reachy Mini YRobot Ops Notes

This document records the practical setup, changes, and failure modes for the
Reachy Mini YRobot deployment used in this project.

## Recent Changes (2026-08-11) — QWEN ASR fragment handling

Observed failure: the user said `音响音量调大一下`, but QWEN ASR first emitted
only `音响音`. YRobot treated that fragment as a complete unmatched command,
cancelled the active response, and injected a no-match failure. That could
interrupt the later ASR fragment, so the robot never saw the real command.

Fix:

1. Recent ASR fragment aggregation now uses a 3.0 s window.
2. A spoken-control no-match is silent and only logs candidates; it no longer
   injects `我没听清` immediately.
3. When a local tool does execute, YRobot still injects the real tool result
   back to QWEN so the model cannot invent success or failure.
4. `timed out during opening handshake` is treated as reconnectable for QWEN,
   preventing transient provider handshakes from pushing the app into safe
   mode.

Verification:

- Production focused tests passed for handshake-timeout reconnect, unmatched
  fragment no-op, longer ASR joining, active-response reconnect, wake resume
  after reconnect, and `音响音量调大一下` speaker-volume up.
- Live restart must load `/home/pollen/.config/yrobot/ha.env`; starting only
  `/home/pollen/YRobot/.venv/bin/python -u -m yrobot.main` falls back to
  XIAOZHI because QWEN/HA settings are in `ha.env`.
- Current accepted live state after restart: `runtime.backend=qwen`,
  `runtime.ws_state=connected`, mic input enabled, Home Assistant configured,
  and the official daemon still running.

## Recent Changes (2026-08-08 / 2026-08-09) — Stability & Startup

### Startup guard chain

After a power cycle or daemon restart the Reachy daemon WebSocket handler
takes longer to initialise than its HTTP API. The old `wait-for-daemon.conf`
grep'd for `"running"` in the HTTP response — too early.

Fix:

1. `scripts/wait_daemon_ready.py`, deployed to
   `/home/pollen/wait_daemon_ready.py` — SDK-level probe that creates a
   `ReachyMini`, enables motors if disabled, and exits 0 on success.
2. `/etc/systemd/system/yrobot.service.d/wait-for-daemon.conf` —
   `ExecStartPre` calls this script, giving the daemon up to 30 s.

The script exits the success path with `os._exit(0)`. This avoids a startup
stall where the SDK/GStreamer worker threads keep Python alive after the
daemon probe already succeeded, leaving `yrobot.service` stuck in
`activating (start-pre)` until systemd times it out.

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

### Smooth startup motion handoff

**Symptom:** after power-on the head jumped upward too quickly before the
robot settled into normal idle motion.

Root cause: YRobot enabled motors and immediately started the 50 Hz
`Choreographer`. The first `set_target()` frame was based on the internal
idle pose, not the real pose the robot was physically holding after sleep or
power loss.

Fix:

1. `yrobot/main.py` snapshots `get_current_head_pose()` and
   `get_current_joint_positions()` before motor enable.
2. `yrobot/motion.py: Choreographer` accepts `startup_head_pose` and
   `startup_antennas`.
3. The first 4 s of motion blend from the captured real pose to the composed
   idle pose with smoothstep interpolation.
4. Normal gaze speed is still restored after the existing 8 s startup window.

Verification on hardware:

- service restart and full `sudo reboot` both came back automatically;
- logs showed `captured startup pose for smooth motor handoff`,
  `motors enabled (attempt 1)`, and `head rise complete`;
- user observed the head now rises slowly.

### Power health / undervoltage

**Symptom:** robot froze briefly, then behaved like it had restarted.

Evidence:

- Current boot started at `2026-08-09 09:40:49`.
- `yrobot.service` and `reachy-mini-daemon.service` had `NRestarts=0`, so this
  was not an application-level restart.
- Kernel log showed `Undervoltage detected!` followed by `Voltage normalised`.
- `vcgencmd get_throttled` returned `0x50000`: current voltage was normal, but
  undervoltage/throttling had happened since boot.

Mitigation:

- use a stable high-current 5 V supply and low-resistance cable;
- avoid marginal USB power ports or loose connectors;
- Dashboard `/api/status` now includes `system.power` from
  `vcgencmd get_throttled`;
- Dashboard displays `电源正常`, `电源异常`, or `曾低电压`.

Interpretation of the current power flags:

- `under_voltage` / `throttled`: problem is happening now;
- `under_voltage_seen` / `throttled_seen`: it happened sometime since boot;
- `raw=0x50000`: no current low voltage, but low voltage and throttling were
  seen earlier in this boot.

### Xiaozhi reconnect logging

Xiaozhi WebSocket can close with code `1005` and then reconnect successfully.
This used to produce a large Traceback even though the recovery path was
working.

Fix:

- close codes `1000`, `1001`, and `1005`, plus `ConnectionClosedOK`, are now
  treated as expected reconnect events;
- YRobot logs a concise warning and increments reconnects;
- unexpected exceptions still use `logger.exception()` and keep the full
  Traceback.

This makes real failures easier to see in `/api/logs` and `journalctl`.

### Xiaozhi pause when mic upload is disabled

The Dashboard MIC button controls whether microphone audio is uploaded to the
conversation backend. When MIC upload is disabled, YRobot now pauses the
Xiaozhi WebSocket session instead of keeping an idle connection open. This
avoids Xiaozhi's ~70 s idle close cycle (`received 1005`) and the repeated
3 s reconnect loop.

Expected states:

- MIC enabled: `/api/status` shows `runtime.ws_state=connected`.
- MIC disabled: `/api/status` shows `runtime.ws_state=paused` and
  `audio.input_enabled=false`.
- Re-enabling MIC lets the outer loop reconnect Xiaozhi automatically.

### IK errors from official recorded emotions

**Symptom:** during conversation the robot froze, then made a sudden movement,
then recovered.

Evidence from `journalctl`:

- YRobot logged automatic emotions such as
  `xz emotion happy -> recorded cheerful1` and
  `xz emotion winking -> recorded welcoming1`.
- Within the same seconds, the daemon logged:
  `IK error: WARNING: Collision detected or head pose not achievable!`

Root cause: automatic Xiaozhi emotions were using the official recorded-emotion
library. Some recorded moves can request poses that are unreachable or collide
with the current composed YRobot posture on this robot. The daemon rejects IK,
which looks like a short freeze followed by a catch-up movement.

Fix:

- automatic Xiaozhi emotions no longer call recorded emotions;
- automatic emotions now use only bounded programmatic moves from
  `EMOTION_FALLBACK_MOVE` (`nod`, `tilt`, `surprise`, etc.);
- manual Dashboard/API motion still keeps access to recorded moves and dances.

Operational rule:

Do not re-enable official recorded moves for **automatic** model emotions unless
they are first filtered through a reachability/safety layer. If the daemon logs
`Collision detected or head pose not achievable`, check for a nearby
`xz emotion ... -> recorded ...` line before looking elsewhere.

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

### Speaker tracking: audio + vision

YRobot uses the XVF3800 DoA angle as the fast speaker signal and the camera
face detector as a stabilizer. The camera does not replace audio blindly:
only a recent, consecutive face detection is fused with the audio target.

Current behavior:

- `yrobot.motion.SoundCompass` samples DoA only while the app believes the
  user is speaking.
- `yrobot/main.py` keeps audio as the fallback target.
- When vision is available and within the accepted audio/visual delta,
  `YROBOT_HEAD_TRACKING_WEIGHT` controls how strongly the fused target moves
  toward the face. Default: `0.7`.
- Dashboard `/api/status` exposes `motion.tracking` with
  `audio_yaw_rad`, `visual_yaw_rad`, `target_yaw_rad`, `source`,
  `face_detected`, and `age_s`.
- Dashboard shows a `追踪` card so field testing can tell whether bad facing
  came from audio DoA, missing face detection, or final target fusion.

Operational note: if Dashboard shows `source=声音` and `未看到脸`, the camera
is not helping; check the camera preview first. A frame with hands, monitors,
or books but no frontal face will often leave tracking audio-only.

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

The official GPIO shutdown service only calls `sudo shutdown -h now`. YRobot
adds a systemd override for `gpio-shutdown-daemon.service` so the physical
button first posts to the official daemon sleep endpoint:

```text
/api/daemon/stop?goto_sleep=true
```

The override runs:

```text
/home/pollen/YRobot/scripts/gpio_graceful_shutdown_monitor.py
```

Dashboard Power Off / Reboot also requests the same sleep endpoint before the
system power command. If the sleep request fails, shutdown still proceeds so the
button cannot get stuck.

After reboot, verify:

```bash
systemctl is-active reachy-mini-daemon.service
systemctl is-active yrobot.service
systemctl cat gpio-shutdown-daemon.service
curl http://127.0.0.1:8000/api/motors/status | python3 -m json.tool
```

## 2026-08-11 QWEN active-response safe-mode recovery

The operator reported logs showed an error and Reachy could not converse.
Evidence showed the official daemon, media, and motors were healthy, but YRobot
was in `safe_mode` with QWEN `ws_state=error` and
`last_error='Conversation already has an active response'`.

Journal context:

- `23:31:32` QWEN STT recognized `你叫小白。` and detected the wake word.
- The QWEN WebSocket closed normally with code `1000`.
- QWEN then emitted `Conversation already has an active response`.
- YRobot counted this as a startup failure and reached `8/3`, so it refused to
  retry until service restart.

Recovery:

```bash
sudo systemctl reset-failed yrobot.service
sudo systemctl restart yrobot.service
```

Persistent fix:

- Added a regression test for active-response reconnect classification.
- Added `conversation already has an active response` to the QWEN reconnectable
  error classifier.
- Production commit: `fde99ce`.
- Feature worktree commit: `645f2ea`.

Verification:

- Targeted reconnect tests passed:
  `test_qwen_active_response_error_is_reconnectable`,
  `test_qwen_idle_timeout_is_reconnectable`, and
  `test_qwen_internal_service_error_is_reconnectable`.
- After restart, YRobot reported `active`, QWEN `connected`, `last_error=null`,
  audio input enabled, official daemon `running`, and motors `enabled`.
- The broader current suite still has unrelated drift failures in VAD default,
  expanded tool schema, and prompt wording; do not treat those as regressions
  from this one-line reconnect fix.

## 2026-08-11 Local speaker volume voice control

The operator reported that `音箱音量调大` did not work. Evidence showed QWEN ASR
recognized variants such as `音响大一点`, `音响一点`, `音响`, and `音响音`, but no
local spoken-control action executed. The dashboard `/api/volume` endpoint
already worked and reported volume `88%`.

Implemented volume voice control as a local-only `ToolExecutor` branch with an
injected `VolumeController`. It recognizes specific up/down phrases for
`音量`/`声音`/`音响`/`音箱` and adjusts volume by 10 percentage points. It does
not expose a new QWEN remote tool schema and does not use Home Assistant.
Ambiguous `音响` alone is ignored.

Verification:

- Regression tests for volume up, volume down, and ambiguous speaker phrase
  passed with related QWEN/tool tests.
- Production commit: `18ab39d`.
- Feature worktree commit: `4c66fb9`.
- After restart, local executor validation for `音箱音量调大` changed volume from
  `88` to `98`.
- YRobot reported active, QWEN connected, audio input enabled, and
  `last_error=null`.

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


## 2026-08-12 Sonos volume step control

Request: treat `音响音量调大` / `音响音量调小` as deterministic Sonos volume controls.

Implemented in `yrobot/qwen_tools.py`:

- Sonos volume up/down now uses Home Assistant directly, not Hermes natural-language guessing.
- Entity: `media_player.ke_ting`.
- Step size: 10 percentage points.
- Maximum Sonos volume: 70%.
- `音响音量调大` reads current HA `volume_level`, adds 10, clamps to 70, then calls `media_player.volume_set`.
- `音响音量调小` reads current HA `volume_level`, subtracts 10, clamps to 0, then calls `media_player.volume_set`.
- Explicit numeric Sonos commands like `音响音量二` / `音响音量二十` still route through the existing normalized `control_sonos` path.
- TV volume remains explicitly unsupported; robot/ALSA volume remains separate.

Validation:

```bash
.venv/bin/python -m pytest   tests/test_qwen_tools.py::test_spoken_control_sonos_volume_up_steps_by_10_and_caps_at_70   tests/test_qwen_tools.py::test_spoken_control_sonos_volume_down_steps_by_10   tests/test_qwen_tools.py::test_spoken_control_routes_sonos_volume_away_from_robot_speaker   tests/test_qwen_tools.py::test_spoken_control_normalizes_terse_sonos_volume_number   tests/test_qwen_tools.py::test_spoken_control_treats_truncated_sonos_volume_digit_as_tens   tests/test_qwen_tools.py::test_spoken_control_returns_failure_for_unconnected_tv_volume -q
```

Result: 6 passed.

Live checks:

- HA reported `media_player.ke_ting` volume 20% before direct test.
- Direct local executor call `音响音量调大` changed Sonos volume from 20% to 30%.
- After YRobot restart, direct local executor call `音响音量调小` changed Sonos volume from 30% to 20%.
- Runtime after restart: pid `5335`, backend `qwen`, WebSocket `connected`, mic available, motion ready.


## 2026-08-12 ASR/Sonos volume rollback note

Observation after live testing:

- QWEN ASR still often truncates `音响音量调大/调小` to fragments such as `音响`, `音响音`, `音响音量`, `调小`, or `声音小`.
- Executing Sonos volume based on assistant output text was unsafe. Examples included QWEN asking `还想再调大一点吗？` while the local recovery still executed `音响音量调大`.
- This caused inconsistent user-visible behavior and extra `Conversation already has an active response` reconnects.

Current safety decision:

- Do not execute Home Assistant/Sonos controls based on QWEN assistant replies.
- Keep direct user-ASR execution only.
- Keep narrow context recovery only for user ASR: if recent user ASR was an incomplete Sonos fragment and the next user ASR is `调大/调小/声音大/声音小`, map it to `音响音量调大/调小`.
- Incomplete Sonos fragments such as `音响音量` now return no local execution instead of injecting a failure into QWEN, reducing response races.

Validation:

```bash
.venv/bin/python -m pytest   tests/test_qwen_runtime.py::test_qwen_assistant_sonos_step_never_executes_from_model_reply   tests/test_qwen_runtime.py::test_qwen_contextual_sonos_step_recovers_standalone_direction   tests/test_qwen_runtime.py::test_qwen_contextual_sonos_step_requires_sonos_context   tests/test_qwen_tools.py::test_spoken_control_sonos_volume_up_steps_by_10_and_caps_at_70   tests/test_qwen_tools.py::test_spoken_control_sonos_volume_down_steps_by_10 -q
```

Result: 5 passed.

Runtime after restart: pid `6786`, backend `qwen`, WebSocket `connected`, mic available, motion ready, `last_error=None`.


## 2026-08-12 Command recognizer interface for ASR separation

Decision:

- QWEN realtime remains the conversation backend.
- Home/Sonos commands should not depend on QWEN realtime ASR for final authority.
- Added a separate external command recognizer client that can receive the captured utterance WAV and return a safe, allowlisted command text.
- No command recognizer service is currently exposed by `192.168.1.200:8900` or `192.168.1.200:8766`; the YRobot-side client is therefore default-disabled.

New configuration:

```bash
YROBOT_COMMAND_RECOGNIZER_ENABLED=1
YROBOT_COMMAND_RECOGNIZER_URL=http://192.168.1.200:8910/recognize
```

Expected external recognizer API:

- Request: `POST /recognize`
- Body: `audio/wav`, 16 kHz mono int16 WAV bytes captured by Reachy/YRobot.
- Response example:

```json
{"ok": true, "command_text": "音响音量调大"}
```

Safety behavior:

- If disabled, missing, unreachable, or response is not allowlisted, YRobot executes nothing.
- Current allowlist starts with:
  - `音响音量调大`
  - `音响音量调小`
- Recognized command text is routed through the existing `ToolExecutor.execute_spoken_control()` path, so Sonos still uses the deterministic HA step logic: step 10%, cap 70%.
- QWEN assistant replies are not used to execute controls.

Validation:

```bash
.venv/bin/python -m py_compile yrobot/config.py yrobot/command_recognizer.py yrobot/main.py yrobot/qwen_tools.py
.venv/bin/python -m pytest   tests/test_config.py::test_command_recognizer_defaults_disabled   tests/test_config.py::test_command_recognizer_env_overrides   tests/test_command_recognizer.py   tests/test_qwen_runtime.py::test_qwen_assistant_sonos_step_never_executes_from_model_reply   tests/test_qwen_runtime.py::test_qwen_contextual_sonos_step_recovers_standalone_direction   tests/test_qwen_runtime.py::test_qwen_contextual_sonos_step_requires_sonos_context   tests/test_qwen_tools.py::test_spoken_control_sonos_volume_up_steps_by_10_and_caps_at_70   tests/test_qwen_tools.py::test_spoken_control_sonos_volume_down_steps_by_10 -q
```

Result: py_compile succeeded; 11 focused tests passed.

Runtime after restart: pid `7583`, backend `qwen`, WebSocket `connected`, mic available, motion ready, `last_error=None`.

Next required work:

- Deploy a recognizer service on `192.168.1.200:8910` or another LAN host.
- Test it against captured samples in `/tmp/yrobot-asr-debug/asr-*.wav`.
- Enable the two env vars only after the service reliably returns allowlisted command text.

## 2026-08-12 10:20 CST - Sonos volume ASR safety fix

- Evidence: with TV off, QWEN still truncated `音响音量调大` to fragments such as `音响。` / `音量，音量。`; a complaint sentence `我说音响，然后他一直调。` was locally mis-parsed as `音响音量调到1` because the Chinese numeral `一` in `一直` was treated as a volume percentage.
- Fix: Sonos/robot/TV volume set now requires an explicit volume-set form (`调到` / `设置到` / `百分之` / trailing Arabic number). Bare Chinese numerals inside normal sentences no longer trigger volume setting.
- Fix: after recent Sonos-volume context, narrow ASR homophones `搅拌` / `交大` are accepted as follow-up `调大`; this is context-gated and does not apply globally.
- Safety: unmatched volume fragments inject a clarification that no local control was executed, preventing QWEN from claiming volume changed without a tool result.
- Recovery: Sonos `media_player.ke_ting` was restored to 20% and unmuted after the accidental 1% set.
## 2026-08-12 11:05 CST - Local ASR/KWS offline validation on OrangePi

Goal: evaluate whether a larger/local pipeline can replace `Reachy mic -> QWEN ASR -> YRobot local rules` for Sonos volume commands before enabling any production control path.

Scope: offline-only tests on `192.168.1.200`; no OrangePi recognizer service was enabled and YRobot production control path was not changed.

Findings:

- sherpa-onnx Chinese/English KWS with custom keywords is very fast (~0.07-0.08s per captured sample) and can sometimes detect the target phrase fragments (`音响` / `音箱` / `音响音量`).
- The same KWS model did not reliably detect the action words `调大` / `调小` from the existing Reachy failure samples, so it is not safe for one-shot volume execution.
- sherpa-onnx Moonshine Chinese quantized ASR is also fast (~0.08-0.26s per sample), but transcripts on the same samples were not reliable enough to recover `调大` / `调小` and included obvious misrecognitions/hallucinations.
- Audio evidence suggests many captured samples contain only about 0.5-0.7s of active speech, often ending around 2.1s in a ~3.5s file. This points to capture/VAD/front-end loss of the command tail, not just an ASR model choice problem.

Decision:

- Do not deploy local ASR as an automatic Sonos-volume executor yet.
- Recommended next safe path is target-detection plus explicit confirmation, or fixing the capture/microphone path first (external mic / mic array / push-to-talk) before retrying command-intent execution.
## 2026-08-12 11:16 CST - Minimal Sonos volume short commands

Change: added exact short spoken commands `音量大` and `音量小` as direct Sonos volume controls.

Behavior:

- `音量大` -> `media_player.ke_ting` volume +10%, capped at 70%.
- `音量小` -> `media_player.ke_ting` volume -10%, floored at 0%.
- No new recognizer service was enabled.
- TV volume and Reachy/ALSA volume routing were not expanded.

Validation:

```bash
.venv/bin/python -m py_compile yrobot/qwen_tools.py
.venv/bin/python -m pytest tests/test_qwen_tools.py::test_spoken_control_sonos_volume_up_steps_by_10_and_caps_at_70 tests/test_qwen_tools.py::test_spoken_control_sonos_volume_down_steps_by_10 tests/test_qwen_tools.py::test_spoken_control_sonos_short_volume_up_steps_by_10 tests/test_qwen_tools.py::test_spoken_control_sonos_short_volume_down_steps_by_10 tests/test_qwen_tools.py::test_spoken_control_routes_sonos_volume_away_from_robot_speaker tests/test_qwen_tools.py::test_spoken_control_can_raise_robot_speaker_volume tests/test_qwen_tools.py::test_spoken_control_sonos_ignores_complaint_with_chinese_one tests/test_qwen_tools.py::test_spoken_control_sonos_allows_explicit_numeric_set -q
```

Result: py_compile succeeded; 8 focused tests passed. YRobot restarted with copied environment; runtime pid `11863`, backend `qwen`, WebSocket `connected`, `last_error=None`.
## 2026-08-12 11:40 CST - Local spoken stock price routing

Change: added a local read-only spoken route for stock price queries before falling back to QWEN tool selection.

Behavior:

- Phrases containing query/price terms plus a 6-digit stock code call existing Hermes `get_stock_price`.
- Spoken digit forms such as `六八八零幺八` are normalized to `688018`.
- No new service was added; it reuses `hermes-mcp-xiaozhi` on `192.168.1.200:8900`.
- Scope is price lookup only; no trading/advice automation was added.

Validation:

```bash
.venv/bin/python -m py_compile yrobot/qwen_tools.py
.venv/bin/python -m pytest tests/test_qwen_tools.py::test_spoken_control_routes_spoken_stock_code_to_hermes_price tests/test_qwen_tools.py::test_spoken_control_routes_arabic_stock_code_to_hermes_price tests/test_qwen_tools.py::test_spoken_control_sonos_short_volume_up_steps_by_10 tests/test_qwen_tools.py::test_spoken_control_sonos_short_volume_down_steps_by_10 tests/test_qwen_tools.py::test_spoken_control_sonos_ignores_complaint_with_chinese_one -q
```

Result: py_compile succeeded; 5 focused tests passed. Direct runtime-env call for `帮我查询六八八零幺八的价格` returned Hermes stock price for `688018`. YRobot restarted as pid `13245`, backend `qwen`, WebSocket `connected`, `last_error=None`.
## 2026-08-12 11:55 CST - Wake latency and active-response guard

Symptoms:

- User reported `你好小白` wake sometimes reacted slowly or not at all.
- Logs showed many empty QWEN ASR completions after VAD was lowered to `0.025`, plus recurring `Conversation already has an active response`.
- WakeGate itself matches `你好，小白。`; the issue was not the wake phrase list.

Change:

- Raised VAD threshold from `0.025` to `0.030` RMS to reduce empty/noise turns while staying below the earlier too-high `0.041`.
- `QwenRealtimeClient.request_response()` now cancels a known active response before creating a normal new response.
- Tool-call follow-up responses explicitly skip that cancel so stock/home/Sonos tool result replies are not broken.

Validation:

```bash
.venv/bin/python -m py_compile yrobot/qwen_realtime.py yrobot/main.py yrobot/audio_runtime.py
.venv/bin/python -m pytest tests/test_qwen_realtime.py::test_request_response_cancels_active_response_first tests/test_qwen_realtime.py::test_function_call_followup_does_not_cancel_tool_response tests/test_qwen_realtime.py::test_speech_started_cancels_response_and_flushes_playback tests/test_qwen_realtime.py::test_function_call_done_executes_and_writes_result tests/test_qwen_runtime.py::test_qwen_wake_list_contains_nihao_xiaobai tests/test_qwen_runtime.py::test_qwen_wake_expires_after_sixty_seconds -q
```

Result: py_compile succeeded; 6 focused tests passed. YRobot restarted as pid `13855`, backend `qwen`, WebSocket `connected`, `last_error=None`, VAD `0.030`.
## 2026-08-12 12:05 CST - Stock code dropped-zero ASR repair

Symptom: QWEN ASR heard `查询六八八幺八价格` for the intended `查询 688018 价格`, so local stock routing did not match and the fallback response was affected by an active-response state.

Change: in stock-price query context only, a 5-digit spoken/converted code matching `688xx` is normalized to `6880xx`. This repairs the observed dropped-zero case `六八八幺八` -> `688018` without widening general device control.

Validation:

```bash
.venv/bin/python -m py_compile yrobot/qwen_tools.py yrobot/qwen_realtime.py
.venv/bin/python -m pytest tests/test_qwen_tools.py::test_spoken_control_recovers_dropped_zero_in_688_stock_code tests/test_qwen_tools.py::test_spoken_control_routes_spoken_stock_code_to_hermes_price tests/test_qwen_tools.py::test_spoken_control_routes_arabic_stock_code_to_hermes_price -q
```

Result: py_compile succeeded; 3 focused tests passed. Direct runtime-env call for `查询六八八幺八价格` returned Hermes stock price for `688018`. YRobot restarted as pid `14287`, backend `qwen`, WebSocket `connected`, `last_error=None`.



### 2026-08-12 12:15 - Stock price narration must use exact local tool result

Symptom: user asked for 688018 price; Hermes returned the correct local result text, but QWEN replied with an invented price such as 3.25. Root cause: YRobot injected only a generic success message for local spoken-control results; stock results do not have device/action fields, so the model saw a vague success instead of the exact quote text.

Change: added _qwen_spoken_control_result_feedback() and route local spoken-control results through it. When a tool result contains a final result string, QWEN now receives that exact sentence and an instruction not to change numbers, codes, or units. Device-control success/failure feedback remains unchanged in behavior.

Verification: py_compile passed for yrobot/main.py, yrobot/qwen_tools.py, yrobot/qwen_realtime.py; targeted pytest passed for QWEN feedback and stock spoken-control routing. Runtime restarted as QWEN on port 8042 with official daemon on 8000 still available.


Follow-up: verified the old inline local spoken-control feedback block was still present after the first edit; replaced it with _qwen_spoken_control_result_feedback(result), reran py_compile and focused runtime-feedback tests, then restarted YRobot as pid 15409. Current status: backend qwen, WebSocket connected, last_error None, mic available, official daemon firmware 1.9.0 available. Log check also showed the 12:12-12:13 stock-query attempts were outside wake-active state, so they produced QWEN STT lines but no local spoken-control/Hermes call.


### 2026-08-12 12:25 - Stock quote result must bypass main conversational generation

Evidence: after wake, user said the full query for 688018. Logs showed local spoken control called Hermes get_stock_price with prompt 688018 and Hermes returned `乐鑫科技 当前价格 114.64 元，上涨 0.17 元，涨幅 0.15%`; QWEN still replied with a different generated quote (`24.35`, `0.29`, `1.21%`). This proves prompt-only exact-result injection is not reliable for numeric data.

Change: local spoken-control results with an ok `result` string now first use an isolated short QWEN realtime session only for exact text-to-speech playback, keeping the main conversation context out of stock quote narration. If exact TTS fails, fallback no longer passes the quote text to the main model; it only says that the quote was fetched but exact voice playback failed, and explicitly forbids providing price/code numbers.

Verification: py_compile passed for yrobot/main.py; focused pytest passed for exact-result detection, stock result feedback, and spoken stock-code routing. YRobot restarted as pid 16249 with backend qwen, WebSocket connected, last_error None, mic available, and official daemon firmware 1.9.0 available.


### 2026-08-12 12:45 - Local stock tool routing beyond price

Change: expanded YRobot local spoken stock routing so more Hermes stock tools use the same exact-TTS result path instead of QWEN main conversation summarization.

Enabled local routes:

- 我的自选股 / 查询自选股 / 持仓 -> get_portfolio_stocks
- 查询自选股建议 / 持仓建议 -> get_stock_advice with prompt 自选股
- 把 XX 加入自选股 -> add_portfolio_stock
- 从自选股删除/移出 XX -> remove_portfolio_stock
- 查询 688018 基本面/详情/财务/股东/资金流/分析 -> get_stock_detail

Safety decision: do not locally route single-stock phrases like 688018 能不能买 to get_stock_advice, because Hermes currently returns the whole portfolio advice for that tool even when a single code/name is passed. This avoids speaking a misleading all-portfolio answer for a single-stock question.

Verification: py_compile passed for yrobot/qwen_tools.py and yrobot/main.py; 10 focused pytest cases passed for price, portfolio, add/remove, portfolio advice, detail, and exact-result handling. Live read-only executor checks returned expected portfolio quotes, portfolio advice, and 688018 detail; single-stock advice returned no local route by design. YRobot restarted as pid 17983, backend qwen, WebSocket connected, mic available, official daemon firmware 1.9.0 available.


### 2026-08-12 13:05 - Normal QWEN questions after local-command miss

Symptom: after wake, ASR heard "今天深圳怎么样？" but there was no assistant reply. Logs showed local spoken control had no match, but because an ASR debug WAV existed, YRobot called the optional command recognizer and treated that path as matched even when it returned no command. That suppressed the normal QWEN response.create call.

Change: command recognizer now returns a boolean. Only a real recognized command suppresses the normal QWEN answer; if both local spoken control and command recognizer miss, YRobot calls client.request_response() so ordinary questions are answered.

Verification: py_compile passed for yrobot/main.py; focused tests passed for the no-local/no-command response decision, active-response reconnect detection, and request_response cancellation. YRobot restarted as pid 18573; status is backend qwen, WebSocket connected, last_error None, tts inactive, audio_queue 0, mic available, official daemon firmware 1.9.0 available.


## 2026-08-12 14:15 - QWEN tool follow-up active-response race

Symptom: user said "今天深圳的天气怎么样？". ASR was correct and the get_weather tool was called, but the spoken reply became the default greeting. Logs also showed a stale get_stock_price tool call followed by get_weather, then "Conversation already has an active response"; reconnect lost the weather follow-up.

Change: in yrobot/qwen_realtime.py, if the tool-result follow-up response.create is rejected because QWEN still has an active response, defer exactly one follow-up until response.done clears the active response. This keeps the tool output in conversation and avoids a reconnect/safe-mode path.

Verification: added test_function_call_followup_retries_after_active_response_race; py_compile yrobot/qwen_realtime.py yrobot/main.py passed; tests/test_qwen_realtime.py and focused QWEN runtime reconnect/no-local-match tests passed. Full runtime test still has an unrelated environment-dependent VAD default assertion: current env returns 0.11 vs historical 0.065.


## 2026-08-12 14:45 - Persona identity changed to XiaoBai

Change: default YRobot persona now identifies as "小白" instead of "Reachy". Wake words remain unchanged; the assistant should answer as 小白 and no longer introduce itself as Reachy or mention hardware identity by default.

Verification: added test_default_persona_identifies_as_xiaobai_not_reachy; py_compile passed for yrobot/config.py, yrobot/qwen_realtime.py, and yrobot/main.py; focused config/QWEN wake/session tests passed.


## 2026-08-12 14:50 - Local deterministic weather spoken route

Symptom: ASR correctly heard "今天深圳天气怎么样啊？", but QWEN did not call get_weather and replied by repeating/rewriting the question. Root cause: weather depended on model-selected function calling after local spoken-control miss.

Change: YRobot spoken control now directly routes phrases containing "天气" to the verified Hermes REST weather endpoint when Hermes tools are enabled. It extracts the city locally, formats the structured weather response into an exact result sentence, and uses the existing exact-TTS path instead of letting the main model rewrite it.

Verification: added test_spoken_control_routes_weather_query_to_exact_result; py_compile passed for yrobot/qwen_tools.py and yrobot/main.py; focused weather/stock/exact-result tests passed; live read-only ToolExecutor check for "今天深圳天气怎么样啊？" returned an exact Shenzhen weather sentence.


## 2026-08-12 14:58 - Runtime persona override migration to XiaoBai

Symptom: after changing the default persona to XiaoBai, QWEN could still introduce itself as Reachy. Root cause: local runtime .env and historical dashboard settings still contained an older YROBOT_PERSONA/persona value with "你是 Reachy", overriding the new default at startup.

Change: build_system_prompt now narrowly migrates old persona text from "你是 Reachy"/"你是Reachy" to "你是小白" for all persona sources. The robot-local .env and /home/pollen/.config/yrobot/settings.json were also updated in place so the current runtime source no longer contains the old identity.

Verification: added test_env_persona_migrates_old_reachy_identity_to_xiaobai; py_compile passed for yrobot/config.py and yrobot/main.py; focused persona/QWEN session tests passed; dotenv-based prompt check shows XiaoBai true and Reachy identity false.


## 2026-08-13 11:45 - Restore QWEN wake to cloud-ASR baseline

Symptom: in QWEN mode, saying "你好小白" no longer reliably woke the robot. Logs showed repeated empty QWEN ASR transcripts and no "QWEN wake word detected" entries after the local sherpa-onnx KWS wake gate was enabled by default.

Root cause: the new experimental local KWS path was enabled by default, but it did not produce runtime wake hits on the deployed robot. The previous stable path relied on QWEN ASR audio upload with a low VAD threshold and WakeGate transcript matching.

Change: disabled local KWS by default again; it remains opt-in via YROBOT_WAKE_ENABLED=1. Current robot config keeps YROBOT_VAD_RMS_MIN at 0.015 so normal speech uploads to QWEN for cloud-ASR wake matching.

Verification: py_compile passed for yrobot/config.py, yrobot/main.py, and yrobot/audio_runtime.py; focused wake/config tests passed. Runtime config check showed wake_enabled False, wake_phrase "你好小白", vad_rms_min 0.015, backend qwen.


## 2026-08-13 11:50 - Add narrow QWEN wake ASR alias

Symptom: after restoring cloud-ASR wake, a real spoken "你好小白" still did not open the gate.

Evidence: QWEN was connected and receiving audio, but recent ASR logs showed the wake utterance as "明白。" or empty text instead of "你好小白" / "小白".

Change: added strict wake aliases "明白" and "你好明白" for the observed "小白" ASR confusion. These aliases are exact after punctuation/space cleanup; longer phrases such as "我明白了" do not wake the robot.


## 2026-08-13 11:58 - Widen QWEN pre-wake end silence

Symptom: after the strict alias patch, repeated "你好小白" still did not wake reliably.

Evidence: post-patch logs showed QWEN receiving pre-wake audio but completing ASR as empty text, "Yes, sir.", or "你好。" instead of the full wake phrase. This points to pre-wake turn segmentation cutting the utterance before "小白" reached the ASR result.

Change: widened only the pre-wake QWEN manual-turn end-silence window from 8 frames to 24 frames (about 160 ms to 480 ms). The active command window remains 16 frames. This avoids making plain "你好" a wake word while giving QWEN enough trailing audio to hear "小白".


## 2026-08-13 12:05 - Pin QWEN ASR transcription language to Chinese

Symptom: after widening the pre-wake audio window, repeated "你好小白" still did not wake.

Evidence: QWEN was connected and receiving audio, but the post-fix ASR result for the wake attempt was "Up." or empty text. This indicates automatic language detection was still unstable for the short Chinese wake phrase.

Change: set QWEN `input_audio_transcription` to `{"model": "qwen3-asr-flash-realtime", "language": "zh"}` for the main realtime session and dashboard voice-test session. This keeps the spoken wake phrase in the Chinese ASR path instead of letting short audio be misclassified as English.


## 2026-08-13 12:10 - Add strict aliases for latest Chinese wake ASR errors

Symptom: after pinning QWEN ASR to Chinese, "你好小白" still did not reliably wake.

Evidence: new QWEN ASR logs no longer returned English, but still recognized the wake attempt as "喂。" or "你说，你咋？"; an earlier same-session attempt was "你老来。". The bare "喂" is too broad and must not be a wake word.

Change: added exact compact aliases "你老来" and "你说你咋" to the WakeGate observed-ASR alias list. Kept "喂" excluded to avoid accidental wake from normal room speech.


## 2026-08-13 12:15 - Two-stage QWEN wake for split ASR results

Symptom: after adding strict aliases, the next wake attempts were still not accepted.

Evidence: logs showed QWEN ASR splitting or misreading the wake phrase into separate short results: "你好。", then "好嘞。", then "对。". None alone is safe enough to become a wake word.

Change: WakeGate now treats "你好" only as a short pending prefix, not a wake. If one of the observed XiaoBai tail/suffix confusions ("好嘞", "对", "明白", "你老来", "你说你咋") arrives within 3 seconds, the gate opens. A bare "你好", "好嘞", or "对" alone still does not wake the robot.


## 2026-08-13 12:20 - Pair-specific wake for latest split ASR

Symptom: the next post-patch wake attempt still did not open the gate.

Evidence: logs showed the attempt as "你把。" followed by "行。". Neither fragment alone is safe as a wake word.

Change: expanded the two-stage WakeGate to remember the observed prefix. "你把" followed by "行" within 8 seconds now wakes the robot, but "行" alone and "你好" followed by "行" do not. The previous "你好" prefix path remains limited to the earlier observed XiaoBai suffix confusions.


## 2026-08-13 12:25 - Accept bare QWEN ASR "你好" as wake

Symptom: after pair-specific wake handling, the latest wake attempt was still not accepted.

Evidence: logs showed the active process recognized the spoken "你好小白" attempt as only "你好。"; no second suffix fragment followed. An earlier attempt did wake through "嘿嘿。", proving the gate/action path itself works.

Change: accept exact compact "你好" as a wake alias in QWEN mode because current ASR often drops the "小白" tail entirely. Kept broad suffixes such as "行" non-wake unless paired with their specific observed prefix.


## 2026-08-13 12:35 - Default bare spoken weather to Shenzhen

Symptom: after wake, user said "今天深圳的天气怎么样", but QWEN answered an unrelated visual question.

Evidence: logs showed QWEN ASR recognized the utterance as only "天气。". Local spoken weather routing saw no city after cleanup, returned no match, and the query fell through to the main model.

Change: if spoken control contains "天气" but no city remains after cleanup, default the local weather route to 深圳. This keeps ASR-truncated weather requests on the deterministic Hermes exact-TTS path instead of asking QWEN to infer intent.


## 2026-08-13 12:45 - Add latest strict QWEN wake alias

Symptom: after accepting exact "你好" as a wake alias, another spoken "你好小白" attempt still did not wake.

Evidence: QWEN ASR recognized recent wake attempts as "哎，等会儿。", "对。", "你等会儿。", or empty text. "哎，等会儿" is too broad for wake, but "你等会儿" is a repeated close misrecognition of the wake phrase.

Change: added exact compact "你等会儿" as a wake alias. Also clear stale QWEN `last_error` when the realtime WebSocket reports `connected`, so recovered idle reconnects do not remain visible as current Dashboard errors.


## 2026-08-13 13:00 - Keep vision out of pre-wake ASR

Symptom: user observed that QWEN wake was normal earlier, but degraded after vision recognition was added.

Evidence: git comparison against the pre-vision stable baseline showed commit `8b4733e` began calling `_drain_camera()` from both the active and pre-wake audio upload branches; later face recognition also called `_drain_face()` and could re-emit `session.update` from the same paths. Current runtime had `YROBOT_SEND_VIDEO=1`. Logs also showed weather questions falling through to visual answers after ASR truncation.

Change: keep QWEN vision input and face-based session updates out of the pre-wake branch. The robot now sends only audio before wake; camera frames and face prompts are sent only after WakeGate is active. This preserves vision after wake while reducing interference with wake ASR.


## 2026-08-13 13:08 - Debounce QWEN face speaker updates

Symptom: after wake recovered, ASR and replies were still unstable in normal conversation.

Evidence: logs showed repeated `QWEN session.update re-emitted` events toggling between speaker `阿皮` and `None` during one active wake window. This can perturb the realtime session while the user is speaking. Logs also showed `天气吧。` was routed as city `吧`.

Change: face speaker session updates now require the same recognition result to remain stable for 3 seconds before re-emitting `session.update`. The spoken weather cleaner now removes trailing `吧`, so ASR-truncated `天气吧` defaults to 深圳 instead of querying city `吧`.
