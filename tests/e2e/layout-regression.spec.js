import { expect, test } from "@playwright/test";

const VIEWPORTS = [
  {
    name: "desktop 1440x900",
    width: 1440,
    height: 900,
    layout: "three-column",
    scanColumns: { summary: 7, presets: 3, filters: 5, table: 10 },
  },
  {
    name: "compact desktop 1024x768",
    width: 1024,
    height: 768,
    layout: "two-column",
    scanColumns: { summary: 4, presets: 2, filters: 3, table: 10 },
  },
  {
    name: "tablet 768x900",
    width: 768,
    height: 900,
    layout: "stacked",
    scanColumns: { summary: 2, presets: 2, filters: 2, table: 2 },
  },
  {
    name: "mobile 390x844",
    width: 390,
    height: 844,
    layout: "stacked",
    scanColumns: { summary: 2, presets: 1, filters: 2, table: 2 },
  },
];

test.describe("responsive layout regression", () => {
  for (const viewport of VIEWPORTS) {
    test(`${viewport.name} keeps the workspace and side tools usable`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop-chromium", "the explicit viewport matrix runs once");
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await mockLayoutApi(page);
      await installLayoutProbe(page);

      await page.goto("/");
      await page.addStyleTag({
        content: "*, *::before, *::after { animation: none !important; transition: none !important; }",
      });
      await expect(page.locator("#stockName")).toHaveText("贵州茅台");
      await expect(page.locator("#marketStrip .index-card")).toHaveCount(3);
      await expect(page.locator("#leaderList .leader-row")).toHaveCount(4);
      await expect(page.locator("#plateList .leader-row")).toHaveCount(3);
      await settleLayout(page);

      await assertNoDocumentOverflow(page);
      await assertHeaderAndQueryFit(page, viewport);
      await assertWorkspaceTabsAreReachable(page, viewport);
      await assertGlobalProgressDoesNotCoverTabs(page, viewport);
      await assertChartToolbarFits(page, viewport);
      await assertPrimaryLayout(page, viewport);

      await page.locator("#workspace-tab-market-scan").click();
      await expect(page.locator("#workspace-panel-market-scan")).toBeVisible();
      await expect(page.locator("#marketScanRows tr")).toHaveCount(3);
      await settleLayout(page);

      await assertNoDocumentOverflow(page);
      await assertMarketScanLayout(page, viewport);
      await assertPrimaryLayout(page, viewport);
      await assertSideToolsAreReachable(page, viewport);
    });
  }
});

async function assertNoDocumentOverflow(page) {
  await expect.poll(() => page.evaluate(() => {
    const viewportWidth = document.documentElement.clientWidth;
    const documentWidth = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
    return documentWidth - viewportWidth;
  })).toBeLessThanOrEqual(1);
}

async function assertHeaderAndQueryFit(page, viewport) {
  const measurements = await page.evaluate(() => {
    const rect = (selector) => window.__layoutRect(document.querySelector(selector));
    return {
      viewportWidth: document.documentElement.clientWidth,
      topbar: rect(".topbar"),
      topbarCopy: rect(".topbar-copy"),
      topbarStatus: rect(".topbar-status"),
      statusPill: rect("#dataStatus"),
      query: rect(".query-panel"),
      searchInput: rect("#symbolInput"),
      searchButton: rect("#searchForm button"),
    };
  });

  expectWithinViewport(measurements.topbar, measurements.viewportWidth);
  expectWithinViewport(measurements.statusPill, measurements.viewportWidth);
  expectWithinViewport(measurements.query, measurements.viewportWidth);
  expectWithinViewport(measurements.searchInput, measurements.viewportWidth);
  expectWithinViewport(measurements.searchButton, measurements.viewportWidth);
  expect(measurements.topbarCopy.right).toBeLessThanOrEqual(measurements.topbarStatus.left + 1);
  expect(measurements.searchInput.right).toBeLessThanOrEqual(measurements.searchButton.left + 1);
  expect(measurements.searchButton.height).toBeGreaterThanOrEqual(viewport.width <= 820 ? 44 : 42);
}

