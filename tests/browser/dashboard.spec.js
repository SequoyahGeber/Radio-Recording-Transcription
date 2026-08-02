const { test, expect } = require("@playwright/test");

const TEST_USERNAME = "phase0-admin";
const TEST_PASSWORD = "phase0-browser-password";

async function signIn(page, viewport = { width: 1440, height: 900 }) {
  await page.setViewportSize(viewport);
  await page.goto("/login");
  await page.getByLabel("Username").fill(TEST_USERNAME);
  await page.getByLabel("Password").fill(TEST_PASSWORD);
  await page.getByRole("button", { name: "Sign in securely" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator("#search-status")).not.toContainText("Loading");
}

async function overlappingFeedPairs(page) {
  return page.locator(".channel-column:visible").evaluateAll((columns) => {
    const rectangles = columns.map((column) => ({
      name: column.dataset.channel || column.querySelector(".col-title")?.textContent,
      ...column.getBoundingClientRect().toJSON(),
    }));
    const overlaps = [];
    for (let left = 0; left < rectangles.length; left += 1) {
      for (let right = left + 1; right < rectangles.length; right += 1) {
        const a = rectangles[left];
        const b = rectangles[right];
        const horizontal = a.left < b.right && a.right > b.left;
        const vertical = a.top < b.bottom && a.bottom > b.top;
        if (horizontal && vertical) {
          overlaps.push([a.name, b.name]);
        }
      }
    }
    return overlaps;
  });
}

test("administrator can sign in and scan a realistic multi-feed board", async ({
  page,
}) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await signIn(page);

  await expect(page.locator(".channel-column")).toHaveCount(8);
  await expect(page.locator(".message-card")).toHaveCount(100);
  await expect(page.locator("#current-profile")).toContainText(
    "Phase Zero Administrator",
  );
  await expect(page.locator("#metric-backlog")).toHaveText("0");
  await expect(page.locator("#service-status")).toContainText("Workers healthy");
  await expect(overlappingFeedPairs(page)).resolves.toEqual([]);
  const topbarHeight = await page.locator("#topbar").evaluate(
    (topbar) => topbar.getBoundingClientRect().height,
  );
  expect(topbarHeight).toBeLessThanOrEqual(96);
  expect(pageErrors).toEqual([]);
});

test("older messages load only in the scrolled feed without moving its anchor", async ({
  page,
}) => {
  const pageErrors = [];
  const olderHistoryRequests = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/history" && url.searchParams.has("before_id")) {
      olderHistoryRequests.push(url);
    }
  });
  await signIn(page);

  const medical = page.locator('.channel-column[data-channel="Medical"]');
  const security = page.locator('.channel-column[data-channel="Security"]');
  const medicalMessages = medical.locator(".message-card");
  const securityMessages = security.locator(".message-card");
  const medicalBefore = await medicalMessages.count();
  const securityBefore = await securityMessages.count();
  const medicalScroller = medical.locator(".messages-container");

  const anchorBefore = await medicalScroller.evaluate(async (container) => {
    container.scrollTop = container.scrollHeight;
    await new Promise((resolve) => requestAnimationFrame(resolve));
    container.scrollTop = 0;
    const card = container.querySelector(".message-card[data-transcript-id]");
    return {
      id: card?.dataset.transcriptId,
      offset:
        card.getBoundingClientRect().top - container.getBoundingClientRect().top,
    };
  });

  await expect.poll(() => medicalMessages.count()).toBeGreaterThan(medicalBefore);
  await expect(securityMessages).toHaveCount(securityBefore);
  const anchorAfter = await medicalScroller.evaluate((container, id) => {
    const card = container.querySelector(`[data-transcript-id="${CSS.escape(id)}"]`);
    return {
      offset:
        card.getBoundingClientRect().top - container.getBoundingClientRect().top,
      scrollTop: container.scrollTop,
    };
  }, anchorBefore.id);

  expect(Math.abs(anchorAfter.offset - anchorBefore.offset)).toBeLessThanOrEqual(2);
  expect(anchorAfter.scrollTop).toBeGreaterThan(0);
  expect(olderHistoryRequests).toHaveLength(1);
  expect(olderHistoryRequests[0].searchParams.get("channel")).toBe("Medical");
  await expect(page.locator(".toast-danger")).toHaveCount(0);
  expect(pageErrors).toEqual([]);
});

