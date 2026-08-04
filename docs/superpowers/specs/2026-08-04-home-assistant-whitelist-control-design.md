# Home Assistant Whitelist Control Design

## Goal

Let Reachy Mini control a small, explicit set of Home Assistant devices through local REST API calls, while keeping the online realtime model away from unrestricted Home Assistant access.

## Current Context

YRobot runs on Reachy Mini and streams microphone audio plus optional camera frames to the MiniCPM-o 4.5 realtime gateway. The app receives `text` and `audio` deltas in `Conversation._on_delta()`. Text fragments are already filtered and logged as `robot: ...`; audio is independently queued for playback.

Home Assistant control is not present today. The new feature must be local to the robot process and must not require changing the realtime gateway protocol.

## Safety Model

The online model may suggest intent in natural language, but only local deterministic code can execute a Home Assistant action.

Allowed actions are an operator-maintained whitelist. Each entry maps a human phrase pattern to exactly one Home Assistant service call. Unknown, ambiguous, security-sensitive, or non-whitelisted requests are ignored for execution.

The first version excludes locks, alarm panels, garage doors, covers, gas, heaters, and other safety-critical or high-risk entities. Those can be added later only with explicit confirmation rules.

## Configuration

Use environment variables for secrets and deployment-specific settings:

- `YROBOT_HA_URL`, for example `http://192.168.1.133:8123`
- `YROBOT_HA_TOKEN`, a Home Assistant long-lived access token
- `YROBOT_HA_ENABLED`, default `0`

Store the token only on the Reachy Mini host, preferably in a root-readable systemd environment file or another local file outside git. Do not print the token in logs, tests, or chat output.

Use a small JSON whitelist file for non-secret device mappings. Proposed default path:

`~/.config/yrobot/home_assistant_whitelist.json`

Example shape:

```json
[
  {
    "name": "客厅灯",
    "phrases": ["打开客厅灯", "客厅灯打开"],
    "service": "light.turn_on",
    "entity_id": "light.living_room"
  }
]
```

## Runtime Flow

1. YRobot receives model text deltas.
2. A small action detector accumulates assistant text until a clear phrase from the whitelist appears.
3. If HA is disabled or config is incomplete, the detector returns no action.
4. If exactly one whitelist entry matches, the HA client calls `/api/services/<domain>/<service>` with `{"entity_id": ...}`.
5. The app logs a non-secret success or failure line with the whitelist entry name.
6. Each assistant response can trigger each whitelist entry at most once to prevent repeated delta fragments from firing the same action multiple times.

## Components

`yrobot/home_assistant.py`

- Parse and validate HA settings.
- Load whitelist JSON.
- Match normalized Chinese command text against phrase lists.
- Call Home Assistant REST services with the standard library `urllib.request`.
- Return structured results so `main.py` logging stays simple.

`yrobot/main.py`

- Initialize the HA controller from settings during `Conversation` setup.
- Feed allowed assistant text fragments to the controller after text gating.
- Reset per-response dedupe state when a new response starts.

`yrobot/config.py`

- Add HA settings to `Settings.from_env()`.
- Keep defaults disabled so existing installs behave unchanged.

## Error Handling

Missing HA URL, missing token, disabled HA, missing whitelist, invalid JSON, unknown phrase, and duplicate trigger are non-fatal no-ops.

HTTP failures are logged without token or request body. The robot continues the conversation even when HA is down.

## Tests

Add unit tests before production code:

- Defaults keep HA disabled.
- Environment variables enable HA settings without exposing token.
- Whitelist matching returns one exact allowed action.
- Unknown text returns no action.
- Duplicate fragments do not fire the same action twice for one response.
- HA REST client sends the expected URL, auth header, and payload using a fake opener.
- HTTP failure returns a failed result without raising into the conversation loop.

## Acceptance Criteria

- Existing YRobot tests pass.
- New HA tests pass locally on the robot.
- With `YROBOT_HA_ENABLED=0`, behavior is unchanged.
- With HA enabled and a one-device whitelist, saying the configured phrase causes one Home Assistant service call.
- No HA token is committed or printed.
