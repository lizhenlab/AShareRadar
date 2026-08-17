import { expect, test } from "@playwright/test";
import {
  dailyKlines,
  delay,
  emitQuoteFrame,
  expectPrimaryView,
  minuteAnalysisPayload,
  minuteKlines,
  mockApi,
  paperTradingDashboard,
  primaryViewButton,
  selectPrimaryView,
  workbenchPayload,
} from "./frontend-flow-api-fixtures.mjs";
test("primary navigation separates research, market, review, and monitoring workspaces", async ({ page }) => {
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/market-scans/latest") return { payload: null };
      return null;
    },
  });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  const primaryButtons = page.locator("#primaryNavigation button[data-primary-view]");
  await expect(primaryButtons).toHaveCount(4);
  await expect(primaryButtons).toHaveText(["个股研究", "全市场选股", "复盘工具", "自选监控"]);
  await expectPrimaryView(page, "research");
  await expect(page.locator("#stockWorkbench")).toBeVisible();
  await expect(page.locator(".query-panel")).toBeVisible();
  await expect(page.locator("#workspace-panel-overview")).toBeVisible();
  await expect(page.locator(".control-panel")).toBeHidden();
  await expect(page.locator(".side-column")).toBeHidden();
  await selectPrimaryView(page, "market");
  await expect(page.locator("#stockWorkbench")).toBeHidden();
  await expect(page.locator(".query-panel")).toBeHidden();
  await expect(page.locator("#workspace-panel-market-scan")).toBeVisible();
  await expect(page.locator("#workspace-panel-overview")).toBeHidden();
  await selectPrimaryView(page, "review");
  await expect(page.locator(".query-panel")).toBeVisible();
  await expect(page.locator("#stockWorkbench")).toBeHidden();
  await expect(page.locator("#workspace-panel-replay")).toBeVisible();
  await expect(page.locator("#workspace-tab-replay")).toBeVisible();
  await expect(page.locator("#workspace-tab-paper")).toBeVisible();
  await expect(page.locator("#workspace-tab-tools")).toBeVisible();
  await expect(page.locator("#workspace-tab-data")).toBeVisible();
  await page.locator("#workspace-tab-tools").click();
  await expect(page.locator("#workspace-panel-tools")).toBeVisible();

  await selectPrimaryView(page, "monitor");
  await expect(page.locator(".query-panel")).toBeHidden();
  await expect(page.locator(".workspace")).toBeHidden();
  await expect(page.locator(".control-panel")).toBeVisible();
  await expect(page.locator(".side-column")).toBeVisible();

  await selectPrimaryView(page, "research");
  await expect(page.locator("#stockWorkbench")).toBeVisible();
  await expect(page.locator(".query-panel")).toBeVisible();
  await expect(page.locator("#workspace-panel-overview")).toBeVisible();
});
test("layout controls prioritize primary content across desktop and mobile workspaces", async ({ page }, testInfo) => {
  const mobileProject = Boolean(testInfo.project.use.isMobile);
  await page.setViewportSize(mobileProject ? { width: 390, height: 844 } : { width: 1440, height: 900 });
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/market-scans/latest") return { payload: null };
      return null;
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");

  if (mobileProject) {
    await page.locator("#queryPanelToggle").click();
    await expect(page.locator("#searchForm")).toBeHidden();
    await expect(page.locator("body")).toHaveClass(/query-panel-collapsed/);
    await page.locator("#queryPanelToggle").click();
    await expect(page.locator("#searchForm")).toBeVisible();

    await selectPrimaryView(page, "review");
    await expect(page.locator("#searchForm")).toBeHidden();
    await expect(page.locator(".advice-review-panel")).toHaveClass(/layout-panel-collapsed/);
    await page.locator(".advice-review-panel .layout-collapse-toggle").click();
    await page.locator("#reviewAdviceId").evaluate((select) => {
      select.innerHTML = '<option value="">请选择</option><option value="1">600519 贵州茅台 · 2026-07-30 · 观察</option><option value="2">000001 平安银行 · 2026-07-29 · 等待</option>';
    });
    await page.locator("#reviewAdviceSearch").fill("茅台");
    await expect(page.locator("#reviewAdviceSearchFeedback")).toHaveText("找到 1 条匹配快照");
    await expect(page.locator('#reviewAdviceId option[value="2"]')).toHaveJSProperty("hidden", true);

    await selectPrimaryView(page, "monitor");
    await expect(page.locator("#watchForm")).toBeHidden();
    const monitorOrder = await page.evaluate(() => ({
      side: document.querySelector(".side-column").getBoundingClientRect().top,
      notice: document.querySelector(".notice").getBoundingClientRect().top,
    }));
    expect(monitorOrder.side).toBeLessThan(monitorOrder.notice);
    await page.locator("#watchFormToggle").click();
    await expect(page.locator("#watchForm")).toBeVisible();
    return;
  }
  const before = await page.locator(".layout").evaluate((element) => getComputedStyle(element).gridTemplateColumns);
  await page.locator("#queryPanelToggle").click();
  const after = await page.locator(".layout").evaluate((element) => getComputedStyle(element).gridTemplateColumns);
  expect(before).not.toBe(after);
  expect(Number.parseFloat(after)).toBeLessThan(Number.parseFloat(before));
  await page.locator("#queryPanelToggle").click();

  await page.locator("#minuteAnalysis").evaluate((element) => { element.dataset.availability = "unavailable"; });
  await expect(page.locator("#minuteChartPane")).toBeVisible();
  const chartWidths = await page.locator("#chartWorkspace").evaluate((workspace) => ({
    daily: workspace.querySelector("#dailyChartPane").getBoundingClientRect().width,
    grid: workspace.querySelector(".chart-grid").getBoundingClientRect().width,
  }));
  expect(chartWidths.daily).toBeGreaterThan(chartWidths.grid * 0.95);
});

test("paper trading freezes a review plan and renders deterministic simulated fills", async ({ page }, testInfo) => {
  let strategyAdded = false;
  let simulated = false;
  const writes = [];
  const runRequests = [];
  await mockApi(page, {
    api(url, request) {
      if (url.pathname === "/api/reviews/summary") {
        return { payload: { generated_at: "2026-07-15 10:00:00", total_plan_count: 1, pending_count: 1,
          evaluated_count: 0, insufficient_count: 0, favorable_count: 0, unfavorable_count: 0, ambiguous_count: 0,
          target_hit_count: 0, stop_hit_count: 0, favorable_rate_pct: null, conclusion_counts: { pending: 1 } } };
      }
      if (url.pathname === "/api/reviews/due") return { payload: [] };
      if (url.pathname === "/api/reviews" && !url.searchParams.has("symbol")) {
        return { payload: url.searchParams.get("offset") ? [] : [{ plan: reviewPlan(), latest_evaluation: null }] };
      }
      if (url.pathname === "/api/paper-trading/strategies" && request.method() === "POST") {
        writes.push(request.postDataJSON());
        strategyAdded = true;
        return { payload: paperStrategy(), status: 201 };
      }
      if (url.pathname === "/api/paper-trading/run" && request.method() === "POST") {
        simulated = true;
        runRequests.push(request.postDataJSON());
        const dashboard = simulatedPaperDashboard();
        return { payload: { run_id: 1, as_of: "2026-07-03 15:15:00", execution_count: 2, closed_count: 1, data_unavailable_count: 0, dashboard } };
      }
      if (url.pathname === "/api/paper-trading") {
        return { payload: strategyAdded ? paperTradingDashboard({ strategies: [paperStrategy()] }) : paperTradingDashboard() };
      }
      return null;
    },
  });

  await page.goto("/");
  await selectPrimaryView(page, "review");
  await page.locator("#workspace-tab-paper").click();
  if (testInfo.project.use.isMobile) {
    await page.locator(".paper-strategy-create-panel .layout-collapse-toggle").click();
  }
  await expect(page.locator("#paperReviewPlan option[value='10']")).toHaveCount(1);
  await page.locator("#paperReviewPlan").selectOption("10");
  await page.locator("#paperAllocationPct").fill("25");
  await page.locator("#paperStrategyForm button[type='submit']").click();
  await expect.poll(() => writes).toEqual([{
    plan_id: 10, expected_plan_revision: 1, expected_plan_payload_digest: "a".repeat(64),
    allocation_pct: 25, priority: 0, entry_expiry_sessions: 5,
  }]);
  await expect(page.locator("#paperStrategyList")).toContainText("等待入场");

  await page.locator("#paperRunAsOf").fill("2026-07-03");
  await page.locator("#runPaperTrading").click();
  await expect.poll(() => simulated).toBe(true);
  await expect.poll(() => runRequests).toEqual([{
    as_of: "2026-07-03T23:59:59+08:00",
    cost_profile: "base",
    benchmark_symbol: "000300.SH",
  }]);
  await expect(page.locator("#paperStrategyList")).toContainText("已平仓");
  await expect(page.locator("#paperTradeList")).toContainText("目标价触达");
  await expect(page.locator("#paperEquityChart .paper-benchmark-line")).toHaveCount(1);
  await expect(page.locator("#paperEquityChart .paper-trade-marker")).toHaveCount(2);
  await expect(page.locator("#paperEventList")).toContainText("buy_filled");
  await expect(page.locator("#paperRunMetadata")).toContainText("paper-review-plan-v2");
  await expect(page.locator("#paperExportJson")).toHaveAttribute("href", "/api/paper-trading/runs/1/export.json");
  await expect(page.locator("#paperTradingFeedback")).toContainText("生成 2 笔成交");
});


