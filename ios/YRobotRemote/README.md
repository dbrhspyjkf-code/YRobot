# YRobot Remote — iOS / iPadOS SwiftUI app

Controls a Reachy Mini robot running the YRobot dashboard. See
`docs/superpowers/specs/2026-08-10-yrobot-ios-remote-design.md` for the full
design.

## Build & test

Requires Xcode 26+, iOS 26 SDK, and `xcodegen` (`brew install xcodegen`).

```bash
cd ios/YRobotRemote
xcodegen generate
open YRobotRemote.xcodeproj          # or use the CLI below

# CLI build + test on iPhone 17 Pro simulator
xcodebuild -project YRobotRemote.xcodeproj -scheme YRobotRemote \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
```

The test target bundles real captured JSON from the live robot under
`Tests/Fixtures/` so the Codable models are exercised against actual server
shapes, not invented fixtures.

## Layout

```
ios/YRobotRemote/
  project.yml                       # xcodegen config (single source of truth)
  YRobotRemote.xcodeproj/           # generated; do not edit by hand
  Sources/
    App/
      YRobotRemoteApp.swift         # @main + TabView root + env injection
    Core/
      Discovery/
        DiscoverySession.swift      # NWBrowser wrapper + pure TXT-record parser
        RobotEndpoint.swift         # Host/IP parser + URL builders
        RobotPreferences.swift      # UserDefaults wrapper for preferred robot
      Networking/
        APIClient.swift             # URLSession wrapper, typed verbs, status → APIError mapping
        Errors.swift                # APIError (transport/timeout/422/409/400/503/sealed)
        Models.swift                # All Codable models for YRobot + daemon endpoints
      Session/
        RobotSession.swift          # @Observable: two APIClients + per-subsystem availability
      Storage/
        KeychainStore.swift         # Generic-password wrapper, kSecAttrAccessibleAfterFirstUnlock
    Features/
      Overview/OverviewView.swift   # Tab 1 — pulls YRobot status, 5s polling, stale indicator
      Network/NetworkView.swift     # Discovery list + manual IP/hostname entry
      Media/MediaView.swift         # Tab 2 placeholder (Stage 4)
      Motions/MotionsView.swift     # Tab 3 placeholder (Stage 5)
      Logs/LogsView.swift           # Tab 4 placeholder (Stage 5)
      Settings/SettingsView.swift   # Tab 5 — connected robot summary + link to Network
  Tests/
    YRobotRemoteTests/              # XCTest target
    Fixtures/                       # Live-captured JSON from 192.168.1.14
```

Future stages add `Core/Bluetooth/`, `Core/WiFi/`, and flesh out
`Features/{Overview,Media,Motions,Logs,Settings}/`. The layout matches
the spec's §4 plan.

## Design choices worth knowing

- **No third-party runtime dependencies.** Only Apple frameworks
  (Foundation, Security, CryptoKit, NetworkExtension, CoreBluetooth).
- **`httpBodyStream` instead of `httpBody`.** URLSession converts `httpBody`
  into a private stream before URLProtocol sees the request, so test stubs
  that read `req.httpBody` see `nil`. The APIClient sets
  `req.httpBodyStream = InputStream(data: body)` so tests (and any future
  on-device debugging hooks) can drain the actual bytes.
- **JSON `{"detail": "..."}` is parsed into a typed `FastAPIError` envelope.**
  Status codes are mapped to the typed `APIError` cases in spec §9
  (validation, robotBusy, unavailableSubsystem, sealedCredential, httpStatus).
- **Swift 6 strict concurrency is on.** All models are `Sendable & Equatable`,
  `APIClient` is `Sendable`. The `Tests/Fixtures/` folder is added to the test
  target as a folder reference so `Bundle(for: Self.self).url(forResource:…
  subdirectory: "Fixtures")` works without per-file resource entries.
- **`RobotEndpoint` parser is lenient.** It only flags IPv4 vs `.local` vs
  generic hostname; anything else is accepted as a hostname and the actual
  reachability check happens during the dual-port probe (spec §5.2). This
  matches the spec's "validate both ports before saving" — we don't try to
  be a strict IPv4 validator here.
- **Bonjour instance names are normalized.** Service instance names like
  `reachy_mini` become `reachy-mini.local` for HTTP URLs (mDNS rewrites
  underscores to hyphens for DNS-label legality). We do the same rewrite
  in `DiscoverySession.parseBonjourRecord`.
- **`RobotSession` takes a `URLSession`** so tests can inject an ephemeral
  session whose `URLProtocol` stub returns canned responses. Production
  default is `URLSession.shared`.

## How to verify against the real robot

```bash
DEVICE_ID=$(xcrun simctl list devices available | grep "iPhone 17 Pro " \
  | head -1 | grep -oE '\([A-F0-9-]{36}\)' | tr -d '()')

# Boot + install + launch
xcrun simctl boot "$DEVICE_ID"
xcrun simctl install "$DEVICE_ID" \
  ~/Library/Developer/Xcode/DerivedData/YRobotRemote-*/Build/Products/Debug-iphonesimulator/YRobotRemote.app
xcrun simctl launch "$DEVICE_ID" ai.proma.yrobotremote

# Pre-populate "preferred robot" so Overview connects on first launch
CONTAINER=$(xcrun simctl get_app_container "$DEVICE_ID" ai.proma.yrobotremote data)
PLIST="$CONTAINER/Library/Preferences/ai.proma.yrobotremote.plist"
JSON='{"serviceName":"reachy_mini","hardwareId":"5a5e0ad96b539eae","lastHost":"reachy-mini.local","savedAt":"2026-08-10T07:29:11Z"}'
B64=$(printf '%s' "$JSON" | base64)
plutil -create xml1 "$PLIST"
plutil -replace "preferredRobot" -data "$B64" "$PLIST"
xcrun simctl launch "$DEVICE_ID" ai.proma.yrobotremote
xcrun simctl io "$DEVICE_ID" screenshot /tmp/overview.png
```

This is how Stage 2 was verified against the live robot at `192.168.1.14`.

## Where the ground truth lives

The Codable models were designed against real responses captured from the
robot at `192.168.1.14`. Originals live in the agent workspace at
`.context/api-trace/` and the protocol references in `.context/protocol/`.
