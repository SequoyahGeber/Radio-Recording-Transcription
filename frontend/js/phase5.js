// Phase Five incident coordination, shift handoff, and actionable health center.
const phaseFiveState = {
  profile: null,
  incidents: [],
  incident: null,
  assignees: [],
  health: null,
  handoff: null,
  handoffText: "",
  pendingLinkTranscriptId: null,
};

const phaseFiveIncidentView = document.getElementById("incident-workspace-view");
const phaseFiveHealthView = document.getElementById("health-center-view");
const phaseFiveIncidentList = document.getElementById("incident-list");
const phaseFiveIncidentDetail = document.getElementById("incident-detail");
const phaseFiveIncidentDialog = document.getElementById("incident-editor-dialog");
const phaseFiveLinkDialog = document.getElementById("incident-link-dialog");
const phaseFiveHandoffDialog = document.getElementById("shift-handoff-dialog");

function phaseFiveCan(permission) {
  return Boolean(phaseFiveState.profile?.permissions?.[permission]);
}

function phaseFiveDate(value, includeSeconds = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    ...(includeSeconds ? { second: "2-digit" } : {}),
  });
}

function phaseFiveBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(
    units.length - 1,
    Math.floor(Math.log(bytes) / Math.log(1024)),
  );
  return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function phaseFiveButton(label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function phaseFiveHidePrimaryViews() {
  dashboard.hidden = true;
  document.getElementById("archive-search-view").hidden = true;
  document.getElementById("alert-inbox-view").hidden = true;
  phaseFiveIncidentView.hidden = true;
  phaseFiveHealthView.hidden = true;
  document.body.classList.remove(
    "archive-search-mode",
    "alert-inbox-mode",
    "incident-workspace-mode",
    "health-center-mode",
  );
}

function phaseFiveBackToBoard() {
  phaseFiveIncidentView.hidden = true;
  phaseFiveHealthView.hidden = true;
  document.body.classList.remove("incident-workspace-mode", "health-center-mode");
  dashboard.hidden = false;
  updateSearchResults();
}

function phaseFiveOpenIncidents() {
  phaseFiveHidePrimaryViews();
  phaseFiveIncidentView.hidden = false;
  document.body.classList.add("incident-workspace-mode");
  phaseFiveLoadIncidents().catch((error) => showToast(error.message, "danger"));
}

function phaseFiveOpenHealth(componentId = "") {
  phaseFiveHidePrimaryViews();
  phaseFiveHealthView.hidden = false;
  document.body.classList.add("health-center-mode");
  phaseFiveLoadHealth().then(() => {
    if (componentId) {
      document
        .querySelector(`[data-health-card="${CSS.escape(componentId)}"]`)
        ?.scrollIntoView({ block: "center" });
    }
  }).catch((error) => showToast(error.message, "danger"));
}

async function phaseFiveLoadAssignees() {
  phaseFiveState.assignees = await phaseTwoJson("/api/alert-assignees");
  const owner = document.getElementById("incident-editor-owner");
  owner.querySelectorAll("option:not(:first-child)").forEach((option) => option.remove());
  phaseFiveState.assignees.forEach((user) => {
    const option = document.createElement("option");
    option.value = user.username;
    option.textContent = user.display_name;
    owner.appendChild(option);
  });
}

function phaseFiveSetIncidentBadge(count) {
  const badge = document.getElementById("incident-active-count");
  const total = Number(count || 0);
  badge.textContent = total > 99 ? "99+" : String(total);
  badge.hidden = total === 0;
}

function phaseFiveRenderIncidentList() {
  phaseFiveIncidentList.replaceChildren();
  if (!phaseFiveState.incidents.length) {
    const empty = document.createElement("div");
    empty.className = "incident-empty";
    empty.innerHTML = "<strong>No incidents match this view</strong><span>Create an incident from a transmission or broaden the filter.</span>";
    phaseFiveIncidentList.appendChild(empty);
    return;
  }
  phaseFiveState.incidents.forEach((incident) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `incident-list-item severity-${incident.severity}`;
    button.classList.toggle("selected", phaseFiveState.incident?.id === incident.id);
    button.setAttribute("aria-label", `Open incident ${incident.title}`);

    const header = document.createElement("span");
    header.className = "incident-list-item-header";
    const title = document.createElement("strong");
    title.textContent = incident.title;
    const severity = document.createElement("span");
    severity.className = `alert-severity severity-${incident.severity}`;
    severity.textContent = incident.severity;
    header.append(title, severity);

    const meta = document.createElement("span");
    meta.className = "incident-list-item-meta";
    meta.textContent = [
      incident.status,
      incident.location,
      incident.owner_username ? `Owner: ${incident.owner_username}` : "Unassigned",
    ].filter(Boolean).join(" · ");
    const counts = document.createElement("span");
    counts.className = "incident-list-item-counts";
    counts.textContent = `${incident.transcript_count} transmissions · ${incident.open_task_count} open tasks`;
    button.append(header, meta, counts);
    button.addEventListener("click", () => phaseFiveSelectIncident(incident.id));
    phaseFiveIncidentList.appendChild(button);
  });
}

