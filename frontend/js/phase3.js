// Phase Three archive search, facets, saved filters, and server preferences.
const phaseThreeState = {
  query: "",
  cursor: null,
  controller: null,
  timer: null,
  requestNumber: 0,
  facetsLoaded: false,
  preferences: {},
  savedSearches: [],
};

const phaseThreeView = document.getElementById("archive-search-view");
const phaseThreeResults = document.getElementById("archive-search-results");
const phaseThreeSummary = document.getElementById("archive-search-summary");
const phaseThreeMore = document.getElementById("archive-search-more");
const phaseThreeSavedDialog = document.getElementById("saved-search-dialog");
const phaseThreeFilterIds = {
  channel: "archive-search-channel",
  year: "archive-search-year",
  review_state: "archive-search-review-state",
  status: "archive-search-status",
  reviewer: "archive-search-reviewer",
  model: "archive-search-model",
};

function phaseThreeOpenView() {
  dashboard.hidden = true;
  document.getElementById("empty-state").hidden = true;
  phaseThreeView.hidden = false;
  document.body.classList.add("archive-search-mode");
}

function phaseThreeCloseView() {
  phaseThreeState.controller?.abort();
  phaseThreeView.hidden = true;
  dashboard.hidden = false;
  document.body.classList.remove("archive-search-mode");
  phaseThreeResults.replaceChildren();
  phaseThreeMore.hidden = true;
  updateSearchResults();
}

function phaseThreeMarkedSnippet(value) {
  return escapeHTML(value || "")
    .replaceAll("⟦", "<mark>")
    .replaceAll("⟧", "</mark>");
}

function phaseThreeCurrentFilters() {
  const filters = {};
  for (const [key, id] of Object.entries(phaseThreeFilterIds)) {
    const value = document.getElementById(id).value;
    if (value) filters[key] = key === "year" ? Number(value) : value;
  }
  if (document.getElementById("archive-search-bookmarked").checked) {
    filters.bookmarked = true;
  }
  const selectedDate = document.getElementById("filter-date").value;
  if (selectedDate) {
    filters.date_from = selectedDate;
    filters.date_to = selectedDate;
  }
  const start = document.getElementById("filter-start").value;
  const end = document.getElementById("filter-end").value;
  if (start) filters.start = start;
  if (end) filters.end = end;
  return filters;
}

function phaseThreeSearchConfiguration() {
  return {
    query: phaseThreeState.query,
    filters: phaseThreeCurrentFilters(),
    sort: document.getElementById("archive-search-sort").value,
  };
}

function phaseThreeSetSelect(id, value) {
  const select = document.getElementById(id);
  const normalized = String(value ?? "");
  if ([...select.options].some((option) => option.value === normalized)) {
    select.value = normalized;
  }
}

function phaseThreeApplyFilters(filters = {}) {
  for (const [key, id] of Object.entries(phaseThreeFilterIds)) {
    phaseThreeSetSelect(id, filters[key] ?? "");
  }
  document.getElementById("archive-search-bookmarked").checked =
    Boolean(filters.bookmarked);
  if (filters.date_from === filters.date_to) {
    document.getElementById("filter-date").value = filters.date_from || "";
  } else if (!filters.date_from && !filters.date_to) {
    document.getElementById("filter-date").value = "";
  }
  document.getElementById("filter-start").value = filters.start || "";
  document.getElementById("filter-end").value = filters.end || "";
}

function phaseThreeRenderResult(item) {
  const recordedAt = new Date(item.recorded_at || item.timestamp);
  const article = document.createElement("article");
  article.className = "archive-result message-card";
  article.tabIndex = 0;
  article.dataset.transcriptId = String(item.id);
  article.dataset.filename = item.filename;
  article.dataset.version = String(item.version || 1);
  article.dataset.reviewState = item.review_state || "unreviewed";
  const reviewLabel = String(item.review_state || "unreviewed").replaceAll(
    "_",
    " ",
  );
  article.innerHTML = `
    <div class="archive-result-meta">
      <span class="archive-result-channel">${escapeHTML(
        item.channel || getChannelName(item.filename),
      )}</span>
      <span>${escapeHTML(
        Number.isNaN(recordedAt.getTime())
          ? item.recorded_at || item.timestamp || ""
          : recordedAt.toLocaleString(),
      )}</span>
      ${
        item.recording_year
          ? `<span>${escapeHTML(String(item.recording_year))}</span>`
          : ""
      }
      ${
        item.review_state && item.review_state !== "unreviewed"
          ? `<span class="card-state-badge reviewed-state">${escapeHTML(
              reviewLabel,
            )}</span>`
          : ""
      }
    </div>
    <div class="transcript-content search-result-snippet">${phaseThreeMarkedSnippet(
      item.snippet || item.transcript_text,
    )}</div>
    <div class="archive-result-actions">
      <button class="btn btn-primary" data-action="open-transmission" type="button">
        Open transmission
      </button>
      ${
        currentProfile?.permissions?.audio
          ? `<button
               class="btn card-play-button"
               data-action="toggle-play"
               data-audio-url="${phaseTwoAudioUrl(item.filename)}"
               type="button"
               aria-label="Play recording in global player"
             ><span aria-hidden="true">▶</span><span>Play recording</span></button>`
          : ""
      }
    </div>
  `;
  return article;
}

