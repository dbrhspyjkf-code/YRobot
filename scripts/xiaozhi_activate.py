#!/usr/bin/env python3
"""Xiaozhi 激活脚本 — 获取 6 位码 → 用户去网站输入 → 轮询拿到 wsURL。"""
import json, sys, time, uuid, urllib.request

OTA_URL = "https://api.tenclass.net/xiaozhi/ota/"
CLIENT_ID = str(uuid.uuid4())
DEVICE_ID_FILE = "/tmp/xiaozhi_reachy_mac_id"

try:
    with open(DEVICE_ID_FILE) as f:
        DEVICE_ID = f.read().strip()
except FileNotFoundError:
    DEVICE_ID = f"cc:dd:ee:ff:{uuid.uuid4().hex[:4]}:{uuid.uuid4().hex[:4]}"
    with open(DEVICE_ID_FILE, "w") as f:
        f.write(DEVICE_ID)

SYSTEM_INFO = {
    "flash_size": 16777216, "psram_size": 33554432,
    "minimum_free_heap_size": 8388608, "mac_address": DEVICE_ID,
    "chip_model_name": "Reachy Mini",
    "application": {"name": "Reachy", "version": "1.0.0"},
    "board": {"type": "Reachy Mini", "sdk_version": "1.0.0"},
}

def call():
    req = urllib.request.Request(
        OTA_URL,
        data=json.dumps(SYSTEM_INFO).encode(),
        headers={
            "Device-Id": DEVICE_ID, "Client-Id": CLIENT_ID,
            "Activation-Version": "1", "Content-Type": "application/json",
            "User-Agent": "Reachy/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

print(f"Device-Id: {DEVICE_ID}\n")
data = call()

activation = data.get("activation", {})
code = activation.get("code", "")
msg = activation.get("message", "")

if code:
    print(f"🔢 请在 xiaozhi.me 输入激活码: {code}")
    print(f"   {msg}")
    print("\n输入后按回车继续 → ", end="", flush=True)
    input()
    print("\n轮询激活状态…")
    for i in range(30):
        data2 = call()
        ws = data2.get("websocket", {})
        ws_url = ws.get("url", "")
        ws_token = ws.get("token", "")
        if ws_token and ws_token != "test-token":
            full = f"{ws_url}?token={ws_token}" if "?" not in ws_url else f"{ws_url}&token={ws_token}"
            print(f"\n✅ 激活成功！")
            print(f"XIAOZHI_CONV_URL=\"{full}\"")
            sys.exit(0)
        time.sleep(2)
    print("\n❌ 30 秒内未激活，请重试")
else:
    ws = data.get("websocket", {})
    if ws.get("url"):
        print(f"✅ 已激活！wsURL: {ws['url']}")
    else:
        print("未知状态:", json.dumps(data, ensure_ascii=False)[:500])
