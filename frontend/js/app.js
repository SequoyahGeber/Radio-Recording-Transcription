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
if ("Notification" in window) {
  Notification.requestPermission();
}

function triggerDesktopNotification(channel, text) {
  if (Notification.permission === "granted") {
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

// Dynamic Keyword Setup
let alertKeywords = JSON.parse(localStorage.getItem("radioKeywords")) || [
  "medical",
  "police",
  "fire",
  "emergency",
  "security",
  "breach",
  "injury",
  "help",
];
let keywordRegex;

function renderKeywords() {
  const container = document.getElementById("keyword-list");
  container.innerHTML = "";

  alertKeywords.forEach((kw) => {
    const span = document.createElement("span");
    span.className = "keyword-alert";
    span.style.cursor = "pointer";
    span.style.fontSize = "0.75rem";
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
    result += `<span class="keyword-alert">${escapeHTML(match)}</span>`;
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
function createColumn(channelName) {
  const colId = `col-${channelName.replace(/[^a-zA-Z0-9]/g, "-")}`;
  unreadCounts[colId] = 0;

  const toggleWrapper = document.createElement("label");
  toggleWrapper.className = "checkbox-label";
  toggleWrapper.innerHTML = `
      <div class="checkbox-inner">
          <input type="checkbox" id="toggle-${colId}" checked onchange="toggleColumn('${colId}', this.checked)"> 
          ${channelName}
      </div>
      <span class="unread-badge" id="badge-${colId}">0</span>
  `;
  togglesContainer.appendChild(toggleWrapper);

  const col = document.createElement("div");
  col.className = "channel-column";
  col.id = colId;
  autoScrollState[colId] = true;

  col.innerHTML = `
      <div class="col-header">
          <div class="col-title-row">
              <div class="col-title">${channelName}</div>
              <div class="col-actions">
                  <button class="arrow-btn" onclick="moveColumn('${colId}', 'left')" title="Move Left">◀</button>
                  <button class="arrow-btn" onclick="moveColumn('${colId}', 'right')" title="Move Right">▶</button>
                  <button class="scroll-toggle active" onclick="toggleAutoScroll('${colId}', this)">Auto-Scroll</button>
              </div>
          </div>
          <input type="text" class="search-input" style="background: rgba(0,0,0,0.2); margin:0;" placeholder="Filter this feed..." onkeyup="filterMessages('${colId}', this.value)">
      </div>
      <div class="messages-container" id="msgs-${colId}"></div>
  `;

  dashboard.appendChild(col);
  showToast(`New Channel Connected: ${channelName}`);
  return document.getElementById(`msgs-${colId}`);
}

window.moveColumn = function (colId, direction) {
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
};

window.updatePlayState = function (audio, state) {
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
};

window.togglePlay = function (btn) {
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
};

window.updateProgress = function (audio) {
  const container = audio.parentElement;
  const fill = container.querySelector(".progress-fill");
  const timeDisplay = container.querySelector(".time-display");
  let percent = 0;
  if (audio.duration && !isNaN(audio.duration)) {
    percent = (audio.currentTime / audio.duration) * 100;
  }
  fill.style.width = `${percent}%`;
  timeDisplay.innerText = formatTime(audio.currentTime);
};

window.seekAudio = function (e, container) {
  const audio = container.parentElement.querySelector("audio");
  if (!audio.duration || isNaN(audio.duration)) return;
  const rect = container.getBoundingClientRect();
  const pos = (e.clientX - rect.left) / rect.width;
  audio.currentTime = pos * audio.duration;
};

window.resetPlayer = function (audio) {
  updatePlayState(audio, "paused");
  audio.parentElement.querySelector(".progress-fill").style.width = "0%";
  audio.parentElement.querySelector(".time-display").innerText = "0:00";
};

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
  const realTime = extractTimeFromFilename(data.filename, data.timestamp);
  const hours = realTime.getHours().toString().padStart(2, "0");
  const mins = realTime.getMinutes().toString().padStart(2, "0");
  card.setAttribute("data-time", `${hours}:${mins}`);
  const timeStr = realTime.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  let cleanText = data.transcript_text.replace(
    /\[\d+\.\d+s -> \d+\.\d+s\]\s*/g,
    "",
  );
  const encodedCleanText = encodeURIComponent(cleanText);
  const safeRawText = cleanText.toLowerCase().replace(/"/g, "&quot;");
  const safeAudioUrl =
    "/audio/" + data.filename.split("/").map(encodeURIComponent).join("/");

  card.innerHTML = `
      <div class="msg-meta">
          <span>${timeStr}</span>
          <a href="${safeAudioUrl}" target="_blank" class="audio-link" title="Download Source File">💾 Source</a>
      </div>
      <div class="transcript-content" data-clean="${encodedCleanText}" data-raw="${safeRawText}">
          ${highlightText(cleanText)}
      </div>
      
      <div class="custom-audio-player">
          <button class="play-btn" onclick="togglePlay(this)">▶</button>
          <div class="progress-bar-container" onclick="seekAudio(event, this)">
              <div class="progress-fill"></div>
          </div>
          <div class="time-display">0:00</div>
          <audio src="${safeAudioUrl}" preload="none" 
              ontimeupdate="updateProgress(this)" 
              onended="resetPlayer(this)"
              onplay="updatePlayState(this, 'waiting')"
              onplaying="updatePlayState(this, 'playing')"
              onpause="updatePlayState(this, 'paused')">
          </audio>
      </div>
  `;
  return card;
}

window.toggleColumn = function (colId, isChecked) {
  const col = document.getElementById(colId);
  const badge = document.getElementById(`badge-${colId}`);
  col.style.display = isChecked ? "flex" : "none";
  if (isChecked) {
    unreadCounts[colId] = 0;
    badge.style.display = "none";
    badge.innerText = "0";
  }
};

window.toggleAutoScroll = function (colId, btn) {
  autoScrollState[colId] = !autoScrollState[colId];
  btn.classList.toggle("active", autoScrollState[colId]);
};

window.filterMessages = function (colId, query) {
  const container = document.getElementById(`msgs-${colId}`);
  const messages = container.getElementsByClassName("message-card");
  const lowerQuery = query.toLowerCase();
  for (let msg of messages) {
    const text = msg
      .querySelector(".transcript-content")
      .getAttribute("data-raw");
    msg.classList.toggle("hidden-search", !text.includes(lowerQuery));
  }
};

window.applyTimeFilter = function () {
  const start = document.getElementById("filter-start").value;
  const end = document.getElementById("filter-end").value;
  if (!start || !end) return;

  function timeToMins(t) {
    if (!t) return 0;
    const [h, m] = t.split(":").map(Number);
    return h * 60 + m;
  }

  const startMins = timeToMins(start);
  const endMins = timeToMins(end);

  document.querySelectorAll(".message-card").forEach((card) => {
    const msgTimeStr = card.getAttribute("data-time");
    if (!msgTimeStr) return;

    const msgMins = timeToMins(msgTimeStr);
    let isHidden = false;

    if (startMins <= endMins) {
      isHidden = msgMins < startMins || msgMins > endMins;
    } else {
      isHidden = !(msgMins >= startMins || msgMins <= endMins);
    }
    card.classList.toggle("hidden-time", isHidden);
  });

  const startLabel =
    document.getElementById("filter-start").options[
      document.getElementById("filter-start").selectedIndex
    ].text;
  const endLabel =
    document.getElementById("filter-end").options[
      document.getElementById("filter-end").selectedIndex
    ].text;
  showToast(`Time filter applied: ${startLabel} - ${endLabel}`);
};

window.clearTimeFilter = function () {
  document.getElementById("filter-start").value = "";
  document.getElementById("filter-end").value = "";
  document
    .querySelectorAll(".message-card")
    .forEach((card) => card.classList.remove("hidden-time"));
  showToast("Time filter cleared");
};

window.exportCSV = function () {
  let csvContent = "data:text/csv;charset=utf-8,Time,Channel,Transcript\n";
  const visibleColumns = [...dashboard.children].filter(
    (col) => col.style.display !== "none",
  );

  visibleColumns.forEach((col) => {
    const channel = col.querySelector(".col-title").textContent.trim();
    const messages = col.querySelectorAll(
      ".message-card:not(.hidden-search):not(.hidden-time)",
    );
    messages.forEach((msg) => {
      const time = msg.querySelector(".msg-meta span").innerText;
      let text = msg
        .querySelector(".transcript-content")
        .innerText.replace(/"/g, '""');
      csvContent += `"${time}","${channel}","${text}"\n`;
    });
  });

  const encodedUri = encodeURI(csvContent);
  const link = document.createElement("a");
  link.setAttribute("href", encodedUri);
  link.setAttribute(
    "download",
    `LiveOps_Log_${new Date().toLocaleDateString().replace(/\//g, "-")}.csv`,
  );
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  showToast("CSV Exported Successfully!", "success");
};

function processIncomingData(data) {
  if (processedMessages.has(data.filename)) return;
  processedMessages.add(data.filename);

  const channelName = getChannelName(data.filename);

  if (!knownChannels.has(channelName)) {
    knownChannels.add(channelName);
    createColumn(channelName);
  }

  const colId = `col-${channelName.replace(/[^a-zA-Z0-9]/g, "-")}`;
  const container = document.getElementById(`msgs-${colId}`);
  const card = createMessageCard(data);
  container.appendChild(card);

  if (keywordRegex && data.transcript_text.match(keywordRegex)) {
    triggerDesktopNotification(channelName, data.transcript_text);
  }

  const colElement = document.getElementById(colId);
  if (colElement.style.display === "none") {
    unreadCounts[colId]++;
    const badge = document.getElementById(`badge-${colId}`);
    badge.innerText = unreadCounts[colId];
    badge.style.display = "inline-block";
    badge.style.transform = "scale(1.2)";
    setTimeout(() => (badge.style.transform = "scale(1)"), 150);
  }

  if (autoScrollState[colId]) {
    container.scrollTop = container.scrollHeight;
  }
}

let ws;
function connectWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

  ws.onmessage = (event) => processIncomingData(JSON.parse(event.data));
  ws.onopen = () => showToast("Live Server Connected", "success");
  ws.onclose = () => {
    showToast("Connection Lost! Reconnecting in 3s...", "danger");
    setTimeout(connectWebSocket, 3000);
  };
  ws.onerror = () => ws.close();
}

fetch("/api/history")
  .then((res) => res.json())
  .then((history) => history.forEach(processIncomingData))
  .catch((err) => console.log("No history found or server offline."))
  .finally(() => {
    connectWebSocket();
  });
