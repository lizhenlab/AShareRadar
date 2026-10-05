import { expect, test } from "@playwright/test";
import { selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";
import { IMPACT_UNSAFE_NAME, impactRun, routeImpactFixture } from "./market-scan-condition-impact-fixtures.mjs";

const impact = (page, field) => page.locator(`[data-condition-impact="range.${field}"]`);

async function openMarket(page) {
  const fixture = await routeImpactFixture(page);
  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanModeOfficial").check({ force: true });
  await expect(page.locator("#marketScanRows")).toContainText("满足全部");
  await page.locator("#marketScanFilterToggle").click();
  return fixture;
}

async function applyStandard(page, minimum = "80") {
  if (!await page.locator("#marketScanAdvancedFilters").evaluate((element) => element.open)) {
    await page.locator("#marketScanAdvancedFilters > summary").click();
  }
  await page.locator("#marketScanConfidenceMin").fill(minimum);
  await page.locator("#marketScanRiskMax").fill("50");
  await page.locator('#marketScanFilters button[type="submit"]').click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(minimum === "99" ? 0 : 2);
}

async function openWorkbench(page) {
  const shell = page.locator("#marketScanScreeningWorkbench");
  if (!await shell.evaluate((element) => element.open)) await shell.locator(":scope > summary").click();
  await expect(page.locator("#marketScanConditionImpacts [data-condition-impact]")).toHaveCount(3);
}

async function applyPreset(page, id) {
  await page.locator("#discoveryPresetSelect").selectOption(String(id));
  await page.locator("#discoveryPresetApply").click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(id === 8 ? 0 : 2);
}

function assertReadOnly(state) {
  expect(state.calls.filter(({ method, path }) => method !== "GET" && !path.endsWith("/screen/evaluate") && !path.endsWith("/apply"))).toEqual([]);
  expect(state.calls.filter(({ path }) => /condition|impact/.test(path))).toEqual([]);
}

test("independent impacts preserve zero, missing and multiple failures without condition-by-condition reads", async ({ page }, testInfo) => {
  const state = await openMarket(page);
  await applyStandard(page);
  expect(state.evaluations).toHaveLength(0);
  await openWorkbench(page);
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 2/6");
  await expect(impact(page, "confidence").locator("td").nth(1)).toHaveText("+2");
  await expect(impact(page, "risk").locator("td").nth(1)).toHaveText("+1");
  await expect(impact(page, "confidence")).toContainText(IMPACT_UNSAFE_NAME);
  await expect(impact(page, "confidence")).toContainText("置信度缺失");
  await expect(impact(page, "confidence").locator(".screening-impact-examples li").nth(1)).toContainText("原值：不可用（证据缺失）");
  await expect(impact(page, "confidence")).toContainText(/原值[^\n]*0/);
  await expect(page.locator("#marketScanConditionImpacts")).not.toContainText("同时失败两条件");
  await expect(page.locator("#marketScanConditionImpacts img")).toHaveCount(0);
  expect(await page.evaluate(() => window.impactXss)).toBeUndefined();
  expect(state.evaluations).toHaveLength(1);
  expect(state.returned[0].condition_impacts.map((entry) => [entry.condition_code, entry.additional_count, entry.missing_additional_count]))
    .toEqual([["status", 0, 0], ["range.confidence", 2, 1], ["range.risk", 1, 1]]);
  assertReadOnly(state);
  if (testInfo.project.use.isMobile) await expectMobileLayout(page);
  await page.locator("#marketScanConditionImpacts").screenshot({ path: testInfo.outputPath("market-scan-condition-impact.png") });
});

test("empty standard results expose a keyboard accessible explanation without changing the applied filters", async ({ page }) => {
  const state = await openMarket(page);
  await applyStandard(page, "99");
  const explain = page.locator("#marketScanExplainEmpty");
  await expect(explain).toBeVisible();
  await expect(page.locator("#marketScanScreeningWorkbench")).not.toHaveAttribute("open", "");
  await explain.focus();
  await page.keyboard.press("Enter");
  await expect(impact(page, "confidence")).toBeVisible();
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 0/6");
  expect(state.evaluations.at(-1).spec.ranges.confidence.min).toBe(99);
  await expect(page.locator("#marketScanConfidenceMin")).toHaveValue("99");
  expect(state.evaluations).toHaveLength(1);
  assertReadOnly(state);
});

test("saved-plan explanations retain the applied complete spec through drafts and switch within the same batch", async ({ page }) => {
  const state = await openMarket(page);
  await applyPreset(page, 7);
  await expect.poll(() => state.applications.length).toBe(1);
  await openWorkbench(page);
  await expect(page.locator("#marketScanScreeningContextLabel")).toContainText("高置信方案");
  expect(state.evaluations.at(-1).spec).toMatchObject({ ranges: { confidence: { min: 80 }, risk: { max: 50 } }, sort: [{ field: "score", order: "desc" }] });
  if (!await page.locator("#marketScanAdvancedFilters").evaluate((element) => element.open)) {
    await page.locator("#marketScanAdvancedFilters > summary").click();
  }
  await page.locator("#marketScanConfidenceMin").fill("99");
  await page.locator("#marketScanScreeningRefresh").click();
  await expect.poll(() => state.evaluations.length).toBe(2);
  expect(state.evaluations.at(-1).spec.ranges.confidence.min).toBe(80);
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 2/6");
  await applyPreset(page, 8);
  await expect(page.locator("#marketScanExplainEmpty")).toBeVisible();
  await page.locator("#marketScanExplainEmpty").click();
  await expect(page.locator("#marketScanScreeningContextLabel")).toContainText("空榜方案");
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 0/6");
  expect(state.evaluations.at(-1).spec.ranges.confidence.min).toBe(99);
  assertReadOnly(state);
});

test("same-id snapshot replacement invalidates old impact evidence and rebinds to the committed batch", async ({ page }) => {
  const state = await openMarket(page);
  await applyStandard(page);
  await openWorkbench(page);
  const previousReads = state.results.length;
  const previousEvaluations = state.evaluations.length;
  state.run = impactRun({ snapshot_digest: "f".repeat(64) });
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect.poll(() => state.results.length).toBeGreaterThan(previousReads);
  await expect.poll(() => state.evaluations.length).toBeGreaterThan(previousEvaluations);
  await expect(page.locator("#marketScanScreeningEvidence")).toContainText("ffffffffffff");
  expect(state.returned.at(-1).evidence.snapshot_digest).toBe("f".repeat(64));
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 2/6");
  assertReadOnly(state);
});

test("wrong batch, spec, count or example responses are rejected and cannot leave actionable impact content", async ({ page }) => {
  const state = await openMarket(page);
  await applyStandard(page);
  await openWorkbench(page);
  const corruptions = [
    (payload) => { payload.evidence.snapshot_digest = "f".repeat(64); },
    (payload) => { payload.spec.sort = [{ field: "score", order: "desc" }]; },
    (payload) => { payload.condition_impacts[1].matched_without_condition += 1; },
    (payload) => { payload.condition_impacts[1].examples[0].missing = true; },
  ];
  for (const mutate of corruptions) {
    state.mutateNext = mutate;
    const expected = state.evaluations.length + 1;
    await page.locator("#marketScanScreeningRefresh").click();
    await expect.poll(() => state.evaluations.length).toBe(expected);
    await expect(page.locator("#marketScanScreeningEvaluation .error")).toBeVisible();
    await expect(page.locator("#marketScanConditionImpacts [data-condition-impact]")).toHaveCount(0);
  }
  await page.locator("#marketScanScreeningRefresh").click();
  await expect(impact(page, "confidence")).toBeVisible();
  assertReadOnly(state);
});

test("a late explanation cannot overwrite a newer applied filter or restore obsolete examples", async ({ page }) => {
  const state = await openMarket(page);
  await applyStandard(page);
  state.holdNext = true;
  await page.locator("#marketScanScreeningWorkbench > summary").click();
  await expect.poll(() => typeof state.releaseHeld).toBe("function");
  await applyStandard(page, "99");
  await expect.poll(() => state.evaluations.length).toBe(2);
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 0/6");
  state.releaseHeld();
  await expect.poll(() => state.returned.length).toBe(2);
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 0/6");
  await expect(impact(page, "confidence").locator("td").nth(1)).toHaveText("+4");
  await expect(page.locator("#marketScanScreeningSpec")).toContainText("99");
  assertReadOnly(state);
});

test("a late standard-results producer cannot overwrite a saved plan committed in the same batch", async ({ page }) => {
  const state = await openMarket(page);
  state.holdResultNext = true;
  await page.locator("#marketScanAdvancedFilters > summary").click();
  await page.locator("#marketScanConfidenceMin").fill("99");
  await page.locator("#marketScanRiskMax").fill("50");
  await page.locator('#marketScanFilters button[type="submit"]').click();
  await expect.poll(() => typeof state.releaseHeldResult).toBe("function");
  await applyPreset(page, 7);
  await openWorkbench(page);
  await expect(page.locator("#marketScanScreeningContextLabel")).toContainText("高置信方案");
  const previousReturns = state.returnedResults.length;
  state.releaseHeldResult();
  await expect.poll(() => state.returnedResults.length).toBe(previousReturns + 1);
  const lateResponse = await state.heldResultRequest.response();
  if (lateResponse) await lateResponse.finished();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(2);
  await expect(page.locator("#marketScanScreeningContextLabel")).toContainText("高置信方案");
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 2/6");
  await expect(page.locator("#marketScanExplainEmpty")).toBeHidden();
  const reads = state.evaluations.length;
  await page.locator("#marketScanScreeningRefresh").click();
  await expect.poll(() => state.evaluations.length).toBe(reads + 1);
  expect(state.evaluations.at(-1).spec.ranges.confidence.min).toBe(80);
  expect(state.evaluations.at(-1).spec.sort).toEqual([{ field: "score", order: "desc" }]);
  assertReadOnly(state);
});

test("a late saved-plan producer cannot replace newer ordinary filters in the same batch", async ({ page }) => {
  const state = await openMarket(page);
  state.holdPresetNext = true;
  await page.locator("#discoveryPresetSelect").selectOption("7");
  await page.locator("#discoveryPresetApply").click();
  await expect.poll(() => typeof state.releaseHeldPreset).toBe("function");
  await page.locator("#marketScanSort").selectOption("rank");
  await page.locator("#marketScanOrder").selectOption("asc");
  await applyStandard(page, "99");
  await page.locator("#marketScanExplainEmpty").click();
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 0/6");
  state.releaseHeldPreset();
  await expect.poll(() => state.returnedPresets.length).toBe(1);
  const lateResponse = await state.heldPresetRequest.response();
  if (lateResponse) await lateResponse.finished();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(0);
  await expect(page.locator("#marketScanScreeningContextLabel")).toContainText("普通榜单筛选");
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 0/6");
  const reads = state.evaluations.length;
  await page.locator("#marketScanScreeningRefresh").click();
  await expect.poll(() => state.evaluations.length).toBe(reads + 1);
  expect(state.evaluations.at(-1).spec.ranges.confidence.min).toBe(99);
  expect(state.evaluations.at(-1).spec.sort).toEqual([{ field: "rank", order: "asc" }]);
  assertReadOnly(state);
});

async function expectMobileLayout(page) {
  const layout = await page.locator("#marketScanConditionImpacts").evaluate((element) => ({
    right: element.getBoundingClientRect().right, width: document.documentElement.clientWidth,
    documentWidth: document.documentElement.scrollWidth,
  }));
  expect(layout.right).toBeLessThanOrEqual(layout.width + 1);
  expect(layout.documentWidth).toBeLessThanOrEqual(layout.width + 1);
  const scrollArea = page.locator("#marketScanConditionImpacts .market-scan-screening-table-wrap");
  if (await scrollArea.evaluate((element) => element.scrollWidth > element.clientWidth)) {
    await scrollArea.focus();
    await page.keyboard.press("ArrowRight");
    await expect.poll(() => scrollArea.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
    await scrollArea.evaluate((element) => { element.scrollLeft = 0; });
  }
}
