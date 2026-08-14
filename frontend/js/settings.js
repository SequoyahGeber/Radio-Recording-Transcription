// Administrator settings. The server describes every field it accepts, so this
// screen renders whatever the current build supports without hard-coding it.
const settingsState = {
  document: null,
  pending: new Map(),
  activeGroup: null,
  loading: false,
  restartScope: "none",
};

const settingsModal = document.getElementById("settings-modal");
const settingsButton = document.getElementById("settings-button");
const settingsTabs = document.getElementById("settings-tabs");
const settingsPanels = document.getElementById("settings-panels");
const settingsStatus = document.getElementById("settings-status");
const settingsSaveButton = document.getElementById("settings-save");
const settingsRevertButton = document.getElementById("settings-revert");
const settingsRestartBanner = document.getElementById("settings-restart-banner");
const settingsRestartMessage = document.getElementById("settings-restart-message");
const settingsRestartButton = document.getElementById("settings-restart-button");
const settingsSubtitle = document.getElementById("settings-subtitle");

const settingsEscape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character],
  );

function settingsNotify(message, tone = "default") {
  settingsStatus.textContent = message;
  settingsStatus.dataset.tone = tone;
  if (typeof showToast === "function" && tone === "danger") {
    showToast(message, "danger");
  }
}

async function settingsRequest(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (response.status === 401) {
    window.location.replace("/login");
    throw new Error("Sign-in required");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "The settings service is unavailable");
  }
  return payload;
}

function settingsFormatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) return "0 GB";
  const gigabytes = value / 1024 ** 3;
  return gigabytes >= 1
    ? `${gigabytes.toFixed(gigabytes >= 10 ? 0 : 1)} GB`
    : `${Math.round(value / 1024 ** 2)} MB`;
}

function settingsDisplayValue(field, value) {
  return field.scale ? Number(value) / field.scale : value;
}

function settingsStoredValue(field, displayed) {
  return field.scale
    ? Math.round(Number(displayed) * field.scale)
    : displayed;
}

function settingsFieldControl(field) {
  const identifier = `setting-${field.key}`;
  const disabled = field.managed_by_environment ? " disabled" : "";
  if (field.type === "boolean") {
    return `<label class="settings-switch">
      <input type="checkbox" id="${identifier}" data-setting-key="${field.key}"${
        field.value ? " checked" : ""
      }${disabled} />
      <span>${settingsEscape(field.label)}</span>
    </label>`;
  }
  if (field.type === "choice") {
    const options = (field.options || [])
      .map(
        (option) =>
          `<option value="${settingsEscape(option.value)}"${
            option.value === field.value ? " selected" : ""
          }>${settingsEscape(option.label)}</option>`,
      )
      .join("");
    return `<label class="settings-field-label" for="${identifier}">${settingsEscape(
      field.label,
    )}</label>
      <select id="${identifier}" data-setting-key="${field.key}"${disabled}>${options}</select>`;
  }
  if (field.type === "integer") {
    const displayed = settingsDisplayValue(field, field.value);
    const minimum = field.minimum === undefined ? "" : ` min="${settingsDisplayValue(field, field.minimum)}"`;
    const maximum = field.maximum === undefined ? "" : ` max="${settingsDisplayValue(field, field.maximum)}"`;
    const step = field.scale ? ' step="0.5"' : ' step="1"';
    return `<label class="settings-field-label" for="${identifier}">${settingsEscape(
      field.label,
    )}</label>
      <span class="settings-number">
        <input type="number" id="${identifier}" data-setting-key="${field.key}" value="${settingsEscape(
          displayed,
        )}"${minimum}${maximum}${step}${disabled} />
        ${field.unit ? `<em>${settingsEscape(field.unit)}</em>` : ""}
      </span>`;
  }
  const extra =
    field.key === "source_dir"
      ? `<div class="settings-inline-actions">
          <button class="btn btn-quiet" type="button" data-settings-action="browse">Choose folder…</button>
          <button class="btn btn-quiet" type="button" data-settings-action="check-source">Check folder</button>
          <span class="settings-inline-result" data-settings-result="source"></span>
        </div>`
      : "";
  return `<label class="settings-field-label" for="${identifier}">${settingsEscape(
    field.label,
  )}</label>
    <input type="text" id="${identifier}" data-setting-key="${field.key}" value="${settingsEscape(
      field.value,
    )}"${disabled} spellcheck="false" />${extra}`;
}

