import { expect, test } from "@playwright/test";

test("watchlist table refreshes after elapsed cache TTL when the wall clock moves backward", async ({ context, page }) => {
  let status = "watching";
  let reads = 0;
  await page.addInitScript(() => { globalThis.__ASHARE_RADAR_DISABLE_AUTOLOAD__ = true; });
  await context.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const rows = url.pathname === "/api/watchlist" ? [{
      symbol: "600519.SH", code: "600519", market: "SH", name: "缓存时钟测试",
      research_status: status, priority: "medium",
    }] : [];
    if (url.pathname === "/api/watchlist") reads += 1;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) });
  });
  await page.goto("/");
  await page.locator("#primary-nav-monitor").click();
  await page.evaluate(async () => {
    const { clearCachedJsonRequests } = await import("/static/js/api.js");
    const { loadWatchlist } = await import("/static/js/watchlist.js");
    clearCachedJsonRequests();
    globalThis.clockWatchlistState = { watchlist: [] };
    const wall = Date.now();
    Date.now = () => wall;
    await loadWatchlist(globalThis.clockWatchlistState, { ttlMs: 20 });
  });
  await expect(page.locator("#watchList .watch-status")).toBeVisible();
  await expect(page.locator("#watchList .watch-status")).toHaveText("持续观察");
  const initialReads = reads;
  status = "excluded";
  await page.evaluate(() => {
    const shifted = Date.now() - 3_600_000;
    Date.now = () => shifted;
  });
  // A short supported TTL keeps the browser test fast; Node cases cover the default 15s boundary.
  await page.waitForTimeout(30);
  await page.evaluate(async () => {
    const { loadWatchlist } = await import("/static/js/watchlist.js");
    await loadWatchlist(globalThis.clockWatchlistState, { ttlMs: 20 });
  });
  await expect(page.locator("#watchList .watch-status")).toHaveText("已排除");
  expect(reads).toBe(initialReads + 1);
  expect(await page.evaluate(() => globalThis.clockWatchlistState.watchlist[0].research_status)).toBe("excluded");
});