test("daily and minute chart controls redraw locally and preserve responsive state", async ({ page }, testInfo) => {
  const mobileProject = Boolean(testInfo.project.use.isMobile);
  await page.setViewportSize(mobileProject ? { width: 390, height: 844 } : { width: 1440, height: 900 });
  const apiRequests = [];
  const minuteRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith("/api/")) return;
    apiRequests.push(`${url.pathname}${url.search}`);
    if (url.pathname === "/api/stock/minute-analysis") minuteRequests.push(url.searchParams.get("interval"));
  });
  await mockApi(page, {
    workbench(symbol) {
      return workbenchPayload(symbol, { chartMarks: true, withKlines: true });
    },
    api(url) {
      if (url.pathname !== "/api/stock/minute-analysis") return null;
      return { payload: minuteAnalysisPayload(url.searchParams.get("interval") || "5m") };
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect.poll(() => minuteRequests).toEqual(["5m"]);
  await expect(page.locator("#dailyRange60")).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => canvasHasInk(page.locator("#klineCanvas"))).toBe(true);
  await assertChartWorkspaceFits(page);

  if (mobileProject) {
    await expect(page.locator("#dailyChartPane")).toBeVisible();
    await expect(page.locator("#minuteChartPane")).toBeHidden();
    await expect(page.locator("#mobileChartDaily")).toHaveAttribute("aria-pressed", "true");
  } else {
    await expect(page.locator("#dailyChartPane")).toBeVisible();
    await expect(page.locator("#minuteChartPane")).toBeVisible();
  }

  for (const range of [20, 60, 120, 240]) {
    const before = apiRequests.length;
    await page.locator(`#dailyRange${range}`).click();
    await expect(page.locator(`#dailyRange${range}`)).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("#dailyChartStatus")).toContainText(`${range}日`);
    expect(apiRequests).toHaveLength(before);
  }
  const beforeOverlays = apiRequests.length;
  await page.locator("#dailyMa5Toggle").uncheck();
  await page.locator("#dailyMa20Toggle").uncheck();
  await expect(page.locator("#dailyMa5Toggle")).not.toBeChecked();
  await expect(page.locator("#dailyMa20Toggle")).not.toBeChecked();
  expect(apiRequests).toHaveLength(beforeOverlays);

  if (await page.locator("#mobileChartMinute").isVisible()) {
    await page.locator("#mobileChartMinute").click();
    await expect(page.locator("#minuteChartPane")).toBeVisible();
    await expect(page.locator("#dailyChartPane")).toBeHidden();
  }
  await expect.poll(() => canvasHasInk(page.locator("#minuteKlineCanvas"))).toBe(true);
  const beforeSameInterval = minuteRequests.length;
  await page.locator("#minuteInterval5m").click();
  await page.waitForTimeout(50);
  expect(minuteRequests).toHaveLength(beforeSameInterval);

  await page.locator("#minuteInterval15m").click();
  await expect.poll(() => minuteRequests).toEqual(["5m", "15m"]);
  await expect(page.locator("#minuteInterval15m")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#minuteChartStatus")).toContainText("15分钟");
  await expect(page.locator("#minuteKlineCanvas")).toHaveAttribute(
    "aria-label",
    "15分钟分时K线走势图，可用左右方向键逐根查看"
  );
  await expect(page.locator("#minuteAnalysisPeriod")).toHaveText("15分钟区间 / 盘中强弱");
  await expect.poll(() => canvasHasInk(page.locator("#minuteKlineCanvas"))).toBe(true);

  await page.locator("#minuteInterval30m").click();
  await expect.poll(() => minuteRequests).toEqual(["5m", "15m", "30m"]);
  await expect(page.locator("#minuteChartStatus")).toContainText("不可用");
  await expect(page.locator("#minuteChartPane")).toHaveAttribute("data-availability", "unavailable");
  await expect.poll(() => canvasHasInk(page.locator("#minuteKlineCanvas"))).toBe(false);

  await page.locator("#minuteInterval60m").click();
  await expect.poll(() => minuteRequests).toEqual(["5m", "15m", "30m", "60m"]);
  await expect(page.locator("#minuteChartStatus")).toContainText("降级");
  await expect(page.locator("#minuteChartPane")).toHaveAttribute("data-availability", "degraded");
  await expect.poll(() => canvasHasInk(page.locator("#minuteKlineCanvas"))).toBe(true);

  const beforeResize = apiRequests.length;
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator("#dailyChartPane")).toBeVisible();
  await expect(page.locator("#minuteChartPane")).toBeVisible();
  await expect(page.locator("#dailyRange240")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#minuteInterval60m")).toHaveAttribute("aria-pressed", "true");
  await assertChartWorkspaceFits(page);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator("#mobileChartMinute").click();
  await expect(page.locator("#dailyChartPane")).toBeHidden();
  await expect(page.locator("#minuteChartPane")).toBeVisible();
  await expect(page.locator("#minuteInterval60m")).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => canvasHasInk(page.locator("#minuteKlineCanvas"))).toBe(true);
  await assertChartWorkspaceFits(page);

  await page.setViewportSize({ width: 320, height: 800 });
  await expect(page.locator("#minuteChartPane")).toBeVisible();
  await assertChartWorkspaceFits(page);
  expect(apiRequests).toHaveLength(beforeResize);
});

