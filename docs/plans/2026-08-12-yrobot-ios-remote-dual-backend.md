# YRobot Remote 双后端 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 构建一个只在局域网工作的原生 iOS/iPadOS YRobot Remote，覆盖当前 Dashboard 能力、Reachy Wi-Fi 迁移能力，并把 XIAOZHI 与 QWEN 作为两个对等、可观察、可验收的正式对话后端。

**Architecture:** iOS 端使用一个共享的 `RobotSession` 管理 Reachy daemon、YRobot Dashboard 和 conversation backend 状态；Camera、Audio、Motion、Tracking、Logs、Privacy 等功能不按后端拆分。后端选择只通过 YRobot 的保存接口修改配置，随后由用户显式重启 YRobot；运行中的后端状态必须从服务端读取，QWEN 失败时保持失败可见，绝不隐式回退到 XIAOZHI。Wi-Fi 配网单独抽象为 REST hotspot 和 BLE 两条 transport，共享 CryptoKit sealed payload 生成器，但不共享 transport 时序。

**Tech Stack:** SwiftUI, Swift 6 strict concurrency, iOS/iPadOS 26, Foundation/URLSession, Network.framework/NWBrowser, CoreBluetooth, CryptoKit, NetworkExtension/NEHotspotConfigurationManager, Security/Keychain, XCTest/URLProtocol, XcodeGen。

---

## 0. 关键修订与不可变规则

本计划取代原设计中“先做一般 Dashboard、再补后端”的隐含顺序。以下规则在实现和验收中必须保持：

1. **XIAOZHI 和 QWEN 是两个正式模式，不是主模式和 fallback 模式。**
2. 配置后端与运行后端分开显示：`configured_backend` 不等于 `running_backend`。
3. 后端切换必须是 `save + 显式重启`，不做热切换。
4. 用户保存 QWEN 后，在重启前允许显示“配置 QWEN、当前仍运行 XIAOZHI、需要重启”。
5. QWEN 启动或连接失败时显示 `QWEN failed` 和错误，不得显示成 `XIAOZHI connected`，不得自动切换。
6. 最近对话必须同时支持：
   - XIAOZHI：`xz stt:`、`xz tts text:`
   - QWEN：`qwen stt:`、`qwen response:`
7. Camera、Audio、Motion、Tracking、Logs、Privacy 由共享功能层实现，两种后端都可用。
8. QWEN voice 只在 QWEN 配置场景显示，保存 voice 后同样需要显式重启；preview 是单独的短会话，不改变正在运行的后端。
9. QWEN 失败不可悄悄回退 XIAOZHI；任何后端回退都必须是用户主动选择并保存后的显式重启结果。
10. Wi-Fi 密码、HA token、DashScope key、XIAOZHI token 不进入日志、UserDefaults、错误文本、fixture、Git 或 UI。
11. `POST /wifi/connect` 禁止使用；只能使用官方 sealed 协议。
12. poweroff、reboot、restart 等可能已执行但响应未知的命令不得自动重试。

## 1. 当前代码基线与工作目录

计划执行前先核对：

- 历史 iOS 基础实现位于提交 `cb5e042` 的 `ios/YRobotRemote/`；当前 QWEN 分支未直接包含该目录，需要恢复/移植后再开发。
- 当前 YRobot 真实服务：daemon `:8000`，Dashboard `:8042`。
- 项目 API 真相表：`workspace-files/.context/api-truth.md`。
- 现有 Dashboard 路由主要位于 `yrobot/app_config.py`，前端行为位于 `yrobot/static/main.js`。
- QWEN/XIAOZHI 共享 marker 和运行约束见项目根 `AGENTS.md` 及 `docs/reachy-mini-yrobot-ops.md`。

实现时遵守项目已有流程：代码实现应在指定 feature worktree，代码改动前备份并生成 manifest SHA-256；采用红→绿回归测试、focused 测试和 narrow Ruff；硬件 acceptance 由操作者在机器人旁确认。

---

## Task 1: 恢复 iOS 基线并建立可重复构建

