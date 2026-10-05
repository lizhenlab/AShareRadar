import { expect, test } from "@playwright/test";
import { emitQuoteFrame, mockApi } from "./frontend-flow-api-fixtures.mjs";
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

for (const [primaryView, workspaceView] of [["system", "diagnostics"], ["system", "data"], ["monitor", "finance"]]) {
  test(`restored ${primaryView}/${workspaceView} loads visible data before any stock research`, async ({ page }) => {
    const reads = [];
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(({ primaryView, workspaceView }) => {
      localStorage.setItem("ashare-radar.workspace-preferences", JSON.stringify({version:2,preferences:{primaryView,workspaceView}}));
      localStorage.setItem("ashare-radar.stock-search-history", JSON.stringify({version:1,items:[{symbol:"000001.SZ",name:"平安银行"}]}));
    }, { primaryView, workspaceView });
    await mockApi(page, { api(url) {
      reads.push(url.pathname);
      if (url.pathname === "/api/fuyao/status") return {payload:fuyaoStatus()};
      if (url.pathname === "/api/fuyao/market") return {payload:{sectors:null,sentiment:null,history:null}};
      return null;
    } });
    await page.goto("/");
    await expect(page.locator(`#primary-nav-${primaryView}`)).toHaveAttribute("aria-current", "page");
    await expect.poll(() => reads.includes("/api/data/status")).toBe(true);
    if (primaryView === "monitor") {
      await expect(page.locator("#watchList")).toContainText("暂无自选");
      await emitQuoteFrame(page);
      await expect(page.locator("#dataStatus")).toHaveText("观察报价流已收到有效帧");
      await page.locator("#primary-nav-monitor").click();
      await emitQuoteFrame(page);
    } else {
      await expect(page.locator("#dataStatus")).toHaveText("系统维护");
      expect(reads.filter(path => ["/api/market", "/api/strong-stocks", "/api/plates", "/api/watchlist"].includes(path))).toEqual([]);
    }
    expect(reads.filter(path => path.startsWith("/api/stock/") || path === "/api/fuyao/stock")).toEqual([]);
    expect(reads.filter(path => path === "/api/advice/timeline" || path === "/api/reviews")).toEqual([]);
    await page.locator("#primary-nav-research").click();
    await expect(page.locator("#stockName")).toHaveText("平安银行");
    expect(reads.filter(path => path === "/api/stock/workbench")).toHaveLength(1);
    expect(errors).toEqual([]);
  });
}

test("reload resumes the last successful stock and clearing history restores the default", async ({ page }) => {
  let failSelection = false;
  await mockApi(page, { api(url) {
    if (failSelection && url.pathname === "/api/stock/workbench" && url.searchParams.get("symbol") === "300750.SZ") {
      return {status:503,payload:{detail:"研究数据暂不可用"}};
    }
    return null;
  } });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator("#symbolInput").fill("000001");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  failSelection = true;
  await page.locator("#symbolInput").fill("300750");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#dataStatus")).toContainText("加载失败");
  await page.reload();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await expect(page.locator("#symbolInput")).toHaveValue("000001");
  if (!(await page.locator("#stockSearchHistoryClear").isVisible())) await page.locator("#queryPanelToggle").click();
  await page.locator("#stockSearchHistoryClear").click();
  await page.reload();
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
});
