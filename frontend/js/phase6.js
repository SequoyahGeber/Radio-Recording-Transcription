// Phase Six model readiness, durable queues, glossaries, and comparison controls.
const phaseSixState = {
  glossaries: new Map(),
  timer: null,
};

const phaseSixCard = document.getElementById("quality-control-card");

async function phaseSixJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : detail?.message || "The quality control request failed",
    );
  }
  return payload;
}

function phaseSixStateLabel(model) {
  const labels = {
    not_installed: "Not installed",
    downloading: "Downloading",
    paused: "Paused",
    ready: "Ready",
    error: "Needs attention",
  };
  let label = labels[model.state] || model.state;
  if (model.state === "downloading" && model.progress !== null) {
    label += ` · ${Math.round(model.progress * 100)}%`;
  }
  if (model.eta_seconds) {
    label += ` · about ${Math.max(1, Math.ceil(model.eta_seconds / 60))} min`;
  }
  return label;
}

function phaseSixModelActions(model) {
  if (!currentProfile?.permissions?.profiles || model.ready) return [];
  if (model.state === "downloading") return ["pause", "cancel"];
  if (model.state === "paused") return ["resume", "cancel"];
  if (model.state === "error") return ["retry"];
  return ["download"];
}

function phaseSixRenderModels(payload) {
  const container = document.getElementById("model-readiness-list");
  container.replaceChildren();
  for (const model of payload.models || []) {
    const row = document.createElement("div");
    row.className = "model-readiness-row";
    const title = document.createElement("strong");
    title.textContent = model.model_key === "medium" ? "Medium" : "Large V3 rescue";
    const status = document.createElement("span");
    status.textContent = model.error || phaseSixStateLabel(model);
    const actions = document.createElement("div");
    actions.className = "model-readiness-actions";
    for (const action of phaseSixModelActions(model)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-quiet";
      button.dataset.model = model.model_key;
      button.dataset.action = action;
      button.textContent = action[0].toUpperCase() + action.slice(1);
      actions.appendChild(button);
    }
    row.append(title, status, actions);
    if (["downloading", "paused"].includes(model.state)) {
      const progress = document.createElement("progress");
      progress.max = 1;
      if (model.progress !== null) progress.value = model.progress;
      row.appendChild(progress);
    }
    container.appendChild(row);
  }
  const primary = payload.queue?.primary || {};
  const rescue = payload.queue?.quality_retry || {};
  const total = (queue) =>
    Number(queue.queued || 0) + Number(queue.running || 0) + Number(queue.retry_wait || 0);
  document.getElementById("quality-queue-summary").textContent =
    `${total(primary)} primary · ${total(rescue)} rescue`;
}

async function phaseSixLoadStatus() {
  if (phaseSixCard.hidden) return;
  try {
    phaseSixRenderModels(await phaseSixJson("/api/models/status"));
  } catch (error) {
    document.getElementById("quality-queue-summary").textContent = error.message;
  }
}

async function phaseSixControlModel(model, action) {
  try {
    const url = `/api/models/${encodeURIComponent(model)}/download`;
    await phaseSixJson(url, {
      method: action === "download" ? "POST" : "PATCH",
      headers: { "Content-Type": "application/json" },
      body: action === "download" ? undefined : JSON.stringify({ action }),
    });
    await phaseSixLoadStatus();
  } catch (error) {
    showToast(error.message, "danger");
  }
}

function phaseSixRenderGlossary() {
  const channel = document.getElementById("glossary-channel").value;
  const glossary = phaseSixState.glossaries.get(channel);
  document.getElementById("glossary-terms").value =
    glossary?.terms?.join("\n") || "";
  document.getElementById("glossary-save-status").textContent = glossary
    ? `Version ${glossary.version}`
    : "No saved terms";
}

async function phaseSixLoadGlossaries() {
  if (!currentProfile?.permissions?.review) return;
  try {
    const payload = await phaseSixJson("/api/glossaries");
    phaseSixState.glossaries = new Map(
      (payload.glossaries || []).map((item) => [item.channel, item]),
    );
    const channels = new Set([
      ...(payload.channels || []),
      ...phaseSixState.glossaries.keys(),
    ]);
    const select = document.getElementById("glossary-channel");
    const previous = select.value;
    select.replaceChildren();
    for (const channel of [...channels].sort((a, b) => a.localeCompare(b))) {
      const option = document.createElement("option");
      option.value = channel;
      option.textContent = channel;
      select.appendChild(option);
    }
    if (channels.has(previous)) select.value = previous;
    phaseSixRenderGlossary();
  } catch (error) {
    document.getElementById("glossary-save-status").textContent = error.message;
  }
}

async function phaseSixSaveGlossary() {
  const channel = document.getElementById("glossary-channel").value;
  if (!channel) return;
  const existing = phaseSixState.glossaries.get(channel);
  const terms = document
    .getElementById("glossary-terms")
    .value.split(/\n|,/)
    .map((term) => term.trim())
    .filter(Boolean);
  try {
    const result = await phaseSixJson(`/api/glossaries/${encodeURIComponent(channel)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ terms, language: "en", version: existing?.version }),
    });
    phaseSixState.glossaries.set(channel, result);
    phaseSixRenderGlossary();
    showToast(`Radio terms saved for ${channel}.`, "success");
  } catch (error) {
    showToast(error.message, "danger");
  }
}

function phaseSixBoot() {
  if (!currentProfile) {
    window.setTimeout(phaseSixBoot, 100);
    return;
  }
  if (!currentProfile.permissions?.review) return;
  phaseSixCard.hidden = false;
  document.getElementById("glossary-editor").hidden =
    !currentProfile.permissions.correct;
  phaseSixLoadStatus();
  phaseSixLoadGlossaries();
  phaseSixState.timer = window.setInterval(phaseSixLoadStatus, 5000);
}

document.getElementById("model-readiness-list").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-model]");
  if (button) phaseSixControlModel(button.dataset.model, button.dataset.action);
});
document
  .getElementById("glossary-channel")
  .addEventListener("change", phaseSixRenderGlossary);
document
  .getElementById("glossary-save-button")
  .addEventListener("click", phaseSixSaveGlossary);

phaseSixBoot();