test("desktop chart inspectors expose exact values, crosshairs, and keyboard movement", async ({ page }, testInfo) => {
  test.skip(Boolean(testInfo.project.use.isMobile), "covered by the mobile tap regression");
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockApi(page, {
    workbench(symbol) {
      return workbenchPayload(symbol, { withKlines: true });
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect.poll(() => canvasHasInk(page.locator("#klineCanvas"))).toBe(true);
  await expect.poll(() => canvasHasInk(page.locator("#minuteKlineCanvas"))).toBe(true);

  const dailyRows = dailyKlines(240).slice(-60);
  const dailyCanvas = page.locator("#klineCanvas");
  const dailyInspector = page.locator("#dailyChartInspector");
  const dailyValues = page.locator("#dailyChartInspectorValues");
  await pointAtChartRow(page, dailyCanvas, 0, dailyRows.length);
  await assertChartInspection(dailyInspector, dailyValues, dailyRows[0], "日线");
  await assertCrosshairPosition(dailyInspector, dailyCanvas, 0, dailyRows.length);

  await leaveCanvas(page, dailyCanvas);
  await expect(dailyInspector).toBeHidden();
  await expect(dailyInspector).toHaveAttribute("aria-hidden", "true");
  await expect(dailyValues).toBeEmpty();

  await dailyCanvas.focus();
  await dailyCanvas.press("ArrowRight");
  await assertChartInspection(dailyInspector, dailyValues, dailyRows[0], "日线");
  const firstKeyboardX = await crosshairCoordinate(dailyInspector, ".chart-crosshair-x", "left");
  await dailyCanvas.press("ArrowRight");
  await assertChartInspection(dailyInspector, dailyValues, dailyRows[1], "日线");
  const secondKeyboardX = await crosshairCoordinate(dailyInspector, ".chart-crosshair-x", "left");
  expect(secondKeyboardX).toBeGreaterThan(firstKeyboardX);
  await dailyCanvas.press("ArrowLeft");
  await assertChartInspection(dailyInspector, dailyValues, dailyRows[0], "日线");
  await page.locator("#dailyRange60").focus();
  await expect(dailyInspector).toBeHidden();
  await expect(dailyValues).toBeEmpty();

  const minuteRows = minuteKlines("5m", 24);
  const minuteCanvas = page.locator("#minuteKlineCanvas");
  const minuteInspector = page.locator("#minuteChartInspector");
  const minuteValues = page.locator("#minuteChartInspectorValues");
  await pointAtChartRow(page, minuteCanvas, 5, minuteRows.length);
  await assertChartInspection(minuteInspector, minuteValues, minuteRows[5], "5分钟");
  await assertCrosshairPosition(minuteInspector, minuteCanvas, 5, minuteRows.length);
  await leaveCanvas(page, minuteCanvas);
  await expect(minuteInspector).toBeHidden();
  await expect(minuteValues).toBeEmpty();
});

test("mobile chart inspectors are reachable by tap without horizontal overflow", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.use.isMobile, "covered by the desktop pointer regression");
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, {
    workbench(symbol) {
      return workbenchPayload(symbol, { withKlines: true });
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  const dailyCanvas = page.locator("#klineCanvas");
  await expect.poll(() => canvasHasInk(dailyCanvas)).toBe(true);
  await tapChart(page, dailyCanvas, 20, 60);
  await expect(page.locator("#dailyChartInspector")).toBeVisible();
  await expect(page.locator("#dailyChartInspectorValues")).toContainText("日线");
  await expect(page.locator("#dailyChartInspectorValues")).toContainText("量");
  await assertChartWorkspaceFits(page);

  await page.locator("#mobileChartMinute").click();
  const minuteCanvas = page.locator("#minuteKlineCanvas");
  await expect(minuteCanvas).toBeVisible();
  await expect.poll(() => canvasHasInk(minuteCanvas)).toBe(true);
  await tapChart(page, minuteCanvas, 8, 24);
  await expect(page.locator("#minuteChartInspector")).toBeVisible();
  await expect(page.locator("#minuteChartInspectorValues")).toContainText("2026-07-15");
  await expect(page.locator("#minuteChartInspectorValues")).toContainText("5分钟");
  await assertChartWorkspaceFits(page);

  await page.setViewportSize({ width: 320, height: 800 });
  await expect(minuteCanvas).toBeVisible();
  await expect.poll(() => canvasHasInk(minuteCanvas)).toBe(true);
  await tapChart(page, minuteCanvas, 12, 24);
  await expect(page.locator("#minuteChartInspector")).toBeVisible();
  await assertChartWorkspaceFits(page);
});

test("research activity merges local records, filters each type, and keeps partial data", async ({ page }) => {
  const timeline = [
    {
      id: 301,
      action: "继续观察",
      confidence: 72,
      reason: "等待量价确认",
      market_time: "2026-07-15 10:25:00",
      created_at: "2026-07-15 10:30:00",
      updated_at: "2026-07-15 10:30:00",
      trend_label: "震荡偏强",
      trend_score: 64,
      risk_level: "中等",
      comparison_status: "comparable",
      has_changes: true,
      changes: [{ category: "trend", field: "trend_score", before: 60, after: 64 }],
    },
  ];
  const alertEvents = [
    {
      id: 202,
      rule_id: 12,
      name: "突破提醒",
      event_type: "向上突破",
      message: "价格突破关键压力位",
      price: 102.5,
      threshold: 102,
      change_pct: 1.25,
      created_at: "2026-07-15 11:30:00",
    },
  ];
  const notes = [
    {
      id: 101,
      note_type: "午后复盘",
      content: "关注成交量能否持续放大",
      price: 101.2,
      trade_date: "2026-07-15",
      created_at: "2026-07-15 09:00:00",
      updated_at: "2026-07-15 12:30:00",
    },
  ];
  await mockApi(page, {
    async timeline() {
      await delay(100);
      return timeline;
    },
    workbench(symbol) {
      const payload = workbenchPayload(symbol);
      return {
        ...payload,
        alert_events: alertEvents.map((item) => ({ ...item, symbol: payload.symbol })),
        notes: notes.map((item) => ({ ...item, symbol: payload.symbol })),
        local_data_warnings: [{ component: "notes", message: "本地笔记读取失败" }],
      };
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator('#quickList button[data-symbol="000001"]').click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await selectPrimaryView(page, "review");
  await page.locator("#workspace-tab-tools").click();

  const activity = page.locator("#researchActivity");
  const items = activity.locator(".research-activity-item");
  await expect(activity).toHaveAttribute("aria-busy", "false");
  await expect(items).toHaveCount(3);
  await expect.poll(() => items.evaluateAll((nodes) => nodes.map((node) => node.dataset.kind))).toEqual([
    "note",
    "alert",
    "advice",
  ]);
  await expect(items.nth(0)).toContainText("2026-07-15 12:30:00");
  await expect(items.nth(0)).toContainText("关注成交量能否持续放大");
  await expect(items.nth(1)).toContainText("2026-07-15 11:30:00");
  await expect(items.nth(1)).toContainText("价格突破关键压力位");
  await expect(items.nth(2)).toContainText("2026-07-15 10:30:00");
  await expect(items.nth(2)).toContainText("等待量价确认");
  await expect(activity).toContainText("部分本地记录暂不可用");
  await expect(activity).toContainText("笔记：本地笔记读取失败");

  const filters = page.locator("#researchActivityFilters button");
  await expect(filters).toHaveCount(4);
  await assertActivityFilterState(filters, "all");
  for (const kind of ["advice", "alert", "note"]) {
    await page.locator(`#researchActivityFilters button[data-activity-filter="${kind}"]`).click();
    await assertActivityFilterState(filters, kind);
    await expect(items).toHaveCount(1);
    await expect(items).toHaveAttribute("data-kind", kind);
    await expect(activity).toContainText("部分本地记录暂不可用");
  }
  await page.locator('#researchActivityFilters button[data-activity-filter="all"]').click();
  await assertActivityFilterState(filters, "all");
  await expect(items).toHaveCount(3);
  await expect(items.filter({ hasText: "价格突破关键压力位" })).toHaveCount(1);
  await expect(items.filter({ hasText: "等待量价确认" })).toHaveCount(1);
});

test("advice timeline shows snapshot changes without narrow-screen overflow", async ({ page }, testInfo) => {
  if (testInfo.project.use.isMobile) await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, {
    timeline: [
      {
        id: 2,
        action: "控制风险",
        confidence: 74,
        market_time: "2026-07-15 14:55:00",
        created_at: "2026-07-15 15:01:00",
        updated_at: "2026-07-15 15:03:00",
        trend_label: "震荡",
        trend_score: 61,
        risk_level: "偏高",
        data_quality_level: "良好",
        data_quality_score: 82,
        data_quality_source: "日线收盘快照",
        conclusion_basis: "analysis_action_advice",
        snapshot_contract_version: "2",
        rule_version: "rules-7",
        model_version: "model-3",
        comparison_status: "comparable",
        has_changes: true,
        changes: [
          { category: "action", field: "action", before: "观察", after: "控制风险", comparable: true },
          { category: "trend", field: "trend_score", before: 67, after: 61, delta: -6, direction: "down", comparable: true },
        ],
      },
    ],
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "review");
  await page.locator("#workspace-tab-tools").click();

  const panel = page.locator('.timeline-panel[aria-labelledby="adviceTimelineTitle"]');
  await expect(panel).toHaveAttribute("aria-labelledby", "adviceTimelineTitle");
  await expect(panel).toHaveAttribute("aria-describedby", "adviceTimelineDescription");
  await expect(page.locator("#adviceTimelineTitle")).toHaveText("核心分析建议变化");
  await expect(page.locator("#adviceTimelineDescription")).toHaveText("与上一条保留快照比较");
  await expect(panel).toContainText("控制风险 · 建议强度 74/100");
  await expect(panel).toContainText("市场时间 2026-07-15 14:55:00 · 记录时间 2026-07-15 15:01:00 至 2026-07-15 15:03:00");
  await expect(panel).toContainText("震荡 · 61/100");
  await expect(panel).toContainText("偏高");
  await expect(panel).toContainText("良好 · 82/100 · 来源 日线收盘快照");
  await expect(panel).not.toContainText("最终 AI");
  await expect(panel).not.toContainText("研究诊断");

  const details = panel.locator("details");
  await expect(details.locator("summary")).toHaveText("自上次保留快照以来 2 项变化");
  await details.locator("summary").click();
  await expect(details).toHaveAttribute("open", "");
  await expect(details).toContainText("动作 · 建议动作");
  await expect(details).toContainText("前观察");
  await expect(details).toContainText("后控制风险");

  const widths = await panel.evaluate((element) => {
    const timeline = element.querySelector("#adviceTimeline");
    const detailsElement = element.querySelector("details");
    const panelRect = element.getBoundingClientRect();
    const detailsRect = detailsElement.getBoundingClientRect();
    return {
      viewport: document.documentElement.clientWidth,
      documentScrollWidth: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth),
      panelLeft: panelRect.left,
      panelRight: panelRect.right,
      timelineClientWidth: timeline.clientWidth,
      timelineScrollWidth: timeline.scrollWidth,
      detailsClientWidth: detailsElement.clientWidth,
      detailsScrollWidth: detailsElement.scrollWidth,
      detailsRight: detailsRect.right,
    };
  });
  expect(widths.documentScrollWidth).toBeLessThanOrEqual(widths.viewport);
  expect(widths.panelLeft).toBeGreaterThanOrEqual(0);
  expect(widths.panelRight).toBeLessThanOrEqual(widths.viewport);
  expect(widths.timelineScrollWidth).toBeLessThanOrEqual(widths.timelineClientWidth);
  expect(widths.detailsScrollWidth).toBeLessThanOrEqual(widths.detailsClientWidth);
  expect(widths.detailsRight).toBeLessThanOrEqual(widths.viewport);
});

test("watchlist research queue supports ordered entry, editing, viewed state, and narrow widths", async ({ page }, testInfo) => {
  const watchlist = [
    {
      ...watchlistItem("600000.SH", "浦发银行"),
      group_name: "银行研究",
      note: "等待财报",
      pinned: true,
      research_status: "to_research",
      priority: "high",
      next_review_date: "2000-01-01",
      unread_change_count: 2,
    },
    {
      ...watchlistItem("000001.SZ", "平安银行"),
      group_name: "核心观察",
      unread_change_count: 4,
    },
    {
      ...watchlistItem("600036.SH", "招商银行"),
      research_status: "excluded",
      priority: "low",
    },
  ];
  const requests = { marks: [], patches: [], posts: [], streams: [] };
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/stream/quotes") requests.streams.push(url.searchParams.get("symbols") || "");
    if (url.pathname === "/api/watchlist" && request.method() === "POST") requests.posts.push(request.postDataJSON());
    if (url.pathname.startsWith("/api/watchlist/") && request.method() === "PATCH") {
      requests.patches.push(request.postDataJSON());
    }
    if (url.pathname.endsWith("/mark-viewed") && request.method() === "POST") {
      requests.marks.push(request.postDataJSON());
    }
  });
  await mockApi(page, {
    watchlist,
    timeline(symbol) {
      return [{
        id: symbol === "000001.SZ" ? 801 : 800,
        action: "观察",
        confidence: 55,
        trend_score: 52,
        risk_level: "中等",
        created_at: "2026-07-15 10:00:00",
        market_time: "2026-07-15 10:00:00",
        comparison_status: "comparable",
        has_changes: true,
        changes: [{ category: "action", field: "action", before: "等待", after: "观察" }],
      }];
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "monitor");
  if (testInfo.project.use.isMobile) await page.locator("#watchFormToggle").click();
  await expect(page.locator(".watch-queue-row")).toHaveCount(3);
  const firstRow = page.locator('.watch-queue-row[data-symbol="600000.SH"]');
  await expect(firstRow).toContainText("待研究");
  await expect(firstRow).toContainText("高优先级");
  await expect(firstRow).toContainText("逾期复核 · 2000-01-01");
  await expect(firstRow).toContainText("2 条新变化");
  await expect(firstRow).toContainText("分组 · 银行研究");
  await expect(firstRow).toContainText("关注原因 · 等待财报");
  await expect(firstRow).toContainText("置顶");
  await expect(page.locator(".watch-queue-row").last()).toHaveClass(/is-excluded/);
  await expect.poll(() => requests.streams.length).toBeGreaterThan(0);
  expect(requests.streams.at(-1)).not.toContain("600036.SH");

  const symbolInput = page.locator("#watchSymbolInput");
  const statusInput = page.locator("#watchStatusInput");
  const priorityInput = page.locator("#watchPriorityInput");
  const reviewInput = page.locator("#watchReviewDateInput");
  const groupInput = page.locator("#watchGroupInput");
  const noteInput = page.locator("#watchNoteInput");
  const addButton = page.getByRole("button", { name: "加入队列" });
  await symbolInput.focus();
  await page.keyboard.press("Tab");
  await expect(statusInput).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(priorityInput).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(reviewInput).toBeFocused();
  for (let index = 0; index < 4 && !(await groupInput.evaluate((element) => element === document.activeElement)); index += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(groupInput).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(noteInput).toBeFocused();
  await page.keyboard.press("Tab");
  if (testInfo.project.name === "desktop-webkit") {
    await addButton.focus();
  }
  await expect(addButton).toBeFocused();

  await symbolInput.fill("601318");
  await statusInput.selectOption("holding_research");
  await priorityInput.selectOption("high");
  await reviewInput.fill("2026-07-30");
  await groupInput.fill("保险研究");
  await noteInput.fill("中报后复核");
  await addButton.click();
  await expect.poll(() => requests.posts.length).toBe(1);
  expect(requests.posts[0]).toMatchObject({
    symbol: "601318",
    research_status: "holding_research",
    priority: "high",
    next_review_date: "2026-07-30",
    group_name: "保险研究",
    note: "中报后复核",
  });
  await expect(page.locator('.watch-queue-row[data-symbol="601318.SH"]')).toContainText("新增 601318");

  await firstRow.getByRole("button", { name: "编辑 浦发银行" }).click();
  const editForm = firstRow.locator(".watch-edit-form");
  await expect(editForm).toBeVisible();
  await editForm.locator('[name="research_status"]').selectOption("excluded");
  await editForm.locator('[name="priority"]').selectOption("low");
  await editForm.locator('[name="next_review_date"]').fill("");
  await editForm.locator('[name="group_name"]').fill("归档观察");
  await editForm.locator('[name="note"]').fill("");
  await editForm.locator('[name="pinned"]').uncheck();
  const streamCountBeforePatch = requests.streams.length;
  await editForm.getByRole("button", { name: "保存" }).click();
  await expect.poll(() => requests.patches.length).toBe(1);
  expect(requests.patches[0]).toMatchObject({
    research_status: "excluded",
    priority: "low",
    next_review_date: null,
    group_name: "归档观察",
    note: null,
    pinned: false,
  });
  await expect(firstRow).toHaveClass(/is-excluded/);
  await expect(firstRow).toContainText("未设复核");
  await expect(firstRow).toContainText("关注原因 · 暂无");
  await expect.poll(() => requests.streams.length).toBeGreaterThan(streamCountBeforePatch);
  expect(requests.streams.at(-1)).not.toContain("600000.SH");

  const unreadRow = page.locator('.watch-queue-row[data-symbol="000001.SZ"]');
  await expect(unreadRow).toContainText("4 条新变化");
  await unreadRow.locator(".watch-main").click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await expect.poll(() => requests.marks.length).toBe(1);
  expect(requests.marks[0]).toEqual({ clear_unread: true, viewed_through_advice_id: 801 });
  await selectPrimaryView(page, "monitor");
  await expect(unreadRow).not.toContainText("4 条新变化");

  await unreadRow.getByRole("button", { name: "编辑 平安银行" }).click();
  await assertWatchlistFits(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await assertWatchlistFits(page);
  await page.setViewportSize({ width: 360, height: 800 });
  await assertWatchlistFits(page);
});

async function assertWatchlistFits(page) {
  const measurements = await page.locator(".watchlist-box").evaluate((box) => {
    const viewport = document.documentElement.clientWidth;
    const targets = [box, ...box.querySelectorAll(".watch-row, .watch-edit-form, input, select, button")];
    return {
      viewport,
      documentWidth: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth),
      targets: targets.map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          clientWidth: element.clientWidth,
          scrollWidth: element.scrollWidth,
        };
      }),
    };
  });
  expect(measurements.documentWidth).toBeLessThanOrEqual(measurements.viewport);
  for (const target of measurements.targets) {
    expect(target.left).toBeGreaterThanOrEqual(-1);
    expect(target.right).toBeLessThanOrEqual(measurements.viewport + 1);
    expect(target.scrollWidth).toBeLessThanOrEqual(target.clientWidth + 1);
  }
}

test("mobile actions show only current results and keep local errors in the query panel", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.use.isMobile, "covered by the real mobile device project");
  const watchlist = [watchlistItem("600000.SH", "浦发银行")];
  await mockApi(page, {
    watchlist,
    async workbench(symbol) {
      if (symbol === "000001.SZ") await delay(500);
      return workbenchPayload(symbol);
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect(page.locator("#searchForm")).toBeInViewport();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  const input = page.locator("#symbolInput");
  await input.fill("000001");
  await page.locator("#searchForm button").click();
  await input.fill("300750");
  await page.locator("#searchForm button").click();

  await expect(page.locator("#stockName")).toHaveText("宁德时代");
  await delay(600);
  await expect(page.locator("#stockName")).toHaveText("宁德时代");
  await expect(page.locator(".main-card")).toBeInViewport();

  await selectPrimaryView(page, "monitor");
  await page.getByRole("button", { name: "移出自选" }).click();
  await expect(page.locator("#watchList")).toContainText("暂无自选");
  await expect(page.locator(".watchlist-box")).toBeInViewport();

  await selectPrimaryView(page, "research");
  await input.scrollIntoViewIfNeeded();
  await input.fill("bad-code");
  await expect(page.locator("#symbolSuggestions")).toContainText("未找到匹配股票");
  const scrollBeforeValidation = await page.evaluate(() => window.scrollY);
  await page.locator("#searchForm button").click();
  await expect(page.locator("#symbolError")).toBeVisible();
  await expect(page.locator("#symbolError")).toContainText("未找到匹配股票，请检查名称或输入6位代码");
  await expect(input).toHaveAttribute("aria-invalid", "true");
  await expect(input).toBeFocused();
  await expect(page.locator("#stockName")).toHaveText("宁德时代");
  await expect(page.locator("#summary")).not.toContainText("股票代码应为6位数字");
  expect(Math.abs((await page.evaluate(() => window.scrollY)) - scrollBeforeValidation)).toBeLessThanOrEqual(1);

  await input.fill("000001");
  await expect(page.locator("#symbolError")).toBeHidden();
  await expect(input).toHaveAttribute("aria-invalid", "false");
});

test("historical watchlist scans stay read-only until current analysis is explicitly requested", async ({ page }, testInfo) => {
  const workbenchSymbols = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/stock/workbench") workbenchSymbols.push(url.searchParams.get("symbol"));
  });
  await mockApi(page, {
    api(url, request) {
      if (url.pathname === "/api/watchlist/scan" && request.method() === "POST") {
        return {
          payload: {
            universe: ["000001.SZ"],
            as_of: "2026-07-10 23:59:59",
            rule_version: "watchlist-scan-v1",
            conditions: ["close_above_ma20", "volume_surge_5d"],
            success: [{
              symbol: "000001.SZ",
              data_date: "2026-07-10",
              matched: false,
              condition_results: { close_above_ma20: true, volume_surge_5d: false },
              matched_conditions: ["close_above_ma20"],
              metrics: { close: 12.34, ma20: 12.01, volume_ratio_5d: 1.18 },
            }],
            missing: [],
          },
        };
      }
      return null;
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "review");
  await page.locator("#workspace-tab-replay").click();
  if (testInfo.project.use.isMobile) {
    await page.locator(".watchlist-scan-panel .layout-collapse-toggle").click();
  }
  await page.locator("#watchlistScanAsOf").fill("2026-07-10");
  await page.locator("#watchlistScanForm button[type=submit]").click();

  const evidence = page.locator("#watchlistScanResults .scan-result");
  await expect(evidence).toHaveCount(1);
  await expect(evidence).toHaveJSProperty("tagName", "ARTICLE");
  await expect(page.locator("#watchlistScanResults")).toContainText("只读历史证据");
  await expect(page.locator("#watchlistScanResults")).toContainText("2026-07-10 23:59:59");
  await expect(page.locator("#watchlistScanResults")).toContainText("watchlist-scan-v1");
  await expect(evidence).toContainText("收盘价 12.34");
  await expect(evidence).toContainText("20日均线 12.01");
  await expect(evidence).toContainText("5日量比 1.18");
  await expect(evidence).toContainText("成交量达到5日均量1.5倍：否");

  await evidence.click({ position: { x: 6, y: 6 } });
  await expect(page.locator("#workspace-panel-replay")).toBeVisible();
  expect(workbenchSymbols).toEqual(["600519"]);

  await evidence.getByRole("button", { name: "查看当前分析" }).click();
  await expect(page.locator("#workspace-panel-overview")).toBeVisible();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  expect(workbenchSymbols).toEqual(["600519", "000001.SZ"]);
});

test("failed alert and note writes retain the rendered rows and drafts", async ({ page }) => {
  await mockApi(page, {
    async api(url, request) {
      if (
        request.method() === "POST"
        && ["/api/alerts", "/api/stock/notes"].includes(url.pathname)
      ) {
        await delay(120);
        return { status: 503, payload: { detail: "写入暂不可用，请稍后重试" } };
      }
      return null;
    },
    workbench(symbol) {
      const payload = workbenchPayload(symbol);
      return {
        ...payload,
        alert_rules: [{
          id: 7,
          symbol: payload.symbol,
          name: "原有价格提醒",
          condition_type: "price_above",
          condition_label: "价格高于",
          threshold: 120,
          enabled: true,
          trigger_count: 0,
          cooldown_seconds: 300,
          last_state: "等待",
        }],
        notes: [{
          id: 9,
          symbol: payload.symbol,
          note_type: "观察",
          content: "原有笔记证据",
          price: 100,
          trade_date: "2026-07-14",
          created_at: "2026-07-14 10:00:00",
          visible: true,
        }],
      };
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "review");
  await page.locator("#workspace-tab-tools").click();
  await expect(page.locator("#alertList")).toContainText("原有价格提醒");
  await expect(page.locator("#noteList")).toContainText("原有笔记证据");

  await page.locator("#alertThreshold").fill("130.5");
  await page.locator("#alertForm button[type=submit]").click();
  await expect(page.locator("#alertForm")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#alertForm button[type=submit]")).toHaveText("添加中");
  await expect(page.locator("#alertFormFeedback")).toContainText("写入暂不可用");
  await expect(page.locator("#alertForm")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#alertList")).toContainText("原有价格提醒");
  await expect(page.locator("#alertThreshold")).toHaveValue("130.5");

  await page.locator("#noteContent").fill("失败后仍需保留的草稿");
  await page.locator("#noteForm button[type=submit]").click();
  await expect(page.locator("#noteForm")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#noteForm button[type=submit]")).toHaveText("保存中");
  await expect(page.locator("#noteFormFeedback")).toContainText("写入暂不可用");
  await expect(page.locator("#noteForm")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#noteList")).toContainText("原有笔记证据");
  await expect(page.locator("#noteContent")).toHaveValue("失败后仍需保留的草稿");
});

test("restored tabs and tools retain the last successful current stock", async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    localStorage.setItem("ashare-radar.workspace-preferences", JSON.stringify({
      version: 1,
      preferences: {
        workspaceView: "tools",
        dailyChartRange: 60,
        dailyChartMa5: true,
        dailyChartMa20: true,
        minuteChartInterval: "5m",
        mobileChartView: "daily",
      },
    }));
  });
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/stock/workbench" && url.searchParams.get("symbol") === "000001.SZ") {
        return { status: 503, payload: { detail: "银行分析暂不可用" } };
      }
      return null;
    },
  });

  await page.goto("/");
  await expect(page.locator("#workspace-panel-tools")).toBeVisible();
  await expect(page.locator("#workspace-tab-tools")).toHaveAttribute("aria-selected", "true");
  await expectPrimaryView(page, "review");
  await expect(page.locator("#workspace-tab-tools")).toBeInViewport();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await expect(page.locator("#toolsStockContext")).toContainText("当前股票");
  await expect(page.locator("#toolsStockContext")).toContainText("贵州茅台");
  await expect(page.locator("#toolsStockContext")).toContainText("SH600519");

  if (testInfo.project.use.isMobile) await page.locator("#queryPanelToggle").click();
  await page.locator("#symbolInput").fill("000001");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#dataStatus")).toContainText("仍显示贵州茅台");
  await expect(page.locator("#toolsStockContext")).toContainText("贵州茅台");
  await expect(page.locator("#toolsStockContext")).not.toContainText("平安银行");
  await expect(page.locator("#symbolInput")).toHaveValue("600519");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");

  await page.locator("#toolsStockOpen").click();
  await expect(page.locator("#workspace-panel-overview")).toBeVisible();
  await expect(page.locator("#stockWorkbench")).toBeFocused();
});

test("question and minute capability labels follow their response contracts", async ({ page }) => {
  let questionCount = 0;
  await mockApi(page, {
    api(url, request) {
      if (url.pathname === "/api/data/status") {
        return {
          payload: {
            providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [],
            llm_explanation_available: false,
            minute_analysis_available: true,
          },
        };
      }
      if (url.pathname === "/api/stock/minute-analysis") {
        return {
          payload: {
            ...minuteAnalysisPayload(url.searchParams.get("interval") || "5m", url.searchParams.get("symbol") || "600519.SH"),
            availability: "unavailable",
            availability_reason: "分钟源当前无可验证样本",
            reason_code: "provider_unavailable",
            klines: [],
            sample_count: 0,
          },
        };
      }
      if (url.pathname === "/api/stock/ask" && request.method() === "POST") {
        questionCount += 1;
        const llmUsed = questionCount === 2;
        return {
          payload: {
            symbol: "600519.SH",
            updated_at: "2026-07-14 10:00:00",
            question: request.postDataJSON().question,
            topic: "risk",
            conclusion: llmUsed ? "AI解释" : "规则结论",
            answer: llmUsed ? "AI增强回答" : "规则回答",
            confidence: 70,
            answer_source: llmUsed ? "本地模型解释增强" : "规则问诊",
            llm_used: llmUsed,
            llm_status: llmUsed ? "仅增强解释" : "未配置大模型API",
            evidence: [], actions: [], invalidations: [], related_questions: [],
          },
        };
      }
      return null;
    },
  });

  await page.goto("/");
  await expect(page.locator("#aiQuestionCapability")).toHaveText("规则问诊");
  await expect(page.locator("#minuteAnalysis")).toHaveAttribute("data-capability", "available");
  await expect(page.locator("#minuteAnalysis")).toHaveAttribute("data-availability", "unavailable");
  await expect(page.locator("#minuteAnalysis")).toContainText("分钟源当前无可验证样本");
  await page.locator("#workspace-tab-qa").click();
  const input = page.locator("#aiQuestionInput");
  await input.fill("风险在哪里？");
  await page.locator("#aiQuestionForm button[type=submit]").click();
  await expect(page.locator("#aiQuestionCapability")).toHaveText("规则问诊");
  await expect(page.locator("#aiDashboard .ai-card-wide")).toContainText("规则回答");

  await input.fill("再解释一次");
  await page.locator("#aiQuestionForm button[type=submit]").click();
  await expect(page.locator("#aiQuestionCapability")).toHaveText("AI增强问诊");
  await expect(page.locator("#aiDashboard .ai-card-wide")).toContainText("AI增强回答");
});

test("mobile DOM order, focus order, tabs, filters, and width remain accessible", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.use.isMobile, "covered by the real mobile device project");
  await mockApi(page, {
    workbench(symbol) {
      return workbenchPayload(symbol, { chartMarks: true });
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");

  const mainOrder = await page.locator("main.layout").evaluate((main) =>
    Array.from(main.children).map((element) => {
      if (element.classList.contains("query-panel")) return "query";
      if (element.classList.contains("workspace")) return "workspace";
      if (element.classList.contains("control-panel")) return "controls";
      return "other";
    })
  );
  expect(mainOrder.slice(0, 3)).toEqual(["query", "workspace", "controls"]);

  await primaryViewButton(page, "research").focus();
  await expect(primaryViewButton(page, "research")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(primaryViewButton(page, "market")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(primaryViewButton(page, "review")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(primaryViewButton(page, "monitor")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#queryPanelToggle")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#symbolInput")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#searchForm button")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#stockSearchHistoryClear")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#stockSearchHistoryList .stock-search-history-item").first()).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#quickList button").first()).toBeFocused();

  const overviewTab = page.locator("#workspace-tab-overview");
  const qaTab = page.locator("#workspace-tab-qa");
  const themeTab = page.locator("#workspace-tab-theme");
  const replayTab = page.locator("#workspace-tab-replay");
  const paperTab = page.locator("#workspace-tab-paper");
  const toolsTab = page.locator("#workspace-tab-tools");
  const dataTab = page.locator("#workspace-tab-data");
  await overviewTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(qaTab).toBeFocused();
  await expect(qaTab).toHaveAttribute("aria-selected", "true");
  await expect(qaTab).toHaveAttribute("tabindex", "0");
  await expect(overviewTab).toHaveAttribute("tabindex", "-1");
  await expect(page.locator("#workspace-panel-qa")).not.toHaveAttribute("hidden", "");
  await expect(page.locator("#workspace-panel-overview")).toHaveAttribute("hidden", "");

  await page.keyboard.press("End");
  await expect(themeTab).toBeFocused();
  await expect(themeTab).toHaveAttribute("aria-selected", "true");
  await themeTab.focus();
  await page.keyboard.press("Home");
  await expect(overviewTab).toBeFocused();
  await overviewTab.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(themeTab).toBeFocused();

  await selectPrimaryView(page, "review");
  await replayTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(paperTab).toBeFocused();
  await expect(paperTab).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#workspace-panel-paper")).not.toHaveAttribute("hidden", "");
  await page.keyboard.press("ArrowRight");
  await expect(toolsTab).toBeFocused();
  await expect(toolsTab).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#workspace-panel-tools")).not.toHaveAttribute("hidden", "");
  const markFilter = page.locator('#markFilters button[data-mark-category="买点"]');
  await expect(markFilter).toHaveAttribute("aria-pressed", "true");
  await markFilter.click();
  await expect(markFilter).toHaveAttribute("aria-pressed", "false");
  await toolsTab.focus();
  await page.keyboard.press("Home");
  await expect(replayTab).toBeFocused();
  await replayTab.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(dataTab).toBeFocused();
  await expect(page.locator("#workspace-panel-data")).not.toHaveAttribute("hidden", "");

  const widths = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }));
  expect(Math.max(widths.body, widths.document)).toBeLessThanOrEqual(widths.viewport);

  await page.setViewportSize({ width: 320, height: 800 });
  await selectPrimaryView(page, "research");
  const researchLayout = await narrowPrimaryLayout(page, [".query-panel", ".workspace"]);
  expect(researchLayout.scrollWidth).toBeLessThanOrEqual(researchLayout.viewport);
  for (const item of researchLayout.items) {
    expect(item.left).toBeCloseTo(12, 0);
    expect(item.width).toBeCloseTo(296, 0);
  }

  await selectPrimaryView(page, "monitor");
  const monitorLayout = await narrowPrimaryLayout(page, [
    ".watchlist-box", ".side-column", ".notice", ".data-health", ".data-monitor",
  ]);
  expect(monitorLayout.scrollWidth).toBeLessThanOrEqual(monitorLayout.viewport);
  for (const item of monitorLayout.items) {
    expect(item.left).toBeCloseTo(12, 0);
    expect(item.width).toBeCloseTo(296, 0);
  }
});

test("background restore retries auxiliary failures without letting SSE hide degradation", async ({ page }) => {
  let degraded = true;
  const calls = { market: 0, strong: 0, status: 0 };
  await mockApi(page, {
    api(url) {
      const keys = {
        "/api/market": "market",
        "/api/strong-stocks": "strong",
        "/api/data/status": "status",
      };
      const key = keys[url.pathname];
      if (!key) return null;
      calls[key] += 1;
      return degraded ? { status: 503, payload: { detail: `${key} unavailable` } } : null;
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect(page.locator("#dataStatus")).toHaveText("3 项辅助数据降级，详细信息见诊断区");
  await expect(page.locator("#dataStatus")).not.toContainText("unavailable");
  await expect(page.locator("#marketStrip")).toContainText("market unavailable");
  await expect(page.locator("#leaderList")).toContainText("strong unavailable");
  await expect(page.locator(".data-health")).toContainText("status unavailable");
  const statusLayout = await page.locator(".topbar").evaluate((topbar) => {
    const pill = topbar.querySelector("#dataStatus");
    return {
      topbarHeight: topbar.getBoundingClientRect().height,
      pillWidth: pill.getBoundingClientRect().width,
      viewport: document.documentElement.clientWidth,
    };
  });
  expect(statusLayout.pillWidth).toBeLessThanOrEqual(Math.min(380, statusLayout.viewport));
  expect(statusLayout.topbarHeight).toBeLessThanOrEqual(104);
  await emitQuoteFrame(page);
  await expect(page.locator("#dataStatus")).not.toContainText("实时连接正常");
  await expect(page.locator("#dataStatus")).toHaveText("3 项辅助数据降级，详细信息见诊断区");

  degraded = false;
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    document.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => calls).toEqual({ market: 2, strong: 2, status: 2 });
  await expect(page.locator("#dataStatus")).not.toContainText("部分辅助数据降级");
  await emitQuoteFrame(page);
  await expect(page.locator("#dataStatus")).toHaveText("核心分析快照已加载；观察报价流已收到有效帧");
});

test("online recovery replaces unfinished workbench loads and retries failures once", async ({ page }) => {
  let delayFirstBankLoad = true;
  let failNextWorkbench = false;
  const workbenchRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/stock/workbench") workbenchRequests.push(url.searchParams.get("symbol"));
  });
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/stock/workbench" && failNextWorkbench) {
        failNextWorkbench = false;
        return { status: 503, payload: { detail: "temporary offline failure" } };
      }
      return null;
    },
    async workbench(symbol) {
      if (symbol === "000001.SZ" && delayFirstBankLoad) {
        delayFirstBankLoad = false;
        await delay(500);
      }
      return workbenchPayload(symbol);
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  const input = page.locator("#symbolInput");
  await input.fill("000001");
  await page.locator("#searchForm button").click();
  await expect.poll(() => workbenchRequests.length).toBe(2);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("online"));
    window.dispatchEvent(new Event("online"));
  });
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await expect.poll(() => workbenchRequests.length).toBe(3);
  await page.waitForTimeout(550);
  expect(workbenchRequests).toHaveLength(3);

  failNextWorkbench = true;
  await input.fill("300750");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#dataStatus")).toContainText("加载失败");
  await page.evaluate(() => {
    window.dispatchEvent(new Event("online"));
    window.dispatchEvent(new Event("online"));
  });
  await expect(page.locator("#stockName")).toHaveText("宁德时代");
  await expect.poll(() => workbenchRequests.length).toBe(5);
  expect(workbenchRequests).toEqual(["600519", "000001.SZ", "000001.SZ", "300750.SZ", "300750.SZ"]);
});

async function canvasHasInk(locator) {
  return locator.evaluate((canvas) => {
    if (!canvas.width || !canvas.height) return false;
    const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
    for (let index = 3; index < pixels.length; index += 16) {
      if (pixels[index] !== 0) return true;
    }
    return false;
  });
}

async function pointAtChartRow(page, canvas, index, rowCount) {
  const box = await canvas.boundingBox();
  const dimensions = await canvas.evaluate((element) => ({ width: element.clientWidth, height: element.clientHeight }));
  expect(box).not.toBeNull();
  const localX = 46 + (dimensions.width - 62) / rowCount * (index + 0.5);
  await page.mouse.move(
    box.x + localX * box.width / dimensions.width,
    box.y + dimensions.height / 2 * box.height / dimensions.height
  );
}

async function tapChart(page, canvas, index, rowCount) {
  const box = await canvas.boundingBox();
  const dimensions = await canvas.evaluate((element) => ({ width: element.clientWidth, height: element.clientHeight }));
  expect(box).not.toBeNull();
  const localX = 46 + (dimensions.width - 62) / rowCount * (index + 0.5);
  await canvas.tap({
    position: {
      x: localX * box.width / dimensions.width,
      y: dimensions.height / 2 * box.height / dimensions.height,
    },
  });
}

async function leaveCanvas(page, canvas) {
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box.x + box.width + 20, box.y + box.height + 20);
}