async function phaseFiveLoadIncidents() {
  const parameters = new URLSearchParams({
    status: document.getElementById("incident-filter-status").value,
    q: document.getElementById("incident-filter-query").value.trim(),
  });
  const payload = await phaseTwoJson(`/api/incidents?${parameters}`);
  phaseFiveState.incidents = payload.items;
  const openCount = payload.items.filter((item) => item.status !== "resolved").length;
  phaseFiveSetIncidentBadge(openCount);
  document.getElementById("incident-workspace-summary").textContent =
    `${payload.count} incident${payload.count === 1 ? "" : "s"} in this view`;
  phaseFiveRenderIncidentList();
  if (
    phaseFiveState.incident &&
    payload.items.some((item) => item.id === phaseFiveState.incident.id)
  ) {
    await phaseFiveSelectIncident(phaseFiveState.incident.id, false);
  }
}

async function phaseFiveSelectIncident(incidentId, focus = true) {
  phaseFiveState.incident = await phaseTwoJson(`/api/incidents/${incidentId}`);
  phaseFiveRenderIncidentList();
  phaseFiveRenderIncidentDetail();
  if (focus) phaseFiveIncidentDetail.focus?.();
}

function phaseFiveTaskRow(task) {
  const row = document.createElement("div");
  row.className = `incident-task incident-task-${task.status}`;
  const updateTask = async (changes) => {
    try {
      await phaseTwoJson(`/api/incident-tasks/${task.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...changes, version: task.version }),
      });
      await phaseFiveSelectIncident(task.incident_id, false);
      await phaseFiveLoadIncidents();
    } catch (error) {
      showToast(error.message, "danger");
    }
  };
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = task.status === "done";
  toggle.disabled = !phaseFiveCan("manage_incidents");
  toggle.setAttribute("aria-label", `Complete task ${task.description}`);
  toggle.addEventListener("change", () =>
    updateTask({ status: toggle.checked ? "done" : "open" }),
  );
  const copy = document.createElement("span");
  const description = document.createElement("strong");
  description.textContent = task.description;
  const meta = document.createElement("small");
  meta.textContent = [
    task.assigned_to ? `Assigned to ${task.assigned_to}` : "Unassigned",
    task.due_at ? `Due ${phaseFiveDate(task.due_at)}` : "",
    task.status.replace("_", " "),
  ].filter(Boolean).join(" · ");
  copy.append(description, meta);
  row.append(toggle, copy);
  if (phaseFiveCan("manage_incidents")) {
    const assignee = document.createElement("select");
    assignee.className = "incident-task-assignee";
    assignee.setAttribute("aria-label", `Assign task ${task.description}`);
    const unassigned = document.createElement("option");
    unassigned.value = "";
    unassigned.textContent = "Unassigned";
    assignee.appendChild(unassigned);
    phaseFiveState.assignees.forEach((user) => {
      const option = document.createElement("option");
      option.value = user.username;
      option.textContent = user.display_name;
      assignee.appendChild(option);
    });
    assignee.value = task.assigned_to || "";
    assignee.addEventListener("change", () =>
      updateTask({ assigned_to: assignee.value }),
    );
    row.appendChild(assignee);
  }
  return row;
}

function phaseFiveRenderIncidentDetail() {
  const incident = phaseFiveState.incident;
  phaseFiveIncidentDetail.replaceChildren();
  phaseFiveIncidentDetail.tabIndex = -1;
  if (!incident) return;

  const header = document.createElement("header");
  header.className = "incident-detail-header";
  const heading = document.createElement("div");
  const eyebrow = document.createElement("span");
  eyebrow.className = `alert-severity severity-${incident.severity}`;
  eyebrow.textContent = incident.severity;
  const title = document.createElement("h3");
  title.textContent = incident.title;
  const meta = document.createElement("div");
  meta.className = "incident-detail-meta";
  meta.textContent = [
    incident.status,
    incident.location,
    incident.owner_username ? `Owner: ${incident.owner_username}` : "Unassigned",
    `Started ${phaseFiveDate(incident.started_at)}`,
  ].filter(Boolean).join(" · ");
  heading.append(eyebrow, title, meta);
  const actions = document.createElement("div");
  if (phaseFiveCan("manage_incidents")) {
    actions.appendChild(
      phaseFiveButton("Edit", "btn", () => phaseFiveOpenIncidentEditor(incident)),
    );
  }
  header.append(heading, actions);

  const summary = document.createElement("p");
  summary.className = "incident-summary";
  summary.textContent = incident.summary || "No incident summary has been added yet.";
  const tags = document.createElement("div");
  tags.className = "incident-tags";
  incident.tags.forEach((tag) => {
    const chip = document.createElement("span");
    chip.textContent = tag;
    tags.appendChild(chip);
  });

  const transmissionSection = document.createElement("section");
  transmissionSection.className = "incident-detail-section";
  const transmissionHeading = document.createElement("h4");
  transmissionHeading.textContent = `Timeline · ${incident.transcripts.length} transmissions`;
  transmissionSection.appendChild(transmissionHeading);
  if (!incident.transcripts.length) {
    const empty = document.createElement("p");
    empty.textContent = "Add transmissions from the detail drawer to build the incident timeline.";
    transmissionSection.appendChild(empty);
  }
  incident.transcripts.forEach((transcript) => {
    const row = document.createElement("div");
    row.className = "incident-transcript";
    const open = phaseFiveButton(
      `${transcript.channel || "Unknown"} · ${phaseFiveDate(transcript.recorded_at || transcript.timestamp)}`,
      "incident-transcript-open",
      () => phaseTwoOpenDrawer(transcript.id),
    );
    const excerpt = document.createElement("span");
    excerpt.textContent = transcript.transcript_text || "Transcript unavailable";
    row.append(open, excerpt);
    if (phaseFiveCan("manage_incidents")) {
      row.appendChild(
        phaseFiveButton("Remove", "btn btn-quiet", async () => {
          try {
            await phaseTwoJson(
              `/api/incidents/${incident.id}/transcripts/${transcript.id}`,
              { method: "DELETE" },
            );
            await phaseFiveSelectIncident(incident.id, false);
            await phaseFiveLoadIncidents();
          } catch (error) {
            showToast(error.message, "danger");
          }
        }),
      );
    }
    transmissionSection.appendChild(row);
  });

  const tasks = document.createElement("section");
  tasks.className = "incident-detail-section";
  const tasksHeading = document.createElement("h4");
  tasksHeading.textContent = `Tasks · ${incident.tasks.filter((task) => ["open", "in_progress"].includes(task.status)).length} open`;
  tasks.appendChild(tasksHeading);
  incident.tasks.forEach((task) => tasks.appendChild(phaseFiveTaskRow(task)));
  if (phaseFiveCan("manage_incidents")) {
    const form = document.createElement("div");
    form.className = "incident-inline-form";
    const input = document.createElement("input");
    input.placeholder = "Add a follow-up task";
    input.maxLength = 500;
    const add = phaseFiveButton("Add task", "btn", async () => {
      if (!input.value.trim()) return;
      try {
        await phaseTwoJson(`/api/incidents/${incident.id}/tasks`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ description: input.value.trim() }),
        });
        await phaseFiveSelectIncident(incident.id, false);
        await phaseFiveLoadIncidents();
      } catch (error) {
        showToast(error.message, "danger");
      }
    });
    form.append(input, add);
    tasks.appendChild(form);
  }

  const notes = document.createElement("section");
  notes.className = "incident-detail-section";
  const notesHeading = document.createElement("h4");
  notesHeading.textContent = "Operator notes";
  notes.appendChild(notesHeading);
  incident.notes.forEach((note) => {
    const item = document.createElement("div");
    item.className = "incident-note";
    const copy = document.createElement("p");
    copy.textContent = note.note;
    const byline = document.createElement("small");
    byline.textContent = `${note.created_by} · ${phaseFiveDate(note.created_at)}`;
    item.append(copy, byline);
    notes.appendChild(item);
  });
  if (phaseFiveCan("manage_incidents")) {
    const form = document.createElement("div");
    form.className = "incident-inline-form";
    const input = document.createElement("textarea");
    input.rows = 2;
    input.placeholder = "Add a timestamped incident note";
    const add = phaseFiveButton("Add note", "btn", async () => {
      if (!input.value.trim()) return;
      try {
        await phaseTwoJson(`/api/incidents/${incident.id}/notes`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note: input.value.trim() }),
        });
        await phaseFiveSelectIncident(incident.id, false);
      } catch (error) {
        showToast(error.message, "danger");
      }
    });
    form.append(input, add);
    notes.appendChild(form);
  }
  phaseFiveIncidentDetail.append(header, summary, tags, transmissionSection, tasks, notes);
}

function phaseFiveDateTimeLocal(value) {
  const date = value ? new Date(value) : new Date();
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function phaseFiveOpenIncidentEditor(incident = null, preservePendingLink = false) {
  if (!phaseFiveCan("manage_incidents")) {
    showToast("Operator clearance is required to manage incidents.", "danger");
    return;
  }
  if (!preservePendingLink || incident) {
    phaseFiveState.pendingLinkTranscriptId = null;
  }
  document.getElementById("incident-editor-heading").textContent =
    incident ? "Edit incident" : "New incident";
  document.getElementById("incident-editor-id").value = incident?.id || "";
  document.getElementById("incident-editor-version").value = incident?.version || "";
  document.getElementById("incident-editor-title").value = incident?.title || "";
  document.getElementById("incident-editor-severity").value = incident?.severity || "informational";
  document.getElementById("incident-editor-status").value = incident?.status || "monitoring";
  document.getElementById("incident-editor-owner").value = incident?.owner_username || "";
  document.getElementById("incident-editor-started").value = phaseFiveDateTimeLocal(incident?.started_at);
  document.getElementById("incident-editor-location").value = incident?.location || "";
  document.getElementById("incident-editor-tags").value = (incident?.tags || []).join(", ");
  document.getElementById("incident-editor-summary").value = incident?.summary || "";
  phaseFiveIncidentDialog.showModal();
  document.getElementById("incident-editor-title").focus();
}

async function phaseFiveSaveIncident() {
  const id = document.getElementById("incident-editor-id").value;
  const version = Number(document.getElementById("incident-editor-version").value);
  const started = document.getElementById("incident-editor-started").value;
  const payload = {
    title: document.getElementById("incident-editor-title").value,
    severity: document.getElementById("incident-editor-severity").value,
    status: document.getElementById("incident-editor-status").value,
    owner_username: document.getElementById("incident-editor-owner").value || null,
    location: document.getElementById("incident-editor-location").value,
    summary: document.getElementById("incident-editor-summary").value,
    tags: document.getElementById("incident-editor-tags").value
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean),
    started_at: started ? new Date(started).toISOString() : null,
    ...(version ? { version } : {}),
  };
  try {
    const incident = await phaseTwoJson(id ? `/api/incidents/${id}` : "/api/incidents", {
      method: id ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    phaseFiveIncidentDialog.close();
    const pendingTranscriptId = phaseFiveState.pendingLinkTranscriptId;
    phaseFiveState.pendingLinkTranscriptId = null;
    if (!id && pendingTranscriptId) {
      try {
        await phaseTwoJson(`/api/incidents/${incident.id}/transcripts`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ transcript_id: pendingTranscriptId }),
        });
      } catch (linkError) {
        showToast(
          `Incident created, but the transmission was not linked: ${linkError.message}`,
          "danger",
        );
      }
    }
    await phaseFiveLoadIncidents();
    await phaseFiveSelectIncident(incident.id, false);
    showToast("Incident saved.", "success");
  } catch (error) {
    if (error.status === 409 && id) await phaseFiveSelectIncident(Number(id), false);
    showToast(error.message, "danger");
  }
}

async function phaseFiveOpenLinkDialog() {
  if (!phaseFiveCan("manage_incidents")) {
    showToast("Operator clearance is required to link incidents.", "danger");
    return;
  }
  const transcriptId = phaseTwoState.detail?.id;
  if (!transcriptId) return;
  phaseFiveState.pendingLinkTranscriptId = transcriptId;
  const payload = await phaseTwoJson("/api/incidents?status=open");
  const list = document.getElementById("incident-link-list");
  list.replaceChildren();
  payload.items.forEach((incident) => {
    const button = phaseFiveButton(
      incident.title,
      "workspace-row",
      async () => {
        try {
          await phaseTwoJson(`/api/incidents/${incident.id}/transcripts`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ transcript_id: transcriptId }),
          });
          phaseFiveLinkDialog.close();
          phaseFiveState.pendingLinkTranscriptId = null;
          showToast(`Added to ${incident.title}.`, "success");
          await phaseFiveLoadIncidents();
        } catch (error) {
          showToast(error.message, "danger");
        }
      },
    );
    const meta = document.createElement("span");
    meta.textContent = `${incident.severity} · ${incident.status} · ${incident.transcript_count} transmissions`;
    button.appendChild(meta);
    list.appendChild(button);
  });
  if (!payload.items.length) {
    const empty = document.createElement("p");
    empty.textContent = "No open incidents. Create one to link this transmission.";
    list.appendChild(empty);
  }
  phaseFiveLinkDialog.showModal();
}

function phaseFiveHandoffText(report) {
  const lines = [
    "RADIO COMMAND CENTER SHIFT HANDOFF",
    `Generated: ${phaseFiveDate(report.generated_at, true)}`,
    `Window: ${report.window_hours} hours`,
    "",
    `OPEN INCIDENTS (${report.counts.open_incidents})`,
    ...report.incidents.map((incident) =>
      `- [${incident.severity.toUpperCase()}] ${incident.title} (${incident.status}; owner: ${incident.owner_username || "unassigned"}; ${incident.open_task_count} open tasks)`),
    "",
    `ACTIVE ALERTS (${report.counts.active_alerts})`,
    ...report.alerts.map((alert) =>
      `- [${alert.severity.toUpperCase()}] ${alert.rule_name} on ${alert.channel || "unknown channel"} (${alert.status})`),
    "",
    `RECENT BOOKMARKS (${report.counts.recent_bookmarks})`,
    ...report.bookmarks.map((item) =>
      `- ${item.channel || "Unknown"}: ${String(item.transcript_text || "").replace(/\s+/g, " ").slice(0, 140)}`),
    "",
    `SYSTEM ISSUES (${report.counts.system_issues})`,
    ...report.system_issues.map((item) => `- ${item.label}: ${item.summary}`),
  ];
  return lines.join("\n");
}

function phaseFiveRenderHandoff(report) {
  const container = document.getElementById("shift-handoff-content");
  container.replaceChildren();
  const counts = document.createElement("div");
  counts.className = "shift-handoff-counts";
  Object.entries({
    "Open incidents": report.counts.open_incidents,
    "Active alerts": report.counts.active_alerts,
    "Recent bookmarks": report.counts.recent_bookmarks,
    "System issues": report.counts.system_issues,
  }).forEach(([label, value]) => {
    const card = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = value;
    const span = document.createElement("span");
    span.textContent = label;
    card.append(strong, span);
    counts.appendChild(card);
  });
  const pre = document.createElement("pre");
  phaseFiveState.handoffText = phaseFiveHandoffText(report);
  pre.textContent = phaseFiveState.handoffText;
  container.append(counts, pre);
}

async function phaseFiveLoadHandoff() {
  const hours = document.getElementById("shift-handoff-hours").value;
  phaseFiveState.handoff = await phaseTwoJson(`/api/shift-handoff?hours=${hours}`);
  phaseFiveRenderHandoff(phaseFiveState.handoff);
}

async function phaseFiveOpenHandoff() {
  phaseFiveHandoffDialog.showModal();
  document.getElementById("shift-handoff-content").textContent = "Generating handoff…";
  try {
    await phaseFiveLoadHandoff();
  } catch (error) {
    showToast(error.message, "danger");
  }
}

function phaseFiveHealthValue(component) {
  if (component.id === "storage") {
    return `${phaseFiveBytes(component.metrics.free_bytes)} free`;
  }
  if (component.queue_depth !== null && component.queue_depth !== undefined) {
    return `${component.queue_depth} queued`;
  }
  return component.status;
}

function phaseFiveRenderHealth(payload) {
  document.getElementById("health-center-summary").textContent =
    `${payload.status === "healthy" ? "All operational paths are healthy" : "One or more operational paths need attention"} · updated ${phaseFiveDate(payload.generated_at, true)}`;
  const list = document.getElementById("health-component-list");
  list.replaceChildren();
  payload.components.forEach((component) => {
    const card = document.createElement("article");
    card.className = `health-component health-${component.status}`;
    card.dataset.healthCard = component.id;
    const header = document.createElement("header");
    const identity = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = component.label;
    const status = document.createElement("span");
    status.className = `health-status health-status-${component.status}`;
    status.textContent = component.status;
    identity.append(title, status);
    const value = document.createElement("strong");
    value.textContent = phaseFiveHealthValue(component);
    header.append(identity, value);
    const summary = document.createElement("p");
    summary.textContent = component.summary;
    const details = document.createElement("dl");
    const rows = [
      ["Last success", component.last_success_at ? phaseFiveDate(component.last_success_at, true) : "Not recorded"],
      ["Heartbeat age", component.heartbeat_age_seconds === null ? "Not applicable" : `${component.heartbeat_age_seconds}s`],
      ["Last error", component.last_error || "None"],
      ["Safe recovery", component.action],
    ];
    rows.forEach(([term, description]) => {
      const dt = document.createElement("dt");
      dt.textContent = term;
      const dd = document.createElement("dd");
      dd.textContent = description;
      details.append(dt, dd);
    });
    card.append(header, summary, details);
    if (phaseFiveState.profile?.permissions?.console) {
      card.appendChild(
        phaseFiveButton("Open related events", "btn btn-quiet", () => {
          const service = document.getElementById("console-service");
          service.value = component.console_service;
          setConsoleOpen(true);
          fetchConsoleEntries();
        }),
      );
    }
    list.appendChild(card);
  });
}

async function phaseFiveLoadHealth() {
  phaseFiveState.health = await phaseTwoJson("/api/health-center");
  phaseFiveRenderHealth(phaseFiveState.health);
  phaseFiveState.health.components.forEach((component) => {
    const chip = document.querySelector(`[data-health-component="${component.id}"]`);
    if (!chip) return;
    chip.className = `health-chip health-${component.status}`;
    chip.querySelector("span").textContent = phaseFiveHealthValue(component);
    chip.title = component.summary;
  });
}

window.phaseFiveHandleEvent = function (event) {
  if (!event.type?.startsWith("incident")) return;
  phaseFiveLoadIncidents().catch(() => {});
  if (!phaseFiveIncidentView.hidden && phaseFiveState.incident?.id === event.resource_id) {
    phaseFiveSelectIncident(event.resource_id, false).catch(() => {});
  }
};

document.getElementById("incidents-button").addEventListener("click", phaseFiveOpenIncidents);
document.getElementById("health-center-button").addEventListener("click", () => phaseFiveOpenHealth());
document.getElementById("incidents-back-board").addEventListener("click", phaseFiveBackToBoard);
document.getElementById("incident-new-button").addEventListener("click", () => phaseFiveOpenIncidentEditor());
document.getElementById("incident-editor-save").addEventListener("click", phaseFiveSaveIncident);
document.getElementById("incident-refresh-button").addEventListener("click", phaseFiveLoadIncidents);
document.getElementById("incident-filter-status").addEventListener("change", phaseFiveLoadIncidents);
let phaseFiveIncidentSearchTimer;
document.getElementById("incident-filter-query").addEventListener("input", () => {
  clearTimeout(phaseFiveIncidentSearchTimer);
  phaseFiveIncidentSearchTimer = setTimeout(() => phaseFiveLoadIncidents(), 250);
});
document.getElementById("drawer-incident-button").addEventListener("click", () =>
  phaseFiveOpenLinkDialog().catch((error) => showToast(error.message, "danger")),
);
document.getElementById("incident-link-create").addEventListener("click", () => {
  phaseFiveLinkDialog.close();
  phaseFiveOpenIncidentEditor(null, true);
});
document.getElementById("shift-handoff-button").addEventListener("click", phaseFiveOpenHandoff);
document.getElementById("shift-handoff-refresh").addEventListener("click", phaseFiveLoadHandoff);
document.getElementById("shift-handoff-copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText(phaseFiveState.handoffText);
  showToast("Shift handoff copied.", "success");
});
document.getElementById("shift-handoff-download").addEventListener("click", () => {
  const hours = document.getElementById("shift-handoff-hours").value;
  window.location.assign(`/api/shift-handoff?hours=${hours}&format=text`);
});
document.getElementById("health-back-board").addEventListener("click", phaseFiveBackToBoard);
document.getElementById("health-refresh-button").addEventListener("click", phaseFiveLoadHealth);
document.getElementById("health-export-diagnostics").addEventListener("click", () => {
  window.location.assign("/api/diagnostics.zip");
});
document.querySelectorAll("[data-health-component]").forEach((chip) => {
  chip.addEventListener("click", () => phaseFiveOpenHealth(chip.dataset.healthComponent));
});

Promise.all([
  phaseTwoJson("/api/me"),
  phaseFiveLoadAssignees(),
  phaseFiveLoadIncidents(),
  phaseFiveLoadHealth(),
])
  .then(([profile]) => {
    phaseFiveState.profile = profile;
    document.getElementById("incident-new-button").hidden =
      !phaseFiveCan("manage_incidents");
    document.getElementById("drawer-incident-button").hidden =
      !phaseFiveCan("manage_incidents");
    document.getElementById("health-export-diagnostics").hidden =
      !phaseFiveCan("diagnostics");
  })
  .catch((error) =>
    recordConsoleEvent("warning", "Phase Five tools could not initialize", error.message),
  );

window.setInterval(() => {
  phaseFiveLoadHealth().catch(() => {});
  phaseFiveLoadIncidents().catch(() => {});
}, 30000);
