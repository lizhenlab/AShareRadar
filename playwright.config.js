import { defineConfig, devices } from "@playwright/test";

const baseURL = "http://127.0.0.1:4173";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 15000,
  expect: { timeout: 5000 },
  fullyParallel: false,
  workers: 1,
  reporter: "line",
  outputDir: "/tmp/ashare-radar-playwright-results",
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop-chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "mobile-chromium",
      use: { ...devices["Pixel 7"] },
    },
  ],
  webServer: {
    command: "node tests/e2e/static-server.mjs",
    url: baseURL,
    reuseExistingServer: true,
    timeout: 10000,
  },
});