async function assertChartInspection(inspector, values, row, period) {
  const time = row.date || row.timestamp;
  await expect(inspector).toBeVisible();
  await expect(inspector).toHaveAttribute("aria-hidden", "false");
  await expect(values).toContainText(time);
  await expect(values).toContainText(period);
  for (const [label, value] of [
    ["开", row.open],
    ["高", row.high],
    ["低", row.low],
    ["收", row.close],
  ]) {
    await expect(values).toContainText(`${label} ${Number(value).toFixed(2)}`);
  }
  await expect(values).toContainText(`量 ${formatExpectedChartVolume(row.volume)}`);
}

async function assertCrosshairPosition(inspector, canvas, index, rowCount) {
  const dimensions = await canvas.evaluate((element) => ({ width: element.clientWidth, height: element.clientHeight }));
  const positions = await inspector.evaluate((element) => {
    const vertical = element.querySelector(".chart-crosshair-x");
    const horizontal = element.querySelector(".chart-crosshair-y");
    return {
      verticalLeft: Number.parseFloat(vertical.style.left),
      verticalTop: Number.parseFloat(vertical.style.top),
      verticalHeight: Number.parseFloat(vertical.style.height),
      horizontalLeft: Number.parseFloat(horizontal.style.left),
      horizontalTop: Number.parseFloat(horizontal.style.top),
      horizontalWidth: Number.parseFloat(horizontal.style.width),
    };
  });
  const expectedX = 46 + (dimensions.width - 62) / rowCount * (index + 0.5);
  expect(positions.verticalLeft).toBeCloseTo(expectedX, 1);
  expect(positions.verticalTop).toBeCloseTo(18, 1);
  expect(positions.verticalHeight).toBeCloseTo(dimensions.height - 46, 1);
  expect(positions.horizontalLeft).toBeCloseTo(46, 1);
  expect(positions.horizontalTop).toBeGreaterThanOrEqual(18);
  expect(positions.horizontalTop).toBeLessThanOrEqual(dimensions.height - 28);
  expect(positions.horizontalWidth).toBeCloseTo(dimensions.width - 62, 1);
}

