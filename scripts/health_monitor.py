#!/usr/bin/env python3
"""Reachy Mini health monitor: system, service and runtime probes."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from urllib.request import urlopen

LOG = "/home/pollen/yrobot-health.log"
MAX_LINES = 2000
GATEWAY = "192.168.1.1"
WATCHDOG_STATE = "/tmp/wifi_watchdog_count"


def run_cmd(command: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def first_line(value: str) -> str:
    return value.strip().splitlines()[0] if value.strip() else ""


def http_probe(url: str) -> tuple[bool, str]:
    try:
        with urlopen(url, timeout=3) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
        return True, body
    except Exception as exc:
        return False, str(exc)


def ping(host: str, timeout: int = 3) -> bool:
    return run_cmd(["ping", "-c", "1", "-W", str(timeout), host], timeout + 2).returncode == 0


def load_metrics() -> tuple[str, str, float, str]:
    mem_lines = run_cmd(["free", "-m"]).stdout.splitlines()
    disk_lines = run_cmd(["df", "-h", "/"]).stdout.splitlines()
    mem = mem_lines[1].split() if len(mem_lines) > 1 else []
    disk = disk_lines[1].split() if len(disk_lines) > 1 else []
    temp_path = "/sys/class/thermal/thermal_zone0/temp"
    try:
        temp_c = float(open(temp_path, encoding="utf-8").read().strip()) / 1000
    except (OSError, ValueError):
        temp_c = 0.0
    load = os.getloadavg()
    return (
        f"{mem[2] if len(mem) > 2 else '?'}M/{mem[1] if len(mem) > 1 else '?'}M",
        disk[4] if len(disk) > 4 else "?",
        temp_c,
        f"{load[0]:.1f}/{load[1]:.1f}/{load[2]:.1f}",
    )


def main() -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mem, disk, temp_c, load = load_metrics()
    pid = first_line(run_cmd(["pgrep", "-f", "yrobot.main"]).stdout)
    net_output = run_cmd(["ip", "addr", "show", "wlan0"]).stdout
    has_net = "inet " in net_output
    daemon_pid = first_line(run_cmd(["pgrep", "-f", "reachy_mini.daemon.app.main"]).stdout)
    daemon_port = ":8000" in run_cmd(["ss", "-tlnp"]).stdout
    dashboard_ok, _ = http_probe("http://127.0.0.1:8042/api/status")
    daemon_api_ok, daemon_body = http_probe("http://127.0.0.1:8000/api/daemon/status")
    daemon_running = daemon_api_ok and "running" in daemon_body.lower()

    journal = run_cmd(
        ["sudo", "journalctl", "-u", "yrobot", "--no-pager", "-S", "5 min ago", "-o", "cat"],
        timeout=5,
    ).stdout
    error_lines = [
        line for line in journal.splitlines()
        if "ERROR" in line or "Traceback" in line or "CRITICAL" in line
    ]
    top_raw = run_cmd(["ps", "-eo", "pid,comm,%mem", "--sort=-%mem", "--no-headers"]).stdout
    top_mem = " | ".join(
        line.split()[1][:20]
        for line in top_raw.strip().splitlines()[:3]
        if line.split()
    )

    parts = [
        f"t={timestamp}",
        f"pid={pid or 'DOWN'}",
        f"net={'UP' if has_net else 'DOWN'}",
        f"load={load}",
        f"mem={mem}",
        f"disk={disk}",
        f"temp={temp_c:.0f}C",
        f"daemon={'UP' if daemon_pid and daemon_port else 'DOWN'}",
        f"daemon_api={'UP' if daemon_running else 'DOWN'}",
        f"dashboard={'UP' if dashboard_ok else 'DOWN'}",
        f"errors={len(error_lines)}",
    ]
    if error_lines:
        parts.append(f"last_err={error_lines[-1][:120]}")
    if top_mem:
        parts.append(f"top={top_mem}")
    line = " | ".join(parts) + "\n"

    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 500_000:
            old_lines = open(LOG, encoding="utf-8").readlines()
            with open(LOG, "w", encoding="utf-8") as output:
                output.writelines(old_lines[-MAX_LINES:])
    except OSError:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as output:
            output.write(line)
    except OSError:
        pass
    print(line.strip())


if __name__ == "__main__":
    main()

# Network watchdog runs after the main snapshot so a slow reconnect does not
# block the service-health line indefinitely.
try:
    existing = open(WATCHDOG_STATE, encoding="utf-8").read().strip() if os.path.exists(WATCHDOG_STATE) else "0"
    fail_count = int(existing) if existing.isdigit() else 0
except (OSError, ValueError):
    fail_count = 0

if not ping(GATEWAY):
    fail_count += 1
    try:
        with open(WATCHDOG_STATE, "w", encoding="utf-8") as output:
            output.write(str(fail_count))
    except OSError:
        pass
    if fail_count >= 3:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(LOG, "a", encoding="utf-8") as output:
                output.write(f"{now} | WATCHDOG: WiFi restart after {fail_count} ping failures, gateway {GATEWAY}\n")
        except OSError:
            pass
        run_cmd(["sudo", "nmcli", "device", "disconnect", "wlan0"], timeout=10)
        time.sleep(2)
        run_cmd(["sudo", "nmcli", "device", "connect", "wlan0"], timeout=10)
        try:
            with open(WATCHDOG_STATE, "w", encoding="utf-8") as output:
                output.write("0")
        except OSError:
            pass
else:
    try:
        with open(WATCHDOG_STATE, "w", encoding="utf-8") as output:
            output.write("0")
    except OSError:
        pass
