import { expect, test } from "@playwright/test";
import { mockApi } from "./frontend-flow-api-fixtures.mjs";
import { fuyaoStatus, fuyaoStock } from "./fuyao-api-fixtures.mjs";

const diagnosticPaths = new Set([
  "/api/tasks/status", "/api/tasks/runs", "/api/monitor/events", "/api/system/diagnostics",
]);

test("financial and data pages load without automatic diagnostics or cleanup work", async ({ page }) => {
  const reads = [];
  const writes = [];
  const failed = new Set();
  let releaseDiagnostics;
  const held = new Promise(resolve => { releaseDiagnostics = resolve; });
  page.on("requestfailed", request => {
    const path = new URL(request.url()).pathname;
    if (diagnosticPaths.has(path) && /abort|cancel/i.test(request.failure()?.errorText || "")) failed.add(path);
  });
  await mockApi(page, { async api(url, request) {
    if (request.method() === "GET") reads.push(url.pathname);
    else writes.push(url.pathname);
    if (diagnosticPaths.has(url.pathname)) {
      await held;
      return { payload: url.pathname === "/api/tasks/status" ? { enabled: false, tasks: [] } : [] };
    }
    if (url.pathname === "/api/fuyao/stock") return { payload: fuyaoStock(url.searchParams.get("symbol")) };
    if (url.pathname === "/api/fuyao/status") return { payload: fuyaoStatus() };
    if (url.pathname === "/api/fuyao/market") return { payload: { sectors: null, sentiment: null, history: null } };
    if (url.pathname === "/api/local-data/cleanup-preview") return { payload: { total_rows: 0, user_history_rows: 0, tables: {} } };
    return null;
  } });
  try {
    await page.goto("/");
    await expect(page.locator("#stockName")).toHaveText("贵州茅台");
    await page.locator("#workspace-tab-finance").click();
    await expect(page.locator("#fuyaoFinancialFacts")).toContainText("茅台专属事实2025收入");
    expect(reads.filter(path => diagnosticPaths.has(path))).toEqual([]);
    expect(reads.filter(path => path.includes("cleanup-preview"))).toEqual([]);
    await page.locator('#primaryNavigation [data-primary-view="system"]').click();
    await expect.poll(() => reads.filter(path => diagnosticPaths.has(path)).length).toBe(4);
    await page.locator("#workspace-tab-data").click();
    await expect(page.locator("#fuyaoStatusSummary")).toContainText("7");
    await expect(page.locator("#fuyaoDataPanel")).toBeVisible();
    expect(reads.filter(path => path.includes("cleanup-preview"))).toEqual([]);
    await expect(page.locator("#runRuntimeCleanup")).toBeDisabled();
    releaseDiagnostics();
    await expect.poll(() => failed.size).toBe(4);
    await page.locator("#refreshRuntimeCleanupPreview").click();
    await expect(page.locator("#runtimeCleanupPreview")).toContainText("暂无超出保留上限的记录");
    await expect(page.locator("#runRuntimeCleanup")).toBeDisabled();
    expect(reads.filter(path => path.includes("cleanup-preview"))).toHaveLength(1);
    expect(writes).toEqual([]);
  } finally { releaseDiagnostics(); }
});