async function crosshairCoordinate(inspector, selector, property) {
  const value = await inspector.locator(selector).evaluate(
    (element, styleProperty) => Number.parseFloat(element.style[styleProperty]),
    property
  );
  expect(Number.isFinite(value)).toBe(true);
  return value;
}

function formatExpectedChartVolume(value) {
  const number = Number(value);
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(2)}亿`;
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(2)}万`;
  return number.toFixed(2);
}

async function assertActivityFilterState(filters, activeKind) {
  await expect.poll(() => filters.evaluateAll((buttons) => buttons.map((button) => ({
    kind: button.dataset.activityFilter,
    pressed: button.getAttribute("aria-pressed"),
  })))).toEqual(["all", "advice", "alert", "note"].map((kind) => ({
    kind,
    pressed: String(kind === activeKind),
  })));
}


async function narrowPrimaryLayout(page, selectors) {
  return page.evaluate((targets) => {
    const bounds = (selector) => {
      const rect = document.querySelector(selector).getBoundingClientRect();
      return { left: rect.left, width: rect.width };
    };
    return {
      viewport: document.documentElement.clientWidth,
      scrollWidth: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth),
      items: targets.map(bounds),
    };
  }, selectors);
}

async function assertChartWorkspaceFits(page) {
  const metrics = await page.locator("#chartWorkspace").evaluate((workspace) => {
    const viewport = document.documentElement.clientWidth;
    const visibleTargets = [workspace, ...workspace.querySelectorAll("button, label, canvas, .research-chart-pane")]
      .filter((element) => element.getClientRects().length > 0)
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          clientWidth: element.clientWidth,
          scrollWidth: element.scrollWidth,
        };
      });
    return {
      viewport,
      documentWidth: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth),
      visibleTargets,
    };
  });
  expect(metrics.documentWidth).toBeLessThanOrEqual(metrics.viewport);
  for (const target of metrics.visibleTargets) {
    expect(target.left).toBeGreaterThanOrEqual(-1);
    expect(target.right).toBeLessThanOrEqual(metrics.viewport + 1);
    expect(target.scrollWidth).toBeLessThanOrEqual(target.clientWidth + 1);
  }
}

