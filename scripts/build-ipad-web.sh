#!/usr/bin/env bash
# Build the iPad cockpit web bundle into the iOS app resources (plan Task 6.4).
#
# Contract:
#   - set -euo pipefail; fails loudly on typecheck/test/build errors.
#   - Runs targeted tests + typecheck before building.
#   - Atomically replaces ios/YRobotRemote/YRobotRemote/Resources/Cockpit
#     (gitignored; only .gitkeep is committed).
#   - Invoked from the Xcode build phase before resources are copied, so
#     Archive never depends on a pre-committed dist.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_APP="$REPO_ROOT/desktop-app"
COCKPIT_DIR="$REPO_ROOT/ios/YRobotRemote/YRobotRemote/Resources/Cockpit"

command -v yarn >/dev/null 2>&1 || { echo "yarn is required" >&2; exit 1; }

echo "[build-ipad-web] typecheck"
(cd "$DESKTOP_APP" && npm run typecheck)

echo "[build-ipad-web] targeted vitest"
(cd "$DESKTOP_APP" && npx vitest run src/ipad --passWithNoTests)

echo "[build-ipad-web] vite build"
(cd "$DESKTOP_APP" && VITE_IPAD_MODE=true npx vite build --config vite.ipad.config.ts)

# Vite keeps the input html filename; the app expects index.html.
if [ -f "$COCKPIT_DIR/ipad.html" ] && [ ! -f "$COCKPIT_DIR/index.html" ]; then
  mv "$COCKPIT_DIR/ipad.html" "$COCKPIT_DIR/index.html"
fi

# vite already emptied and refilled the Cockpit dir (emptyOutDir). Verify the
# essentials and keep the .gitkeep marker present.
test -f "$COCKPIT_DIR/index.html" || { echo "index.html missing after build" >&2; exit 1; }
touch "$COCKPIT_DIR/.gitkeep"

echo "[build-ipad-web] done: $COCKPIT_DIR"