test("compact icon controls expose plain-language tooltips", async ({ page }) => {
  await signIn(page, { width: 1280, height: 720 });
  const missingTopbarTooltips = await page
    .locator(".topbar-actions button:visible")
    .evaluateAll((buttons) =>
      buttons
        .filter((button) => button.querySelector(".button-icon"))
        .filter((button) => !(button.title || "").trim())
        .map((button) => button.id || button.getAttribute("aria-label")),
    );
  expect(missingTopbarTooltips).toEqual([]);

  const missingFeedTooltips = await page
    .locator(".channel-column:visible .col-actions button")
    .evaluateAll((buttons) =>
      buttons
        .filter((button) => !(button.title || "").trim())
        .map((button) => button.getAttribute("aria-label") || button.textContent),
    );
  expect(missingFeedTooltips).toEqual([]);
});

test("model readiness, radio terms, and transcript comparison are operator-visible", async ({
  page,
}) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await signIn(page);

  await page.getByRole("button", { name: "Show dashboard controls" }).click();
  const qualityCard = page.locator("#quality-control-card");
  await expect(qualityCard).toBeVisible();
  await expect(qualityCard.locator(".model-readiness-row")).toHaveCount(2);
  await expect(page.locator("#quality-queue-summary")).toContainText("primary");

  await page.locator("#glossary-channel").selectOption("Medical");
  await page.locator("#glossary-terms").fill("Alpha One\nNorth Gate");
  await page.getByRole("button", { name: "Save terms" }).click();
  await expect(page.locator("#glossary-save-status")).toContainText("Version");

  const recoveryCard = page.locator("#recovery-control-card");
  await expect(recoveryCard).toBeVisible();
  await page.getByRole("button", { name: "Create verified backup" }).click();
  await expect(page.locator("#backup-status")).toContainText("verified");
  await page.getByRole("button", { name: "Run restore drill" }).click();
  await expect(page.locator("#backup-status")).toContainText("Restore drill passed");

  const retryId = await page.evaluate(async () => {
    const response = await fetch("/api/history?include_suspect=true&limit=100");
    const rows = await response.json();
    return rows.find((row) => row.retry_status === "selected")?.id;
  });
  expect(retryId).toBeTruthy();
  await page.evaluate((id) => window.phaseTwoOpenDrawer(id), retryId);
  await expect(page.locator("#drawer-comparison-section")).toBeVisible();
  await expect(page.locator("#drawer-retry-transcript")).toContainText("Large V3");
  await page.getByRole("button", { name: "Use Medium result" }).click();
  await expect(page.locator("#drawer-selection-reason")).toContainText(
    "selected manually",
  );
  expect(pageErrors).toEqual([]);
});

test("archive search and overlay controls work with populated data", async ({
  page,
}) => {
  await signIn(page);

  const search = page.getByRole("searchbox", { name: "Search all feeds" });
  await search.click();
  await expect(search).toBeEditable();
  await search.fill("missing child");
  await expect(page.locator("#search-status")).toContainText("result");
  await expect(page.locator("#archive-search-view")).toBeVisible();
  await expect(page.locator("#archive-search-summary")).toContainText(
    "exact match",
  );
  await expect(
    page.locator(".message-card:visible", { hasText: "missing child" }).first(),
  ).toBeVisible();

  const dashboardHeight = await page.locator("#dashboard").evaluate(
    (dashboard) => dashboard.getBoundingClientRect().height,
  );
  await page.getByRole("button", { name: "Show dashboard controls" }).click();
  await expect(page.locator("#control-deck")).toBeVisible();
  await expect
    .poll(() =>
      page.locator("#dashboard").evaluate(
        (dashboard) => dashboard.getBoundingClientRect().height,
      ),
    )
    .toBe(dashboardHeight);
  await page.getByRole("button", { name: "Hide dashboard controls" }).click();
  await page.getByRole("button", { name: "Program console" }).click();
  await expect(page.locator("#console-panel")).toBeVisible();
  await expect(page.locator("#console-output")).toContainText(
    "Phase Zero fixture dashboard started",
  );
});

