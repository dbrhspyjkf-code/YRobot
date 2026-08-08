# YRobot Stability Improvements Implementation Plan

**Goal:** 修复 YRobot 启动、配置持久化、音频/WebSocket 恢复和运动线程边界问题，提升 Reachy Mini 长时间运行稳定性。

**Architecture:** 保留现有 Xiaozhi WebSocket 和 Choreographer 架构。先修正已确认的局部 bug，再为音频/会话增加有界队列与 watchdog，最后把外部运动请求收敛为命令队列；不移植官方 HF Realtime 协议或 BackgroundToolManager。

**Tech Stack:** Python 3.11、asyncio、websockets、FastAPI、Reachy Mini SDK、pytest。

---

### Task 1: 修复启动与配置持久化

**Files:**
- Modify: `yrobot/main.py`
- Modify: `yrobot/app_config.py`
- Test: `tests/test_stability_guards.py`

实施：
- 保持 Choreographer 低速启动参数直到启动抬头阶段结束，再恢复正常 gaze 参数。
- 只在 `_run_xiaozhi()` 正常返回后清除启动失败计数；异常启动保留计数。
- VAD 配置按 key 更新 env 文件，保留 HA token 和其他变量，使用同目录临时文件 + `os.replace()` 原子替换。
- 电机使能失败时进入明确的启动失败路径，不启动后续对话循环。

验证：纯函数/静态测试、`py_compile`、运动测试。

### Task 2: 加固音频与 TTS

**Files:**
- Modify: `yrobot/main.py`
- Test: `tests/test_stability_guards.py`

实施：
- 将播放队列设为有界队列，满载时丢弃旧音频或清空后保留最新帧，并统计丢帧。
- 记录最后一个 TTS 音频包时间和 TTS 总开始时间。
- 同时覆盖“零包卡死”和“中途断流”两类恢复。
- 统一清空队列、切换 IDLE 和记录诊断日志。
- 保存 aplay 进程句柄，关闭时有界 terminate/kill。

验证：队列满载、flush、TTS watchdog 状态转换测试。

### Task 3: 监督 WebSocket 会话

**Files:**
- Modify: `yrobot/main.py`
- Test: `tests/test_stability_guards.py`

实施：
- 观察 `recv` task 的 done/exception 状态。
- recv 提前结束时让当前会话退出，由外层重连循环接管。
- 关闭时 cancel 后 await task，避免未取出的异常和悬挂任务。
- 为 JSON decode、WS send/recv 异常保留明确日志。

验证：模拟 recv 异常、正常 stop、task cancellation。

### Task 4: 收敛运动线程状态

**Files:**
- Modify: `yrobot/motion.py`
- Modify: `yrobot/app_config.py`
- Modify: `yrobot/main.py`
- Test: `tests/test_motion.py`

实施：
- 为 `set_mode`、`play_move`、`play_recorded`、`play_dance`、`set_gaze_target`、stillness 请求增加 command queue。
- 外部 API 只入队，运动线程在 tick 开始统一消费。
- 保持动作请求顺序；Dashboard 返回“已排队”而不是假装动作已在当前帧完成。
- 保留现有 antenna freeze、姿态 fade、单一 `set_target`。

验证：跨线程请求顺序、模式切换、动作替换和线程停止测试。

### Task 5: 测试与可观测性

**Files:**
- Delete or rewrite: tests importing deleted MiniCPM-o modules
- Modify: `yrobot/motion.py`, `yrobot/app_config.py`, `scripts/health_monitor.py`
- Test: `tests/test_stability_guards.py`, `tests/test_motion.py`

实施：
- 清理失效测试引用，不恢复已删除运行时代码。
- Dashboard status 增加运动 loop、队列、TTS、WS、motor readiness 摘要。
- health monitor 增加音频队列/服务状态检查，所有外部 subprocess 设置 timeout。
- 更新 README/plan 中过时的 MiniCPM-o 描述。

验证：`pytest`、`ruff check .`、`python -m compileall`，再进行 Reachy 远端 stop/start 和短时实机验证。
