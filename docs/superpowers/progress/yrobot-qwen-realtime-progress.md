# YRobot Qwen Realtime Project Progress

**Last updated:** 2026-08-10

## Objective

Add a YRobot setting that selects exactly one conversation backend:
`XIAOZHI` or `QWEN`. QWEN uses `qwen3.5-omni-flash-realtime`, supports
multilingual realtime speech and interruption, and executes only allowlisted
local tools.

## Current status

**Phase:** Reviewed QWEN implementation is deployed. Production remains on
XIAOZHI while robot-local QWEN and Home Assistant credentials are configured.

## Completed

- Confirmed the official Reachy Mini daemon is running at `192.168.1.14:8000`,
  version 1.9.0, with motors enabled, media available, and a healthy ~49 Hz
  control loop.
- Confirmed the production app is `/home/pollen/YRobot`, run by
  `yrobot.service`, with environment from `/home/pollen/.config/yrobot/ha.env`.
- Confirmed the model `qwen3.5-omni-flash-realtime` supports realtime audio,
  semantic interruption, multilingual speech, and WebSocket Function Calling.
- Approved restart-based XIAOZHI/QWEN switching with no silent fallback.
- Wrote and committed the design as `a4f238e`.
- Audited the live dirty working tree and documented that implementation must
  not overwrite its uncommitted restructure or `.bak` files.
- Probed `192.168.1.200:8766`: `/health` and `/weather` work, but `/mcp` and
  `/sse` do not. It is currently treated as a REST tool adapter, not a standard
  MCP endpoint.
- Wrote the implementation plan at
  `docs/superpowers/plans/2026-08-10-yrobot-qwen-realtime-backend.md`.
- Committed the plan and this progress log on production as `ba847a0`; no
  runtime source or service configuration was included in that commit.
- Created isolated worktree `/home/pollen/YRobot-qwen-realtime` on branch
  `codex/qwen-realtime`.
- Mirrored the active production source into the worktree while excluding
  `.env`, `.venv`, caches, bytecode, `.bak` files, egg-info, and credential-
  named artifacts. The dry comparison reported zero residual differences.
- Committed that secret-free active runtime snapshot as `2a42070`.
- Verified the active baseline configuration and motion suites: `40 passed`.
- Added validated `xiaozhi|qwen` configuration, secret-safe QWEN settings,
  restart-required backend APIs, runtime status, and a two-choice dashboard
  selector in feature commit `41f1e61`.
- Verified Task 2 with `48 passed`, `git diff --check`, a clean added-line
  credential scan, and a live browser render. The browser showed exactly two
  enabled choices, highlighted XIAOZHI correctly, and logged no warnings or
  errors.
- Added the isolated QWEN WebSocket protocol client in `31474fb`. It fixes the
  model query to `qwen3.5-omni-flash-realtime`, keeps Bearer authorization in
  the request header, configures PCM/transcription/semantic VAD/tools, drops
  cancelled audio, and returns bounded Function Calling results.
- Updated the plan to match the current official protocol: tool output is
  followed by plain `{"type":"response.create"}` because modalities belong
  in `session.update`. The legacy DashScope domain remains officially
  functional, so a Workspace ID is not required for the initial deployment.
- Verified Task 3 with `57 passed`; Ruff passed for both new protocol files,
  `git diff --check` passed, and no real provider request was made.
- Added mutually exclusive startup routing and the Reachy QWEN PCM runtime in
  `abf2573`. QWEN starts in manual transcription mode, explicitly preserves
  `你好小白`, switches to semantic VAD only after wake, and returns to the
  wake gate after 60 seconds.
- Added bounded 24 kHz mono PCM playback. Interruption drains queued audio and
  terminates the current `aplay` process so buffered speech does not continue.
  The official Reachy daemon remains untouched.
- Verified Task 4 with `65 passed`; Ruff passed for the QWEN protocol/runtime
  files and `git diff --check` passed. A temporary test hang was traced to an
  incomplete fake Reachy object lacking `.media`; the test fixture was fixed
  and fail-fast safe-mode handling now prevents recurrence.
- Added the fixed Function Calling boundary in `eac6daf`: `get_weather`,
  `get_device_state`, and `control_allowed_device` are the only schemas.
  Appliance arguments use local friendly names, never raw entity IDs,
  services, or credentials.
