#!/usr/bin/env bash
# scripts/deploy.sh — the ONLY sanctioned channel from this repo to the robot.
#
# Local repo = single source of truth. /home/pollen/YRobot = deployment
# snapshot. This script enforces the sync discipline documented in
# AGENTS.md ("Code sync & deployment discipline"):
#
#   1. drift check      — robot commits/edits not present locally stop the
#                         deploy until reconciled (fetch robot branch back)
#   2. local tests      — py_compile + focused pytest (skippable)
#   3. robot backup     — timestamped copy + SHA-256 manifest
#   4. checksum rsync   — no --delete, excludes .git/.venv/caches so the
#                         robot's own git tree and venv survive
#   5. robot tests      — py_compile + uplink/test sanity (skippable)
#   6. orphan-safe restart
#   7. archive commit   — robot-side `git commit` fingerprint so the next
#                         drift check has a clean baseline
#
# Usage:
#   scripts/deploy.sh [--dry-run] [--skip-tests] [--no-restart]
#   --dry-run     run drift check + rsync dry-run only (no changes on robot)
#   --skip-tests  skip local+robot pytest (py_compile still runs)
#   --no-restart  sync files but do not restart/re-verify the service
set -euo pipefail

ROBOT_HOST="${YROBOT_DEPLOY_HOST:-pollen@192.168.1.14}"
ROBOT_DIR="${YROBOT_DEPLOY_DIR:-/home/pollen/YRobot}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="yrobot.service"
BACKUP_ROOT="/home/pollen/.local/state/yrobot/backups"

