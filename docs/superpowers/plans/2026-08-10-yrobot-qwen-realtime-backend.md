# YRobot Qwen Realtime Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a restart-based XIAOZHI/QWEN selector to YRobot, run `qwen3.5-omni-flash-realtime` with multilingual speech and interruption, and execute only allowlisted local tools.

**Architecture:** Keep the official Reachy daemon and the current XIAOZHI runtime unchanged. Persist the selected backend in the existing systemd environment file, branch once in `Yrobot.run()`, and isolate QWEN WebSocket protocol handling in `yrobot/qwen_realtime.py`. QWEN function calls pass through a local `ToolExecutor`; Home Assistant actions remain whitelist-bound, while `192.168.1.200:8766` is treated as its verified REST interface rather than an unverified MCP transport.

**Tech Stack:** Python 3.12, FastAPI, vanilla HTML/CSS/JavaScript, `websockets`, `sounddevice`, NumPy, pytest, systemd, Reachy Mini SDK 1.9.x.

## Global Constraints

- Production checkout: `/home/pollen/YRobot` on `pollen@192.168.1.14`.
- Preserve the official Reachy Mini daemon and its port-8000 APIs.
- Preserve all pre-existing dirty-tree changes and `.bak` files.
- Implement in an isolated `codex/qwen-realtime` worktree created from the current runtime snapshot; deploy only reviewed files back to production.
- Exactly one backend runs at a time; switching requires an explicit YRobot restart.
- Default backend is `xiaozhi`; never silently fall back from QWEN.
- Fixed model: `qwen3.5-omni-flash-realtime`.
- Keep `DASHSCOPE_API_KEY`, Home Assistant tokens, and MCP credentials out of API responses, logs, commits, and browser storage.
- Preserve the explicit `你好小白` wake phrase and the existing 60-second wake timeout.
- Initially permit only lights, low-risk plugs, fans, and state/read-only tools; reject locks, covers, garage doors, alarms, gas, and heaters.
- Do not expose generic MCP discovery to the model.
- Update `docs/superpowers/progress/yrobot-qwen-realtime-progress.md` after every task with commit, tests, live evidence, blockers, and next action.

---

### Task 1: Create an isolated implementation baseline

**Files:**
- Create in feature worktree: all currently active tracked/untracked source files required by YRobot
- Modify: `docs/superpowers/progress/yrobot-qwen-realtime-progress.md`

**Interfaces:**
- Consumes: production working tree at `/home/pollen/YRobot`, design commit `a4f238e`
- Produces: isolated branch `codex/qwen-realtime` whose first commit exactly represents the active source state without secrets, virtualenvs, caches, or `.bak` files

- [ ] **Step 1: Record production state without changing it**

Run:

```bash
cd /home/pollen/YRobot
git status --short --branch
git rev-parse HEAD
systemctl is-active yrobot.service reachy-mini-daemon.service
curl -fsS http://127.0.0.1:8000/api/daemon/status
```

Expected: branch `codex/home-assistant-control`, design commit at HEAD, both services active, and the existing dirty files still present.

- [ ] **Step 2: Create the isolated worktree**

Run using the `superpowers:using-git-worktrees` workflow, selecting an explicit path such as `/home/pollen/YRobot-qwen-realtime` and branch `codex/qwen-realtime`.

Expected: production remains `/home/pollen/YRobot`; the new worktree starts at `a4f238e`.

- [ ] **Step 3: Copy the active source snapshot into the isolated worktree**

Copy only active project files. Exclude `.git`, `.env`, `.venv`, caches, bytecode, backup files, and generated egg-info. Apply tracked deletions in the isolated worktree so its source layout matches production.

Expected: `diff -qr` over `yrobot`, `tests`, `pyproject.toml`, `.env.example`, and active docs shows no differences except excluded artifacts.

- [ ] **Step 4: Commit the isolated baseline**

```bash
git add -A
git status --short
git commit -m "chore: snapshot active YRobot runtime"
```

Expected: the commit contains no `.env`, key, token, `.venv`, cache, bytecode, or `.bak` path.

- [ ] **Step 5: Update progress**

Record the baseline commit, production HEAD, excluded artifacts, current service state, and that production was not modified.

---

### Task 2: Add backend configuration and dashboard selection

**Files:**
- Modify: `yrobot/config.py`
- Modify: `yrobot/app_config.py`
- Modify: `yrobot/state.py`
- Modify: `yrobot/static/index.html`
- Modify: `yrobot/static/main.js`
- Modify: `yrobot/static/style.css`
- Modify: `.env.example`
- Create: `tests/test_backend_selection.py`
- Modify: `docs/superpowers/progress/yrobot-qwen-realtime-progress.md`

