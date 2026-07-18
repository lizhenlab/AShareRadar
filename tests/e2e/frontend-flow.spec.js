import { expect, test } from "@playwright/test";

test("SSE status waits for the current frame and preserves degradation", async ({ page }) => {
  let degraded = false;
  await mockApi(page, {
    workbench(symbol) {
      return workbenchPayload(symbol, { degraded });
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect(page.locator("#dataStatus")).not.toContainText("正常");
  await emitQuoteFrame(page);
  await expect(page.locator("#quoteList")).toContainText("浏览器行情帧");
  await expect(page.locator("#dataStatus")).toHaveText("核心分析快照已加载；观察报价流已收到有效帧");
  await expect(page.locator("#dataStatus")).not.toContainText("实时连接正常");

  degraded = true;
  await page.reload();
  await expect(page.locator("#dataStatus")).toContainText("本地数据部分降级");
  await emitQuoteFrame(page);
  await expect(page.locator("#quoteList")).toContainText("浏览器行情帧");
  await expect(page.locator("#dataStatus")).toContainText("本地数据部分降级");
  await expect(page.locator("#dataStatus")).not.toContainText("实时连接正常");
});

test("three stock loads reuse global requests and add only five stock requests", async ({ page }) => {
  const apiRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) apiRequests.push(`${url.pathname}${url.search}`);
  });
  await mockApi(page);

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect.poll(() => apiRequests.length).toBe(14);

  const input = page.locator("#symbolInput");
  await input.fill("000001");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await expect.poll(() => apiRequests.length).toBe(19);

  await input.fill("300750");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#stockName")).toHaveText("宁德时代");
  await expect.poll(() => apiRequests.length).toBe(24);

  const globalEndpoints = [
    "/api/market",
    "/api/strong-stocks",
    "/api/data/status",
    "/api/tasks/status",
    "/api/tasks/runs?limit=8",
    "/api/monitor/events?limit=8",
    "/api/watchlist",
    "/api/plates?limit=8",
    "/api/system/diagnostics",
  ];
  for (const endpoint of globalEndpoints) {
    expect(apiRequests.filter((url) => url === endpoint), endpoint).toHaveLength(1);
  }
  const stockKinds = [
    "/api/stock/workbench?",
    "/api/stock/minute-analysis?",
    "/api/advice/timeline?",
    "/api/reviews?",
    "/api/stream/quotes?",
  ];
  for (const prefix of stockKinds) {
    expect(apiRequests.filter((url) => url.startsWith(prefix)), prefix).toHaveLength(3);
  }
  expect(
    apiRequests.filter(
      (url) => !globalEndpoints.includes(url) && !stockKinds.some((prefix) => url.startsWith(prefix))
    )
  ).toEqual([]);
});

