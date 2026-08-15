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

- 2026-08-15: the previously user-owned local iOS changes were committed by
  explicit user instruction:
  - `35f63ae` (`chore(ios): sync Xcode project after toolchain upgrade`) —
    `project.pbxproj` and the shared `YRobotRemote.xcscheme`, rewritten by
    Xcode on open with a newer toolchain; no source changes.
  - `b974d74` (`docs: add iOS remote dual-backend implementation plan`) —
    adds `docs/plans/2026-08-12-yrobot-ios-remote-dual-backend.md`.
- Validation: diffs reviewed before staging; the plan doc was grepped for
  `token|password|secret|api[_-]?key|bearer|ssh-rsa|-----BEGIN` and only
  matched its own no-secrets requirement text, not real credential values.
- Not yet synced to `/home/pollen/YRobot`.

- 2026-08-14: repository-local agent memory was created in commit `1a8dadb`.
  It adds the durable project context, a dynamic handoff, decision records,
  this deployment runbook, and the `AGENTS.md` read-first instruction.
- Validation: `git diff --check` passed before commit; a repository scan of the
  new files found no credential values or secret environment-variable values.
- Sync: the same files were copied to `/home/pollen/YRobot`; remote checks for
  `AGENT_HANDOFF.md`, `PROJECT_MEMORY.md`, and the deployment runbook passed.

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

## Handoff update rule

After every completed stage, update this file with: affected files, the exact
verification command and result, live acceptance status, remaining work, and
the new commit. Remove superseded dynamic facts; keep durable facts in
`PROJECT_MEMORY.md` or a decision record instead.