**Interfaces:**
- Consumes: `update_env_value(path: Path, key: str, value: str) -> None`
- Produces: `Settings.conversation_backend: Literal["xiaozhi", "qwen"]`, `GET /api/conversation/backend`, `PUT /api/conversation/backend`, and `RUNTIME_HEALTH["backend"]`

- [ ] **Step 1: Write failing configuration tests**

```python
import pytest

from yrobot.config import Settings


def test_backend_defaults_to_xiaozhi():
    assert Settings.from_env({}).conversation_backend == "xiaozhi"


@pytest.mark.parametrize("value", ["xiaozhi", "qwen"])
def test_backend_accepts_only_supported_values(value):
    assert Settings.from_env({"YROBOT_CONVERSATION_BACKEND": value}).conversation_backend == value


def test_backend_rejects_unknown_value():
    with pytest.raises(ValueError, match="YROBOT_CONVERSATION_BACKEND"):
        Settings.from_env({"YROBOT_CONVERSATION_BACKEND": "both"})


def test_qwen_secret_is_read_but_not_in_repr():
    settings = Settings.from_env({"DASHSCOPE_API_KEY": "secret-value"})
    assert settings.qwen_api_key == "secret-value"
    assert "secret-value" not in repr(settings)
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/pytest -q tests/test_backend_selection.py`

Expected: FAIL because the default is currently `minicpmo`, validation is absent, and QWEN fields do not exist.

- [ ] **Step 3: Implement minimal configuration**

Add constants and fields equivalent to:

```python
SUPPORTED_CONVERSATION_BACKENDS = frozenset({"xiaozhi", "qwen"})
QWEN_REALTIME_MODEL = "qwen3.5-omni-flash-realtime"
QWEN_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"

conversation_backend: str = "xiaozhi"
qwen_api_key: str | None = field(default=None, repr=False)
qwen_model: str = QWEN_REALTIME_MODEL
qwen_url: str = QWEN_REALTIME_URL
qwen_voice: str = "Ethan"
```

Validate the backend enum in `Settings.__post_init__` and read the values from `YROBOT_CONVERSATION_BACKEND`, `DASHSCOPE_API_KEY`, `YROBOT_QWEN_URL`, and `YROBOT_QWEN_VOICE`. Do not make the model configurable; it stays fixed.

- [ ] **Step 4: Write failing API tests**

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yrobot.app_config import register_settings_routes


def test_backend_api_persists_choice(tmp_path, monkeypatch):
    env_path = tmp_path / "ha.env"
    monkeypatch.setenv("YROBOT_CONVERSATION_BACKEND", "xiaozhi")
    app = FastAPI()
    register_settings_routes(app, vad_env_path=env_path)
    client = TestClient(app)

    response = client.put("/api/conversation/backend", json={"backend": "qwen"})
    assert response.status_code == 200
    assert response.json() == {"configured_backend": "qwen", "restart_required": True}
    assert "YROBOT_CONVERSATION_BACKEND=qwen" in env_path.read_text()


def test_backend_api_rejects_unknown_value(tmp_path):
    app = FastAPI()
    register_settings_routes(app, vad_env_path=tmp_path / "ha.env")
    response = TestClient(app).put("/api/conversation/backend", json={"backend": "both"})
    assert response.status_code == 422
```

- [ ] **Step 5: Implement backend API and safe status**

The GET response contains only:

```json
{
  "configured_backend": "qwen",
  "running_backend": "xiaozhi",
  "connection_state": "connected",
  "error": null
}
```

The PUT endpoint validates `xiaozhi|qwen`, calls `update_env_value(vad_env_path, "YROBOT_CONVERSATION_BACKEND", backend)`, and returns `restart_required: true`. Add `backend` and `last_error` defaults to `RUNTIME_HEALTH`. Never include API keys or tokens.

- [ ] **Step 6: Add the selector to the existing dashboard**

Add a compact segmented control labeled `对话后端` with exactly `XIAOZHI` and `QWEN`. Reuse existing panel, pill, button, disabled, notice, and error styles. JavaScript loads the backend endpoint, saves a changed selection, shows the existing restart banner, and does not automatically restart or fallback.

- [ ] **Step 7: Run focused tests and static checks**

```bash
.venv/bin/pytest -q tests/test_backend_selection.py
.venv/bin/ruff check yrobot/config.py yrobot/app_config.py yrobot/state.py tests/test_backend_selection.py
```

Expected: PASS. Manually inspect the HTML/JS diff for exactly two backend choices and no credential fields.

- [ ] **Step 8: Commit and update progress**

```bash
git add .env.example yrobot/config.py yrobot/app_config.py yrobot/state.py \
  yrobot/static/index.html yrobot/static/main.js yrobot/static/style.css \
  tests/test_backend_selection.py docs/superpowers/progress/yrobot-qwen-realtime-progress.md
