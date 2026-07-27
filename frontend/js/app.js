// --- Sidebar ---
function readStoredJSON(key, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "null");
    return value ?? fallback;
  } catch {
    localStorage.removeItem(key);
    return fallback;
  }
}

const sidebarToggleButton = document.getElementById("sidebar-collapse-btn");

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  sidebarToggleButton.setAttribute("aria-expanded", String(!collapsed));
  sidebarToggleButton.setAttribute(
    "aria-label",
    collapsed ? "Expand sidebar" : "Collapse sidebar",
  );
  sidebarToggleButton.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
  localStorage.setItem("radioSidebarCollapsed", collapsed ? "1" : "0");
}

window.toggleSidebar = function () {
  setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
};

setSidebarCollapsed(localStorage.getItem("radioSidebarCollapsed") === "1");

// --- Time Selector Generator ---
function populateTimeFilters() {
  const startSel = document.getElementById("filter-start");
  const endSel = document.getElementById("filter-end");

  startSel.innerHTML = `<option value="">Start Time...</option>`;
  endSel.innerHTML = `<option value="">End Time...</option>`;

  for (let i = 0; i < 24; i++) {
    for (let j = 0; j < 60; j += 15) {
      const hh = i.toString().padStart(2, "0");
      const mm = j.toString().padStart(2, "0");
      const time24 = `${hh}:${mm}`;

      let ampm = i >= 12 ? "PM" : "AM";
      let h12 = i % 12 || 12; // Converts 0 to 12
      const time12 = `${h12}:${mm} ${ampm}`;

      const opt = `<option value="${time24}">${time12}</option>`;
      startSel.insertAdjacentHTML("beforeend", opt);
      endSel.insertAdjacentHTML("beforeend", opt);
    }
  }
}
populateTimeFilters();

// --- OS Notification Setup ---
const notificationToggle = document.getElementById("notification-toggle");
let notificationsEnabled =
  localStorage.getItem("radioEmergencyNotifications") === "1";
notificationToggle.checked =
  notificationsEnabled &&
  "Notification" in window &&
  Notification.permission === "granted";

function triggerDesktopNotification(channel, text) {
  if (
    notificationsEnabled &&
    "Notification" in window &&
    Notification.permission === "granted"
  ) {
    const notification = new Notification(`🚨 CRITICAL: ${channel}`, {
      body: text,
      icon: 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🚨</text></svg>',
    });
    notification.onclick = () => window.focus();
  }
}

// --- Core Configuration & State ---
const dashboard = document.getElementById("dashboard");
const togglesContainer = document.getElementById("channel-toggles");
const knownChannels = new Set();
const processedMessages = new Set();
const autoScrollState = {};
const unreadCounts = {};
let globalSearchQuery = "";
let dateTimeFilterActive = false;
let currentProfile = null;
let archiveSearchTimer = null;
let archiveRequestNumber = 0;
let lastSeenTranscriptId = 0;
let showSuspectTranscripts = false;
let bookmarksOnly = false;
const storedHiddenChannels = readStoredJSON("radioHiddenChannels", []);
const hiddenChannels = new Set(
  Array.isArray(storedHiddenChannels) ? storedHiddenChannels : [],
);
const storedColumnOrder = readStoredJSON("radioColumnOrder", []);
let savedColumnOrder = Array.isArray(storedColumnOrder) ? storedColumnOrder : [];

// Dynamic Keyword Setup
const DEFAULT_ALERT_KEYWORDS = [
  "medical",
  "medic",
  "paramedic",
  "ambulance",
  "patient",
  "casualty",
  "injury",
  "injured",
  "bleeding",
  "blood",
  "unconscious",
  "unresponsive",
  "overdose",
  "naloxone",
  "narcan",
  "seizure",
  "cardiac",
  "heart attack",
  "chest pain",
  "not breathing",
  "heatstroke",
  "heat exhaustion",
  "dehydration",
  "allergic reaction",
  "anaphylaxis",
  "police",
  "rcmp",
  "fire",
  "smoke",
  "flames",
  "explosion",
  "gas leak",
  "evacuate",
  "evacuation",
  "emergency",
  "urgent",
  "mayday",
  "sos",
  "security",
  "breach",
  "lockdown",
  "assault",
  "fight",
  "weapon",
  "gun",
  "knife",
  "threat",
  "missing person",
  "missing child",
  "lost child",
  "code red",
  "help",
];
const KEYWORD_SET_VERSION = 2;
const storedKeywords = readStoredJSON("radioKeywords", null);
const storedKeywordVersion = Number(
  localStorage.getItem("radioKeywordSetVersion") || "0",
);
let alertKeywords = Array.isArray(storedKeywords)
  ? storedKeywords
  : [...DEFAULT_ALERT_KEYWORDS];

if (storedKeywordVersion < KEYWORD_SET_VERSION) {
  alertKeywords = [...new Set([...alertKeywords, ...DEFAULT_ALERT_KEYWORDS])];
  localStorage.setItem("radioKeywordSetVersion", String(KEYWORD_SET_VERSION));
}
let keywordRegex;

