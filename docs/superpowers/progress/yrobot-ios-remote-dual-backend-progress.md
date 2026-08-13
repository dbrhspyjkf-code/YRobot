# YRobot iOS Remote — Final Progress (2026-08-12)

> 计划：`docs/plans/2026-08-12-yrobot-ios-remote-dual-backend.md`
> iOS worktree：`/Users/leenzhou/Projects/YRobot-ios-cb5e042`（分支 `codex/ios-remote-dual-backend`）
> 后端 worktree：`/Users/leenzhou/Projects/YRobot-reachy-current`（分支 `reachy-stable-2026-08-12-qwen`）
> 备份：`/Users/leenzhou/.local/state/yrobot/backups/ios-remote-baseline-20260812-172101/`

## 完成总览

| 阶段 | 提交 | 内容 |
|---|---|---|
| Task 1 | (baseline) cb5e042 | iOS 历史基线恢复，Xcode 26.5 / iOS 26 SDK 编译通过 |
| Task 2 | 42425f2 | 后端 `build_status` 不再硬编码 backend；XIAOZHI/QWEN 真实运行态可见 |
| Task 3 | 6d2ebed | iOS `ConversationBackendState` / `ConversationVoiceState` + 显式重启 |
| Task 4 | cb98a94 | iOS `ConversationTurn` 同时解析 xz 和 qwen 四种 marker；ID 用微秒时间戳 |
| Task 5 | 9e41339 | iOS `ConnectionResult` 四态 + bounded `rediscover()` |
| Task 6 | 2793ca2 | `ConversationView` 设置页；UI 标签；移除 iPhone 摄像头/麦克风权限 |
| Task 7 | d729b73 | `WifiSealer`（X25519+HKDF+AES-GCM）+ `RestWifiProvisioner` + 状态机 |
| Task 8 | 0296abd | `ReachyBLETransport` + `ReachyBLESession` + `BLEWifiProvisioner` + Mock |
| Task 9 | b6f6863 | iPad `NavigationSplitView`；`NEHotspotConfiguration` 描述 |
| Task 10 | (本文档) | 双后端 fixtures + 物理 acceptance 清单 |

测试基线变化：

```text
cb5e042 baseline           74 iOS tests
+ Task 3 (ConversationBackendTests)    +9  →  83
+ Task 4 (ConversationTurn tests)      +6  →  89
+ Task 5 (ConnectionResultTests)       +7  →  96
+ Task 6 (UI 完善 + 权限清理)            0  →  96
+ Task 7 (WifiSealer + REST)          +11  → 107
+ Task 8 (BLE provisioner)            +6  → 113
+ Task 10 (model round-trips)          +5  → 118 (ModelRoundTripTests alone 18)
```

最终 iOS test 套件：**118 tests across 16 test files**，全部通过。

后端 focused tests：14 个 `tests/test_backend_selection.py` 全部通过；broader 109/115，6 个 unrelated failures（VAD 默认值、SonOS schema）按规则不阻塞。

## 关键不变量（已锁为自动化测试）

```text
✓ QWEN failed → running_backend="qwen" + connection_state="failed" + last_error 非空
✓ QWEN 失败不得回退 XIAOZHI
✓ PUT /api/conversation/backend 不修改 running_backend
✓ restartRequired 仅在用户保存后翻转
✓ restart timeout 不得自动重试
✓ 422 → validation, 503 → unavailableSubsystem
✓ bothAvailable / daemonOnly / yrobotOnly / unavailable 四态独立
✓ 完全失败时不保存 preferred robot
✓ rediscover 5 次后停止，不无限轮询
✓ ConversationTurn ID 用微秒时间戳，不再冲突
✓ WifiSealer 字节级 round-trip 与 daemon 一致
✓ BLE async ACK 与 notification 解耦
✓ BLE wrong PIN 立即失败（不等 90s）
✓ BLE cooperative cancellation
✓ Info.plist 不申请 iPhone 摄像头/麦克风权限
```

## 双后端 fixtures

新增 fixtures（`ios/YRobotRemote/Tests/Fixtures/`）：

| 文件 | 用途 |
|---|---|
| `conversation_backend_xiaozhi.json` | XIAOZHI connected |
| `conversation_backend_qwen.json` | QWEN connected |
| `conversation_backend_qwen_failed.json` | QWEN failed 状态 + error |
| `conversation_voice.json` | QWEN voice picker (Ethan + 14 voices) |
| `yrobot_logs_chat_qwen.json` | 8 行 QWEN chat log |

`ModelRoundTripTests` 增加 5 个 case 覆盖这些 fixtures，每个 case 都做完整 decode → encode → decode round-trip，确保 Codable 与真实服务字节级一致。