git commit -m "feat: add YRobot conversation backend selector"
```

---

### Task 3: Implement the QWEN Realtime protocol client

**Files:**
- Create: `yrobot/qwen_realtime.py`
- Create: `tests/test_qwen_realtime.py`
- Modify: `docs/superpowers/progress/yrobot-qwen-realtime-progress.md`

**Interfaces:**
- Consumes: mono PCM16 input at 16 kHz and QWEN settings
- Produces: `QwenRealtimeClient.run(stop_event)`, `append_pcm(pcm: bytes)`, `commit_turn()`, `request_response()`, and callback events for PCM, transcripts, interruption, tool calls, connection state, and errors

- [ ] **Step 1: Write fake-WebSocket tests**

Cover these exact events:

```python
def test_session_update_uses_fixed_model_and_tools(): ...
def test_audio_delta_is_decoded_and_forwarded(): ...
def test_transcript_callback_receives_completed_text(): ...
def test_speech_started_cancels_response_and_flushes_playback(): ...
def test_function_call_done_executes_and_writes_result(): ...
def test_malformed_function_arguments_return_tool_error(): ...
def test_disconnect_reports_error_without_provider_fallback(): ...
```

Assert that function output is written as:

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "function_call_output",
    "call_id": "call_xxx",
    "output": "{\"ok\":true}"
  }
}
```

and followed by the current official client event `{"type": "response.create"}`.
Output modalities are configured once in `session.update`; do not repeat
unsupported response fields.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/pytest -q tests/test_qwen_realtime.py`

Expected: FAIL because `yrobot.qwen_realtime` does not exist.

- [ ] **Step 3: Implement the smallest protocol state machine**

Use `websockets.connect()` with `Authorization: Bearer <key>` and append the
fixed model as the `model` query parameter. The legacy
`wss://dashscope.aliyuncs.com` domain remains officially supported; a
workspace-specific URL can be supplied through `YROBOT_QWEN_URL`. Send
`session.update` after `session.created`; set PCM formats, `Ethan`, input
transcription, bilingual instructions, turn detection, and the fixed tool
list. Handle:

- `conversation.item.input_audio_transcription.completed`
- `input_audio_buffer.speech_started`
- `response.audio.delta`
- `response.audio_transcript.done`
- `response.function_call_arguments.delta`
- `response.function_call_arguments.done`
- `response.done`
- `error`

Use `json.loads()` only after complete function arguments arrive. Bound serialized tool output to 4 KiB before returning it to QWEN.

- [ ] **Step 4: Implement explicit cancellation**

On user speech during playback, send `response.cancel`, invoke the playback-flush callback, and ignore stale `response.audio.delta` events for the cancelled response ID.

- [ ] **Step 5: Run focused verification**

```bash
.venv/bin/pytest -q tests/test_qwen_realtime.py
.venv/bin/ruff check yrobot/qwen_realtime.py tests/test_qwen_realtime.py
```

Expected: PASS with no real API calls.

- [ ] **Step 6: Commit and update progress**

```bash
git add yrobot/qwen_realtime.py tests/test_qwen_realtime.py \
  docs/superpowers/progress/yrobot-qwen-realtime-progress.md
git commit -m "feat: add Qwen realtime protocol client"
```

---

### Task 4: Integrate QWEN with Reachy audio, wake, and motion

**Files:**
- Modify: `yrobot/main.py`
- Modify: `yrobot/audio_runtime.py`
- Modify: `yrobot/state.py`
- Create: `tests/test_qwen_runtime.py`
- Modify: `docs/superpowers/progress/yrobot-qwen-realtime-progress.md`

**Interfaces:**
- Consumes: `Settings.conversation_backend`, `QwenRealtimeClient`, existing sounddevice capture, `BoundedLatestQueue`, Choreographer, dashboard mic signal, and stop event
- Produces: mutually exclusive `_run_xiaozhi()` and `_run_qwen()` paths with shared hardware cleanup guarantees

- [ ] **Step 1: Write failing routing and cleanup tests**

