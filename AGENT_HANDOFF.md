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

- 2026-08-22 (Asia/Shanghai), repository work only: added the optional
  voice-driven photo album and OrangePi SFTP sync on `feat/reachy-photo-sync`
  (commits `a092112`, `e4b5369`, `f509da4`, `2d97f7a`, `038bfae`, `25bf569`).
  Feature worktree:
  `/Users/leenzhou/Projects/YRobot-reachy-current/.worktrees/feat-reachy-photo-sync`.
  - Touched files: `yrobot/config.py`, `yrobot/photos.py`,
    `yrobot/photos_sftp.py`, `yrobot/app_config.py`, `yrobot/main.py`,
    `yrobot/static/{index.html,main.js,style.css}`, `scripts/yrobot_photo_askpass.sh`,
    `scripts/deploy.sh`, `tests/test_photos.py`, `.env.example`, `README.md`.
  - Validation: 30 tests pass in `tests/test_photos.py`; `node --check`
    `yrobot/static/main.js`; `git diff --check` clean; `git grep` finds no
    weak host-key policy (`StrictHostKeyChecking=no|accept-new`,
    `UserKnownHostsFile=/dev/null`) and no real photo-upload password in the
    tracked tree (only the example placeholder in `.env.example`).
  - Deploy gate: `scripts/deploy.sh` now compiles `yrobot/app_config.py`
    and `yrobot/photos.py` and runs `tests/test_photos.py` locally and on the
    robot. The robot still needs operator-side secret material and verified
    SSH host key before the feature can sync anything.
  - Pending operator-side action (not part of this commit, never to be
    written back to the repository): edit
    `/home/pollen/.config/yrobot/ha.env` to set
    `YROBOT_PHOTO_UPLOAD_ENABLED=1`, `YROBOT_PHOTO_SFTP_*`, and the password
    (mode `0600`), create
    `/home/pollen/.config/yrobot/orangepi_known_hosts` from a manually
    verified host key (mode `0600`), then restart `yrobot.service`.
  - Physical acceptance still required before shipping: camera preview,
    single capture per `帮我拍张照`, OrangePi upload of both variants,
    Reachy-side spool cleanup on success, retry path while offline, dashboard
    delete after explicit confirm, both XIAOZHI and QWEN backends.

- 2026-08-22 23:48 Asia/Shanghai: deployed follow-up photo hotfixes through
  `scripts/deploy.sh`; current source commit `a6aa056`, robot archive
  fingerprint `7cfb771`, backup
  `/home/pollen/.local/state/yrobot/backups/deploy-20260822-234654`.
  - Fixes: `/api/status` now reads the same `PhotoLibrary` as the album
    routes; OpenSSH SFTP batch construction now uses valid one-line commands,
    incremental directory creation, a compatible `fetch` signature, and no
    conflicting `subprocess.run(input=..., stdin=...)` arguments.
  - Validation: local and robot focused suites passed with
    `tests/test_photos.py` and `tests/test_photos_sftp.py`; service and
    official daemon are both `active`; Dashboard `/api/status` and
    `/api/photos/status` agree on the photo queue state.
  - Current block as of that time: systemd loads
    `/home/pollen/.config/yrobot/ha.env` (mode `0600`), but it has no
    `YROBOT_PHOTO_SFTP_*` entries and the manually verified
    `orangepi_known_hosts` file does not exist. Photo upload therefore remains
    disabled. One dashboard-origin pending spool item exists; it is retained
    locally and has not attempted an upload.
  - Do not resume SFTP preflight or physical acceptance until an operator has
    entered the private settings and verified host key locally. Do not put
    those values in Git, logs, Dashboard, handoff files, or chat.

