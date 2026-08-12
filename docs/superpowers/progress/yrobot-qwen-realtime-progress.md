# YRobot Qwen Realtime Project Progress

**Last updated:** 2026-08-11

## Objective

Add a YRobot setting that selects exactly one conversation backend:
`XIAOZHI` or `QWEN`. QWEN uses `qwen3.5-omni-flash-realtime`, supports
multilingual realtime speech and interruption, and executes only allowlisted
local tools.

## Current status

**Phase:** QWEN is deployed and selected on the robot. Current work is live
voice-control hardening from physical ASR evidence.

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
- The operator confirmed QWEN automatically switched languages during a real
  multilingual conversation. Multilingual acceptance is complete.
- Diagnosed delayed interruption from the conversation timeline: after model
  generation completes, buffered PCM can still be playing locally. A later
  `speech_started` event previously did not invoke the flush callback because
  there was no active cloud response left to cancel.
- Added a failing regression test for speech beginning after `response.done`,
  then made every `input_audio_buffer.speech_started` event flush the local
  playback path immediately while retaining cloud cancellation when applicable.
  The full focused suite and focused Ruff passed. Feature fix commit:
  `07ca1b2`; production fix commit: `d42910a`.
- Backed up the two changed files at
  `/home/pollen/.local/state/yrobot/backups/qwen-interrupt-flush-20260810-180127`
  (manifest SHA-256
  `3f7dc03dd5c806ed446ff630d521b8a6b72d454e227ff6d0d6f5aeedd32ac4cd`).
- After the interruption repair restart, QWEN returned to `connected` with no
  error; motion was about 50.1 Hz with zero target failures and no error-level
  journal entries. Physical interruption confirmation remains pending.
- The operator tested the repaired interruption path and confirmed it works.
  QWEN speech now stops instead of finishing buffered old content.
- Camera capture was disabled by dashboard state rather than broken. It was
  enabled for acceptance; capture resumed with `captured=2`, zero failures,
  and two consecutive frame hashes differed. QWEN remained connected and the
  motion loop remained about 50 Hz with zero target failures.
- Before appliance acceptance, `YROBOT_HA_ENABLED` was found disabled. The
  operator-selected test of `书台灯` authorized enabling this robot-local
  whitelist switch; its environment file mode remains `0600`. QWEN reconnected
  after the required restart, and a read-only whitelist call confirmed the
  current `书台灯` state is `on`.
- Diagnosed the volume-command failure from logs: QWEN ASR emitted partial
  fragments such as `音响音`, while YRobot immediately injected a no-match
  failure into the active response. That early cancellation prevented the
  later fragment from completing `音响音量调大一下`.
- Added a failing regression test for unmatched spoken-control fragments, then
  changed no-match handling to stay silent and wait for more ASR fragments.
  Successful local tool execution still injects the real result back to QWEN.
- Increased the recent ASR fragment aggregation window to 3.0 seconds and
  added coverage for joining `音响音` + `量调大一下`.
- Added coverage that the joined phrase `音响音量调大一下` raises the local
  speaker volume, while the existing `音响`-only safety test still prevents
  ambiguous bare speaker control.
- Classified `timed out during opening handshake` as a reconnectable QWEN
  error so transient provider handshakes do not push YRobot into safe mode.
- Production verification on 2026-08-11 passed 6 focused tests:
  handshake-timeout reconnect, unmatched-fragment no-op, longer ASR join,
  active-response reconnect, wake resume after reconnect, and joined-ASR
  speaker-volume up.
- Restarted YRobot by loading `/home/pollen/.config/yrobot/ha.env` explicitly.
  The previous detached process was not managed by `systemctl --user`, and a
  manual restart without `ha.env` falls back to XIAOZHI. Current live status:
  `service.state=active`, `runtime.backend=qwen`, `runtime.ws_state=connected`,
  Home Assistant enabled/configured, mic input enabled, official daemon running.
- Fixed Dashboard log freshness after detached restarts. `/api/logs` previously
  read only `journalctl -u yrobot.service`, while the active QWEN process was
  started as a detached Python process writing `/tmp/yrobot-main.log`; this made
  "运行 LOG" and "最近对话" appear stale. `LogReader` now prefers the current
  process log file and falls back to journal for service-managed deployments.
  Empty `qwen stt:` lines are filtered out of "最近对话".
