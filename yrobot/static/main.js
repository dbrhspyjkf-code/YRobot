const form = document.getElementById("settings-form");
const loading = document.getElementById("loading");
const restartBanner = document.getElementById("restart-banner");
const overrideWarning = document.getElementById("override-warning");
const gatewayUrl = document.getElementById("gateway-url");
const tlsVerify = document.getElementById("tls-verify");
const videoEnabled = document.getElementById("video-enabled");
const proactiveEnabled = document.getElementById("proactive-enabled");
const persona = document.getElementById("persona");
const personaCount = document.getElementById("persona-count");
const profile = document.getElementById("profile");
const profileDesc = document.getElementById("profile-desc");
const privacyConfirm = document.getElementById("privacy-confirm");
const saveButton = document.getElementById("save-button");
const conversationBackend = document.getElementById("conversation-backend");
const saveStatus = document.getElementById("save-status");
const configPath = document.getElementById("config-path");
const statusPanel = document.getElementById("status-panel");
const refreshStatus = document.getElementById("refresh-status");
const statusService = document.getElementById("status-service");
const statusUptime = document.getElementById("status-uptime");
const statusConversation = document.getElementById("status-conversation");
const statusProactive = document.getElementById("status-proactive");
const statusHa = document.getElementById("status-ha");
const statusHaUrl = document.getElementById("status-ha-url");
const statusHermes = document.getElementById("status-hermes");
const statusHermesUrl = document.getElementById("status-hermes-url");
const statusLocalInfo = document.getElementById("status-local-info");
const statusPrivacy = document.getElementById("status-privacy");
const statusRefreshTime = document.getElementById("status-refresh-time");
const volumeSlider = document.getElementById("volume-slider");
const volumeMute = document.getElementById("volume-mute");
const audioVolumeValue = document.getElementById("audio-volume-value");
const audioVolumeNote = document.getElementById("audio-volume-note");
const audioMicValue = document.getElementById("audio-mic-value");
const audioMicNote = document.getElementById("audio-mic-note");
const audioStatus = document.getElementById("audio-status");
const micMeterFill = document.getElementById("mic-meter-fill");
const micDisable = document.getElementById("mic-disable");
const vadSlider = document.getElementById("vad-slider");
const audioVadValue = document.getElementById("audio-vad-value");

const cameraToggle = document.getElementById("camera-toggle");
const cameraImage = document.getElementById("camera-image");
const cameraPlaceholder = document.getElementById("camera-placeholder");
const cameraStatus = document.getElementById("camera-status");
const cameraMeta = document.getElementById("camera-meta");

const logList = document.getElementById("log-list");
const powerRestart = document.getElementById("power-restart");
const logFrame = document.getElementById("log-frame");
const logEmpty = document.getElementById("log-empty");
const logMeta = document.getElementById("log-meta");
const logLevel = document.getElementById("log-level");
const logToggle = document.getElementById("log-toggle");
const logClear = document.getElementById("log-clear");
const logJump = document.getElementById("log-jump");

let lastNonZeroVolume = 50;
let volumeInFlight = 0;
let cameraPollTimer = null;
let cameraRequestSeq = 0;
let cameraPageVisible = !document.hidden;
let cameraWantedRunning = false;

let logPollTimer = null;
let logPaused = false;
let logAutoScroll = true;
let logRenderedIds = new Set();
let logInFlight = 0;
let micInputEnabled = true;

function updatePersonaCount() {
  personaCount.textContent = String(persona.value.length);
}

function syncVideoControls() {
  proactiveEnabled.disabled = !videoEnabled.checked;
  if (!videoEnabled.checked) proactiveEnabled.checked = false;
}

function showSettings(settings) {
  gatewayUrl.value = settings.gateway_url;
  tlsVerify.checked = settings.tls_verify;
  videoEnabled.checked = settings.video_enabled;
  proactiveEnabled.checked = settings.proactive_enabled;
  persona.value = settings.persona;
  conversationBackend.value = settings.conversation_backend || "minicpmo";
  configPath.textContent = `保存位置：${settings.config_path}`;

  // Profile dropdown.
  if (Array.isArray(settings.profiles) && settings.profiles.length) {
    const current = settings.profile || "default";
    profile.innerHTML = settings.profiles
      .map((name) => `<option value="${name}"${name === current ? " selected" : ""}>${name}</option>`)
      .join("");
    profile.disabled = false;
    if (settings.profile_instructions) {
      profileDesc.textContent = `说明：${settings.profile_instructions}`;
    } else {
      profileDesc.textContent = "";
    }
  }

  updatePersonaCount();
  syncVideoControls();

  if (settings.environment_overrides.length) {
    overrideWarning.textContent =
      `以下设置由 daemon 环境变量管理，重启后会覆盖页面值：${settings.environment_overrides.join(", ")}`;
    overrideWarning.classList.remove("hidden");
  } else {
    overrideWarning.classList.add("hidden");
  }
}

