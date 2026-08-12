# YRobot Remote for iPhone and iPad - Design

Date: 2026-08-10

## 1. Objective

Build a personal-use native iOS/iPadOS app that provides every function currently exposed by the YRobot Dashboard and can reconfigure Reachy Mini Wi-Fi when the robot is moved to another location.

The app targets the owner's iPhone 17 and iPad mini running iOS/iPadOS 26. It operates only on the local network and connects directly to Reachy Mini without a cloud service, user account, or public internet endpoint.

Working product name: **YRobot Remote**.

## 2. Scope

### Included

- Discover Reachy Mini over Bonjour/mDNS.
- Fall back to a manually entered hostname or IPv4 address.
- Show YRobot, system, daemon, motion, tracking, privacy, and conversation status.
- Show recent conversation turns derived from YRobot chat logs.
- Start and stop camera preview and display the latest JPEG frames.
- Control speaker volume and mute state.
- Enable or disable microphone upload, show microphone level, and configure the persisted VAD threshold.
- List and play basic, emotion, and dance motions.
- Read, filter, pause, clear locally rendered, and follow YRobot logs.
- Wake, sleep, and restart the official Reachy daemon.
- Restart YRobot, reboot Reachy, and power Reachy off with explicit confirmation.
- Configure Reachy Wi-Fi over Bluetooth when the robot is offline.
- Configure Reachy Wi-Fi through its setup hotspot as a fallback.
- Support compact iPhone navigation and iPad split-view navigation.
- Support light and dark appearance while matching the official Reachy Mini visual language.

### Excluded from the first release

- App Store publication, TestFlight distribution, accounts, subscriptions, or cloud sync.
- Control from outside the home or current local network.
- Android, macOS, Apple Watch, or visionOS clients.
- Push notifications, background monitoring, and background camera streaming.
- A 3D robot model, WebRTC media, app-store management, or direct joystick teleoperation.
- Changes to Xiaozhi conversation behavior, Home Assistant control, or robot motion algorithms.

## 3. Verified Existing Interfaces

The deployed Reachy Mini advertises `_reachy-mini._tcp.local.` as `reachy_mini`. The service resolves to `reachy-mini.local:8000` and includes the robot name, daemon version, hardware ID, IP address, and capability metadata.

The YRobot Dashboard is reachable on the same host at port `8042`. The iOS app derives the Dashboard base URL from the discovered daemon host instead of requiring a second discovery service.

YRobot endpoints used by the app:

- `GET /api/status`
- `GET` and `POST /api/motion`
- `GET` and `PUT /api/volume`
- `GET` and `PUT /api/audio/vad`
- `GET` and `PUT /api/audio/input`
- `GET` and `PUT /api/camera/state`
- `GET /api/camera/frame`
- `GET /api/logs`
- `GET /api/system/state`
- `POST /api/system/restart`
- `POST /api/system/power`
- `POST /api/reachy-daemon/action`

Official daemon Wi-Fi endpoints used by the app:

- `GET /wifi/status`
- `POST /wifi/scan_and_list`
- `GET /wifi/error`
- `POST /wifi/reset_error`
- `POST /wifi/forget`
- `GET /wifi/prov_key`
- `POST /wifi/connect_sealed`
- `POST /wifi/setup_hotspot`

The app must not use the legacy `/wifi/connect` endpoint because it places the Wi-Fi password in an HTTP query string.

## 4. Technical Approach

### Platform

- SwiftUI application with an iOS/iPadOS 26.0 deployment target.
- Swift 6 concurrency with `async`/`await`.
- `URLSession` for HTTP requests.
- `Network.framework` and `NWBrowser` for Bonjour discovery.
- `CoreBluetooth` for offline Wi-Fi provisioning.
- `CryptoKit` for the official sealed-password protocol.
- `NetworkExtension.NEHotspotConfigurationManager` for the optional setup-hotspot join flow.
- Keychain for the device PIN and any saved connection metadata.
- No third-party runtime dependencies.