async function assertWorkspaceTabsAreReachable(page, viewport) {
  const tabs = page.locator(".workspace-tabs");
  const first = page.locator("#workspace-tab-overview");
  const last = page.locator("#workspace-tab-tools");
  await expect(tabs).toBeVisible();
  await expect(first).toBeVisible();

  const initial = await tabs.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
    overflowX: getComputedStyle(element).overflowX,
    position: getComputedStyle(element).position,
    stickyTop: Number.parseFloat(getComputedStyle(element).top),
    rect: window.__layoutRect(element),
    topbarHeight: document.querySelector(".topbar").getBoundingClientRect().height,
    firstHeight: element.querySelector("button").getBoundingClientRect().height,
  }));
  expect(initial.overflowX).toBe("auto");
  expect(initial.position).toBe(viewport.width <= 820 ? "static" : "sticky");
  expect(initial.firstHeight).toBeGreaterThanOrEqual(viewport.width <= 820 ? 44 : 30);
  expectWithinViewport(initial.rect, viewport.width);
  if (viewport.width > 820) {
    expect(initial.stickyTop).toBeGreaterThanOrEqual(initial.topbarHeight - 1);
  }

  await tabs.evaluate((element) => {
    element.scrollLeft = element.scrollWidth;
  });
  const end = await page.evaluate(() => ({
    tabs: window.__layoutRect(document.querySelector(".workspace-tabs")),
    last: window.__layoutRect(document.querySelector("#workspace-tab-tools")),
  }));
  expect(end.last.left).toBeGreaterThanOrEqual(end.tabs.left - 1);
  expect(end.last.right).toBeLessThanOrEqual(end.tabs.right + 1);
  await tabs.evaluate((element) => {
    element.scrollLeft = 0;
  });
}

async function assertGlobalProgressDoesNotCoverTabs(page, viewport) {
  if (viewport.width <= 820) return;
  const progress = page.locator("#marketScanGlobalProgress");
  await progress.evaluate((element) => {
    element.hidden = false;
  });
  await settleLayout(page);
  const metrics = await page.evaluate(() => {
    const progressElement = document.querySelector("#marketScanGlobalProgress");
    const tabsElement = document.querySelector(".workspace-tabs");
    return {
      progressHeight: progressElement.getBoundingClientRect().height,
      progressTop: Number.parseFloat(getComputedStyle(progressElement).top),
      tabsTop: Number.parseFloat(getComputedStyle(tabsElement).top),
    };
  });
  expect(metrics.tabsTop).toBeGreaterThanOrEqual(metrics.progressTop + metrics.progressHeight - 1);
  await progress.evaluate((element) => {
    element.hidden = true;
  });
}

async function assertChartToolbarFits(page, viewport) {
  const metrics = await page.locator("#chartWorkspace").evaluate((workspace) => {
    const visible = (selector) => Array.from(workspace.querySelectorAll(selector))
      .filter((element) => element.getClientRects().length > 0)
      .map((element) => ({
        rect: window.__layoutRect(element),
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
      }));
    return {
      workspace: window.__layoutRect(workspace),
      mobileSwitchDisplay: getComputedStyle(workspace.querySelector(".chart-mobile-switch")).display,
      mobileSwitchButtons: visible(".chart-mobile-switch button"),
      panes: visible(".research-chart-pane"),
      toolbars: visible(".chart-toolbar"),
      controls: visible(".chart-controls, #minuteIntervalControls"),
    };
  });

  expectWithinViewport(metrics.workspace, viewport.width);
  for (const target of [...metrics.panes, ...metrics.toolbars, ...metrics.controls]) {
    expect(target.rect.left).toBeGreaterThanOrEqual(metrics.workspace.left - 1);
    expect(target.rect.right).toBeLessThanOrEqual(metrics.workspace.right + 1);
    expect(target.scrollWidth).toBeLessThanOrEqual(target.clientWidth + 1);
  }

  if (viewport.width <= 820) {
    expect(metrics.mobileSwitchDisplay).not.toBe("none");
    expect(metrics.mobileSwitchButtons).toHaveLength(2);
    for (const button of metrics.mobileSwitchButtons) expect(button.rect.height).toBeGreaterThanOrEqual(44);
    expect(metrics.panes).toHaveLength(1);
  } else {
    expect(metrics.mobileSwitchDisplay).toBe("none");
    expect(metrics.panes).toHaveLength(2);
  }
}

