import { expect, test } from "@playwright/test";
import { selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";
import { comparisonRun, routeComparisonFixture, SYMBOLS, UNSAFE_NAME } from "./market-scan-comparison-fixtures.mjs";

const rowButton = (page, symbol) => page.locator(`#marketScanRows [data-market-scan-compare-symbol="${symbol}"]`);

async function openMarket(page, options = {}) {
  const fixture = await routeComparisonFixture(page, options);
  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanModeOfficial").check({ force: true });
  await expect(rowButton(page, SYMBOLS.first)).toBeVisible();
  return fixture;
}

async function choose(page, symbols) {
  for (const symbol of symbols) {
    await rowButton(page, symbol).click();
    await expect(rowButton(page, symbol)).toHaveAttribute("aria-pressed", "true");
  }
}

async function runComparison(page) {
  await expect(page.locator("#marketScanCompareRun")).toBeEnabled();
  await page.locator("#marketScanCompareRun").click();
  await expect(page.locator("#marketScanCompareExport")).toBeEnabled();
  await expect(page.locator("#marketScanCompareTable table")).toBeVisible();
}

async function downloadedJson(download) {
  const stream = await download.createReadStream();
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

test("frozen comparison keeps cross-page selection bounded, filters differences and exports exact evidence", async ({ page }, testInfo) => {
  const state = await openMarket(page);
  await choose(page, [SYMBOLS.first, SYMBOLS.second]);
  await page.locator("#marketScanNext").click();
  const third = await page.locator("#marketScanRows [data-market-scan-compare-symbol]").first().getAttribute("data-market-scan-compare-symbol");
  await choose(page, [third]);
  await page.locator("#marketScanPrev").click();
  await expect(rowButton(page, SYMBOLS.first)).toHaveAttribute("aria-pressed", "true");
  await expect(rowButton(page, SYMBOLS.second)).toHaveAttribute("aria-pressed", "true");
  await choose(page, [SYMBOLS.fourth]);
  if (await rowButton(page, SYMBOLS.fifth).isEnabled()) await rowButton(page, SYMBOLS.fifth).click();
  await runComparison(page);
  expect(state.calls).toEqual([{ symbols: [SYMBOLS.first, SYMBOLS.second, third, SYMBOLS.fourth], expected_snapshot_digest: state.run.snapshot_digest }]);
  const table = page.locator("#marketScanCompareTable");
  await expect(table).toContainText(UNSAFE_NAME);
  expect(await table.locator("img").count()).toBe(0);
  expect(await page.evaluate(() => window.comparisonXss)).toBeUndefined();
  const allRows = await table.locator("tbody tr").count();
  await page.locator("#marketScanCompareDiffOnly").check();
  await expect.poll(() => table.locator("tbody tr:visible").count()).toBeLessThan(allRows);
  await page.locator("#marketScanCompareDiffOnly").uncheck();
  await expect(table.locator("tbody tr:visible")).toHaveCount(allRows);
  const nextDownload = page.waitForEvent("download");
  await page.locator("#marketScanCompareExport").click();
  const download = await nextDownload;
  expect(download.suggestedFilename()).toMatch(/\.json$/);
  expect(await downloadedJson(download)).toEqual(state.returned.at(-1));
  expect(state.calls).toHaveLength(1);
  if (testInfo.project.use.isMobile) await expectMobileComparison(page);
  await page.locator("#marketScanComparison").screenshot({ path: testInfo.outputPath("market-scan-comparison.png") });
});

test("saved-plan results expose comparison separately from the research queue", async ({ page }) => {
  const state = await openMarket(page);
  await page.locator("#marketScanFilterToggle").click();
  await page.locator("#discoveryPresetSelect").selectOption("7");
  await page.locator("#discoveryPresetApply").click();
  await expect.poll(() => state.schemeCalls.length).toBe(1);
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(3);
  await choose(page, [SYMBOLS.first, SYMBOLS.second]);
  await expect(page.locator("#discoverySelectedCount")).toHaveText("已选 0 项");
  await runComparison(page);
  expect(state.calls).toEqual([{ symbols: [SYMBOLS.first, SYMBOLS.second], expected_snapshot_digest: state.run.snapshot_digest }]);
  expect(state.returned.at(-1).ranking_basis).toBe("frozen_base");
  expect(state.returned.at(-1).audit_only).toBe(true);
  await expect(page.locator("#marketScanCompareTable")).toContainText("冻结样本2");
});

test("legacy audit comparison preserves missing values, failure reasons and risk direction", async ({ page }) => {
  const state = await openMarket(page, { legacy: true });
  await page.locator("#marketScanFilterToggle").click();
  await page.locator("#marketScanStatus").selectOption("all");
  await page.locator('#marketScanFilters button[type="submit"]').click();
  await expect(rowButton(page, SYMBOLS.missing)).toBeVisible();
  await choose(page, [SYMBOLS.first, SYMBOLS.missing]);
  await runComparison(page);
  const table = page.locator("#marketScanCompareTable");
  await expect(table).toContainText("日K缺失，不能评分");
  await expect(table).toContainText("未取得当前动作来源资格");
  await expect(table.locator('[data-comparison-field="risk"] td').nth(1)).toHaveText("不可用");
  await expect(table.locator('[data-comparison-field="score"] td').nth(1)).toHaveText("不可用");
  await expect(page.locator("#marketScanComparison")).toContainText(/越高.*风险|风险.*越高/);
  expect(state.returned.at(-1).action_source_eligible).toBe(false);
  expect(state.returned.at(-1).items[1]).toMatchObject({ status: "missing", rank: null, score: null, risk: null, confidence: null });
  await page.locator("#marketScanCompareDiffOnly").check();
  await expect(table).toContainText("日K缺失，不能评分");
  await expect(table).not.toContainText("NaN");
});

test("comparison failure and mismatched response disable export until a fresh valid retry", async ({ page }) => {
  const state = await openMarket(page);
  await choose(page, [SYMBOLS.first, SYMBOLS.second]);
  state.failNext = true;
  await page.locator("#marketScanCompareRun").click();
  await expect(page.locator("#marketScanCompareStatus")).toContainText("榜单快照已变化");
  await expect(page.locator("#marketScanCompareExport")).toBeDisabled();
  state.wrongDigestNext = true;
  await page.locator("#marketScanCompareRun").click();
  await expect.poll(() => state.calls.length).toBe(2);
  await expect(page.locator("#marketScanCompareStatus")).toContainText(/不一致|不匹配|校验|变化/);
  await expect(page.locator("#marketScanCompareExport")).toBeDisabled();
  await runComparison(page);
  expect(state.calls).toHaveLength(3);
});

test("same-id snapshot replacement and historical batch selection clear frozen comparison state", async ({ page }) => {
  const state = await openMarket(page);
  await choose(page, [SYMBOLS.first, SYMBOLS.second]);
  await runComparison(page);
  const previousResults = state.results.length;
  state.run = comparisonRun({ snapshot_digest: "b".repeat(64) });
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect.poll(() => state.results.length).toBeGreaterThan(previousResults);
  await expect(rowButton(page, SYMBOLS.first)).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator("#marketScanCompareExport")).toBeDisabled();
  await choose(page, [SYMBOLS.first, SYMBOLS.second]);
  await runComparison(page);
  expect(state.calls.at(-1).expected_snapshot_digest).toBe("b".repeat(64));
  await page.locator("#marketScanHistoryToggle").click();
  await page.locator("#marketScanHistoryRun").selectOption("41");
  await expect(page.locator("#marketScanTableWrap")).toHaveAttribute("data-market-scan-run-id", "41");
  await expect(rowButton(page, SYMBOLS.first)).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator("#marketScanCompareExport")).toBeDisabled();
  await expect(page.locator("#marketScanCompareRun")).toBeDisabled();
});