**Files:**
- Recover: `ios/YRobotRemote/` from commit `cb5e042` into the approved iOS feature worktree
- Inspect: `ios/YRobotRemote/project.yml`
- Inspect: `ios/YRobotRemote/README.md`
- Test: `ios/YRobotRemote/Tests/`
- Create: `docs/superpowers/progress/yrobot-ios-remote-progress.md`

### Step 1: 建立 feature worktree 和改动前备份

确认当前实现 worktree、分支和 production dirty 状态；在进行恢复或代码改动前创建：

```text
/home/pollen/.local/state/yrobot/backups/ios-remote-baseline-YYYYMMDD-HHMMSS/
```

备份必须包含涉及文件的 manifest 和 SHA-256，不得备份任何 secret 文件内容。

### Step 2: 恢复历史 iOS 目录

从 `cb5e042` 恢复 `ios/YRobotRemote/`，不要修改用户手写的两个 `AGENTS.md`。将恢复操作记录为 baseline，而不是把历史实现误标为已完成产品。

### Step 3: 生成工程并先运行现有测试

在 `ios/YRobotRemote` 执行：

```bash
xcodegen generate
xcodebuild -project YRobotRemote.xcodeproj \
  -scheme YRobotRemote \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
```

预期：明确记录当前编译/测试失败；若失败来自历史 fixture 或当前 Swift/Xcode 版本，先建立回归清单，不要在没有证据时大面积重构。

### Step 4: 提交基线恢复

```bash
git add ios/YRobotRemote docs/superpowers/progress/yrobot-ios-remote-progress.md
git commit -m "chore: restore YRobot Remote iOS baseline"
```

commit message 末尾追加唯一 trailer：

```text
Made-with: Proma
```

---

## Task 2: 冻结双后端服务端契约

**Files:**
- Modify: `yrobot/app_config.py`
- Modify: `yrobot/config.py` 或实际运行时状态模块（按当前实现定位）
- Modify: `yrobot/static/main.js`（只在必要时同步 Dashboard 显示）
- Test: `tests/test_app_config.py`
- Test: `tests/test_backend_selection.py`
- Test: `tests/test_qwen_runtime.py`
- Test: `ios/YRobotRemote/Tests/Fixtures/yrobot_status.json`
- Create: `ios/YRobotRemote/Tests/Fixtures/conversation_backend_xiaozhi.json`
- Create: `ios/YRobotRemote/Tests/Fixtures/conversation_backend_qwen.json`

### Step 1: 写失败测试，锁定实际 backend 状态

新增测试覆盖：

- 配置 XIAOZHI、运行 XIAOZHI；
- 配置 QWEN、运行 QWEN；
- 配置已保存为 QWEN 但进程仍运行 XIAOZHI；
- QWEN 运行失败时 `running_backend=qwen` 且错误可见；
- QWEN 失败不会得到 XIAOZHI fallback；
- `/api/status` 不再固定返回 `backend: xiaozhi`；
- `/api/conversation/backend` 返回 configured、running、connection state、error。

运行：

```bash
pytest tests/test_app_config.py tests/test_backend_selection.py tests/test_qwen_runtime.py -q
```

预期：新增测试先失败。

### Step 2: 修正服务端状态来源

`/api/status` 的 conversation backend 摘要必须来自真实 `RUNTIME_HEALTH` 或对应 backend runtime，而不是硬编码 XIAOZHI。完整配置接口保持：

```text
GET /api/conversation/backend
PUT /api/conversation/backend
```

接口语义：

```json
{
  "configured_backend": "qwen",
  "running_backend": "qwen",
  "connection_state": "connected",
  "error": null
}
```

PUT 成功只代表配置已保存：

```json
{
  "configured_backend": "qwen",
  "restart_required": true
}
```

不得在 PUT 后修改运行中 backend。

### Step 3: 修正并验证 QWEN voice 契约

保持：

```text
GET /api/conversation/voice
PUT /api/conversation/voice
POST /api/conversation/voice/preview?voice=<voice>
```

voice preview 使用临时 QWEN session，不修改运行中 session，不改变 configured/running backend。

### Step 4: 运行 focused 验证并提交

