"""Wait until the Reachy Mini daemon is reachable through the SDK.

This script is used as the yrobot.service ExecStartPre readiness gate. The
Reachy SDK can leave non-daemon worker threads alive after a successful probe,
so the success path exits the process directly instead of waiting for Python
interpreter shutdown.
"""

import os
import sys
import time


def _exit(code: int) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


for attempt in range(1, 16):
    try:
        from reachy_mini import ReachyMini

        reachy = ReachyMini()
        status = reachy.client.get_status()
        motor_mode = status.backend_status.motor_control_mode.name
        print(
            "SDK connected, motors=%s ready=%s"
            % (motor_mode, status.backend_status.ready),
            flush=True,
        )
        if motor_mode == "Disabled":
            reachy.enable_motors()
            print("motors enabled", flush=True)
        reachy.client.disconnect()
        print("daemon OK (attempt %d)" % attempt, flush=True)
        _exit(0)
    except Exception as exc:
        print("attempt %d: %s" % (attempt, exc), flush=True)
    time.sleep(2)

print("daemon not responding after 30s", flush=True)
_exit(1)