test("indexed search spans years, filters facets, paginates, and saves views", async ({
  page,
}) => {
  await signIn(page);
  const search = page.getByRole("searchbox", { name: "Search all feeds" });
  await search.click();
  await search.fill("gate");

  const searchView = page.locator("#archive-search-view");
  await expect(searchView).toBeVisible();
  await expect(page.locator(".archive-result").first()).toBeVisible();
  await expect(page.locator(".search-result-snippet mark").first()).toBeVisible();
  await expect(page.locator("#archive-search-summary")).toContainText("ms");

  const years = page.locator("#archive-search-year");
  await expect(years.locator('option[value="2024"]')).toHaveCount(1);
  await expect(years.locator('option[value="2025"]')).toHaveCount(1);
  await expect(years.locator('option[value="2026"]')).toHaveCount(1);
  await years.selectOption("2024");
  await expect(page.locator("#archive-search-summary")).toContainText(
    "exact match",
  );
  await expect(page.locator(".archive-result")).not.toHaveCount(0);
  await expect(
    page.locator(".archive-result-meta", { hasText: "2024" }).first(),
  ).toBeVisible();

  await years.selectOption("");
  await page.locator("#archive-search-page-size").selectOption("25");
  await search.fill("the");
  const initialResultCount = await page.locator(".archive-result").count();
  const moreButton = page.getByRole("button", { name: "Load more results" });
  if (await moreButton.isVisible()) {
    await moreButton.click();
    await expect
      .poll(() => page.locator(".archive-result").count())
      .toBeGreaterThan(initialResultCount);
  }

  await search.fill('"missing child"');
  await page.getByRole("button", { name: "Saved searches" }).click();
  const savedSearchDialog = page.locator("#saved-search-dialog");
  await savedSearchDialog
    .getByLabel("Saved search name")
    .fill("Missing Child Archive");
  await savedSearchDialog
    .getByRole("button", { name: "Save current search" })
    .click();
  await expect(
    savedSearchDialog.getByText("Missing Child Archive", { exact: true }),
  ).toBeVisible();

  const archiveYears = await page.evaluate(async () => {
    const response = await fetch("/api/archive/years");
    return response.json();
  });
  expect(archiveYears.total).toBe(176);
  expect(archiveYears.years.map((item) => item.year)).toEqual([
    2024, 2025, 2026,
  ]);
});

test("alert inbox acknowledges events, tests rules, and stays contained on mobile", async ({
  page,
}) => {
  await signIn(page);

  const alertsButton = page.getByRole("button", { name: "Alert inbox" });
  await expect(page.locator("#alert-active-count")).toBeVisible();
  await alertsButton.click();
  const inbox = page.locator("#alert-inbox-view");
  await expect(inbox).toBeVisible();
  await expect(page.locator(".alert-item").first()).toBeVisible();
  await expect(page.locator(".alert-explanation").first()).toContainText(
    "because rule",
  );
  await expect(page.locator(".alert-severity").first()).toBeVisible();

  const firstAlert = page.locator(".alert-item").first();
  const acknowledge = firstAlert.getByRole("button", { name: "Acknowledge" });
  await expect(acknowledge).toBeVisible();
  await acknowledge.click();
  await expect(firstAlert.locator(".alert-status")).toHaveText("acknowledged");

  await page.getByRole("button", { name: "Manage rules" }).click();
  const rulesDialog = page.locator("#alert-rule-dialog");
  await expect(rulesDialog).toBeVisible();
  await expect(page.locator(".alert-rule-row")).toHaveCount(5);
  await page.getByRole("button", { name: "Edit Missing person" }).click();
  await page
    .getByPlaceholder("Example radio transmission")
    .fill("Missing child reported at the east entrance");
  await page.getByRole("button", { name: "Test rule" }).click();
  await expect(page.locator("#alert-rule-test-result")).toContainText(
    "Missing person",
  );
  await page.getByRole("button", { name: "Close alert rules" }).click();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(inbox).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => ({
        viewport: window.innerWidth,
        document: document.documentElement.scrollWidth,
      })),
    )
    .toEqual({ viewport: 390, document: 390 });
  await expect(page.locator(".alert-item").first()).toBeVisible();
});

