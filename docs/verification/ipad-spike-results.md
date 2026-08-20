# iPad P0 Spike Results

> Technical prerequisites for the cockpit inside WKWebView (plan Task 6).
> Simulator/CI rows are verified locally; **device rows require the operator
> with a signed iPad build and the live robot** and stay pending until then.

## Local verification (2026-08-20, this branch)

| Row | Result | Evidence |
| --- | --- | --- |
| JS executes | PASS | bundle boots in `xcodebuild test` UI run; spike view renders |
| WASM instantiates | PASS (sim) | `WebAssembly.instantiate` smoke in spike view; kinematics WASM reuses same pipeline |
| Binary asset fetch (`file://`) | PASS (sim) | `probe.bin` (512 B) fetched with bytes intact from the Cockpit directory |
| Native bridge GET/PUT/POST | PASS (unit) | `NativeBridgeHandlerTests` + `RobotHTTPClientTests` (URLProtocol stub, 32 cases) |
| Build pipeline | PASS | `scripts/build-ipad-web.sh` runs typecheck → targeted vitest → vite build; Xcode `PhaseScriptExecution Build iPad Web Cockpit` confirmed in build log |
| Bundle contents | PASS | `Cockpit/index.html` + hashed assets; `base: './'`; no `/v2/` base reuse |
| Import graph | PASS (Task 7 guard pending) | iPad entry `src/ipad/main.tsx` imports only `IpadApp`/MUI/spike; desktop `main.tsx` refuses to boot in `VITE_IPAD_MODE` |
| WebRTC video-only offer | PASS (offer level) | spike asserts created offer contains no `m=audio` before any signaling |

## Device + live-robot rows (operator acceptance pending)

These cannot be concluded from logs and must be verified next to the robot
with a signed device build (plan Task 6.10, §4.5):

- [ ] `file://` bundle loads JS/WASM/STL on device (URDF/STL MIME via `loadFileURL`).
- [ ] Native HTTP bridge reaches 8000/8042 from device; no browser-CORS requests.
- [ ] `ws://host:8000` and `ws://host:8443` connect under Local Network ATS
      (`NSAllowsLocalNetworking` only). If WKWebView refuses `ws://`, the only
      permitted addition is `NSAllowsArbitraryLoadsInWebContent=true`.
- [ ] WebRTC H.264 decode on device; remote stream has exactly 1 video track.
- [ ] BLE max `maximumWriteValueLength(for: .withResponse)` supports the ~273-byte
      sealed command (Task 11 gate).

## Decisions

- No `CockpitSchemeHandler` was added: simulator `loadFileURL` loads assets
  correctly; the plan only allows the scheme handler if device evidence
  demands it.
- The spike page stays reachable via the `#spike` hash and a DEBUG toolbar
  toggle in the app, for hardware verification sessions.
