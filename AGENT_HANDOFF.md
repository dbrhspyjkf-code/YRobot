# YRobot agent handoff

## Read order

1. Read this file.
2. Read [`PROJECT_MEMORY.md`](PROJECT_MEMORY.md).
3. Inspect the source named by the task.
4. For Reachy-affecting work, run the relevant read-only health check before
   changing code.

## Current baseline

- Local branch: `reachy-stable-2026-08-12-qwen`.
- Baseline commit before this handoff: `c5a1b32` (`fix(qwen): greet face once per wake`).
- Deployed source: `/home/pollen/YRobot` on Reachy at `192.168.1.14`.
- Official daemon: port `8000`; YRobot Dashboard/API: port `8042`.
- User-owned local changes that must not be overwritten or staged without
  explicit scope: iOS Xcode project/scheme changes and
  `docs/plans/2026-08-12-yrobot-ios-remote-dual-backend.md`.

## Completed and verified

- QWEN backend uses `qwen3.5-omni-flash-realtime`; it is selected in the
  deployed runtime and does not fall back silently to XIAOZHI.
- QWEN speech, multilingual conversation, interruption, local HA allowlist
  control, local face management, gesture/dance commands, and narrow visual
  snapshot behavior were accepted during prior live tests. See
  `docs/reachy-mini-yrobot-ops.md` for dated evidence.
- A stable local face is greeted once per explicit wake window. Repeated face
  matches and a QWEN reconnect within the same wake window do not repeat the
  greeting.

## Known limits and pending work

- QWEN ASR quality is provider-dependent. Do not add broad phrase/device rules
  to mask an ASR error; retain narrow aliases only after observing evidence.
- The browser Dashboard has separate audio/camera controls. The iOS remote has
  existing Camera & Audio and Settings tabs. A proposed compact iOS chat
  interface with rotary volume, left-swipe audio controls, power controls, and
  video preview is not implemented yet.
- Camera preview is cached JPEG polling (currently 640 px long edge, 0.5 s
  interval), not a continuous video stream.

## Last live verification

Date: 2026-08-14 (Asia/Shanghai), read-only request:

```bash
curl -fsS --max-time 8 http://192.168.1.14:8042/api/camera/state
curl -fsS --max-time 8 http://192.168.1.14:8042/api/status
```

Observed results:

- Camera cache: `running=true`, `interval_s=0.5`, `long_edge=640`,
  `frame_bytes=19955`, `failures=0`.
- Official daemon: `running`, `awake=true`.
- YRobot audio: `input_enabled=true`, PCM volume `77%`, live microphone data
  available.
- Earlier deployment of `c5a1b32`: `yrobot.service` and the official daemon
  were both `active`; QWEN WebSocket was `connected` and `last_error=null`.

## Latest completed stage

- 2026-08-14: repository-local agent memory was created in commit `1a8dadb`.
  It adds the durable project context, a dynamic handoff, decision records,
  this deployment runbook, and the `AGENTS.md` read-first instruction.
- Validation: `git diff --check` passed before commit; a repository scan of the
  new files found no credential values or secret environment-variable values.
- Sync: the same files were copied to `/home/pollen/YRobot`; remote checks for
  `AGENT_HANDOFF.md`, `PROJECT_MEMORY.md`, and the deployment runbook passed.

- 2026-08-20 16:00 Asia/Shanghai: reconciled the deployed MQTT+UDP Xiaozhi
  runtime into local commit `df6044b`, then deployed liveness commits
  `f4873e7` and `ff4c083` through `scripts/deploy.sh`. The robot archive
  fingerprint is `752c947`; backup and verified SHA-256 manifest are at
  `/home/pollen/.local/state/yrobot/backups/deploy-20260820-160015`.
- The watchdog now ignores `mcp`, `llm`, and other control traffic. Only UDP
  audio, `stt`, or `tts` clears the first unanswered uplink deadline. Focused
  local and robot tests both passed: 31 tests across `test_uplink_vad.py`,
  `test_stability_guards.py`, and `test_xiaozhi_mqtt.py`.
- Deployment acceptance evidence: an unsolicited/noisy burst ended at
  16:00:58 without a cloud reply; at 16:01:10 the robot logged
  `no server response 12s after uplink burst`, rebuilt its MQTT+UDP session,
  and connected a new session at 16:01:14. Both YRobot and the official
  daemon were active; current status was connected with `last_error=null`.
- **Physical acceptance:** at 2026-08-20 16:18 Asia/Shanghai, the operator
  confirmed acceptance of the deployed Xiaozhi liveness fix after the spoken
  wake/query check. This closes the voice acceptance gate; retain log-based
  checks as diagnostics, not as a substitute for future physical checks.

## Working commands

Run local focused tests from the repository root:

```bash
PYTHONPATH=. uvx --from pytest pytest -q tests/test_qwen_emotion.py
PYTHONPATH=. uv run --no-project --with pytest --with opencv-python python -m pytest -q tests/test_faces.py
python3 -m py_compile yrobot/main.py yrobot/qwen_emotion.py
git diff --check
```

For iOS simulator build and tests:

```bash
cd ios/YRobotRemote
./scripts/build.sh simulator
```

Deployment specifics, service restart cautions, and health checks are in
[`docs/runbooks/deploy-yrobot.md`](docs/runbooks/deploy-yrobot.md).

## Stage log: iPad cockpit (feature/ipad-cockpit, started 2026-08-20)

- Worktree `.worktrees/ipad-cockpit`, branch `feature/ipad-cockpit`, base
  `b8e35d804c3b` (= planning baseline). Plan: `docs/plans/2026-08-20-yrobot-ipad-cockpit.md`.
- Task 1 complete: plan copied, parity baseline recorded in
  `docs/verification/ipad-parity-baseline.md`.
- Baseline test state (pre-existing, not caused by this branch): Python
  collectable subset 320 passed / 6 failed / 11 errors plus 12 uncollectable
  stale test files (list in parity doc); desktop-app `npm run typecheck` PASS
  and Vitest 102/102 PASS. `desktop-app` installs with
  `yarn install --frozen-lockfile` (yarn.lock only, no package-lock.json).
- The notes above about `ios/YRobotRemote` + `./scripts/build.sh` refer to an
  earlier Xcode experiment outside this branch's scope; the iPad branch uses
  XcodeGen under `ios/YRobotRemote` per the 2026-08-20 plan.

## Handoff update rule

After every completed stage, update this file with: affected files, the exact
verification command and result, live acceptance status, remaining work, and
the new commit. Remove superseded dynamic facts; keep durable facts in
`PROJECT_MEMORY.md` or a decision record instead.

## 2026-08-20 — iPad cockpit development (16 tasks) handoff

- Branch: `feature/ipad-cockpit` (worktree `.worktrees/ipad-cockpit`).
- Plan: `docs/plans/2026-08-20-yrobot-ipad-cockpit.md`.
- Acceptance: `docs/verification/ipad-acceptance-report.md` (operator
  gates still pending; deploy was **not** run).
- All 16 plan tasks have a checkpoint commit. iPad bundle has zero
  Tauri runtime, zero daemon `set_target`, zero secret leakage; the
  Choreographer is the only `set_target` writer via the manual lease.
- Local gates green: desktop 174 tests / 0 TS errors, iOS tests green,
  Python focused tests green, `scripts/deploy.sh --dry-run` clean.
- **Operator must confirm before any actual deploy.** See
  `docs/verification/ipad-testflight-checklist.md` for TestFlight
  inputs that only the user can provide.
