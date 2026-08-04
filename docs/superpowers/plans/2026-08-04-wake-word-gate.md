# Wake Word Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require the phrase `你好大白` before YRobot speaks, controls Home Assistant, or calls Hermes HTTP tools.

**Architecture:** Add a small wake-window state object in `yrobot/wake.py`. Because the realtime gateway does not expose user transcripts, wake detection uses model text output plus a strict prompt rule requiring the model to include `你好大白` when it hears the wake phrase. `Conversation._on_delta` will pass text through the wake gate before speaker playback and side effects.

**Tech Stack:** Python 3.12, existing `TurnGate`, pytest, ruff.

## Global Constraints

- Wake phrase is exactly `你好大白`.
- Wake window defaults to 10 seconds and is configurable by `YROBOT_WAKE_WINDOW_S`.
- Window is enabled by default with `YROBOT_WAKE_ENABLED=1`.
- Outside the window, audio playback, Home Assistant actions, and Hermes tools are blocked.
- Do not add a local wake-word model in this task.

---

### Task 1: Wake Gate

**Files:**
- Create: `yrobot/wake.py`
- Test: `tests/test_wake.py`

**Interfaces:**
- Produces: `WakeGate(phrase: str, window_s: float, enabled: bool)`, `.observe_text(text, now) -> bool`, `.awake(now) -> bool`, `.allow_text(text, now) -> bool`.

- [ ] Write tests for phrase matching, timeout, and disabled mode.
- [ ] Implement the minimal state object.
- [ ] Run `pytest tests/test_wake.py -q`.

### Task 2: Conversation Integration

**Files:**
- Modify: `yrobot/config.py`
- Modify: `yrobot/main.py`
- Test: `tests/test_config.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `WakeGate`.
- Produces: settings fields `wake_enabled`, `wake_phrase`, `wake_window_s`; prompt policy requiring exact marker.

- [ ] Add config tests for wake defaults and env overrides.
- [ ] Add main tests proving outside-window audio is dropped and outside-window side effects are blocked.
- [ ] Wire `_wake` into `Conversation`.
- [ ] Run affected tests.

### Task 3: Deploy

**Files:**
- Modify environment file `/home/pollen/.config/yrobot/ha.env`.

- [ ] Run full `pytest -q` and `ruff check .`.
- [ ] Ensure env enables wake phrase `你好大白` and 10-second window.
- [ ] Restart `yrobot.service` and check active status.
- [ ] Commit the change.
