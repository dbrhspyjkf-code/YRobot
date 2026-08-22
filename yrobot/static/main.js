const statusPanel = document.getElementById("status-panel");
const refreshStatus = document.getElementById("refresh-status");
const statusService = document.getElementById("status-service");
const statusUptime = document.getElementById("status-uptime");
const statusSys = document.getElementById("status-sys");
const daemonState = document.getElementById("daemon-state");
const daemonMotors = document.getElementById("daemon-motors");
const daemonApp = document.getElementById("daemon-app");
const daemonTransport = document.getElementById("daemon-transport");
const daemonDoa = document.getElementById("daemon-doa");
const daemonSpeech = document.getElementById("daemon-speech");
const daemonWake = document.getElementById("daemon-wake");
const daemonSleep = document.getElementById("daemon-sleep");
const daemonRestart = document.getElementById("daemon-restart");
const trackingSource = document.getElementById("tracking-source");
const trackingTarget = document.getElementById("tracking-target");
const trackingFace = document.getElementById("tracking-face");
const chatMiniEntries = document.getElementById("chat-mini-entries");
const backendButtons = Array.from(document.querySelectorAll("[data-backend]"));
const backendStatus = document.getElementById("backend-status");
const backendDetail = document.getElementById("backend-detail");
const voiceSection = document.getElementById("voice-section");
const voiceSelect = document.getElementById("voice-select");
const voicePreview = document.getElementById("voice-preview");
const voiceDetail = document.getElementById("voice-detail");
let voiceAvailable = [];
const restartBanner = document.getElementById("restart-banner");
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
const facePanel = document.getElementById("face-panel");
const faceName = document.getElementById("face-name");
const faceRegister = document.getElementById("face-register");
const faceRefresh = document.getElementById("face-refresh");
const faceNote = document.getElementById("face-note");
const faceList = document.getElementById("face-list");

const logList = document.getElementById("log-list");
const powerReboot = document.getElementById("power-reboot");
const powerOff = document.getElementById("power-off");
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

