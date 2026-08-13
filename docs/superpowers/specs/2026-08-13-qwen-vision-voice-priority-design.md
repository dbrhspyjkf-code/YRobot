# QWEN Vision With Voice Priority Design

## Goal

Keep Reachy visual interaction useful without reducing wake-word reliability,
ASR accuracy, or deterministic spoken-control behavior.

## Evidence and scope

On 2026-08-13, `YROBOT_SEND_VIDEO=0` restored accurate QWEN recognition for
Shenzhen and Guangzhou weather questions.  However, live inspection showed
that visual work was still active: `SpeakerTracker` starts the Dashboard
camera streamer through loopback HTTP, which captures and JPEG-encodes frames
at 2 fps.  With video enabled, `LatestCamera` starts a second independent
`media.get_frame()` capture loop and sends images during active speech.

This change is limited to QWEN visual scheduling and its camera consumers. It
does not change the official Reachy daemon, microphone device, VAD threshold,
ASR prompts, wake-word aliases, Home Assistant rules, or tools.

## Design

### One camera owner

YRobot will have one shared latest-frame provider. It alone calls
`media.get_frame()`, resizes, and JPEG-encodes a frame. Dashboard preview,
visual gaze/face tracking, face recognition, and QWEN image upload consume
that cached frame; they must not create another camera capture loop or request
frames through YRobot's own HTTP API.

The provider keeps only the newest frame, has no queue, and starts only while
at least one visual consumer is enabled. A stopped provider releases its worker
thread and cached image. Existing Dashboard state and preview endpoints remain
compatible.

### Independent visual roles

The following are independent, explicit roles:

| Role | Purpose | During active speech |
| --- | --- | --- |
| Dashboard preview | Human browser preview | May read cached frames; never causes a second capture loop |
| Visual gaze | Low-rate face direction for head tracking | Can inspect a small cached frame; no QWEN upload |
| Face recognition | Identify a known person for persona context | Deferred until speech ends |
| QWEN vision | Answer a visual question | Send a snapshot only after ASR has finished |

`YROBOT_SEND_VIDEO` remains the user-visible switch for QWEN visual answers.
It must not silently disable Dashboard preview or visual gaze. Any new internal
flag is limited to lifecycle control; no speculative settings are added to the
Dashboard.

### Voice-priority scheduling

When QWEN is receiving microphone audio for an active turn, the system must
not call `input_image_buffer.append`, run face recognition, or start a camera
capture job. Audio append and turn commit remain the only realtime input work.

After transcription completes, YRobot classifies the final transcript with a
narrow local visual-intent matcher (for example: `看`, `看到`, `看见`,
`什么`, `这是什么`, `描述`, `画面`, `前面`, `手里`). If it matches and
QWEN vision is enabled, YRobot appends the newest shared JPEG once, then asks
QWEN for the response. Non-visual turns append no images. A visual follow-up
receives one current snapshot per turn, rather than a rolling 1 fps history.

If no cached frame is ready, YRobot continues the normal text/audio response
without an image; a missing image must never block ASR or enter safe mode.

### Resource limits

The shared frame remains bounded to a 640-pixel long edge and an image payload
below QWEN's existing 190 KB decoded-image limit. Face direction may run at
the existing low rate, but face recognition executes only after a speech turn.
There is no image upload loop while a user is speaking.

## Failure handling

- A camera read, JPEG encode, or image upload failure is logged at debug or
  warning level and skips only that visual result.
- QWEN audio/WebSocket errors retain their existing reconnect behavior.
- Audio, wake-word, and local spoken-control paths do not depend on camera
  availability.
- The official daemon on port 8000 remains untouched; only YRobot is restarted
  for deployment.

## Acceptance criteria

1. With `YROBOT_SEND_VIDEO=1`, there is exactly one YRobot camera capture
   worker and no `media.get_frame()` call from a second QWEN-specific worker.
2. While speaking "今天深圳天气怎么样" or "打开吸顶灯", logs show no QWEN
   image-append event before the final transcript is handled.
3. After "小白，你看到什么？", QWEN receives one current image after the
   transcript and produces a visual answer.
4. Dashboard camera preview and visual head tracking continue to work from the
   shared frame source.
5. Ten repeated weather/control utterances and five visual utterances produce
   their expected transcript/response class without safe mode or QWEN session
   failure. CPU and response timestamps are recorded in the operations log for
   comparison with the current baseline.

## Out of scope

- Replacing QWEN ASR with a local model.
- Moving visual inference to Orange Pi.
- Changing Home Assistant mappings, stock/weather tools, persona, or wake
  rules.
- Continuous scene monitoring or proactive visual narration.
