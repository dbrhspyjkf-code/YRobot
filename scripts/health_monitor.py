import os, time, subprocess
t = time.strftime('%Y-%m-%d %H:%M:%S')
try:
    mem = subprocess.run(['free', '-m'], capture_output=True, text=True).stdout.split('\n')[1].split()
    disk = subprocess.run(['df', '-h', '/'], capture_output=True, text=True).stdout.split('\n')[1].split()
    temp_path = '/sys/class/thermal/thermal_zone0/temp'
    temp = open(temp_path).read().strip() if os.path.exists(temp_path) else 'N/A'
    pid = subprocess.run(['pgrep', '-f', 'yrobot.main'], capture_output=True, text=True).stdout.strip()
    net = subprocess.run(['ip', 'addr', 'show', 'wlan0'], capture_output=True, text=True).stdout
    has_net = 'inet ' in net
    load = os.getloadavg()
    temp_c = float(temp) / 1000 if temp != 'N/A' else 0
    mem_used = mem[2] if len(mem) > 2 else '?'
    mem_total = mem[1] if len(mem) > 1 else '?'
    disk_use = disk[4] if len(disk) > 4 else '?'
    line = f"{t} | PID={pid or 'dead'} | NET={'up' if has_net else 'DOWN'} | LOAD={load[0]:.1f} | MEM={mem_used}M/{mem_total}M | DISK={disk_use} | TEMP={temp_c:.0f}C\n"
    with open('/tmp/yrobot-health.log', 'a') as f:
        f.write(line)
    print(line.strip())
except Exception as e:
    with open('/tmp/yrobot-health.log', 'a') as f:
        f.write(f'{t} | ERROR: {e}\n')
    print(f'ERROR: {e}')