async function assertPrimaryLayout(page, viewport) {
  const layout = await page.evaluate(() => {
    const elementRect = (selector) => window.__layoutRect(document.querySelector(selector));
    const sidePanels = Array.from(document.querySelectorAll(".side-column > .panel")).map(window.__layoutRect);
    const layoutElement = document.querySelector("main.layout");
    const sideColumn = document.querySelector(".side-column");
    return {
      display: getComputedStyle(layoutElement).display,
      viewportWidth: document.documentElement.clientWidth,
      query: elementRect(".query-panel"),
      workspace: elementRect(".workspace"),
      controls: elementRect(".control-panel"),
      side: elementRect(".side-column"),
      sidePanels,
      sideColumnCount: window.__layoutGridColumnCount(sideColumn),
      marketColumnCount: window.__layoutGridColumnCount(document.querySelector(".market-strip")),
    };
  });

  for (const target of [layout.query, layout.workspace, layout.controls, layout.side, ...layout.sidePanels]) {
    expectWithinViewport(target, layout.viewportWidth);
  }
  expect(layout.marketColumnCount).toBe(3);

  if (viewport.layout === "three-column") {
    expect(layout.display).toBe("grid");
    expect(layout.query.right).toBeLessThanOrEqual(layout.workspace.left - 17);
    expect(layout.workspace.right).toBeLessThanOrEqual(layout.side.left - 17);
    expect(layout.query.top).toBeCloseTo(layout.workspace.top, 0);
    expect(layout.workspace.top).toBeCloseTo(layout.side.top, 0);
    expect(layout.controls.left).toBeCloseTo(layout.query.left, 0);
    expect(layout.controls.width).toBeCloseTo(layout.query.width, 0);
    expect(layout.sideColumnCount).toBe(1);
    expect(layout.sidePanels[1].top).toBeGreaterThan(layout.sidePanels[0].bottom);
    return;
  }

  if (viewport.layout === "two-column") {
    expect(layout.display).toBe("grid");
    expect(layout.query.right).toBeLessThanOrEqual(layout.workspace.left - 17);
    expect(layout.query.top).toBeCloseTo(layout.workspace.top, 0);
    expect(layout.controls.left).toBeCloseTo(layout.query.left, 0);
    expect(layout.controls.width).toBeCloseTo(layout.query.width, 0);
    expect(layout.side.left).toBeCloseTo(layout.query.left, 0);
    expect(layout.side.right).toBeCloseTo(layout.workspace.right, 0);
    expect(layout.side.top).toBeGreaterThan(layout.workspace.top);
    expect(layout.sideColumnCount).toBe(2);
    expect(layout.sidePanels[0].top).toBeCloseTo(layout.sidePanels[1].top, 0);
    expect(layout.sidePanels[0].right).toBeLessThanOrEqual(layout.sidePanels[1].left - 17);
    return;
  }

  expect(layout.display).toBe("flex");
  expect(layout.query.top).toBeLessThan(layout.workspace.top);
  expect(layout.workspace.top).toBeLessThan(layout.controls.top);
  expect(layout.workspace.top).toBeLessThan(layout.side.top);
  const auxiliarySections = [layout.controls, layout.side].sort((left, right) => left.top - right.top);
  expect(auxiliarySections[0].bottom).toBeLessThanOrEqual(auxiliarySections[1].top - 17);
  for (const target of [layout.workspace, layout.controls, layout.side]) {
    expect(target.left).toBeCloseTo(layout.query.left, 0);
    expect(target.width).toBeCloseTo(layout.query.width, 0);
  }
  expect(layout.sideColumnCount).toBe(1);
  expect(layout.sidePanels[1].top).toBeGreaterThan(layout.sidePanels[0].bottom);
}