```bash
pytest tests/test_app_config.py tests/test_backend_selection.py tests/test_qwen_runtime.py -q
ruff check yrobot/app_config.py yrobot/config.py tests/test_app_config.py tests/test_backend_selection.py tests/test_qwen_runtime.py
```

提交：

```bash
git add yrobot tests ios/YRobotRemote/Tests/Fixtures
git commit -m "fix: expose truthful XIAOZHI and QWEN runtime state"
```

追加 `Made-with: Proma` trailer，并按项目双 commit 流程同步 feature/production 两个 SHA。

---

## Task 3: 实现 iOS 双后端状态模型与显式重启流程

**Files:**
- Modify: `ios/YRobotRemote/Sources/Core/Networking/Models.swift`
- Modify: `ios/YRobotRemote/Sources/Core/Networking/APIClient.swift`
- Modify: `ios/YRobotRemote/Sources/Core/Session/RobotSession.swift`
- Create or modify: `ios/YRobotRemote/Sources/Core/Conversation/ConversationBackendState.swift`
- Test: `ios/YRobotRemote/Tests/YRobotRemoteTests/ModelRoundTripTests.swift`
- Test: `ios/YRobotRemote/Tests/YRobotRemoteTests/RobotSessionTests.swift`
- Create: `ios/YRobotRemote/Tests/YRobotRemoteTests/ConversationBackendTests.swift`

### Step 1: 定义状态模型

实现以下独立字段：

```text
configuredBackend: xiaozhi | qwen
runningBackend: xiaozhi | qwen | unknown
connectionState: not_started | connecting | connected | paused | reconnecting | failed | safe_mode
lastError: String?
restartRequired: Bool
```

`runningBackend` 不得由 `configuredBackend` 推断；必须来自服务端实际状态。

### Step 2: 写失败测试

测试：

1. 读取 XIAOZHI connected；
2. 读取 QWEN connected；
3. QWEN failed 保持 QWEN；
4. PUT backend 只设置 `restartRequired`，不修改 `runningBackend`；
5. restart 后重新 probe 才更新 `runningBackend`；
6. timeout 后状态为 `destructiveActionUnknownResult`，不自动重试；
7. `/api/conversation/backend` 的 503/422 映射正确。

运行：

```bash
xcodebuild -project ios/YRobotRemote/YRobotRemote.xcodeproj \
  -scheme YRobotRemote \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  test -only-testing:YRobotRemoteTests/ConversationBackendTests
```

### Step 3: 实现 RobotSession 共享后端管理

在 `RobotSession` 中增加：

```text
refreshConversationBackend()
setConversationBackend(_:)
refreshConversationVoice()
setConversationVoice(_:)
previewConversationVoice(_:)
```

Camera、Audio、Motion、Logs 方法继续留在共享 session，不创建 `QwenSession` 或 `XiaozhiSession` 两套功能树。

### Step 4: 显式重启后的重新连接

重启流程：

```text
保存 backend/voice
→ restartRequired=true
→ 用户确认 Restart YRobot
→ 不自动重复发送 restart
→ 等待 8042 短暂消失/恢复
→ 重新 probe 8000 和 8042
→ 读取真实 backend 状态
```

重启请求超时只显示“结果未知”，并提供用户主动的“重新检查”按钮。

### Step 5: 验证并提交

```bash
xcodegen generate
xcodebuild -project ios/YRobotRemote/YRobotRemote.xcodeproj \
  -scheme YRobotRemote \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test \
  -only-testing:YRobotRemoteTests/ConversationBackendTests
```

提交：

```bash
git add ios/YRobotRemote
 git commit -m "feat: model explicit XIAOZHI and QWEN backend lifecycle"
```

追加 `Made-with: Proma` trailer。

---

## Task 4: 同时支持两套最近对话日志

**Files:**
- Modify: `ios/YRobotRemote/Sources/Features/Overview/ConversationTurn.swift`
- Modify: `ios/YRobotRemote/Sources/Core/Session/RobotSession.swift`
- Modify: `yrobot/static/main.js`（保持 Web Dashboard 与 iOS 解析规则一致时才修改）
- Test: `ios/YRobotRemote/Tests/YRobotRemoteTests/ConversationTurnTests.swift`
- Create: `ios/YRobotRemote/Tests/Fixtures/yrobot_logs_chat_qwen.json`
- Test: `tests/test_app_config.py` 或对应 chat marker 测试

