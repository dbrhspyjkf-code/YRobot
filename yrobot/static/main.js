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

loadSettings();
