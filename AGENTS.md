# Agent entrypoint

Before modifying this repository, read `AGENT_HANDOFF.md`, then inspect the
relevant source and verify the live state when the task affects Reachy.

Keep `AGENT_HANDOFF.md` current after each completed stage. Record only facts,
commands, and observed results. Never put credentials, tokens, or other
secrets in repository files, commits, logs, or chat.

## Code sync & deployment discipline (added 2026-08-19)

This repository (the Mac-side clone) is the SINGLE source of truth.
The robot's `/home/pollen/YRobot` is a deployment snapshot only.

1. **All code changes happen in this local repo** — by any agent
   (Codex, Proma, Claude) or human. Never edit robot files directly.
2. **Deploy only via `scripts/deploy.sh`** (flags: `--dry-run`,
   `--skip-tests`, `--no-restart`). It performs drift detection, backup
   with SHA-256 manifest, checksum rsync (no delete), robot-side
   py_compile/pytest, orphan-safe restart, and a robot-side archive
   commit. No ad-hoc `scp` of single files.
3. **The robot's git tree is a fingerprint, not a history**: its
   `git log` shows which deploy is live; a dirty `git status` on the
   robot means someone bypassed this rule — reconcile before deploying.
4. **If on-robot work is unavoidable** (live debugging), commit it on
   the robot (`git add -A && git commit -m '...'`) so the next deploy's
   drift check can `git fetch` it back into this repo and nothing
   drifts silently.

Detailed runbook: `docs/reachy-mini-yrobot-ops.md` § "Deploy script
and sync discipline".