- Added zero-network rejection for unknown tools/devices and locks, covers,
  alarms, gas, and heating classes. Function `call_id` execution is idempotent,
  local results are bounded, and provider replies cannot expose the HA token.
- Verified Task 5 with `79 passed` and clean focused Ruff. Live read-only REST
  checks returned healthy status and current Guangzhou weather from
  `192.168.1.200:8766`; it remains documented as REST rather than MCP.
- Completed deployment preflight without displaying secret values. The QWEN
  API key and Home Assistant URL/token are not configured; the existing Home
  Assistant whitelist file is present.
- Backed up the exact production targets to
  `/home/pollen/.local/state/yrobot/backups/qwen-realtime-20260810-170954`.
  The 11-file backup manifest SHA-256 is
  `a1340d1a3f23eff9ca6f2c26930c843dda69b459d40982027b9ed7205a9371e6`.
- Deployed the reviewed files with zero copy mismatches. Production verification
  passed all 79 focused tests, focused Ruff, and `git diff --check`.
- Restarted `yrobot.service` once at 2026-08-10 17:10:30+08, intentionally
  retaining XIAOZHI. The YRobot service and official daemon are active; XIAOZHI
  is connected, the motion loop is approximately 50 Hz with no target failures,
  and consecutive camera frames changed.
- At 17:15:22, a LAN client at `192.168.1.181` explicitly called
  `PUT /api/audio/input`; the service then logged `xiaozhi paused: mic input
  disabled`. This is the current reason the backend reports `paused`, not a
  WebSocket, motion, camera, or daemon failure. No second restart was performed.
- Confirmed all three required robot-local QWEN/HA settings are configured and
  that `ha.env` mode remains `0600`, without reading or recording their values.
- Selected QWEN, enabled microphone input, and restarted `yrobot.service` once
  at 2026-08-10 17:34:32+08. QWEN reports configured/running `qwen`,
  connection `connected`, and no error; the official daemon remains active.
- After the QWEN restart, motion is ready and alive at about 50 Hz with zero
  target failures, and the post-restart error-level journal count is zero.
- Diagnosed the first QWEN runtime failure as a duplicate `response.create`:
  a second wake phrase received during an already active semantic-VAD session
  reactivated the wake gate. The provider closed the conversation with
  `Conversation already has an active response`.
- Added a failing regression test, then changed `WakeGate` to ignore repeated
  wake transcripts while already active. The focused runtime suite passed, then
  the full 79-test focused suite and focused Ruff passed. Feature fix commit:
  `5c6bd29`; production fix commit: `a6bfb02`.
- Backed up the two changed production files at
  `/home/pollen/.local/state/yrobot/backups/qwen-response-guard-20260810-174027`
  (manifest SHA-256
  `52baf212e2dbbc2a66c4587057c8d78a70cc3819e603c64d58d530dab24d11fe`).
- Restarted into the repaired QWEN runtime at 2026-08-10 17:42:00+08. It now
  reports configured/running `qwen`, `connected`, and no error; motion is
  alive at about 52.5 Hz with zero target failures and no error-level journal
  entries after startup.
- Camera capture is presently disabled (`running=false`) by dashboard state,
  so `/api/camera/frame` returns 404. This is separate from the QWEN fix and
  must be enabled before camera acceptance is marked complete.
- Diagnosed silent QWEN speech with direct ALSA evidence: the system default
  PCM is `null`, while QWEN's `aplay` command had no device argument. QWEN
  text responses and PCM delivery were therefore present but discarded.
- Added a failing test requiring QWEN playback to target
  `reachymini_audio_sink`, then added only `-D reachymini_audio_sink` to the
  `aplay` command. The runtime suite, full focused suite, and focused Ruff
  passed. Feature fix commit: `9a098df`; production fix commit: `4190180`.
- Backed up the two changed files at
  `/home/pollen/.local/state/yrobot/backups/qwen-audio-sink-20260810-174517`
  (manifest SHA-256
  `7a7f0423196742aebad8597c1c7f0d9ea37d227cff034cc17109efac53f42d4d`).
- Restarted QWEN after the audio fix. It returned to `connected` with no error;
  motion was about 49.9 Hz with zero target failures and no error-level journal
  entries. A short 440 Hz test tone was sent to `reachymini_audio_sink`; audible
  confirmation from the operator is still required.