function formatUptime(seconds) {
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分钟`;
}

function stateText(enabled, configured = true) {
  if (!enabled) return "关闭";
  return configured ? "已启用" : "未配置";
}

function renderVoiceSectionVisibility(configuredBackend) {
  // The voice picker drives DashScope realtime voices (/api/conversation/voice*),
  // which only exist under QWEN. Hide the dead UI under XIAOZHI.
  const visible = configuredBackend === "qwen";
  const wasHidden = voiceSection.classList.contains("hidden");
  voiceSection.classList.toggle("hidden", !visible);
  if (visible && wasHidden) loadVoice();
}

function renderFacePanelVisibility(configuredBackend) {
  // Face recognition only runs inside QWEN sessions (monitor_face_identity
  // in main.py). Under XIAOZHI the recognition never fires, so keep the
  // panel hidden instead of showing a dead "当前识别" area.
  const visible = configuredBackend === "qwen";
  const wasHidden = facePanel.classList.contains("hidden");
  facePanel.classList.toggle("hidden", !visible);
  if (visible && wasHidden) loadFaces();
}

function renderBackend(data) {
  const configured = data.configured_backend;
  const running = data.running_backend;
  for (const button of backendButtons) {
    button.classList.toggle("active", button.dataset.backend === configured);
  }
  backendStatus.textContent = running ? `运行中：${running.toUpperCase()}` : "未运行";
  backendStatus.classList.toggle("warning", Boolean(data.error));
  backendDetail.textContent = data.error
    ? `连接失败：${data.error}`
    : `当前配置 ${configured.toUpperCase()} · 连接 ${data.connection_state || "未知"}`;
  renderFacePanelVisibility(configured);
  renderVoiceSectionVisibility(configured);
}

async function loadBackend() {
  try {
    const response = await fetch("/api/conversation/backend", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "读取失败");
    renderBackend(data);
  } catch (error) {
    backendStatus.textContent = "读取失败";
    backendDetail.textContent = error.message;
  }
}

async function saveBackend(button) {
  for (const item of backendButtons) item.disabled = true;
  try {
    const response = await fetch("/api/conversation/backend", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backend: button.dataset.backend }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "保存失败");
    for (const item of backendButtons) {
      item.classList.toggle("active", item === button);
    }
    backendDetail.textContent = `已保存 ${data.configured_backend.toUpperCase()}，重启后生效。`;
    restartBanner.classList.remove("hidden");
    renderFacePanelVisibility(data.configured_backend);
    renderVoiceSectionVisibility(data.configured_backend);
  } catch (error) {
    backendDetail.textContent = `保存失败：${error.message}`;
  } finally {
    for (const item of backendButtons) item.disabled = false;
  }
}

for (const button of backendButtons) {
  button.addEventListener("click", () => saveBackend(button));
}

function renderVoice(data) {
  const configured = data.configured_voice;
  // Lazy-populate select options from backend-reported available voices.
  voiceAvailable = Array.isArray(data.available_voices) ? data.available_voices.slice() : [];
  if (voiceSelect.options.length === 0 && voiceAvailable.length > 0) {
    for (const voice of voiceAvailable) {
      const option = document.createElement("option");
      option.value = voice;
      option.textContent = voice;
      voiceSelect.appendChild(option);
    }
  }
  voiceSelect.value = configured;
  voicePreview.disabled = false;
  voiceDetail.textContent = `当前 ${configured} · 重启后生效`;
}

async function loadVoice() {
  if (voiceSection.classList.contains("hidden")) return;
  try {
    const response = await fetch("/api/conversation/voice", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "读取失败");
    renderVoice(data);
  } catch (error) {
    voiceDetail.textContent = `读取失败：${error.message}`;
  }
}

async function saveVoice(voice) {
  voiceSelect.disabled = true;
  voicePreview.disabled = true;
  try {
    const response = await fetch("/api/conversation/voice", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "保存失败");
    voiceDetail.textContent = `已保存 ${data.configured_voice}，重启后生效。`;
    restartBanner.classList.remove("hidden");
  } catch (error) {
    voiceDetail.textContent = `保存失败：${error.message}`;
  } finally {
    voiceSelect.disabled = false;
    voicePreview.disabled = false;
  }
}

voiceSelect.addEventListener("change", () => saveVoice(voiceSelect.value));
voicePreview.addEventListener("click", () => previewVoice(voiceSelect.value));

async function previewVoice(voice) {
  if (!voice) return;
  voicePreview.disabled = true;
  voiceSelect.disabled = true;
  const originalLabel = voicePreview.textContent;
  voicePreview.textContent = "试听中...";
  try {
    const response = await fetch(`/api/conversation/voice/preview?voice=${encodeURIComponent(voice)}`, {
      method: "POST",
      cache: "no-store",
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: "试听失败" }));
      throw new Error(err.detail || "试听失败");
    }
    const payload = await response.json();
    const pcmBytes = base64ToPCM16Bytes(payload.pcm_base64);
    const sampleRate = payload.sample_rate || 24000;
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate });
    const audioBuffer = audioCtx.createBuffer(1, pcmBytes.length / 2, sampleRate);
    const channel = audioBuffer.getChannelData(0);
    for (let i = 0; i < pcmBytes.length; i += 2) {
      const sample = pcmBytes[i] | (pcmBytes[i + 1] << 8);
      channel[i / 2] = sample < 0x8000 ? sample / 0x8000 : (sample - 0x10000) / 0x8000;
    }
    const source = audioCtx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(audioCtx.destination);
    source.start();
    voiceDetail.textContent = `试听 ${voice} · ${(audioBuffer.duration).toFixed(1)}s`;
  } catch (error) {
    voiceDetail.textContent = `试听失败：${error.message}`;
  } finally {
    voicePreview.textContent = originalLabel;
    voicePreview.disabled = false;
    voiceSelect.disabled = false;
  }
}

function base64ToPCM16Bytes(b64) {
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function showStatus(status) {
  const stateLabels = { active: "活跃", sleeping: "待机", deep_sleep: "休眠", safe_mode: "安全模式" };
  statusService.textContent = stateLabels[status.service.state] || status.service.state;
  statusUptime.textContent = `PID ${status.service.pid} / 已运行 ${formatUptime(status.service.uptime_s)}`;

  statusPanel.classList.remove("hidden");
  renderDaemon(status.daemon);
  renderTracking(status.motion && status.motion.tracking);
  if (status.system) renderSystem(status.system, status.motion);
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
    statusSys.textContent = "--";
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
  const level = logLevel.value || "info";
  if (level === "chat") {
    url.searchParams.set("min_level", "info");
    url.searchParams.set("filter", "chat");
  } else {
    url.searchParams.set("min_level", level);
  }
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

function setPowerButtonsBusy(button) {
  for (const item of [powerReboot, powerOff]) {
    if (!item) continue;
    item.disabled = true;
    item.classList.toggle("busy", item === button);
  }
}

function clearPowerButtonsBusy() {
  for (const item of [powerReboot, powerOff]) {
    if (!item) continue;
    item.disabled = false;
    item.classList.remove("busy");
  }
}

async function requestPowerAction(action, button, confirmText, successText) {
  if (!window.confirm(confirmText)) return;
  setPowerButtonsBusy(button);
  try {
    const response = await fetch("/api/system/power", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.ok === false) {
      const detail = result.message || result.detail || `HTTP ${response.status}`;
      window.alert(`命令失败：${detail}`);
      clearPowerButtonsBusy();
      return;
    }
    window.alert(result.message || successText);
  } catch (error) {
    window.alert(`请求失败：${error.message}`);
    clearPowerButtonsBusy();
  }
}

powerReboot.addEventListener("click", () => {
  requestPowerAction(
    "reboot",
    powerReboot,
    "确认重启 Reachy Mini？网络会短暂断开，启动后会自动恢复。",
    "正在重启 Reachy Mini…",
  );
});
powerOff.addEventListener("click", () => {
  requestPowerAction(
    "poweroff",
    powerOff,
    "确认关闭 Reachy Mini？关机后需要手动按电源开机。",
    "正在关闭 Reachy Mini…",
  );
});

function renderDaemon(daemon) {
  if (!daemon || !daemon.available) {
    daemonState.textContent = "不可用";
    daemonMotors.textContent = "无法读取官方 daemon";
    daemonApp.textContent = "--";
    daemonTransport.textContent = "--";
    daemonDoa.textContent = "--";
    daemonSpeech.textContent = "--";
    return;
  }
  daemonState.textContent = daemon.daemon_state || "未知";
  const motor = daemon.motor_mode || "未知";
  const awake = daemon.awake === true ? "已唤醒" : daemon.awake === false ? "睡眠" : "未知";
  daemonMotors.textContent = `电机 ${motor} · ${awake}`;
  daemonApp.textContent = daemon.active_app || daemon.app_lock_state || "空闲";
  daemonTransport.textContent = daemon.remote_session_active ? "远程会话占用" : (daemon.active_app_transport || "本地/空闲");
  if (typeof daemon.doa_angle_rad === "number") {
    daemonDoa.textContent = `${Math.round(daemon.doa_angle_rad * 180 / Math.PI)}°`;
  } else {
    daemonDoa.textContent = "--";
  }
  daemonSpeech.textContent = daemon.doa_speech_detected === true ? "检测到说话" : daemon.doa_speech_detected === false ? "未检测到说话" : "无 DoA 数据";
}

function setDaemonButtonsBusy(button, busy) {
  for (const item of [daemonWake, daemonSleep, daemonRestart]) {
    if (!item) continue;
    item.disabled = busy;
    item.classList.toggle("busy", busy && item === button);
  }
}

async function requestDaemonAction(action, button, confirmText) {
  if (!window.confirm(confirmText)) return;
  setDaemonButtonsBusy(button, true);
  try {
    const response = await fetch("/api/reachy-daemon/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.ok === false) {
      window.alert(`命令失败：${result.detail || result.message || `HTTP ${response.status}`}`);
      return;
    }
    await loadStatus();
  } catch (error) {
    window.alert(`请求失败：${error.message}`);
  } finally {
    setDaemonButtonsBusy(button, false);
  }
}

daemonWake.addEventListener("click", () => {
  requestDaemonAction("wake", daemonWake, "确认唤醒 Reachy daemon？");
});
daemonSleep.addEventListener("click", () => {
  requestDaemonAction("sleep", daemonSleep, "确认让 Reachy 进入睡眠？");
});
daemonRestart.addEventListener("click", () => {
  requestDaemonAction("restart", daemonRestart, "确认重启 Reachy daemon？");
});

loadStatus();
loadBackend();
loadVoice();
loadVolume();
loadVad();
loadCameraState();
loadLogs({ fullReplace: true });
scheduleLogPoll();
setInterval(loadStatus, 10000);
loadChatMini();
setInterval(loadChatMini, 2000);

function renderSystem(sys, motion) {
  const el = document.getElementById("status-sys");
  if (!el) return;
  let power = "";
  if (sys.power?.available) {
    if (sys.power.under_voltage || sys.power.throttled) {
      power = " · 电源异常";
    } else if (sys.power.under_voltage_seen || sys.power.throttled_seen || sys.power.frequency_capped_seen) {
      power = " · 曾低电压";
    } else {
      power = " · 电源正常";
    }
  }
  let gaze = "";
  if (motion?.gaze_source) {
    const deg = motion.gaze_target_rad === undefined ? "" : ` ${Math.round(motion.gaze_target_rad * 180 / Math.PI)}°`;
    gaze = ` · 视线 ${motion.gaze_source}${deg}`;
  }
  el.textContent = `CPU ${sys.cpu_percent}% · 内存 ${sys.memory_percent}% · 磁盘 ${sys.disk_percent}% · CPU ${sys.temperature_c}°C${power}${gaze}`;
}

function angleLabel(rad) {
  if (rad === null || rad === undefined || Number.isNaN(Number(rad))) return "--";
  return `${Math.round(Number(rad) * 180 / Math.PI)}°`;
}

function renderTracking(tracking) {
  if (!trackingSource || !trackingTarget || !trackingFace) return;
  if (!tracking) {
    trackingSource.textContent = "--";
    trackingTarget.textContent = "--";
    trackingFace.textContent = "等待追踪数据";
    return;
  }
  const sourceLabels = { audio: "声音", "audio+visual": "声音+视觉", idle: "待机" };
  trackingSource.textContent = sourceLabels[tracking.source] || tracking.source || "--";
  trackingTarget.textContent = `目标 ${angleLabel(tracking.target_yaw_rad)} · 声音 ${angleLabel(tracking.audio_yaw_rad)}`;
  const visual = tracking.visual_yaw_rad === null || tracking.visual_yaw_rad === undefined
    ? "视觉 --"
    : `视觉 ${angleLabel(tracking.visual_yaw_rad)}`;
  const face = tracking.face_detected ? "已看到脸" : "未看到脸";
  const age = tracking.age_s === null || tracking.age_s === undefined ? "" : ` · ${tracking.age_s}s`;
  trackingFace.textContent = `${face} · ${visual}${age}`;
}

let _chatMiniLastRendered = "";

async function loadChatMini() {
  try {
    const response = await fetch("/api/logs?filter=chat&limit=200", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) return;
    const lines = (result.logs || []).map(l => l.message || l.text || String(l));
    // Extract stt/tts pairs: find last 6 turns.
    // Recognize both legacy xiaozhi markers and current qwen markers.
    const isStt = (line) => /xz stt:/.test(line) || /qwen stt:/.test(line);
    const isTts = (line) => /xz tts text:/.test(line) || /qwen response:/.test(line);
    const turns = [];
    let i = lines.length - 1;
    while (i >= 0 && turns.length < 6) {
      let ttsLine = null, sttLine = null;
      // scan backward for bot reply (tts / response)
      while (i >= 0) {
        const line = lines[i--];
        if (isTts(line)) { ttsLine = line; break; }
      }
      // scan backward for matching user speech
      while (i >= 0) {
        const line = lines[i];
        if (isStt(line)) { sttLine = line; i--; break; }
        if (isTts(line)) { i--; continue; }
        i--;
      }
      if (ttsLine || sttLine) turns.unshift({ stt: sttLine, tts: ttsLine });
    }
    if (turns.length === 0) {
      if (!_chatMiniLastRendered) {
        chatMiniEntries.innerHTML = '<span class="muted">暂无对话记录</span>';
      }
      return;
    }
    const stripPrefix = (line) =>
      line.replace(/^.*?xz stt:\s*/, "")
          .replace(/^.*?xz tts text:\s*/, "")
          .replace(/^.*?qwen stt:\s*/, "")
          .replace(/^.*?qwen response:\s*/, "")
          .replace(/^\[.*?\]\s*/, "");
    const html = turns.map(t => {
      let h = "";
      if (t.stt) {
        h += `<div class="chat-entry chat-entry-user">👤 ${escapeHtml(stripPrefix(t.stt))}</div>`;
      }
      if (t.tts) {
        h += `<div class="chat-entry chat-entry-bot">🤖 ${escapeHtml(stripPrefix(t.tts))}</div>`;
      }
      return h;
    }).join("");
    if (html !== _chatMiniLastRendered) {
      _chatMiniLastRendered = html;
      chatMiniEntries.innerHTML = html;
    }
  } catch (_) { /* silently fail */ }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ── Local face registry ─────────────────────────────────────────────────────
function faceSeenLabel(lastSeen) {
  if (!lastSeen) return "尚未识别";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - lastSeen));
  return seconds < 60 ? "刚刚识别" : `${Math.floor(seconds / 60)} 分钟前识别`;
}

async function loadFaces() {
  if (facePanel.classList.contains("hidden")) return;
  try {
    const response = await fetch("/api/face", { cache: "no-store" });
    if (!response.ok) throw new Error("读取失败");
    const data = await response.json();
    const faces = data.faces || [];
    const recognition = data.recognition || {};
    if (recognition.name) {
      faceNote.textContent = `当前识别：${recognition.name}（匹配分数 ${recognition.score ?? "--"}）`;
    } else if (recognition.score !== undefined && recognition.score !== null) {
      faceNote.textContent = `当前未匹配登记人脸（最高分 ${recognition.score}）。`;
    }
    faceList.innerHTML = faces.length ? faces.map((face) => `
      <div class="face-item">
        <div><b>${escapeHtml(face.name)}</b><small>${face.sample_count} 张样本 · ${faceSeenLabel(face.last_seen)}</small></div>
        <button class="face-delete" type="button" data-name="${escapeHtml(face.name)}">删除</button>
      </div>`).join("") : '<p class="muted">尚未登记人脸</p>';
    faceList.querySelectorAll(".face-delete").forEach((button) => {
      button.addEventListener("click", () => deleteFace(button.dataset.name));
    });
  } catch (error) {
    faceList.innerHTML = `<p class="muted">无法读取人脸：${escapeHtml(error.message)}</p>`;
  }
}

async function registerFace() {
  const name = faceName.value.trim();
  if (!name) { faceNote.textContent = "请先输入姓名。"; return; }
  faceRegister.disabled = true;
  faceNote.textContent = "正在采集约 2 秒，请面向镜头缓慢转头…";
  try {
    const response = await fetch("/api/face", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "登记失败");
    faceName.value = "";
    faceNote.textContent = `已登记 ${data.name}，共有 ${data.samples} 张样本。`;
    await loadFaces();
  } catch (error) {
    faceNote.textContent = `登记失败：${error.message}`;
  } finally {
    faceRegister.disabled = false;
  }
}

async function deleteFace(name) {
  if (!name || !confirm(`删除“${name}”的人脸样本？`)) return;
  faceNote.textContent = "正在删除…";
  try {
    const response = await fetch(`/api/face/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "删除失败");
    faceNote.textContent = `已删除 ${name}。`;
    await loadFaces();
  } catch (error) {
    faceNote.textContent = `删除失败：${error.message}`;
  }
}

