#!/usr/bin/env bash
# Build YRobotRemote for either Simulator or a real iOS device.
#
# Usage:
#   scripts/build.sh                          # build for the default iPhone 17 Pro simulator + test
#   scripts/build.sh device                   # build for the connected iPhone (requires USB + unlocked)
#   scripts/build.sh device <UDID>            # build for a specific connected device
#   scripts/build.sh test                     # test-only (no install)
#   scripts/build.sh clean                    # nuke DerivedData
#
# All commands are deliberately idempotent: re-running with the same
# arguments is safe.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

ACTION="${1:-simulator}"
DEVICE_UDID="${2:-}"

# -- helpers ----------------------------------------------------------------

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "✗ $1 not found in PATH" >&2
    exit 1
  }
}

step() { printf "\n\033[1;36m▶ %s\033[0m\n" "$1"; }
ok()   { printf "\033[1;32m✓ %s\033[0m\n" "$1"; }
fail() { printf "\033[1;31m✗ %s\033[0m\n" "$1" >&2; exit 1; }

# -- guards -----------------------------------------------------------------

require xcodegen
require xcodebuild
require xcrun

step "Regenerate Xcode project from project.yml"
xcodegen generate

# -- actions -----------------------------------------------------------------

case "$ACTION" in
  simulator|sim)
    DESTINATION='platform=iOS Simulator,name=iPhone 17 Pro'
    step "Build for iPhone 17 Pro simulator"
    xcodebuild \
      -project YRobotRemote.xcodeproj \
      -scheme YRobotRemote \
      -configuration Debug \
      -destination "$DESTINATION" \
      build | tail -5
    ok "Built for simulator."

    step "Run tests on iPhone 17 Pro simulator"
    xcodebuild \
      -project YRobotRemote.xcodeproj \
      -scheme YRobotRemote \
      -configuration Debug \
      -destination "$DESTINATION" \
      test 2>&1 | tail -10
    ok "Tests passed."
    ;;

  device)
    if [ -z "$DEVICE_UDID" ]; then
      # First device listed in devicectl.
      DEVICE_UDID="$(xcrun devicectl list devices 2>/dev/null \
        | awk '/-- Devices --/,/--/ {print}' \
        | grep -E "iPhone|iPad" \
        | grep -oE '\([A-F0-9-]{36}\)' \
        | head -1 \
        | tr -d '()')"
    fi
    if [ -z "$DEVICE_UDID" ]; then
      fail "No connected iOS device found. Plug in + unlock the iPhone, then re-run."
    fi
    step "Build for connected device $DEVICE_UDID"
    xcodebuild \
      -project YRobotRemote.xcodeproj \
      -scheme YRobotRemote \
      -configuration Debug \
      -destination "platform=iOS,id=$DEVICE_UDID" \
      -allowProvisioningUpdates \
      build 2>&1 | tail -5
    ok "Built for device."

    APP_PATH="$(xcodebuild -project YRobotRemote.xcodeproj \
      -scheme YRobotRemote \
      -configuration Debug \
      -destination "platform=iOS,id=$DEVICE_UDID" \
      -showBuildSettings 2>/dev/null \
      | awk -F' = ' '/ BUILT_PRODUCTS_DIR /{p=$2} /WRAPPER_NAME = /{n=$2} END{print p"/"n}')"
    if [ ! -d "$APP_PATH" ]; then
      fail "Could not locate built .app at $APP_PATH"
    fi
    step "Install on device $DEVICE_UDID"
    xcrun devicectl device install app --device "$DEVICE_UDID" "$APP_PATH"
    ok "Installed. Launch with: xcrun devicectl device process launch --device $DEVICE_UDID ai.proma.yrobotremote"
    ;;

  test)
    step "Run tests on default iPhone 17 Pro simulator"
    xcodebuild \
      -project YRobotRemote.xcodeproj \
      -scheme YRobotRemote \
      -configuration Debug \
      -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
      test 2>&1 | tail -10
    ;;

  clean)
    step "Clean DerivedData"
    rm -rf ~/Library/Developer/Xcode/DerivedData/YRobotRemote-*
    ok "Cleaned."
    ;;

  *)
    cat <<EOF
Usage: $0 [simulator|device [UDID]|test|clean]

Examples:
  $0 simulator               # build + test on iPhone 17 Pro sim
  $0 device                  # build + install on first connected iPhone
  $0 device 00008150-001430691480401C
  $0 test                    # test-only on iPhone 17 Pro sim
  $0 clean                   # nuke DerivedData
EOF
    exit 2
    ;;
esac