test("incident workspace coordinates transmissions, tasks, handoff, and health", async ({
  page,
}) => {
  await signIn(page);

  await page.getByRole("button", { name: "Incident workspace" }).click();
  const workspace = page.locator("#incident-workspace-view");
  await expect(workspace).toBeVisible();
  await page.getByRole("button", { name: "New incident" }).click();
  const editor = page.locator("#incident-editor-dialog");
  await editor.getByLabel("Title").fill("Browser north gate response");
  await editor.getByLabel("Severity").selectOption("urgent");
  await editor.getByLabel("Status").selectOption("active");
  await editor.getByLabel("Location").fill("North gate");
  await editor
    .getByLabel("Summary")
    .fill("Coordinate the responding team and retain the radio timeline.");
  await editor.getByRole("button", { name: "Save incident" }).click();

  await expect(
    page.locator(".incident-list-item", { hasText: "Browser north gate response" }),
  ).toBeVisible();
  await expect(page.locator("#incident-detail")).toContainText(
    "Browser north gate response",
  );
  await page.getByPlaceholder("Add a follow-up task").fill("Confirm gate access");
  await page.getByRole("button", { name: "Add task" }).click();
  await expect(page.locator(".incident-task")).toContainText("Confirm gate access");

  await page.getByRole("button", { name: "Back to live board" }).click();
  const card = page.locator(".message-card").first();
  await card.locator(".transcript-content").click();
  await page.getByRole("button", { name: "Add to incident" }).click();
  const picker = page.locator("#incident-link-dialog");
  await expect(picker).toBeVisible();
  await picker
    .getByRole("button", { name: /Browser north gate response/ })
    .click();
  await expect(picker).toBeHidden();
  await page.locator("#drawer-close-button").click();

  await page.getByRole("button", { name: "Incident workspace" }).click();
  await page
    .locator(".incident-list-item", { hasText: "Browser north gate response" })
    .click();
  await expect(page.locator(".incident-transcript")).toHaveCount(1);

  await page.getByRole("button", { name: "Shift handoff" }).click();
  const handoff = page.locator("#shift-handoff-dialog");
  await expect(handoff).toBeVisible();
  await expect(handoff).toContainText("Browser north gate response");
  await handoff.getByRole("button", { name: "Close shift handoff" }).click();

  await page.getByRole("button", { name: "Back to live board" }).click();
  await page.locator('[data-health-component="source"]').click();
  const health = page.locator("#health-center-view");
  await expect(health).toBeVisible();
  await expect(page.locator(".health-component")).toHaveCount(5);
  await expect(page.locator('[data-health-card="source"]')).toContainText(
    "Safe recovery",
  );
});