function reviewPlan() { return { id: 10, advice_id: 20, symbol: "600519.SH", revision: 1,
    snapshot_market_time: "2026-07-01 09:45:00", snapshot_price: 100, target_price: 110, stop_price: 90, horizon_days: 20,
    snapshot_adjustment_mode: "qfq", snapshot_anchor_date: "2026-07-01", snapshot_anchor_close: 100,
    snapshot_data_version: "paper-e2e-v1", snapshot_contract_version: "daily-kline-pit.v1",
    hypothesis: "趋势延续", trigger_condition: "次日开盘", invalidation_condition: "跌破止损",
    trigger_basis: "daily_high_gte_target_price", invalidation_basis: "daily_low_lte_stop_price",
    plan_payload_digest: "a".repeat(64),
    created_at: "2026-07-01 09:45:00", updated_at: "2026-07-01 09:45:00" }; }

function paperStrategy(overrides = {}) {
  return {
    id: 7, plan_id: 10, plan_revision: 1, plan_payload_digest: "a".repeat(64), advice_id: 20, symbol: "600519.SH",
    activation_market_time: "2026-07-01 10:00:00", allocation_pct: 25,
    priority: 0, entry_expiry_sessions: 5,
    snapshot_market_time: "2026-07-01 09:45:00", snapshot_price: 100,
    target_price: 110, stop_price: 90, horizon_days: 20, status: "pending",
    ...overrides,
  };
}

