# YRobot Qwen Realtime Project Progress

**Last updated:** 2026-08-10

## Objective

Add a YRobot setting that selects exactly one conversation backend:
`XIAOZHI` or `QWEN`. QWEN uses `qwen3.5-omni-flash-realtime`, supports
multilingual realtime speech and interruption, and executes only allowlisted
local tools.

## Current status

**Phase:** Planning complete; isolated implementation not started.

## Completed

- Confirmed the official Reachy Mini daemon is running at `192.168.1.14:8000`,
  version 1.9.0, with motors enabled, media available, and a healthy ~49 Hz
  control loop.
- Confirmed the production app is `/home/pollen/YRobot`, run by
  `yrobot.service`, with environment from `/home/pollen/.config/yrobot/ha.env`.
- Confirmed the model `qwen3.5-omni-flash-realtime` supports realtime audio,
  semantic interruption, multilingual speech, and WebSocket Function Calling.
- Approved restart-based XIAOZHI/QWEN switching with no silent fallback.
- Wrote and committed the design as `a4f238e`.
- Audited the live dirty working tree and documented that implementation must
  not overwrite its uncommitted restructure or `.bak` files.
- Probed `192.168.1.200:8766`: `/health` and `/weather` work, but `/mcp` and
  `/sse` do not. It is currently treated as a REST tool adapter, not a standard
  MCP endpoint.
- Wrote the implementation plan at
  `docs/superpowers/plans/2026-08-10-yrobot-qwen-realtime-backend.md`.

## Decisions that must remain stable

- Do not factory-reset the robot or modify the official daemon.
- Keep `你好小白` explicitly registered.
- Default to XIAOZHI when no backend setting exists.
- Switching is save + explicit YRobot restart, not a hot swap.
- QWEN failure stays visible and never silently activates XIAOZHI.
- Model is fixed to `qwen3.5-omni-flash-realtime`.
- Secrets remain robot-local and are never shown by the dashboard.
- Appliance execution is deterministic and whitelist-bound.
- Do not expose generic MCP-discovered tools to the model.

## Active risks

- Production contains extensive uncommitted source changes and tracked
  deletions. Implementation must use an isolated worktree built from a clean
  snapshot of the active runtime.
- The currently known Hermes endpoint is REST, not a verified MCP transport.
  Native MCP integration remains gated on an exact endpoint and contract.
- Real interruption quality depends on Reachy speaker echo behavior; automatic
  tests cannot replace an audible ten-interruption hardware test.
- Existing full pytest discovery references modules currently deleted from the
  production working tree. Feature-specific tests are authoritative until that
  unrelated migration is reconciled.

## Next action

Create the isolated `codex/qwen-realtime` worktree and commit a secret-free
snapshot of the active runtime before changing implementation files.

## Verification log

| Date | Stage | Evidence | Result |
|---|---|---|---|
| 2026-08-10 | Official daemon | `/api/daemon/status`, `/api/media/status`, `/api/motors/status` | Healthy |
| 2026-08-10 | YRobot source audit | SSH read-only inspection of branch and working tree | Dirty tree preserved |
| 2026-08-10 | Hermes endpoint | `/health` 200; `/weather` live; `/mcp` and `/sse` 404 | REST only |
| 2026-08-10 | Design | Commit `a4f238e` | Approved |

## Update protocol

After every implementation task, append:

1. task and commit;
2. files changed;
3. exact tests and results;
4. live deployment evidence, if any;
5. blocker or risk change;
6. next action.

Never record API keys, tokens, passwords, or unredacted credential values.