test("stock name suggestions select a canonical code without changing request baselines", async ({ page }) => {
  const apiRequests = [];
  const searchKeywords = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith("/api/")) return;
    apiRequests.push(`${url.pathname}${url.search}`);
    if (url.pathname === "/api/stocks") searchKeywords.push(url.searchParams.get("keyword"));
  });
  await mockApi(page);

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect.poll(() => apiRequests.length).toBe(14);

  const input = page.locator("#symbolInput");
  const suggestions = page.locator("#symbolSuggestions");
  await input.fill("000001");
  await page.waitForTimeout(350);
  expect(searchKeywords).toEqual([]);
  expect(apiRequests).toHaveLength(14);

  await input.fill("平安");
  await expect(suggestions).toBeVisible();
  await expect(suggestions.getByRole("option")).toHaveCount(1);
  await expect(suggestions).toContainText("平安银行");
  await expect(suggestions).toContainText("000001.SZ");
  await expect.poll(() => searchKeywords).toEqual(["平安"]);

  await input.press("Escape");
  await expect(suggestions).toBeHidden();
  await expect(input).toHaveAttribute("aria-expanded", "false");

  await input.fill("");
  await input.fill("平安");
  await expect(suggestions).toBeVisible();
  expect(searchKeywords).toEqual(["平安"]);
  await input.press("ArrowDown");
  await expect(input).toHaveAttribute("aria-activedescendant", "symbolSuggestions-option-0");
  await expect(suggestions.getByRole("option")).toHaveAttribute("aria-selected", "true");
  await input.press("Enter");

  await expect(input).toHaveValue("000001");
  await expect(suggestions).toBeHidden();
  await expect(page.locator("#stockCode")).toHaveText("SZ000001");
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await expect.poll(() => apiRequests.length).toBe(20);
  expect(searchKeywords).toEqual(["平安"]);

  const globalEndpoints = [
    "/api/market",
    "/api/strong-stocks",
    "/api/data/status",
    "/api/tasks/status",
    "/api/tasks/runs?limit=8",
    "/api/monitor/events?limit=8",
    "/api/watchlist",
    "/api/plates?limit=8",
    "/api/system/diagnostics",
  ];
  for (const endpoint of globalEndpoints) {
    expect(apiRequests.filter((url) => url === endpoint), endpoint).toHaveLength(1);
  }
  const stockKinds = [
    "/api/stock/workbench?",
    "/api/stock/minute-analysis?",
    "/api/advice/timeline?",
    "/api/reviews?",
    "/api/stream/quotes?",
  ];
  for (const prefix of stockKinds) {
    expect(apiRequests.filter((url) => url.startsWith(prefix)), prefix).toHaveLength(2);
  }
  expect(apiRequests.filter((url) => url.startsWith("/api/stocks?"))).toHaveLength(1);
  expect(
    apiRequests.filter(
      (url) =>
        !globalEndpoints.includes(url)
        && !stockKinds.some((prefix) => url.startsWith(prefix))
        && !url.startsWith("/api/stocks?")
    )
  ).toEqual([]);
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
      return {
        ...workbenchPayload(symbol),
        alert_events: alertEvents,
        notes,
        local_data_warnings: [{ component: "notes", message: "本地笔记读取失败" }],
      };
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator('#quickList button[data-symbol="000001"]').click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
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