async function assertMarketScanLayout(page, viewport) {
  const metrics = await page.locator("#workspace-panel-market-scan").evaluate((panel) => {
    const rect = (selector) => window.__layoutRect(panel.querySelector(selector));
    const tableWrap = panel.querySelector("#marketScanTableWrap");
    const table = panel.querySelector(".market-scan-table");
    const firstRow = panel.querySelector("#marketScanRows tr");
    const actions = panel.querySelector(".market-scan-actions");
    const start = panel.querySelector("#marketScanStart");
    return {
      panel: window.__layoutRect(panel),
      heading: rect(".market-scan-heading"),
      progress: rect("#marketScanProgress"),
      summary: rect("#marketScanSummary"),
      presets: rect("#discoveryPresetControls"),
      filters: rect("#marketScanFilters"),
      tableWrap: window.__layoutRect(tableWrap),
      pagination: rect("#marketScanPagination"),
      summaryColumns: window.__layoutGridColumnCount(panel.querySelector("#marketScanSummary")),
      presetColumns: window.__layoutGridColumnCount(panel.querySelector("#discoveryPresetControls")),
      filterColumns: window.__layoutGridColumnCount(panel.querySelector("#marketScanFilters")),
      rowColumns: window.__layoutGridColumnCount(firstRow),
      tableDisplay: getComputedStyle(table).display,
      tableScrollable: tableWrap.scrollWidth > tableWrap.clientWidth + 1,
      tableClientWidth: tableWrap.clientWidth,
      tableScrollWidth: tableWrap.scrollWidth,
      tableClientHeight: tableWrap.clientHeight,
      tableMaxHeight: Number.parseFloat(getComputedStyle(tableWrap).maxHeight),
      tableOverflowY: getComputedStyle(tableWrap).overflowY,
      actionRect: window.__layoutRect(actions),
      startRect: window.__layoutRect(start),
    };
  });

  for (const target of [
    metrics.panel,
    metrics.heading,
    metrics.progress,
    metrics.summary,
    metrics.presets,
    metrics.filters,
    metrics.tableWrap,
    metrics.pagination,
  ]) {
    expectWithinViewport(target, viewport.width);
  }
  expect(metrics.summaryColumns).toBe(viewport.scanColumns.summary);
  expect(metrics.presetColumns).toBe(viewport.scanColumns.presets);
  expect(metrics.filterColumns).toBe(viewport.scanColumns.filters);
  expect(metrics.startRect.height).toBeGreaterThanOrEqual(viewport.width <= 820 ? 44 : 36);
  expect(metrics.startRect.width).toBeGreaterThan(0);
  expect(metrics.startRect.left).toBeGreaterThanOrEqual(metrics.actionRect.left - 1);
  expect(metrics.startRect.right).toBeLessThanOrEqual(metrics.actionRect.right + 1);

  if (viewport.width <= 820) {
    expect(metrics.tableDisplay).toBe("block");
    expect(metrics.tableScrollable).toBe(false);
    expect(metrics.tableScrollWidth).toBeLessThanOrEqual(metrics.tableClientWidth + 1);
    expect(metrics.rowColumns).toBe(viewport.scanColumns.table);
    expect(metrics.tableOverflowY).toBe("auto");
    expect(metrics.tableMaxHeight).toBeGreaterThan(0);
    expect(metrics.tableMaxHeight).toBeLessThanOrEqual(viewport.height * 0.72 + 1);
    expect(metrics.tableClientHeight).toBeLessThanOrEqual(metrics.tableMaxHeight + 1);
  } else {
    expect(metrics.tableDisplay).toBe("table");
    expect(metrics.tableScrollable).toBe(true);
  }
}

async function assertSideToolsAreReachable(page, viewport) {
  const watchSubmit = page.locator("#watchForm button[type=submit]");
  const sidePanels = page.locator(".side-column > .panel");
  await watchSubmit.scrollIntoViewIfNeeded();
  await expect(watchSubmit).toBeVisible();
  await expect(watchSubmit).toBeEnabled();
  await expect(sidePanels).toHaveCount(3);
  await sidePanels.first().scrollIntoViewIfNeeded();
  await expect(sidePanels.first()).toBeVisible();

  const controls = await page.evaluate(() => ({
    watchButton: window.__layoutRect(document.querySelector("#watchForm button[type=submit]")),
    sidePanel: window.__layoutRect(document.querySelector(".side-column > .panel")),
  }));
  expect(controls.watchButton.height).toBeGreaterThanOrEqual(viewport.width <= 820 ? 44 : 36);
  expect(controls.watchButton.width).toBeGreaterThan(0);
  expect(controls.sidePanel.width).toBeGreaterThan(0);
}

function expectWithinViewport(rect, viewportWidth) {
  expect(rect.width).toBeGreaterThan(0);
  expect(rect.left).toBeGreaterThanOrEqual(-1);
  expect(rect.right).toBeLessThanOrEqual(viewportWidth + 1);
}

