import { expect, test } from "@playwright/test";
import { fuyaoJob, mockFuyaoApi } from "./fuyao-api-fixtures.mjs";

const request = { kind: "financials", symbols: ["600519.SH", "000001.SZ"], index_symbols: [], period: "quarterly", limit: 4, report: "2025-4" };
const jobId = "synthetic-fuyao-job";
const listAction = (page, action, id = jobId) => page.locator(`#fuyaoJobs [data-fuyao-action="${action}"][data-fuyao-id="${id}"]`);

test("task details expose recorded input and cancellation waits before an explicit remaining-only retry", async ({ page }) => {
  const state = await mockFuyaoApi(page);
  state.jobs = [fuyaoJob("financials", { request, completed: 1, completed_symbols: ["600519.SH"],
    progress: { stage: "collecting", current: 1, total: 2, unit: "items" } })];
  await openData(page);
  await expect(page.locator("#fuyaoJobs")).toContainText("采集数据");
  expect(state.reads.filter(item => item.path.includes("/jobs/"))).toEqual([]);
  await listAction(page, "detail").click();
  const detail = page.locator("#fuyaoJobDetail");
  await expect(detail).toContainText("2025-4");
  await expect(detail).toContainText("季度报告");
  await expect(detail).toContainText("已保存股票");
  await expect(detail.locator("dd")).toContainText(["同步财报", "600519.SH、000001.SZ", "未指定成员板块", "季度报告", "600519.SH"]);
  await listAction(page, "cancel").click();
  await expect(page.locator("#fuyaoJobs")).toContainText("正在停止");
  await expect(page.locator('[data-fuyao-job="financials"]')).toBeDisabled();
  await expect(listAction(page, "retry")).toHaveCount(0);
  state.jobs[0] = { ...state.jobs[0], status: "cancelled", finished_at: "2026-09-10T02:01:00+00:00" };
  await expect(listAction(page, "retry")).toBeEnabled({ timeout: 10000 });
  await listAction(page, "retry").click();
  await expect(detail).toContainText("补做来源任务");
  await expect(detail.locator("dd").nth(1)).toHaveText("000001.SZ");
  expect(state.actions).toEqual([`/api/fuyao/jobs/${jobId}/cancel`, `/api/fuyao/jobs/${jobId}/retry`]);
  expect(state.writes).toEqual([]);
});

test("a submitted task missing from the recent list is resolved by its exact saved record", async ({ page }) => {
  const state = await mockFuyaoApi(page);
  await openData(page);
  await page.locator('[data-fuyao-job="financials"]').click();
  await expect(page.locator("#fuyaoJobs")).toContainText("进行中");
  state.records[jobId] = { ...state.jobs[0], status: "completed", completed: 2, message: "精确任务查询确认完成" };
  state.jobs = Array.from({ length: 20 }, (_, index) => fuyaoJob("financials", { id: `newer-${index}`, status: "completed" }));
  await expect(page.locator("#fuyaoJobs")).toContainText("精确任务查询确认完成", { timeout: 10000 });
  await expect(page.locator('[data-fuyao-job="financials"]')).toBeEnabled();
  expect(state.reads.some(item => item.path === `/api/fuyao/jobs/${jobId}`)).toBe(true);
  expect(state.actions).toEqual(["/api/fuyao/jobs"]);
});

test("a cancellation POST finishes after leaving the data workspace and does not start replacement work", async ({ page }) => {
  let release;
  let posted = false;
  const held = new Promise(resolve => { release = resolve; });
  const state = await mockFuyaoApi(page, { async api(url, httpRequest, current) {
    if (!url.pathname.endsWith("/cancel")) return null;
    posted = true;
    await held;
    current.jobs[0] = { ...current.jobs[0], status: "cancelling" };
    return { status: 202, payload: current.jobs[0] };
  } });
  state.jobs = [fuyaoJob("financials", { request })];
  try {
    await openData(page);
    await listAction(page, "cancel").click();
    await expect.poll(() => posted).toBe(true);
    await page.locator('#primaryNavigation [data-primary-view="research"]').click();
    release();
    await page.locator('#primaryNavigation [data-primary-view="system"]').click();
    await page.locator("#workspace-tab-data").click();
    await expect(page.locator("#fuyaoJobs")).toContainText("正在停止");
    expect(state.actions).toEqual([`/api/fuyao/jobs/${jobId}/cancel`]);
  } finally { release(); }
});

test("filtering and local refresh preserve expanded market sections and search focus", async ({ page }) => {
  const state = await mockFuyaoApi(page, { api(url) {
    if (url.pathname !== "/api/fuyao/market") return null;
    return { payload: { sectors: { source: "合成数据", payload: { rows: [
      { name: "银行", symbol: "881155.TI", category: "industry" }, { name: "计算机", symbol: "881156.TI", category: "industry" },
    ], members: { "881155.TI": ["000001.SZ"] } } }, sentiment: null, history: null } };
  } });
  await openData(page);
  const sector = page.locator('[data-fuyao-section="sectors"]');
  const members = page.locator('[data-fuyao-section="members:881155.TI"]');
  await sector.locator(":scope > summary").click();
  await members.locator("summary").click();
  const filter = page.locator("#fuyaoMarketFilter");
  await filter.fill("银行");
  await expect(filter).toBeFocused();
  await expect(sector).toHaveAttribute("open", "");
  await expect(members).toHaveAttribute("open", "");
  await expect(sector.locator("tbody")).toContainText("银行");
  await expect(sector.locator("tbody")).not.toContainText("计算机");
  await page.locator("#fuyaoReloadLocal").click();
  await expect(sector).toHaveAttribute("open", "");
  await filter.fill("");
  await expect(sector.locator("tbody")).toContainText("计算机");
  expect(state.actions).toEqual([]);
});

test("unknown byte totals and failed retry retain truthful progress and permit read-only reconciliation", async ({ page }) => {
  const state = await mockFuyaoApi(page, { api(url) {
    if (url.pathname.endsWith("/retry")) return { status: 409, payload: { detail: "合成补做尚未确认" } };
    return null;
  } });
  state.jobs = [fuyaoJob("history_full", { request: { ...request, kind: "history_full", symbols: [] }, status: "failed",
    progress: { stage: "downloading_daily", current: 1024, total: null, unit: "bytes" } })];
  await openData(page);
  await expect(page.locator("#fuyaoJobs")).toContainText("1,024 字节（总量未知）");
  await expect(page.locator("#fuyaoJobs")).not.toContainText("%");
  await listAction(page, "retry").click();
  await expect(page.locator("#fuyaoJobFeedback")).toContainText("不会自动重发");
  await listAction(page, "detail").click();
  await expect(page.locator("#fuyaoJobDetail")).toContainText("下载完整历史");
  expect(state.actions).toEqual([`/api/fuyao/jobs/${jobId}/retry`]);
});

async function openData(page) {
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator('#primaryNavigation [data-primary-view="system"]').click();
  await page.locator("#workspace-tab-data").click();
}