## 物理 Acceptance 清单

> iOS Simulator 不能作为 Bonjour permission、Bluetooth、hotspot joining、physical robot 行为的验收证据。所有需要真机/机器人旁确认的项都必须由操作者在机器人旁完成。

### 设备
- iPhone 17（iOS 26）
- iPad mini（iPadOS 26）
- Reachy Mini（pollen@192.168.1.14）

### 通用前置

- [ ] 备份 `~/.local/state/yrobot/backups/ios-remote-acceptance-<timestamp>/` 含 manifest SHA-256
- [ ] iPhone 17 / iPad mini 都开 Developer Mode
- [ ] 两个设备都信任 Apple ID 开发者身份
- [ ] App 通过 Xcode 直接安装
- [ ] production YRobot 已 sync feature commit `42425f2`（见 `progress` 旧版 production 同步清单）

### 双后端 acceptance

- [ ] **XIAOZHI connected**：iPhone 显示 `running=xiaozhi, connection=connected`
- [ ] **XIAOZHI paused**：禁用麦克风上传后显示 `connection=paused`
- [ ] **QWEN connected**：iPad 显示 `running=qwen, connection=connected`
- [ ] **QWEN reconnecting**：临时断网后状态变为 `reconnecting`，网络恢复后回到 `connected`
- [ ] **QWEN failed**：制造 DashScope 鉴权错误，确认界面显示 `running=qwen, connection=failed, error=...`，不出现 `xiaozhi connected`
- [ ] **XIAOZHI → QWEN**：保存 QWEN → 显示 "Restart to apply" → 显式 Restart YRobot → 等待重启完成 → 重新 probe → 显示 `qwen connected`
- [ ] **QWEN → XIAOZHI**：反向同样验证
- [ ] **QWEN voice 列表**：显示 14 个可用 voice
- [ ] **QWEN voice 试听**：选 Ethan → Preview 听到中文 sample
- [ ] **QWEN voice 保存**：选 Serena → 保存 → 显示 "Restart to apply"
- [ ] **xz marker 解析**：XIAOZHI 模式对话记录正常显示 user/bot 双方
- [ ] **qwen marker 解析**：QWEN 模式对话记录正常显示 user/bot 双方
- [ ] **跨后端混合**：切换后端后最近对话混合显示
- [ ] **restart timeout**：故意制造 `/api/system/restart` 超时，确认 iOS 不自动重试

### LAN control acceptance

- [ ] **Bonjour 发现**：iPhone 和 iPad 都能在 Robot tab 看到 Reachy
- [ ] **daemon 8000 不可用**：制造 8000 故障，确认 iOS 显示 `daemonOnly`，仍可控制 YRobot
- [ ] **YRobot 8042 不可用**：制造 8042 故障，确认 iOS 显示 `yrobotOnly`，daemon 状态仍可见
- [ ] **partial failure UI**：顶部 banner 显示哪个子系统不可用
- [ ] **Camera**：打开 → 500ms 拉帧 → JPG 显示最新帧
- [ ] **Camera 失败保留最后一帧**：让 robot 拉帧失败，确认保留最后帧 + 错误指示
- [ ] **Motions**：基础/情绪/舞蹈各玩一个，确认机器人真的动了
- [ ] **Logs**：Chat / Debug / Info / Notice / Warning / Error 6 个 filter 切换；scroll 到顶部不自动 follow；Clear 只清客户端
- [ ] **Logs 2,000 行上限**：超过 2,000 行后 ring buffer 截断
- [ ] **Daemon wake**：点击 Wake → 机器人动起来
- [ ] **Daemon sleep**：点击 Sleep → 机器人停
- [ ] **Daemon restart**：daemon 短暂不可达，恢复后 iOS 自动重连
- [ ] **YRobot restart**：点 Restart YRobot → 服务短暂消失 → 恢复
- [ ] **Reboot 机器人**：发出 reboot → 确认 graceful head movement → 等待网络恢复 → 重连
- [ ] **Power off**：发 poweroff → 确认 graceful head movement → 必须物理按电源开机

### iPhone / iPad 双设备 acceptance

- [ ] **iPhone portrait**：UI 不破
- [ ] **iPhone landscape**：UI 不破
- [ ] **iPad portrait**：NavigationSplitView 左侧 sidebar + 右侧 detail
- [ ] **iPad landscape**：同上
- [ ] **Dark mode**：所有页面配色合理
- [ ] **Dynamic Type**：smallest / largest 都不破
- [ ] **VoiceOver**：关键控件可被读出