test("late comparison response cannot restore cleared selection or replace a newer result", async ({ page }) => {
  const state = await openMarket(page);
  await choose(page, [SYMBOLS.first, SYMBOLS.second]);
  state.holdNext = true;
  await page.locator("#marketScanCompareRun").click();
  await expect.poll(() => typeof state.releaseHeld).toBe("function");
  await page.locator("#marketScanCompareClear").click();
  await expect(page.locator("#marketScanCompareExport")).toBeDisabled();
  await choose(page, [SYMBOLS.first, SYMBOLS.third]);
  await runComparison(page);
  const current = structuredClone(state.returned.at(-1));
  state.releaseHeld();
  await expect.poll(() => state.returned.length).toBe(2);
  const nextDownload = page.waitForEvent("download");
  await page.locator("#marketScanCompareExport").click();
  expect(await downloadedJson(await nextDownload)).toEqual(current);
  await expect(page.locator("#marketScanCompareTable")).toContainText("冻结样本3");
  await expect(page.locator("#marketScanCompareTable")).not.toContainText("冻结样本2");
});

async function expectMobileComparison(page) {
  const layout = await page.locator("#marketScanComparison").evaluate((element) => ({
    right: element.getBoundingClientRect().right,
    viewport: document.documentElement.clientWidth,
    documentWidth: document.documentElement.scrollWidth,
    scrollWidth: document.querySelector("#marketScanCompareTable").scrollWidth,
    clientWidth: document.querySelector("#marketScanCompareTable").clientWidth,
  }));
  expect(layout.right).toBeLessThanOrEqual(layout.viewport + 1);
  expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewport + 1);
  expect(layout.scrollWidth).toBeGreaterThan(layout.clientWidth);
}