faceRegister.addEventListener("click", registerFace);
faceRefresh.addEventListener("click", loadFaces);
loadFaces();

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

// ── Photo album (synced to remote SFTP) ────────────────────────────────
const photoPanel = document.getElementById("photo-panel");
const photoGrid = document.getElementById("photo-grid");
const photoStatus = document.getElementById("photo-status");
const photoMeta = document.getElementById("photo-meta");
const photoNote = document.getElementById("photo-note");
const photoCapture = document.getElementById("photo-capture");
const photoRefresh = document.getElementById("photo-refresh");

const photoPreviewOverlay = document.createElement("div");
photoPreviewOverlay.className = "photo-preview hidden";
photoPreviewOverlay.setAttribute("role", "dialog");
photoPreviewOverlay.setAttribute("aria-label", "照片预览");
const previewImage = document.createElement("img");
photoPreviewOverlay.appendChild(previewImage);
document.body.appendChild(photoPreviewOverlay);
photoPreviewOverlay.addEventListener("click", () => {
  photoPreviewOverlay.classList.add("hidden");
  previewImage.removeAttribute("src");
});

function photoStatusLabel(value) {
  if (value === "uploaded") return "已同步";
  if (value === "pending") return "待同步";
  if (value === "uploading") return "同步中";
  if (value === "failed") return "同步失败";
  return value || "--";
}

