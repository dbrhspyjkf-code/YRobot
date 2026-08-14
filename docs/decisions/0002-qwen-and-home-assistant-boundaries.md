# 0002 — Keep conversation and Home Assistant control bounded

## Decision

YRobot runs either `qwen` or `xiaozhi`, never both media sessions at once.
QWEN is configured for `qwen3.5-omni-flash-realtime`; a failure is shown to
the operator and must not silently activate XIAOZHI.

Home Assistant actions are selected by YRobot's local allowlist and executed
through the local Home Assistant API. The realtime model cannot choose an
arbitrary entity or service.

## Consequences

- Keep backend switching restart-based and explicit.
- Keep tokens, API keys, and HA credentials outside the repository.
- Add a device control mapping only with a narrow, observed voice phrase and a
  corresponding test.
- Block high-risk devices unless they receive separate explicit approval.