- The direct speaker test then exposed the remaining format mismatch:
  `reachymini_audio_sink` accepts 16 kHz stereo, while QWEN supplies 24 kHz
  mono. The direct command failed with `Channels count non available`.
- Verified ALSA's `plug:reachymini_audio_sink` opens at QWEN's 24 kHz mono
  format and converts to the hardware format. A new failing regression test was
  added, then the QWEN playback device was changed to that conversion endpoint.
  The runtime suite, full focused suite, and focused Ruff passed. Feature fix
  commit: `60bd98d`; production fix commit: `b24af62`.
- Backed up the two changed files at
  `/home/pollen/.local/state/yrobot/backups/qwen-audio-convert-20260810-174913`
  (manifest SHA-256
  `af11e3d7304322dac60f0de3db03e72e95c0713cdd80145e85eb0e551b8c75c4`).
- After the conversion restart, QWEN returned to `connected` with no error;
  motion was about 51.4 Hz with zero target failures and no error-level journal
  entries. A 24 kHz mono 660 Hz tone was sent through the exact QWEN output
  path; the operator confirmed it was audible.
- The operator then confirmed QWEN spoken replies are audible and normal
  conversation works. This completes the baseline realtime speech acceptance.

## Decisions that must remain stable

- Do not factory-reset the robot or modify the official daemon.
- Keep `你好小白` explicitly registered.
- Default to XIAOZHI when no backend setting exists.
- Switching is save + explicit YRobot restart, not a hot swap.
- QWEN failure stays visible and never silently activates XIAOZHI.
- Model is fixed to `qwen3.5-omni-flash-realtime`.
- Secrets remain robot-local and are never shown by the dashboard.
- Appliance execution is deterministic and whitelist-bound.
- Do not expose generic MCP-discovered tools to the model.

## Active risks

- Production contains extensive uncommitted source changes and tracked
  deletions. Implementation must use an isolated worktree built from a clean
  snapshot of the active runtime.
- The currently known Hermes endpoint is REST, not a verified MCP transport.
  Native MCP integration remains gated on an exact endpoint and contract.
- Real interruption quality depends on Reachy speaker echo behavior; automatic
  tests cannot replace an audible ten-interruption hardware test.
- Live QWEN speech, interruption, language switching, and appliance control
  need a person beside the robot for audible and physical confirmation. This is
  a hardware acceptance step, not a source-code or daemon blocker.
- Existing full pytest discovery references modules currently deleted from the
  production working tree. Feature-specific tests are authoritative until that
  unrelated migration is reconciled.
- Camera hardware verification remains incomplete while dashboard capture is
  disabled; do not infer camera health from the active official media daemon.

## Next action

Perform the physical acceptance sequence with QWEN: say `你好小白`, converse in
Chinese then English then Chinese, interrupt playback ten times, ask for the
weather, operate one explicitly allowlisted low-risk light, and confirm an
unknown device plus a blocked class take no action. Record the audible and
physical observations, re-enable camera capture and verify changing frame
hashes, before testing rollback to XIAOZHI.

## Verification log

