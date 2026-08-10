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