async function settleLayout(page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function installLayoutProbe(page) {
  await page.addInitScript(() => {
    window.__layoutRect = (element) => {
      const rect = element.getBoundingClientRect();
      return {
        top: rect.top,
        right: rect.right,
        bottom: rect.bottom,
        left: rect.left,
        width: rect.width,
        height: rect.height,
      };
    };
    window.__layoutGridColumnCount = (element) => {
      if (!element || getComputedStyle(element).display !== "grid") return 0;
      const columns = getComputedStyle(element).gridTemplateColumns.trim();
      return columns ? columns.split(/\s+/).length : 0;
    };
  });
}

async function mockLayoutApi(page) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/stream/quotes") {
      await route.continue();
      return;
    }
    await fulfillJson(route, layoutApiPayload(url, request));
  });
}

function layoutApiPayload(url, request) {
  const pathname = url.pathname;
  if (pathname === "/api/market") {
    return {
      indices: [
        { name: "上证指数", price: 4021.35, change_pct: 0.62 },
        { name: "深证成指", price: 15388.21, change_pct: 0.91 },
        { name: "创业板指", price: 3966.73, change_pct: 1.08 },
      ],
    };
  }
  if (pathname === "/api/strong-stocks") {
    return {
      scope: "观察池 + 默认样本",
      sample_count: 3,
      items: [
        strongStock("600519", "贵州茅台", 1, 76),
        strongStock("000001", "平安银行", 2, 68),
        strongStock("300750", "宁德时代", 3, 64),
      ],
    };
  }
  if (pathname === "/api/plates") {
    return [
      { name: "白酒", rank: 1, change_pct: 1.42, leading_stock: "贵州茅台" },
      { name: "银行", rank: 2, change_pct: 0.83, leading_stock: "平安银行" },
      { name: "新能源", rank: 3, change_pct: 0.55, leading_stock: "宁德时代" },
    ];
  }
  if (pathname === "/api/stock/workbench") return workbenchPayload();
  if (pathname === "/api/stock/minute-analysis") return minuteAnalysisPayload(url.searchParams.get("interval") || "5m");
  if (pathname === "/api/watchlist" && request.method() === "GET") return [watchlistPayload()];
  if (pathname === "/api/data/status") {
    return {
      providers: [],
      source_plan: {},
      cache: {},
      capabilities: [],
      capability_statuses: [],
      minute_analysis_available: true,
      llm_explanation_available: true,
    };
  }
  if (pathname === "/api/tasks/status") return { enabled: false, running: false, tasks: [] };
  if (pathname === "/api/tasks/runs" || pathname === "/api/monitor/events") return [];
  if (pathname === "/api/advice/timeline" || pathname === "/api/reviews") return [];
  if (pathname === "/api/system/diagnostics") return [];
  if (pathname === "/api/discovery/presets") {
    return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
  }
  if (pathname === "/api/market-scans/latest" || pathname === "/api/market-scans/latest-published") {
    return marketScanRun();
  }
  if (pathname === "/api/market-scans/42/results") return marketScanResults();
  return [];
}

function strongStock(code, name, rank, score) {
  return {
    code,
    name,
    rank,
    leader_score: score,
    change_pct: rank === 1 ? 1.75 : 0.86,
    reason: "趋势结构与成交活跃度保持稳定",
    tags: ["观察", "趋势"],
  };
}

function workbenchPayload() {
  return {
    analysis: {
      quote: {
        code: "600519",
        market: "SH",
        name: "贵州茅台",
        price: 1278.56,
        change: 12.45,
        change_pct: 0.98,
        source: "E2E行情",
        timestamp: "2026-07-28 15:00:00",
      },
      trend_score: 72,
      trend_label: "趋势偏强，等待确认",
      action_advice: { action: "观察", confidence: 72 },
      support: 1248.2,
      resistance: 1326.8,
      ma5: 1266.4,
      ma20: 1239.1,
      beginner_summary: "当前趋势偏强，但仍需结合支撑、压力与数据质量确认，不宜只依据单一分数追涨。",
      data_quality: { level: "优秀", score: 94, notes: [] },
      signal_snapshot: { label: "观察", summary: "等待量价进一步确认" },
      buy_points: [],
      sell_points: [],
      t_plan: [],
      review: {},
      klines: [],
    },
    insights: {
      overview: {
        total_score: 72,
        total_level: "偏强",
        main_conflict: "趋势保持向上，但短线位置接近压力区。",
        beginner_takeaways: ["观察支撑有效性", "避免压力位附近追涨"],
        key_prices: [],
        factors: [],
      },
    },
    chart_marks: { marks: [], categories: [] },
    alert_rules: [],
    alert_events: [],
    notes: [],
    local_data_warnings: [],
  };
}

