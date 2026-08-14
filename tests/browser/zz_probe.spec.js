const { test, expect } = require("@playwright/test");
const TEST_USERNAME = "phase0-admin";
const TEST_PASSWORD = "phase0-browser-password";

test("probe: incident note text survives a background refresh", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/login");
  await page.getByLabel("Username").fill(TEST_USERNAME);
  await page.getByLabel("Password").fill(TEST_PASSWORD);
  await page.getByRole("button", { name: "Sign in securely" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator("#search-status")).not.toContainText("Loading");

  await page.getByRole("button", { name: "Incident workspace" }).click();
  await page.getByRole("button", { name: "New incident" }).click();
  const editor = page.locator("#incident-editor-dialog");
  await editor.getByLabel("Title").fill("Probe incident");
  await editor.getByRole("button", { name: "Save incident" }).click();
  await expect(page.locator("#incident-detail")).toContainText("Probe incident");

  const note = page.getByPlaceholder("Add a timestamped incident note");
  await note.fill("Medic unit dispatched, ETA four minutes, staging at gate 3");
  const task = page.getByPlaceholder("Add a follow-up task");
  await task.fill("Call the duty supervisor");
  console.log("BEFORE note:", JSON.stringify(await note.inputValue()));
  console.log("BEFORE task:", JSON.stringify(await task.inputValue()));
  console.log("BEFORE focused:", await page.evaluate(() => document.activeElement?.placeholder || document.activeElement?.tagName));

  // Exactly what the 30-second timer (phase5.js:747) and every incoming
  // incident websocket event (phase5.js:682) call.
  await page.evaluate(() => window.phaseFiveLoadIncidents());
  await page.waitForTimeout(300);

  const noteAfter = await page.getByPlaceholder("Add a timestamped incident note").inputValue();
  const taskAfter = await page.getByPlaceholder("Add a follow-up task").inputValue();
  console.log("AFTER note:", JSON.stringify(noteAfter));
  console.log("AFTER task:", JSON.stringify(taskAfter));
  console.log("AFTER focused:", await page.evaluate(() => document.activeElement?.placeholder || document.activeElement?.tagName));
});
