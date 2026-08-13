# Build & Install — YRobot Remote

This file covers the two ways to ship YRobot Remote onto an iOS device:

1. **iPhone 17 / iPad mini simulator** — for layout and unit tests.
2. **Real iPhone or iPad** — for the physical-acceptance matrix.

The project is a SwiftUI app for iOS/iPadOS 26, built with Xcode 26.5 and
Swift 6. The repository is generated from `project.yml` via
[XcodeGen](https://github.com/yonaskolb/XcodeGen); the .xcodeproj is
**not** meant to be hand-edited.

## Prerequisites

```bash
# Apple toolchain
xcode-select --install           # if Xcode is not already on the path
brew install xcodegen

# Confirm versions
xcodebuild -version              # expect Xcode 26.5 / iOS 26.5 SDK
xcodegen --version
```

For real-device builds you also need:

- A Mac running macOS 14+ (Apple silicon recommended).
- An iPhone or iPad on iOS/iPadOS 26.0+ signed into your Apple ID.
- A Lightning or USB-C cable (the iPhone 17 is USB-C).
- An Apple Developer account (free Personal Team is enough for ad-hoc
  install, but it expires every 7 days; a paid Apple Developer Program
  account is required for stable device installation).

## Quickstart — Simulator

```bash
cd ios/YRobotRemote
./scripts/build.sh simulator
```

That:

1. Regenerates the Xcode project from `project.yml`.
2. Builds for the iPhone 17 Pro simulator.
3. Runs the full XCTest suite. Expected: 118/118 passing in ~6s.

To run only the tests (skip the build):

```bash
./scripts/build.sh test
```

## Quickstart — Real iPhone or iPad

```bash
cd ios/YRobotRemote

# 1. Plug in + unlock the iPhone, then confirm it is visible:
xcrun devicectl list devices

# 2. Build + install on the first connected device
./scripts/build.sh device
# or pin to a specific UDID:
./scripts/build.sh device 00008150-001430691480401C
```

That will:

1. Regenerate the Xcode project.
2. Build for the device (Debug).
3. Run `xcrun devicectl device install app` to side-load the bundle.

### First-time device prep

On the iPhone:

1. **Settings → Privacy & Security → Developer Mode** → turn on. The
   phone will prompt to restart.
2. After restart, connect the iPhone to the Mac, unlock the screen,
   and tap **Trust** when the "Trust This Mac" dialog appears.
3. **Settings → General → VPN & Device Management** → tap your
   developer profile → **Trust**.

On the Mac (only the first time, or when the personal-team cert
expires):

1. Open `ios/YRobotRemote/YRobotRemote.xcodeproj`.
2. Select the **YRobotRemote** target → **Signing & Capabilities**.
3. Set the Team to your Personal Team (or paid team).
4. Repeat for the **YRobotRemoteTests** target if you want to run
   on-device tests later.

After that, every `scripts/build.sh device` run will pick up the
existing signing identity without re-prompting. If the personal-team
provisioning profile expires (7 days), the build will fail; re-run
`scripts/build.sh device` and accept the prompt that asks you to
rebuild the profile.

### Launch on device

After a successful install:

```bash
xcrun devicectl device process launch \
  --device 00008150-001430691480401C \
  ai.proma.yrobotremote
```

Or just open the app from the home screen. On first launch, iOS will
prompt for:

- **Local Network** — required for Bonjour discovery + daemon / YRobot
  HTTP traffic.
- **Bluetooth** — required for the BLE Wi-Fi provisioning path.

Tap **OK** on both. If you skip them, the app will fall back to
manual host entry, and Wi-Fi provisioning will only be available via
the setup-hotspot flow.

## Manual xcodebuild reference

The script wraps a few common invocations. If you need finer control
(archive, custom configuration, etc.), here are the equivalent raw
commands.

### Build for simulator

```bash
xcodebuild \
  -project ios/YRobotRemote/YRobotRemote.xcodeproj \
  -scheme YRobotRemote \
  -configuration Debug \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  build
```

The output `.app` lands in:

```text
~/Library/Developer/Xcode/DerivedData/YRobotRemote-<hash>/Build/Products/Debug-iphonesimulator/YRobotRemote.app
```

Install on the simulator with `xcrun simctl install`:

```bash
DEVICE_ID=$(xcrun simctl list devices available \
  | grep "iPhone 17 Pro " | head -1 | grep -oE '\([A-F0-9-]{36}\)' | tr -d '()')
xcrun simctl boot "$DEVICE_ID"
xcrun simctl install "$DEVICE_ID" \
  ~/Library/Developer/Xcode/DerivedData/YRobotRemote-*/Build/Products/Debug-iphonesimulator/YRobotRemote.app
xcrun simctl launch "$DEVICE_ID" ai.proma.yrobotremote
```

### Run tests

```bash
xcodebuild \
  -project ios/YRobotRemote/YRobotRemote.xcodeproj \
  -scheme YRobotRemote \
  -configuration Debug \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  test
```

### Build for a real iPhone (archive-ready)

```bash
xcodebuild \
  -project ios/YRobotRemote/YRobotRemote.xcodeproj \
  -scheme YRobotRemote \
  -configuration Debug \
  -destination 'generic/platform=iOS' \
  -allowProvisioningUpdates \
  build
```

This produces a device-targeted `.app` that is ready for `devicectl`
or Xcode's Devices window.

## Troubleshooting

### `xcodebuild` fails with "no provisioning profile"

Open the project, pick the YRobotRemote target, Signing & Capabilities
→ Team → your Personal Team. Re-run.

### `devicectl` says "device offline"

The iPhone is locked, on the lock screen, or not plugged in. Unlock
it; if the cable is loose, replug. Verify with:

```bash
xcrun devicectl list devices
```

The target device should appear under `Devices` (not `Devices Offline`)
and not in `Devices Offline`.

### "Could not find Developer Disk Image"

The iPhone's iOS version is newer than the iOS SDK Xcode knows about.
Update Xcode via the App Store, or pin to an older iPhone iOS via
the simulator.

### Signing expires every 7 days

This is the free Personal Team limitation. Either:

- Re-run `scripts/build.sh device` and accept the re-provisioning
  prompt; the new profile lasts another 7 days.
- Enroll the Apple ID in the paid Apple Developer Program
  ($99/yr) and pick that team. Provisioning then lasts a year
  and the app can be TestFlight-distributed.

### Simulator says "Application failed preflight checks: Busy"

Two `xcodebuild test` runs collided in the same simulator. Wait ~5s
or restart the simulator with `xcrun simctl shutdown all &&
xcrun simctl boot "iPhone 17 Pro"`.

### App opens to "No robot selected"

That is the expected first-launch state. Tap the **Settings** tab →
**Network setup** → either pick the discovered Reachy Mini from
Bonjour or type `reachy-mini.local` / `192.168.1.14`. If the
Local Network permission was denied, the picker will only show
manual entry.

### Wi-Fi provisioning path needs the iPhone on the home network

After the robot is on the new Wi-Fi, the iPhone has to be on the
same SSID for Bonjour to find it again. If the iPhone is still on
the old SSID, the app will time out; switch it manually or rely
on the iOS "auto-join" preference.

## Where the build output lives

| Type | Path |
|---|---|
| Build products (simulator) | `~/Library/Developer/Xcode/DerivedData/YRobotRemote-<hash>/Build/Products/Debug-iphonesimulator/` |
| Build products (device) | `~/Library/Developer/Xcode/DerivedData/YRobotRemote-<hash>/Build/Products/Debug-iphoneos/` |
| Test logs | `~/Library/Developer/Xcode/DerivedData/YRobotRemote-<hash>/Logs/Test/` |
| Source .xcodeproj | `ios/YRobotRemote/YRobotRemote.xcodeproj` (regenerated) |
| Source of truth | `ios/YRobotRemote/project.yml` |