- Diagnosed failed voice volume control: QWEN heard phrases such as
  `音量到二十`, but the local spoken-control path only supported relative
  volume changes (`调大` / `调小`). QWEN then verbally claimed success while
  ALSA stayed at 98%. Added local parsing for absolute volume expressions
  such as `音量到二十` and `音响音量调到10`, returning a real `volume_set`
  result. A direct deployed `ToolExecutor + VolumeController` check set the
  robot speaker to 20% and `/api/status` reported `volume_percent=20`.
- Split volume intent ownership after operator clarification. `音响/音箱/Sonos`
  volume commands now route to `control_sonos`; `电视音量` no longer controls
  Reachy ALSA; Reachy's own ALSA volume only responds to explicit robot
  targets such as `你的音量`, `小白音量`, `机器人音量`, `你的声音`. Targeted tests
  cover robot volume up/down/set, Sonos routing without ALSA writes, and TV
  volume not touching ALSA. A direct deployed check confirmed `你的音量到二十`
  writes ALSA while `电视音量到三十` returns no local ALSA action.
- Follow-up log review showed QWEN still verbally claimed success after local
  no-match for `你的音量七十`, `音响一十`, and `电视音量到二十`. Added support
  for numeric volume commands without `到/调到`, routed `音响一十` to Sonos,
  and changed unconnected TV volume commands to return an explicit local
  failure so QWEN receives a truthful result instead of inventing success. A
  deployed check set Reachy ALSA from 85% to 70% with `你的音量七十`;
  `电视音量到二十` returned `电视音量尚未接入本地控制` and left ALSA at 70%.
- Sonos live log review showed `音响二十` reached `control_sonos`, but Hermes
  returned a clarification because the prompt was too terse. Direct Hermes
  probing showed only Arabic-number prompts with `调到` are reliable; Chinese
  `二十` could be misapplied as 50%. YRobot now normalizes Sonos absolute
  volume commands to `音响音量调到<N>` before calling Hermes. A deployed check
  confirmed `execute_spoken_control("音响二十")` returns `好的，音响音量已调到 20%`.
- Further ASR review for the phrase `音箱音量 20` showed QWEN heard
  `音响音量二`, causing Sonos to be set to 2%. YRobot now treats this narrow
  Sonos-only truncated single Chinese digit form as tens, so `音响音量二`
  normalizes to `音响音量调到20`. Incomplete Sonos volume targets such as
  `音响音量` now return `没听清音响音量要调到多少` instead of letting QWEN invent
  a successful adjustment. Deployed checks confirmed `音响音量二` returns
  `好的，音响音量已调到 20%` and `音响音量` returns the explicit failure.

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
  deletions. Implementation must keep surgical commits scoped to the touched
  QWEN/HA files and avoid cleaning unrelated work.
- The currently known Hermes endpoint is REST, not a verified MCP transport.
  Native MCP integration remains gated on an exact endpoint and contract.
- Real interruption quality depends on Reachy speaker echo behavior; automatic
  tests cannot replace an audible ten-interruption hardware test.
- ASR still depends on QWEN's provider output. The current fix prevents YRobot
  from prematurely failing on fragments; it does not force QWEN to transcribe
  every word perfectly.
- Existing full pytest discovery references modules currently deleted from the
  production working tree. Feature-specific tests are authoritative until that
  unrelated migration is reconciled.
- Camera hardware verification passed after dashboard capture was enabled.

## Next action