- 2026-08-23 10:59 Asia/Shanghai: completed physical acceptance of the secure
  Xiaozhi local-photo flow on feature branch `feat/reachy-photo-sync`.
  - Reachy source: `1d516a6` (`fix(xiaozhi): preserve active wake across reconnects`),
    deployed through `scripts/deploy.sh`; final backup is
    `/home/pollen/.local/state/yrobot/backups/deploy-20260823-105207`.
  - Hermes source: `a8a17b3` (`fix(vision): block delayed local-photo tool retries`),
    deployed on OrangePi. The known OrangePi-only full-suite failure in
    `tests/test_mac_bridge.py` requires a Mac-only token and is unrelated;
    relevant focused tests passed.
  - The local command has a fixed sequence: locally play “好的，现在拍”, capture
    and SFTP-sync, then locally play “拍好啦”. Xiaozhi uses the same dmix-backed
    `plug:reachymini_audio_sink` device as the fixed clips.
  - Security: a 10-second post-photo microphone quarantine drops residual
    frames before the reconnect; Hermes blocks Reachy `analyze_image` calls for
    90 seconds after a signed photo intent. Do not weaken either guard without
    a separate security review.
  - Conversation continuity: the active, verified wake state is leased across
    a short normal Xiaozhi transport reconnect; the photo quarantine still
    defers restoration until it expires.
  - Live acceptance: two local photos played both fixed prompts and uploaded;
    the final multi-turn session completed 12 TTS stops (3044 enqueued / 3034
    written frames) with no `aplay` errors and no `analyze_image`/Qwen-VL
    markers. `yrobot.service`, the official daemon, and Hermes were active.

- 2026-08-23 11:13 Asia/Shanghai: deployed Dashboard remote-album pagination
  from `f7d4f8d` (`feat(dashboard): paginate remote photo album`); backup:
  `/home/pollen/.local/state/yrobot/backups/deploy-20260823-111113`.
  - `GET /api/photos` accepts bounded `limit` and `offset`, and returns the
    public `photos` slice plus `total`, `limit`, and `offset`. It must never
    include any SFTP host/path/credential metadata.
  - The Dashboard requests six records per page, shows previous/next controls
    and `第 N / M 页`, resets to page one after a new capture, and falls back
    to the final valid page after a deletion empties the current page.
  - Production verification: seven records returned six at offset zero and one
    at offset six; the UI rendered `第 1 / 2 页`, then one card on `第 2 / 2 页`,
    and returned correctly. Both `yrobot.service` and the official daemon
    were active with no startup traceback.

- 2026-08-23 11:30 Asia/Shanghai: deployed root-level single-JPEG storage
  from `5bf0d48` (`feat(photos): store one root-level jpeg`); code backup:
  `/home/pollen/.local/state/yrobot/backups/deploy-20260823-112757`.
  - The robot-local mode-0600 `ha.env` now has exactly one
    `YROBOT_PHOTO_SFTP_REMOTE_DIR=/home/orangepi/图片/Reachy` line. Do not read,
    print, commit, or otherwise copy its other contents.
  - New captures create and upload only one root-level
    `<timestamp>_<opaque-id>.full.jpg`; no date directories or `thumb.jpg`
    files are generated. The Dashboard requests the full JPEG for a card and
    its preview.
  - Existing rows migrate with `has_thumbnail=1`, so old two-file records
    remain readable and only delete their old thumbnail if the user explicitly
    deletes that photo. Do not bulk-delete historical remote thumbnails.
  - Local and robot focused suites passed, the SQLite migration column exists,
    both services are active, and the Dashboard continues to render existing
    photos. No physical test photo was taken during this deployment; any live
    capture/upload confirmation must be explicitly approved first.

- 2026-08-23 12:03 Asia/Shanghai: deployed `9fe3b07`
  (`fix(photos): clear stale missing remote records`); code backup:
  `/home/pollen/.local/state/yrobot/backups/deploy-20260823-120317`.
  - Dashboard deletion now treats only OpenSSH's explicit remote-photo
    `No such file or directory` response as an idempotent success. It clears
    the stale SQLite row, while host-key, permission, connectivity, and local
    configuration errors still fail closed and retain the record.
  - For legacy two-file records, deletion continues to the thumbnail even when
    the full JPEG was already manually removed.
  - After a fresh image probe, 7 records whose remote full JPEGs were absent
    were cleared with the user’s explicit approval. Two readable records were
    retained; the final Dashboard API probe was `total=2`, with both image
    requests succeeding. No photo was captured and no readable remote photo
    was deleted by this cleanup.

