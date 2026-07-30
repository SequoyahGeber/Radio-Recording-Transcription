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