function simulatedPaperDashboard() {
  const run = {
    id: 1, as_of: "2026-07-03 15:15:00", rule_version: "paper-review-plan-v2",
    modelled_one_way_friction_pct: 0, cost_profile_id: "base",
    cost_profile_name: "基础成本", cost_profile_version: "2026.07",
    benchmark_symbol: "000300.SH", benchmark_status: "available",
    benchmark_message: null, strategy_count: 1, execution_count: 2,
    closed_count: 1, data_unavailable_count: 0, input_fingerprint: "b".repeat(64),
    output_digest: "c".repeat(64),
    strategy_snapshot_hash: "strategy123", market_data_hash: "market123",
    data_start_date: "2026-07-01", data_end_date: "2026-07-03",
    configuration: {}, rule_profiles: [], data_sources: ["test"],
    message: "已重放 1 条策略，生成 2 笔成交，平仓 1 条，数据不可用 0 条",
    created_at: "2026-07-03T08:00:00.000000Z",
  };
  const strategy = paperStrategy({
    status: "closed", normalized_target_price: 110, normalized_stop_price: 90,
    entry_date: "2026-07-02", entry_price: 100, quantity: 2400,
    exit_date: "2026-07-03", exit_price: 110, exit_reason: "target_hit",
    held_sessions: 2, realized_pnl: 23748, return_pct: 9.89,
  });
  return paperTradingDashboard({
    performance: {
      strategy_count: 1, pending_count: 0, open_count: 0, closed_count: 1,
      skipped_count: 0, data_unavailable_count: 0, win_count: 1, win_rate_pct: 100,
      cash_balance: 1023748, market_value: 0, total_equity: 1023748,
      gross_equity: 1024000, realized_pnl: 23748, unrealized_pnl: 0,
      gross_pnl: 24000, total_cost: 252, cost_drag_pct: 0.0252,
      total_return_pct: 2.3748, gross_return_pct: 2.4,
      benchmark_return_pct: 1, excess_return_pct: 1.3748, max_drawdown_pct: -0.05,
    },
    strategies: [strategy],
    trades: [
      { id: 1, run_id: 1, strategy_id: 7, symbol: "600519.SH", side: "buy", trade_date: "2026-07-02", price: 100, quantity: 2400, gross_amount: 240000, commission_amount: 60, stamp_duty_amount: 0, transfer_fee_amount: 2.4, slippage_amount: 48, friction_amount: 110.4, reason: "strategy_entry" },
      { id: 2, run_id: 1, strategy_id: 7, symbol: "600519.SH", side: "sell", trade_date: "2026-07-03", price: 110, quantity: 2400, gross_amount: 264000, commission_amount: 66, stamp_duty_amount: 132, transfer_fee_amount: 2.64, slippage_amount: 52.8, friction_amount: 253.44, reason: "target_hit" },
    ],
    events: [
      { id: 1, run_id: 1, sequence: 1, strategy_id: 7, symbol: "600519.SH", event_date: "2026-07-02", event_code: "buy_filled", category: "execution", severity: "info", message: "买入成交", details: {} },
      { id: 2, run_id: 1, sequence: 2, strategy_id: 7, symbol: "600519.SH", event_date: "2026-07-03", event_code: "sell_filled", category: "execution", severity: "info", message: "卖出成交", details: {} },
    ],
    equity_curve: [
      { as_of_date: "2026-07-02", total_equity: 999760, benchmark_equity: 1000000, return_pct: -0.024, drawdown_pct: -0.024 },
      { as_of_date: "2026-07-03", total_equity: 1023748, benchmark_equity: 1010000, return_pct: 2.3748, drawdown_pct: 0 },
    ],
    latest_run: run, selected_run_id: 1, runs: [run],
  });
}

function watchlistItem(symbol, name) {
  const [code] = symbol.split(".");
  return {
    symbol,
    code,
    name,
    latest_price: 10,
    latest_change_pct: 0,
    note: "E2E自选",
    group_name: "默认",
    pinned: false,
    research_status: "watching",
    priority: "medium",
    next_review_date: null,
    last_viewed_at: null,
    unread_change_count: 0,
  };
}
