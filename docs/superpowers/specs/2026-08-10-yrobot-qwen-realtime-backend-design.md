# YRobot Qwen Realtime Backend Design

## Goal

Add a mutually exclusive `XIAOZHI` / `QWEN` conversation-backend selector to the
existing YRobot dashboard. Keep the official Reachy Mini daemon unchanged. The
QWEN option uses `qwen3.5-omni-flash-realtime`, supports spoken interruption and
multilingual conversation, and exposes only explicitly approved local tools.

## Confirmed constraints

- The robot remains at `192.168.1.14`; the production checkout is
  `/home/pollen/YRobot` and runs as `yrobot.service`.
- The official Reachy Mini daemon continues to own motor, camera and media
  hardware access.
- Exactly one conversation backend runs at a time.
- Existing XIAOZHI behavior and the explicit `你好小白` wake phrase remain intact.
- QWEN never silently falls back to XIAOZHI. A failed QWEN connection remains a
  visible QWEN error and the operator may deliberately switch back.
- The QWEN model is fixed to `qwen3.5-omni-flash-realtime`.
- Credentials stay in the robot-local systemd environment file and are never
  returned by dashboard APIs or written into browser storage.
- Existing uncommitted work and backup files in the production checkout must be
  preserved. Implementation commits stage only files belonging to this feature.

## Current code context

The active YRobot implementation is a single `ReachyMiniApp` process. The
dashboard routes are in `yrobot/app_config.py`, static UI in
`yrobot/static/index.html`, `yrobot/static/main.js`, and
`yrobot/static/style.css`, environment parsing in `yrobot/config.py`, and the
current XIAOZHI runtime in `yrobot/main.py`. The service imports
`/home/pollen/.config/yrobot/ha.env`.

The production tree is intentionally dirty and contains a substantial in-flight
restructure. Several previously tracked tool modules are deleted while their
replacements are not yet committed. The QWEN implementation must build on the
working-tree versions and must not restore deleted modules wholesale.

## Architecture

### Backend selection

Persist one value in the existing environment file:

```text
YROBOT_CONVERSATION_BACKEND=xiaozhi|qwen
```

`xiaozhi` remains the default when the value is absent. A new dashboard endpoint
validates and saves this exact two-value enum through the existing
`update_env_value()` helper. Saving returns `restart_required: true`; the UI uses
the existing YRobot restart action only after an explicit user click.

On startup, `Yrobot.run()` reads `Settings.conversation_backend` once and calls
either the existing XIAOZHI runtime or the new QWEN runtime. Restart-based
switching is deliberate: it guarantees the old WebSocket, microphone stream,
playback queue, camera worker, compass, and motion threads are closed before the
new backend starts.

### QWEN transport

Create `yrobot/qwen_realtime.py` for the QWEN WebSocket protocol and event state
machine. Use the already-installed `websockets` dependency rather than adding
the DashScope SDK. Configuration is:

```text
model=qwen3.5-omni-flash-realtime
endpoint=wss://dashscope.aliyuncs.com/api-ws/v1/realtime
credential=DASHSCOPE_API_KEY
input audio=mono PCM16 at 16 kHz
output audio=mono PCM16 at 24 kHz
```

The module owns only cloud protocol state. Reachy hardware, motion, microphone
capture, playback, dashboard meters, and shutdown ownership remain in
`yrobot/main.py`. The QWEN client accepts callbacks for output PCM, transcript,
interruption, tool execution, status changes, and logging. It exposes a blocking
`run(stop_event)` entry point consistent with the current app lifecycle.

The initial `session.update` sets the fixed model instructions, bilingual
language-following policy, input transcription, output voice, and the approved
function schemas. The default voice is `Ethan`, which supports Chinese and
English. The UI does not add model or voice pickers in this phase.

### Interruption

Microphone upload continues while QWEN audio is playing. When the server reports
user speech or a semantic interruption:

1. stop the current response on the QWEN session;
2. flush the bounded local playback queue;
3. stop the active local playback process or stream;
4. mark `tts_active=false` in runtime health;
5. continue streaming the user's new utterance without reconnecting.

Server-side semantic interruption is authoritative. Existing local VAD and
dashboard RMS reporting remain useful for capture visibility, but YRobot does
not add a second speculative intent classifier. Real-device acceptance must
confirm that Reachy's own speaker output does not repeatedly self-interrupt; if
it does, tune the existing audio thresholds rather than adding another model.

### Wake behavior

