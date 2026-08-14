// Phase Seven deployment readiness, verified backup, and recovery drill controls.
const phaseSevenState = { latestBackup: null };

async function phaseSevenJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Recovery operation failed");
  return payload;
}

async function phaseSevenRefresh() {
  const [readiness, history, quality] = await Promise.all([
    phaseSevenJson("/api/readiness"),
    phaseSevenJson("/api/backups"),
    phaseSevenJson("/api/quality-analytics"),
  ]);
  const list = document.getElementById("readiness-check-list");
  list.replaceChildren();
  for (const check of readiness.checks) {
    const row = document.createElement("div");
    row.textContent = `${check.ready ? "✓" : "○"} ${check.label}`;
    list.appendChild(row);
  }
  const readyCount = readiness.checks.filter((check) => check.ready).length;
  document.getElementById("readiness-summary").textContent =
    `${readyCount}/${readiness.checks.length} ready`;
  phaseSevenState.latestBackup = history.backups?.[0] || null;
  document.getElementById("backup-drill-button").disabled =
    !phaseSevenState.latestBackup;
  document.getElementById("backup-status").textContent = phaseSevenState.latestBackup
    ? `Latest backup ${phaseSevenState.latestBackup.status} · ${new Date(
        phaseSevenState.latestBackup.created_at,
      ).toLocaleString()}`
    : "No recovery backup has been created yet.";
  phaseSevenRenderBackups(history.backups || []);
  const analytics = quality.analytics;
  const percent = (value) =>
    value === null || value === undefined ? "Not enough reviews" : `${(value * 100).toFixed(1)}%`;
  document.getElementById("quality-evaluation-summary").textContent =
    `${Number(analytics.totals.reviewed || 0).toLocaleString()} reviewed · ` +
    `${Number(analytics.totals.rescued || 0).toLocaleString()} rescues · ` +
    `false quarantine ${percent(analytics.false_quarantine_rate)} · ` +
    `missed garbage ${percent(analytics.missed_garbage_rate)}. ` +
    analytics.evaluation_gate.explanation;
  document.getElementById("quality-channel-metrics").innerHTML = analytics.channels
    .slice(0, 6)
    .map(
      (channel) =>
        `<div><strong>${escapeHTML(channel.channel)}</strong> · ${Number(
          channel.reviewed || 0,
        ).toLocaleString()} reviewed · ${Math.round(
          Number(channel.medium_quality || 0) * 100,
        )}% Medium quality</div>`,
    )
    .join("");
}

function phaseSevenRenderBackups(backups) {
  const list = document.getElementById("backup-list");
  list.replaceChildren();
  if (!backups.length) {
    const empty = document.createElement("p");
    empty.className = "backup-empty";
    empty.textContent =
      "Backups appear here once one has been created. They are also taken automatically.";
    list.appendChild(empty);
    return;
  }
  for (const backup of backups.slice(0, 5)) {
    const row = document.createElement("div");
    row.className = "backup-row";
    const summary = document.createElement("div");
    summary.className = "backup-summary";
    const when = new Date(backup.created_at);
    summary.innerHTML =
      `<strong>${escapeHTML(
        Number.isNaN(when.getTime()) ? backup.created_at : when.toLocaleString(),
      )}</strong>` +
      `<span>${escapeHTML(backup.status)} · ${Math.max(
        1,
        Math.round(Number(backup.database_bytes || 0) / 1024 / 1024),
      )} MB · ${Number(backup.transcript_count || 0).toLocaleString()} transcripts</span>` +
      (backup.last_error
        ? `<span class="backup-error">${escapeHTML(backup.last_error)}</span>`
        : "");
    const verify = document.createElement("button");
    verify.type = "button";
    verify.className = "btn btn-quiet";
    verify.textContent = "Verify";
    verify.addEventListener("click", () => phaseSevenVerifyBackup(backup.id, verify));
    row.append(summary, verify);
    list.appendChild(row);
  }
}

async function phaseSevenVerifyBackup(backupId, button) {
  button.disabled = true;
  const status = document.getElementById("backup-status");
  status.textContent = "Verifying the stored backup…";
  try {
    const result = await phaseSevenJson(`/api/backups/${backupId}/verify`, {
      method: "POST",
    });
    status.textContent = `Backup verified · ${Number(
      result.transcript_count || 0,
    ).toLocaleString()} transcripts checked`;
    showToast("Backup verified.", "success");
    await phaseSevenRefresh();
  } catch (error) {
    status.textContent = error.message;
    showToast(error.message, "danger");
  } finally {
    button.disabled = false;
  }
}

async function phaseSevenCreateBackup() {
  const button = document.getElementById("backup-create-button");
  button.disabled = true;
  document.getElementById("backup-status").textContent = "Creating and verifying backup…";
  try {
    await phaseSevenJson("/api/backups", { method: "POST" });
    await phaseSevenRefresh();
    showToast("Recovery backup created and verified.", "success");
  } catch (error) {
    document.getElementById("backup-status").textContent = error.message;
    showToast(error.message, "danger");
  } finally {
    button.disabled = false;
  }
}

async function phaseSevenRunDrill() {
  const backup = phaseSevenState.latestBackup;
  if (!backup) return;
  const button = document.getElementById("backup-drill-button");
  button.disabled = true;
  document.getElementById("backup-status").textContent = "Testing recovery in a temporary location…";
  try {
    const result = await phaseSevenJson(`/api/backups/${backup.id}/restore-drill`, {
      method: "POST",
    });
    document.getElementById("backup-status").textContent =
      `Restore drill passed · ${result.transcript_count.toLocaleString()} transcripts verified`;
    showToast("Restore drill passed without changing live data.", "success");
  } catch (error) {
    document.getElementById("backup-status").textContent = error.message;
    showToast(error.message, "danger");
  } finally {
    button.disabled = false;
  }
}

function phaseSevenBoot() {
  if (!currentProfile) {
    window.setTimeout(phaseSevenBoot, 100);
    return;
  }
  if (!currentProfile.permissions?.profiles) return;
  document.getElementById("recovery-control-card").hidden = false;
  phaseSevenRefresh().catch((error) => {
    document.getElementById("backup-status").textContent = error.message;
  });
}

document
  .getElementById("backup-create-button")
  .addEventListener("click", phaseSevenCreateBackup);
document
  .getElementById("backup-drill-button")
  .addEventListener("click", phaseSevenRunDrill);
phaseSevenBoot();
