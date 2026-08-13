# Integrate iOS Remote Implementation Plan

**Goal:** Version the verified YRobot Remote SwiftUI source with the current
YRobot production branch without merging its historical robot-backend branch.

**Method:** Use an isolated branch from production, import only
`ios/YRobotRemote` and iOS documentation, regenerate with XcodeGen, run
XCTest, then fast-forward the verified integration commit into production.

**Constraints:** No `yrobot/`, `tests/`, or `scripts/` path from the old iOS
branch. Exclude `.DS_Store`, `xcuserdata`, and `.xcuserstate`. Do not deploy
iOS source to Reachy.

**Verification:** `xcodegen generate` and
`xcodebuild -quiet -project YRobotRemote.xcodeproj -scheme YRobotRemote -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test`.
