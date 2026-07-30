const { defineConfig, devices } = require("@playwright/test");
const os = require("node:os");
const path = require("node:path");

const host = "127.0.0.1";
const port = Number(process.env.RADIO_BROWSER_TEST_PORT || 8765);
const baseURL = `https://${host}:${port}`;
const fixtureRoot =
  process.env.RADIO_BROWSER_FIXTURE_ROOT ||
  path.join(os.tmpdir(), `radio-command-center-browser-${process.pid}`);

module.exports = defineConfig({
  testDir: "./tests/browser",
  outputDir: "./test-results/playwright",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "line",
  expect: {
    timeout: 10_000,
  },
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    channel: process.env.PLAYWRIGHT_BROWSER_CHANNEL || "chrome",
    headless: process.env.PLAYWRIGHT_HEADED !== "1",
    ignoreHTTPSErrors: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
  },
  webServer: {
    command: "./venv/bin/python tests/browser/start_fixture_server.py",
    url: `${baseURL}/login`,
    env: {
      RADIO_BROWSER_FIXTURE_ROOT: fixtureRoot,
      RADIO_BROWSER_TEST_HOST: host,
      RADIO_BROWSER_TEST_PORT: String(port),
    },
    ignoreHTTPSErrors: true,
    reuseExistingServer: false,
    timeout: 60_000,
    gracefulShutdown: {
      signal: "SIGTERM",
      timeout: 5_000,
    },
    stdout: "pipe",
    stderr: "pipe",
  },
});