### Step 1: 写失败测试

覆盖：

```text
xz stt:             → user / XIAOZHI
xz tts text:        → bot / XIAOZHI
qwen stt:           → user / QWEN
qwen response:      → bot / QWEN
```

测试混合日志、乱序日志、缺少 user 或 bot 的半个 turn、同一秒多条日志和空文本。

Conversation turn ID 不得只使用秒级 timestamp + speaker；使用 `timestamp_us`、speaker、backend 或稳定 index 避免冲突。

### Step 2: 实现统一 parser

解析器输出：

```text
speaker
text
timestamp
authoredBackend: xiaozhi | qwen | nil
```

UI 默认展示统一的用户/机器人对话；诊断模式可显示 backend 标签。

### Step 3: 验证

```bash
xcodebuild -project ios/YRobotRemote/YRobotRemote.xcodeproj \
  -scheme YRobotRemote \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test \
  -only-testing:YRobotRemoteTests/ConversationTurnTests
```

同时运行当前 Python chat marker focused tests，确保两端 marker 集合一致。

---

## Task 5: 修正连接发现和 partial failure 行为

**Files:**
- Modify: `ios/YRobotRemote/Sources/Core/Discovery/DiscoverySession.swift`
- Modify: `ios/YRobotRemote/Sources/Core/Discovery/RobotEndpoint.swift`
- Modify: `ios/YRobotRemote/Sources/Core/Session/RobotSession.swift`
- Modify: `ios/YRobotRemote/Sources/Features/Network/NetworkView.swift`
- Test: `ios/YRobotRemote/Tests/YRobotRemoteTests/DiscoverySessionTests.swift`
- Test: `ios/YRobotRemote/Tests/YRobotRemoteTests/RobotEndpointParserTests.swift`
- Test: `ios/YRobotRemote/Tests/YRobotRemoteTests/RobotSessionTests.swift`

### Step 1: 使用真实 resolved endpoint

不要把 Bonjour instance name 简单替换下划线后推导 hostname。持久化：

```text
service instance name
hardware ID
last resolved host/IP
savedAt
```

优先通过 Bonjour endpoint 连接；Bonjour 不可用时才使用 lastHost/manual host。

### Step 2: 定义连接结果

使用明确状态而非单一 Bool：

```text
bothAvailable
 daemonOnly
 yrobotOnly
 unavailable
```

手工输入保存前的规则：

- 两端都可用：保存 preferred robot；
- 仅一端可用：允许临时进入对应能力，并明确提示另一端不可用；是否保存由 UI 策略决定；
- 两端都不可用：不保存。

### Step 3: 后台恢复与 bounded rediscovery

进入 background 时停止状态、日志、聊天和 camera 高频请求；回到 foreground 时：

1. 使用 preferred Bonjour service 重新发现；
2. 失败后使用 lastHost；
3. 失败后显示 manual entry；
4. 重试次数和间隔有上限，不无限轮询。

### Step 4: 验证并提交

```bash
xcodebuild -project ios/YRobotRemote/YRobotRemote.xcodeproj \
  -scheme YRobotRemote \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test \
  -only-testing:YRobotRemoteTests/DiscoverySessionTests \
  -only-testing:YRobotRemoteTests/RobotEndpointParserTests \
  -only-testing:YRobotRemoteTests/RobotSessionTests
```

---

## Task 6: 完成共享 Media、Motion、Logs 和 Settings 功能

**Files:**
- Modify: `ios/YRobotRemote/Sources/Features/Overview/OverviewView.swift`
- Modify: `ios/YRobotRemote/Sources/Features/Overview/StatusCards.swift`
- Modify: `ios/YRobotRemote/Sources/Features/Media/MediaView.swift`
- Modify: `ios/YRobotRemote/Sources/Features/Motions/MotionsView.swift`
- Modify: `ios/YRobotRemote/Sources/Features/Logs/LogsView.swift`
- Modify: `ios/YRobotRemote/Sources/Features/Settings/SettingsView.swift`
- Modify: `ios/YRobotRemote/Sources/Core/Session/RobotSession.swift`
- Test: corresponding `ios/YRobotRemote/Tests/YRobotRemoteTests/*`