- 2026-08-24 17:55 Asia/Shanghai: restored the photo integration that an
  uncommitted production edit had stripped, while preserving that edit's
  non-photo changes. Backup of the overwritten production file (SHA-256 in
  manifest):
  `/home/pollen/.local/state/yrobot/backups/restore-photo-20260824-175510`.
  - Incident: `/home/pollen/YRobot/yrobot/main.py` was modified on
    2026-08-23 16:21 (28 insertions / 220 deletions, never committed) removing
    PhotoLibrary startup, `/api/photos*` registration, the Xiaozhi photo
    interception, and the wake-lease/quarantine logic. After the 2026-08-24
    17:16 service restart, spoken “拍照” fell through to the Xiaozhi cloud
    (“我现在没法直接拍照”) and `/api/photos*` returned 404.
  - Restoration: rebuilt `main.py` as verified deployed `ed9fd16` content plus
    the two legitimate uncommitted additions — the Übersicht CORS middleware
    block and the `run()` try-wrap around the media assignment. The merged
    file KEEPS `xiaozhi_aplay_command()`; the production edit's hardcoded
    `aplay` argv had dropped `-D SHARED_APLAY_DEVICE`, which would have
    reintroduced the `Device or resource busy` dmix regression.
  - Validation: local and robot py_compile plus the full focused suite
    (93 tests: uplink_vad, stability_guards, xiaozhi_mqtt, photos,
    photos_sftp, photo_feedback) all green; ruff diff versus the deployed
    baseline is empty. After the orphan-safe restart, `yrobot.service` and
    the daemon are active, `/api/photos` returns `total=3`, photo sync is
    enabled with queue_depth 0, and there is no startup traceback. Spoken
    "拍照" physical acceptance still needs an on-site confirmation.
  - Robot git remains at `ed9fd16` with `main.py` modified (= deployed +
    CORS + try-wrap). A future `scripts/deploy.sh` run would overwrite the
    CORS addition; upstream the CORS middleware into the feature branch
    before the next code deploy.

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

## Pending deployment: quiet conversation motion (2026-08-24 Asia/Shanghai)

- Feature worktree: `.worktrees/quiet-conversation-motion` on `fix/quiet-conversation-motion`; commit pending at this handoff update.
- Scope: QWEN no longer exposes or dispatches `express_emotion`; Xiaozhi gateway `llm` emotion metadata and per-sentence keyword/cloud-LLM emotion classification no longer actuate the Choreographer. SPEAK/LISTEN/IDLE posture, tracking, wake/backchannel nods, explicit user requests, idle show, and Dashboard manual motions remain outside this change.
- Local verification passed: `python3 -m py_compile yrobot/main.py yrobot/qwen_realtime.py yrobot/qwen_tools.py`; `PYTHONPATH=. uvx --from pytest --with cryptography --with fastapi --with httpx --with opencv-python-headless --with websockets --with numpy --with python-dotenv --with reachy-mini pytest -q tests/test_xiaozhi_emotion.py tests/test_qwen_tools.py::test_qwen_never_exposes_or_executes_autonomous_emotion_tool tests/test_sentence_emotion_llm.py` (17 passed); `git diff --check` passed.
- The full `tests/test_qwen_tools.py` has four pre-existing failed numeric-volume expectations; they are unrelated to this action-boundary change, so deployment runs only the new targeted regression from that module.
- Before deploy, keep the live production CORS/media-start patch: it is already present in local `main` and must not be lost when the robot's current dirty `main.py` is reconciled by the standard deploy script.

## Handoff update rule

After every completed stage, update this file with: affected files, the exact
verification command and result, live acceptance status, remaining work, and
the new commit. Remove superseded dynamic facts; keep durable facts in
`PROJECT_MEMORY.md` or a decision record instead.
