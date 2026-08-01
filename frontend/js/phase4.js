// Phase Four durable collaboration, alert inbox, and server-owned alert rules.
const phaseFourState = {
  alerts: [],
  rules: [],
  assignees: [],
  nextBeforeId: null,
  profile: null,
  notificationPreferences: {},
};

const phaseFourView = document.getElementById("alert-inbox-view");
const phaseFourList = document.getElementById("alert-inbox-list");
const phaseFourSummary = document.getElementById("alert-inbox-summary");
const phaseFourBadge = document.getElementById("alert-active-count");
const phaseFourRulesDialog = document.getElementById("alert-rule-dialog");
const phaseFourSeverityOrder = {
  informational: 1,
  caution: 2,
  urgent: 3,
  critical: 4,
};
window.phaseFourAlertsEnabled = true;

function phaseFourCan(permission) {
  return Boolean(phaseFourState.profile?.permissions?.[permission]);
}

function phaseFourSetBadge(count) {
  const total = Number(count || 0);
  phaseFourBadge.textContent = total > 99 ? "99+" : String(total);
  phaseFourBadge.hidden = total === 0;
  document.getElementById("alerts-button").classList.toggle(
    "has-active-alerts",
    total > 0,
  );
}

function phaseFourOpenView() {
  dashboard.hidden = true;
  document.getElementById("archive-search-view").hidden = true;
  phaseFourView.hidden = false;
  document.body.classList.add("alert-inbox-mode");
  document.body.classList.remove("archive-search-mode");
  phaseFourLoadAlerts().catch((error) => showToast(error.message, "danger"));
}

function phaseFourCloseView() {
  phaseFourView.hidden = true;
  document.body.classList.remove("alert-inbox-mode");
  if (globalSearchInput.value.trim() && window.phaseThreeSearch) {
    window.phaseThreeSearch(globalSearchInput.value, true);
  } else {
    dashboard.hidden = false;
    updateSearchResults();
  }
}

function phaseFourFormatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Time unavailable"
    : date.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
}