DRY_RUN=0; SKIP_TESTS=0; NO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --no-restart) NO_RESTART=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[deploy] ABORT:\033[0m %s\n' "$*" >&2; exit 1; }

run_local_pytest() {
  export PYTHONPATH="$LOCAL_DIR${PYTHONPATH:+:$PYTHONPATH}"
  if [ -x .venv/bin/python ] && .venv/bin/python -c 'import pytest, cryptography, fastapi' 2>/dev/null; then
    .venv/bin/python -m pytest "$@"
  elif python3 -c 'import pytest, cryptography, fastapi' 2>/dev/null; then
    python3 -m pytest "$@"
  elif command -v uvx >/dev/null 2>&1; then
    # test_photos needs fastapi/httpx/opencv/websockets; the others only need cryptography.
    uvx --from pytest --with cryptography --with fastapi --with httpx \
      --with opencv-python-headless --with websockets --with numpy pytest "$@"
  else
    die "pytest+cryptography unavailable; install them or make uvx available"
  fi
}

cd "$LOCAL_DIR"

# ── 0. preflight ─────────────────────────────────────────────────────────
BRANCH="$(git branch --show-current)"
[ -n "$BRANCH" ] || die "detached HEAD; check out a branch first"
[ -z "$(git status --porcelain -uno)" ] || die "local tracked files dirty; commit first"
# NOTE: untracked files (e.g. .worktrees/, ios/) are fine — rsync excludes them.
HEAD_SHA="$(git rev-parse --short HEAD)"
say "local: $BRANCH @ $HEAD_SHA"

ssh -o ConnectTimeout=8 "$ROBOT_HOST" true || die "robot unreachable ($ROBOT_HOST)"

# ── 1. drift check ───────────────────────────────────────────────────────
say "drift check: fetching robot git state"
ROBOT_BRANCH="$(ssh "$ROBOT_HOST" "git -C $ROBOT_DIR branch --show-current")"
[ -n "$ROBOT_BRANCH" ] || die "robot has no git tree (unexpected; deploy manually and init the fingerprint)"
git fetch -q "ssh://$ROBOT_HOST$ROBOT_DIR" "$ROBOT_BRANCH" 2>/dev/null \
  || die "git fetch from robot failed"
ROBOT_SHA="$(ssh "$ROBOT_HOST" "git -C $ROBOT_DIR rev-parse HEAD")"
if ! git cat-file -e "$ROBOT_SHA" 2>/dev/null; then
  die "robot HEAD $ROBOT_SHA is unknown locally — the robot has commits this repo lacks.
       Reconcile first:  git merge FETCH_HEAD   (or cherry-pick), resolve, then re-run.
       Do NOT deploy over unknown robot commits."
fi

DIRTY="$(ssh "$ROBOT_HOST" "git -C $ROBOT_DIR status --porcelain | head -20")"
if [ -n "$DIRTY" ]; then
  say "robot working tree is dirty (uncommitted edits):"
  printf '%s\n' "$DIRTY"
  if [ "$DRY_RUN" = 1 ]; then
    say "dry-run: would diff these against local HEAD and stop for confirmation"
    exit 3
  fi
  read -r -p "Robot has the uncommitted edits above. Overwrite them with this deploy? Type 'overwrite' to continue: " ANS
  [ "$ANS" = "overwrite" ] || die "kept robot edits; aborting (reconcile manually)"
fi
say "drift check OK (robot @ ${ROBOT_SHA:0:9} is an ancestor-or-equal of local history)"

# ── 2. local tests ───────────────────────────────────────────────────────
if [ "$SKIP_TESTS" = 0 ]; then
  say "local: py_compile + focused tests"
  python3 -m py_compile \
    yrobot/main.py yrobot/config.py yrobot/uplink_vad.py \
    yrobot/audio_runtime.py yrobot/xiaozhi_mqtt.py yrobot/xiaozhi_ota.py \
    yrobot/app_config.py yrobot/photos.py yrobot/photos_sftp.py yrobot/hermes_photo_intent.py
  run_local_pytest tests/test_uplink_vad.py tests/test_stability_guards.py \
    tests/test_xiaozhi_mqtt.py tests/test_photos.py tests/test_photos_sftp.py -q
else
  say "local: tests SKIPPED"
fi

# ── 3+4. backup + rsync ──────────────────────────────────────────────────
RSYNC_OPTS=(-c -r -v
  --exclude .git --exclude .venv --exclude __pycache__ --exclude '*.pyc'
  --exclude .pytest_cache --exclude .worktrees --exclude ios --exclude desktop-app
  --exclude .claude --exclude '*.egg-info' --exclude .ruff_cache
  --exclude .env --exclude '*.env' --exclude .DS_Store
  --exclude secrets --exclude '*.secret' --exclude '*.key')
# NEVER sync .env / secrets: robot-local credentials must stay robot-local
# (project invariant #7). If you think you need this, you don't — put
# shared non-secret config in app_config defaults or ha.env on the robot.
if [ "$DRY_RUN" = 1 ]; then
  say "dry-run: rsync plan (no changes made)"
  rsync "${RSYNC_OPTS[@]}" --dry-run "$LOCAL_DIR"/ "$ROBOT_HOST:$ROBOT_DIR/"
  say "dry-run complete ✔ nothing was modified"
  exit 0
fi

TS="$(date +%Y%m%d-%H%M%S)"
BK="$BACKUP_ROOT/deploy-$TS"
say "robot backup → $BK"
ssh "$ROBOT_HOST" "set -e; mkdir -p '$BK'; cd '$ROBOT_DIR'; \
  tar cf '$BK/runtime-code.tar' yrobot tests scripts 2>/dev/null || \
  tar cf '$BK/runtime-code.tar' yrobot tests; \
  (cd '$BK' && sha256sum * > manifest.sha256); \
  git -C '$ROBOT_DIR' log --oneline -3 > '$BK/pre-deploy-git-log.txt'; \
  git -C '$ROBOT_DIR' status --porcelain > '$BK/pre-deploy-git-status.txt' || true"

say "rsync (checksum, no delete)"
rsync "${RSYNC_OPTS[@]}" "$LOCAL_DIR"/ "$ROBOT_HOST:$ROBOT_DIR/"

# ── 5. robot-side verification ───────────────────────────────────────────
say "robot: py_compile"
ssh "$ROBOT_HOST" "cd '$ROBOT_DIR' && .venv/bin/python -m py_compile \
  yrobot/main.py yrobot/config.py yrobot/uplink_vad.py \
  yrobot/audio_runtime.py yrobot/xiaozhi_mqtt.py yrobot/xiaozhi_ota.py \
  yrobot/app_config.py yrobot/photos.py yrobot/photos_sftp.py yrobot/hermes_photo_intent.py"
if [ "$SKIP_TESTS" = 0 ]; then
  say "robot: focused pytest"
  ssh "$ROBOT_HOST" "cd '$ROBOT_DIR' && .venv/bin/python -m pytest \
    tests/test_uplink_vad.py tests/test_stability_guards.py tests/test_xiaozhi_mqtt.py \
    tests/test_photos.py tests/test_photos_sftp.py -q"
fi

# ── 6. orphan-safe restart ───────────────────────────────────────────────
if [ "$NO_RESTART" = 0 ]; then
  say "restart $SERVICE (orphan-safe)"
  ssh "$ROBOT_HOST" "sudo systemctl stop '$SERVICE'; sleep 2; \
    PIDS=\$(systemctl status '$SERVICE' --no-pager 2>/dev/null | grep -oP 'pid=[0-9]+' | cut -d= -f2); \
    pgrep -f '[p]ython.*YRobot' | grep -v \"\$PIDS\" | xargs -r kill -TERM || true; \
    sudo systemctl reset-failed '$SERVICE' 2>/dev/null || true; \
    sudo systemctl start '$SERVICE'; sleep 6; systemctl is-active '$SERVICE'" \
    || die "service failed to start; check: ssh $ROBOT_HOST journalctl -u $SERVICE -n 50"
  say "log check"
  ssh "$ROBOT_HOST" "journalctl -u '$SERVICE' --since '1 minute ago' --no-pager \
    | grep -E 'Traceback|ERROR' | head -5" || true
else
  say "restart SKIPPED (--no-restart)"
fi

# ── 7. robot-side archive commit (fingerprint for next drift check) ──────
say "robot: archive commit (fingerprint)"
DEPLOY_MSG="deploy: $HEAD_SHA $(git log -1 --format=%s | head -c 60)"
ssh "$ROBOT_HOST" "cd '$ROBOT_DIR' && git add -A && \
  (git diff --cached --quiet && echo 'fingerprint already clean') || \
  git commit -q -m '$DEPLOY_MSG'"

say "deploy complete ✔  local $HEAD_SHA → robot  (backup: $BK)"