test("feed geometry is collision-free at every supported breakpoint", async ({
  page,
}) => {
  await signIn(page);
  const breakpoints = [
    { width: 1920, height: 1080, visibleFeeds: 8 },
    { width: 1440, height: 900, visibleFeeds: 8 },
    { width: 1280, height: 720, visibleFeeds: 8 },
    { width: 1040, height: 900, visibleFeeds: 8 },
    { width: 820, height: 900, visibleFeeds: 8 },
    { width: 700, height: 900, visibleFeeds: 8 },
    { width: 640, height: 720, visibleFeeds: 1 },
    { width: 390, height: 844, visibleFeeds: 1 },
    { width: 320, height: 720, visibleFeeds: 1 },
  ];

  for (const viewport of breakpoints) {
    await test.step(`${viewport.width} × ${viewport.height}`, async () => {
      await page.setViewportSize(viewport);
      await expect(page.locator(".channel-column:visible")).toHaveCount(
        viewport.visibleFeeds,
      );
      await expect(overlappingFeedPairs(page)).resolves.toEqual([]);
      const pageOverflow = await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      );
      expect(pageOverflow).toBeLessThanOrEqual(1);
      const clippedToolbarActions = await page
        .locator(".topbar-actions > *:visible")
        .evaluateAll((controls) =>
          controls
            .map((control) => ({
              name: control.id || control.className,
              rectangle: control.getBoundingClientRect().toJSON(),
            }))
            .filter(
              ({ rectangle }) =>
                rectangle.left < -1 || rectangle.right > window.innerWidth + 1,
            )
            .map(({ name }) => name),
        );
      expect(clippedToolbarActions).toEqual([]);
    });
  }
});

test("mobile navigator focuses one feed and preserves access to every feed", async ({
  page,
}) => {
  await signIn(page, { width: 390, height: 844 });

  const switcher = page.locator("#mobile-feed-switcher");
  await expect(switcher).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Program console" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Export matching transmissions" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  const select = page.getByLabel("Active radio feed");
  await expect(select.locator("option")).toHaveCount(8);
  const firstFeed = await select.inputValue();
  await page.getByRole("button", { name: "Next feed" }).click();
  await expect(select).not.toHaveValue(firstFeed);
  await expect(page.locator(".channel-column:visible")).toHaveCount(1);
  await expect(
    page.locator(".channel-column.mobile-feed-active:visible"),
  ).toHaveAttribute("data-channel", await select.inputValue());
});

test("cards expose one audio control and keep secondary actions in More", async ({
  page,
}) => {
  await signIn(page);

  const firstCard = page.locator(".message-card").first();
  const transcriptId = await firstCard.getAttribute("data-transcript-id");
  const card = page.locator(
    `.message-card[data-transcript-id="${transcriptId}"]`,
  );
  await expect(card.locator('[data-action="toggle-play"]')).toHaveCount(1);
  await expect(card.locator("audio")).toHaveCount(0);
  await expect(page.locator("audio#global-audio")).toHaveCount(1);
  await expect(card.locator('[data-action="toggle-card-audio"]')).toHaveCount(0);
  await card.getByText("More", { exact: true }).click();
  await expect(card.locator(".card-actions-menu")).toHaveAttribute("open", "");
  await expect(card.locator(".message-actions [role='menuitem']")).toHaveCount(5);
});