### Step 1: Media

实现并测试：

- camera off/start/stale/failed 状态；
- active scene + visible Camera 页面才轮询，每 500ms 一帧；
- 音量 debounce，显示机器人返回值；
- mute 使用 `volume=0` 语义并保存本地 last-non-zero UI 值，不伪造服务端 mute API；
- Reachy 麦克风电平独立于 upload enable；
- VAD 保存失败明确显示 persistence failure。

不要把 Reachy 麦克风电平误称为 iPhone 麦克风，也不要为远程 JPEG/电平申请不需要的 iPhone Camera/Microphone 权限。

### Step 2: Motions

从 `/api/motion` 获取权威列表；保留 Dashboard 的基础、情绪、舞蹈分类；请求进行中禁用冲突按钮；422/409 显示原始可行动错误，不自动重试。

### Step 3: Logs

实现 Chat、Debug、Info、Notice、Warning、Error；Chat 必须同时支持两套 marker；内存最多 2,000 行；clear 只清客户端；离开底部后停止 follow；进入后台停止轮询。

### Step 4: Settings / Conversation

增加独立 Conversation section：

```text
当前配置后端
实际运行后端
连接状态
最近错误
是否需要重启
QWEN voice（仅 QWEN）
试听（仅 QWEN）
```

所有 daemon、YRobot、reboot、poweroff 操作继续使用确认对话框。

---

## Task 7: 实现 REST setup-hotspot 配网

**Files:**
- Create: `ios/YRobotRemote/Sources/Core/WiFi/WifiProvisioningState.swift`
- Create: `ios/YRobotRemote/Sources/Core/WiFi/WifiSealer.swift`
- Create: `ios/YRobotRemote/Sources/Core/WiFi/RestWifiProvisioner.swift`
- Create: `ios/YRobotRemote/Sources/Core/WiFi/HotspotJoiner.swift`
- Modify: `ios/YRobotRemote/Sources/Core/Networking/APIClient.swift`
- Modify: `ios/YRobotRemote/Sources/Features/Network/NetworkView.swift`
- Test: `ios/YRobotRemote/Tests/YRobotRemoteTests/WifiSealerTests.swift`
- Test: `ios/YRobotRemote/Tests/YRobotRemoteTests/RestWifiProvisionerTests.swift`

### Step 1: 先写 CryptoKit known-vector 测试

固定并验证：

```text
X25519
HKDF-SHA256
salt = device PIN UTF-8
info = "reachy-mini-wifi-psk-v1"
AES-GCM
AAD = SSID UTF-8
nonce = 12 bytes
ct = ciphertext + 16-byte tag
```

测试必须断言密码不出现在 sealed request 的明文、URL 或错误消息中。

### Step 2: 实现状态机

状态至少包括：

```text
joiningHotspot
waitingForSettingsReturn
connectedToHotspot
scanningNetworks
selectingSSID
fetchingProvisioningKey
sealingCredentials
submitting
waitingForRobot
rediscovering
success
failed
cancelled
```

REST 接口：

```text
GET  /wifi/status
POST /wifi/scan_and_list
GET  /wifi/error
POST /wifi/reset_error
POST /wifi/forget
GET  /wifi/prov_key
POST /wifi/connect_sealed
POST /wifi/setup_hotspot
```

### Step 3: 验证目标网络恢复

不能只等待 hotspot 消失。成功条件必须包括：手机恢复可用局域网、Reachy 被 Bonjour 或 lastHost 找到、daemon/YRobot 至少达到预期可用性，并重新读取 backend 状态。

---

## Task 8: 实现 BLE-only Wi-Fi provisioning