function renderKeywords() {
  const container = document.getElementById("keyword-list");
  container.innerHTML = "";

  alertKeywords.forEach((kw) => {
    const span = document.createElement("span");
    span.className = "keyword-chip";
    span.title = "Click to remove";
    span.innerText = `${kw} ✕`;
    span.onclick = () => removeKeyword(kw);
    container.appendChild(span);
  });

  const validKeywords = alertKeywords.filter((k) => k.trim() !== "");
  const safeKeywords = validKeywords.map((kw) =>
    kw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
  );
  keywordRegex =
    safeKeywords.length > 0
      ? new RegExp(`\\b((?:${safeKeywords.join("|")})\\w*)`, "gi")
      : null;

  localStorage.setItem("radioKeywords", JSON.stringify(alertKeywords));
  document.getElementById("keyword-count").innerText = alertKeywords.length;
}

// TEXT HIGHLIGHTING ENGINE
function escapeHTML(str) {
  return str.replace(
    /[&<>'"]/g,
    (tag) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[
        tag
      ] || tag,
  );
}

function highlightText(text) {
  if (!keywordRegex) return escapeHTML(text);

  let result = "";
  let lastIndex = 0;

  text.replace(keywordRegex, (...args) => {
    const match = args[0];
    const offset = args[args.length - 2];
    result += escapeHTML(text.slice(lastIndex, offset));
    result += `<span class="keyword-highlight">${escapeHTML(match)}</span>`;
    lastIndex = offset + match.length;
    return match;
  });

  result += escapeHTML(text.slice(lastIndex));
  return result;
}

window.rehighlightAllMessages = function () {
  document.querySelectorAll(".message-card").forEach((card) => {
    const contentDiv = card.querySelector(".transcript-content");
    if (contentDiv) {
      const rawText = decodeURIComponent(contentDiv.getAttribute("data-clean"));
      contentDiv.innerHTML = highlightText(rawText);
      card.classList.toggle("has-alert", hasAlertKeyword(rawText));
    }
  });
};

window.addKeyword = function (e) {
  if (e.key === "Enter" && e.target.value.trim() !== "") {
    const kw = e.target.value.trim().toLowerCase();
    if (!alertKeywords.includes(kw)) {
      alertKeywords.push(kw);
      renderKeywords();
      rehighlightAllMessages();
      showToast(`Keyword added: ${kw}`, "success");
    }
    e.target.value = "";
  }
};

window.removeKeyword = function (kw) {
  alertKeywords = alertKeywords.filter((k) => k !== kw);
  renderKeywords();
  rehighlightAllMessages();
  showToast(`Keyword removed: ${kw}`);
};

renderKeywords();

// --- Utility Functions ---
function showToast(message, type = "default") {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast ${type !== "default" ? "toast-" + type : ""}`;
  toast.innerText = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.animation = "toastFadeOut 0.3s forwards";
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}

function getChannelName(filename) {
  if (filename.includes("/")) return filename.split("/")[0];
  const match = filename.match(
    /^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-(.+)\.mp3$/i,
  );
  if (match) return match[1];
  return filename.replace(".mp3", "");
}

function getColumnId(channelName) {
  let hash = 2166136261;
  for (const character of channelName) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  const readable = channelName.replace(/[^a-zA-Z0-9]/g, "-");
  return `col-${readable}-${(hash >>> 0).toString(36)}`;
}

function saveColumnPreferences() {
  const order = [...dashboard.querySelectorAll(".channel-column")].map((column) =>
    column.getAttribute("data-channel"),
  );
  localStorage.setItem("radioColumnOrder", JSON.stringify(order));
  localStorage.setItem(
    "radioHiddenChannels",
    JSON.stringify([...hiddenChannels]),
  );
}

function restoreColumnOrder() {
  const positions = new Map(savedColumnOrder.map((name, index) => [name, index]));
  const columns = [...dashboard.querySelectorAll(".channel-column")];
  columns
    .sort((left, right) => {
      const leftPosition = positions.has(left.dataset.channel)
        ? positions.get(left.dataset.channel)
        : Number.MAX_SAFE_INTEGER;
      const rightPosition = positions.has(right.dataset.channel)
        ? positions.get(right.dataset.channel)
        : Number.MAX_SAFE_INTEGER;
      return leftPosition - rightPosition;
    })
    .forEach((column) => dashboard.appendChild(column));
}

function hasMeaningfulTranscript(text) {
  const withoutMarkers = String(text || "").replace(
    /[\[(](?:blank[_ ]audio|silence|music|no speech|inaudible)[\])]/gi,
    "",
  );
  return /[\p{L}\p{N}]/u.test(withoutMarkers);
}

function hasAlertKeyword(text) {
  if (!keywordRegex) return false;
  keywordRegex.lastIndex = 0;
  const found = keywordRegex.test(text);
  keywordRegex.lastIndex = 0;
  return found;
}

function extractTimeFromFilename(filename, fallbackTimestamp) {
  const fullMatch = filename.match(
    /(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})/,
  );
  if (fullMatch) {
    const [_, y, m, d, h, min, s] = fullMatch.map(Number);
    const dateObj = new Date(y, m - 1, d, h, min, s);
    if (!isNaN(dateObj)) return dateObj;
  }
  const fallback = fallbackTimestamp ? new Date(fallbackTimestamp) : new Date();
  return isNaN(fallback) ? new Date() : fallback;
}

// --- Column Builder ---
function createColumn(channelName, announce = true) {
  const colId = getColumnId(channelName);
  unreadCounts[colId] = 0;
  const isVisible = !hiddenChannels.has(channelName);
  const safeChannelName = escapeHTML(channelName);

  const toggleWrapper = document.createElement("label");
  toggleWrapper.className = "checkbox-label";
  toggleWrapper.innerHTML = `
      <div class="checkbox-inner">
          <input type="checkbox" id="toggle-${colId}" data-channel-id="${colId}" ${isVisible ? "checked" : ""}>
          ${safeChannelName}
      </div>
      <span class="unread-badge" id="badge-${colId}">0</span>
  `;
  togglesContainer.appendChild(toggleWrapper);

  const col = document.createElement("div");
  col.className = "channel-column";
  col.id = colId;
  col.setAttribute("data-channel", channelName);
  col.classList.toggle("channel-user-hidden", !isVisible);
  autoScrollState[colId] = true;

  col.innerHTML = `
      <div class="col-header">
          <div class="col-title-row">
              <div class="col-title"><span class="col-name">${safeChannelName}</span> <span class="channel-count" id="count-${colId}">0</span></div>
              <div class="col-actions">
                  <button class="arrow-btn" data-action="move-column" data-column-id="${colId}" data-direction="left" title="Move left" aria-label="Move ${safeChannelName} left">◀</button>
                  <button class="arrow-btn" data-action="move-column" data-column-id="${colId}" data-direction="right" title="Move right" aria-label="Move ${safeChannelName} right">▶</button>
                  <button class="scroll-toggle active" data-action="toggle-auto-scroll" data-column-id="${colId}">Auto-Scroll</button>
              </div>
          </div>
      </div>
      <div class="messages-container" id="msgs-${colId}"></div>
  `;

  dashboard.appendChild(col);
  restoreColumnOrder();
  if (announce) showToast(`New Channel Connected: ${channelName}`);
  return document.getElementById(`msgs-${colId}`);
}

function moveColumn(colId, direction) {
  const col = document.getElementById(colId);
  if (!col) return;

  if (direction === "left") {
    const prev = col.previousElementSibling;
    if (prev && prev.classList.contains("channel-column")) {
      dashboard.insertBefore(col, prev);
    }
  } else if (direction === "right") {
    const next = col.nextElementSibling;
    if (next && next.classList.contains("channel-column")) {
      dashboard.insertBefore(next, col);
    }
  }
  savedColumnOrder = [...dashboard.querySelectorAll(".channel-column")].map(
    (column) => column.dataset.channel,
  );
  saveColumnPreferences();
}

function updatePlayState(audio, state) {
  const btn = audio.parentElement.querySelector(".play-btn");
  if (!btn) return;
  if (state === "playing") {
    btn.innerHTML = "⏸";
    btn.style.paddingLeft = "0";
  } else if (state === "paused") {
    btn.innerHTML = "▶";
    btn.style.paddingLeft = "2px";
  } else if (state === "waiting") {
    btn.innerHTML = "⏳";
    btn.style.paddingLeft = "0";
  }
}

function togglePlay(btn) {
  const audio = btn.parentElement.querySelector("audio");
  if (audio.paused) {
    document.querySelectorAll("audio").forEach((a) => {
      if (a !== audio && !a.paused) a.pause();
    });
    updatePlayState(audio, "waiting");
    audio.play().catch((e) => {
      if (e.name !== "AbortError") {
        console.error("Playback failed", e);
        showToast("Audio unavailable or missing.", "danger");
        updatePlayState(audio, "paused");
      }
    });
  } else {
    updatePlayState(audio, "paused");
    audio.pause();
  }
}

function updateProgress(audio) {
  const container = audio.parentElement;
  const fill = container.querySelector(".progress-fill");
  const timeDisplay = container.querySelector(".time-display");
  let percent = 0;
  if (audio.duration && !isNaN(audio.duration)) {
    percent = (audio.currentTime / audio.duration) * 100;
  }
  fill.style.width = `${percent}%`;
  timeDisplay.innerText = formatTime(audio.currentTime);
}

function seekAudio(event, container) {
  const audio = container.parentElement.querySelector("audio");
  if (!audio.duration || isNaN(audio.duration)) return;
  const rect = container.getBoundingClientRect();
  const pos = (event.clientX - rect.left) / rect.width;
  audio.currentTime = pos * audio.duration;
}

function resetPlayer(audio) {
  updatePlayState(audio, "paused");
  audio.parentElement.querySelector(".progress-fill").style.width = "0%";
  audio.parentElement.querySelector(".time-display").innerText = "0:00";
}

function formatTime(seconds) {
  if (isNaN(seconds) || !isFinite(seconds)) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60)
    .toString()
    .padStart(2, "0");
  return `${m}:${s}`;
}

function createMessageCard(data) {
  const card = document.createElement("div");
  card.className = "message-card";
  if (data.status === "suspect") card.classList.add("is-suspect");
  if (data.reviewed) card.classList.add("is-reviewed");
  if (data.bookmarked) card.classList.add("is-bookmarked");
  if (data.id) {
    card.dataset.transcriptId = String(data.id);
    lastSeenTranscriptId = Math.max(lastSeenTranscriptId, Number(data.id) || 0);
  }
  const realTime = extractTimeFromFilename(data.filename, data.timestamp);
  const hours = realTime.getHours().toString().padStart(2, "0");
  const mins = realTime.getMinutes().toString().padStart(2, "0");
  card.setAttribute("data-time", `${hours}:${mins}`);
  card.setAttribute("data-recorded-at", realTime.getTime().toString());
  const timeStr = realTime.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const dateStr = realTime.toLocaleDateString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
  const isoDate = [
    realTime.getFullYear(),
    String(realTime.getMonth() + 1).padStart(2, "0"),
    String(realTime.getDate()).padStart(2, "0"),
  ].join("-");
  card.setAttribute("data-date", isoDate);

  let cleanText = data.transcript_text.replace(
    /\[\d+\.\d+s -> \d+\.\d+s\]\s*/g,
    "",
  );
  if (hasAlertKeyword(cleanText)) card.classList.add("has-alert");
  card.setAttribute(
    "data-search",
    `${getChannelName(data.filename)} ${cleanText} ${dateStr} ${isoDate} ${realTime.toLocaleDateString()}`.toLowerCase(),
  );
  const encodedCleanText = encodeURIComponent(cleanText);
  const safeRawText = cleanText.toLowerCase().replace(/"/g, "&quot;");
  const safeAudioUrl =
    "/audio/" + data.filename.split("/").map(encodeURIComponent).join("/");
  const canAudio = currentProfile?.permissions?.audio;
  const canReview = currentProfile?.permissions?.review;
  const canCorrect = currentProfile?.permissions?.correct;
  const qualityBadge =
    data.status === "suspect"
      ? `<span class="quality-badge" title="${escapeHTML(data.quality_reason || "Suspicious transcript pattern")}">Needs review</span>`
      : "";
  const correctionBadge = data.corrected_by
    ? `<span class="correction-badge">Corrected by ${escapeHTML(data.corrected_by)}</span>`
    : "";
  const actionButtons = canReview
    ? `
      <div class="message-actions">
        <button class="card-action ${data.reviewed ? "active" : ""}" data-action="review-transcript" title="Mark reviewed">✓ Reviewed</button>
        <button class="card-action ${data.bookmarked ? "active" : ""}" data-action="bookmark-transcript" title="Bookmark transmission">★ Bookmark</button>
        <button class="card-action" data-action="note-transcript" title="Add an operator note">Note</button>
        ${canCorrect ? '<button class="card-action" data-action="correct-transcript" title="Correct transcript">Correct</button>' : ""}
      </div>`
    : "";
  const audioPlayer = canAudio
    ? `
      <div class="custom-audio-player">
          <button class="play-btn" data-action="toggle-play" aria-label="Play recording">▶</button>
          <div class="progress-bar-container" data-action="seek-audio">
              <div class="progress-fill"></div>
          </div>
          <div class="time-display">0:00</div>
          <audio src="${safeAudioUrl}" preload="none"></audio>
      </div>`
    : '<div class="audio-restricted">Audio requires operator clearance</div>';

  card.innerHTML = `
      <div class="msg-meta">
          <div class="msg-datetime"><span class="msg-date">${dateStr}</span><span class="msg-time">${timeStr}</span></div>
          <div>${qualityBadge}${correctionBadge}${canAudio ? `<a href="${safeAudioUrl}" target="_blank" class="audio-link" title="Download Source File">💾 Source</a>` : ""}</div>
      </div>
      <div class="transcript-content" data-clean="${encodedCleanText}" data-raw="${safeRawText}">
          ${highlightText(cleanText)}
      </div>
      ${data.notes ? `<div class="operator-note">${escapeHTML(data.notes)}</div>` : ""}
      ${actionButtons}
      ${audioPlayer}
  `;
  return card;
}

function insertCardChronologically(container, card) {
  const recordedAt = Number(card.getAttribute("data-recorded-at"));
  if (!Number.isFinite(recordedAt)) {
    container.appendChild(card);
    return true;
  }

  const existingCards = container.querySelectorAll(".message-card");
  for (const existingCard of existingCards) {
    const existingRecordedAt = Number(
      existingCard.getAttribute("data-recorded-at"),
    );
    if (Number.isFinite(existingRecordedAt) && existingRecordedAt > recordedAt) {
      container.insertBefore(card, existingCard);
      return false;
    }
  }

  container.appendChild(card);
  return true;
}

function toggleColumn(colId, isChecked) {
  const col = document.getElementById(colId);
  const badge = document.getElementById(`badge-${colId}`);
  col.classList.toggle("channel-user-hidden", !isChecked);
  const channelName = col.getAttribute("data-channel");
  if (isChecked) hiddenChannels.delete(channelName);
  else hiddenChannels.add(channelName);
  saveColumnPreferences();
  if (isChecked) {
    unreadCounts[colId] = 0;
    badge.style.display = "none";
    badge.innerText = "0";
  }
  updateSearchResults();
}

function toggleAutoScroll(colId, btn) {
  autoScrollState[colId] = !autoScrollState[colId];
  btn.classList.toggle("active", autoScrollState[colId]);
}

function timeToMinutes(time) {
  if (!time) return 0;
  const [hours, minutes] = time.split(":").map(Number);
  return hours * 60 + minutes;
}

function matchesActiveDateTimeFilter(card) {
  if (!dateTimeFilterActive) return true;

  const selectedDate = document.getElementById("filter-date").value;
  const start = document.getElementById("filter-start").value;
  const end = document.getElementById("filter-end").value;
  const cardDate = card.getAttribute("data-date");
  const messageMinutes = timeToMinutes(card.getAttribute("data-time"));

  if (selectedDate && cardDate !== selectedDate) return false;
  if (start && end) {
    const startMinutes = timeToMinutes(start);
    const endMinutes = timeToMinutes(end);
    return startMinutes <= endMinutes
      ? messageMinutes >= startMinutes && messageMinutes <= endMinutes
      : messageMinutes >= startMinutes || messageMinutes <= endMinutes;
  }
  if (start && messageMinutes < timeToMinutes(start)) return false;
  if (end && messageMinutes > timeToMinutes(end)) return false;
  return true;
}

function updateSearchResults() {
  let totalMessages = 0;
  let visibleMessages = 0;
  let visibleFeeds = 0;
  const filtering = Boolean(globalSearchQuery) || dateTimeFilterActive;

  document.querySelectorAll(".channel-column").forEach((column) => {
    const cards = [...column.querySelectorAll(".message-card")];
    let columnMatches = 0;
    const userHidden = column.classList.contains("channel-user-hidden");

    cards.forEach((card) => {
      card.classList.toggle(
        "hidden-time",
        !matchesActiveDateTimeFilter(card),
      );
      const matches =
        !globalSearchQuery ||
        card.getAttribute("data-search").includes(globalSearchQuery);
      card.classList.toggle("hidden-search", !matches);
      if (!userHidden) totalMessages += 1;
      if (!userHidden && matches && !card.classList.contains("hidden-time")) {
        columnMatches += 1;
        visibleMessages += 1;
      }
    });

    column.classList.toggle(
      "hidden-search-column",
      filtering && columnMatches === 0,
    );
    if (!userHidden && (columnMatches > 0 || !filtering)) visibleFeeds += 1;

    const count = column.querySelector(".channel-count");
    if (count) count.innerText = filtering ? `${columnMatches}/${cards.length}` : cards.length;
  });

  const status = document.getElementById("search-status");
  status.innerText = filtering
    ? `${visibleMessages} result${visibleMessages === 1 ? "" : "s"} across ${visibleFeeds} feed${visibleFeeds === 1 ? "" : "s"}`
    : `${totalMessages} transmissions across ${visibleFeeds} feeds`;

  document.getElementById("clear-global-search").hidden = !globalSearchQuery;
  document.getElementById("empty-state").hidden = visibleMessages > 0 || !filtering;
}

function clearRenderedTranscripts() {
  dashboard.innerHTML = "";
  togglesContainer.innerHTML = "";
  knownChannels.clear();
  processedMessages.clear();
  Object.keys(unreadCounts).forEach((key) => delete unreadCounts[key]);
  Object.keys(autoScrollState).forEach((key) => delete autoScrollState[key]);
}

function archiveQueryParameters(options = {}) {
  const parameters = new URLSearchParams();
  if (globalSearchQuery) parameters.set("q", globalSearchQuery);
  if (dateTimeFilterActive) {
    const date = document.getElementById("filter-date").value;
    const start = document.getElementById("filter-start").value;
    const end = document.getElementById("filter-end").value;
    if (date) parameters.set("date", date);
    if (start) parameters.set("start", start);
    if (end) parameters.set("end", end);
  }
  if (showSuspectTranscripts) parameters.set("include_suspect", "true");
  if (bookmarksOnly) parameters.set("bookmarked", "true");
  if (options.afterId) parameters.set("after_id", String(options.afterId));
  parameters.set("limit", options.afterId ? "2000" : "1000");
  return parameters;
}

async function loadArchive(options = {}) {
  const requestNumber = ++archiveRequestNumber;
  const status = document.getElementById("search-status");
  if (!options.afterId) status.innerText = "Searching the complete archive…";
  const response = await fetch(`/api/history?${archiveQueryParameters(options)}`, {
    cache: "no-store",
  });
  if (response.status === 401) {
    window.location.replace("/login");
    return;
  }
  if (!response.ok) throw new Error("Archive search unavailable");
  const rows = await response.json();
  if (requestNumber !== archiveRequestNumber && !options.afterId) return;
  if (!options.afterId) clearRenderedTranscripts();
  rows.forEach((item) =>
    processIncomingData(item, { announce: false, notify: false }),
  );
  updateSearchResults();
  requestAnimationFrame(scrollAllAutoColumnsToLatest);
}

function scheduleArchiveSearch() {
  clearTimeout(archiveSearchTimer);
  archiveSearchTimer = setTimeout(() => {
    loadArchive().catch(() =>
      showToast("Archive search is temporarily unavailable.", "danger"),
    );
  }, 250);
}

window.filterAllFeeds = function (query) {
  globalSearchQuery = query.trim().toLowerCase();
  updateSearchResults();
  scheduleArchiveSearch();
};

window.clearGlobalSearch = function () {
  const input = document.getElementById("global-search");
  input.value = "";
  globalSearchQuery = "";
  updateSearchResults();
  scheduleArchiveSearch();
  input.focus();
};

window.applyDateTimeFilter = function () {
  const selectedDate = document.getElementById("filter-date").value;
  const start = document.getElementById("filter-start").value;
  const end = document.getElementById("filter-end").value;

  if (!selectedDate && !start && !end) {
    clearDateTimeFilter();
    return;
  }
  dateTimeFilterActive = true;
  scheduleArchiveSearch();

  const startLabel =
    document.getElementById("filter-start").options[
      document.getElementById("filter-start").selectedIndex
    ].text;
  const endLabel =
    document.getElementById("filter-end").options[
      document.getElementById("filter-end").selectedIndex
    ].text;
  const parts = [];
  if (selectedDate) parts.push(selectedDate);
  if (start || end) parts.push(`${start ? startLabel : "Start of day"} – ${end ? endLabel : "End of day"}`);
  showToast(`Filter applied: ${parts.join(", ")}`);
};

window.clearDateTimeFilter = function () {
  dateTimeFilterActive = false;
  document.getElementById("filter-date").value = "";
  document.getElementById("filter-start").value = "";
  document.getElementById("filter-end").value = "";
  document
    .querySelectorAll(".message-card")
    .forEach((card) => card.classList.remove("hidden-time"));
  scheduleArchiveSearch();
  showToast("Date and time filter cleared");
};

window.exportCSV = function () {
  if (!currentProfile?.permissions?.export) {
    showToast("Supervisor clearance is required for exports.", "danger");
    return;
  }
  const parameters = archiveQueryParameters();
  parameters.delete("include_suspect");
  parameters.delete("limit");
  window.location.assign(`/api/export.csv?${parameters}`);
  showToast("Preparing complete archive export…", "success");
};

function processIncomingData(data, options = {}) {
  if (!hasMeaningfulTranscript(data.transcript_text)) return;
  if (processedMessages.has(data.filename)) return;
  processedMessages.add(data.filename);

  const channelName = getChannelName(data.filename);

  if (!knownChannels.has(channelName)) {
    knownChannels.add(channelName);
    createColumn(channelName, options.announce !== false);
  }

  const colId = getColumnId(channelName);
  const container = document.getElementById(`msgs-${colId}`);
  const card = createMessageCard(data);
  const insertedAtEnd = insertCardChronologically(container, card);

  if (options.notify !== false && hasAlertKeyword(data.transcript_text)) {
    triggerDesktopNotification(channelName, data.transcript_text);
  }

  const colElement = document.getElementById(colId);
  if (colElement.classList.contains("channel-user-hidden")) {
    unreadCounts[colId]++;
    const badge = document.getElementById(`badge-${colId}`);
    badge.innerText = unreadCounts[colId];
    badge.style.display = "inline-block";
    badge.style.transform = "scale(1.2)";
    setTimeout(() => (badge.style.transform = "scale(1)"), 150);
  }

  if (autoScrollState[colId] && insertedAtEnd) {
    container.scrollTop = container.scrollHeight;
  }
  updateSearchResults();
}

function scrollAllAutoColumnsToLatest() {
  document.querySelectorAll(".messages-container").forEach((container) => {
    const columnId = container.id.replace(/^msgs-/, "");
    if (autoScrollState[columnId]) {
      container.scrollTop = container.scrollHeight;
    }
  });
}

function formatMetric(value) {
  return new Intl.NumberFormat([], { notation: "compact" }).format(value || 0);
}

function formatEta(minutes) {
  if (minutes == null) return "—";
  if (minutes < 60) return `${Math.max(1, Math.round(minutes))}m`;
  const hours = minutes / 60;
  return hours < 24 ? `${hours.toFixed(1)}h` : `${(hours / 24).toFixed(1)}d`;
}

async function refreshSystemStats() {
  try {
    const response = await fetch("/api/stats", { cache: "no-store" });
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    if (!response.ok) throw new Error("Stats unavailable");
    const stats = await response.json();
    document.getElementById("metric-backlog").innerText = formatMetric(stats.backlog);
    document.getElementById("metric-rate").innerText = stats.rate_per_minute || "—";
    document.getElementById("metric-eta").innerText = formatEta(stats.eta_minutes);
    document.getElementById("active-operators").innerText =
      `${stats.active_clients} operator${stats.active_clients === 1 ? "" : "s"}`;
    const engineLabel = stats.engine === "mlx" ? "MLX / Apple GPU" : "CPU fallback";
    document.getElementById("engine-status").innerText =
      `${stats.model[0].toUpperCase()}${stats.model.slice(1)} model · ${engineLabel} · ${formatMetric(stats.processed)} complete · ${formatMetric(stats.suspect)} suspect`;
    const serviceStatus = document.getElementById("service-status");
    const unavailable = Object.entries(stats.services || {})
      .filter(([, service]) => service.stale || !["online", "idle"].includes(service.status))
      .map(([name]) => name);
    serviceStatus.innerText =
      stats.status === "online"
        ? `Workers healthy · ${formatMetric(stats.pending_delivery)} pending delivery`
        : `Attention: ${unavailable.join(", ") || "worker status unavailable"}`;
    serviceStatus.classList.toggle("service-status-warning", stats.status !== "online");
  } catch {
    document.getElementById("engine-status").innerText = "System metrics temporarily unavailable";
    document.getElementById("service-status").innerText = "Worker status unavailable";
  }
}

async function updateNotificationPreference(enabled) {
  if (!enabled) {
    notificationsEnabled = false;
    localStorage.setItem("radioEmergencyNotifications", "0");
    return;
  }
  if (!("Notification" in window)) {
    notificationToggle.checked = false;
    showToast("Notifications are not supported by this browser.", "danger");
    return;
  }
  const permission = await Notification.requestPermission();
  notificationsEnabled = permission === "granted";
  notificationToggle.checked = notificationsEnabled;
  localStorage.setItem("radioEmergencyNotifications", notificationsEnabled ? "1" : "0");
  showToast(
    notificationsEnabled ? "Emergency notifications enabled." : "Notification permission was not granted.",
    notificationsEnabled ? "success" : "danger",
  );
}

async function updateTranscriptCard(card, changes) {
  const transcriptId = card.dataset.transcriptId;
  if (!transcriptId) return;
  const response = await fetch(`/api/transcripts/${transcriptId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "Update failed");
  const replacement = createMessageCard(payload);
  card.replaceWith(replacement);
  updateSearchResults();
}

async function handleTranscriptAction(control) {
  const card = control.closest(".message-card");
  const action = control.dataset.action;
  try {
    if (action === "review-transcript") {
      await updateTranscriptCard(card, {
        reviewed: !card.classList.contains("is-reviewed"),
      });
    } else if (action === "bookmark-transcript") {
      await updateTranscriptCard(card, {
        bookmarked: !card.classList.contains("is-bookmarked"),
      });
    } else if (action === "note-transcript") {
      const existing = card.querySelector(".operator-note")?.innerText || "";
      const notes = window.prompt("Operator note", existing);
      if (notes !== null) await updateTranscriptCard(card, { notes });
    } else if (action === "correct-transcript") {
      const existing = card.querySelector(".transcript-content")?.innerText || "";
      const transcriptText = window.prompt("Corrected transcript", existing);
      if (transcriptText?.trim()) {
        await updateTranscriptCard(card, { transcript_text: transcriptText.trim() });
      }
    }
  } catch (error) {
    showToast(error.message, "danger");
  }
}

async function loadProfiles() {
  if (!currentProfile?.permissions?.profiles) return;
  const response = await fetch("/api/users", { cache: "no-store" });
  if (!response.ok) throw new Error("Profiles unavailable");
  const profiles = await response.json();
  const list = document.getElementById("profile-list");
  list.innerHTML = "";
  profiles.forEach((profile) => {
    const button = document.createElement("button");
    button.className = "profile-row";
    button.type = "button";
    button.innerHTML = `<strong>${escapeHTML(profile.display_name)}</strong><span>${escapeHTML(profile.username)} · ${escapeHTML(profile.role)}${profile.active ? "" : " · disabled"}</span>`;
    button.addEventListener("click", () => {
      document.getElementById("profile-username").value = profile.username;
      document.getElementById("profile-display-name").value = profile.display_name;
      document.getElementById("profile-password").value = "";
      document.getElementById("profile-role").value = profile.role;
      document.getElementById("profile-active").checked = profile.active;
    });
    list.appendChild(button);
  });
}

async function saveProfile() {
  const payload = {
    username: document.getElementById("profile-username").value.trim(),
    display_name: document.getElementById("profile-display-name").value.trim(),
    password: document.getElementById("profile-password").value || null,
    role: document.getElementById("profile-role").value,
    active: document.getElementById("profile-active").checked,
  };
  const response = await fetch("/api/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "Could not save profile");
  document.getElementById("profile-password").value = "";
  showToast(`Saved ${result.display_name}`, "success");
  await loadProfiles();
}

async function loadCurrentProfile() {
  const response = await fetch("/api/me", { cache: "no-store" });
  if (response.status === 401) {
    window.location.replace("/login");
    return;
  }
  if (!response.ok) throw new Error("Profile unavailable");
  currentProfile = await response.json();
  document.getElementById("current-profile").innerText =
    `${currentProfile.display_name} · ${currentProfile.role}`;
  document.getElementById("export-csv").hidden =
    !currentProfile.permissions.export;
  document.getElementById("suspect-toggle-row").hidden =
    !currentProfile.permissions.suspect;
  document.getElementById("profile-admin-section").hidden =
    !currentProfile.permissions.profiles;
  if (currentProfile.permissions.profiles) await loadProfiles();
}

const compactModeToggle = document.getElementById("compact-mode-toggle");
compactModeToggle.checked = localStorage.getItem("radioCompactMode") === "1";
document.body.classList.toggle("compact-mode", compactModeToggle.checked);

sidebarToggleButton.addEventListener("click", toggleSidebar);
document.getElementById("global-search").addEventListener("input", (event) =>
  filterAllFeeds(event.target.value),
);
document.getElementById("clear-global-search").addEventListener("click", clearGlobalSearch);
document.getElementById("new-keyword").addEventListener("keypress", addKeyword);
document.getElementById("apply-date-filter").addEventListener("click", applyDateTimeFilter);
document.getElementById("clear-date-filter").addEventListener("click", clearDateTimeFilter);
document.getElementById("export-csv").addEventListener("click", exportCSV);
document.getElementById("suspect-toggle").addEventListener("change", (event) => {
  showSuspectTranscripts = event.target.checked;
  scheduleArchiveSearch();
});
document.getElementById("bookmarks-only-toggle").addEventListener("change", (event) => {
  bookmarksOnly = event.target.checked;
  scheduleArchiveSearch();
});
document.getElementById("save-profile").addEventListener("click", () => {
  saveProfile().catch((error) => showToast(error.message, "danger"));
});
compactModeToggle.addEventListener("change", (event) => {
  document.body.classList.toggle("compact-mode", event.target.checked);
  localStorage.setItem("radioCompactMode", event.target.checked ? "1" : "0");
});
notificationToggle.addEventListener("change", (event) =>
  updateNotificationPreference(event.target.checked),
);

document.getElementById("logout-button").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  window.location.replace("/login");
});