function phaseFourButton(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function phaseFourAssigneeSelect(alert) {
  const select = document.createElement("select");
  select.className = "alert-assignee";
  select.setAttribute("aria-label", `Assign ${alert.rule_name}`);
  const unassigned = document.createElement("option");
  unassigned.value = "";
  unassigned.textContent = "Unassigned";
  select.appendChild(unassigned);
  phaseFourState.assignees.forEach((user) => {
    const option = document.createElement("option");
    option.value = user.username;
    option.textContent = user.display_name;
    select.appendChild(option);
  });
  select.value = alert.assigned_to || "";
  select.disabled = !phaseFourCan("acknowledge_alerts");
  select.addEventListener("change", () =>
    phaseFourUpdateAlert(alert, { assigned_to: select.value }),
  );
  return select;
}

function phaseFourRenderAlert(alert) {
  const item = document.createElement("article");
  item.className = `alert-item severity-${alert.severity}`;
  item.dataset.alertId = alert.id;
  item.dataset.version = alert.version;
  item.tabIndex = 0;

  const header = document.createElement("div");
  header.className = "alert-item-header";
  const identity = document.createElement("div");
  identity.className = "alert-identity";
  const severity = document.createElement("span");
  severity.className = `alert-severity severity-${alert.severity}`;
  severity.textContent = alert.severity;
  const title = document.createElement("strong");
  title.textContent = alert.rule_name;
  const status = document.createElement("span");
  status.className = `alert-status alert-status-${alert.status}`;
  status.textContent = alert.status.replace("_", " ");
  identity.append(severity, title, status);
  const time = document.createElement("time");
  time.dateTime = alert.created_at;
  time.textContent = phaseFourFormatDate(alert.created_at);
  header.append(identity, time);

  const explanation = document.createElement("p");
  explanation.className = "alert-explanation";
  explanation.textContent = alert.explanation;
  const transcript = document.createElement("p");
  transcript.className = "alert-transcript";
  transcript.textContent = alert.transcript_text || "Transcript unavailable";
  const meta = document.createElement("div");
  meta.className = "alert-meta";
  meta.textContent = [
    alert.channel || "Unknown channel",
    phaseFourFormatDate(alert.recorded_at),
    alert.acknowledged_by ? `Acknowledged by ${alert.acknowledged_by}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const actions = document.createElement("div");
  actions.className = "alert-actions";
  actions.appendChild(
    phaseFourButton("Open transmission", "btn btn-primary", () =>
      phaseTwoOpenDrawer(alert.transcript_id),
    ),
  );
  if (phaseFourCan("acknowledge_alerts")) {
    if (alert.status === "open") {
      actions.appendChild(
        phaseFourButton("Acknowledge", "btn alert-acknowledge", () =>
          phaseFourUpdateAlert(alert, { status: "acknowledged" }),
        ),
      );
    } else if (alert.status === "acknowledged") {
      actions.appendChild(
        phaseFourButton("Resolve", "btn alert-resolve", () =>
          phaseFourUpdateAlert(alert, { status: "resolved" }),
        ),
      );
    } else {
      actions.appendChild(
        phaseFourButton("Reopen", "btn btn-quiet", () =>
          phaseFourUpdateAlert(alert, { status: "open" }),
        ),
      );
    }
    if (
      phaseFourCan("manage_alert_rules") &&
      !["resolved", "false_positive"].includes(alert.status)
    ) {
      actions.appendChild(
        phaseFourButton("False positive", "btn btn-quiet", () =>
          phaseFourUpdateAlert(alert, {
            status: "false_positive",
            resolution_note: "Marked false positive from alert inbox",
          }),
        ),
      );
    }
    actions.appendChild(phaseFourAssigneeSelect(alert));
  }
  item.append(header, explanation, transcript, meta, actions);
  return item;
}

function phaseFourRenderAlerts() {
  phaseFourList.replaceChildren();
  if (!phaseFourState.alerts.length) {
    const empty = document.createElement("div");
    empty.className = "archive-search-empty";
    const title = document.createElement("strong");
    title.textContent = "No alerts match this view";
    const help = document.createElement("span");
    help.textContent = "Try another status or severity filter.";
    empty.append(title, help);
    phaseFourList.appendChild(empty);
    return;
  }
  phaseFourState.alerts.forEach((alert) =>
    phaseFourList.appendChild(phaseFourRenderAlert(alert)),
  );
}

async function phaseFourLoadAlerts(append = false) {
  const parameters = new URLSearchParams({
    status: document.getElementById("alert-filter-status").value,
    severity: document.getElementById("alert-filter-severity").value,
    assigned_to: document.getElementById("alert-filter-assignee").value,
    limit: "100",
  });
  if (append && phaseFourState.nextBeforeId) {
    parameters.set("before_id", phaseFourState.nextBeforeId);
  }
  const payload = await phaseTwoJson(`/api/alerts?${parameters}`);
  phaseFourState.alerts = append
    ? [...phaseFourState.alerts, ...payload.items]
    : payload.items;
  phaseFourState.nextBeforeId = payload.next_before_id;
  phaseFourSetBadge(payload.active_count);
  phaseFourSummary.textContent =
    `${payload.count.toLocaleString()} matching alert${
      payload.count === 1 ? "" : "s"
    } · ${payload.active_count.toLocaleString()} need attention`;
  document.getElementById("alert-load-more").hidden = !payload.next_before_id;
  phaseFourRenderAlerts();
}

async function phaseFourUpdateAlert(alert, changes) {
  try {
    const updated = await phaseTwoJson(`/api/alerts/${alert.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...changes, version: alert.version }),
    });
    const index = phaseFourState.alerts.findIndex((item) => item.id === alert.id);
    if (index >= 0) phaseFourState.alerts[index] = updated;
    phaseFourRenderAlerts();
    await phaseFourLoadSummary();
    showToast(`Alert ${updated.status.replace("_", " ")}.`, "success");
  } catch (error) {
    if (error.status === 409) {
      showToast("Another operator changed this alert. Refreshing…", "danger");
      await phaseFourLoadAlerts();
      return;
    }
    showToast(error.message, "danger");
  }
}