### Source layout

The Xcode project lives under `ios/YRobotRemote/` in the existing repository.

- `App/`: application entry point and adaptive navigation.
- `Core/Discovery/`: Bonjour discovery and manual-host validation.
- `Core/API/`: endpoint client, request/response models, and error mapping.
- `Core/Bluetooth/`: Reachy BLE discovery, session authentication, and command transport.
- `Core/WiFi/`: Wi-Fi provisioning state machine and sealed credential generation.
- `Core/Storage/`: Keychain-backed device preferences.
- `Features/Overview/`: status and recent conversations.
- `Features/Media/`: camera, speaker, microphone, and VAD controls.
- `Features/Motions/`: motion categories and playback.
- `Features/Logs/`: log filters and follow/pause behavior.
- `Features/Network/`: current network and Wi-Fi setup wizard.
- `Features/Settings/`: robot selection, diagnostics, daemon, and power actions.

Each feature depends on the shared API client and observable robot session. Features do not call `URLSession` or CoreBluetooth directly.

## 5. Connection Lifecycle

1. On first launch, request Local Network permission with a clear Reachy-specific explanation.
2. Browse `_reachy-mini._tcp` on `local.` and show discovered robots.
3. Resolve the selected service to a host and daemon port.
4. Probe daemon `GET /api/daemon/status` on port `8000` and YRobot `GET /api/status` on port `8042` independently.
5. Save the stable Bonjour service name and hardware ID as the preferred robot. Do not treat the DHCP address as the permanent identity.
6. On later launches, try the preferred Bonjour service first, then its last known address, then manual entry.
7. Represent daemon and YRobot availability separately so a partial failure does not make the entire app appear offline.
8. Stop high-frequency requests when the app enters the background. Resume with a fresh status load when it becomes active.

Manual entry accepts a hostname such as `reachy-mini.local` or a local IPv4 address. It validates both ports before saving.

## 6. Wi-Fi Provisioning

### Primary path: Bluetooth-only provisioning

This path works when Reachy is no longer connected to a usable Wi-Fi network. It follows the official Reachy Mini BLE provisioning protocol.

1. Scan for Bluetooth devices whose advertised name identifies Reachy Mini.
2. Connect to command service `12345678-1234-5678-1234-56789abcdef0`.
3. Subscribe to response characteristic `12345678-1234-5678-1234-56789abcdef2` before issuing asynchronous Wi-Fi commands.
4. Ask the user for the last five characters of the serial number and send `PIN_<pin>`.
5. Read `WIFI_STATUS`, then issue authenticated `WIFI_SCAN`.
6. Let the user select or manually enter an SSID and enter its password.
7. Request `WIFI_KEYEX` and generate the official encrypted payload in CryptoKit:
   - Curve25519 key agreement.
   - HKDF-SHA256 with the device PIN as salt.
   - AES-256-GCM with the SSID as additional authenticated data.
8. Send `WIFI_CONNECT_ENC <json>`.
9. Poll `WIFI_STATUS` until connected, failed, or timed out.
10. Disconnect BLE and resume Bonjour discovery on the destination network.

The BLE transport must handle the protocol's immediate `OK: working` acknowledgement separately from the later result notification.

### Secondary path: Reachy setup hotspot

Use this path when BLE provisioning is unavailable.

1. Offer to join `reachy-mini-ap` through `NEHotspotConfigurationManager` when the Hotspot Configuration capability is available.
2. If iOS does not permit automatic joining, show a short system-settings handoff and resume the wizard when the app returns to the foreground.
3. Connect to the daemon at `http://10.42.0.1:8000`.
4. Scan networks, request `/wifi/prov_key`, build the same sealed CryptoKit payload, and call `/wifi/connect_sealed`.
5. Wait for the hotspot to disappear, then rediscover Reachy on the destination network.

### Saved networks and destructive actions

