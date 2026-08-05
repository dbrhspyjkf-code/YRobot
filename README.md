---
title: YRobot
emoji: 🤖
colorFrom: indigo
colorTo: pink
sdk: static
pinned: false
short_description: Full-duplex MiniCPM-o conversation for Reachy Mini
tags:
  - reachy_mini
  - reachy_mini_python_app
---

# YRobot

Full-duplex, omni-modal conversation for **Reachy Mini Wireless**, powered by the
**MiniCPM-o 4.5 realtime API** ([docs](https://minicpmo45.modelbest.cn/docs/en/realtime-api/overview/)).

The robot listens, watches, speaks and moves at the same time: you can talk over it and it
hard-stops the interrupted turn after a locally qualified onset, it turns to face whoever
is speaking, and it breathes, glances and dances its antennas while it talks.

```
you ─── voice ─► tuned XVF3800 AEC ─► 20 ms VAD/control ─► 1 s model units ──┐
      camera ─► continuous scene-aware latest-only JPEG worker ───────────────┤
                                                                              ▼
                                wss://…/v1/realtime?mode={audio|video}
                                                                              │
◄── lifelike motion ◄─ 50 Hz choreographer ◄─ DoA compass                    │
◄── speech          ◄─ epoch-tagged speaker ◄─ 24 kHz audio deltas ◄─────────┘
```

## Why it feels responsive

Everything below is encoded in the source with the reasoning attached; this is the map.

| Problem | Mechanism | Where |
|---|---|---|
| Reply latency | Mic/VAD never waits for camera encoding or WebSocket sends. Audio enters a bounded realtime queue; video is latest-only. The MiniCPM-o stream uses its native complete one-second inference units, while adaptive 0.25–0.8 s playback preroll absorbs server jitter | `main.py`, `audio.py` |
| Barge-in | Client-owned and destructive: clear near-end speech takes a high-confidence 140 ms path; ambiguous double-talk takes the conservative 500 ms path. A commit advances the playback epoch and calls SDK `clear_player()`, so interrupted audio cannot replay. Complete one-second inputs then carry `force_listen` until the model returns to listening | `barge.py`, `turn.py`, `main.py` |
| False triggers from its own echo/motors | Every candidate is compared with exact recently scheduled speaker PCM. Echo similarity, unexplained energy and cheap speech-shape checks gate the fast path; head bumps and uncertain residuals must survive the safe path. A 40 ms gap tolerance handles XVF double-talk suppression | `barge.py`, `audio.py` |
| Wooden motion | One 50 Hz thread owns the pose. Breathing and posture cross-fade; gaze and idle saccades both use velocity-limited second-order trajectories, so a new random glance cannot step the head in one tick | `motion.py` |
| Deaf DoA | Samples are gated by locally confirmed user voice, transformed with the daemon's physical head pose, confidence-weighted with the XVF speech flag, circularly averaged, and dead-banded | `motion.py`, `main.py` |
| Visual blindness / context rot | Camera capture continues while either side speaks and stays alive across Gateway rotation. Conversation frames publish at 1 fps; idle frames publish on scene change plus a 3 s heartbeat, always latest-only. Video sessions rotate before the 300 s cap at a quiet boundary and carry a bounded assistant-side continuity hint; capture, send and true handoff gaps are logged | `vision.py`, `session.py`, `main.py` |
| Generic voice / passive behaviour | Optional LLM and TTS reference WAVs use the documented `session.init.voice` fields. Video mode also adds a restrained proactive-observation policy; both features are explicit environment settings | `config.py`, `realtime.py` |

## Protocol in one paragraph

Connect to `wss://HOST/v1/realtime?mode=audio` for voice-only or `mode=video` for
camera input, wait for `session.queue_done`, send
`session.init` (the system prompt must start with the trained line `You are a helpful
assistant.` — a free-form persona drifts the model out of its duplex distribution), wait
~14 s for `session.created`, then stream complete `input.append` inference units: exactly
16,000 base64 float32 16 kHz mono samples (one second), a unique `input_id`, optional
`force_listen`, and—only in video mode—base64 JPEG `video_frames`. The server streams
`response.output.delta` events with `kind ∈ {listen, text, audio}` (audio is 24 kHz
float32); **only `listen` is an utterance boundary** — text and audio are independent
streams. YRobot uses `input_id` for strong local causality when the gateway echoes it;
because the public protocol does not require that echo, it also has a guarded fallback:
an untagged `listen` is accepted only after the latest forced packet has actually crossed
the socket and no newer voice has appeared. See `realtime.py` and `turn.py`.

## Run

On the robot (Python 3.12 venv on the CM4), or any machine that can reach the daemon:

```bash
pip install -e .
cp .env.example .env   # optional; the default already uses the official public Gateway
yrobot
```

The shipped default is the official public Gateway at
`wss://minicpmo45.modelbest.cn/v1/realtime?mode=video`, with continuous frames; it does
not require a locally deployed model Host. Voice-only mode is an explicit fallback: use
`mode=audio`, set `YROBOT_SEND_VIDEO=0`, and proactive vision is disabled automatically.
Optional reference-voice and tuning variables are documented in
[`.env.example`](.env.example).

It also registers as a Reachy Mini app (`reachy_mini_apps` entry point `yrobot`), so the
dashboard can start and stop it. While the app is running, use its settings icon to change
the Gateway, TLS verification, continuous video, proactive observation and the one-line
persona. Dashboard values are stored in `~/.config/yrobot/settings.json`; daemon/process
environment variables take precedence, and saved changes apply after restarting YRobot.

## Privacy

YRobot processes microphone and camera input off-device:

- While the app runs, 16 kHz microphone audio is streamed to the configured MiniCPM-o
  realtime Gateway.
- In video mode, JPEG camera frames are also transmitted—about 1 fps during conversation,
  with scene-change delivery and a bounded idle heartbeat. Selecting audio-only mode in the
  settings page disables camera transmission.
- YRobot does not write microphone recordings or camera frames to local storage. Remote
  processing, retention and access are governed by the operator of the Gateway you select.

Review this disclosure before running the app around other people, and obtain any consent
required in your location.

Development without hardware:

```bash
pip install -e ".[dev]"
pytest && ruff check .
```

Before publishing to the Reachy Mini app store, also run:

```bash
reachy-mini-app-assistant check .
```

The automated checks do not replace the Wireless hardware acceptance gates in
[`plan.md`](plan.md).

## Layout

```
yrobot/config.py     env → one frozen Settings dataclass; URL normalization
yrobot/app_config.py dashboard settings API + atomic per-user persistence
yrobot/realtime.py   gateway protocol client + <think>-leak filter
yrobot/turn.py       barge-in state machine (pure logic, fully unit-tested)
yrobot/audio.py      mic framing, VAD stack, 24→16 k resampler, epoch speaker
yrobot/barge.py      echo-aware fast/safe acoustic barge-in qualification
yrobot/vision.py     continuous scene-aware, latest-only camera worker
yrobot/session.py    quiet-boundary rotation + bounded continuity memory
yrobot/motion.py     DoA sound compass + 50 Hz choreographer
yrobot/main.py       wiring, session rotation, ReachyMiniApp + CLI
yrobot/static/       Reachy Mini dashboard settings interface
```

Every module docstring states the non-obvious constraint it encodes (gateway behaviour
verified live, SDK threading rules, XVF3800 quirks). If you change a number, read the
docstring above it first. See [`plan.md`](plan.md) for the real-hardware acceptance gates.

## License

Apache-2.0