test("detail drawer supports review states, model comparison, and undo", async ({
  page,
}) => {
  await signIn(page);
  const card = page.locator(".message-card.review-state-unreviewed").first();
  const transcriptId = await card.getAttribute("data-transcript-id");
  await card.locator(".transcript-content").click();

  const drawer = page.locator("#transmission-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.locator("#drawer-title")).toContainText(transcriptId);
  await expect(drawer.locator("#drawer-transcript")).not.toBeEmpty();
  await expect(drawer.locator("#drawer-comparison-section")).toBeVisible();
  await expect(drawer.locator("#drawer-correction-section")).toBeVisible();

  await drawer.locator("#drawer-review-state").selectOption("in_review");
  await drawer
    .locator("#drawer-review-resolution")
    .fill("Confirm the responding callsign.");
  await drawer
    .locator("#drawer-notes")
    .fill("Phase Two browser workflow review.");
  await drawer.getByRole("button", { name: "Save changes" }).click();

  await expect(
    page.locator(
      `.message-card[data-transcript-id="${transcriptId}"].review-state-in-review`,
    ),
  ).toBeVisible();
  const undo = page.getByRole("button", { name: "Undo" });
  await expect(undo).toBeVisible();
  await undo.click();
  await expect(
    page.locator(
      `.message-card[data-transcript-id="${transcriptId}"].review-state-unreviewed`,
    ),
  ).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(drawer).toBeVisible();
  const mobileDrawerGeometry = await drawer.evaluate((element) => {
    const rectangle = element.getBoundingClientRect();
    return {
      left: Math.round(rectangle.left),
      right: Math.round(rectangle.right),
      viewport: window.innerWidth,
      overflow: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
  expect(mobileDrawerGeometry).toEqual({
    left: 0,
    right: 390,
    viewport: 390,
    overflow: 0,
  });
});

test("one global player provides transport, seek, speed, and volume controls", async ({
  page,
}) => {
  await signIn(page);
  const card = page.locator(".message-card").first();
  await card.getByRole("button", { name: "Play recording in global player" }).click();

  const player = page.locator("#global-player");
  await expect(player).toBeVisible();
  await expect(
    player.getByRole("button", { name: /Play recording|Pause recording/ }),
  ).toBeVisible();
  await expect(
    player.getByRole("button", { name: "Skip back 5 seconds" }),
  ).toBeVisible();
  await expect(
    player.getByRole("button", { name: "Skip forward 5 seconds" }),
  ).toBeVisible();
  await expect(player.getByLabel("Recording position")).toBeVisible();
  await expect(player.getByLabel("Playback speed")).toHaveValue("1");
  await player.getByLabel("Playback speed").selectOption("1.5");
  await expect(player.getByLabel("Playback speed")).toHaveValue("1.5");
  await expect(player.getByLabel("Recording volume")).toBeVisible();
  await player.getByRole("button", { name: "Close recording player" }).click();
  await expect(player).toBeHidden();
});

test("keyboard workflow, command palette, feed focus, and saved workspaces work", async ({
  page,
}) => {
  await signIn(page);

  await page.keyboard.press("j");
  const selected = page.locator(".message-card.keyboard-selected");
  await expect(selected).toHaveCount(1);
  await page.keyboard.press("Enter");
  await expect(page.locator("#transmission-drawer")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("#transmission-drawer")).toBeHidden();

  await page.keyboard.press("Meta+k");
  await expect(page.locator("#command-palette")).toBeVisible();
  await expect(page.locator(".command-result")).not.toHaveCount(0);
  await page.keyboard.press("Escape");

  const firstFeed = page.locator(".channel-column").first();
  const firstFeedName = await firstFeed.getAttribute("data-channel");
  await firstFeed.getByRole("button", { name: new RegExp(`Focus ${firstFeedName}`) }).click();
  await expect(page.locator(".channel-column:visible")).toHaveCount(1);

  await page.getByRole("button", { name: "Saved workspaces" }).click();
  const dialog = page.locator("#workspace-dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Workspace name").fill("Phase Two Browser Desk");
  await dialog.getByRole("button", { name: "Save current layout" }).click();
  await expect(dialog.getByText("Phase Two Browser Desk", { exact: true })).toBeVisible();
});

test("export count matches the complete streamed CSV", async ({ page }) => {
  await signIn(page);
  const exported = await page.evaluate(async () => {
    const countResponse = await fetch("/api/export/count");
    const countPayload = await countResponse.json();
    const csvResponse = await fetch(
      `/api/export.csv?through_id=${countPayload.through_id}`,
    );
    return {
      count: countPayload.count,
      throughId: countPayload.through_id,
      headerCount: Number(csvResponse.headers.get("X-Radio-Export-Count")),
      headerThroughId: Number(
        csvResponse.headers.get("X-Radio-Export-Through-Id"),
      ),
      csv: await csvResponse.text(),
    };
  });

  expect(exported.count).toBe(176);
  expect(exported.headerCount).toBe(exported.count);
  expect(exported.headerThroughId).toBe(exported.throughId);
  expect(exported.csv.trim().split("\n")).toHaveLength(exported.count + 1);
});
