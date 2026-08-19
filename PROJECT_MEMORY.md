# YRobot project memory

This is the stable, repository-local context for agents. Read
[`AGENT_HANDOFF.md`](AGENT_HANDOFF.md) first for mutable work state.

## Goal and architecture

YRobot provides conversation, motion, local face recognition, and safe local
Home Assistant control for a Reachy Mini. The deployed robot runs YRobot at
`http://192.168.1.14:8042`; the official Reachy daemon runs locally on port
`8000` and owns the robot hardware.

- The official daemon owns motors, camera, microphone, speaker, and SDK media
  resources. Do not modify or restart it for normal YRobot changes.
- YRobot is the upper conversation/control layer. The deployed checkout is
  `/home/pollen/YRobot`; the primary local checkout is this repository.
- Exactly one conversation backend runs at a time: `qwen` or `xiaozhi`.
  A QWEN failure must remain visible and must not silently start XIAOZHI.
- QWEN model: `qwen3.5-omni-flash-realtime`.
- Home Assistant control uses explicit local allowlists and local API calls;
  model-selected arbitrary entities/services are not allowed.

## Key paths

- `yrobot/main.py` — application lifecycle and QWEN runtime.
- `yrobot/app_config.py` — Dashboard API, system/audio/camera controls.
- `yrobot/qwen_realtime.py` — realtime WebSocket protocol client.
- `yrobot/qwen_emotion.py` — local emotion, dance, identity and greeting gates.
- `yrobot/faces.py` — local face registry and matching.
- `yrobot/static/` — browser Dashboard.
- `ios/YRobotRemote/` — native SwiftUI iPhone/iPad remote.
- `docs/reachy-mini-yrobot-ops.md` — chronological operations evidence.

## Media and privacy boundaries

- `CameraStreamer` is the sole normal `media.get_frame()` owner. Dashboard
  preview, face recognition, and visual gaze consume its cached JPEG.
- QWEN receives one image only after a narrow visual request; conversation
  audio does not continuously upload images.
- Local face profiles are stored on Reachy at `~/.config/yrobot/faces.json`.
- QWEN/HA configuration and tokens are stored outside Git. Document only that
  the required configuration exists; never print values.

## Durable decisions

Read [`docs/decisions/`](docs/decisions/) before changing runtime ownership,
conversation routing, local media, or Home Assistant behavior. Use
[`docs/runbooks/`](docs/runbooks/) for deployment and verification.