test("watchlist research queue supports ordered entry, editing, viewed state, and narrow widths", async ({ page }) => {
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

async function emitQuoteFrame(page) {
  await expect
    .poll(async () => {
      const response = await page.request.get("/__e2e/quote-streams");
      return (await response.json()).clients;
    })
    .toBe(1);
  const response = await page.request.post("/__e2e/quote-frame");
  expect(response.ok()).toBeTruthy();
  expect((await response.json()).sent).toBe(1);
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
  const input = page.locator("#symbolInput");
  await input.fill("000001");
  await page.locator("#searchForm button").click();
  await input.fill("300750");
  await page.locator("#searchForm button").click();

  await expect(page.locator("#stockName")).toHaveText("宁德时代");
  await delay(600);
  await expect(page.locator("#stockName")).toHaveText("宁德时代");
  await expect(page.locator(".main-card")).toBeInViewport();

  await page.getByRole("button", { name: "移出自选" }).click();
  await expect(page.locator("#watchList")).toContainText("暂无自选");
  await expect(page.locator(".watchlist-box")).toBeInViewport();

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

  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press("Tab");
  await expect(page.locator("#symbolInput")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#searchForm button")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#quickList button").first()).toBeFocused();

  const overviewTab = page.locator("#workspace-tab-overview");
  const qaTab = page.locator("#workspace-tab-qa");
  const toolsTab = page.locator("#workspace-tab-tools");
  await overviewTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(qaTab).toBeFocused();
  await expect(qaTab).toHaveAttribute("aria-selected", "true");
  await expect(qaTab).toHaveAttribute("tabindex", "0");
  await expect(overviewTab).toHaveAttribute("tabindex", "-1");
  await expect(page.locator("#workspace-panel-qa")).not.toHaveAttribute("hidden", "");
  await expect(page.locator("#workspace-panel-overview")).toHaveAttribute("hidden", "");

  await page.keyboard.press("End");
  await expect(toolsTab).toBeFocused();
  await expect(toolsTab).toHaveAttribute("aria-selected", "true");
  const markFilter = page.locator('#markFilters button[data-mark-category="买点"]');
  await expect(markFilter).toHaveAttribute("aria-pressed", "true");
  await markFilter.click();
  await expect(markFilter).toHaveAttribute("aria-pressed", "false");
  await toolsTab.focus();
  await page.keyboard.press("Home");
  await expect(overviewTab).toBeFocused();
  await overviewTab.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(toolsTab).toBeFocused();

  const widths = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }));
  expect(Math.max(widths.body, widths.document)).toBeLessThanOrEqual(widths.viewport);

  await page.setViewportSize({ width: 320, height: 800 });
  const narrowLayout = await page.evaluate(() => {
    const bounds = (selector) => {
      const rect = document.querySelector(selector).getBoundingClientRect();
      return { left: rect.left, width: rect.width };
    };
    return {
      viewport: document.documentElement.clientWidth,
      scrollWidth: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth),
      items: [bounds(".query-panel"), bounds(".workspace"), bounds(".control-panel"), bounds(".side-column")],
    };
  });
  expect(narrowLayout.scrollWidth).toBeLessThanOrEqual(narrowLayout.viewport);
  for (const item of narrowLayout.items) {
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
  await expect(page.locator("#dataStatus")).toContainText("市场概览暂不可用");
  await expect(page.locator("#dataStatus")).toContainText("强股排行暂不可用");
  await expect(page.locator("#dataStatus")).toContainText("数据源状态暂不可用");
  await emitQuoteFrame(page);
  await expect(page.locator("#dataStatus")).not.toContainText("实时连接正常");
  await expect(page.locator("#dataStatus")).toContainText("部分辅助数据降级");

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

async function mockApi(page, options = {}) {
  const watchlist = options.watchlist || [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/stream/quotes") {
      await route.continue();
      return;
    }
    const custom = options.api ? await options.api(url, request) : null;
    if (custom) {
      await fulfillJson(route, custom.payload, custom.status);
      return;
    }
    if (url.pathname === "/api/stocks" && request.method() === "GET") {
      const keyword = url.searchParams.get("keyword") || "";
      const payload = typeof options.stocks === "function"
        ? await options.stocks(keyword)
        : options.stocks || stockSearchPayload(keyword);
      await fulfillJson(route, payload);
      return;
    }
    if (url.pathname === "/api/stock/workbench") {
      const symbol = url.searchParams.get("symbol") || "600519.SH";
      const payload = options.workbench ? await options.workbench(symbol) : workbenchPayload(symbol);
      await fulfillJson(route, payload);
      return;
    }
    if (url.pathname === "/api/watchlist" && request.method() === "GET") {
      await fulfillJson(route, watchlist);
      return;
    }
    if (url.pathname === "/api/watchlist" && request.method() === "POST") {
      const payload = request.postDataJSON();
      const symbol = canonicalWatchlistSymbol(payload.symbol);
      const item = {
        symbol,
        code: symbol.slice(0, 6),
        market: symbol.endsWith(".SH") ? "SH" : "SZ",
        name: `新增 ${symbol.slice(0, 6)}`,
        note: payload.note ?? null,
        group_name: payload.group_name || "默认",
        pinned: Boolean(payload.pinned),
        research_status: payload.research_status || "watching",
        priority: payload.priority || "medium",
        next_review_date: payload.next_review_date ?? null,
        last_viewed_at: null,
        unread_change_count: 0,
        latest_price: 10,
        latest_change_pct: 0,
      };
      const existing = watchlist.findIndex((row) => row.symbol === symbol);
      if (existing >= 0) watchlist.splice(existing, 1, item);
      else watchlist.push(item);
      moveExcludedWatchlistItemsLast(watchlist);
      await fulfillJson(route, item);
      return;
    }
    if (url.pathname.endsWith("/mark-viewed") && request.method() === "POST") {
      const symbol = decodeURIComponent(url.pathname.split("/").at(-2));
      const item = watchlist.find((row) => row.symbol === symbol);
      if (!item) {
        await fulfillJson(route, { detail: "自选股不存在" }, 404);
        return;
      }
      item.unread_change_count = 0;
      item.last_viewed_at = "2026-07-15 12:00:00";
      await fulfillJson(route, item);
      return;
    }
    if (url.pathname.startsWith("/api/watchlist/") && request.method() === "PATCH") {
      const symbol = decodeURIComponent(url.pathname.split("/").at(-1));
      const item = watchlist.find((row) => row.symbol === symbol);
      if (!item) {
        await fulfillJson(route, { detail: "自选股不存在" }, 404);
        return;
      }
      Object.assign(item, request.postDataJSON());
      if (!item.group_name) item.group_name = "默认";
      moveExcludedWatchlistItemsLast(watchlist);
      await fulfillJson(route, item);
      return;
    }
    if (url.pathname === "/api/advice/timeline") {
      const symbol = url.searchParams.get("symbol") || "600519.SH";
      const timeline = typeof options.timeline === "function" ? await options.timeline(symbol) : options.timeline || [];
      await fulfillJson(route, timeline);
      return;
    }
    if (url.pathname.startsWith("/api/watchlist/") && request.method() === "DELETE") {
      const symbol = decodeURIComponent(url.pathname.split("/").at(-1));
      const index = watchlist.findIndex((row) => row.symbol === symbol);
      if (index >= 0) watchlist.splice(index, 1);
      await fulfillJson(route, null, 204);
      return;
    }
    const payload = apiPayload(url);
    await fulfillJson(route, payload);
  });
}

