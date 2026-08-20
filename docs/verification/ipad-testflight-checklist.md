# YRobot Remote — TestFlight First-Ship Checklist

> Plan Task 15. Items marked **[operator]** need the user (not the agent)
> to provide inputs that only they own.

## App Store Connect metadata

- [ ] **Bundle ID** — `[operator]` fill `Config/App.xcconfig`
      `PRODUCT_BUNDLE_IDENTIFIER`. Recommended `com.yrobot.remote.ipad`.
- [ ] **Apple Developer Team** — `[operator]` set
      `DEVELOPMENT_TEAM` and `PROVISIONING_PROFILE_SPECIFIER` (or just
      `Automatic`) in `Config/App.xcconfig`. Do **not** commit personal
      team IDs or p12 keys; the Xcode build will inherit them via the
      user's keychain.
- [ ] **App icon** — full 1024×1024 master + iPad sizes
      (`Assets.xcassets/AppIcon.appiconset`).
- [ ] **Launch screen** — already supplied via `UILaunchScreen` (Storyboard
      or generated); confirm with a designer pass.
- [ ] **Privacy policy URL** — `app store connect → App Information`. The
  app declares no tracking so a single-page "we don't track you" page is
  enough.
- [ ] **Export compliance**: see `ipad-security-review.md`. With CryptoKit
  only we ship without `ITSAppUsesNonExemptEncryption=false`. Re-evaluate
  if the app ever bundles a non-exempt library.

## Build configuration

- [ ] **Build number** — increment on every TestFlight upload.
- [ ] **Signing** — Xcode managed profile, automatic.
- [ ] **Versioning** — `CFBundleShortVersionString` = marketing version,
      `CFBundleVersion` = monotonically increasing integer per upload.
- [ ] **TestFlight build configuration** — `Release` (not `Debug`); the
  XcodeGen `Debug` config still ships with the build script wired in.

## Archive + upload

```bash
# From the worktree (never from the parent working tree)
cd ios/YRobotRemote
xcodegen generate
xcodebuild -project YRobotRemote.xcodeproj -scheme YRobotRemote \
  -destination 'generic/platform=iOS' \
  -configuration Release \
  CODE_SIGNING_ALLOWED=NO build   # or just sign via Xcode
xcrun altool --upload-app --type ios --file YRobotRemote.ipa
# or use Xcode → Organizer → Distribute App → TestFlight
```

- [ ] Archive built.
- [ ] IPA uploaded via Xcode Organizer or `xcrun altool` (the latter
      requires an App Store Connect API key, see App Store Connect →
      Users → Keys).

## Pre-upload gates

- [ ] `xcodebuild ... test` green on the iPad Pro 13-inch (M5) simulator
      (this branch has a green build history).
- [ ] `npm --prefix desktop-app test` green.
- [ ] `npm --prefix desktop-app run build:ipad` produces a bundle with no
      `@tauri-apps` / `set_target` / secrets (enforced by
      `platformImportGuard.test.ts`).
- [ ] `./scripts/deploy.sh --dry-run` returns 0 against the worktree.
- [ ] No diff in `~/.env`, `~/.local/state`, or any developer file outside
      the worktree — the deploy script only copies files tracked in git.

## On-device smoke (operator)

- [ ] Install the build fresh, accept Local Network + Bluetooth
      permission dialogs. Verify the wording matches the iOS copy deck.
- [ ] Discovery finds a Reachy on the local network.
- [ ] Provision flow: scan → PIN → SSID → connect → poll outcome.
- [ ] Cockpit: 3D pose tracks, Camera toggle swaps views, DoA indicator
      updates when speaking.
- [ ] Controller: 20 Hz setpoints + heartbeat keep the lease; disconnect /
      foreground / background all release within 2 s; no zombie lease
      after force-quit.
- [ ] Power button: a sleep action shows a confirmation dialog and
      triggers `/api/reachy-daemon/action` (no direct daemon kill).
- [ ] Reinstall over an existing build: the last-known robot survives
      because we only store non-sensitive metadata.

## Rollback

- [ ] TestFlight build can be removed from App Store Connect without
      affecting the App Store listing (until the first official release).
- [ ] Local dashboard (desktop app + 8042 dashboard) keeps working
      because the iPad changes are additive (the new `/api/manual-control/*`
      routes exist alongside `/api/motion`).
- [ ] If the manual-control lease misbehaves on the device, server-side
      the TTL (`DEFAULT_TTL_SECONDS = 2.0`) drops the lease within two
      seconds; no daemon restart needed.