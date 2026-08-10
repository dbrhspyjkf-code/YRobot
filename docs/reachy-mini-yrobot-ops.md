# Reachy Mini YRobot Ops Notes

This document records the practical setup, changes, and failure modes for the
Reachy Mini YRobot deployment used in this project.

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

## QWEN Backend Deployment (2026-08-10)

YRobot now offers exactly two restart-based conversation backends in Settings:
`XIAOZHI` and `QWEN`. QWEN is fixed to
`qwen3.5-omni-flash-realtime`; there is no model text field and no silent
fallback to XIAOZHI after a QWEN failure. The official Reachy Mini daemon was
not modified.

Required robot-local environment entries live in
`/home/pollen/.config/yrobot/ha.env`:

```text
DASHSCOPE_API_KEY=...
YROBOT_HA_URL=...
YROBOT_HA_TOKEN=...
```

Never paste these values into progress notes, logs, Git, or the dashboard. The
dashboard reports only whether a backend is configured and running.

Appliance control is restricted to the local friendly-name whitelist and the
three fixed tools `get_weather`, `get_device_state`, and
`control_allowed_device`. High-risk device classes and arbitrary entity IDs or
services are rejected before network access. The service at
`192.168.1.200:8766` currently exposes working REST `/health` and `/weather`
routes; `/mcp` and `/sse` return 404, so it is not treated as a standard MCP
transport.

Current deployment state:

- reviewed QWEN source is installed in `/home/pollen/YRobot`;
- production focused verification is `79 passed` with clean focused Ruff;
- all required QWEN and Home Assistant entries are configured in the robot-local
  `ha.env` file, which remains mode `0600`;
- QWEN is selected, running, and reports `connected` with no runtime error;
- the QWEN activation restarted only `yrobot.service` once at
  2026-08-10 17:34:32+08; the official daemon remained active and the post-start
  motion loop was about 50 Hz with zero target failures;
- audible interruption, multilingual speech, and a physical allowlisted-light
  check remain operator acceptance tests; do not mark them passed from logs.

### Repaired duplicate wake response

The first QWEN hardware session exposed `Conversation already has an active
response`: repeated `你好小白` transcripts within an already active semantic-VAD
window sent a second `response.create`. The repair makes the wake gate ignore
repeated wake transcripts until the active window expires. It was added
test-first, then verified with the focused runtime suite, the full 79-test
focused suite, and focused Ruff.

- feature commit: `5c6bd29`;
- production commit: `a6bfb02`;
- exact two-file rollback backup:
  `/home/pollen/.local/state/yrobot/backups/qwen-response-guard-20260810-174027`;
- backup-manifest SHA-256:
  `52baf212e2dbbc2a66c4587057c8d78a70cc3819e603c64d58d530dab24d11fe`.

After the repaired restart, QWEN reported `connected` with no runtime error;
the motion loop was about 52.5 Hz with zero target failures and no error-level
journal entries. At that moment camera capture was disabled by dashboard state
(`running=false`), so `/api/camera/frame` returned 404. Re-enable capture and
check changing frame hashes before recording camera acceptance.

### QWEN audio output repair

QWEN text replies arrived but had no audible playback because the system ALSA
default PCM is `null`; the original QWEN `aplay` invocation did not choose the
Reachy sink. QWEN playback now explicitly uses `-D reachymini_audio_sink`.

- feature commit: `9a098df`;
- production commit: `4190180`;
- exact two-file rollback backup:
  `/home/pollen/.local/state/yrobot/backups/qwen-audio-sink-20260810-174517`;
- backup-manifest SHA-256:
  `7a7f0423196742aebad8597c1c7f0d9ea37d227cff034cc17109efac53f42d4d`.

The test-first regression, full focused suite, and focused Ruff passed. After
restart, QWEN was connected with no runtime error, and a short 440 Hz tone was
sent through `reachymini_audio_sink`. Record only an operator's audible report
as proof that this physical path is working.

The direct sink then rejected the QWEN output format: the hardware alias
requires 16 kHz stereo while QWEN supplies 24 kHz mono. The correct ALSA
conversion endpoint is `plug:reachymini_audio_sink`; it successfully opens at
24 kHz mono and converts to the Reachy hardware format.

- feature commit: `60bd98d`;
- production commit: `b24af62`;
- exact two-file rollback backup:
  `/home/pollen/.local/state/yrobot/backups/qwen-audio-convert-20260810-174913`;
- backup-manifest SHA-256:
  `af11e3d7304322dac60f0de3db03e72e95c0713cdd80145e85eb0e551b8c75c4`.

After restart, QWEN was connected with no runtime error and a 24 kHz mono 660
Hz tone was sent using this exact conversion path. The operator confirmed it was
audible, so the Reachy speaker route is accepted; QWEN spoken-response playback
was then confirmed in a normal conversation.

The operator also confirmed automatic language switching in a real QWEN
conversation. Multilingual support is accepted; interruption and appliance
actions remain separate physical acceptance checks.

### QWEN interruption repair