- Show the connected network and known networks returned by `/wifi/status`.
- Forgetting a saved network requires confirmation.
- Forgetting the active network requires a stronger warning because Reachy will fall back to hotspot mode.
- Do not expose `forget_all` in the first release; it is unnecessary for normal relocation and has a larger recovery cost.
- Never print, log, persist in `UserDefaults`, or include the Wi-Fi password in an error message.
- Store the five-character device PIN in Keychain only after explicit user consent.

### Security boundary

The official sealed protocol prevents a passive Bluetooth observer from reading the Wi-Fi password. The official protocol documents a known active-man-in-the-middle limitation caused by the short PIN and BLE Just Works pairing. The app must present a concise note recommending that provisioning be performed near the robot and away from untrusted nearby devices. Closing that protocol-level limitation is outside this app's scope.

## 7. User Interface

### Navigation

On iPhone, use a compact tab-based structure:

- Overview
- Camera and Audio
- Motions
- Logs
- Settings

The Network screen is opened prominently from Settings and automatically when no robot is reachable.

On iPad, use `NavigationSplitView` with the same destinations in a sidebar and a persistent detail area. Camera preview, logs, and status details use the additional width without changing the underlying feature behavior.

### Official visual language

Use the official desktop app as the visual reference, not as a code dependency.

- Reachy orange `#FF9500` is the primary accent.
- Neutral white and light-gray work surfaces are the default.
- Dark mode uses near-black neutral surfaces rather than a tinted palette.
- Status colors are green, red, amber, blue, and purple according to meaning.
- Corner radii stay within 4-16 points, with 8-12 points for normal controls.
- SF Symbols are used for standard actions; official Reachy image assets are used for robot identity and empty states where their license permits reuse.
- Destructive actions are visually separated and never placed beside frequently used controls without spacing and confirmation.
- Information density matches a device-control application, with no marketing hero or decorative card nesting.

## 8. Feature Behavior

### Overview

- Refresh status every 5 seconds while visible and support pull-to-refresh.
- Render system, daemon, app-lock, DoA, tracking, motion, and audio status independently.
- Keep the last good value visible with a stale indicator when one subsystem fails.
- Refresh recent conversations every 2 seconds while visible.

### Camera and audio

- Camera remains off until the user enables it.
- Fetch JPEG frames every 500 milliseconds only while the camera screen is visible and the app is active.
- Retain the last valid frame briefly while reporting capture failure; do not report HTTP 200 alone as camera health.
- Debounce volume changes and show the value returned by the robot.
- Show microphone level independently from microphone upload enablement.
- Save VAD changes through the existing endpoint and report persistence failures.

### Motions

- Load the authoritative motion list from Reachy instead of hard-coding availability.
- Preserve the Dashboard's basic, emotion, and dance grouping and Chinese labels.
- Disable conflicting motion buttons while a play request is in flight.
- Surface app-lock or daemon-busy errors without retrying motion commands automatically.

### Logs

- Refresh every 2 seconds while visible.
- Support Chat, Debug, Info, Notice, Warning, and Error filters.
- Keep at most 2,000 rendered rows in memory.
- Pause following when the user scrolls away from the bottom.
- Clear removes only rows held by the app; it does not erase the robot journal.

### Daemon and power

- Wake, sleep, restart daemon, restart YRobot, reboot, and power off use the existing server semantics.
- Every power action shows a confirmation dialog describing the expected disconnect.
- Power off and reboot rely on the robot-side graceful sleep hook already implemented by YRobot.
- The app does not automatically retry any destructive action after a timeout because the command may already have succeeded.

## 9. Error Handling

Use a small typed error model: discovery, permission, connection, timeout, validation, robot busy, unavailable subsystem, and destructive-action-unknown-result.

