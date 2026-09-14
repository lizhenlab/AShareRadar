import { expect, test } from "@playwright/test";
import { fuyaoStock, mockFuyaoApi } from "./fuyao-api-fixtures.mjs";
import { valueStock } from "./value-research-fixtures.mjs";

test("value research follows financial periods and exposes verification actions without extra requests", async ({ page }) => {
  const record = valueStock();
  record.value_research.periods[1].source = "合成年报来源 <img src=x>";
  record.value_research.periods[1].checks[3].action = "核对现金流附注 <img src=x>";
  const state = await mockFuyaoApi(page, { api(url) {
    return url.pathname === "/api/fuyao/stock" ? { payload: record } : null;
  } });
  await openFinance(page);
  const panel = page.locator("[data-fuyao-value-research]");
  await expect(panel).toContainText("已准入评分输入覆盖 2/2");
  await expect(panel).toContainText("5%");
  await expect(panel).toContainText("50%");
  await expect(panel).toContainText("不代表分红现金或预期回报");
  await expect(panel).toContainText("不代表清算价值");
  await expect(panel).toContainText("不是置信度");
  await expect(panel.locator("[data-value-financial]")).toContainText("合成中报来源");
  await expect(panel.locator("[data-value-financial]")).not.toContainText("合成年报来源");
  await expect.poll(() => state.reads.length).toBe(2);
  await page.locator("#fuyaoPeriod").selectOption("2025-12-31|annual");
  await expect(panel.locator("[data-value-financial]")).toContainText("合成年报来源 <img src=x>");
  await expect(panel.locator("[data-value-financial]")).toContainText("2026-04-01T09:00:00+08:00");
  await expect(panel.locator("[data-value-financial]")).not.toContainText("2026-09-10T09:00:00+08:00");
  await expect(panel.locator("[data-value-valuation]")).toContainText("2026-09-11T15:30:00+08:00");
  const cashflow = panel.locator('[data-value-financial] [data-value-check="profit_cashflow_alignment"]');
  await cashflow.locator("summary").click();
  await expect(cashflow).toContainText("年度净利润为正且经营现金流为负");
  await expect(cashflow).toContainText("下一步核实：核对现金流附注 <img src=x>");
  await expect(panel.locator("img")).toHaveCount(0);
  await expect(panel).not.toContainText("/100");
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  expect(state.reads).toHaveLength(2);
  expect(state.writes).toEqual([]);
  expect(state.actions).toEqual([]);
});

test("partial negative valuation evidence and absent matching periods remain explicitly unavailable", async ({ page }) => {
  const record = valueStock();
  Object.assign(record.value_research.valuation, { available_inputs: 1, coverage: "partial", earnings_yield_pct: null,
    earnings_yield_reason: "PE TTM 为负，不能展示盈利收益率。", book_to_price_pct: null,
    book_to_price_reason: "PB MRQ 未取得有效值。" });
  Object.assign(record.value_research.valuation.checks[0], { value: -20, status: "attention",
    summary: "负 PE TTM 已计入输入覆盖。", action: "核实亏损原因及是否持续。" });
  Object.assign(record.value_research.valuation.checks[1], { value: null, status: "unavailable",
    summary: "PB MRQ 缺失。", action: "核实本地估值记录与上游权益口径。" });
  record.value_research.periods = [record.value_research.periods[0]];
  const state = await mockFuyaoApi(page, { api(url) {
    return url.pathname === "/api/fuyao/stock" ? { payload: record } : null;
  } });
  await openFinance(page);
  const panel = page.locator("[data-fuyao-value-research]");
  await expect(panel).toContainText("已准入评分输入覆盖 1/2");
  await expect(panel).toContainText("PE TTM 为负");
  await expect(panel).toContainText("暂不可用");
  await expect(panel).not.toContainText("0%");
  const missing = panel.locator('[data-value-valuation] [data-value-check="pb_mrq"]');
  await missing.locator("summary").click();
  await expect(missing).toContainText("下一步核实：核实本地估值记录与上游权益口径");
  await page.locator("#fuyaoPeriod").selectOption("2025-12-31|annual");
  const financial = panel.locator("[data-value-financial]");
  await expect(financial).toContainText("所选报告期的财务观察不足");
  await expect(financial).toContainText("下一步核实");
  await expect(financial).not.toContainText("合成中报来源");
  await expect(financial.locator("[data-value-check]")).toHaveCount(0);
  expect(state.reads).toHaveLength(2);
  expect(state.writes).toEqual([]);
});

test("value research with a different stock identity fails closed after stock selection", async ({ page }) => {
  const state = await mockFuyaoApi(page, { api(url) {
    if (url.pathname !== "/api/fuyao/stock") return null;
    const record = valueStock(url.searchParams.get("symbol"));
    if (record.symbol === "000001.SZ") record.value_research.symbol = "600519.SH";
    return { payload: record };
  } });
  await openFinance(page);
  await expect(page.locator("[data-fuyao-value-research]")).toContainText("600519.SH");
  await selectBank(page);
  await expect(page.locator("#fuyaoStockPanel")).toContainText("价值研究摘要身份、口径或数值异常");
  await expect(page.locator("[data-fuyao-value-research]")).toHaveCount(0);
  await expect(page.locator("#fuyaoStockPanel")).not.toContainText("合成中报来源");
  expect(state.writes).toEqual([]);
});

test("delayed value research cannot populate a newly selected stock with a legacy response", async ({ page }) => {
  let releaseOld;
  const held = new Promise(resolve => { releaseOld = resolve; });
  let oldRequested = false;
  let oldSettled = false;
  const settle = request => {
    const url = new URL(request.url());
    if (url.pathname === "/api/fuyao/stock" && url.searchParams.get("symbol") === "600519.SH") oldSettled = true;
  };
  page.on("requestfinished", settle);
  page.on("requestfailed", settle);
  const state = await mockFuyaoApi(page, { async api(url) {
    if (url.pathname !== "/api/fuyao/stock") return null;
    const symbol = url.searchParams.get("symbol");
    if (symbol !== "600519.SH") return { payload: fuyaoStock(symbol) };
    oldRequested = true;
    await held;
    return { payload: valueStock(symbol) };
  } });
  try {
    await openFinance(page);
    await expect.poll(() => oldRequested).toBe(true);
    await selectBank(page);
    await expect(page.locator("#fuyaoFinancialFacts")).toContainText("平安专属事实");
    releaseOld();
    await expect.poll(() => oldSettled).toBe(true);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(page.locator("[data-fuyao-value-research]")).toHaveCount(0);
    await expect(page.locator("#fuyaoFinancialFacts")).toContainText("平安专属事实");
    await expect(page.locator("#fuyaoStockPanel")).not.toContainText("合成中报来源");
    expect(state.writes).toEqual([]);
  } finally { releaseOld(); }
});

async function openFinance(page) {
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator("#workspace-tab-finance").click();
}

async function selectBank(page) {
  if (!(await page.locator("#symbolInput").isVisible())) await page.locator("#queryPanelToggle").click();
  await page.locator("#symbolInput").fill("000001");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
}
