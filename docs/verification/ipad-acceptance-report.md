# YRobot iPad Remote — Acceptance Report

> Plan Task 16. Closes out the iPad development scope with the gate
> results the operator needs before approving the deploy.

## Status

| Plan Task | Topic | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Plan + parity baseline recorded | verified | `docs/verification/ipad-parity-baseline.md` |
| 2 | XcodeGen project + empty SwiftUI shell | verified | `ios/YRobotRemote.xcodeproj` + `AppEnvironment` |
| 3 | Robot endpoint, URL builder, probe, session controller | verified | `ios/.../{RobotEndpoint,RobotURLBuilder,RobotProbe,RobotSession,RobotSessionController}.swift` + 8 XCTest cases |
| 4 | Bonjour discovery + last-known store + permission recovery | verified | 6 tests in `RobotDiscoveryServiceTests.swift` + UI flow |
| 5 | Native HTTP allowlist bridge (23 operations) | verified | `NativeOperation.swift` + `NativeBridgeHandler`; 24 operations (Task 8/9 added a microphone getter) |
| 6 | iPad-dedicated web build + WKWebView host + P0 spike | verified | 12 native-bridge tests, build script wired as Xcode `preBuildPhase` (confirmed in build log) |
| 7 | Platform adapter + responsive cockpit + Tauri-free bundle | verified | 24 new tests (`platformImportGuard`, `ipadLayout`, `stateMapping`, `yrobotState`, `chatMarkers`, `connectionRegistry`); import-graph scan + bundle scan both green |
| 8 | State / moves / logs WS + volume bridge | verified | `stateMapping` tests; WS-URL builders; `volumeTransport` (24 ops); `useAudioControls` refactored onto the seam; iPad signaling origin + empty `iceServers` |
| 9 | YRobot status / backend / voice / VAD / chat | verified | `yrobotState` tests (8042 offline, QWEN error → no XIAOZHI, backend save → running backend unchanged, voice/VAD restart_required, chat markers); no localhost fetch in iPad mode |
| 10 | Single WebRTC owner + video-only enforcement | verified | `videoOnlySdp` 7 tests + `WebRTCSessionOwner` 6 tests; dead second path removed; `offerToReceiveAudio:false`; video-only stream assertion closes on violation |
| 11 | BLE Wi-Fi provisioning + CryptoKit sealed PSK | verified | `WiFiPasswordSealerTests` (5 byte-exact vectors against an independent Python cryptography reference); `BLEProvisioningActorTests` (11 lifecycle cases); `BluetoothCentral` notifies-before-writes; `BLEProvisioningView` exposes MITM caveat on every step |
| 12 | YRobot manual-control lease + Choreographer arbitration | verified | 17 `test_manual_control.py` cases (session id, validation, single lease, TTL, heartbeat, release idempotency, status safety, Choreographer arbitration, slew, first-frame continuity); `/api/motion` returns 409 while manual owns the robot; `/api/status` exposes `control_owner` and `manual_remaining_ms`, never the session id |
| 13 | Controller / Expressions → manual lease + Gamepad bridge | verified | `manualControlClient` (8 tests), `safeMotionGuard` (4 tests), `useControllerAPI` iPad branch, `GamepadController` Swift (5 tests) |
| 14 | Scene-phase + network-path lifecycle | verified | `LifecycleTests` (5 cases): background disconnects + bumps generation; idempotent; foreground resets; unsatisfied path disconnects; satisfied path leaves the session alone |
| 15 | Security audit + TestFlight checklist | documented | `ipad-security-review.md`, `ipad-testflight-checklist.md`; bundle scanner rejects `@tauri-apps`, `/api/move/(ws/)?set_target`, `/update/start`, `environment_overrides`, `Authorization: Bearer`, `DASHSCOPE_API_KEY`, `HA_TOKEN`; ATS = `NSAllowsLocalNetworking` only |
| 16 | Integration gate + acceptance | **this report** | see "Local gates" below |

## Local gates

```text
desktop-app:  npm test             → 18 files / 174 tests pass
desktop-app:  npm run typecheck   → 0 errors
desktop-app:  npm run build:ipad  → bundle ships without @tauri-apps or secrets
iOS:          xcodegen + xcodebuild test
               → 8 XCTest classes + 2 XCUITest classes all green
Python:       pytest tests/test_manual_control.py tests/test_motion.py
               tests/test_dashboard_status.py → all green (76 cases)
deploy.sh --dry-run
               → drift check OK; focused tests pass; rsync plan 121 files
```

## Operator gates (cannot be concluded from logs)

Plan Task 16.5 / 4.x matrix. Each row requires the operator next to the
robot. Drop the verification date + device + build + result here as the
matrix completes.

| Item | Verified | Evidence |
| --- | --- | --- |
| Native bundle loads JS / WASM / STL from `file://` | ☐ | device |
| Native HTTP bridge reaches 8000 / 8042 (no browser CORS) | ☐ | device |
| `ws://host:8000` and `wss://host:8443` reach the LAN (Local Network ATS) | ☐ | device |
| WebRTC H.264 decode on device, exactly one video track | ☐ | device |
| BLE maximum write length (≈273 B) survives one command | ☐ | device |
| Background soak (100 cycles): no orphan WS / WebRTC / manual lease | ☐ | device |
| 3D pose + passive joints + DoA update continuously | ☐ | device |
| XIAOZHI + QWEN both speak with camera on | ☐ | device |
| Controller acquires, holds, releases cleanly; second client cannot steal the lease | ☐ | device |
| NaN / Inf / out-of-bounds targets are 422 | ☐ | device |
| 2-hour continuous run: no memory leak, all reconnects clean | ☐ | device |

## Deploy posture

- `./scripts/deploy.sh --dry-run` was the last check executed (see
  "Local gates" above). The script will back up the robot's working
  tree to `~/.local/state/yrobot/backups/` with a SHA-256 manifest
  before any change ships.
- **Do not deploy without operator confirmation.** Plan Task 16 step 3
  requires the operator to read this report and the parity matrix,
  then explicitly run `./scripts/deploy.sh` (no `--dry-run`).
- After deploy, the operator runs the post-deploy health checks from
  plan Task 16 step 4 (`systemctl is-active yrobot.service`,
  `curl http://127.0.0.1:8042/api/status`, etc.).

## What's intentionally not in v1

- Camera microphone waveform (removed in Task 8 / Task 10 — `video-only`
  is the global contract).
- `system/power` reboot/poweroff endpoints (high-risk; plan §2.1 #8
  excludes them from v1).
- `forget_all` (plan §6 explicitly defers it).
- Hotspot Configuration secondary provisioning path
  (`NEHotspotConfigurationManager`); the BLE primary path is enough for
  the v1 feature scope.