function formatUptime(seconds) {
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分钟`;
}

function stateText(enabled, configured = true) {
  if (!enabled) return "关闭";
  return configured ? "已启用" : "未配置";
}

function showStatus(status) {
  statusService.textContent = status.service.state === "running" ? "运行中" : status.service.state;
  statusUptime.textContent = `PID ${status.service.pid} / 已运行 ${formatUptime(status.service.uptime_s)}`;

  const mode = status.conversation.realtime_mode === "video" ? "视频模式" : "音频模式";
  statusConversation.textContent = `${mode} / ${status.conversation.tls_verify ? "TLS 验证" : "TLS 未验证"}`;
  statusProactive.textContent = status.conversation.proactive_enabled ? "主动观察开启" : "主动观察关闭";

  const ha = status.integrations.home_assistant;
  statusHa.textContent = stateText(ha.enabled, ha.configured);
  statusHaUrl.textContent = ha.url || "未设置 Home Assistant 地址";

  const hermes = status.integrations.hermes_tools;
  statusHermes.textContent = stateText(hermes.enabled);
  statusHermesUrl.textContent = hermes.url || "未设置 Hermes 地址";

  statusLocalInfo.textContent = stateText(status.integrations.local_info.enabled);
  statusPrivacy.textContent = status.privacy.video_uploaded_to_gateway ? "音频 + 摄像头" : "仅音频";
  statusRefreshTime.textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
  statusPanel.classList.remove("hidden");
  if (status.system) renderSystem(status.system);
}

async function loadStatus() {
  refreshStatus.disabled = true;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "读取失败");
    showStatus(result.status);
    showAudio(result.status.audio);
  } catch (error) {
    statusPanel.classList.remove("hidden");
  if (status.system) renderSystem(status.system);
    statusService.textContent = "读取失败";
    statusUptime.textContent = error.message;
  } finally {
    refreshStatus.disabled = false;
  }
}

function showAudio(audio) {
  if (!audio || audio.volume_percent === null || audio.volume_percent === undefined) {
    if (audio && audio.volume_error) {
      audioVolumeValue.textContent = "不可用";
      audioVolumeNote.textContent = audio.volume_error;
      volumeSlider.disabled = true;
      volumeMute.disabled = true;
    } else {
      audioVolumeNote.textContent = "需要较新版本的 YRobot 才能调节音量。";
      volumeSlider.disabled = true;
      volumeMute.disabled = true;
    }
  } else {
    applyVolumeToUI(audio.volume_percent, audio);
  }
  renderMicInput(audio ? audio.input_enabled !== false : true);
  renderMic(audio && audio.mic);
}

function renderMicInput(enabled) {
  micInputEnabled = enabled;
  micDisable.disabled = false;
  micDisable.textContent = enabled ? "禁用" : "启用";
  micDisable.classList.toggle("active", !enabled);
}

function renderMic(mic) {
  if (!mic || mic.available === false) {
    micMeterFill.style.width = "0%";
    audioMicValue.textContent = "--";
    audioMicNote.textContent = mic && mic.updated_at ? "麦克风数据不可用" : "等待媒体连接…";
    audioStatus.textContent = "未连接";
    audioStatus.classList.remove("voiced");
    audioStatus.classList.add("unavailable");
    return;
  }
  const percent = Math.max(0, Math.min(100, Number(mic.level_percent) || 0));
  const db = Number(mic.level_db) || -120;
  micMeterFill.style.width = `${percent.toFixed(1)}%`;
  audioMicValue.textContent = `${db.toFixed(1)} dB`;
  audioStatus.classList.remove("unavailable");
  audioStatus.classList.toggle("voiced", micInputEnabled && Boolean(mic.voiced));
  audioStatus.classList.toggle("disabled", !micInputEnabled);
  audioStatus.textContent = micInputEnabled ? (mic.voiced ? "检测到人声" : "运行中") : "已禁用";
  const stamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const prefix = micInputEnabled ? "实时电平" : "仅显示电平";
  audioMicNote.textContent = `${prefix} · RMS ${(mic.rms || 0).toFixed(4)} · ${stamp}`;
}

function applyVolumeToUI(percent, audio) {
  const safePercent = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
  audioVolumeValue.textContent = `${safePercent}%`;
  if (document.activeElement !== volumeSlider) {
    volumeSlider.value = String(safePercent);
  }
  volumeSlider.style.setProperty("--volume-fill", `${safePercent}%`);
  volumeSlider.disabled = false;
  volumeMute.disabled = false;
  volumeMute.textContent = safePercent === 0 ? "取消静音" : "静音";
  if (audio && Array.isArray(audio.range) && audio.range.length === 2) {
    audioVolumeNote.textContent = `ALSA ${audio.control || "PCM"}（${audio.range[0]}–${audio.range[1]}）· 运行时生效`;
  } else {
    audioVolumeNote.textContent = "运行时生效，重启后恢复默认值";
  }
  if (safePercent > 0) lastNonZeroVolume = safePercent;
}

function showCameraPlaceholder(text) {
  cameraImage.removeAttribute("src");
  cameraImage.classList.add("hidden");
  cameraPlaceholder.classList.remove("hidden");
  if (text) {
    const small = cameraPlaceholder.querySelector("small");
    const heading = cameraPlaceholder.querySelector("strong");
    if (heading) heading.textContent = text.heading;
    if (small) small.textContent = text.note;
  }
}

function hideCameraPlaceholder() {
  cameraImage.classList.remove("hidden");
  cameraPlaceholder.classList.add("hidden");
}

function renderCameraMeta(state) {
  if (!state) {
    cameraMeta.textContent = "";
    return;
  }
  if (!state.running) {
    cameraMeta.textContent = "摄像头预览已关闭，不占用 CPU。";
    return;
  }
  const intervalMs = Math.round((state.interval_s || 0.5) * 1000);
  const longEdge = state.long_edge || 640;
  const q = state.jpeg_quality || 70;
  const captured = state.captured || 0;
  const failures = state.failures || 0;
  const bytes = state.frame_bytes || 0;
  const failPart = failures ? ` / 失败 ${failures}` : "";
  cameraMeta.textContent = `每 ${intervalMs} ms 一帧 · 长边 ${longEdge}px · JPEG ${q} · 已抓拍 ${captured} 帧${failPart} · 当前帧 ${(bytes / 1024).toFixed(1)} KB`;
}

function applyCameraState(state) {
  cameraToggle.checked = Boolean(state.running);
  cameraToggle.disabled = false;
  cameraWantedRunning = Boolean(state.running);
  if (state.running) {
    cameraStatus.textContent = "运行中";
    cameraStatus.classList.add("mint");
    if (cameraImage.complete && cameraImage.naturalWidth === 0) {
      hideCameraPlaceholder();
    }
    if (cameraPageVisible && !cameraPollTimer) startCameraPolling();
  } else {
    cameraStatus.textContent = "关闭";
    cameraStatus.classList.remove("mint");
    stopCameraPolling();
    showCameraPlaceholder({
      heading: "摄像头预览已关闭",
      note: "开启后每 0.5 秒刷新一帧；不使用时建议关闭以节省 Reachy Mini 的 CPU。",
    });
  }
  renderCameraMeta(state);
}

function startCameraPolling() {
  if (cameraPollTimer || !cameraWantedRunning || !cameraPageVisible) return;
  // Immediate refresh + 500ms interval. ?t= avoids browser caching.
  cameraRequestSeq += 1;
  cameraImage.src = `/api/camera/frame?t=${Date.now()}_${cameraRequestSeq}`;
  cameraPollTimer = window.setInterval(() => {
    if (!cameraWantedRunning || !cameraPageVisible) return;
    cameraRequestSeq += 1;
    cameraImage.src = `/api/camera/frame?t=${Date.now()}_${cameraRequestSeq}`;
  }, 500);
}

function stopCameraPolling() {
  if (cameraPollTimer) {
    window.clearInterval(cameraPollTimer);
    cameraPollTimer = null;
  }
  cameraImage.removeAttribute("src");
}

async function loadCameraState() {
  try {
    const response = await fetch("/api/camera/state", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "读取失败");
    applyCameraState(result.state);
  } catch (error) {
    cameraStatus.textContent = "不可用";
    cameraToggle.disabled = true;
    showCameraPlaceholder({ heading: "摄像头暂不可用", note: error.message });
    cameraMeta.textContent = "";
  }
}

async function setCameraRunning(running) {
  cameraToggle.disabled = true;
  try {
    const response = await fetch("/api/camera/state", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ running }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "设置失败");
    applyCameraState(result.state);
  } catch (error) {
    cameraStatus.textContent = "设置失败";
    cameraMeta.textContent = error.message;
  } finally {
    cameraToggle.disabled = false;
  }
}

function logEntryId(entry) {
  return `${entry.timestamp_us}_${entry.pid}_${entry.message.length}_${entry.level}`;
}

function appendLogEntries(entries) {
  if (!entries || !entries.length) return 0;
  const fragment = document.createDocumentFragment();
  let added = 0;
  for (const entry of entries) {
    const id = logEntryId(entry);
    if (logRenderedIds.has(id)) continue;
    logRenderedIds.add(id);
    const item = document.createElement("li");
    item.className = `log-row log-${entry.level}`;
    item.dataset.id = id;
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = entry.timestamp ? entry.timestamp.slice(11) : "";
    const level = document.createElement("span");
    level.className = "log-level";
    level.textContent = (entry.level || "info").toUpperCase();
    const logger = document.createElement("span");
    logger.className = "log-logger";
    logger.textContent = entry.logger || "yrobot";
    const message = document.createElement("span");
    message.className = "log-message";
    message.textContent = entry.message;
    item.append(time, level, logger, message);
    fragment.append(item);
    added += 1;
  }
  if (added > 0) {
    logList.append(fragment);
    // Keep at most 2000 rendered rows so long sessions stay snappy.
    while (logList.childElementCount > 2000) {
      const removed = logList.firstElementChild;
      if (!removed) break;
      logRenderedIds.delete(removed.dataset.id);
      removed.remove();
    }
    if (logList.childElementCount > 0) logEmpty.classList.add("hidden");
  }
  return added;
}

function isLogAtBottom() {
  const distance = logFrame.scrollHeight - logFrame.scrollTop - logFrame.clientHeight;
  return distance <= 12;
}

function maybeLogAutoScroll() {
  if (!logAutoScroll) return;
  logFrame.scrollTop = logFrame.scrollHeight;
}

function syncLogJump() {
  if (logAutoScroll) logJump.classList.add("hidden");
  else logJump.classList.remove("hidden");
}

async function loadLogs({ fullReplace = false } = {}) {
  if (logPaused) return;
  const requestId = ++logInFlight;
  const url = new URL("/api/logs", window.location.origin);
  url.searchParams.set("lines", "300");
  url.searchParams.set("min_level", logLevel.value || "info");
  try {
    const response = await fetch(url, { cache: "no-store" });
    const result = await response.json();
    if (requestId !== logInFlight) return;
    if (!response.ok) throw new Error(result.detail || "读取失败");
    if (fullReplace) {
      logList.innerHTML = "";
      logRenderedIds.clear();
    }
    const added = appendLogEntries(result.logs || []);
    if (fullReplace || logList.childElementCount === 0) {
      logAutoScroll = true;
    }
    if (added > 0) {
      maybeLogAutoScroll();
    }
    if (logList.childElementCount === 0) {
      logEmpty.classList.remove("hidden");
    } else {
      logEmpty.classList.add("hidden");
    }
    syncLogJump();
    const stamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    logMeta.textContent = `unit=${result.unit || "yrobot.service"} · level=${result.level} · 已加载 ${logList.childElementCount} 行 · 刷新于 ${stamp}`;
  } catch (error) {
    logMeta.textContent = `日志读取失败：${error.message}`;
  }
}

function scheduleLogPoll() {
  if (logPollTimer || logPaused) return;
  logPollTimer = window.setInterval(() => loadLogs(), 2000);
}

function stopLogPoll() {
  if (logPollTimer) {
    window.clearInterval(logPollTimer);
    logPollTimer = null;
  }
}

async function loadVolume() {
  try {
    const response = await fetch("/api/volume", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "读取失败");
    applyVolumeToUI(result.volume.percent, result.volume);
  } catch (error) {
    audioVolumeValue.textContent = "不可用";
    audioVolumeNote.textContent = error.message;
    volumeSlider.disabled = true;
    volumeMute.disabled = true;
  }
}

let volumeDebounce = null;
let pendingPercent = null;

async function setVolume(percent) {
  const clamped = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
  pendingPercent = clamped;
  if (volumeDebounce) clearTimeout(volumeDebounce);
  volumeDebounce = setTimeout(commitVolume, 60);
}

async function commitVolume() {
  const target = pendingPercent;
  if (target === null) return;
  pendingPercent = null;
  const requestId = ++volumeInFlight;
  try {
    const response = await fetch("/api/volume", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ percent: target }),
    });
    const result = await response.json();
    if (requestId !== volumeInFlight) return;
    if (!response.ok) throw new Error(result.detail || "设置失败");
    applyVolumeToUI(result.volume.percent, result.volume);
  } catch (error) {
    audioVolumeNote.textContent = `音量设置失败：${error.message}`;
  }
}

async function toggleMute() {
  const current = Number(volumeSlider.value) || 0;
  if (current > 0) {
    await setVolume(0);
  } else {
    await setVolume(lastNonZeroVolume || 50);
  }
}

async function loadVad() {
  try {
    const response = await fetch("/api/audio/vad", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "读取失败");
    const v = result.vad;
    vadSlider.min = Math.round(v.min * 1000);
    vadSlider.max = Math.round(v.max * 1000);
    vadSlider.step = Math.round(v.step * 1000);
    vadSlider.value = Math.round(v.rms_min * 1000);
    vadSlider.disabled = false;
    audioVadValue.textContent = `${v.rms_min.toFixed(3)} RMS`;
  } catch (error) {
    audioVadValue.textContent = error.message;
  }
}

async function setVad(value) {
  const rms = Number(value) / 1000;
  audioVadValue.textContent = `${rms.toFixed(3)} RMS`;
  try {
    const response = await fetch("/api/audio/vad", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rms_min: rms }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "设置失败");
    audioVadValue.textContent = `${result.vad.rms_min.toFixed(3)} RMS`;
  } catch (error) {
    audioVadValue.textContent = error.message;
  }
}

async function setMicInputEnabled(enabled) {
  micDisable.disabled = true;
  try {
    const response = await fetch("/api/audio/input", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "设置失败");
    renderMicInput(result.audio_input.enabled);
    audioMicNote.textContent = result.audio_input.enabled
      ? "麦克风上传已启用"
      : "麦克风上传已禁用";
    loadStatus();
  } catch (error) {
    micDisable.disabled = false;
    audioMicNote.textContent = `麦克风设置失败：${error.message}`;
  }
}



async function loadSettings() {
  try {
    const response = await fetch("/api/settings", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "读取失败");
    showSettings(result.settings);
    loading.classList.add("hidden");
    form.classList.remove("hidden");
  } catch (error) {
    loading.textContent = `无法读取设置：${error.message}`;
  }
}

persona.addEventListener("input", updatePersonaCount);
videoEnabled.addEventListener("change", syncVideoControls);
refreshStatus.addEventListener("click", loadStatus);
volumeSlider.addEventListener("input", (event) => {
  const value = event.target.value;
  audioVolumeValue.textContent = `${value}%`;
  volumeSlider.style.setProperty("--volume-fill", `${value}%`);
  setVolume(value);
});
volumeSlider.addEventListener("change", commitVolume);
volumeMute.addEventListener("click", toggleMute);
micDisable.addEventListener("click", () => {
  setMicInputEnabled(!micInputEnabled);
});
vadSlider.addEventListener("input", (event) => {
  setVad(event.target.value);
});
cameraToggle.addEventListener("change", (event) => {
  setCameraRunning(event.target.checked);
});
cameraImage.addEventListener("load", () => {
  if (cameraImage.naturalWidth > 0) {
    hideCameraPlaceholder();
  }
});
cameraImage.addEventListener("error", () => {
  showCameraPlaceholder({ heading: "摄像头无信号", note: "无法读取帧，可能正在启动或已关闭预览。" });
});
document.addEventListener("visibilitychange", () => {
  cameraPageVisible = !document.hidden;
  if (cameraWantedRunning && cameraPageVisible) {
    startCameraPolling();
  } else if (!cameraPageVisible) {
    stopCameraPolling();
  }
});
logToggle.addEventListener("click", () => {
  logPaused = !logPaused;
  logToggle.textContent = logPaused ? "继续" : "暂停";
  logToggle.classList.toggle("active", logPaused);
  if (logPaused) {
    stopLogPoll();
    logMeta.textContent = "已暂停日志轮询。";
  } else {
    logAutoScroll = true;
    syncLogJump();
    loadLogs({ fullReplace: true });
    scheduleLogPoll();
  }
});
logClear.addEventListener("click", () => {
  logList.innerHTML = "";
  logRenderedIds.clear();
  logEmpty.classList.remove("hidden");
  logAutoScroll = true;
  syncLogJump();
  loadLogs({ fullReplace: true });
});
logLevel.addEventListener("change", () => {
  logAutoScroll = true;
  syncLogJump();
  loadLogs({ fullReplace: true });
});
logFrame.addEventListener("scroll", () => {
  const atBottom = isLogAtBottom();
  if (atBottom && !logAutoScroll) {
    logAutoScroll = true;
    syncLogJump();
  } else if (!atBottom && logAutoScroll) {
    logAutoScroll = false;
    syncLogJump();
  }
});
logJump.addEventListener("click", () => {
  logAutoScroll = true;
  syncLogJump();
  maybeLogAutoScroll();
});
form.addEventListener("input", () => {
  saveStatus.textContent = "有未保存的修改";
  restartBanner.classList.add("hidden");
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!privacyConfirm.checked) {
    saveStatus.textContent = "请先确认数据传输说明";
    privacyConfirm.focus();
    return;
  }

  const document = {
    gateway_url: gatewayUrl.value.trim(),
    tls_verify: tlsVerify.checked,
    video_enabled: videoEnabled.checked,
    proactive_enabled: proactiveEnabled.checked,
    persona: persona.value.trim(),
    profile: profile.value || "default",
    conversation_backend: conversationBackend.value || "minicpmo",
  };

  saveButton.disabled = true;
  saveStatus.textContent = "正在验证并保存…";
  try {
    const response = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(document),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "保存失败");
    showSettings(result.settings);
    saveStatus.textContent = "已保存，等待重启应用";
    restartBanner.classList.remove("hidden");
    window.scrollTo({ top: restartBanner.offsetTop - 20, behavior: "smooth" });
  } catch (error) {
    saveStatus.textContent = `保存失败：${error.message}`;
  } finally {
    saveButton.disabled = false;
  }
});

async function restartSystem() {
  if (!window.confirm("重启 YRobot？服务会短暂离线几秒后自动恢复。")) return;
  powerRestart.classList.add("busy");
  powerRestart.disabled = true;
  try {
    const response = await fetch("/api/system/restart", { method: "POST" });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.ok === false) {
      const detail = result.message || result.detail || `HTTP ${response.status}`;
      window.alert(`重启失败：${detail}`);
      powerRestart.classList.remove("busy");
      powerRestart.disabled = false;
      return;
    }
    window.alert("正在重启…几秒后会自动重新连接。");
  } catch (error) {
    powerRestart.classList.remove("busy");
    powerRestart.disabled = false;
    window.alert(`请求失败：${error.message}`);
  }
}

powerRestart.addEventListener("click", restartSystem);

loadStatus();
loadSettings();
loadVolume();
loadVad();
loadCameraState();
loadLogs({ fullReplace: true });
scheduleLogPoll();
setInterval(loadStatus, 10000);

function renderSystem(sys) {
  const cpu = document.getElementById("status-sys-cpu");
  const mem = document.getElementById("status-sys-mem");
  const disk = document.getElementById("status-sys-disk");
  const temp = document.getElementById("status-sys-temp");
  if (!cpu) return;
  cpu.textContent = `CPU ${sys.cpu_percent}%`;
  mem.textContent = `内存 ${sys.memory_percent}%`;
  disk.textContent = `磁盘 ${sys.disk_percent}%`;
  temp.textContent = `CPU ${sys.temperature_c}°C`;
}

// ── Motion panel ────────────────────────────────────────────────────────────
const MOTION_BASIC = new Set(["shake", "nod", "tilt", "surprise", "think", "yawn", "sad", "angry"]);
const MOTION_DANCE = new Set(["simple_nod", "head_tilt_roll", "side_to_side_sway", "dizzy_spin",
  "stumble_and_recover", "headbanger_combo", "interwoven_spirals", "sharp_side_tilt",
  "side_peekaboo", "yeah_nod", "uh_huh_tilt", "neck_recoil", "chin_lead",
  "groovy_sway_and_roll", "chicken_peck", "side_glance_flick", "polyrhythm_combo",
  "grid_snap", "pendulum_swing", "jackson_square"]);
let motionAll = [];

// 动作英文名 → 中文显示名（按钮显示中文，data-move 仍用英文调用）
const MOTION_CN = {
  // 基础动作
  shake: "摇头", nod: "点头", tilt: "歪头", surprise: "惊喜",
  think: "思考", yawn: "打哈欠", sad: "伤心", angry: "生气",
  // 常用情绪（官方录制）
  cheerful1: "愉快", laughing1: "大笑", laughing2: "轻笑",
  surprised1: "惊讶", surprised2: "惊讶2", amazed1: "惊叹",
  thoughtful1: "思考中", thoughtful2: "思考2", confused1: "困惑",
  sad1: "悲伤", sad2: "悲伤2", downcast1: "沮丧", crying: "哭泣",
  rage1: "愤怒", furious1: "暴怒", reprimand1: "训斥", irritated1: "烦躁",
  loving1: "喜爱", shy1: "害羞", embarrassed: "尴尬",
  dance1: "跳舞(短)", dance2: "跳舞(长)", dance3: "跳舞(活力)",
  happy: "开心", yes1: "好的", no1: "不要", come1: "过来",
  welcome1: "欢迎", welcoming1: "欢迎1", go_away1: "走开",
  // 舞蹈
  simple_nod: "点头舞", head_tilt_roll: "歪头滚", side_to_side_sway: "左右摆",
  dizzy_spin: "转圈舞", stumble_and_recover: "踉跄恢复", headbanger_combo: "甩头舞",
  interwoven_spirals: "螺旋舞", sharp_side_tilt: "侧倾舞", side_peekaboo: "躲猫猫",
  yeah_nod: "耶点头", uh_huh_tilt: "嗯哼歪头", neck_recoil: "缩脖舞",
  chin_lead: "下巴领舞", groovy_sway_and_roll: "律动摇摆", chicken_peck: "啄木鸟",
  side_glance_flick: "侧瞥舞", polyrhythm_combo: "复节奏舞", grid_snap: "机械定格",
  pendulum_swing: "钟摆舞", jackson_square: "杰克逊方步",
};

function motionLabel(m) {
  return MOTION_CN[m] || m;
}


async function loadMotions() {
  const grid = document.getElementById("motion-grid");
  const status = document.getElementById("motion-status");
  try {
    const response = await fetch("/api/motion", { cache: "no-store" });
    const data = await response.json();
    motionAll = data.moves || [];
    status.textContent = `${motionAll.length} 个动作`;
    renderMotionTab(currentMotionTab());
  } catch (error) {
    status.textContent = "加载失败";
    grid.innerHTML = `<p class="muted">无法加载动作：${error.message}</p>`;
  }
}

function currentMotionTab() {
  const active = document.querySelector(".motion-tab.active");
  return active ? active.dataset.cat : "basic";
}

function renderMotionTab(cat) {
  const grid = document.getElementById("motion-grid");
  const moves = motionAll.filter((m) => {
    if (cat === "basic") return MOTION_BASIC.has(m);
    if (cat === "dance") return MOTION_DANCE.has(m);
    return !MOTION_BASIC.has(m) && !MOTION_DANCE.has(m);
  });
  if (moves.length === 0) {
    grid.innerHTML = `<p class="muted">此分类暂无动作</p>`;
    return;
  }
  grid.innerHTML = moves.map((m) => `<button class="motion-btn" data-move="${m}" title="${m}">${motionLabel(m)}</button>`).join("");
  grid.querySelectorAll(".motion-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await fetch("/api/motion", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ move: btn.dataset.move }),
        });
      } catch (_) { /* ignore */ }
      setTimeout(() => { btn.disabled = false; }, 600);
    });
  });
}

document.querySelectorAll(".motion-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".motion-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    renderMotionTab(tab.dataset.cat);
  });
});
document.getElementById("motion-refresh").addEventListener("click", loadMotions);
loadMotions();