function apiPayload(url) {
  const pathname = url.pathname;
  if (pathname === "/api/market") return { indices: [] };
  if (pathname === "/api/strong-stocks") return { items: [] };
  if (pathname === "/api/data/status") {
    return { providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] };
  }
  if (pathname === "/api/tasks/status") return { enabled: false, running: false, tasks: [] };
  if (pathname === "/api/tasks/runs" || pathname === "/api/monitor/events") return [];
  if (pathname === "/api/stock/minute-analysis") {
    return minuteAnalysisPayload(
      url.searchParams.get("interval") || "5m",
      url.searchParams.get("symbol") || "600519.SH"
    );
  }
  if (pathname === "/api/advice/timeline" || pathname === "/api/plates") return [];
  return [];
}

function stockSearchPayload(keyword) {
  const query = String(keyword || "").trim().toLowerCase();
  if (!query) return [];
  return [
    {
      symbol: "600519.SH",
      code: "600519",
      market: "SH",
      name: "贵州茅台",
      industry: "白酒",
      source: "E2E股票检索",
      updated_at: "2026-07-15 10:00:00",
    },
    {
      symbol: "000001.SZ",
      code: "000001",
      market: "SZ",
      name: "平安银行",
      industry: "股份制银行",
      source: "E2E股票检索",
      updated_at: "2026-07-15 10:00:00",
    },
    {
      symbol: "300750.SZ",
      code: "300750",
      market: "SZ",
      name: "宁德时代",
      industry: "电池",
      source: "E2E股票检索",
      updated_at: "2026-07-15 10:00:00",
    },
  ].filter((stock) => [stock.symbol, stock.code, stock.name].some((value) => value.toLowerCase().includes(query)));
}

function workbenchPayload(symbol, { degraded = false, chartMarks = false, withKlines = false } = {}) {
  const stock = stockDetails(symbol);
  return {
    analysis: {
      quote: {
        code: stock.code,
        market: stock.market,
        name: stock.name,
        price: 100,
        change: 1,
        change_pct: 1,
        source: "E2E行情",
        timestamp: "2026-07-14 10:00:00",
      },
      data_quality: { level: "优秀", score: 95 },
      signal_snapshot: { label: "观察", summary: "E2E" },
      action_advice: { action: "观察", confidence: 60 },
      review: {},
      klines: withKlines ? dailyKlines(240) : [],
    },
    insights: { overview: {} },
    local_data_warnings: degraded ? [{ component: "notes", message: "本地笔记暂不可用" }] : [],
    chart_marks: chartMarks
      ? { marks: [{ category: "买点", price: 100, trade_date: "2026-07-14" }], categories: ["买点"] }
      : { marks: [], categories: [] },
  };
}

function dailyKlines(count) {
  const start = Date.UTC(2025, 10, 17);
  return Array.from({ length: count }, (_, index) => {
    const date = new Date(start + index * 86400000).toISOString().slice(0, 10);
    const open = 90 + index * 0.06 + Math.sin(index / 7) * 1.5;
    const close = open + Math.sin(index / 3) * 0.7;
    return {
      date,
      open,
      close,
      high: Math.max(open, close) + 0.8,
      low: Math.min(open, close) - 0.8,
      volume: 1000000 + index * 1000,
    };
  });
}