document.addEventListener("change", (event) => {
  if (event.target.matches("input[data-channel-id]")) {
    toggleColumn(event.target.dataset.channelId, event.target.checked);
  }
});

document.addEventListener("click", (event) => {
  const control = event.target.closest("[data-action]");
  if (!control) return;
  const action = control.dataset.action;
  if (action === "move-column") {
    moveColumn(control.dataset.columnId, control.dataset.direction);
  } else if (action === "toggle-auto-scroll") {
    toggleAutoScroll(control.dataset.columnId, control);
  } else if (action === "toggle-play") {
    togglePlay(control);
  } else if (action === "seek-audio") {
    seekAudio(event, control);
  } else if (
    [
      "review-transcript",
      "bookmark-transcript",
      "note-transcript",
      "correct-transcript",
    ].includes(action)
  ) {
    handleTranscriptAction(control);
  }
});

for (const eventName of ["timeupdate", "ended", "play", "playing", "pause"]) {
  document.addEventListener(
    eventName,
    (event) => {
      if (event.target.tagName !== "AUDIO") return;
      if (eventName === "timeupdate") updateProgress(event.target);
      else if (eventName === "ended") resetPlayer(event.target);
      else if (eventName === "play") updatePlayState(event.target, "waiting");
      else if (eventName === "playing") updatePlayState(event.target, "playing");
      else if (eventName === "pause") updatePlayState(event.target, "paused");
    },
    true,
  );
}