The existing XIAOZHI wake handling remains unchanged. QWEN uses the same explicit
wake list, including `你好小白`. While idle, the QWEN session runs in manual-turn
mode: YRobot commits a locally segmented utterance for transcription but does not
request a model response until the returned transcript contains a wake phrase.
The wake utterance itself then receives `response.create`, so a single sentence
such as `你好小白，今天天气怎么样` is not discarded. While the conversation is
active, session turn detection and semantic interruption are enabled. After the
existing 60-second wake timeout, YRobot returns to manual wake-gated mode.
Switching backends must not alter or shorten the wake list.

### Function calls and local tools

QWEN tool calls are proposals. YRobot validates and executes them locally, then
returns a bounded result to the same realtime session. The model never receives
arbitrary network access, Home Assistant credentials, entity IDs, service names,
or an unrestricted tool-discovery surface.

Tools are registered from a fixed allowlist. Phase-one appliance control permits
only ordinary lights, low-risk plugs, fans, and state queries. Locks, covers,
garage doors, alarm panels, gas, heaters, and other high-risk devices are
rejected locally even if the model proposes them.

The currently reachable service at `http://192.168.1.200:8766` is an HTTP tool
adapter with `/health`, `/weather`, and related REST routes; probes found no
standard `/mcp` or `/sse` endpoint. It must not be mislabeled as a standard MCP
transport. QWEN integration therefore uses a narrow local `ToolExecutor`
boundary:

- REST tools may call only documented, explicitly mapped endpoints on port 8766.
- A true MCP adapter is enabled only after its exact URL, transport, tool list,
  and authentication contract are verified on `192.168.1.200`.
- Generic MCP auto-discovery is not exposed directly to QWEN.
- Appliance actions remain disabled until the verified adapter maps them to the
  existing local device whitelist.

This keeps QWEN realtime work independently deployable while preventing an
unknown MCP transport from becoming a hidden blocker or security hole.

## Dashboard design

Add one compact row to the existing settings/status visual language:

```text
对话后端  [ XIAOZHI | QWEN ]
当前运行：XIAOZHI
```

Selecting a different backend saves the pending value and shows the existing
restart-required banner. It does not restart automatically. The status payload
reports `configured_backend`, `running_backend`, connection state, and a safe
error summary. It never reports API keys or tokens.

When QWEN is selected but unavailable, the dashboard shows `QWEN 连接失败` and a
normal `切回 XIAOZHI` selection. There is no automatic provider change.

## Errors and recovery

- Missing QWEN API key: enter dashboard-only safe mode with an actionable error.
- Realtime disconnect: reconnect to QWEN with the existing bounded delay; do not
  switch provider.
- Malformed model tool arguments: reject the call and return a short tool error.
- Local tool timeout: return failure and never claim the appliance changed.
- Unsupported or blocked device: reject before any network request.
- Repeated startup failure: retain the current startup-failure guard and
  dashboard access.
- Rollback: save `xiaozhi`, restart YRobot, and verify XIAOZHI reconnects; no
  factory reset or official-daemon change is involved.

## Verification

### Automated

- Config tests cover the two allowed backend values, default XIAOZHI, and
  rejection of all other values.
- Dashboard API tests cover persistence, restart-required response, running vs.
  configured status, and secret redaction.
- QWEN protocol tests use a fake WebSocket to cover session setup, PCM events,
  transcripts, interruption, disconnect, function-call assembly, tool results,
  and malformed arguments.
- Tool tests prove allowlist enforcement, blocked-device rejection, timeout
  behavior, and bounded results.
- Existing targeted YRobot tests continue to pass.

### Real robot

1. Confirm the official daemon remains running and YRobot has zero motion-write
   failures before the change.
2. Select QWEN, explicitly restart YRobot, and verify the dashboard reports QWEN.
3. Say `你好小白`, hold a Chinese conversation, switch to English, then switch
   back to Chinese in the same session.
4. Interrupt at least ten spoken replies at early, middle, and late playback;
   verify playback stops promptly and no self-echo loop occurs.
5. Exercise a read-only local tool, then one allowlisted light; verify the real
   device state rather than trusting the spoken response.
6. Attempt a blocked appliance and an unknown tool; verify no network-side action.
7. Disconnect QWEN networking; verify visible failure and no XIAOZHI fallback.
8. Select XIAOZHI, restart, and verify the original wake/conversation path.
9. Recheck changing camera frames, motion counters, audio playback, and service
   stability after sustained operation.

## Out of scope

- Factory reset, Reachy daemon changes, firmware changes, or OS replacement.
- Simultaneous XIAOZHI and QWEN sessions.
- Hot-switching providers without restarting YRobot.
- Model/voice selection beyond the confirmed QWEN model and `Ethan` voice.
- Automatic exposure of every MCP-discovered tool.
- High-risk home-automation controls.
- Silent provider fallback.
