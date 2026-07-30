// Phase Two operator workflow: detail drawer, global player, shortcuts, and workspaces.
const phaseTwoState = {
  detail: null,
  selectedCard: null,
  playerCard: null,
  focusedFeed: "",
  workspaces: [],
  commandIndex: 0,
};

const phaseTwoDrawer = document.getElementById("transmission-drawer");
const phaseTwoDrawerScrim = document.getElementById("drawer-scrim");
const phaseTwoDrawerContent = document.getElementById("drawer-content");
const phaseTwoDrawerLoading = document.getElementById("drawer-loading");
const phaseTwoAudio = document.getElementById("global-audio");
const phaseTwoPlayer = document.getElementById("global-player");
const phaseTwoPlayerProgress = document.getElementById("player-progress");
const phaseTwoWorkspaceDialog = document.getElementById("workspace-dialog");
const phaseTwoCommandDialog = document.getElementById("command-palette");
const phaseTwoCommandQuery = document.getElementById("command-query");
const phaseTwoCommandResults = document.getElementById("command-results");

function phaseTwoAudioUrl(filename) {
  return `/audio/${String(filename || "")
    .split("/")
    .map(encodeURIComponent)
    .join("/")}`;
}

function phaseTwoVisibleCards() {
  const archiveResults = [
    ...document.querySelectorAll(
      "#archive-search-view:not([hidden]) .archive-result",
    ),
  ];
  if (archiveResults.length) return archiveResults;
  return [...document.querySelectorAll(".message-card")].filter((card) => {
    const column = card.closest(".channel-column");
    return (
      !card.classList.contains("hidden-search") &&
      !card.classList.contains("hidden-time") &&
      column &&
      !column.classList.contains("channel-user-hidden") &&
      !column.classList.contains("hidden-search-column") &&
      !column.classList.contains("focus-feed-hidden")
    );
  });
}

function phaseTwoSelectCard(card, scroll = false) {
  if (!card) return null;
  phaseTwoState.selectedCard?.classList.remove("keyboard-selected");
  phaseTwoState.selectedCard = card;
  card.classList.add("keyboard-selected");
  if (scroll) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  return card;
}

function phaseTwoCardById(transcriptId) {
  return document.querySelector(
    `.message-card[data-transcript-id="${CSS.escape(String(transcriptId))}"]`,
  );
}

function phaseTwoReplaceCard(payload) {
  const current = phaseTwoCardById(payload.id);
  if (!current) return null;
  const replacement = createMessageCard(payload);
  const wasSelected = current === phaseTwoState.selectedCard;
  const wasPlaying = current === phaseTwoState.playerCard;
  current.replaceWith(replacement);
  if (wasSelected) phaseTwoSelectCard(replacement);
  if (wasPlaying) phaseTwoState.playerCard = replacement;
  updateSearchResults();
  return replacement;
}

