#!/usr/bin/env python3
"""GPIO power-button monitor with a Reachy sleep step before shutdown."""

from __future__ import annotations

import subprocess
import time
import urllib.request
from signal import pause

from gpiozero import Button

SLEEP_URL = "http://127.0.0.1:8000/api/daemon/stop?goto_sleep=true"

shutdown_button = Button(23, pull_up=False)


def _request_sleep() -> None:
    req = urllib.request.Request(SLEEP_URL, method="POST")
    with urllib.request.urlopen(req, timeout=10.0):
        pass


def released() -> None:
    for _ in range(200):
        time.sleep(0.001)
        if shutdown_button.is_pressed:
            return

    print("Shutdown button released, asking Reachy to sleep...", flush=True)
    try:
        _request_sleep()
        time.sleep(1.0)
    except Exception as exc:  # noqa: BLE001 - shutdown should still happen
        print(f"Pre-shutdown sleep failed: {exc}", flush=True)
    print("Powering off...", flush=True)
    subprocess.call(["sudo", "shutdown", "-h", "now"])


shutdown_button.when_released = released
print("Monitoring GPIO23 for graceful shutdown signal...", flush=True)
pause()