The initial interruption path cancelled only an active cloud response. When the
cloud had already finished generating but PCM was still buffered in the local
speaker process, user speech started a new turn without clearing that old audio.
Every `input_audio_buffer.speech_started` event now flushes local playback
immediately; cloud cancellation still runs when an active response exists.

- feature commit: `07ca1b2`;
- production commit: `d42910a`;
- exact two-file rollback backup:
  `/home/pollen/.local/state/yrobot/backups/qwen-interrupt-flush-20260810-180127`;
- backup-manifest SHA-256:
  `3f7dc03dd5c806ed446ff630d521b8a6b72d454e227ff6d0d6f5aeedd32ac4cd`.

The regression test first failed, then the full focused suite and focused Ruff
passed. The repaired QWEN service reconnected cleanly; final physical
acceptance was performed by interrupting a long reply; the operator confirmed
the old speech stopped promptly.

### Camera acceptance

Camera capture was disabled by dashboard state, not by the QWEN runtime. It was
enabled for this acceptance check and resumed with two captured frames, zero
failures, and differing consecutive frame hashes. QWEN remained connected and
the motion loop stayed near 50 Hz with zero target failures.

### Home Assistant acceptance enablement

The QWEN tool executor correctly refused appliance control until the explicit
robot-local `YROBOT_HA_ENABLED=true` switch was enabled. The operator chose the
allowlisted low-risk `书台灯` test, which authorized enabling that switch. The
environment file remains mode `0600`; after the required QWEN restart, a
read-only allowlisted call reported the lamp state as `on`. The next step is a
spoken QWEN request to turn it off, followed by a read-only state check.

### 2026-08-10 HA voice-control policy repair

The first spoken `书台灯` requests did not call the tool: the transcript was
fragmented and the QWEN session instructions omitted the enabled Home Assistant
policy. A regression test reproduced the omission. The minimal fix passes
`Settings.effective_system_prompt` into each QWEN session; QWEN/tool/runtime
checks and the full focused suite passed (83 tests), as did Ruff. It was
committed as `4086742` in the feature worktree and `33cf690` in production,
with backup `/home/pollen/.local/state/yrobot/backups/qwen-ha-policy-20260810-181643`.
The restarted service reports QWEN connected, HA enabled, and a healthy motion
loop. Physical voice acceptance remains pending.

### 2026-08-10 HA voice tool-call repair

After the policy repair, QWEN heard `关闭书灯` and replied that it was closing
`书台灯`, but a read-only Home Assistant state check still reported `on`. The
new root cause was event handling: Qwen Realtime can return function calls in
`response.done.response.output[]`, while YRobot only executed
`response.function_call_arguments.done`.

A regression test first failed with zero tool executions. The fix routes
`response.done` function-call output through the existing local whitelist tool
executor, then requests the final model response. The focused
QWEN/tool/runtime/config/backend suite passed (74 tests), Ruff passed, and the
service was restarted. Production commit: `8931388`; feature worktree commit:
`fd280b2`. Re-run the spoken `书台灯` acceptance next.

### 2026-08-10 HA local spoken-control repair

The repeated spoken test still left `书台灯` at `on`: QWEN replied with
`关闭书台灯`, but no server tool event was emitted. The safer fallback is now
local and deterministic: completed QWEN STT text is matched only against
robot-local allowlisted phrases, then executed through the same
`control_allowed_device` path. No arbitrary entity IDs or non-allowlisted
devices are accepted.

The `书台灯` whitelist now also includes `书灯` aliases for ASR variants.
Regression tests first failed on the missing spoken-control entrypoint; after
the fix the focused QWEN/tool/runtime/config/backend suite passed (76 tests),
`py_compile` passed, and narrowed Ruff checks passed. Production commit:
`ce35ed7`; feature worktree commit: `c5eb9eb`. The service was restarted and
reported QWEN connected with no runtime error. Re-run the spoken close-lamp
acceptance using `你好小白，关闭书灯` or `你好小白，关闭书台灯`.

### 2026-08-10 HA short ASR aliases

The next spoken attempts still left `书台灯` at `on`. The relevant QWEN STT
outputs were shortened to `关闭书` and `关闭书台`, so the strict local whitelist
matcher did not fire. Added short aliases only for the selected allowlisted
`书台灯`: `关闭书`, `关闭书台`, `关掉书台`, and open-side `打开书台`.
After restarting `yrobot.service`, QWEN reported connected with no runtime
error, the aliases were loaded by `ToolExecutor`, and pre-test HA state was
still `on`. Re-run voice acceptance with a short phrase such as
`你好小白，关闭书`.

### 2026-08-10 HA voice acceptance passed

The spoken close/open sequence passed through the local whitelist executor.
Logs show `关闭书` triggered `{'ok': True, 'device': '书台灯', 'action':
'turn_off'}` and `打开书台` triggered `{'ok': True, 'device': '书台灯',
'action': 'turn_on'}`. QWEN replied to both actions. A read-only HA state check
after the sequence reported `书台灯` as `on`, matching the final open command.
The operator replied `好了`.

### 2026-08-10 QWEN idle reconnect repair

