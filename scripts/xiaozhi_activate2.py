#!/usr/bin/env python3
"""Xiaozhi 激活脚本 — 按 ESP32 固件的协议走 OTA 激活。"""

import json, sys, time, uuid, urllib.request

OTA_URL = "https://api.tenclass.net/xiaozhi/ota/"
DEVICE_ID_FILE = "/tmp/xiaozhi_reachy_device_id"
CLIENT_ID = str(uuid.uuid4())

# 读或生成 Device-Id（用 MAC 风格——服务器可能要求特定格式）
try:
    with open(DEVICE_ID_FILE) as f:
        DEVICE_ID = f.read().strip()
except FileNotFoundError:
    DEVICE_ID = str(uuid.uuid4()).replace("-", "")[:12]
    with open(DEVICE_ID_FILE, "w") as f:
        f.write(DEVICE_ID)

print(f"Device-Id: {DEVICE_ID}")
print(f"Client-Id: {CLIENT_ID}\n")

def call(path=""):
    req = urllib.request.Request(
        f"{OTA_URL}{path}",
        headers={"Device-Id": DEVICE_ID, "Client-Id": CLIENT_ID},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

# 查询
print("查询激活状态…")
data = call()
print(json.dumps(data, ensure_ascii=False, indent=2)[:600])

# 检查 activation 字段
activation = data.get("activation")
if isinstance(activation, dict):
    code = activation.get("code", "")
    msg = activation.get("message", "")
    if code:
        print(f"\n🔢 请在 xiaozhi.me 输入 6 位激活码: {code}")
        print(f"   {msg}")
        print(f"   Device-Id: {DEVICE_ID}")
        print("\n输入后按回车重试…")
        input()
        data2 = call()
        websocket = data2.get("websocket")
        if isinstance(websocket, dict) and websocket.get("url"):
            full = websocket["url"]
            print(f"\n✅ 已激活！")
            print(f"wsURL: {full}")
            sys.exit(0)
        else:
            print("仍未激活，请稍后再试")
            print(json.dumps(data2, ensure_ascii=False, indent=2)[:600])
    else:
        print("已在激活流程中但无 code")
else:
    # 可能已激活
    websocket = data.get("websocket")
    if isinstance(websocket, dict) and websocket.get("url"):
        print(f"\n✅ 已激活！wsURL: {websocket['url']}")
    else:
        print("\n❌ 未激活且无 activation 字段")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:600])