function settingsFieldMarkup(field) {
  const notes = [];
  if (field.help) notes.push(settingsEscape(field.help));
  if (field.managed_by_environment) {
    notes.push(
      `Currently set by the <code>${settingsEscape(
        field.environment,
      )}</code> environment variable, so it cannot be changed here.`,
    );
  } else if (field.restart === "services") {
    notes.push("Takes effect after a service restart.");
  } else if (field.restart === "worker") {
    notes.push("Takes effect after the transcription worker restarts.");
  }
  return `<div class="settings-field settings-field-${field.type}" data-setting-field="${field.key}">
    ${settingsFieldControl(field)}
    ${notes.length ? `<p class="settings-note">${notes.join(" ")}</p>` : ""}
  </div>`;
}

function settingsGroupMarkup(group) {
  return `<section class="settings-panel" data-settings-panel="${group.id}" hidden>
    <h3>${settingsEscape(group.title)}</h3>
    <p class="settings-panel-summary">${settingsEscape(group.summary || "")}</p>
    <div class="settings-grid">
      ${group.fields.map(settingsFieldMarkup).join("")}
    </div>
  </section>`;
}

function settingsAboutMarkup(document_) {
  const paths = document_.paths || {};
  const service = document_.service || {};
  const rows = [
    ["Dashboard address", service.dashboard_url || "—"],
    ["Network address", service.network_dashboard_url || "Not shared on the network"],
    ["Recording source", service.source_directory || "—"],
    ["Recordings stored in", paths.audio_dir || "—"],
    ["Archive database", paths.database || "—"],
    ["Service logs", paths.logs || "—"],
    ["Transcription models", paths.models || "—"],
    ["Settings file", paths.settings_file || "—"],
    [
      "Disk free",
      `${settingsFormatBytes(document_.disk?.free_bytes)} of ${settingsFormatBytes(
        document_.disk?.total_bytes,
      )}`,
    ],
  ];
  const components = Object.entries(service.components || {})
    .map(
      ([name, details]) =>
        `<li><strong>${settingsEscape(name)}</strong> · ${settingsEscape(
          details.status || "unknown",
        )}</li>`,
    )
    .join("");
  return `<section class="settings-panel" data-settings-panel="about" hidden>
    <h3>System</h3>
    <p class="settings-panel-summary">
      Where this installation keeps its data, and how each service is behaving.
    </p>
    <dl class="settings-facts">
      ${rows
        .map(
          ([label, value]) =>
            `<div><dt>${settingsEscape(label)}</dt><dd>${settingsEscape(value)}</dd></div>`,
        )
        .join("")}
    </dl>
    ${components ? `<h4>Services</h4><ul class="settings-service-list">${components}</ul>` : ""}
    <div class="settings-inline-actions">
      <button class="btn btn-quiet" type="button" data-settings-action="copy-address">
        Copy dashboard address
      </button>
      <button class="btn btn-quiet" type="button" data-settings-action="restart">
        Restart services
      </button>
    </div>
    <p class="settings-note">
      Sign-in profiles are managed under <strong>Access Profiles</strong> in the control
      panel. The Mac app on this computer signs in automatically; browsers on other
      devices always sign in with a username and password.
    </p>
  </section>`;
}

