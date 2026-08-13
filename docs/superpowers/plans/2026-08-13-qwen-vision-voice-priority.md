# QWEN Vision With Voice Priority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve Dashboard, visual gaze, and QWEN visual answers while ensuring that camera work and image upload cannot compete with an active QWEN ASR turn.

**Architecture:** Reuse the existing Dashboard `CameraStreamer` as the only owner of `media.get_frame()`. Inject that streamer into both `SpeakerTracker` and the QWEN runtime. The QWEN runtime sends at most one cached JPEG after the completed transcript indicates a visual request; it never sends images in the microphone loop.

**Tech Stack:** Python 3.12, FastAPI, asyncio, OpenCV, Qwen realtime WebSocket, pytest.

**Spec:** `docs/superpowers/specs/2026-08-13-qwen-vision-voice-priority-design.md`

## Global Constraints

- Do not modify the official Reachy daemon, microphone/VAD settings, wake aliases, Home Assistant rules, or tool behavior.
- Keep the QWEN/XIAOZHI backend exclusivity and restart only YRobot for deployment.
- Do not add dependencies or new Dashboard settings.
- Preserve all pre-existing uncommitted changes not required by this plan.
- Never expose credentials in output, docs, tests, or commits.

---

### Task 1: Make `CameraStreamer` the shared frame source

**Files:**
- Modify: `yrobot/app_config.py:506-637`
- Modify: `yrobot/tracking.py:47-282`
- Test: `tests/test_tracking.py` (create if absent)

**Interfaces:**
- Consumes: `CameraStreamer.set_running(bool) -> bool` and `CameraStreamer.latest() -> bytes | None`.
- Produces: `SpeakerTracker(..., camera_streamer: CameraStreamer | None = None)`. An injected streamer must replace all loopback `/api/camera/*` calls.

- [ ] **Step 1: Write the failing test**

```python
def test_speaker_tracker_uses_injected_camera_streamer():
    class Camera:
        def set_running(self, value): pass
        def latest(self): return b"jpeg"
    tracker = SpeakerTracker(object(), object(), camera_streamer=Camera())
    assert tracker._camera_streamer.latest() == b"jpeg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/pollen/YRobot/.venv/bin/python -m pytest -q tests/test_tracking.py::test_speaker_tracker_uses_injected_camera_streamer`

Expected: FAIL because `SpeakerTracker` has no `camera_streamer` argument.

- [ ] **Step 3: Implement the minimal shared ownership**

Add the optional `camera_streamer` constructor argument and a private helper
that calls `set_running(True)` then returns `latest()`. Replace loopback HTTP
frame/state reads in `SpeakerTracker._face_tracker_loop` with this helper.
Change `register_settings_routes` to return its existing `CameraStreamer` and
retain it on `YRobot`, so the runtime passes one instance to the tracker.

- [ ] **Step 4: Verify green**

Run: `/home/pollen/YRobot/.venv/bin/python -m pytest -q tests/test_tracking.py tests/test_cli_startup.py`

Expected: PASS.

### Task 2: Defer QWEN image upload until a visual transcript completes

**Files:**
- Modify: `yrobot/main.py:257-260,445-950`
- Modify: `tests/test_qwen_runtime.py`
- Modify: `tests/test_qwen_realtime.py` only if required by the image event contract
- Delete: `yrobot/vision.py` and adapt `tests/test_vision.py` only when no caller imports `LatestCamera`

**Interfaces:**
- Consumes: `CameraStreamer.latest() -> bytes | None`, `QwenRealtimeClient.append_image(str)`, and completed QWEN transcripts.
- Produces: `_qwen_wants_visual_snapshot(transcript: str) -> bool` plus an async helper that sends one cached image before the normal QWEN response request.

- [ ] **Step 1: Write the failing intent test**

```python
def test_qwen_visual_snapshot_intent_is_narrow():
    assert _qwen_wants_visual_snapshot("小白，你看到什么？") is True
    assert _qwen_wants_visual_snapshot("这是什么？") is True
    assert _qwen_wants_visual_snapshot("今天深圳天气怎么样？") is False
    assert _qwen_wants_visual_snapshot("打开吸顶灯") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/pollen/YRobot/.venv/bin/python -m pytest -q tests/test_qwen_runtime.py::test_qwen_visual_snapshot_intent_is_narrow`

Expected: FAIL because the matcher is not defined.

- [ ] **Step 3: Implement the smallest scheduling change**

Remove `LatestCamera`, `_drain_camera`, `_drain_face`, and their per-960-sample
microphone-loop call. Add a narrow matcher for `看到`, `看见`, `看`, `什么`,
`描述`, `画面`, `前面`, and `手里`. In the existing completed-transcript task,
if QWEN vision is enabled and the matcher succeeds, base64-encode exactly one
cached JPEG and call `append_image` immediately before the ordinary response
request. Run face recognition from the same cached frame only after the
transcript. A missing frame or failed append logs and skips vision without
interrupting audio.

- [ ] **Step 4: Verify green and inspect camera callers**

Run:

```bash
/home/pollen/YRobot/.venv/bin/python -m pytest -q tests/test_qwen_runtime.py tests/test_qwen_realtime.py tests/test_tracking.py
/home/pollen/YRobot/.venv/bin/python -m py_compile yrobot/main.py yrobot/app_config.py yrobot/tracking.py
rg -n "LatestCamera|media\.get_frame\(" yrobot
```

Expected: PASS; `CameraStreamer` is the only production `media.get_frame()` owner.

### Task 3: Deploy and perform hardware acceptance

**Files:**
- Modify: `docs/reachy-mini-yrobot-ops.md`

**Interfaces:**
- Consumes: local validated source and Reachy `GET /api/status`.
- Produces: an operations record with deploy hash, runtime mode, spoken acceptance results, and CPU/timestamp evidence.

- [ ] **Step 1: Transfer only changed production files**

Copy changed `yrobot/`, `tests/`, and the operations record to
`/home/pollen/YRobot`; exclude all environment files, backups, `ios/`, and
unrelated untracked files.

- [ ] **Step 2: Verify before restart**

Run Reachy `py_compile` and the three focused test files from Task 2. Expect
PASS before touching the process.

- [ ] **Step 3: Restart only YRobot in QWEN visual mode**

Load the existing `.env` and `ha.env`, set `YROBOT_SEND_VIDEO=1`, stop only
the `python -u -m yrobot.main` process, and restart it using the existing log
path. Do not restart the official daemon.

- [ ] **Step 4: Check live status and one owner**

Check `/api/status`, `/api/camera/state`, process CPU, and log ordering.
Confirm QWEN, `video_enabled=true`, no safe mode, and a single capture worker.

- [ ] **Step 5: Spoken acceptance with the user**

Test in order: `你好小白，今天深圳天气怎么样？`, `打开吸顶灯`, `小白，你看到什么？`, then Dashboard preview. Record final STT, image append ordering, response, and CPU. If pure voice is wrong or truncated, restore `YROBOT_SEND_VIDEO=0` immediately and report evidence without changing ASR rules.

- [ ] **Step 6: Update operations record and commit safely**

Append acceptance evidence and rollback instruction to the operations record.
Stage and commit only plan-related files after all checks and spoken acceptance
succeed; do not include pre-existing unrelated changes.