**Files:**
- Create: `ios/YRobotRemote/Sources/Core/Bluetooth/ReachyBLEConstants.swift`
- Create: `ios/YRobotRemote/Sources/Core/Bluetooth/ReachyBLETransport.swift`
- Create: `ios/YRobotRemote/Sources/Core/Bluetooth/ReachyBLESession.swift`
- Create: `ios/YRobotRemote/Sources/Core/Bluetooth/BLEWifiProvisioner.swift`
- Modify: `ios/YRobotRemote/Sources/Core/WiFi/WifiProvisioningState.swift`
- Modify: `ios/YRobotRemote/Sources/Features/Network/NetworkView.swift`
- Test: `ios/YRobotRemote/Tests/YRobotRemoteTests/BLECommandStateTests.swift`
- Test: `ios/YRobotRemote/Tests/YRobotRemoteTests/BLEWifiProvisionerTests.swift`

### Step 1: 写 BLE transport 状态测试

覆盖：

- 扫描 Reachy 广播；
- 连接 command service `12345678-1234-5678-1234-56789abcdef0`；
- 发现 command characteristic `...abcdef1`；
- 订阅 response characteristic `...abcdef2`；
- 订阅完成前禁止发送异步 Wi-Fi 命令；
- `OK: working` 与后续 notification 分开处理；
- notification timeout；
- disconnect/cancel cleanup。

### Step 2: 实现认证和 Wi-Fi 命令

流程：

```text
scan
→ connect
→ subscribe
→ PIN_<5 chars>
→ WIFI_STATUS
→ WIFI_SCAN
→ select SSID
→ WIFI_KEYEX
→ WifiSealer
→ WIFI_CONNECT_ENC <json>
→ wait/poll WIFI_STATUS
→ rediscover
```

PIN 只在用户明确同意后写入 Keychain；默认不保存。

### Step 3: 处理安全提示和错误

在 UI 显示简短提示：

```text
BLE 配网请靠近机器人，并远离不受信任的附近设备。
官方短 PIN + Just Works pairing 不能防止主动中间人攻击。
```

错误分别显示：wrong PIN、busy、bad password、stale key、daemon unreachable、rediscovery timeout。

---

## Task 9: 实现 iPhone / iPad 自适应导航和权限边界

**Files:**
- Modify: `ios/YRobotRemote/Sources/App/YRobotRemoteApp.swift`
- Modify: `ios/YRobotRemote/Sources/App/Info.plist`
- Modify: `ios/YRobotRemote/project.yml`
- Modify: all relevant `ios/YRobotRemote/Sources/Features/*View.swift`
- Test: UI/layout tests or screenshot checklist

### Step 1: 自适应导航

- iPhone compact：`TabView`；
- iPad regular：`NavigationSplitView`；
- Network wizard 可从 Settings 进入，也可在无机器人时 deep-link 打开；
- Camera、Logs 在 iPad detail area 使用额外宽度；
- 底层 RobotSession 和 feature 行为不分叉。

### Step 2: 清理不必要权限

远程显示 Reachy camera JPEG 不等于使用 iPhone camera；远程显示 Reachy mic level 不等于使用 iPhone microphone。只有确实实现本机摄像头/麦克风功能时才保留对应 usage description。

保留并准确描述：

- Local Network；
- Bluetooth；
- Hotspot Configuration 所需能力/说明。

### Step 3: 验证

在 iPhone 17 和 iPad mini 上检查：

- portrait / landscape；
- iPad split view；
- dark mode；
- Dynamic Type；
- VoiceOver；
- destructive controls 的间距与确认。

---

## Task 10: 双后端自动化和物理 acceptance

**Files:**
- Modify: `ios/YRobotRemote/Tests/Fixtures/`
- Modify: `ios/YRobotRemote/Tests/YRobotRemoteTests/`
- Modify: `tests/`
- Create or modify: `docs/superpowers/progress/yrobot-ios-remote-progress.md`
- Reference: `docs/superpowers/specs/2026-08-10-yrobot-ios-remote-design.md`
- Reference: `.context/api-truth.md`

### Step 1: 自动化测试矩阵

必须同时覆盖 XIAOZHI 和 QWEN：