function settingsRender(document_) {
  settingsState.document = document_;
  settingsState.pending.clear();
  const groups = document_.groups || [];
  settingsTabs.innerHTML = [
    ...groups.map(
      (group) =>
        `<button type="button" class="settings-tab" data-settings-tab="${group.id}">${settingsEscape(
          group.title,
        )}</button>`,
    ),
    '<button type="button" class="settings-tab" data-settings-tab="about">System</button>',
  ].join("");
  settingsPanels.innerHTML = [
    ...groups.map(settingsGroupMarkup),
    settingsAboutMarkup(document_),
  ].join("");
  settingsSelectGroup(
    settingsState.activeGroup && settingsPanels.querySelector(
      `[data-settings-panel="${settingsState.activeGroup}"]`,
    )
      ? settingsState.activeGroup
      : groups[0]?.id || "about",
  );
  settingsSubtitle.textContent = `${groups.reduce(
    (total, group) => total + group.fields.length,
    0,
  )} settings · changes are saved to this Mac`;
  settingsUpdateDirtyState();
  settingsShowRestart(document_.restart_required || "none");
}

function settingsSelectGroup(groupId) {
  settingsState.activeGroup = groupId;
  settingsTabs.querySelectorAll("[data-settings-tab]").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.settingsTab === groupId);
  });
  settingsPanels.querySelectorAll("[data-settings-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.settingsPanel !== groupId;
  });
}

function settingsFieldByKey(key) {
  for (const group of settingsState.document?.groups || []) {
    const field = group.fields.find((item) => item.key === key);
    if (field) return field;
  }
  return null;
}

function settingsUpdateDirtyState() {
  const dirty = settingsState.pending.size;
  settingsSaveButton.disabled = dirty === 0 || settingsState.loading;
  settingsRevertButton.disabled = dirty === 0 || settingsState.loading;
  settingsSaveButton.textContent = dirty
    ? `Save ${dirty} change${dirty === 1 ? "" : "s"}`
    : "Save changes";
}

function settingsShowRestart(scope) {
  settingsState.restartScope = scope;
  const needed = scope && scope !== "none";
  settingsRestartBanner.hidden = !needed;
  if (needed) {
    settingsRestartMessage.textContent =
      scope === "worker"
        ? "Transcription settings changed. Restart the services to apply them."
        : "Network or engine settings changed. Restart the services to apply them.";
  }
}

function settingsHandleInput(event) {
  const control = event.target.closest("[data-setting-key]");
  if (!control) return;
  const key = control.dataset.settingKey;
  const field = settingsFieldByKey(key);
  if (!field) return;
  let value;
  if (field.type === "boolean") {
    value = control.checked;
  } else if (field.type === "integer") {
    if (control.value === "") return;
    value = settingsStoredValue(field, control.value);
  } else {
    value = control.value;
  }
  const original =
    field.type === "boolean" ? Boolean(field.value) : field.value;
  const unchanged =
    field.type === "integer"
      ? Number(value) === Number(original)
      : value === original;
  if (unchanged) {
    settingsState.pending.delete(key);
  } else {
    settingsState.pending.set(key, value);
  }
  settingsUpdateDirtyState();
}

async function settingsLoad() {
  settingsState.loading = true;
  settingsNotify("Loading settings…");
  try {
    const document_ = await settingsRequest("/api/settings");
    settingsRender(document_);
    settingsNotify("");
  } catch (error) {
    settingsNotify(error.message, "danger");
  } finally {
    settingsState.loading = false;
    settingsUpdateDirtyState();
  }
}

async function settingsSave() {
  if (!settingsState.pending.size) return;
  settingsState.loading = true;
  settingsUpdateDirtyState();
  settingsNotify("Saving…");
  try {
    const values = Object.fromEntries(settingsState.pending);
    const document_ = await settingsRequest("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const restart = document_.restart_required || "none";
    settingsRender(document_);
    const warnings = document_.warnings || [];
    settingsNotify(
      warnings.length ? warnings.join(" ") : "Settings saved.",
      warnings.length ? "warning" : "success",
    );
    if (typeof showToast === "function") showToast("Settings saved", "success");
    settingsShowRestart(restart);
  } catch (error) {
    settingsNotify(error.message, "danger");
  } finally {
    settingsState.loading = false;
    settingsUpdateDirtyState();
  }
}

async function settingsCheckSource(pathValue, resultElement) {
  resultElement.textContent = "Checking…";
  try {
    const result = await settingsRequest("/api/settings/source-check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: pathValue }),
    });
    if (!result.readable) {
      resultElement.textContent =
        result.error || "That folder cannot be read from this Mac.";
      resultElement.dataset.tone = "danger";
      return;
    }
    resultElement.dataset.tone = "success";
    resultElement.textContent = result.sample_files.length
      ? `Readable · found ${result.sample_files.length} recording${
          result.sample_files.length === 1 ? "" : "s"
        } (${result.sample_files[0]})`
      : "Readable · no recordings in it yet";
  } catch (error) {
    resultElement.dataset.tone = "danger";
    resultElement.textContent = error.message;
  }
}