### Wi-Fi migration acceptance

> **BLE-only Wi-Fi provisioning** 需要真机 CoreBluetooth 行为；模拟器不能作为证据。

- [ ] **BLE Wi-Fi 重定位**：把 Reachy 拿到新 Wi-Fi → iPhone 走 BLE 流程 → PIN 5 位 → scan → 选 SSID → 输入密码 → 加密提交 → 机器人连上 → rediscovery
- [ ] **BLE 错误路径**：故意输错 PIN → 显示 "wrong PIN" 不挂起
- [ ] **BLE stale kid**：制造 kid 不一致 → 一次 fresh key exchange + retry
- [ ] **BLE 主动 MITM 警告**：UI 显示 "perform near the robot, away from untrusted devices"
- [ ] **Hotspot fallback**：关闭手机蓝牙 → iPhone 加入 `reachy-mini-ap` → 走 REST 流程 → 同样成功
- [ ] **Hotspot 自动 join**：iOS 接受 Hotspot Configuration prompt 后自动连上
- [ ] **Hotspot 手动 join**：iOS 拒绝自动 join → 显示 "Open Settings to join reachy-mini-ap" → 手动 join 后回 App
- [ ] **WIFI_FORGET**：忘记已保存的网络 → robot 回到默认

### 性能与稳定性

- [ ] **两小时 foreground soak**：iPhone 17 跑 2 小时 status polling + 间歇 camera；无内存泄漏；无重复命令
- [ ] **background/foreground**：App 后台 1 分钟后回前台，确认 Bonjour 重新发现 + 状态正常
- [ ] **Wi-Fi loss**：飞行模式 30s → 关闭 → iOS 自动 rediscovery
- [ ] **daemon-only failure**：daemon 挂掉时 YRobot 仍可用
- [ ] **YRobot-only failure**：YRobot 挂掉时 daemon 状态仍可见

## Production 同步清单（最终）

SSH 进 Reachy 后执行：

```bash
ssh pollen@192.168.1.14
cd /home/pollen/YRobot

# 1. 备份当前 production
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=/home/pollen/.local/state/yrobot/backups/prod-final-${TIMESTAMP}
mkdir -p "${BACKUP}"
git rev-parse HEAD > "${BACKUP}/source_head.txt"
find yrobot/app_config.py tests/test_backend_selection.py -type f -not -path '*/.git/*' \
  | xargs -r shasum -a 256 > "${BACKUP}/source_sha256.txt"

# 2. 同步 feature commit 42425f2（Task 2 修复）
cp /Users/leenzhou/Projects/YRobot-reachy-current/yrobot/app_config.py \
   /home/pollen/YRobot/yrobot/app_config.py
cp /Users/leenzhou/Projects/YRobot-reachy-current/tests/test_backend_selection.py \
   /home/pollen/YRobot/tests/test_backend_selection.py

git add yrobot/app_config.py tests/test_backend_selection.py
git -c user.name='Proma Agent' -c user.email='agent@proma.local' commit -m \
  "fix(prod): surface truthful XIAOZHI and QWEN runtime state in /api/status

Mirror of feature commit 42425f2.

Made-with: Proma"

# 3. focused 验证
PYTHONPATH=. /home/pollen/YRobot/.venv/bin/python -m pytest \
  /home/pollen/YRobot/tests/test_backend_selection.py -q

# 4. 重启 yrobot.service 让新 build_status 生效
sudo systemctl restart yrobot.service
journalctl -u yrobot.service --since "1 minute ago" --no-pager | tail -20

# 5. 人工确认
curl -s http://127.0.0.1:8042/api/conversation/backend | python3 -m json.tool
```

## 风险与未决

1. **iOS 物理 acceptance 必须由操作者在机器人旁完成**。本会话无法远程完成。
2. **QWEN live 状态需先 SSH 确认当前 yrobot 是 XIAOZHI 还是 QWEN**。可以根据 `/home/pollen/.config/yrobot/ha.env` 里的 `YROBOT_CONVERSATION_BACKEND` 判断。
3. **personal team provisioning** 会每周过期；长期使用需要付费 Apple Developer Program。
4. **iPad split view** 的 sidebar 模式是单列（list with buttons）。如果未来需要更复杂的多级导航，可升级到 `NavigationSplitView(sidebar: ...)` 多列。
5. **plan 中关于"YRobot 和 iOS 工程同步开发"的取舍**：当前 production 已带 Task 2 fix（`42425f2`），iOS Tasks 3-10 在 iOS worktree `codex/ios-remote-dual-backend` 中，最终需通过 cherry-pick 或 PR 合入主分支。