```python
def test_run_routes_xiaozhi_without_constructing_qwen(): ...
def test_run_routes_qwen_without_connecting_xiaozhi(): ...
def test_missing_qwen_key_enters_safe_mode_with_qwen_error(): ...
def test_qwen_interrupt_flushes_audio_queue_and_stops_playback(): ...
def test_qwen_wake_list_contains_nihao_xiaobai(): ...
def test_qwen_wake_expires_after_sixty_seconds(): ...
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/pytest -q tests/test_qwen_runtime.py`

Expected: FAIL because QWEN routing and runtime do not exist.

- [ ] **Step 3: Route once at startup**

Set `RUNTIME_HEALTH.update(backend=settings.conversation_backend, last_error=None)` and branch in `Yrobot.run()`:

```python
if settings.conversation_backend == "qwen":
    self._run_qwen(reachy_mini, stop_event, settings)
else:
    self._run_xiaozhi(reachy_mini, stop_event)
```

Do not change `_run_xiaozhi()` behavior.

- [ ] **Step 4: Implement QWEN audio wiring**

Reuse the current 16 kHz microphone capture and dashboard meter publication. Send PCM16 mono frames to QWEN. Play 24 kHz PCM16 mono responses through one bounded latest-only queue. Cleanup order is: stop cloud task, flush queue, stop playback, stop/close mic, stop visual/DOA workers, close/join choreographer.

- [ ] **Step 5: Implement wake-gated idle and active conversation**

Use the exact wake tuple currently in XIAOZHI:

```python
WAKE_WORDS = ("你好小白", "小白", "阿皮", "reachy", "hey reachy", "嘿")
WAKE_TIMEOUT = 60.0
```

In idle manual-turn mode, commit speech for transcription but do not request a response until the transcript contains a wake phrase. In active mode, allow semantic/server VAD interruption. Reset the 60-second deadline on every user speech burst and return to wake-gated mode when it expires.

- [ ] **Step 6: Preserve motion semantics**

Set `LISTEN` on recognized user speech, `SPEAK` on first QWEN audio, and idle/release-still on response completion or cancellation. Do not add new automatic recorded moves.

- [ ] **Step 7: Verify automatically**

```bash
.venv/bin/pytest -q tests/test_qwen_runtime.py tests/test_qwen_realtime.py tests/test_backend_selection.py
.venv/bin/ruff check yrobot/main.py yrobot/audio_runtime.py yrobot/state.py tests/test_qwen_runtime.py
```

Expected: PASS. Existing targeted Xiaozhi tests that still match the active source must also pass; stale deleted-module tests are recorded rather than repaired outside scope.

- [ ] **Step 8: Commit and update progress**

```bash
git add yrobot/main.py yrobot/audio_runtime.py yrobot/state.py tests/test_qwen_runtime.py \
  docs/superpowers/progress/yrobot-qwen-realtime-progress.md
git commit -m "feat: run Qwen realtime conversations on Reachy"
```

---

### Task 5: Add allowlisted Function Calling and local integrations

**Files:**
- Create: `yrobot/qwen_tools.py`
- Create: `tests/test_qwen_tools.py`
- Modify: `yrobot/main.py`
- Modify: `yrobot/config.py`
- Modify: `docs/superpowers/progress/yrobot-qwen-realtime-progress.md`

**Interfaces:**
- Consumes: QWEN `name` plus JSON arguments, existing Home Assistant URL/token/whitelist settings, verified REST base `http://192.168.1.200:8766`
- Produces: `ToolExecutor.schemas() -> list[dict]` and `ToolExecutor.execute(name: str, arguments: dict) -> dict`

- [ ] **Step 1: Write security-first failing tests**

```python
def test_schemas_expose_only_explicit_tools(): ...
def test_unknown_tool_is_rejected_before_network(): ...
def test_unknown_device_is_rejected_before_network(): ...
def test_lock_cover_alarm_gas_and_heater_are_blocked(): ...
def test_rest_weather_uses_only_verified_8766_route(): ...
def test_ha_light_action_uses_whitelist_mapping_not_model_entity_id(): ...
def test_tool_timeout_returns_failure_not_success(): ...
def test_tool_result_is_bounded_and_contains_no_token(): ...
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/pytest -q tests/test_qwen_tools.py`

Expected: FAIL because `yrobot.qwen_tools` does not exist.

- [ ] **Step 3: Implement fixed tool schemas**

Expose only narrow names such as `get_weather`, `get_device_state`, and `control_allowed_device`. `control_allowed_device` accepts a friendly whitelist name plus enum action; it never accepts raw Home Assistant domain, service, token, or entity ID.

- [ ] **Step 4: Implement verified adapters**