async function settingsRestartServices() {
  settingsRestartButton.disabled = true;
  settingsNotify("Restarting services…");
  try {
    await settingsRequest("/api/services/restart", { method: "POST" });
    settingsShowRestart("none");
    settingsNotify(
      "Services are restarting. The dashboard reconnects on its own.",
      "success",
    );
    setTimeout(() => {
      settingsRestartButton.disabled = false;
    }, 8000);
  } catch (error) {
    settingsRestartButton.disabled = false;
    settingsNotify(error.message, "danger");
  }
}

function settingsOpen() {
  settingsModal.hidden = false;
  document.body.classList.add("settings-open");
  settingsLoad();
  setTimeout(() => document.getElementById("settings-close")?.focus(), 0);
}

function settingsClose() {
  settingsModal.hidden = true;
  document.body.classList.remove("settings-open");
  settingsState.pending.clear();
  settingsUpdateDirtyState();
  settingsButton?.focus();
}

settingsButton?.addEventListener("click", settingsOpen);
document.getElementById("settings-close").addEventListener("click", settingsClose);
settingsSaveButton.addEventListener("click", settingsSave);
settingsRevertButton.addEventListener("click", () => {
  if (settingsState.document) settingsRender(settingsState.document);
  settingsNotify("Changes discarded.");
});
settingsRestartButton.addEventListener("click", settingsRestartServices);
settingsPanels.addEventListener("input", settingsHandleInput);
settingsPanels.addEventListener("change", settingsHandleInput);
settingsTabs.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-settings-tab]");
  if (tab) settingsSelectGroup(tab.dataset.settingsTab);
});
settingsPanels.addEventListener("click", (event) => {
  const action = event.target.closest("[data-settings-action]")?.dataset
    .settingsAction;
  if (!action) return;
  if (action === "check-source" || action === "browse") {
    const input = document.getElementById("setting-source_dir");
    const result = settingsPanels.querySelector('[data-settings-result="source"]');
    if (action === "browse") {
      // The native app answers this by opening a real macOS folder chooser.
      const bridge = window.webkit?.messageHandlers?.nativeAction;
      if (!bridge) {
        result.dataset.tone = "";
        result.textContent =
          "Folder browsing is available in the Mac app. Type or paste a path here.";
        input?.focus();
        return;
      }
      bridge.postMessage({ action: "chooseSourceFolder" });
      result.dataset.tone = "";
      result.textContent = "Choose a folder in the window that opened…";
      return;
    }
    settingsCheckSource(input?.value || "", result);
    return;
  }
  if (action === "copy-address") {
    const address = settingsState.document?.service?.dashboard_url || "";
    navigator.clipboard?.writeText(address).then(
      () => settingsNotify(`Copied ${address}`, "success"),
      () => settingsNotify("The address could not be copied.", "danger"),
    );
    return;
  }
  if (action === "restart") settingsRestartServices();
});
settingsModal.addEventListener("click", (event) => {
  if (event.target === settingsModal) settingsClose();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !settingsModal.hidden) {
    event.stopPropagation();
    settingsClose();
  }
});

// The native app calls this after its folder chooser returns a path.
window.radioApplySourceFolder = function (path) {
  const input = document.getElementById("setting-source_dir");
  if (!input) return;
  input.value = path;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  const result = settingsPanels.querySelector('[data-settings-result="source"]');
  if (result) settingsCheckSource(path, result);
};

window.radioOpenSettings = settingsOpen;
