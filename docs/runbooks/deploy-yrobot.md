# Deploy and verify YRobot

## Scope and safety

This runbook deploys YRobot only. It does not modify the official Reachy daemon
on port `8000`. Credentials are supplied by the operator's secure local SSH
configuration; do not add them to commands, documents, logs, or commits.

## Preflight

From the local repository root:

```bash
git status --short
PYTHONPATH=. uvx --from pytest pytest -q tests/test_qwen_emotion.py
PYTHONPATH=. uv run --no-project --with pytest --with opencv-python python -m pytest -q tests/test_faces.py
python3 -m py_compile yrobot/main.py yrobot/qwen_emotion.py
git diff --check
```

Use the tests for the files changed; do not claim unrelated full-suite failures
are caused by the current patch.

## Deploy

Copy only reviewed files to `/home/pollen/YRobot`. Compile them on Reachy, then
restart only YRobot:

```bash
ssh pollen@192.168.1.14 \
  'cd /home/pollen/YRobot && .venv/bin/python -m py_compile yrobot/main.py && sudo systemctl restart yrobot.service'
```

If deployment includes other Python modules, list every target explicitly in
the copy command and compile each changed module. Preserve the robot-local
environment files, including HA configuration.

## Health check

```bash
curl -fsS --max-time 8 http://192.168.1.14:8042/api/status
ssh pollen@192.168.1.14 'systemctl is-active yrobot.service reachy-mini-daemon.service'
```

Accept only when YRobot and the official daemon are `active`, the configured
backend is expected, QWEN reports `connected` when selected, and
`runtime.last_error` is `null`.

## Live acceptance

Command output is insufficient for hardware behavior. Ask the operator to
perform the relevant spoken, motion, audio, camera, or device-control test.
Record the exact request and acceptance result in `AGENT_HANDOFF.md` and
`docs/reachy-mini-yrobot-ops.md`.