async function phaseTwoJson(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    const message =
      typeof detail === "string"
        ? detail
        : detail?.message || "The requested operation could not be completed";
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function phaseTwoShowUndo(message, undoAction) {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = "toast toast-success undo-toast";
  const label = document.createElement("span");
  label.textContent = message;
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "Undo";
  let active = true;
  const remove = () => {
    if (!active) return;
    active = false;
    toast.remove();
  };
  button.addEventListener("click", async () => {
    if (!active) return;
    button.disabled = true;
    try {
      await undoAction();
      showToast("Change undone.", "success");
    } catch (error) {
      showToast(error.message, "danger");
    } finally {
      remove();
    }
  });
  toast.append(label, button);
  container.appendChild(toast);
  window.setTimeout(remove, 8000);
}

function phaseTwoHistoryLabel(entry) {
  const action = {
    review: "Review updated",
    correction: "Transcript corrected",
    annotation: "Annotation updated",
  }[entry.change_type] || "Transmission updated";
  const changedAt = new Date(entry.changed_at);
  const time = Number.isNaN(changedAt.getTime())
    ? entry.changed_at
    : changedAt.toLocaleString();
  return `${action} by ${entry.changed_by} · ${time}`;
}

function phaseTwoRenderDetail(detail) {
  phaseTwoState.detail = detail;
  const recordedAt = new Date(detail.recorded_at || detail.timestamp);
  document.getElementById("drawer-channel").textContent = getChannelName(
    detail.filename,
  );
  document.getElementById("drawer-title").textContent =
    `Transmission ${detail.id}`;
  document.getElementById("drawer-recorded-at").textContent =
    Number.isNaN(recordedAt.getTime())
      ? detail.recorded_at || detail.timestamp
      : recordedAt.toLocaleString();
  document.getElementById("drawer-version").textContent =
    `Version ${detail.version}`;
  document.getElementById("drawer-review-state").value =
    detail.review_state || "unreviewed";
  document.getElementById("drawer-review-resolution").value =
    detail.review_resolution || "";
  document.getElementById("drawer-review-meta").textContent = detail.reviewed_by
    ? `Last reviewed by ${detail.reviewed_by}${
        detail.reviewed_at
          ? ` · ${new Date(detail.reviewed_at).toLocaleString()}`
          : ""
      }`
    : "Not yet reviewed";
  document.getElementById("drawer-transcript").textContent =
    detail.transcript_text || "";
  document.getElementById("drawer-notes").value = detail.notes || "";
  document.getElementById("drawer-correction").value =
    detail.transcript_text || "";

  const comparison = document.getElementById("drawer-comparison-section");
  const hasComparison = Boolean(
    detail.raw_transcript_text || detail.retry_transcript_text,
  );
  comparison.hidden = !hasComparison;
  document.getElementById("drawer-raw-transcript").textContent =
    detail.raw_transcript_text || "No separate original result stored.";
  document.getElementById("drawer-retry-transcript").textContent =
    detail.retry_transcript_text || "No Large V3 retry was required.";

  const correctionSection = document.getElementById(
    "drawer-correction-section",
  );
  correctionSection.hidden = !currentProfile?.permissions?.correct;
  const canReview = Boolean(currentProfile?.permissions?.review);
  for (const control of phaseTwoDrawer.querySelectorAll(
    "#drawer-review-state, #drawer-review-resolution, #drawer-notes, #drawer-save-button",
  )) {
    control.disabled = !canReview;
  }

  const historyList = document.getElementById("drawer-history-list");
  historyList.replaceChildren();
  if (!detail.history?.length) {
    historyList.textContent = "No changes have been recorded yet.";
  } else {
    detail.history.forEach((entry) => {
      const item = document.createElement("div");
      item.className = "drawer-history-item";
      item.textContent = phaseTwoHistoryLabel(entry);
      historyList.appendChild(item);
    });
  }

  const metadata = document.getElementById("drawer-metadata");
  metadata.innerHTML = `
    <div><dt>Filename</dt><dd>${escapeHTML(detail.filename)}</dd></div>
    <div><dt>Primary model</dt><dd>${escapeHTML(detail.transcription_model || "Unknown")}</dd></div>
    <div><dt>Quality</dt><dd>${escapeHTML(
      detail.quality_reason ||
        `${Math.round(Number(detail.quality_score || 0) * 100)}% confidence`,
    )}</dd></div>
    <div><dt>Retry model</dt><dd>${escapeHTML(detail.retry_model || "Not used")}</dd></div>
  `;
  document.getElementById("drawer-bookmark-button").textContent =
    detail.bookmarked ? "Remove bookmark" : "Bookmark";
  document.getElementById("drawer-save-status").textContent = "";
  phaseTwoDrawerLoading.hidden = true;
  phaseTwoDrawerContent.hidden = false;
}

async function phaseTwoOpenDrawer(transcriptId, focusTarget = "") {
  if (!transcriptId) return;
  phaseTwoDrawer.hidden = false;
  phaseTwoDrawerScrim.hidden = false;
  phaseTwoDrawerLoading.hidden = false;
  phaseTwoDrawerContent.hidden = true;
  document.body.classList.add("drawer-open");
  try {
    const detail = await phaseTwoJson(`/api/transcripts/${transcriptId}`);
    phaseTwoRenderDetail(detail);
    const card = phaseTwoCardById(transcriptId);
    if (card) phaseTwoSelectCard(card);
    if (focusTarget === "notes") {
      document.getElementById("drawer-notes").focus();
    } else if (
      focusTarget === "correction" &&
      currentProfile?.permissions?.correct
    ) {
      document.getElementById("drawer-correction").focus();
    } else {
      document.getElementById("drawer-close-button").focus();
    }
  } catch (error) {
    phaseTwoCloseDrawer();
    showToast(error.message, "danger");
  }
}
window.phaseTwoOpenDrawer = phaseTwoOpenDrawer;

function phaseTwoCloseDrawer() {
  phaseTwoDrawer.hidden = true;
  phaseTwoDrawerScrim.hidden = true;
  document.body.classList.remove("drawer-open");
  phaseTwoState.selectedCard?.focus({ preventScroll: true });
}

async function phaseTwoPatchTranscript(transcriptId, changes, version) {
  return phaseTwoJson(`/api/transcripts/${transcriptId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...changes, version }),
  });
}

function phaseTwoUndoValues(before, changes) {
  const undo = {};
  for (const key of Object.keys(changes)) {
    if (key === "transcript_text") undo.transcript_text = before.transcript_text;
    else if (key in before) undo[key] = before[key];
  }
  return undo;
}

async function phaseTwoMutateDetail(changes, successMessage) {
  const before = phaseTwoState.detail;
  const result = await phaseTwoPatchTranscript(before.id, changes, before.version);
  phaseTwoReplaceCard(result);
  const refreshed = await phaseTwoJson(`/api/transcripts/${before.id}`);
  phaseTwoRenderDetail(refreshed);
  const undoValues = phaseTwoUndoValues(before, changes);
  phaseTwoShowUndo(successMessage, async () => {
    const undone = await phaseTwoPatchTranscript(
      refreshed.id,
      undoValues,
      refreshed.version,
    );
    phaseTwoReplaceCard(undone);
    if (!phaseTwoDrawer.hidden) {
      phaseTwoRenderDetail(await phaseTwoJson(`/api/transcripts/${undone.id}`));
    }
  });
}

async function phaseTwoQuickMutation(card, changes, successMessage) {
  const before = {
    review_state: card.dataset.reviewState || "unreviewed",
    bookmarked: card.classList.contains("is-bookmarked"),
  };
  const result = await phaseTwoPatchTranscript(
    card.dataset.transcriptId,
    changes,
    Number(card.dataset.version || 1),
  );
  const replacement = phaseTwoReplaceCard(result);
  phaseTwoShowUndo(successMessage, async () => {
    const undoValues = phaseTwoUndoValues(before, changes);
    const undone = await phaseTwoPatchTranscript(
      result.id,
      undoValues,
      result.version,
    );
    phaseTwoReplaceCard(undone);
  });
  return replacement;
}
window.phaseTwoQuickCardMutation = phaseTwoQuickMutation;

async function phaseTwoSaveDrawer() {
  const detail = phaseTwoState.detail;
  if (!detail) return;
  const changes = {};
  const reviewState = document.getElementById("drawer-review-state").value;
  const resolution = document
    .getElementById("drawer-review-resolution")
    .value.trim();
  const notes = document.getElementById("drawer-notes").value.trim();
  const correction = document.getElementById("drawer-correction").value.trim();
  if (reviewState !== detail.review_state) changes.review_state = reviewState;
  if (resolution !== (detail.review_resolution || "")) {
    changes.review_resolution = resolution;
  }
  if (notes !== (detail.notes || "")) changes.notes = notes;
  if (
    currentProfile?.permissions?.correct &&
    correction &&
    correction !== detail.transcript_text
  ) {
    changes.transcript_text = correction;
  }
  if (!Object.keys(changes).length) {
    document.getElementById("drawer-save-status").textContent =
      "No changes to save.";
    return;
  }
  const button = document.getElementById("drawer-save-button");
  button.disabled = true;
  document.getElementById("drawer-save-status").textContent = "Saving…";
  try {
    await phaseTwoMutateDetail(changes, "Transmission updated.");
  } catch (error) {
    if (error.status === 409) {
      document.getElementById("drawer-save-status").textContent =
        "Another operator changed this transmission. Reopen it to review the latest version.";
    } else {
      document.getElementById("drawer-save-status").textContent = error.message;
    }
  } finally {
    button.disabled = false;
  }
}

function phaseTwoUpdatePlayerButtons(playing) {
  const toggle = document.getElementById("player-toggle");
  toggle.textContent = playing ? "❚❚" : "▶";
  toggle.setAttribute(
    "aria-label",
    playing ? "Pause recording" : "Play recording",
  );
  document.getElementById("drawer-play-button").textContent = playing
    ? "Pause recording"
    : "Play recording";
  document.querySelectorAll(".card-play-button").forEach((button) => {
    const active = button.closest(".message-card") === phaseTwoState.playerCard;
    button.classList.toggle("active", active);
    button.querySelector("[aria-hidden='true']").textContent =
      active && playing ? "❚❚" : "▶";
  });
}

async function phaseTwoPlayCard(card, autoplay = true) {
  if (!card || !currentProfile?.permissions?.audio) return;
  const filename = card.dataset.filename;
  const source = phaseTwoAudioUrl(filename);
  const sameRecording = phaseTwoAudio.dataset.filename === filename;
  phaseTwoState.playerCard = card;
  phaseTwoSelectCard(card);
  phaseTwoPlayer.hidden = false;
  document.body.classList.add("player-open");
  phaseTwoAudio.dataset.filename = filename;
  if (!sameRecording) {
    phaseTwoAudio.src = source;
    phaseTwoAudio.load();
    phaseTwoPlayerProgress.value = "0";
  }
  document.getElementById("player-channel").textContent =
    getChannelName(filename);
  document.getElementById("player-title").textContent =
    card.querySelector(".transcript-content")?.innerText || filename;
  if (autoplay) {
    if (sameRecording && !phaseTwoAudio.paused) {
      phaseTwoAudio.pause();
    } else {
      await phaseTwoAudio.play().catch((error) => {
        if (error.name !== "AbortError") {
          showToast("Audio unavailable or missing.", "danger");
        }
      });
    }
  }
}
window.phaseTwoPlayCard = phaseTwoPlayCard;

function phaseTwoPlayAdjacent(direction) {
  const cards = phaseTwoVisibleCards();
  if (!cards.length) return;
  const current = phaseTwoState.playerCard || phaseTwoState.selectedCard;
  const currentIndex = Math.max(0, cards.indexOf(current));
  const next = cards[(currentIndex + direction + cards.length) % cards.length];
  phaseTwoPlayCard(next);
}

function phaseTwoClosePlayer() {
  phaseTwoAudio.pause();
  phaseTwoAudio.removeAttribute("src");
  phaseTwoAudio.load();
  delete phaseTwoAudio.dataset.filename;
  phaseTwoPlayer.hidden = true;
  document.body.classList.remove("player-open");
  phaseTwoState.playerCard = null;
  phaseTwoUpdatePlayerButtons(false);
}

function phaseTwoFocusFeed(channelName = "") {
  const columns = [...document.querySelectorAll(".channel-column")];
  const requested =
    channelName &&
    columns.find((column) => column.dataset.channel === channelName);
  phaseTwoState.focusedFeed =
    requested && phaseTwoState.focusedFeed !== channelName ? channelName : "";
  document.body.classList.toggle(
    "focus-feed-mode",
    Boolean(phaseTwoState.focusedFeed),
  );
  columns.forEach((column) => {
    const hidden =
      Boolean(phaseTwoState.focusedFeed) &&
      column.dataset.channel !== phaseTwoState.focusedFeed;
    column.classList.toggle("focus-feed-hidden", hidden);
    const button = column.querySelector('[data-action="focus-column"]');
    if (button) {
      const active = !hidden && Boolean(phaseTwoState.focusedFeed);
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
      button.title = active ? "Show all feeds" : "Focus feed";
    }
  });
  updateMobileFeedSwitcher();
  updateSearchResults();
}

function phaseTwoWorkspaceSnapshot() {
  const columns = [...document.querySelectorAll(".channel-column")];
  return {
    visible_feeds: columns
      .filter((column) => !column.classList.contains("channel-user-hidden"))
      .map((column) => column.dataset.channel),
    feed_order: columns.map((column) => column.dataset.channel),
    focused_feed: phaseTwoState.focusedFeed,
    view_mode: "board",
    filters: {
      query: globalSearchInput.value.trim(),
      date: document.getElementById("filter-date").value,
      start: document.getElementById("filter-start").value,
      end: document.getElementById("filter-end").value,
      include_suspect: showSuspectTranscripts,
      bookmarks_only: bookmarksOnly,
    },
    compact: compactModeToggle.checked,
    alerts_visible: notificationsEnabled,
  };
}

async function phaseTwoLoadWorkspaces() {
  phaseTwoState.workspaces = await phaseTwoJson("/api/workspaces");
  phaseTwoRenderWorkspaces();
}

function phaseTwoRenderWorkspaces() {
  const list = document.getElementById("workspace-list");
  list.replaceChildren();
  if (!phaseTwoState.workspaces.length) {
    const empty = document.createElement("div");
    empty.className = "workspace-empty";
    empty.textContent =
      "No saved workspaces yet. Arrange the board, then save the current layout.";
    list.appendChild(empty);
    return;
  }
  phaseTwoState.workspaces.forEach((workspace) => {
    const row = document.createElement("div");
    row.className = "workspace-row";
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "workspace-apply";
    apply.innerHTML = `<strong>${escapeHTML(workspace.name)}</strong><span>${
      workspace.is_shared
        ? `Shared by ${escapeHTML(workspace.owner_username)}`
        : "Personal workspace"
    }</span>`;
    apply.addEventListener("click", () => {
      phaseTwoApplyWorkspace(workspace).catch((error) =>
        showToast(error.message, "danger"),
      );
    });
    row.appendChild(apply);
    if (
      workspace.owner_username === currentProfile?.username ||
      currentProfile?.role === "admin"
    ) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "workspace-delete";
      remove.setAttribute("aria-label", `Delete ${workspace.name}`);
      remove.textContent = "×";
      remove.addEventListener("click", async () => {
        await phaseTwoJson(`/api/workspaces/${workspace.id}`, {
          method: "DELETE",
        });
        await phaseTwoLoadWorkspaces();
      });
      row.appendChild(remove);
    }
    list.appendChild(row);
  });
}

async function phaseTwoApplyWorkspace(workspace) {
  const configuration = workspace.configuration || {};
  const visibleFeeds = new Set(configuration.visible_feeds || []);
  hiddenChannels.clear();
  knownChannels.forEach((channel) => {
    if (!visibleFeeds.has(channel)) hiddenChannels.add(channel);
  });
  savedColumnOrder = Array.isArray(configuration.feed_order)
    ? [...configuration.feed_order]
    : savedColumnOrder;
  const filters = configuration.filters || {};
  const workspaceQuery = filters.query || "";
  globalSearchInput.value = workspaceQuery;
  globalSearchQuery = workspaceQuery.trim().toLowerCase();
  document.getElementById("filter-date").value = filters.date || "";
  document.getElementById("filter-start").value = filters.start || "";
  document.getElementById("filter-end").value = filters.end || "";
  dateTimeFilterActive = Boolean(filters.date || filters.start || filters.end);
  showSuspectTranscripts = Boolean(filters.include_suspect);
  document.getElementById("suspect-toggle").checked = showSuspectTranscripts;
  bookmarksOnly = Boolean(filters.bookmarks_only);
  document.getElementById("bookmarks-only-toggle").checked = bookmarksOnly;
  compactModeToggle.checked = Boolean(configuration.compact);
  document.body.classList.toggle("compact-mode", compactModeToggle.checked);
  const preservedQuery = globalSearchQuery;
  globalSearchQuery = "";
  await loadArchive();
  globalSearchQuery = preservedQuery;
  phaseTwoState.focusedFeed = "";
  phaseTwoFocusFeed(configuration.focused_feed || "");
  saveColumnPreferences();
  phaseTwoWorkspaceDialog.close();
  if (workspaceQuery && window.phaseThreeSearch) {
    window.phaseThreeSearch(workspaceQuery, true);
  }
  showToast(`Workspace applied: ${workspace.name}`, "success");
}

async function phaseTwoSaveWorkspace() {
  const name = document.getElementById("workspace-name").value.trim();
  if (!name) {
    showToast("Enter a workspace name.", "danger");
    return;
  }
  const workspace = await phaseTwoJson("/api/workspaces", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      configuration: phaseTwoWorkspaceSnapshot(),
      is_shared: document.getElementById("workspace-shared").checked,
    }),
  });
  document.getElementById("workspace-name").value = "";
  await phaseTwoLoadWorkspaces();
  showToast(`Workspace saved: ${workspace.name}`, "success");
}

function phaseTwoCommandEntries() {
  const selected = phaseTwoState.selectedCard;
  const commands = [
    {
      label: "Search the complete archive",
      keywords: "find query archive",
      shortcut: "/",
      run: () => globalSearchInput.focus(),
    },
    {
      label: "Open selected transmission",
      keywords: "detail drawer",
      shortcut: "Enter",
      disabled: !selected,
      run: () => phaseTwoOpenDrawer(selected?.dataset.transcriptId),
    },
    {
      label: "Play or pause selected recording",
      keywords: "audio",
      shortcut: "Space",
      disabled: !selected,
      run: () => phaseTwoPlayCard(selected),
    },
    {
      label: "Toggle compact data view",
      keywords: "density display",
      run: () => {
        compactModeToggle.click();
      },
    },
    {
      label: "Save current workspace",
      keywords: "layout view feeds",
      run: () => {
        phaseTwoCommandDialog.close();
        document.getElementById("workspace-button").click();
      },
    },
    {
      label: phaseTwoState.focusedFeed ? "Show all feeds" : "Focus selected feed",
      keywords: "board channel",
      disabled: !selected,
      run: () =>
        phaseTwoFocusFeed(
          phaseTwoState.focusedFeed
            ? ""
            : selected?.closest(".channel-column")?.dataset.channel,
        ),
    },
    {
      label: "Keyboard shortcut reference",
      keywords: "help keys",
      shortcut: "?",
      run: () => {
        phaseTwoCommandQuery.value = "?";
        phaseTwoRenderCommands();
      },
    },
  ];
  [...document.querySelectorAll(".channel-column")].forEach((column, index) => {
    commands.push({
      label: `Focus feed: ${column.dataset.channel}`,
      keywords: `channel feed ${column.dataset.channel}`,
      shortcut: index < 9 ? String(index + 1) : "",
      run: () => phaseTwoFocusFeed(column.dataset.channel),
    });
  });
  return commands;
}

function phaseTwoShortcutReference() {
  return [
    ["⌘K", "Open command palette"],
    ["/", "Focus archive search"],
    ["J / K", "Next / previous transmission"],
    ["Enter", "Open selected transmission"],
    ["Space", "Play or pause audio"],
    ["Shift + ← / →", "Skip audio five seconds"],
    ["R", "Confirm or unreview selected transmission"],
    ["B", "Toggle bookmark"],
    ["N", "Edit operator note"],
    ["E", "Correct transcript"],
    ["F", "Toggle follow-live for selected feed"],
    ["1–9", "Focus a configured feed"],
    ["Esc", "Close the active panel"],
  ];
}

function phaseTwoRenderCommands() {
  const query = phaseTwoCommandQuery.value.trim().toLowerCase();
  phaseTwoCommandResults.replaceChildren();
  let entries;
  if (query === "?") {
    entries = phaseTwoShortcutReference().map(([shortcut, label]) => ({
      label,
      shortcut,
      disabled: true,
      run: () => {},
    }));
  } else {
    entries = phaseTwoCommandEntries().filter((entry) =>
      `${entry.label} ${entry.keywords || ""} ${entry.shortcut || ""}`
        .toLowerCase()
        .includes(query),
    );
  }
  phaseTwoState.commandIndex = Math.min(
    phaseTwoState.commandIndex,
    Math.max(0, entries.length - 1),
  );
  entries.forEach((entry, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "option");
    button.className = "command-result";
    button.classList.toggle("selected", index === phaseTwoState.commandIndex);
    button.disabled = Boolean(entry.disabled);
    button.innerHTML = `<span>${escapeHTML(entry.label)}</span>${
      entry.shortcut ? `<kbd>${escapeHTML(entry.shortcut)}</kbd>` : ""
    }`;
    button.addEventListener("click", () => {
      if (!entry.disabled) {
        phaseTwoCommandDialog.close();
        entry.run();
      }
    });
    phaseTwoCommandResults.appendChild(button);
  });
}

function phaseTwoOpenCommands(query = "") {
  if (!phaseTwoCommandDialog.open) phaseTwoCommandDialog.showModal();
  phaseTwoCommandQuery.value = query;
  phaseTwoState.commandIndex = 0;
  phaseTwoRenderCommands();
  phaseTwoCommandQuery.focus();
}

function phaseTwoMoveSelection(direction) {
  const cards = phaseTwoVisibleCards();
  if (!cards.length) return;
  const currentIndex = cards.indexOf(phaseTwoState.selectedCard);
  const nextIndex =
    currentIndex < 0
      ? direction > 0
        ? 0
        : cards.length - 1
      : Math.max(0, Math.min(cards.length - 1, currentIndex + direction));
  phaseTwoSelectCard(cards[nextIndex], true)?.focus({ preventScroll: true });
}

document.getElementById("drawer-close-button").addEventListener(
  "click",
  phaseTwoCloseDrawer,
);
phaseTwoDrawerScrim.addEventListener("click", phaseTwoCloseDrawer);
document
  .getElementById("drawer-save-button")
  .addEventListener("click", phaseTwoSaveDrawer);
document
  .getElementById("drawer-bookmark-button")
  .addEventListener("click", async () => {
    const detail = phaseTwoState.detail;
    if (!detail) return;
    try {
      await phaseTwoMutateDetail(
        { bookmarked: !detail.bookmarked },
        detail.bookmarked ? "Bookmark removed." : "Transmission bookmarked.",
      );
    } catch (error) {
      showToast(error.message, "danger");
    }
  });
document.getElementById("drawer-copy-button").addEventListener("click", async () => {
  const text = phaseTwoState.detail?.transcript_text || "";
  try {
    await navigator.clipboard.writeText(text);
    showToast("Transcript copied.", "success");
  } catch {
    showToast("Clipboard access is unavailable.", "danger");
  }
});
document.getElementById("drawer-play-button").addEventListener("click", () => {
  const card = phaseTwoCardById(phaseTwoState.detail?.id);
  if (card) phaseTwoPlayCard(card);
});

document.getElementById("player-toggle").addEventListener("click", () => {
  if (!phaseTwoAudio.src && phaseTwoState.playerCard) {
    phaseTwoPlayCard(phaseTwoState.playerCard);
  } else if (phaseTwoAudio.paused) {
    phaseTwoAudio.play().catch(() =>
      showToast("Audio unavailable or missing.", "danger"),
    );
  } else {
    phaseTwoAudio.pause();
  }
});
document.getElementById("player-back").addEventListener("click", () => {
  phaseTwoAudio.currentTime = Math.max(0, phaseTwoAudio.currentTime - 5);
});
document.getElementById("player-forward").addEventListener("click", () => {
  phaseTwoAudio.currentTime = Math.min(
    phaseTwoAudio.duration || Number.POSITIVE_INFINITY,
    phaseTwoAudio.currentTime + 5,
  );
});
document
  .getElementById("player-previous")
  .addEventListener("click", () => phaseTwoPlayAdjacent(-1));
document
  .getElementById("player-next")
  .addEventListener("click", () => phaseTwoPlayAdjacent(1));
document
  .getElementById("player-close")
  .addEventListener("click", phaseTwoClosePlayer);
document.getElementById("player-speed").addEventListener("change", (event) => {
  phaseTwoAudio.playbackRate = Number(event.target.value);
});
document.getElementById("player-volume").addEventListener("input", (event) => {
  phaseTwoAudio.volume = Number(event.target.value);
  phaseTwoAudio.muted = false;
});
document.getElementById("player-mute").addEventListener("click", () => {
  phaseTwoAudio.muted = !phaseTwoAudio.muted;
  document.getElementById("player-mute").textContent = phaseTwoAudio.muted
    ? "×"
    : "◖";
});
phaseTwoPlayerProgress.addEventListener("input", () => {
  if (!Number.isFinite(phaseTwoAudio.duration)) return;
  phaseTwoAudio.currentTime =
    (Number(phaseTwoPlayerProgress.value) / 1000) * phaseTwoAudio.duration;
});
phaseTwoAudio.addEventListener("timeupdate", () => {
  const duration = Number.isFinite(phaseTwoAudio.duration)
    ? phaseTwoAudio.duration
    : 0;
  phaseTwoPlayerProgress.value = String(
    duration ? Math.round((phaseTwoAudio.currentTime / duration) * 1000) : 0,
  );
  document.getElementById("player-current-time").textContent = formatTime(
    phaseTwoAudio.currentTime,
  );
  document.getElementById("player-duration").textContent = formatTime(duration);
});
phaseTwoAudio.addEventListener("loadedmetadata", () => {
  document.getElementById("player-duration").textContent = formatTime(
    phaseTwoAudio.duration,
  );
});
phaseTwoAudio.addEventListener("play", () => phaseTwoUpdatePlayerButtons(true));
phaseTwoAudio.addEventListener("pause", () => phaseTwoUpdatePlayerButtons(false));
phaseTwoAudio.addEventListener("ended", () => {
  phaseTwoPlayerProgress.value = "0";
  phaseTwoAudio.currentTime = 0;
  phaseTwoUpdatePlayerButtons(false);
});

document.getElementById("workspace-button").addEventListener("click", async () => {
  document.getElementById("workspace-shared-label").hidden =
    !currentProfile?.permissions?.correct;
  document.getElementById("workspace-shared").checked = false;
  if (!phaseTwoWorkspaceDialog.open) phaseTwoWorkspaceDialog.showModal();
  try {
    await phaseTwoLoadWorkspaces();
  } catch (error) {
    showToast(error.message, "danger");
  }
});
document
  .getElementById("workspace-save-button")
  .addEventListener("click", () => {
    phaseTwoSaveWorkspace().catch((error) => showToast(error.message, "danger"));
  });
document
  .getElementById("command-palette-button")
  .addEventListener("click", () => phaseTwoOpenCommands());
phaseTwoCommandQuery.addEventListener("input", () => {
  phaseTwoState.commandIndex = 0;
  phaseTwoRenderCommands();
});
phaseTwoCommandQuery.addEventListener("keydown", (event) => {
  const results = [...phaseTwoCommandResults.querySelectorAll(".command-result")];
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const direction = event.key === "ArrowDown" ? 1 : -1;
    phaseTwoState.commandIndex =
      (phaseTwoState.commandIndex + direction + results.length) %
      Math.max(1, results.length);
    phaseTwoRenderCommands();
  } else if (event.key === "Enter") {
    event.preventDefault();
    results[phaseTwoState.commandIndex]?.click();
  }
});

document.addEventListener("click", (event) => {
  const control = event.target.closest("[data-action]");
  if (control?.dataset.action === "open-transmission") {
    phaseTwoOpenDrawer(
      control.closest(".message-card")?.dataset.transcriptId,
    );
    return;
  }
  if (control?.dataset.action === "focus-column") {
    phaseTwoFocusFeed(
      document.getElementById(control.dataset.columnId)?.dataset.channel,
    );
    return;
  }
  const card = event.target.closest(".message-card");
  if (
    card &&
    !event.target.closest("button, summary, details, input, select, textarea")
  ) {
    phaseTwoSelectCard(card);
    phaseTwoOpenDrawer(card.dataset.transcriptId);
  }
});

document.addEventListener("focusin", (event) => {
  const card = event.target.closest?.(".message-card");
  if (card) phaseTwoSelectCard(card);
});

document.addEventListener("keydown", (event) => {
  const target = event.target;
  const isTyping =
    ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName) ||
    target.isContentEditable;
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    phaseTwoOpenCommands();
    return;
  }
  if (event.key === "Escape") {
    if (phaseTwoCommandDialog.open) phaseTwoCommandDialog.close();
    else if (phaseTwoWorkspaceDialog.open) phaseTwoWorkspaceDialog.close();
    else if (!phaseTwoDrawer.hidden) phaseTwoCloseDrawer();
    return;
  }
  if (isTyping || event.metaKey || event.ctrlKey || event.altKey) return;
  const key = event.key.toLowerCase();
  const selected = phaseTwoState.selectedCard;
  if (event.key === "?") {
    event.preventDefault();
    phaseTwoOpenCommands("?");
  } else if (key === "j" || key === "k") {
    event.preventDefault();
    phaseTwoMoveSelection(key === "j" ? 1 : -1);
  } else if (event.key === "Enter" && selected) {
    event.preventDefault();
    phaseTwoOpenDrawer(selected.dataset.transcriptId);
  } else if (event.code === "Space" && selected) {
    event.preventDefault();
    phaseTwoPlayCard(selected);
  } else if (
    event.shiftKey &&
    (event.key === "ArrowLeft" || event.key === "ArrowRight")
  ) {
    event.preventDefault();
    const amount = event.key === "ArrowLeft" ? -5 : 5;
    phaseTwoAudio.currentTime = Math.max(
      0,
      Math.min(
        phaseTwoAudio.duration || Number.POSITIVE_INFINITY,
        phaseTwoAudio.currentTime + amount,
      ),
    );
  } else if (key === "r" && selected && currentProfile?.permissions?.review) {
    event.preventDefault();
    phaseTwoQuickMutation(
      selected,
      {
        review_state:
          selected.dataset.reviewState === "confirmed"
            ? "unreviewed"
            : "confirmed",
      },
      selected.dataset.reviewState === "confirmed"
        ? "Transmission marked unreviewed."
        : "Transmission confirmed.",
    ).catch((error) => showToast(error.message, "danger"));
  } else if (key === "b" && selected && currentProfile?.permissions?.review) {
    event.preventDefault();
    const bookmarked = !selected.classList.contains("is-bookmarked");
    phaseTwoQuickMutation(
      selected,
      { bookmarked },
      bookmarked ? "Transmission bookmarked." : "Bookmark removed.",
    ).catch((error) => showToast(error.message, "danger"));
  } else if (key === "n" && selected && currentProfile?.permissions?.review) {
    event.preventDefault();
    phaseTwoOpenDrawer(selected.dataset.transcriptId, "notes");
  } else if (key === "e" && selected && currentProfile?.permissions?.correct) {
    event.preventDefault();
    phaseTwoOpenDrawer(selected.dataset.transcriptId, "correction");
  } else if (key === "f" && selected) {
    event.preventDefault();
    const column = selected.closest(".channel-column");
    const button = column?.querySelector('[data-action="toggle-auto-scroll"]');
    if (column && button) toggleAutoScroll(column.id, button);
  } else if (/^[1-9]$/.test(event.key)) {
    const column = document.querySelectorAll(".channel-column")[
      Number(event.key) - 1
    ];
    if (column) {
      event.preventDefault();
      phaseTwoFocusFeed(column.dataset.channel);
    }
  }
});
