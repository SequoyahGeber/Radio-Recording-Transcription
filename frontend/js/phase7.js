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
