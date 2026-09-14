import { expect, test } from "@playwright/test";
import { fuyaoJob, fuyaoStock, fuyaoValuationScore, mockFuyaoApi } from "./fuyao-api-fixtures.mjs";
import { workbenchPayload } from "./workbench-api-fixtures.mjs";

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

test("switching annual and interim reports preserves each observation source and acquisition time", async ({ page }) => {
  const record = fuyaoStock();
  const annual = { ...record.financials.periods[0], source: "合成年报来源 <img src=x>", fetched_at: "2026-04-01T09:00:00+08:00" };
  const interim = { ...record.financials.periods[1], period_end: "2026-06-30", period_type: "quarterly",
    source: "合成中报来源", fetched_at: "2026-09-10T09:00:00+08:00",
    metrics: [{ ...record.financials.periods[1].metrics[0], label: "合成中报收入" }] };
  record.financials.periods = [interim, annual];
  record.financials.fetched_at = "2026-09-11T09:00:00+08:00";
  const state = await mockFuyaoApi(page, { api(url) {
    return url.pathname === "/api/fuyao/stock" ? { payload: record } : null;
  } });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator("#workspace-tab-finance").click();
  const panel = page.locator("#fuyaoStockPanel");
  await expect(panel).toContainText("合成中报收入");
  await expect(panel).toContainText(interim.source);
  await expect(panel).toContainText(interim.fetched_at);
  await expect(panel).not.toContainText(record.financials.fetched_at);
  await page.locator("#fuyaoPeriod").selectOption("2025-12-31|annual");
  await expect(panel).toContainText("茅台专属事实2025收入");
  await expect(panel).toContainText(annual.source);
  await expect(panel).toContainText(annual.fetched_at);
  await expect(panel).not.toContainText(interim.source);
  await expect(panel).not.toContainText(interim.fetched_at);
  await expect(panel.locator("img")).toHaveCount(0);
  await expect(panel).toContainText("财务评分暂不生成");
  await expect(panel).toContainText("不能证明历史时点可知");
  await page.locator("#fuyaoPeriod").selectOption("2026-06-30|quarterly");
  await expect(panel).toContainText(interim.fetched_at);
  await expect(panel).not.toContainText(annual.fetched_at);
  expect(state.writes).toEqual([]);
});

test("cached valuation scores explain contributions and use TTM MRQ labels without supplier writes", async ({ page }) => {
  const record = fuyaoStock();
  record.valuation_score = fuyaoValuationScore("600519.SH", { source: "扶摇观察 <img src=x>" });
  const score = record.valuation_score;
  const state = await mockFuyaoApi(page, { workbench(symbol) {
    const payload = workbenchPayload(symbol);
    Object.assign(payload.insights.valuation, { score: score.score, score_available: true, level: "合成观察",
      pe: score.pe_ttm, pb: score.pb_mrq, source: score.source, input_basis: "fuyao_ttm_mrq",
      observation_fetched_at: score.fetched_at, score_evaluated_at: score.evaluated_at });
    return payload;
  }, api(url) { return url.pathname === "/api/fuyao/stock" ? { payload: record } : null; } });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator("#workspace-tab-finance").click();
  const assessment = page.locator("[data-fuyao-valuation-score]");
  await expect(assessment).toContainText("估值辅助分 57/100");
  for (const text of ["基础分", "55", "+8", "-6", "PE TTM", "PB MRQ", score.source, score.fetched_at, score.evaluated_at]) {
    await expect(assessment).toContainText(text);
  }
  await expect(assessment).toContainText("不代表财务健康或上涨概率");
  await expect(assessment).toContainText("不参与全市场排名");
  await assessment.locator("summary").click();
  await expect(assessment).toContainText(score.observation_digest);
  await expect(assessment.locator("img")).toHaveCount(0);
  await expect(page.locator("#valuationPanel")).toContainText("PE TTM：18");
  await expect(page.locator("#valuationPanel")).toContainText("PB MRQ：9");
  await expect(page.locator("#valuationPanel")).toContainText(score.fetched_at);
  await expect(page.locator("#valuationPanel")).not.toContainText("同行分位");
  await expect(page.locator("#fuyaoFinancialFacts")).not.toContainText("9.3%");
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  expect(state.writes).toEqual([]);
});

test("unavailable valuation scores explain exclusion and legacy stock responses clear the previous score", async ({ page }) => {
  const record = fuyaoStock();
  record.valuation_score = fuyaoValuationScore("600519.SH", { score: null, score_available: false, components: [],
    unavailable_reason: "合成估值观察已超过七日", missing_data: ["当前观察未取得有效 PE TTM"] });
  const state = await mockFuyaoApi(page, { api(url) {
    if (url.pathname !== "/api/fuyao/stock") return null;
    return { payload: url.searchParams.get("symbol") === "600519.SH" ? record : fuyaoStock(url.searchParams.get("symbol")) };
  } });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator("#workspace-tab-finance").click();
  const assessment = page.locator("[data-fuyao-valuation-score]");
  await expect(assessment).toContainText("估值辅助分暂不可用");
  await expect(assessment).toContainText("合成估值观察已超过七日");
  await expect(assessment).not.toContainText("0/100");
  if (!(await page.locator("#symbolInput").isVisible())) await page.locator("#queryPanelToggle").click();
  await page.locator("#symbolInput").fill("000001");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await expect(page.locator("#fuyaoFinancialFacts")).toContainText("平安专属事实");
  await expect(assessment).toHaveCount(0);
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
    if (url.pathname !== "/api/fuyao/stock") return null;
    const symbol = url.searchParams.get("symbol");
    const record = { ...fuyaoStock(symbol), valuation_score: fuyaoValuationScore(symbol,
      { source: symbol.startsWith("600519") ? "旧股专属估值观察" : "新股专属估值观察" }) };
    if (!symbol.startsWith("600519")) return { payload: record };
    oldRequested = true;
    await held;
    return { payload: record };
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
    await expect(page.locator("[data-fuyao-valuation-score]")).toContainText("新股专属估值观察");
    await expect(page.locator("[data-fuyao-valuation-score]")).not.toContainText("旧股专属估值观察");
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
