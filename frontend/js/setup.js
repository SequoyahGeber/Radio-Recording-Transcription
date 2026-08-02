const setupForm = document.getElementById("setup-form");
const setupError = document.getElementById("setup-error");
const setupButton = setupForm.querySelector("button[type='submit']");
const setupState = { step: 1, status: null, administratorCreated: false, backupReady: false };
let modelPoll = null;

async function setupJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Setup could not continue.");
  return payload;
}

function showStep(step) {
  setupState.step = step;
  document.querySelectorAll(".setup-step").forEach((section) => {
    section.hidden = Number(section.dataset.step) !== step;
  });
  const names = ["", "Welcome", "Source and storage", "Medium model", "Administrator", "Recovery"];
  document.getElementById("setup-step-label").textContent = `Step ${step} of 5 · ${names[step]}`;
  document.getElementById("setup-progress-bar").style.width = `${step * 20}%`;
  if (step === 3) refreshModel();
  if (step === 5) renderFinalChecks();
}

function renderModel(model) {
  const status = document.getElementById("setup-model-status");
  const progress = document.getElementById("setup-model-progress");
  const download = document.getElementById("setup-model-download");
  const next = document.getElementById("setup-model-next");
  const transcriptionEnabled = document.getElementById("setup-transcription-enabled").checked;
  if (model.ready) {
    status.textContent = "Medium is installed, verified, and ready.";
    progress.hidden = true;
    download.hidden = true;
    next.disabled = false;
  } else if (model.state === "downloading" || model.state === "paused") {
    status.textContent = model.state === "paused" ? "Download paused." : "Downloading Medium in the background…";
    progress.hidden = false;
    if (model.progress !== null) progress.value = model.progress;
    download.hidden = true;
    next.disabled = transcriptionEnabled;
  } else {
    status.textContent = model.error || "Medium is not installed yet.";
    progress.hidden = true;
    download.hidden = false;
    download.textContent = model.state === "error" ? "Retry Medium download" : "Download Medium model";
    next.disabled = transcriptionEnabled;
  }
}

async function refreshModel() {
  try {
    setupState.status = await setupJson("/api/setup/status");
    const medium = setupState.status.models.find((model) => model.model_key === "medium");
    renderModel(medium);
    if (["downloading", "paused"].includes(medium.state) && !modelPoll) {
      modelPoll = window.setInterval(refreshModel, 1000);
    } else if (!["downloading", "paused"].includes(medium.state) && modelPoll) {
      window.clearInterval(modelPoll);
      modelPoll = null;
    }
  } catch (error) {
    document.getElementById("setup-model-status").textContent = error.message;
  }
}

function renderFinalChecks() {
  const checks = [
    ["Recording source validated", Boolean(setupState.status?.source?.readable)],
    ["Medium model ready or transcription deferred", true],
    ["Administrator secured", setupState.administratorCreated],
    ["Recovery backup verified", setupState.backupReady],
  ];
  document.getElementById("setup-final-checks").innerHTML = checks
    .map(([label, ready]) => `<div>${ready ? "✓" : "○"} ${label}</div>`)
    .join("");
}

document.getElementById("setup-welcome-next").addEventListener("click", async () => {
  try {
    setupState.status = await setupJson("/api/setup/status");
    document.getElementById("setup-source-dir").value = setupState.status.source.path || "";
    showStep(2);
  } catch (error) {
    alert(error.message);
  }
});

document.getElementById("setup-source-next").addEventListener("click", async () => {
  const result = document.getElementById("setup-source-result");
  result.textContent = "Validating source and storage…";
  try {
    setupState.status = await setupJson("/api/setup/configure", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_dir: document.getElementById("setup-source-dir").value,
        transcription_enabled: document.getElementById("setup-transcription-enabled").checked,
        minimum_free_gb: Number(document.getElementById("setup-minimum-free").value),
        setup_step: 3,
      }),
    });
    result.textContent = `${setupState.status.source.sample_files.length} sample recording(s) found · source is readable.`;
    showStep(3);
  } catch (error) {
    result.textContent = error.message;
  }
});

document.getElementById("setup-model-download").addEventListener("click", async () => {
  try {
    await setupJson("/api/setup/models/medium", { method: "POST" });
    refreshModel();
  } catch (error) {
    document.getElementById("setup-model-status").textContent = error.message;
  }
});
document.getElementById("setup-model-next").addEventListener("click", () => showStep(4));

setupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setupError.hidden = true;
  const password = document.getElementById("setup-password").value;
  const confirmation = document.getElementById("setup-password-confirm").value;
  if (password !== confirmation) {
    setupError.textContent = "Passwords do not match.";
    setupError.hidden = false;
    document.getElementById("setup-password-confirm").select();
    return;
  }
  setupButton.disabled = true;
  setupButton.textContent = "Securing administrator…";
  try {
    await setupJson("/api/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        display_name: document.getElementById("setup-display-name").value,
        username: document.getElementById("setup-username").value,
        password,
      }),
    });
    setupState.administratorCreated = true;
    showStep(5);
  } catch (error) {
    setupError.textContent = error.message;
    setupError.hidden = false;
    setupButton.disabled = false;
    setupButton.textContent = "Create administrator and continue";
  }
});

document.getElementById("setup-backup-button").addEventListener("click", async (event) => {
  event.target.disabled = true;
  event.target.textContent = "Creating and verifying backup…";
  try {
    await setupJson("/api/backups", { method: "POST" });
    setupState.backupReady = true;
    event.target.textContent = "Backup verified";
    renderFinalChecks();
  } catch (error) {
    event.target.disabled = false;
    event.target.textContent = error.message;
  }
});

document.getElementById("setup-notification-button").addEventListener("click", async () => {
  if (!("Notification" in window)) return;
  const permission = await Notification.requestPermission();
  if (permission === "granted") new Notification("Radio Command Center is ready", { body: "Operational notifications are enabled." });
});
document.getElementById("setup-finish-button").addEventListener("click", () => window.location.replace("/"));

showStep(1);