function photoSourceLabel(source) {
  if (!source) return "--";
  if (source === "voice-xz") return "XIAOZHI 语音";
  if (source === "voice-qwen") return "QWEN 语音";
  if (source === "dashboard") return "Dashboard";
  return source;
}

function formatPhotoTimestamp(value) {
  if (!value) return "--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

function setPhotoStatusPill(snapshot) {
  if (!snapshot) {
    photoStatus.textContent = "不可用";
    photoStatus.classList.remove("mint", "warning");
    return;
  }
  if (!snapshot.enabled) {
    photoStatus.textContent = "同步未启用";
    photoStatus.classList.remove("mint");
    photoStatus.classList.add("warning");
    return;
  }
  if (snapshot.queue_depth > 0 || snapshot.pending_bytes > 0) {
    photoStatus.textContent = `同步中 ${snapshot.queue_depth}`;
    photoStatus.classList.remove("warning");
    photoStatus.classList.add("mint");
    return;
  }
  photoStatus.textContent = "同步已就绪";
  photoStatus.classList.remove("warning");
  photoStatus.classList.add("mint");
}

async function loadPhotoStatus({ silent = false } = {}) {
  if (photoPanel.classList.contains("hidden")) return;
  try {
    const response = await fetch("/api/photos/status", { cache: "no-store" });
    if (response.status === 503) {
      setPhotoStatusPill(null);
      if (!silent) photoMeta.textContent = "相册服务尚未就绪，请稍候。";
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const snapshot = await response.json();
    setPhotoStatusPill(snapshot);
    if (!snapshot.enabled) {
      photoMeta.textContent = "同步未启用：照片仅暂存于 Reachy。";
    } else if (snapshot.queue_depth > 0) {
      photoMeta.textContent = `同步中：${snapshot.queue_depth} 张待上传。`;
    } else if (snapshot.last_error) {
      photoMeta.textContent = `上次同步失败：${snapshot.last_error}`;
    } else if (snapshot.last_success_at) {
      photoMeta.textContent = `上次同步：${formatPhotoTimestamp(new Date(snapshot.last_success_at * 1000).toISOString())}`;
    } else {
      photoMeta.textContent = "点击“拍摄并上传”向 OrangePi 上传一张当前画面。";
    }
  } catch (error) {
    if (!silent) photoMeta.textContent = `无法读取相册状态：${error.message}`;
  }
}

function renderPhotoCard(photo) {
  const card = document.createElement("div");
  card.className = "photo-card";

  const thumb = document.createElement("div");
  thumb.className = "photo-thumb";
  if (photo.status === "uploaded") {
    const img = document.createElement("img");
    img.alt = "相册缩略图";
    img.loading = "lazy";
    img.src = `/api/photos/${encodeURIComponent(photo.id)}/image?variant=thumb`;
    img.addEventListener("load", () => {
      img.addEventListener("click", () => {
        previewImage.src = `/api/photos/${encodeURIComponent(photo.id)}/image?variant=full`;
        photoPreviewOverlay.classList.remove("hidden");
      });
    });
    img.addEventListener("error", () => {
      thumb.textContent = "缩略图加载失败";
    });
    thumb.appendChild(img);
  } else if (photo.status === "failed") {
    thumb.textContent = "同步失败";
  } else {
    thumb.textContent = "等待同步";
  }
  card.appendChild(thumb);

  const meta = document.createElement("div");
  meta.className = "photo-meta";
  const time = document.createElement("b");
  time.textContent = formatPhotoTimestamp(photo.created_at);
  meta.appendChild(time);
  const status = document.createElement("span");
  status.className = `photo-status ${photo.status || ""}`;
  status.textContent = photoStatusLabel(photo.status);
  meta.appendChild(status);
  const source = document.createElement("span");
  source.textContent = photoSourceLabel(photo.source);
  meta.appendChild(source);
  if (photo.last_error) {
    const errorLine = document.createElement("span");
    errorLine.textContent = `${photo.last_error}`;
    meta.appendChild(errorLine);
  }
  card.appendChild(meta);

  const actions = document.createElement("div");
  actions.className = "photo-actions";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "重试";
  retry.disabled = !(photo.status === "failed");
  retry.addEventListener("click", () => retryPhoto(photo.id, retry));
  actions.appendChild(retry);
  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.textContent = "删除";
  deleteBtn.className = "danger";
  deleteBtn.disabled = !(photo.status === "uploaded");
  deleteBtn.addEventListener("click", () => deletePhoto(photo.id, deleteBtn));
  actions.appendChild(deleteBtn);
  card.appendChild(actions);

  return card;
}

async function loadPhotos({ silent = false } = {}) {
  if (photoPanel.classList.contains("hidden")) return;
  try {
    const response = await fetch("/api/photos?limit=24", { cache: "no-store" });
    if (response.status === 503) {
      photoGrid.innerHTML = '<p class="muted">相册服务尚未就绪</p>';
      if (!silent) photoNote.textContent = "";
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const photos = Array.isArray(data.photos) ? data.photos : [];
    photoGrid.innerHTML = "";
    if (!photos.length) {
      photoGrid.innerHTML = '<p class="muted">尚无照片</p>';
    } else {
      const fragment = document.createDocumentFragment();
      for (const photo of photos) {
        fragment.appendChild(renderPhotoCard(photo));
      }
      photoGrid.appendChild(fragment);
    }
    photoNote.textContent = `共 ${photos.length} 条记录`;
  } catch (error) {
    if (!silent) photoNote.textContent = `读取相册失败：${error.message}`;
  }
}

async function captureDashboardPhoto() {
  photoCapture.disabled = true;
  const original = photoCapture.textContent;
  photoCapture.textContent = "拍摄中…";
  try {
    const response = await fetch("/api/photos/capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    photoNote.textContent = "已加入同步队列";
    await loadPhotos({ silent: true });
    await loadPhotoStatus({ silent: true });
  } catch (error) {
    photoNote.textContent = `拍摄失败：${error.message}`;
  } finally {
    photoCapture.textContent = original;
    photoCapture.disabled = false;
  }
}

async function retryPhoto(photoId, button) {
  if (!photoId) return;
  button.disabled = true;
  try {
    const response = await fetch(`/api/photos/${encodeURIComponent(photoId)}/retry`, {
      method: "POST",
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    photoNote.textContent = "已重新加入同步队列";
    await loadPhotos({ silent: true });
    await loadPhotoStatus({ silent: true });
  } catch (error) {
    photoNote.textContent = `重试失败：${error.message}`;
    button.disabled = false;
  }
}

async function deletePhoto(photoId, button) {
  if (!photoId) return;
  if (!window.confirm("将永久删除 OrangePi 中的这张照片，且无法恢复。继续吗？")) {
    return;
  }
  button.disabled = true;
  try {
    const response = await fetch(`/api/photos/${encodeURIComponent(photoId)}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(result.detail || `HTTP ${response.status}`);
    }
    photoNote.textContent = "已删除";
    await loadPhotos({ silent: true });
    await loadPhotoStatus({ silent: true });
  } catch (error) {
    photoNote.textContent = `删除失败：${error.message}`;
    button.disabled = false;
  }
}

photoCapture.addEventListener("click", captureDashboardPhoto);
photoRefresh.addEventListener("click", () => {
  loadPhotos();
  loadPhotoStatus();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    loadPhotos({ silent: true });
    loadPhotoStatus({ silent: true });
  }
});

if (photoPanel && !photoPanel.classList.contains("hidden")) {
  loadPhotos();
  loadPhotoStatus();
}
