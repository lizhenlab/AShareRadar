import { expect, test } from "@playwright/test";
import { fuyaoJob, fuyaoStock, mockFuyaoApi } from "./fuyao-api-fixtures.mjs";

test("browsing local financial facts preserves period and explicit units without starting collection", async ({ page }) => {
  const state = await mockFuyaoApi(page);
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  expect(state.reads).toEqual([]);
  await page.locator("#workspace-tab-finance").click();
  const facts = page.locator("#fuyaoFinancialFacts");
  await expect(facts).toContainText("茅台专属事实2025收入");
  await expect(facts).toContainText(/12\.5\s*%/);
  await expect(facts).toContainText("9.3");
  await expect(facts).not.toContainText("9.3%");
  const period = page.locator("#fuyaoPeriod");
  await expect(period.locator("option")).toHaveCount(2);
  await period.selectOption({ index: 1 });
  await expect(facts).toContainText("茅台专属事实2024收入");
  await expect(facts).not.toContainText("茅台专属事实2025收入");
  await openData(page);
  await expect(page.locator("#fuyaoDataPanel")).toBeVisible();
  await expect(page.locator("#fuyaoStatusSummary")).toContainText("7");
  expect(state.writes).toEqual([]);
});

test("failed explicit collection can retry and the accepted job shows completion", async ({ page }) => {
  const state = await mockFuyaoApi(page, { api(url, request, current) {
    if (url.pathname === "/api/fuyao/jobs" && request.method() === "POST" && current.writes.length === 1) {
      return { status: 409, payload: { detail: "合成采集准入失败，请重试" } };
    }
    return null;
  } });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await openData(page);
  const button = page.locator('[data-fuyao-job="financials"]');
  await page.locator("#fuyaoSymbols").fill("600519.SH,000001.SZ");
  await button.click();
  await expect(page.locator("#fuyaoJobFeedback")).toContainText("合成采集准入失败");
  await expect(button).toBeEnabled();
  await button.click();
  await expect.poll(() => state.writes.length).toBe(2);
  expect(state.writes[1].kind).toBe("financials");
  expect(state.writes[1].symbols).toEqual(["600519.SH", "000001.SZ"]);
  await expect(page.locator("#fuyaoJobs")).toContainText("合成作业进行中");
  await expect(page.locator("#fuyaoJobs")).toContainText(/0\s*\/\s*2/);
  await expect(button).toBeDisabled();
  state.jobs = [fuyaoJob("financials", { status: "completed", completed: 2, total: 2,
    finished_at: "2026-09-10T02:01:00+00:00", message: "合成财报采集完成" })];
  await expect(page.locator("#fuyaoJobs")).toContainText("合成财报采集完成", { timeout: 10000 });
  await expect(page.locator("#fuyaoJobs")).toContainText(/2\s*\/\s*2/);
  await expect(button).toBeEnabled();
  expect(state.writes).toHaveLength(2);
});

test("a delayed financial read cannot overwrite the newly selected stock", async ({ page }) => {
  let releaseOld;
  const held = new Promise(resolve => { releaseOld = resolve; });
  let oldRequested = false;
  let oldSettled = false;
  const settleOld = request => {
    const url = new URL(request.url());
    if (url.pathname === "/api/fuyao/stock" && url.searchParams.get("symbol")?.startsWith("600519")) oldSettled = true;
  };
  page.on("requestfinished", settleOld);
  page.on("requestfailed", settleOld);
  const state = await mockFuyaoApi(page, { async api(url) {
    if (url.pathname !== "/api/fuyao/stock" || !url.searchParams.get("symbol").startsWith("600519")) return null;
    oldRequested = true;
    await held;
    return { payload: fuyaoStock("600519.SH") };
  } });
  try {
    await page.goto("/");
    await expect(page.locator("#stockName")).toHaveText("贵州茅台");
    await page.locator("#workspace-tab-finance").click();
    await expect.poll(() => oldRequested).toBe(true);
    if (!(await page.locator("#symbolInput").isVisible())) await page.locator("#queryPanelToggle").click();
    await page.locator("#symbolInput").fill("000001");
    await page.locator("#searchForm button").click();
    await expect(page.locator("#stockName")).toHaveText("平安银行");
    const facts = page.locator("#fuyaoFinancialFacts");
    await expect(facts).toContainText("平安专属事实2025收入");
    releaseOld();
    await expect.poll(() => oldSettled).toBe(true);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(facts).toContainText("平安专属事实2025收入");
    await expect(facts).not.toContainText("茅台专属事实");
    expect(state.writes).toEqual([]);
  } finally { releaseOld(); }
});

test("long provider rejection retains stock codes without widening the data page", async ({ page }) => {
  const state = await mockFuyaoApi(page);
  const symbols = Array.from({ length: 31 }, (_, index) => `${String(600000 + index)}.SH`).join(",");
  state.jobs = [fuyaoJob("valuations", { status: "degraded", completed: 30, total: 31,
    errors: [`${symbols}: 扶摇数据请求失败：business_error (code=3001)`] })];
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await openData(page);
  await expect(page.locator("#fuyaoJobs")).toContainText(symbols);
  await expect(page.locator("#fuyaoJobs")).toContainText("code=3001");
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  expect(state.writes).toEqual([]);
});

async function openData(page) {
  await page.locator('#primaryNavigation [data-primary-view="system"]').click();
  await page.locator("#workspace-tab-data").click();
}