function minuteAnalysisPayload(interval, symbol = "600519.SH") {
  const availability = interval === "30m" ? "unavailable" : interval === "60m" ? "degraded" : "ok";
  const rows = minuteKlines(interval, 24);
  return {
    symbol,
    updated_at: rows.at(-1).timestamp,
    interval,
    source: "E2E分钟行情",
    sample_count: rows.length,
    klines: rows,
    availability,
    availability_reason: {
      ok: "分钟分析数据满足分析要求。",
      degraded: "成交量字段降级，价格结构仍可参考。",
      unavailable: "有效样本不足，仅保留审计行。",
    }[availability],
    reason_code: availability === "unavailable" ? "insufficient_samples" : availability === "degraded" ? "volume_unavailable" : "ok",
    latest_price: availability === "unavailable" ? null : rows.at(-1).close,
    intraday_change_pct: 0.8,
    intraday_range_pct: 1.6,
    volume_pulse: availability === "degraded" ? "待确认" : "温和放量",
    trend_label: "盘中偏强",
    momentum_label: "动能温和",
    summary: `${interval} E2E分钟分析`,
    supports: availability === "unavailable" ? [] : [{ label: "盘中支撑", price: 99, strength: 60, reason: "测试" }],
    resistances: availability === "unavailable" ? [] : [{ label: "盘中压力", price: 103, strength: 55, reason: "测试" }],
    t_plan: {
      low_zone: availability === "unavailable" ? "不可用" : "99.00-100.00",
      high_zone: availability === "unavailable" ? "不可用" : "102.00-103.00",
      suitability: availability === "unavailable" ? "等待有效数据" : "仅底仓可做T",
      style: availability === "unavailable" ? "不可用" : "区间型",
      confidence: availability === "unavailable" ? 0 : 60,
      summary: availability === "unavailable" ? "不形成执行区间" : "等待区间确认",
      execution_steps: availability === "unavailable" ? [] : ["等待确认"],
      stop_conditions: availability === "unavailable" ? [] : ["跌破支撑"],
    },
    warnings: availability === "degraded" ? ["成交量不可用"] : [],
    missing_data: availability === "ok" ? [] : [availability === "degraded" ? "分钟成交量" : "有效分钟样本"],
  };
}

function minuteKlines(interval, count) {
  const step = Number.parseInt(interval, 10);
  return Array.from({ length: count }, (_, index) => {
    const minuteOfDay = 9 * 60 + 30 + index * step;
    const hour = String(Math.floor(minuteOfDay / 60)).padStart(2, "0");
    const minute = String(minuteOfDay % 60).padStart(2, "0");
    const open = 100 + index * 0.08 + Math.sin(index / 3) * 0.4;
    const close = open + Math.cos(index / 2) * 0.25;
    return {
      timestamp: `2026-07-15 ${hour}:${minute}:00`,
      interval,
      source: "E2E分钟行情",
      from_cache: false,
      fallback_used: false,
      open,
      close,
      high: Math.max(open, close) + 0.3,
      low: Math.min(open, close) - 0.3,
      volume: 10000 + index * 100,
      amount: 1000000 + index * 1000,
    };
  });
}

function stockDetails(symbol) {
  const rows = {
    "000001.SZ": { code: "000001", market: "SZ", name: "平安银行" },
    "300750.SZ": { code: "300750", market: "SZ", name: "宁德时代" },
  };
  return rows[symbol] || { code: "600519", market: "SH", name: "贵州茅台" };
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

function canonicalWatchlistSymbol(value) {
  const text = String(value || "").trim().toUpperCase();
  if (/^\d{6}\.(SH|SZ)$/.test(text)) return text;
  return `${text.slice(0, 6)}.${text.startsWith("6") ? "SH" : "SZ"}`;
}

function moveExcludedWatchlistItemsLast(items) {
  items.sort((left, right) => Number(left.research_status === "excluded") - Number(right.research_status === "excluded"));
}

async function fulfillJson(route, payload, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: status === 204 ? "" : JSON.stringify(payload),
  });
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
