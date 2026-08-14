const { test, expect } = require("@playwright/test");
const U = "phase0-admin", P = "phase0-browser-password";

async function signIn(page) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/login");
  await page.getByLabel("Username").fill(U);
  await page.getByLabel("Password").fill(P);
  await page.getByRole("button", { name: "Sign in securely" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator("#search-status")).not.toContainText("Loading");
}

test("probe: stale version after a 409 in the incident editor", async ({ page }) => {
  await signIn(page);
  await page.getByRole("button", { name: "Incident workspace" }).click();
  await page.getByRole("button", { name: "New incident" }).click();
  let editor = page.locator("#incident-editor-dialog");
  await editor.getByLabel("Title").fill("Conflict probe");
  await editor.getByRole("button", { name: "Save incident" }).click();
  await expect(page.locator("#incident-detail")).toContainText("Conflict probe");

  const id = await page.evaluate(() => phaseFiveState.incident.id);
  await page.getByRole("button", { name: "Edit" }).click();
  await editor.getByLabel("Summary").fill("Operator's careful summary of the response so far.");

  // Another operator edits the same incident while this dialog is open.
  await page.evaluate(async (id) => {
    const current = await (await fetch(`/api/incidents/${id}`)).json();
    await fetch(`/api/incidents/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...current, summary: "Other operator", version: current.version }),
    });
  }, id);

  for (const attempt of [1, 2, 3]) {
    await editor.getByRole("button", { name: "Save incident" }).click();
    await page.waitForTimeout(400);
    console.log(`attempt ${attempt}: dialogOpen=${await editor.evaluate((d) => d.open)}`,
      `versionField=${await page.locator("#incident-editor-version").inputValue()}`,
      `serverVersion=${await page.evaluate((id) => fetch(`/api/incidents/${id}`).then(r=>r.json()).then(i=>i.version), id)}`,
      `toast=${JSON.stringify(await page.locator(".toast").last().textContent().catch(()=>null))}`,
      `summaryStillTyped=${JSON.stringify(await editor.getByLabel("Summary").inputValue())}`);
  }
});

test("probe: copy report before the handoff has been generated", async ({ page }) => {
  await page.addInitScript(() => {
    window.__copied = [];
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: (t) => { window.__copied.push(t); return Promise.resolve(); } },
      configurable: true,
    });
  });
  await signIn(page);
  await page.route("**/api/shift-handoff**", async (route) => {
    await new Promise((r) => setTimeout(r, 3000));
    await route.continue();
  });
  await page.getByRole("button", { name: "Incident workspace" }).click();
  await page.getByRole("button", { name: "Shift handoff" }).click();
  await page.locator("#shift-handoff-copy").click();
  await page.waitForTimeout(200);
  console.log("content while generating:", JSON.stringify(await page.locator("#shift-handoff-content").textContent()));
  console.log("clipboard payload:", JSON.stringify(await page.evaluate(() => window.__copied)));
  console.log("toast:", JSON.stringify(await page.locator(".toast").last().textContent().catch(() => null)));
});