async function phaseThreeExecuteSearch({ append = false } = {}) {
  const query = phaseThreeState.query.trim();
  if (!query) {
    phaseThreeCloseView();
    return;
  }
  phaseThreeOpenView();
  phaseThreeState.controller?.abort();
  const controller = new AbortController();
  phaseThreeState.controller = controller;
  const requestNumber = ++phaseThreeState.requestNumber;
  const parameters = new URLSearchParams({
    q: query,
    sort: document.getElementById("archive-search-sort").value,
    limit: document.getElementById("archive-search-page-size").value,
  });
  for (const [key, value] of Object.entries(phaseThreeCurrentFilters())) {
    parameters.set(key, String(value));
  }
  if (append && phaseThreeState.cursor) {
    parameters.set("cursor", phaseThreeState.cursor);
  }
  phaseThreeSummary.textContent = append
    ? "Loading more matching transmissions…"
    : "Searching the complete indexed archive…";
  phaseThreeMore.disabled = true;
  try {
    const response = await fetch(`/api/search?${parameters}`, {
      cache: "no-store",
      signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Archive search failed");
    }
    if (requestNumber !== phaseThreeState.requestNumber) return;
    if (!append) phaseThreeResults.replaceChildren();
    payload.items.forEach((item) =>
      phaseThreeResults.appendChild(phaseThreeRenderResult(item)),
    );
    phaseThreeState.cursor = payload.next_cursor;
    phaseThreeMore.hidden = !payload.next_cursor;
    phaseThreeMore.disabled = false;
    const shown = phaseThreeResults.querySelectorAll(".archive-result").length;
    phaseThreeSummary.textContent =
      `${payload.count.toLocaleString()} exact match${
        payload.count === 1 ? "" : "es"
      } · showing ${shown.toLocaleString()} · ${payload.elapsed_ms.toLocaleString()} ms`;
    document.getElementById("search-status").textContent =
      `${payload.count.toLocaleString()} indexed archive result${
        payload.count === 1 ? "" : "s"
      }`;
    if (!payload.count) {
      const empty = document.createElement("div");
      empty.className = "archive-search-empty";
      empty.innerHTML =
        "<strong>No indexed matches</strong><span>Try fewer terms, a prefix ending in *, or broader filters.</span>";
      phaseThreeResults.appendChild(empty);
    }
  } catch (error) {
    if (error.name === "AbortError") return;
    phaseThreeMore.disabled = false;
    phaseThreeSummary.textContent = error.message;
    showToast(error.message, "danger");
  }
}

function phaseThreeScheduleSearch(query, immediate = false) {
  phaseThreeState.query = String(query || "").trim();
  clearTimeout(phaseThreeState.timer);
  phaseThreeState.cursor = null;
  if (!phaseThreeState.query) {
    phaseThreeCloseView();
    return;
  }
  phaseThreeOpenView();
  phaseThreeState.timer = window.setTimeout(
    () => phaseThreeExecuteSearch(),
    immediate ? 0 : 220,
  );
}
window.phaseThreeSearch = phaseThreeScheduleSearch;

window.phaseThreeClearSearch = function () {
  phaseThreeState.query = "";
  phaseThreeState.cursor = null;
  phaseThreeCloseView();
  document.getElementById("search-status").textContent =
    `${document.querySelectorAll(".message-card").length} loaded transmissions`;
};

function phaseThreePopulateFacetSelect(id, items, placeholder) {
  const select = document.getElementById(id);
  const previous = select.value;
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = placeholder;
  select.appendChild(all);
  items.forEach((item) => {
    const option = document.createElement("option");
    option.value = String(item.value);
    option.textContent = `${item.value} (${Number(item.count).toLocaleString()})`;
    select.appendChild(option);
  });
  phaseThreeSetSelect(id, previous);
}

async function phaseThreeLoadFacets() {
  const facets = await phaseTwoJson("/api/archive/facets");
  phaseThreePopulateFacetSelect(
    "archive-search-channel",
    facets.channels,
    "All channels",
  );
  phaseThreePopulateFacetSelect(
    "archive-search-year",
    facets.years,
    "All years",
  );
  phaseThreePopulateFacetSelect(
    "archive-search-model",
    facets.models,
    "All models",
  );
  phaseThreePopulateFacetSelect(
    "archive-search-reviewer",
    facets.reviewers,
    "All reviewers",
  );
  phaseThreeState.facetsLoaded = true;
}

async function phaseThreeLoadPreferences() {
  const payload = await phaseTwoJson("/api/preferences");
  phaseThreeState.preferences = payload.configuration || {};
  phaseThreeSetSelect(
    "archive-search-sort",
    phaseThreeState.preferences.search_sort || "relevance",
  );
  phaseThreeSetSelect(
    "archive-search-page-size",
    phaseThreeState.preferences.search_page_size || 50,
  );
  phaseThreeApplyFilters(
    phaseThreeState.preferences.default_search_filters || {},
  );
}

async function phaseThreeSavePreferences() {
  const configuration = {
    ...phaseThreeState.preferences,
    search_sort: document.getElementById("archive-search-sort").value,
    search_page_size: Number(
      document.getElementById("archive-search-page-size").value,
    ),
    default_search_filters: phaseThreeCurrentFilters(),
  };
  const payload = await phaseTwoJson("/api/preferences", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ configuration }),
  });
  phaseThreeState.preferences = payload.configuration;
}