After successful HA voice acceptance, the operator reported that 小白 could not
wake. Read-only status showed the root cause was not the wake phrase or mic:
audio input was enabled and the mic was available, but YRobot was in
`safe_mode` with QWEN `ws_state=error`. The provider error was `Your session was
closed because no response was generated for 300 seconds.`

The immediate recovery was a narrow `yrobot.service` restart, which restored
QWEN `connected`. The code now treats QWEN idle session-close messages as
reconnectable: it flushes playback, marks the runtime reconnecting, waits
briefly, and rebuilds the QWEN WebSocket instead of letting the app fall into
safe mode. A regression test first failed on the missing reconnect classifier;
after the fix the focused QWEN/tool/runtime/config/backend suite passed
(77 tests), `py_compile` passed, and the narrowed Ruff check passed. Production
commit: `4fdb792`; feature worktree commit: `b1e7c35`. The service was
restarted and reported QWEN connected, mic enabled, and no runtime error.

### 2026-08-10 Fan short ASR aliases

The operator reported `关闭风扇` did not work. Read-only evidence showed
`落地扇` remained `on`; logs showed QWEN STT shortened the command to `关闭风`
and QWEN replied verbally, but no local whitelist execution occurred. Added
short aliases only for the already allowlisted `落地扇`: `关闭风`, `关掉风`, and
`打开风`. After restarting `yrobot.service`, QWEN reported connected, audio
input remained enabled, the aliases were loaded by `ToolExecutor`, and the
pre-test HA state was still `on`. Re-run voice acceptance with
`你好小白，关闭风`.

### 2026-08-10 QWEN local-control reply policy

The operator reported that fan control actually worked, but QWEN replied
`抱歉，我无法控制这个设备`. Logs confirmed both `关闭风扇` and `打开风扇`
triggered the local whitelist executor successfully for `落地扇`; the final
read-only HA state was `on`, matching the final open command. The remaining
bug was response policy: QWEN did not know the local ASR fallback had already
executed the appliance action and judged device control by itself.

Updated the HA prompt policy to describe the current architecture: local
whitelist control handles appliance commands, and QWEN must not answer
`无法控制` unless a tool/local result reports failure. A regression test first
failed on the old prompt, then the focused QWEN/tool/runtime/config/backend
suite passed (77 tests), `py_compile` passed, and the narrowed F401 Ruff check
passed. Production commit: `26def16`; feature worktree commit: `0a190c3`.
The service was restarted and reported QWEN connected with no runtime error.

### 2026-08-10 Ceiling light short ASR aliases

The operator reported `吸顶灯` could not be controlled. Read-only evidence
showed `吸顶灯` was allowlisted and HA state was `off`, but logs showed QWEN
STT shortened the attempted open command to `打开西`; no local whitelist
execution occurred. Added short aliases only for the already allowlisted
`吸顶灯`: `打开西`, `打开吸`, `打开顶灯`, `关闭西`, `关闭吸`, and `关闭顶灯`.
After restarting `yrobot.service`, audio input was re-enabled, QWEN reported
connected, the aliases were loaded by `ToolExecutor`, and pre-test HA state
was still `off`. Re-run voice acceptance with `你好小白，打开西` or
`你好小白，打开吸顶灯`.

### 2026-08-10 Ceiling light second ASR aliases

The repeated open test still failed. Read-only state stayed `off`; logs showed
QWEN STT variants `打开C`, bare `打开`, and `打开系统`, with no local whitelist
execution. Added only the specific observed aliases `打开C` and `打开系统` for
the already allowlisted `吸顶灯`. Bare `打开` was intentionally not added
because it is ambiguous and could affect other devices. After restarting
`yrobot.service`, QWEN reported connected, audio input enabled, the aliases
were loaded by `ToolExecutor`, and pre-test HA state stayed `off`.

Deployment backup:

```text
/home/pollen/.local/state/yrobot/backups/qwen-realtime-20260810-170954
manifest SHA-256: a1340d1a3f23eff9ca6f2c26930c843dda69b459d40982027b9ed7205a9371e6
```

To complete acceptance, say `你好小白`, test Chinese → English → Chinese,
interrupt playback ten times, request weather, and operate one explicitly
allowlisted low-risk light while observing its physical state. An unknown device
and a blocked device class must cause no action. To roll back the code, first
stop `yrobot.service`, restore the 11 files listed by the backup manifest, then
start the service and verify the backend/status, motion loop, and changing
camera frames. Do not reset the robot or modify the official daemon.

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
.venv/bin/python -m py_compile yrobot/main.py yrobot/audio.py yrobot/app_config.py
.venv/bin/python -m pytest tests/test_main.py::test_home_assistant_action_without_response_suppresses_model_loop -q
.venv/bin/python -m pytest tests/test_main.py::test_startup_wake_up_enables_motors_before_movement -q
```

Full `pytest` and `ruff check .` have had unrelated failures from in-progress
local changes. Treat focused hardware-relevant checks as the immediate gate, and
only clean full-suite issues when intentionally doing repo cleanup.
