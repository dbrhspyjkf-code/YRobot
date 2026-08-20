# YRobot Remote — iPad Security Review

> Plan Task 15. Static + dynamic checks baked into the build so the next
> reviewer can verify the audit without re-deriving it.

## Bundle hygiene

| Check | Status | Evidence |
| --- | --- | --- |
| No `@tauri-apps/*` references in the shipped Cockpit bundle | **pass** | `platformImportGuard.test.ts` walks the import graph + scans the built bundle; both fail on any `@tauri-apps` hit. |
| No daemon-8000 `set_target` URL in the iPad bundle | **pass** | bundle-scanner regex `/api/move/(ws/)?set_target`; not in the build. |
| No daemon-8000 `/update/start`, `environment_overrides` in the iPad bundle | **pass** | bundle-scanner regexes; not in the build. |
| No `Authorization: Bearer`, `DASHSCOPE_API_KEY`, `HA_TOKEN` in the iPad bundle | **pass** | bundle-scanner regexes; secrets stay on the robot and never cross the bridge. |
| No recorded-emotion / dance libraries imported in the iPad graph | **pass** | `safeMotionGuard.test.ts` static scan over the iPad entry + cockpit view + manual client. |

## Entitlements / Info.plist

- **ATS**: only `NSAllowsLocalNetworking = YES`. No `NSAllowsArbitraryLoads`,
  no `NSAllowsArbitraryLoadsInWebContent` — every non-local connection
  stays HTTPS or fails loud (WebRTC signaling uses `ws://8443` against
  the LAN and is intentional; the bundle never reaches a public STUN
  server).
- **Local Network**: `NSLocalNetworkUsageDescription` explains Bonjour +
  WS to the robot. No `Hotspot Configuration` capability declared (the
  secondary path is iOS-native but unused in v1).
- **Bluetooth**: `NSBluetoothAlwaysUsageDescription` states the BLE
  provisioning purpose only. No background Bluetooth entitlement
  (CoreBluetooth is foreground only, scan stops on `.background`).
- **Camera / Microphone usage**: not declared. The iPad bundle is video-only
  (`offerToReceiveAudio:false`); the audio mic waveform was removed in
  Task 8 / Task 10.
- **Privacy manifest** (`PrivacyInfo.xcprivacy`): declares UserDefaults
  access (CA92.1 — last-known robot + Bonjour name), FileTimestamp access
  (C617.1 — NSE/Keychain metadata), and DiskSpace (E174.1 — bundle
  integrity checks). No tracking, no tracking domains, no collected
  data types.

## CryptoKit export compliance (plan Task 15.8)

The provisioning seal uses Apple's built-in CryptoKit (X25519 ECDH, HKDF
over SHA-256, AES-256-GCM). The Apple framework ships under Apple's
Export Compliance classification as publicly available encryption source
and does not require an export classification review for compiled apps —
this is the standard position Apple documents at
<https://developer.apple.com/security/cryptography/>.

We deliberately do **not** set `ITSAppUsesNonExemptEncryption=false`,
because that flag is for apps that bundle a third-party cryptography
library outside the platform framework. Adding it would mis-represent
the bundle. iOS App Store review accepts CryptoKit-only apps without the
flag and without CCATS submission.

If a future revision pulls in an additional non-exempt library (a
non-Apple TLS implementation, a custom curve, etc.), re-evaluate this
section **before** that change ships.

## Secrets on the iPad

- `KeychainStore` (plan §4.2): only the **PIN**, only when the user opts in
  ("remember PIN"), and only scoped by hardware-id. PSK, ECDH private
  key and derived key never leave `WiFiPasswordSealer.seal`'s call frame —
  no `print`, no `console.*`, no `UserDefaults`, no file.
- The native bridge never accepts a URL/port/path from the web side
  (`NativeOperation` allowlist; forbidden payload keys). The only HTTP
  destinations the bundle can reach are the 23 named operations; they all
  map to fixed paths on 8000 / 8042 / manual control.

## Web-side safety

- `safeMotionGuard.test.ts` confirms the iPad entry + cockpit view never
  imports the recorded-emotion or dance libraries, never references a
  forbidden daemon path, and never asks the WebRTC stack for an audio
  track.
- `manualControlClient` only calls the five `yrobot.manual.*` operations;
  no `buildApiUrl`, no `fetchWithTimeout`, no `http://localhost` literal.

## Open operator work

- Run the grep from plan §15.2 against the *built* bundle on the
  operator's Mac and paste the output here once it returns empty.
- Device gate from plan §15.9: install a fresh build on a real device and
  read the Local Network + Bluetooth permission dialogs verbatim.