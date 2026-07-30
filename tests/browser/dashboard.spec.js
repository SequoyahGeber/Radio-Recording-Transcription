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
  await expect(card.locator('[data-action="toggle-card-audio"]')).toHaveCount(0);
  await card.getByText("More", { exact: true }).click();
  await expect(card.locator(".card-actions-menu")).toHaveAttribute("open", "");
  await expect(card.locator(".message-actions [role='menuitem']")).toHaveCount(4);
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