async function phaseFourLoadSummary() {
  const summary = await phaseTwoJson("/api/alerts/summary");
  phaseFourSetBadge(summary.active_count);
  return summary;
}

async function phaseFourLoadAssignees() {
  phaseFourState.assignees = await phaseTwoJson("/api/alert-assignees");
  const filter = document.getElementById("alert-filter-assignee");
  filter.querySelectorAll("option:not(:first-child)").forEach((option) => option.remove());
  phaseFourState.assignees.forEach((user) => {
    const option = document.createElement("option");
    option.value = user.username;
    option.textContent = user.display_name;
    filter.appendChild(option);
  });
}

function phaseFourRuleTerms(rules) {
  return rules
    .filter((rule) => rule.active)
    .flatMap((rule) => rule.terms);
}

function phaseFourResetRule(seed = "") {
  document.getElementById("alert-rule-id").value = "";
  document.getElementById("alert-rule-version").value = "";
  document.getElementById("alert-rule-name").value = seed
    ? `${seed} alert`
    : "";
  document.getElementById("alert-rule-description").value = "";
  document.getElementById("alert-rule-severity").value = "caution";
  document.getElementById("alert-rule-match-mode").value = "whole_word";
  document.getElementById("alert-rule-terms").value = seed;
  document.getElementById("alert-rule-exclusions").value = "";
  document.getElementById("alert-rule-channels").value = "";
  document.getElementById("alert-rule-cooldown").value = "60";
  document.getElementById("alert-rule-quality").value = "0";
  document.getElementById("alert-rule-start-time").value = "";
  document.getElementById("alert-rule-end-time").value = "";
  document.getElementById("alert-rule-escalation").value = "0";
  document.getElementById("alert-rule-sound").value = "";
  document.getElementById("alert-rule-requires-ack").checked = false;
  document.getElementById("alert-rule-active").checked = true;
  document.getElementById("alert-rule-test-result").textContent = "";
}

function phaseFourEditRule(rule) {
  document.getElementById("alert-rule-id").value = rule.id;
  document.getElementById("alert-rule-version").value = rule.version;
  document.getElementById("alert-rule-name").value = rule.name;
  document.getElementById("alert-rule-description").value =
    rule.description || "";
  document.getElementById("alert-rule-severity").value = rule.severity;
  document.getElementById("alert-rule-match-mode").value = rule.match_mode;
  document.getElementById("alert-rule-terms").value = rule.terms.join(", ");
  document.getElementById("alert-rule-exclusions").value =
    rule.exclusions.join(", ");
  document.getElementById("alert-rule-channels").value =
    rule.channels.join(", ");
  document.getElementById("alert-rule-cooldown").value =
    rule.cooldown_seconds;
  document.getElementById("alert-rule-quality").value = rule.minimum_quality;
  document.getElementById("alert-rule-start-time").value = rule.start_time || "";
  document.getElementById("alert-rule-end-time").value = rule.end_time || "";
  document.getElementById("alert-rule-escalation").value =
    rule.escalation_seconds || 0;
  document.getElementById("alert-rule-sound").value = rule.sound || "";
  document.getElementById("alert-rule-requires-ack").checked =
    rule.requires_ack;
  document.getElementById("alert-rule-active").checked = rule.active;
  document.getElementById("alert-rule-test-result").textContent = "";
}

function phaseFourRenderRules() {
  const list = document.getElementById("alert-rule-list");
  list.replaceChildren();
  document.getElementById("alert-rule-count").textContent =
    `${phaseFourState.rules.length} rules`;
  phaseFourState.rules.forEach((rule) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "alert-rule-row";
    row.setAttribute("aria-label", `Edit ${rule.name}`);
    const name = document.createElement("strong");
    name.textContent = rule.name;
    const meta = document.createElement("span");
    meta.textContent =
      `${rule.severity} · ${rule.terms.length} term${
        rule.terms.length === 1 ? "" : "s"
      } · ${rule.active ? "active" : "paused"}`;
    row.append(name, meta);
    row.addEventListener("click", () => phaseFourEditRule(rule));
    list.appendChild(row);
  });
}

