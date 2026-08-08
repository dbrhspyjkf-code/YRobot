#!/usr/bin/env python3
"""Reachy Mini health monitor — logs system + YRobot status every minute."""
import os, time, subprocess
from datetime import datetime

LOG = "/home/pollen/yrobot-health.log"
MAX_LINES = 2000  # rotate log if it grows too large

t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
try:
    # ── System metrics ──────────────────────────────────────
    mem = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout.split('\n')[1].split()
    disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.split('\n')[1].split()
    temp_path = "/sys/class/thermal/thermal_zone0/temp"
    temp = open(temp_path).read().strip() if os.path.exists(temp_path) else "N/A"
    temp_c = float(temp) / 1000 if temp != "N/A" else 0
    load = os.getloadavg()
    
    # ── YRobot process ──────────────────────────────────────
    pid = subprocess.run(["pgrep", "-f", "yrobot.main"], capture_output=True, text=True).stdout.strip()
    
    # ── Network ─────────────────────────────────────────────
    net = subprocess.run(["ip", "addr", "show", "wlan0"], capture_output=True, text=True).stdout
    has_net = "inet " in net
    
    # ── Daemon ──────────────────────────────────────────────
    daemon_pid = subprocess.run(
        ["pgrep", "-f", "reachy_mini.daemon.app.main"],
        capture_output=True, text=True
    ).stdout.strip().split('\n')[0] if has_net else ""  # take first PID only
    daemon_port = subprocess.run(
        ["ss", "-tlnp"], capture_output=True, text=True
    ).stdout
    daemon_alive = ":8000" in daemon_port
    
    # ── Recent YRobot errors (last 5 minutes) ──────────────
    try:
        errors = subprocess.run(
            ["sudo", "journalctl", "-u", "yrobot", "--no-pager", "-S", "5 min ago", "-o", "cat"],
            capture_output=True, text=True, timeout=5
        ).stdout
        error_lines = [l for l in errors.split('\n') if 'ERROR' in l or 'Traceback' in l or 'CRITICAL' in l]
        error_count = len(error_lines)
        last_error = error_lines[-1][:120] if error_lines else ""
    except Exception:
        error_count = -1
        last_error = "journalctl failed"
    
    # ── Top memory consumers ────────────────────────────────
    top_raw = subprocess.run(
        ["ps", "-eo", "pid,comm,%mem", "--sort=-%mem", "--no-headers"],
        capture_output=True, text=True
    ).stdout.strip().split('\n')[:3]
    top_mem = " | ".join(l.split()[1][:20] for l in top_raw if l.strip())
    
    # ── Build log line ──────────────────────────────────────
    parts = [
        f"t={t}",
        f"pid={pid or 'DOWN'}",
        f"net={'UP' if has_net else 'DOWN'}",
        f"load={load[0]:.1f}/{load[1]:.1f}/{load[2]:.1f}",
        f"mem={mem[2] if len(mem) > 2 else '?'}M/{mem[1] if len(mem) > 1 else '?'}M",
        f"disk={disk[4] if len(disk) > 4 else '?'}",
        f"temp={temp_c:.0f}C",
        f"daemon={'UP' if daemon_alive else 'DOWN'}",
        f"errors={error_count}",
    ]
    if last_error:
        parts.append(f"last_err={last_error}")
    if top_mem:
        parts.append(f"top={top_mem}")
    
    line = " | ".join(parts) + "\n"
    
    # ── Rotate if too large ─────────────────────────────────
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 500_000:
            with open(LOG) as f:
                old_lines = f.readlines()
            with open(LOG, "w") as f:
                f.writelines(old_lines[-MAX_LINES:])
    except Exception:
        pass
    
    with open(LOG, "a") as f:
        f.write(line)
    
    # Also print to stdout for cron mail
    print(line.strip())
    
except Exception as e:
    with open(LOG, "a") as f:
        f.write(f"{t} | FATAL: {e}\n")
    print(f"FATAL: {e}")

# ── Network watchdog: restart WiFi if gateway unreachable ──────
GATEWAY = "192.168.1.1"
WATCHDOG_STATE = "/tmp/wifi_watchdog_count"
def _ping(host, timeout=3):
    return subprocess.run(["ping", "-c", "1", "-W", str(timeout), host],
                          capture_output=True).returncode == 0
try:
    existing = open(WATCHDOG_STATE).read().strip() if os.path.exists(WATCHDOG_STATE) else "0"
    fail_count = int(existing) if existing.isdigit() else 0
except Exception:
    fail_count = 0
if not _ping(GATEWAY):
    fail_count += 1
    with open(WATCHDOG_STATE, "w") as f:
        f.write(str(fail_count))
    if fail_count >= 3:
        with open(LOG, "a") as f:
            f.write(f"{t} | WATCHDOG: WiFi restart after {fail_count} ping failures, gateway {GATEWAY}\n")
        subprocess.run(["sudo", "nmcli", "device", "disconnect", "wlan0"], capture_output=True, timeout=10)
        time.sleep(2)
        subprocess.run(["sudo", "nmcli", "device", "connect", "wlan0"], capture_output=True, timeout=10)
        with open(WATCHDOG_STATE, "w") as f:
            f.write("0")
else:
    with open(WATCHDOG_STATE, "w") as f:
        f.write("0")