document.addEventListener("keydown", (event) => {
  const searchInput = document.getElementById("global-search");
  const isTyping = ["INPUT", "SELECT", "TEXTAREA"].includes(
    document.activeElement?.tagName,
  );

  if (event.key === "/" && !isTyping) {
    event.preventDefault();
    searchInput.focus();
  } else if (event.key === "Escape" && document.activeElement === searchInput) {
    clearGlobalSearch();
    searchInput.blur();
  }
});

let ws;
let reconnectTimer;
let reconnectAttempt = 0;

function setConnectionState(state) {
  const label = document.getElementById("connection-label");
  const dot = document.getElementById("connection-dot");
  label.innerText =
    state === "online"
      ? "Secure & online"
      : state === "offline"
        ? "Reconnecting…"
        : "Connecting securely…";
  dot.classList.toggle("status-dot-offline", state === "offline");
}

function connectWebSocket() {
  clearTimeout(reconnectTimer);
  if (ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(ws.readyState)) return;
  setConnectionState("connecting");
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

  ws.onmessage = (event) => processIncomingData(JSON.parse(event.data));
  ws.onopen = () => {
    const wasReconnecting = reconnectAttempt > 0;
    reconnectAttempt = 0;
    setConnectionState("online");
    if (wasReconnecting) {
      loadArchive({ afterId: lastSeenTranscriptId }).catch(() =>
        showToast("Live connection restored; archive catch-up will retry.", "danger"),
      );
      showToast("Live connection restored", "success");
    }
  };
  ws.onclose = (event) => {
    if (event.code === 4401) {
      window.location.replace("/login");
      return;
    }
    setConnectionState("offline");
    const delay = Math.min(30000, 2000 * 2 ** Math.min(reconnectAttempt, 4));
    reconnectAttempt += 1;
    if (reconnectAttempt === 1) {
      showToast("Connection lost. Reconnecting automatically…", "danger");
    }
    reconnectTimer = setTimeout(connectWebSocket, delay);
  };
  ws.onerror = () => ws.close();
}

loadCurrentProfile()
  .then(() => loadArchive())
  .catch((error) => {
    console.error(error);
    showToast("Could not load the dashboard archive.", "danger");
  })
  .finally(() => {
    updateSearchResults();
    connectWebSocket();
  });

refreshSystemStats();
setInterval(refreshSystemStats, 15000);