async function phaseFourLoadRules() {
  phaseFourState.rules = await phaseTwoJson("/api/alert-rules");
  window.setServerAlertKeywords(phaseFourRuleTerms(phaseFourState.rules));
  phaseFourRenderRules();
}

window.phaseFourOpenRuleDialog = function (seed = "") {
  if (!phaseFourCan("manage_alert_rules")) {
    phaseFourOpenView();
    showToast("Supervisor clearance is required to change alert rules.");
    return;
  }
  phaseFourResetRule(seed);
  phaseFourRenderRules();
  phaseFourRulesDialog.showModal();
  document.getElementById("alert-rule-name").focus();
};

function phaseFourSplitValues(value) {
  return String(value)
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function phaseFourRulePayload() {
  const version = Number(document.getElementById("alert-rule-version").value);
  return {
    name: document.getElementById("alert-rule-name").value,
    description: document.getElementById("alert-rule-description").value,
    severity: document.getElementById("alert-rule-severity").value,
    match_mode: document.getElementById("alert-rule-match-mode").value,
    terms: phaseFourSplitValues(
      document.getElementById("alert-rule-terms").value,
    ),
    exclusions: phaseFourSplitValues(
      document.getElementById("alert-rule-exclusions").value,
    ),
    channels: phaseFourSplitValues(
      document.getElementById("alert-rule-channels").value,
    ),
    cooldown_seconds: Number(
      document.getElementById("alert-rule-cooldown").value,
    ),
    minimum_quality: Number(
      document.getElementById("alert-rule-quality").value,
    ),
    start_time: document.getElementById("alert-rule-start-time").value,
    end_time: document.getElementById("alert-rule-end-time").value,
    escalation_seconds: Number(
      document.getElementById("alert-rule-escalation").value,
    ),
    sound: document.getElementById("alert-rule-sound").value,
    requires_ack: document.getElementById("alert-rule-requires-ack").checked,
    active: document.getElementById("alert-rule-active").checked,
    ...(version ? { version } : {}),
  };
}

async function phaseFourSaveRule() {
  const id = document.getElementById("alert-rule-id").value;
  try {
    const rule = await phaseTwoJson(id ? `/api/alert-rules/${id}` : "/api/alert-rules", {
      method: id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(phaseFourRulePayload()),
    });
    await phaseFourLoadRules();
    phaseFourEditRule(rule);
    showToast("Alert rule saved.", "success");
  } catch (error) {
    if (error.status === 409) await phaseFourLoadRules();
    showToast(error.message, "danger");
  }
}

async function phaseFourTestRule() {
  const id = document.getElementById("alert-rule-id").value;
  const result = document.getElementById("alert-rule-test-result");
  if (!id) {
    result.textContent = "Save the rule before testing it.";
    return;
  }
  try {
    const payload = await phaseTwoJson(`/api/alert-rules/${id}/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        transcript_text: document.getElementById("alert-rule-test-text").value,
      }),
    });
    result.textContent = payload.explanation;
    result.classList.toggle("test-matched", payload.matched);
  } catch (error) {
    result.textContent = error.message;
  }
}

function phaseFourNotifyAlert(alert) {
  const minimum =
    phaseFourState.notificationPreferences.minimum_severity || "urgent";
  if (
    !notificationsEnabled ||
    phaseFourSeverityOrder[alert.severity] < phaseFourSeverityOrder[minimum] ||
    !("Notification" in window) ||
    Notification.permission !== "granted"
  ) {
    return;
  }
  const notification = new Notification(
    `${alert.severity.toUpperCase()}: ${alert.rule_name}`,
    {
      body: `${alert.channel || "Radio"} · ${alert.matched_text}`,
      tag: `radio-alert-${alert.id}`,
      requireInteraction: alert.severity === "critical",
    },
  );
  notification.onclick = () => {
    window.focus();
    phaseFourOpenView();
  };
}

window.phaseFourHandleEvent = function (event) {
  if (event.type === "alert.created") {
    const alert = event.payload?.alert;
    if (!alert) return;
    if (!event.replayed) {
      phaseFourNotifyAlert(alert);
      showToast(`${alert.severity.toUpperCase()}: ${alert.rule_name}`, "danger");
    }
    phaseFourLoadSummary().catch(() => {});
    if (!phaseFourView.hidden) phaseFourLoadAlerts().catch(() => {});
  } else if (event.type === "alert.updated") {
    phaseFourLoadSummary().catch(() => {});
    if (!phaseFourView.hidden) phaseFourLoadAlerts().catch(() => {});
  } else if (event.type?.startsWith("alert_rule.")) {
    phaseFourLoadRules().catch(() => {});
  }
};

window.phaseFourHandleTranscriptUpdate = function (transcript) {
  if (!transcript?.id) return;
  document
    .querySelectorAll(
      `.message-card[data-transcript-id="${CSS.escape(String(transcript.id))}"]`,
    )
    .forEach((card) => card.replaceWith(createMessageCard(transcript)));
  if (phaseTwoState.detail?.id === transcript.id && !phaseTwoDrawer.hidden) {
    phaseTwoOpenDrawer(transcript.id);
  }
  if (!document.getElementById("archive-search-view").hidden) {
    window.phaseThreeSearch?.(globalSearchInput.value, true);
  }
};

async function phaseFourLoadNotificationPreferences() {
  const payload = await phaseTwoJson("/api/notification-preferences");
  phaseFourState.notificationPreferences = payload.configuration || {};
}

async function phaseFourSaveNotificationPreferences() {
  const configuration = {
    ...phaseFourState.notificationPreferences,
    browser_enabled: notificationsEnabled,
    minimum_severity:
      phaseFourState.notificationPreferences.minimum_severity || "urgent",
    sound_enabled:
      phaseFourState.notificationPreferences.sound_enabled !== false,
  };
  const payload = await phaseTwoJson("/api/notification-preferences", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ configuration }),
  });
  phaseFourState.notificationPreferences = payload.configuration;
}

document.getElementById("alerts-button").addEventListener("click", phaseFourOpenView);
document.getElementById("alerts-back-board").addEventListener("click", phaseFourCloseView);
document.getElementById("refresh-alerts").addEventListener("click", () =>
  phaseFourLoadAlerts(),
);
document.getElementById("manage-alert-rules").addEventListener("click", () =>
  window.phaseFourOpenRuleDialog(),
);
document.getElementById("save-alert-rule").addEventListener("click", phaseFourSaveRule);
document.getElementById("new-alert-rule").addEventListener("click", () =>
  phaseFourResetRule(),
);
document.getElementById("test-alert-rule").addEventListener("click", phaseFourTestRule);
document.getElementById("alert-load-more").addEventListener("click", () =>
  phaseFourLoadAlerts(true),
);
for (const id of [
  "alert-filter-status",
  "alert-filter-severity",
  "alert-filter-assignee",
]) {
  document.getElementById(id).addEventListener("change", () =>
    phaseFourLoadAlerts(),
  );
}
notificationToggle.addEventListener("change", () => {
  window.setTimeout(() => phaseFourSaveNotificationPreferences().catch(() => {}), 0);
});

document.addEventListener("keydown", (event) => {
  if (
    event.key.toLowerCase() !== "a" ||
    phaseFourView.hidden ||
    event.metaKey ||
    event.ctrlKey ||
    event.altKey ||
    ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)
  ) {
    return;
  }
  const focused = document.activeElement.closest?.(".alert-item");
  const item = focused || phaseFourList.querySelector(".alert-item");
  const button = item?.querySelector(".alert-acknowledge");
  if (button) {
    event.preventDefault();
    button.click();
  }
});

Promise.all([
  phaseTwoJson("/api/me"),
  phaseFourLoadAssignees(),
  phaseFourLoadRules(),
  phaseFourLoadNotificationPreferences(),
  phaseFourLoadSummary(),
])
  .then(([profile]) => {
    phaseFourState.profile = profile;
    document.getElementById("manage-alert-rules").hidden =
      !phaseFourCan("manage_alert_rules");
  })
  .catch((error) =>
    recordConsoleEvent("warning", "Alert system could not initialize", error.message),
  );

window.setInterval(() => {
  phaseFourLoadSummary().catch(() => {});
  if (ws?.readyState === WebSocket.OPEN) ws.send("ping");
}, 15000);