- A failure in camera, logs, daemon status, or audio must not hide unrelated working sections.
- Connection loss shows the last successful timestamp and begins bounded rediscovery.
- Bluetooth provisioning has explicit states: scanning, connecting, authenticating, scanning networks, sealing credentials, connecting Wi-Fi, rediscovering, success, and failure.
- A stale provisioning key causes one fresh key exchange and retry; other credential errors require user action.
- Wi-Fi connection failure reports `/wifi/error` and explains that Reachy should revert to hotspot mode.

## 10. Testing and Acceptance

### Automated tests

- Codable fixtures for every YRobot response consumed by the app.
- API-client tests using `URLProtocol` stubs for success, malformed payloads, partial status, timeout, and non-2xx responses.
- Discovery state tests for Bonjour, last-known address, and manual fallback.
- CryptoKit known-vector tests matching the daemon's X25519/HKDF/AES-GCM format.
- BLE command-state tests, including the asynchronous acknowledgement and notification sequence.
- Wi-Fi provisioning state-machine tests for success, wrong PIN, stale key, busy daemon, bad password, and rediscovery timeout.
- View-model tests for camera lifecycle, log retention, partial status, and destructive-action uncertainty.

### Physical-device acceptance

The release is accepted only after verification on both the iPhone 17 and iPad mini:

- Discover and connect to Reachy on the home network.
- Exercise every current Dashboard control and compare robot-side results.
- Preview live changing camera frames in portrait, landscape, and iPad split view.
- Verify volume, mute, microphone upload, meter, and persisted VAD changes.
- Play at least one motion from each category.
- Filter and pause live logs and confirm recent conversations update.
- Wake, sleep, and restart the daemon.
- Reboot and power off with graceful head movement and successful later reconnection.
- Move Reachy to a different Wi-Fi using BLE only.
- Recover through `reachy-mini-ap` when BLE is deliberately unavailable.
- Test app background/foreground transitions, Wi-Fi loss, daemon-only failure, and YRobot-only failure.
- Run a two-hour foreground soak with status polling and intermittent camera use without unbounded memory growth or duplicate commands.

The iOS Simulator is useful for layout and mocked API tests but is not accepted as evidence for Bonjour permission, Bluetooth, hotspot joining, or robot behavior.

## 11. Delivery Stages

1. Project foundation and typed API client.
2. Bonjour discovery, manual connection, and adaptive navigation.
3. Overview, recent conversation, and partial-failure status handling.
4. Camera and audio controls.
5. Motions, logs, daemon actions, and power controls.
6. BLE Wi-Fi provisioning and CryptoKit payload generation.
7. Setup-hotspot fallback and post-provisioning rediscovery.
8. Official-style visual polish, accessibility, and iPad layout.
9. Automated verification and two-device hardware acceptance.

Estimated implementation time: 9-12 development days, excluding delays caused by unavailable hardware, Apple signing, or protocol defects discovered during physical testing.

## 12. Installation and Signing

The app is installed directly from the shared Mac through Xcode. Both devices must enable Developer Mode and trust the selected development identity.

A free Personal Team provisioning profile expires after seven days and requires rebuilding and reinstalling the app. A paid Apple Developer Program membership avoids that weekly personal-team reprovisioning cycle for registered development devices. This design does not require App Store publication.

## 13. References

- Reachy Mini desktop app: <https://github.com/pollen-robotics/reachy-mini-desktop-app>
- Reachy Mini SDK and daemon: <https://github.com/pollen-robotics/reachy_mini>
- Official BLE Wi-Fi protocol: <https://github.com/pollen-robotics/reachy_mini/blob/main/src/reachy_mini/daemon/app/services/bluetooth/BLE_WIFI_PROVISIONING.md>
- Apple local-network privacy: <https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy>
- Apple Wi-Fi API overview: <https://developer.apple.com/documentation/technotes/tn3111-ios-wifi-api-overview>
- Apple hotspot configuration: <https://developer.apple.com/documentation/networkextension/nehotspotconfigurationmanager>
- Apple SwiftUI split navigation: <https://developer.apple.com/documentation/swiftui/navigationsplitview>
