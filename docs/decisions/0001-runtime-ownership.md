# 0001 — Preserve official daemon ownership

## Decision

The official Reachy daemon on port `8000` owns Reachy hardware and SDK media:
motors, camera, microphone, speaker, and daemon lifecycle. YRobot runs beside
it at port `8042` as the conversation, motion-orchestration, and Dashboard
layer.

## Consequences

- Normal YRobot deployments restart only `yrobot.service`.
- Do not change the official daemon to add YRobot features.
- Camera consumers use the YRobot `CameraStreamer` cache instead of creating
  extra direct `media.get_frame()` owners.
- Verify both services after a deployment.