Ask QWEN beside the robot: `你好小白，音响小一点`, then
`音响音量调大一下`. Confirm the logs show either a joined full command or a real
tool execution, not an immediate no-match failure on `音响音`.

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
| 2026-08-10 | Camera state | Dashboard capture `running=false`; frame endpoint 404 | Diagnosed as disabled |
| 2026-08-10 | QWEN audio-sink regression | Test first failed, then runtime suite, full focused suite, Ruff | Fixed; `9a098df` / `4190180` |
| 2026-08-10 | QWEN post-audio restart | Connected/no error; motion 49.9 Hz; target failures 0; journal errors 0 | Passed |
| 2026-08-10 | Speaker path | 440 Hz tone sent to `reachymini_audio_sink` | Pending audible confirmation |
| 2026-08-10 | QWEN audio conversion regression | Direct sink rejected 24 kHz mono; test first failed, then suites and Ruff passed | Fixed; `60bd98d` / `b24af62` |
| 2026-08-10 | QWEN converted audio path | 24 kHz mono 660 Hz tone sent to `plug:reachymini_audio_sink` | Operator confirmed audible |
| 2026-08-10 | QWEN baseline speech acceptance | Wake, spoken reply, and normal conversation | Operator confirmed working |
| 2026-08-10 | QWEN multilingual acceptance | Real conversation automatically switched languages | Operator confirmed working |
| 2026-08-10 | QWEN interruption regression | Speech after `response.done` did not flush local PCM; test first failed then focused suite and Ruff passed | Fixed; `07ca1b2` / `d42910a` |
| 2026-08-10 | QWEN interruption post-restart | Connected/no error; motion 50.1 Hz; target failures 0; journal errors 0 | Ready for operator test |
| 2026-08-10 | QWEN interruption acceptance | Long reply interrupted by new user speech | Operator confirmed working |
| 2026-08-10 | Camera acceptance | Capture enabled; `captured=2`; failures 0; frame hashes changed | Passed |
| 2026-08-10 | HA whitelist enablement | `YROBOT_HA_ENABLED=true`; file mode 0600; QWEN reconnected | Passed |
| 2026-08-10 | 书台灯 initial state | Read-only allowlisted HA call | `on` |
| 2026-08-10 | HA QWEN policy regression | Voice request was transcribed in fragments and QWEN replied without tool use; a failing regression test showed the enabled HA policy was omitted from session instructions | Diagnosed |
| 2026-08-10 | HA QWEN policy fix | Session now includes `Settings.effective_system_prompt`; focused QWEN/tool/runtime suite and full focused suite passed (83 tests); Ruff passed; feature/production commits `4086742` / `33cf690`; production backup `qwen-ha-policy-20260810-181643` | Deployed; QWEN connected |
| 2026-08-10 | HA voice tool-call regression | QWEN heard `关闭书灯` and replied `正在关闭书台灯`, but read-only HA state stayed `on`; regression test first failed because `response.done.response.output[].function_call` was ignored | Diagnosed |
| 2026-08-10 | HA voice tool-call fix | `response.done` function calls now execute through the existing local whitelist tool path; targeted QWEN/tool/runtime/config/backend suite passed (74 tests); Ruff passed; feature/production commits `fd280b2` / `8931388`; service restarted and QWEN reconnected | Pending repeated voice utterance |
| 2026-08-10 | HA local spoken-control fallback | QWEN again replied `关闭书台灯` without a tool event; HA state remained `on`. Added local deterministic execution for exact allowlisted phrases and added `书灯` aliases for `书台灯`; tests first failed, then QWEN/tool/runtime/config/backend suite passed (76 tests), `py_compile` passed, narrowed Ruff passed; feature/production commits `c5eb9eb` / `ce35ed7`; service restarted and QWEN reconnected | Pending repeated voice utterance |
| 2026-08-10 | HA short ASR aliases | Repeated voice attempts were transcribed as `关闭书` / `关闭书台`, so exact allowlisted phrase matching did not fire; added `关闭书`, `关闭书台`, `关掉书台`, and open-side `打开书台` aliases only for the selected allowlisted `书台灯`; service restarted, QWEN reconnected, aliases loaded, pre-test HA state still `on` | Pending repeated voice utterance |
| 2026-08-10 | HA voice acceptance | Spoken `关闭书` triggered local whitelist `turn_off`; spoken `打开书台` triggered local whitelist `turn_on`; QWEN replied for both actions; final read-only HA state is `on`, matching the last open command; operator replied `好了` | Passed |
| 2026-08-10 | QWEN wake failure after idle | Operator reported 小白 could not wake; status showed `safe_mode`, QWEN `ws_state=error`, and `last_error='Your session was closed because no response was generated for 300 seconds.'`; mic input was enabled and available | Diagnosed |
| 2026-08-10 | QWEN idle reconnect fix | Immediate narrow recovery via `yrobot.service` restart restored QWEN connected; added reconnect handling for idle session-close errors so QWEN rebuilds the WebSocket instead of entering safe mode; regression test first failed, then focused QWEN/tool/runtime/config/backend suite passed (77 tests), `py_compile` passed, narrowed Ruff passed; feature/production commits `b1e7c35` / `4fdb792`; service restarted and QWEN reconnected | Ready for operator wake test |
| 2026-08-10 | 落地扇 voice alias repair | Operator reported `关闭风扇` failed; logs showed ASR recognized `关闭风`, QWEN replied verbally, HA state stayed `on`, and no local whitelist execution occurred. Added `关闭风`, `关掉风`, and `打开风` aliases only for allowlisted `落地扇`; service restarted, QWEN reconnected, aliases loaded, pre-test HA state still `on` | Pending repeated voice utterance |
| 2026-08-10 | QWEN local-control reply policy | Operator reported fan control actually executed but QWEN replied `抱歉，我无法控制这个设备`; logs confirmed `关闭风扇` and `打开风扇` both triggered local whitelist success while final HA state was `on`. Updated HA prompt policy to state that local whitelist handles appliance control and QWEN must not answer `无法控制` without a tool failure; regression test first failed, then focused QWEN/tool/runtime/config/backend suite passed (77 tests), `py_compile` passed, F401 Ruff passed; feature/production commits `0a190c3` / `26def16`; service restarted and QWEN reconnected | Ready for operator reply test |
| 2026-08-10 | 吸顶灯 voice alias repair | Operator reported `吸顶灯` could not be controlled; HA read-only state was `off`, whitelist existed, but logs showed ASR shortened the utterance to `打开西` and no local whitelist execution occurred. Added `打开西`, `打开吸`, `打开顶灯`, `关闭西`, `关闭吸`, and `关闭顶灯` aliases only for allowlisted `吸顶灯`; service restarted, audio input re-enabled, QWEN reconnected, aliases loaded | Pending repeated voice utterance |
| 2026-08-10 | 吸顶灯 second ASR alias repair | Repeated open test still failed; logs showed ASR outputs `打开C`, bare `打开`, and `打开系统`, with HA state still `off` and no local whitelist execution. Added only the specific observed aliases `打开C` and `打开系统` for allowlisted `吸顶灯` while intentionally not adding bare `打开`; service restarted, QWEN reconnected, aliases loaded, pre-test state still `off` | Pending repeated voice utterance |
| 2026-08-10 | QWEN 1011 internal-error reconnect | Operator pasted `ConnectionClosedError: received 1011 ... Internal service error: null`; status showed `safe_mode`, QWEN `ws_state=error`, `last_error='Internal service error: null'`. Added `internal service error` to reconnectable QWEN errors; regression test first failed, then focused QWEN/tool/runtime/config/backend suite passed (78 tests), `py_compile` passed, narrowed Ruff passed; feature/production commits `76c5d6c` / `57d3c6d`; service restarted and QWEN reconnected | Ready for operator retry |
| 2026-08-10 | QWEN ASR fragment joining and VAD lowering | Operator noted clear loud speech still recognized as truncated phrases such as `关闭餐`; evidence showed runtime VAD was high at `0.116` while code default in `audio.py` was `0.11`. Added a 1.5 s `RecentTranscriptWindow` so local HA matching tries current STT and recent joined fragments, without adding ambiguous bare commands; lowered `audio.py` default VAD to `0.065` and set runtime VAD to `0.065`. Regression tests first failed, then focused QWEN/tool/config/backend suite passed (80 tests), `py_compile` passed, narrowed Ruff passed; feature/production commits `bd5e482` / `aa97eba`; service restarted and QWEN connected | Ready for operator retry |
| 2026-08-10 | 走廊灯 safety removal | Operator reported `走廊灯` has an electrical/power issue. Read-only check confirmed two allowlist entries for `走廊灯` mapped to `switch.xiaomi_cn_2102538340_w1_on_p_2_1`; backed up `~/.config/yrobot/home_assistant_whitelist.json`, removed both on/off entries, verified `走廊` no longer appears in the allowlist JSON, and restarted only the YRobot Python process with the saved QWEN/HA environment. QWEN reconnected with audio input enabled and no runtime error | Passed; do not voice-control 走廊灯 |
| 2026-08-10 | 走廊灯 restore and close repair | Operator clarified the prior issue was not electrical safety: `走廊灯` can open but could not close. Direct HA `switch.turn_off` on `switch.xiaomi_cn_2102538340_w1_on_p_2_1` returned HTTP 200 and changed HA state from `on` to `off`, proving the entity/service can close. Restored two `走廊灯` allowlist entries, added specific close aliases `关闭走廊`, `关掉走廊`, `走廊关闭`, and `走廊关灯` without adding bare `关闭`; restarted only the YRobot Python process with QWEN/HA env. QWEN reconnected; local executor validation for `关闭走廊灯`, `关闭走廊`, and `走廊关灯` returned success and HA state stayed `off` | Ready for voice open/close acceptance |
| 2026-08-10 | 厨房灯 bare-close follow-up | Operator reported `打开厨房灯` works but `关闭厨房灯` fails. Logs showed QWEN ASR recognized the close request as only `关闭。`, so the exact allowlist did not match and HA state remained `on`. Added a guarded local follow-up rule: bare `关`/`关闭`/`关掉` only controls the last successfully spoken device, only for `turn_off`, and only within 30 seconds; bare `打开` is still ignored. Regression tests first failed, then focused QWEN/tool/runtime/config/backend suite passed (84 tests), `py_compile` passed, Ruff passed; feature/production commits `b5e2be4` / `7dcda03`; service restarted and QWEN reconnected. Local executor validation opened `厨房灯` then bare `关闭` turned it off | Ready for voice retry |
| 2026-08-10 | 卫生间灯 open ASR aliases | Operator reported `卫生间灯` could not open. Logs showed the open request was recognized as `打开卫生间`, `打开卫生间的`, and `打开卫生`, while the allowlist only had full `卫生间灯` phrases. Added only those three observed open aliases to the `卫生间灯` `switch.turn_on` whitelist entry; backed up the whitelist, restarted only the YRobot Python process with QWEN/HA env, and verified local executor opens successfully for all three phrases. Final HA state was returned to `off`; QWEN connected, input enabled, no runtime error | Ready for voice retry |
| 2026-08-10 | 卫生间灯 close ASR aliases | Operator reported `卫生间灯` open works but close has issues. Logs showed close attempts recognized as `关闭卫生`, with no local spoken-control execution and HA state still `on`. Added specific close aliases `关闭卫生`, `关掉卫生`, `卫生关闭`, and `卫生关灯` to the `卫生间灯` `switch.turn_off` whitelist entry; backed up the whitelist, restarted only the YRobot Python process, and verified local executor closes successfully for the observed aliases. Final HA state `off`; QWEN connected, input enabled, no runtime error | Ready for voice retry |
| 2026-08-11 | QWEN active-response safe-mode recovery | Operator reported logs show errors and Reachy cannot converse. Evidence: YRobot was in `safe_mode`, runtime backend `qwen`, `ws_state=error`, `last_error='Conversation already has an active response'`; official daemon, media, and motors were healthy. Logs showed wake at `23:31:32`, websocket close 1000, then active-response error and startup failure counter `8/3`. Restarted only `yrobot.service` to restore QWEN, added a regression test proving active-response errors are reconnectable, then added the minimal reconnect classifier entry. Targeted reconnect tests passed; broader current-suite run has unrelated drift failures in VAD default, expanded tool schema, and prompt wording. Production/feature commits `fde99ce` / `645f2ea`; service restarted and QWEN connected with `last_error=null` | Ready for operator conversation retry |
| 2026-08-11 | Local speaker volume voice control | Operator said `音箱音量调大` did not work. Logs showed QWEN ASR recognized `音响大一点`, `音响一点`, `音响`, and `音响音`, but no local spoken-control action executed; `/api/volume` existed and volume was 88%. Added a local-only `ToolExecutor` volume branch with injected `VolumeController`, matching specific up/down phrases and not exposing a new model tool or HA action. Regression tests first failed, then targeted volume/QWEN tests passed; production/feature commits `18ab39d` / `4c66fb9`. After restart, local executor validation for `音箱音量调大` changed volume from 88 to 98; QWEN connected, input enabled, `last_error=null` | Ready for voice retry |

## Update protocol

After every implementation task, append:

1. task and commit;
2. files changed;
3. exact tests and results;
4. live deployment evidence, if any;
5. blocker or risk change;
6. next action.

Never record API keys, tokens, passwords, or unredacted credential values.