| Date | Stage | Evidence | Result |
|---|---|---|---|
| 2026-08-10 | Official daemon | `/api/daemon/status`, `/api/media/status`, `/api/motors/status` | Healthy |
| 2026-08-10 | YRobot source audit | SSH read-only inspection of branch and working tree | Dirty tree preserved |
| 2026-08-10 | Hermes endpoint | `/health` 200; `/weather` live; `/mcp` and `/sse` 404 | REST only |
| 2026-08-10 | Design | Commit `a4f238e` | Approved |
| 2026-08-10 | Plan and progress docs | Production commit `ba847a0` | Saved |
| 2026-08-10 | Isolated runtime baseline | Worktree commit `2a42070`; snapshot residual 0; secret scan clean | Saved |
| 2026-08-10 | Active baseline tests | `tests/test_config.py tests/test_motion.py` | 40 passed |
| 2026-08-10 | Historical test drift | `test_audio`, `test_app_config`, `test_main` import symbols removed by the pre-existing runtime restructure | Recorded; outside current scope |
| 2026-08-10 | Backend selector tests | `test_backend_selection.py test_config.py test_motion.py` | 48 passed |
| 2026-08-10 | Backend selector UI | Isolated port 18114, browser DOM/screenshot/console | Two choices rendered; no browser errors |
| 2026-08-10 | Task 2 lint | Ruff reports 11 pre-existing findings in the active baseline; no new finding remains from Task 2 | Recorded; unrelated code untouched |
| 2026-08-10 | Task 2 implementation | Feature commit `41f1e61`; added-line secret scan clean | Saved |
| 2026-08-10 | QWEN protocol tests | Fake WebSocket plus prior focused suites | 57 passed |
| 2026-08-10 | QWEN protocol lint | `ruff check yrobot/qwen_realtime.py tests/test_qwen_realtime.py` | Passed |
| 2026-08-10 | Task 3 implementation | Feature commit `31474fb`; added-line secret scan clean | Saved |
| 2026-08-10 | QWEN runtime tests | Routing, key guard, wake gate, 60-second expiry, playback interruption plus prior suites | 65 passed |
| 2026-08-10 | QWEN runtime lint | QWEN protocol/runtime files and tests | Passed |
| 2026-08-10 | Task 4 implementation | Feature commit `abf2573`; added-line secret scan clean | Saved |
| 2026-08-10 | QWEN tool tests | Security, REST, HA mapping, timeout, bounded output plus prior suites | 79 passed |
| 2026-08-10 | Hermes REST live read | `/health`; `/weather?city=广州` | Healthy; live weather returned |
| 2026-08-10 | Task 5 implementation | Feature commit `eac6daf`; added-line secret scan clean | Saved |
| 2026-08-10 | Deployment credential preflight | QWEN key and HA URL/token absent; HA whitelist present | XIAOZHI retained |
| 2026-08-10 | Production backup | 11 targets; manifest SHA-256 `a1340d1a3f23eff9ca6f2c26930c843dda69b459d40982027b9ed7205a9371e6` | Saved |
| 2026-08-10 | Production deployment | Feature-to-production comparison | Zero mismatches |
| 2026-08-10 | Production verification | Focused suite, Ruff, diff check | 79 passed; clean |
| 2026-08-10 | XIAOZHI restart validation | Service/API/motion/daemon/camera/journal | Connected and healthy |
| 2026-08-10 | Later XIAOZHI pause | LAN `PUT /api/audio/input`; journal says mic input disabled | Expected pause; no restart |
| 2026-08-10 | QWEN credential preflight | All three required entries configured; `ha.env` mode `0600`; whitelist present | Ready |
| 2026-08-10 | QWEN activation | One YRobot restart; backend API reports QWEN connected/no error | Passed |
| 2026-08-10 | QWEN post-restart health | Official daemon active; motion ~50 Hz; target failures 0; journal errors 0 | Passed |
| 2026-08-10 | Duplicate response regression | Test first failed, then QWEN runtime suite, full focused suite, Ruff | Fixed; `5c6bd29` / `a6bfb02` |
| 2026-08-10 | QWEN repaired activation | QWEN connected/no error; motion 52.5 Hz; target failures 0; journal errors 0 | Passed |
| 2026-08-10 | Camera state | Dashboard capture `running=false`; frame endpoint 404 | Pending enable and hash check |
| 2026-08-10 | QWEN audio-sink regression | Test first failed, then runtime suite, full focused suite, Ruff | Fixed; `9a098df` / `4190180` |
| 2026-08-10 | QWEN post-audio restart | Connected/no error; motion 49.9 Hz; target failures 0; journal errors 0 | Passed |
| 2026-08-10 | Speaker path | 440 Hz tone sent to `reachymini_audio_sink` | Pending audible confirmation |
| 2026-08-10 | QWEN audio conversion regression | Direct sink rejected 24 kHz mono; test first failed, then suites and Ruff passed | Fixed; `60bd98d` / `b24af62` |
| 2026-08-10 | QWEN converted audio path | 24 kHz mono 660 Hz tone sent to `plug:reachymini_audio_sink` | Operator confirmed audible |
| 2026-08-10 | QWEN baseline speech acceptance | Wake, spoken reply, and normal conversation | Operator confirmed working |
| 2026-08-10 | Remaining QWEN acceptance | Interruption, multilingual switching, allowlisted appliance state, camera | Pending targeted checks |

## Update protocol

After every implementation task, append:

1. task and commit;
2. files changed;
3. exact tests and results;
4. live deployment evidence, if any;
5. blocker or risk change;
6. next action.

Never record API keys, tokens, passwords, or unredacted credential values.
