---
title: YRobot
emoji: 🤖
colorFrom: indigo
colorTo: pink
sdk: static
pinned: false
short_description: Xiaozhi voice conversation and motion control for Reachy Mini Wireless
tags:
  - reachy_mini
  - reachy_mini_python_app
---

# YRobot

YRobot is a Xiaozhi cloud voice application for Reachy Mini Wireless. It provides
wake-word-gated conversation, TTS playback, camera face tracking, sound-direction
tracking, expressive motion, and a small dashboard for runtime health and controls.

## Runtime architecture

```
16 kHz mono mic -> VAD -> Opus -> Xiaozhi WebSocket
                                      |
                         STT / TTS / emotion events
                                      |
24 kHz TTS -> bounded aplay queue -> speaker
                                      |
                 Choreographer command queue -> 50 Hz set_target
                 face tracker / SoundCompass -> gaze commands
```

`Choreographer` is the only normal writer of Reachy pose targets. External callers
queue mode, gaze, stillness, and action requests; the motion thread applies them in
order. Mode changes cross-fade, gaze uses a velocity-limited spring, and listening
freezes the antennas before blending them back after speech input ends.

## Xiaozhi conversation

The production WebSocket endpoint and token are read from environment variables:

```bash
export XIAOZHI_CONV_URL="wss://api.tenclass.net/xiaozhi/v1/"
export XIAOZHI_TOKEN="..."
export XIAOZHI_DEVICE_ID="..."  # optional; wlan0 MAC is the fallback
```

Do not commit the token. The application sends 16 kHz mono Opus frames and accepts
the server's negotiated TTS sample rate. TTS playback is isolated from the receive
coroutine, uses a bounded latest-frame queue, and recovers when the server stops
sending packets without sending `tts.stop`.

The default wake word is `小白`. Before wake, Xiaozhi STT may still identify nearby
speech, but YRobot does not play the response. After wake, each confirmed speech
burst refreshes the 60-second conversation window. `/tmp/yrobot_force_wake` containing
`1` is reserved for testing and should normally be absent.

## Motion and hardware safety

- Motors are enabled before the motion writer starts, with three bounded startup attempts.
- Startup gaze parameters remain limited during the initial eight-second rise.
- `set_target` failures are rate-limited and counted rather than flooding the journal.
- Antennas freeze while listening and blend back over 0.4 seconds.
- DoA USB errors back off instead of terminating the tracking thread.
- Camera HTTP calls, WebSocket sends, and WebSocket setup have finite timeouts.
- Runtime health records motor readiness, WebSocket state, reconnects, TTS packet timing,
  audio queue depth/drops, and motion loop statistics.

The hardware daemon remains responsible for low-level servo control. Changing the
Python motion loop from 50 Hz to a higher number is not considered a stability fix by
itself; verify daemon readiness, power, temperature, and undervoltage logs first.

## Dashboard

The Reachy Mini dashboard is served at port `8042` in the deployment. It exposes:

- machine and service state;
- motion mode, current action, loop frequency and deadline misses;
- Xiaozhi WebSocket/session and TTS health;
- bounded audio queue depth and dropped-frame count;
- camera preview and face tracking;
- speaker volume, microphone enablement and VAD threshold;
- recent conversation and service logs.

VAD persistence updates only `YROBOT_VAD_RMS_MIN` in the configured env file and
preserves unrelated settings such as the Home Assistant token.

## Run and deploy

On the robot, or in an environment with the Reachy Mini SDK:

```bash
pip install -e .
yrobot
```

The application is also registered through the `reachy_mini_apps` entry point. The
known deployment uses `pollen@192.168.1.14:/home/pollen/YRobot` and the systemd unit
`yrobot.service`. Stop/start is preferred over `systemctl restart` when deploying
remotely so an SSH session is not interrupted during hardware initialization.

The daemon should be fully ready before YRobot starts. See
[`docs/reachy-mini-yrobot-ops.md`](docs/reachy-mini-yrobot-ops.md) for deployment,
health log, Wi-Fi watchdog, camera, audio and undervoltage troubleshooting notes.

## Development checks

Hardware-free checks:

```bash
python3 -m compileall -q yrobot scripts
pytest
ruff check .
```

The full test suite must run against the current Xiaozhi code. Obsolete legacy module tests are intentionally not part of the suite.

## Layout

```
yrobot/main.py       Xiaozhi WebSocket, audio pipelines and app lifecycle
yrobot/motion.py     SoundCompass and the 50 Hz Choreographer
yrobot/audio.py      VAD and dashboard microphone signal
yrobot/audio_runtime.py bounded playback queue and TTS watchdog
yrobot/env_store.py  atomic per-key env persistence
yrobot/app_config.py dashboard routes and runtime status
yrobot/state.py      thread-safe service/runtime health snapshots
yrobot/static/       dashboard UI
scripts/health_monitor.py  periodic robot/service/network health probe
tests/               current Xiaozhi, motion and stability tests
docs/plans/          implementation plans
```

## Privacy and secrets

Microphone audio is sent to the configured Xiaozhi service while audio input is
enabled. Camera frames are used by the local tracker and dashboard according to the
current camera setting. Do not commit Xiaozhi, Home Assistant, or any other service
tokens.

## License

Apache-2.0
