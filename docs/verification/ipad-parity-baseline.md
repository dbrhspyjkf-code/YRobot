# iPad Parity Baseline

> Record of the exact baseline this feature branch started from, plus the
> pre-existing test state. Created by Task 1 of
> `docs/plans/2026-08-20-yrobot-ipad-cockpit.md`.

## Baseline commands and output (2026-08-20, Asia/Shanghai)

Run in the feature worktree `/Users/leenzhou/Projects/YRobot-reachy-current/.worktrees/ipad-cockpit`.

```text
$ git status --short --branch
## feature/ipad-cockpit   (clean before Task 1 files)

$ git rev-parse HEAD
b8e35d804c3b7d95632284e913b8316046bc58e8

$ git log -1 --oneline -- desktop-app
b8e35d8 fix(desktop): protect YRobot speech from camera audio
```

- Parent repo branch at planning time: `codex/xz-uplink-vad-v2@b8e35d804c3b` — matches plan §0.1.
- `desktop-app` is a subtree import of the former standalone `yrobot/custom` branch
  (`a4868ae import: desktop app (YRobot Control) @ yrobot/custom 580e084`) followed by
  `b8e35d8`. The standalone-repo commit `9d441599e556` named in plan §0.1 is not a
  commit in this parent repo; the imported snapshot plus `b8e35d8` **is** the desktop
  functional baseline the plan was researched against (the planning-time baseline SHA
  equals this worktree's HEAD). No parity-relevant divergence found.
- Parallel worktree `.worktrees/app-config-controllers-split` (branch
  `app-config-controllers-split`) exists; it is untouched by this branch.

## Toolchain verified on this machine

- Xcode 26.5, Swift 6.3.2, XcodeGen 2.45.4, Node v26.3.1, npm 11.16.0, yarn 1.22.22,
  uv-managed Python 3.12 (project requires >=3.11,<3.13; system python3 is 3.14 and
  must not be used directly).
- `desktop-app` uses `yarn.lock` (no `package-lock.json`): installs run with
  `yarn install --frozen-lockfile`; scripts run via `npm run`. The plan's literal
  `npm --prefix desktop-app ci` is not applicable and is replaced by yarn install.

## Pre-existing Python test state (do not mix into iPad changes)

Command used (uv, python 3.12, PYTHONPATH=.):

```bash
PYTHONPATH=. uv run --no-project --python 3.12 \
  --with pytest --with fastapi --with websockets --with numpy --with python-dotenv \
  --with pydantic --with cryptography --with reachy-mini --with opencv-python-headless \
  python -m pytest -q --ignore=<stale files below>
```

Result for the collectable subset: **320 passed, 6 failed, 11 errors**.

- 11 ERROR — `tests/test_cli_startup.py` fixture `main_module` fails at setup on this
  snapshot (monkeypatch target symbols missing from `yrobot.main`).
- 6 FAILED — `test_config.py::test_home_assistant_prompt_requires_exact_device_action`,
  `test_qwen_runtime.py::test_qwen_vad_default_is_not_overly_aggressive`, and 4
  `test_qwen_tools.py` spoken-control volume cases.
- 12 test files are uncollectable because they import modules that do not exist in this
  snapshot (`yrobot.{session,turn,realtime,hermes_tools,home_assistant,local_info,persistent_memory}`,
  `AppConfig` from `app_config`, `EchoMatch`/`Microphone` from `audio`):
  `test_app_config.py test_audio.py test_barge.py test_hermes_tools.py
  test_home_assistant.py test_local_info.py test_main.py test_persistent_memory.py
  test_realtime.py test_session.py test_turn.py` (+ `test_wake_aliases.py`,
  `test_wake_match.py`, `test_xiaozhi_emotion.py`, `test_xiaozhi_mqtt.py` errors when
  fewer deps injected).
- All of the above reproduce identically on a clean checkout of `b8e35d8` (main worktree
  is clean); they are pre-existing and out of scope for the iPad branch.

Focused gates used during iPad work (per plan):

```bash
PYTHONPATH=. uv run --no-project --python 3.12 --with pytest --with fastapi \
  --with websockets --with numpy python -m pytest -q tests/test_manual_control.py \
  tests/test_motion.py tests/test_dashboard_status.py
```

## desktop-app baseline (Task 1 step 4)

- `yarn install --frozen-lockfile` — OK (peer-dep warnings only, pre-existing).
- `npm run typecheck` (`tsc --noEmit`) — **PASS**, exit 0.
- `npm test` (Vitest) — **7 files / 102 tests PASS**, exit 0.

## Parity matrix (plan §1)

Statuses allowed: `planned / implemented / verified / intentionally-excluded`.
All items start `planned`; updated at each task checkpoint.

| # | Desktop capability | iPad delivery | Status |
| --- | --- | --- | --- |
| 1 | Bonjour/static-domain discovery, manual IP | NWBrowser + probe + last-known + manual | planned |
| 2 | First-run Wi-Fi setup | Native BLE + CryptoKit sealed PSK | planned |
| 3 | Bluetooth troubleshooting | Partial parity: PING/net-status/scan/connect/forget | planned |
| 4 | 3D digital twin | Reuse URDF/STL/WASM/Three.js + 20 Hz state | planned |
| 5 | 3D/camera swap | Reuse web UI; swap button always visible on touch | planned |
| 6 | Camera | User-initiated; GStreamer WebRTC video-only | planned |
| 7 | DoA indicator | From 8000 state WS | planned |
| 8 | Microphone waveform | intentionally-excluded (audio track removed upstream; must not fake) | planned |
| 9 | Speaker/mic volume | Native HTTP bridge to existing APIs | planned |
| 10 | Daemon logs, compact/fullscreen | Direct fixed logs WS; collapsed default; sanitized copy | planned |
| 11 | Expressions, dances | UI reuse; execution via 8042 `/api/motion` | planned |
| 12 | Controller (touch + gamepad) | Touch stays web; native GCController event bridge; 8042 manual lease | planned |
| 13 | YRobot status | 8042 `/api/status`; explicit offline state | planned |
| 14 | XIAOZHI/QWEN backend switch | Save and explicit restart separated | planned |
| 15 | QWEN voice, real preview | Reuse voice API; PCM via Web Audio | planned |
| 16 | VAD settings | Reuse 8042 API; no fake success | planned |
| 17 | Recent chat | Parse all four QWEN/XIAOZHI markers | planned |
| 18 | Visible power button | Safe equivalent: disconnect / sleep via 8042 | planned |
| 19 | USB, Simulation, local sidecar | intentionally-excluded (N/A on iPad) | planned |
| 20 | HF App Store, deep link install | intentionally-excluded (removed in fork) | planned |
| 21 | Tauri updater, daemon/system update | intentionally-excluded (TestFlight manages app) | planned |
| 22 | Env reset, forget-all, fake telemetry | intentionally-excluded (dangerous/dead) | planned |
| 23 | system/HA/privacy cards, snapshot, power dialogs | intentionally-excluded (dead code) | planned |

Static release gate (from plan §1): the iPad import graph and generated bundle must
not contain `/api/move/ws/set_target`, `/api/move/set_target`, `/update/start`, or
`environment_overrides`.