```text
XIAOZHI connected
XIAOZHI paused
QWEN connected
QWEN reconnecting
QWEN failed without fallback
XIAOZHI → QWEN save + restart
QWEN → XIAOZHI save + restart
QWEN voice list/save/preview
xz markers
qwen markers
```

同时覆盖：

```text
Bonjour
manual fallback
both ports available
 daemon only
yrobot only
both unavailable
camera stale frame
logs 2,000-row bound
power timeout unknown result
REST Wi-Fi
BLE Wi-Fi
```

### Step 2: focused 验证命令

Python 侧：

```bash
pytest tests/test_app_config.py tests/test_backend_selection.py tests/test_qwen_runtime.py -q
ruff check yrobot/app_config.py yrobot/config.py tests/test_app_config.py tests/test_backend_selection.py tests/test_qwen_runtime.py
```

Swift 侧：

```bash
xcodegen generate
xcodebuild -project ios/YRobotRemote/YRobotRemote.xcodeproj \
  -scheme YRobotRemote \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
```

不要用全量 `pytest` 或全量 `ruff` 作为单个修复的唯一闸门；记录 unrelated failure。

### Step 3: 真机验收

在 iPhone 17 和 iPad mini 各自执行：

1. Bonjour 发现 Reachy；
2. daemon/YRobot partial failure；
3. Overview、Camera、Audio、Motion、Logs；
4. XIAOZHI connected 与 paused；
5. QWEN connected、voice preview、voice save + restart；
6. QWEN 失败保持 failed，不出现 XIAOZHI fallback；
7. daemon wake/sleep/restart；
8. YRobot restart；
9. reboot/poweroff；
10. BLE-only Wi-Fi relocation；
11. BLE 不可用时 setup-hotspot recovery；
12. background/foreground、Wi-Fi loss、daemon-only failure、YRobot-only failure；
13. 两小时 foreground soak，检查无重复命令和无界内存增长。

Simulator 不能作为 Bonjour permission、Bluetooth、hotspot joining 或物理机器人行为的验收证据。

### Step 4: 记录 acceptance 和交付状态

进度文档必须分别记录：

```text
XIAOZHI acceptance
QWEN acceptance
LAN control acceptance
BLE provisioning acceptance
Hotspot fallback acceptance
iPhone acceptance
iPad acceptance
```

不得把“日志显示 connected”当作硬件功能可用；涉及声音、动作、摄像头、Wi-Fi 迁移的结果必须由人在机器人旁实际确认。

---

## Revised Delivery Order

建议实际执行顺序：

1. 恢复 iOS 基线并在当前 QWEN 分支重新编译；
2. 冻结并修正双后端服务端契约；
3. 实现 `ConversationBackendState` 和 save + explicit restart；
4. 统一 XIAOZHI/QWEN chat marker parser；
5. 修正 Bonjour resolved endpoint 和 partial failure；
6. 完成共享 LAN Dashboard 功能；
7. 加入 QWEN voice 配置和试听；
8. 实现 REST setup-hotspot provisioning；
9. 实现 BLE-only provisioning；
10. 完成 iPad split view、权限、accessibility 和视觉 polish；
11. 执行双后端自动化矩阵；
12. 执行 iPhone 17 + iPad mini 物理 acceptance。

## Revised Effort Estimate

原来的 9–12 个开发日只适合估算 LAN 控制 MVP，不适合完整范围。新的粗略估计：

| 部分 | 估计 |
|---|---:|
| 恢复历史 iOS 基线和接口漂移修正 | 0.5–1.5 天 |
| LAN MVP（发现、状态、媒体、动作、日志、电源） | 3–5 天 |
| 双后端状态、切换、QWEN voice、对话解析 | 2–3 天 |
| iPad、权限、accessibility、视觉 polish | 1–2 天 |
| REST hotspot provisioning | 1–2 天 |
| BLE-only provisioning | 3–5 天 |
| 双设备 acceptance 和修复 | 2–4 天 |
| **合计** | **12.5–22.5 天** |

主要不确定性来自 Apple iOS 26/Xcode 26 环境、CoreBluetooth 时序、Hotspot Configuration 系统限制、真实机器人 Wi-Fi 迁移和 QWEN 运行状态的现场验证。
