import { expect, test } from "@playwright/test";
import { selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";
import { selectionFixture } from "./market-scan-selection-fixtures.mjs";

async function open(page) {
  const fixture = await selectionFixture(page);
  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanModeOfficial").check({ force: true });
  await expect(page.locator("#marketScanPageText")).toContainText("共 220 条");
  await page.locator("#marketScanFilterToggle").click();
  return fixture;
}

async function watchlist(page) {
  await page.locator("#marketScanUniverse").selectOption("watchlist");
  await expect(page.locator("#marketScanPageText")).toContainText("共 110 条");
  await expect(page.locator("#marketScanScopeStatus")).toContainText("当前自选 110 只");
}

test("watchlist range binds complete pagination, explanation, saved preset and export", async ({ page, isMobile }) => {
  const state = await open(page);
  await watchlist(page);
  if (isMobile) {
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: "/tmp/ashare-four-improvements-20261016/watchlist/mobile-scope.png" });
  }
  await expect(page.locator("#marketScanRows tr.market-scan-result-row").first()).toContainText("600110.SH");
  await page.locator("#marketScanNext").click();
  await expect(page.locator("#marketScanPageText")).toContainText("第 2/");
  expect(state.reads.at(-1).watchlist_digest).toBe(state.digest);
  const shell = page.locator("#marketScanScreeningWorkbench");
  await shell.locator(":scope > summary").click();
  await expect(page.locator("#marketScanScreeningEvaluation")).toContainText("筛选结果解释");
  expect(state.evaluations.at(-1)).toMatchObject({ page: 2, universe: "watchlist", watchlist_digest: state.digest });
  await page.locator("#discoveryPresetSelect").selectOption("7");
  await page.locator("#discoveryPresetApply").click();
  await expect(page.locator("#discoveryPresetFeedback")).toContainText("已应用");
  expect(state.applications.at(-1)).toMatchObject({ universe: "watchlist", watchlist_digest: state.digest });
  const download = page.waitForEvent("download");
  await page.locator("#marketScanExport").click();
  await download;
  expect(state.exports.at(-1)).toMatchObject({ universe: "watchlist", watchlist_digest: state.digest, ranking_basis: "frozen" });
  expect(JSON.parse(state.exports.at(-1).screen_spec).ranges).toMatchObject({ confidence: { min: 80 }, risk: { max: 50 } });
});

test("membership conflict retains old page and explicit refresh recovers to empty", async ({ page }) => {
  const state = await open(page);
  await watchlist(page);
  const before = await page.locator("#marketScanRows tr.market-scan-result-row").allTextContents();
  state.members = []; state.digest = "b".repeat(64);
  await page.locator("#marketScanNext").click();
  await expect(page.locator("#marketScanScopeStatus")).toContainText("自选成员已变化");
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveText(before);
  await expect(page.locator("#marketScanNext")).toBeDisabled();
  await expect(page.locator("#marketScanExport")).toBeDisabled();
  state.failRefresh = true;
  await page.locator("#marketScanScopeRefresh").click();
  await expect(page.locator("#marketScanScopeStatus")).toContainText("待确认");
  await expect(page.locator("#marketScanResultState")).toContainText("测试范围读取暂不可用");
  state.failRefresh = false;
  await page.locator("#marketScanScopeRefresh").click();
  await expect(page.locator("#marketScanScopeStatus")).toContainText("当前自选 0 只");
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(0);
  await page.locator("#marketScanUniverse").selectOption("all");
  await expect(page.locator("#marketScanPageText")).toContainText("共 220 条");
  expect(state.reads.at(-1).universe).toBeUndefined();
});

test("late watchlist response cannot overwrite a newer all-market selection", async ({ page }) => {
  const state = await open(page);
  state.hold = true;
  await page.locator("#marketScanUniverse").selectOption("watchlist");
  await expect.poll(() => Boolean(state.release)).toBe(true);
  await page.locator("#marketScanUniverse").selectOption("all");
  state.release();
  await expect(page.locator("#marketScanPageText")).toContainText("共 220 条");
  await expect(page.locator("#marketScanScopeStatus")).toHaveText("查看整个冻结批次。");
  await expect(page.locator("#marketScanRows tr.market-scan-result-row").first()).toContainText("范围候选1");
});

test("all-market saved preset export keeps its complete frozen contract", async ({ page }) => {
  const state = await open(page);
  await page.locator("#discoveryPresetSelect").selectOption("7");
  await page.locator("#discoveryPresetApply").click();
  await expect(page.locator("#discoveryPresetFeedback")).toContainText("已应用");
  const download = page.waitForEvent("download");
  await page.locator("#marketScanExport").click();
  await download;
  expect(state.exports.at(-1).ranking_basis).toBe("frozen");
  expect(state.exports.at(-1).universe).toBeUndefined();
  expect(JSON.parse(state.exports.at(-1).screen_spec).ranges).toMatchObject({ confidence: { min: 80 }, risk: { max: 50 } });
});

test("explanation membership conflict also locks the displayed scope until refresh", async ({ page }) => {
  const state = await open(page);
  await watchlist(page);
  state.digest = "b".repeat(64);
  await page.locator("#marketScanScreeningWorkbench > summary").click();
  await expect(page.locator("#marketScanScopeStatus")).toContainText("自选成员已变化");
  await expect(page.locator("#marketScanNext")).toBeDisabled();
  await expect(page.locator("#marketScanExport")).toBeDisabled();
  await expect(page.locator("#marketScanPageText")).toContainText("共 110 条");
});