async function phaseThreeLoadSavedSearches() {
  phaseThreeState.savedSearches = await phaseTwoJson("/api/saved-searches");
  const list = document.getElementById("saved-search-list");
  list.replaceChildren();
  if (!phaseThreeState.savedSearches.length) {
    const empty = document.createElement("div");
    empty.className = "workspace-empty";
    empty.textContent =
      "No saved searches yet. Run a search, choose filters, then save it here.";
    list.appendChild(empty);
    return;
  }
  phaseThreeState.savedSearches.forEach((savedSearch) => {
    const row = document.createElement("div");
    row.className = "workspace-row";
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "workspace-apply";
    apply.innerHTML = `<strong>${escapeHTML(savedSearch.name)}</strong><span>${escapeHTML(
      savedSearch.configuration.query,
    )}</span>`;
    apply.addEventListener("click", () => {
      const configuration = savedSearch.configuration;
      globalSearchInput.value = configuration.query;
      globalSearchQuery = configuration.query.toLowerCase();
      phaseThreeSetSelect(
        "archive-search-sort",
        configuration.sort || "relevance",
      );
      phaseThreeApplyFilters(configuration.filters || {});
      phaseThreeSavedDialog.close();
      phaseThreeScheduleSearch(configuration.query, true);
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "workspace-delete";
    remove.setAttribute("aria-label", `Delete ${savedSearch.name}`);
    remove.textContent = "×";
    remove.addEventListener("click", async () => {
      await phaseTwoJson(`/api/saved-searches/${savedSearch.id}`, {
        method: "DELETE",
      });
      await phaseThreeLoadSavedSearches();
    });
    row.append(apply, remove);
    list.appendChild(row);
  });
}

async function phaseThreeSaveCurrentSearch() {
  const name = document.getElementById("saved-search-name").value.trim();
  if (!name) {
    showToast("Enter a saved search name.", "danger");
    return;
  }
  const saved = await phaseTwoJson("/api/saved-searches", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      configuration: phaseThreeSearchConfiguration(),
    }),
  });
  document.getElementById("saved-search-name").value = "";
  await phaseThreeLoadSavedSearches();
  showToast(`Saved search: ${saved.name}`, "success");
}

document
  .getElementById("archive-search-close")
  .addEventListener("click", () => {
    globalSearchInput.value = "";
    globalSearchQuery = "";
    phaseThreeState.query = "";
    phaseThreeCloseView();
    globalSearchInput.focus();
  });
document
  .getElementById("archive-search-more")
  .addEventListener("click", () =>
    phaseThreeExecuteSearch({ append: true }),
  );
document
  .getElementById("archive-search-clear-filters")
  .addEventListener("click", () => {
    phaseThreeApplyFilters({});
    for (const id of Object.values(phaseThreeFilterIds)) {
      document.getElementById(id).value = "";
    }
    document.getElementById("archive-search-bookmarked").checked = false;
    phaseThreeExecuteSearch();
    phaseThreeSavePreferences().catch(() => {});
  });

for (const id of [
  "archive-search-sort",
  "archive-search-channel",
  "archive-search-year",
  "archive-search-review-state",
  "archive-search-status",
  "archive-search-reviewer",
  "archive-search-model",
  "archive-search-page-size",
  "archive-search-bookmarked",
]) {
  document.getElementById(id).addEventListener("change", () => {
    if (phaseThreeState.query) {
      phaseThreeState.cursor = null;
      phaseThreeExecuteSearch();
    }
    phaseThreeSavePreferences().catch(() => {});
  });
}

document.getElementById("saved-searches-button").addEventListener("click", async () => {
  if (!phaseThreeSavedDialog.open) phaseThreeSavedDialog.showModal();
  try {
    await phaseThreeLoadSavedSearches();
  } catch (error) {
    showToast(error.message, "danger");
  }
});
document.getElementById("save-current-search").addEventListener("click", () => {
  phaseThreeSaveCurrentSearch().catch((error) =>
    showToast(error.message, "danger"),
  );
});

phaseThreeLoadFacets()
  .then(() => phaseThreeLoadPreferences())
  .catch((error) =>
    recordConsoleEvent(
      "warning",
      "Archive preferences unavailable",
      error.message,
    ),
  );
