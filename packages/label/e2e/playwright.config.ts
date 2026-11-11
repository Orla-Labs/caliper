// Playwright config for the caliper label e2e suite. Run it with scripts/e2e_label.sh
// (starts the server, installs what is missing) or `npx playwright test` here.

import { defineConfig, devices } from "@playwright/test";

const chromiumPath = process.env.CHROMIUM_PATH; // optional: a local Chromium build

export default defineConfig({
  testDir: "./tests",
  // Servers are cheap but share a port range (CALIPER_E2E_PORTS); one worker keeps it simple.
  workers: 1,
  fullyParallel: false,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never", outputFolder: "playwright-report" }], ["github"]]
    : [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1366, height: 860 },
    colorScheme: "light",
    acceptDownloads: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: chromiumPath ? { executablePath: chromiumPath } : {},
  },
});
