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
const privacyConfirm = document.getElementById("privacy-confirm");
const saveButton = document.getElementById("save-button");
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
const statusMemory = document.getElementById("status-memory");
const statusMemoryPath = document.getElementById("status-memory-path");
const statusPrivacy = document.getElementById("status-privacy");
const statusRefreshTime = document.getElementById("status-refresh-time");
const memoryPanel = document.getElementById("memory-panel");
const refreshMemory = document.getElementById("refresh-memory");
const memoryList = document.getElementById("memory-list");
const memoryText = document.getElementById("memory-text");
const addMemory = document.getElementById("add-memory");
const memoryStatus = document.getElementById("memory-status");

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
  configPath.textContent = `保存位置：${settings.config_path}`;
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
  statusMemory.textContent = stateText(status.integrations.memory.enabled);
  statusMemoryPath.textContent = status.integrations.memory.path;
  statusPrivacy.textContent = status.privacy.video_uploaded_to_gateway ? "音频 + 摄像头" : "仅音频";
  statusRefreshTime.textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
  statusPanel.classList.remove("hidden");
}

function showMemory(memory) {
  memoryList.innerHTML = "";
  if (!memory.items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-memory";
    empty.textContent = "还没有保存任何记忆";
    memoryList.append(empty);
  }
  for (const item of memory.items) {
    const row = document.createElement("div");
    row.className = "memory-row";
    const text = document.createElement("span");
    text.textContent = item;
    const button = document.createElement("button");
    button.className = "icon-button";
    button.type = "button";
    button.textContent = "删除";
    button.addEventListener("click", () => deleteMemory(item));
    row.append(text, button);
    memoryList.append(row);
  }
  memoryStatus.textContent = `共 ${memory.count} 条，保存位置：${memory.path}`;
  memoryPanel.classList.remove("hidden");
}

async function loadStatus() {
  refreshStatus.disabled = true;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "读取失败");
    showStatus(result.status);
  } catch (error) {
    statusPanel.classList.remove("hidden");
    statusService.textContent = "读取失败";
    statusUptime.textContent = error.message;
  } finally {
    refreshStatus.disabled = false;
  }
}

async function loadMemory() {
  refreshMemory.disabled = true;
  try {
    const response = await fetch("/api/memory", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "读取失败");
    showMemory(result.memory);
  } catch (error) {
    memoryPanel.classList.remove("hidden");
    memoryStatus.textContent = `无法读取记忆：${error.message}`;
  } finally {
    refreshMemory.disabled = false;
  }
}

async function saveMemory() {
  const text = memoryText.value.trim();
  if (!text) {
    memoryText.focus();
    return;
  }
  addMemory.disabled = true;
  try {
    const response = await fetch("/api/memory", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "保存失败");
    memoryText.value = "";
    showMemory(result.memory);
  } catch (error) {
    memoryStatus.textContent = `保存失败：${error.message}`;
  } finally {
    addMemory.disabled = false;
  }
}

async function deleteMemory(text) {
  try {
    const response = await fetch("/api/memory", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "删除失败");
    showMemory(result.memory);
  } catch (error) {
    memoryStatus.textContent = `删除失败：${error.message}`;
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
refreshMemory.addEventListener("click", loadMemory);
addMemory.addEventListener("click", saveMemory);
memoryText.addEventListener("keydown", (event) => {
  if (event.key === "Enter") saveMemory();
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

loadStatus();
loadMemory();
loadSettings();
setInterval(loadStatus, 10000);