function minuteAnalysisPayload(interval) {
  return {
    symbol: "600519.SH",
    updated_at: "2026-07-28 15:00:00",
    interval,
    source: "E2E分钟行情",
    sample_count: 0,
    klines: [],
    availability: "unavailable",
    availability_reason: "当前没有分钟样本，保留日线分析。",
    reason_code: "insufficient_samples",
    latest_price: null,
    intraday_change_pct: null,
    intraday_range_pct: null,
    volume_pulse: "待确认",
    trend_label: "待确认",
    momentum_label: "待确认",
    summary: "分钟样本暂不可用",
    supports: [],
    resistances: [],
    t_plan: {
      low_zone: "不可用",
      high_zone: "不可用",
      suitability: "等待有效数据",
      style: "不可用",
      confidence: 0,
      summary: "不形成执行区间",
      execution_steps: [],
      stop_conditions: [],
    },
    warnings: [],
    missing_data: ["有效分钟样本"],
  };
}

function watchlistPayload() {
  return {
    symbol: "600519.SH",
    code: "600519",
    market: "SH",
    name: "贵州茅台",
    latest_price: 1278.56,
    latest_change_pct: 0.98,
    note: "关注支撑确认与压力区表现",
    group_name: "核心观察",
    pinned: true,
    research_status: "watching",
    priority: "high",
    next_review_date: "2026-07-30",
    last_viewed_at: null,
    unread_change_count: 2,
  };
}

function marketScanRun() {
  return {
    id: 42,
    status: "success",
    trigger: "manual",
    rule_version: "full-market-score-v3:085ad665",
    as_of: "2026-07-28 16:30:00",
    data_date: "2026-07-28",
    scope: "沪市 + 深市 + 北交所当前上市A股",
    total_count: 3,
    excluded_count: 0,
    processed_count: 3,
    success_count: 3,
    missing_count: 0,
    skipped_count: 0,
    retry_count: 0,
    progress_pct: 100,
    coverage_pct: 100,
    task_run_id: null,
    retry_of_run_id: null,
    stock_pool_source: "E2E股票池",
    created_at: "2026-07-28 16:30:00",
    updated_at: "2026-07-28 16:35:00",
    started_at: "2026-07-28 16:30:01",
    finished_at: "2026-07-28 16:35:00",
    duration_ms: 299000,
    message: "全市场扫描完成：有效排名 3/3",
    last_error: null,
    cancel_requested_at: null,
  };
}

function marketScanResults() {
  const run = marketScanRun();
  const rows = [
    marketScanResult("600519.SH", "贵州茅台", "SH", "白酒", 1, 91),
    marketScanResult("300750.SZ", "宁德时代", "SZ", "电池", 2, 86),
    marketScanResult("920066.BJ", "北交设备样本", "BJ", "专用设备", 3, 82),
  ];
  return {
    run,
    total: rows.length,
    page: 1,
    page_size: 100,
    page_count: 1,
    items: rows,
  };
}

function marketScanResult(symbol, name, market, industry, rank, score) {
  return {
    run_id: 42,
    symbol,
    code: symbol.slice(0, 6),
    market,
    name,
    industry,
    list_date: "2010-01-01",
    metadata_source: "E2E股票池",
    is_st: false,
    is_new: false,
    status: "success",
    rank,
    score,
    trend_score: score - 5,
    leader_score: score,
    data_quality_score: 94,
    price: 100,
    change_pct: 1.2,
    turnover_rate: 2.4,
    volume_ratio: 1.1,
    amount: 120000000,
    tags: ["趋势向上", "量价稳定"],
    metrics: {},
    reason: `短线强势分 ${score}，趋势结构保持稳定。`,
    error: null,
    data_date: "2026-07-28",
    quote_timestamp: "2026-07-28 15:00:00",
    quote_source: "E2E行情",
    kline_source: "E2E日线",
    adjustment_mode: "qfq",
    updated_at: "2026-07-28 16:35:00",
  };
}

async function fulfillJson(route, payload, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(payload),
  });
}