Use standard-library `urllib.request` with a 5-second timeout. Map read-only Hermes REST calls only to known endpoints on port 8766. For appliance control, resolve the friendly name from the robot-local Home Assistant whitelist, reject blocked domains, then call the local Home Assistant API. Do not restore the deleted heuristic text-trigger modules.

- [ ] **Step 5: Keep MCP status honest**

Record in status/progress that port 8766 is a REST adapter. Do not add an MCP dependency or label until a real Streamable HTTP/SSE endpoint, authentication contract, and tool list are supplied and verified. The `ToolExecutor` boundary allows a later MCP adapter without changing QWEN event handling.

- [ ] **Step 6: Wire tool execution into QWEN**

Pass `ToolExecutor.schemas()` in `session.update`. On `response.function_call_arguments.done`, execute once per `call_id`, return structured success/failure, then trigger the second response. Never speak success unless the local executor returned `ok: true`.

- [ ] **Step 7: Run security and integration tests**

```bash
.venv/bin/pytest -q tests/test_qwen_tools.py tests/test_qwen_realtime.py tests/test_qwen_runtime.py
.venv/bin/ruff check yrobot/qwen_tools.py yrobot/qwen_realtime.py yrobot/main.py tests/test_qwen_tools.py
```

Expected: PASS with network calls mocked. Blocked actions produce zero opener calls.

- [ ] **Step 8: Commit and update progress**

```bash
git add yrobot/qwen_tools.py yrobot/qwen_realtime.py yrobot/main.py yrobot/config.py \
  tests/test_qwen_tools.py docs/superpowers/progress/yrobot-qwen-realtime-progress.md
git commit -m "feat: add allowlisted Qwen tool execution"
```

---

### Task 6: Deploy, verify, and document the real robot

**Files:**
- Modify: `/home/pollen/.config/yrobot/ha.env` on the robot without printing secrets
- Deploy reviewed feature files from isolated worktree to `/home/pollen/YRobot`
- Modify: `docs/superpowers/progress/yrobot-qwen-realtime-progress.md`
- Modify: `docs/reachy-mini-yrobot-ops.md`

**Interfaces:**
- Consumes: tested `codex/qwen-realtime` commits and operator-provided API key
- Produces: running selectable XIAOZHI/QWEN system with verified rollback

- [ ] **Step 1: Run pre-deployment verification**

```bash
.venv/bin/pytest -q tests/test_backend_selection.py tests/test_qwen_realtime.py \
  tests/test_qwen_runtime.py tests/test_qwen_tools.py
.venv/bin/ruff check yrobot tests/test_backend_selection.py tests/test_qwen_realtime.py \
  tests/test_qwen_runtime.py tests/test_qwen_tools.py
```

Expected: all feature tests and lint pass. Run `git diff --check` and verify no secret-like value appears in the diff.

- [ ] **Step 2: Back up only deployment targets**

Create timestamped copies of the exact production files that will be replaced. Record paths and hashes in the progress document. Do not touch unrelated dirty or backup files.

- [ ] **Step 3: Deploy reviewed files and credential**

Copy only reviewed feature files to production. Update `DASHSCOPE_API_KEY` and `YROBOT_CONVERSATION_BACKEND` atomically in `/home/pollen/.config/yrobot/ha.env` without displaying their values. Confirm file mode remains `0600`.

- [ ] **Step 4: Restart into QWEN and preserve first-failure evidence**

Restart `yrobot.service` once. Before any second restart, capture `systemctl status`, `/api/status`, `/api/conversation/backend`, and the relevant journal window. Confirm official daemon remains running and motion error counters are zero.

- [ ] **Step 5: Perform physical acceptance**

Verify on hardware:

1. `你好小白` wakes QWEN.
2. Chinese, English, and return-to-Chinese work in one session.
3. Ten interruptions stop playback promptly without self-echo loops.
4. A read-only tool returns live data.
5. One allowlisted light changes physical state and reports true result.
6. An unknown device and a blocked class cause no action.
7. Network loss shows QWEN failure and never starts XIAOZHI.
8. Camera frame hashes change, motion stays responsive, and audio remains audible.

- [ ] **Step 6: Verify rollback**

Select XIAOZHI, restart YRobot, say `你好小白`, and confirm the original conversation path reconnects. Then return to the operator's chosen final backend.

- [ ] **Step 7: Update durable documentation**

Record final commits, deployed hashes, service status, physical results, unresolved MCP endpoint limitation, rollback command, and next recommended change in both progress and operations docs.

- [ ] **Step 8: Commit documentation**

```bash
git add docs/superpowers/progress/yrobot-qwen-realtime-progress.md docs/reachy-mini-yrobot-ops.md
git commit -m "docs: record Qwen realtime deployment"
```
