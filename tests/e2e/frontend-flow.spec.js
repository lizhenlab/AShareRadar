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

test("Beijing name search joins the quote stream and rejects market conflicts", async ({ page }) => {
  const workbenchSymbols = [];
  const streamSymbols = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/stock/workbench") workbenchSymbols.push(url.searchParams.get("symbol"));
    if (url.pathname === "/api/stream/quotes") streamSymbols.push(url.searchParams.get("symbols"));
  });
  await mockApi(page);

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  const input = page.locator("#symbolInput");
  await input.fill("北交");
  await expect(page.locator("#symbolSuggestions")).toContainText("北交样本");
  await expect(page.locator("#symbolSuggestions")).toContainText("920066.BJ");
  await page.locator("#symbolSuggestions button").click();
  await expect(page.locator("#stockCode")).toHaveText("BJ920066");
  await expect(page.locator("#stockName")).toHaveText("北交样本");
  await expect.poll(() => workbenchSymbols).toEqual(["600519", "920066.BJ"]);
  await expect.poll(() => streamSymbols.some((symbols) => symbols?.split(",").includes("920066.BJ"))).toBe(true);

  await input.fill("920066.SH");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#symbolError")).toBeVisible();
  await expect(page.locator("#stockName")).toHaveText("北交样本");
  expect(workbenchSymbols).toEqual(["600519", "920066.BJ"]);
});

test("stock search remains bound after a persisted pagehide lifecycle", async ({ page }) => {
  const searchKeywords = [];
  await mockApi(page, {
    stocks(keyword) {
      searchKeywords.push(keyword);
      return stockSearchPayload(keyword);
    },
  });

  await page.goto("/");
  const input = page.locator("#symbolInput");
  await input.fill("北交");
  await expect(page.locator("#symbolSuggestions")).toContainText("北交样本");
  await page.evaluate(() => {
    window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true }));
    window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
  });
  await input.fill("平安");
  await expect(page.locator("#symbolSuggestions")).toContainText("平安银行");
  await expect.poll(() => searchKeywords).toEqual(["北交", "平安"]);
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
  await input.evaluate((element) => element.blur());
  await input.focus();
  await page.waitForTimeout(150);
  await expect(suggestions).toBeVisible();
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
  await expect(page.locator("#workspace-tab-tools")).toBeVisible();
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

test("full-market scan runs in background and renders a bounded responsive snapshot", async ({ page }, testInfo) => {
  const mobileProject = Boolean(testInfo.project.use.isMobile);
  await page.setViewportSize(mobileProject ? { width: 390, height: 844 } : { width: 1440, height: 900 });
  const resultQueries = [];
  let runPolls = 0;
  let latestCalls = 0;
  let starts = 0;
  const startBodies = [];
  const exportQueries = [];
  const resultQueryStrings = [];
  const exportQueryStrings = [];
  await mockApi(page, {
    async api(url, request) {
      if (url.pathname === "/api/market-scans/latest") {
        latestCalls += 1;
        return { payload: null };
      }
      if (url.pathname === "/api/market-scans" && request.method() === "POST") {
        starts += 1;
        startBodies.push(request.postDataJSON());
        await delay(120);
        return { payload: { accepted: true, deduplicated: false, run: marketScanRunPayload("running", 20) } };
      }
      if (url.pathname === "/api/market-scans/42" && request.method() === "GET") {
        runPolls += 1;
        return { payload: marketScanRunPayload("degraded", 103) };
      }
      if (url.pathname === "/api/market-scans/42/results") {
        resultQueries.push(Object.fromEntries(url.searchParams));
        resultQueryStrings.push(url.searchParams.toString());
        return { payload: marketScanResultPage(url.searchParams) };
      }
      if (url.pathname === "/api/market-scans/42/export.xlsx") {
        exportQueries.push(Object.fromEntries(url.searchParams));
        exportQueryStrings.push(url.searchParams.toString());
        return {
          response: {
            status: 200,
            contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers: { "Content-Disposition": "attachment; filename*=UTF-8''AShareRadar-%E5%85%A8%E5%B8%82%E5%9C%BA%E6%A6%9C%E5%8D%95.xlsx" },
            body: "xlsx fixture",
          },
        };
      }
      return null;
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  const queryControlMetrics = await page.evaluate(() => {
    const input = document.querySelector("#symbolInput");
    const button = document.querySelector("#searchForm button");
    return {
      inputFontSize: Number.parseFloat(getComputedStyle(input).fontSize),
      buttonHeight: button.getBoundingClientRect().height,
    };
  });
  await selectPrimaryView(page, "market");
  await expect(page.locator("#workspace-panel-market-scan")).toBeVisible();
  await expect(page.locator("#marketScanHeadline")).toHaveText("尚无全市场扫描记录");
  await expect(page.locator("#marketScanExport")).toBeDisabled();
  await expect(page.locator("#marketScanProgressBar")).toHaveAttribute("aria-label", "全市场扫描进度");
  await expect(page.locator("#marketScanProgressBar")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#workspace-panel-market-scan [aria-live=polite]")).toHaveCount(1);
  expect(latestCalls).toBe(1);
  await selectPrimaryView(page, "market");
  expect(latestCalls).toBe(1);
  await selectPrimaryView(page, "research");
  await selectPrimaryView(page, "market");
  await expect.poll(() => latestCalls).toBe(2);

  const controlMetrics = await page.evaluate(() => {
    const metric = (selector) => {
      const element = document.querySelector(selector);
      return { fontSize: Number.parseFloat(getComputedStyle(element).fontSize), height: element.getBoundingClientRect().height };
    };
    return {
      marketInput: metric("#marketScanIndustry"),
      scanButton: metric("#marketScanStart"),
    };
  });
  if (mobileProject) {
    expect(controlMetrics.marketInput.fontSize).toBeGreaterThanOrEqual(16);
    expect(queryControlMetrics.inputFontSize).toBeGreaterThanOrEqual(16);
    expect(controlMetrics.scanButton.height).toBeGreaterThanOrEqual(44);
    expect(queryControlMetrics.buttonHeight).toBeGreaterThanOrEqual(44);
  } else {
    expect(controlMetrics.marketInput).toMatchObject({ fontSize: 12, height: 36 });
    expect(controlMetrics.scanButton.height).toBe(36);
  }

  const versionedResources = await page.evaluate(() => performance.getEntriesByType("resource")
    .map((entry) => entry.name)
    .filter((url) => {
      const parsed = new URL(url);
      return parsed.pathname.startsWith("/static/") && parsed.searchParams.has("v");
    }));
  expect(new Set(versionedResources.map((url) => new URL(url).searchParams.get("v"))).size).toBe(1);
  expect(versionedResources.some((url) => url.includes("/static/app.js?v="))).toBe(true);
  expect(versionedResources.some((url) => url.includes("/static/css/market-scan.css?v="))).toBe(true);
  expect(versionedResources.some((url) => url.includes("/static/js/market-scan.js?v="))).toBe(true);
  expect(versionedResources.some((url) => url.includes("/static/js/market-scan-controller.js?v="))).toBe(true);
  expect(versionedResources.some((url) => url.includes("/static/js/market-scan-contracts.js?v="))).toBe(true);
  expect(versionedResources.some((url) => url.includes("/static/js/market-scan-polling.js?v="))).toBe(true);
  expect(versionedResources.some((url) => url.includes("/static/js/market-scan-view.js?v="))).toBe(true);

  await page.locator("#marketScanModeOfficial").check();
  await page.locator("#marketScanStart").click();
  await expect(page.locator("#workspace-panel-market-scan")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#marketScanMarket")).toBeFocused();
  await expect(page.locator("#marketScanAnnouncement")).toContainText("开始扫描请求处理中");
  await page.locator("#marketScanStart").evaluate((button) => button.click());
  await expect(page.locator("#marketScanProgressText")).toHaveText("20/103 · 19.4%");
  await expect(page.locator("#marketScanStage")).toHaveText("K 线获取");
  await expect(page.locator("#marketScanElapsed")).toHaveText("12 秒");
  await expect(page.locator("#marketScanThroughput")).toContainText("只/秒");
  await expect(page.locator("#marketScanEta")).not.toHaveText("估算中");
  expect(starts).toBe(1);
  expect(startBodies).toEqual([{ mode: "official" }]);
  await expect(page.locator("#workspace-panel-market-scan")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#marketScanProgressBar")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#marketScanStart")).toBeDisabled();
  await expect(page.locator("#marketScanExport")).toBeDisabled();
  await expect(page.locator("#marketScanCancel")).toBeVisible();
  await expect(page.locator("#marketScanResultState")).toContainText("盘后正式榜单");

  await selectPrimaryView(page, "research");
  const globalProgress = page.locator("#marketScanGlobalProgress");
  await expect(globalProgress).toBeVisible();
  await expect(globalProgress).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#marketScanGlobalText")).toContainText("扫描中");
  await expect(page.locator("#marketScanGlobalText")).toContainText("20/103");
  await expect(page.locator("#marketScanGlobalCancel")).toBeVisible();
  await page.locator("#marketScanGlobalOpen").click();
  await expect(page.locator("#workspace-panel-market-scan")).toBeVisible();

  await expect(page.locator("#marketScanProgressText")).toHaveText("103/103 · 100.0%", { timeout: 5000 });
  await expect(page.locator("#marketScanProgressBar")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#workspace-panel-market-scan")).toHaveAttribute("aria-busy", "false");
  await expect(globalProgress).toBeHidden();
  await expect(page.locator("#marketScanHeadline")).toContainText("降级完成");
  await expect(page.locator("#marketScanCoverage")).toHaveText("98.1%");
  await expect(page.locator("#marketScanStage")).toHaveText("已结束");
  await expect(page.locator("#marketScanMarketProgress")).toContainText("SH");
  await expect(page.locator("#marketScanMarketProgress")).toContainText("BJ");
  await expect(page.locator("#marketScanDiagnostic")).toContainText("等待数据源恢复后从断点重试");
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(100);
  await expect(page.locator("#marketScanExport")).toBeEnabled();
  await expect(page.locator("#marketScanPageText")).toHaveText("第 1/2 页 · 共 101 条");
  await expect(page.locator("#marketScanAnnouncement")).toHaveText("盘后正式榜单加载完成，第 1/2 页，本页 100 条，共 101 条。");
  await expect(page.locator("#marketScanRows tr.market-scan-result-row").first()).toContainText("北交样本");
  const frozenEvidenceButton = page.locator('button[data-market-scan-snapshot-target]').first();
  await frozenEvidenceButton.click();
  const frozenEvidence = page.locator("#marketScanRows tr.market-scan-snapshot-row").first();
  await expect(frozenEvidence).toBeVisible();
  await expect(frozenEvidence).toContainText("只读持久化证据");
  await expect(frozenEvidence).toContainText("原始排名分");
  await expect(frozenEvidence).toContainText("最终排序规则");
  await expect(frozenEvidence).toContainText("E2E行情");
  await expect(frozenEvidence).toContainText("前复权（qfq）");
  await frozenEvidenceButton.click();
  await expect(frozenEvidence).toBeHidden();
  expect(runPolls).toBe(1);
  expect(resultQueries[0]).toMatchObject({ page: "1", page_size: "100", status: "success", sort: "rank", order: "asc" });

  await expect(page.locator("#marketScanTableWrap")).toHaveAttribute("tabindex", "0");
  const responsiveTable = await page.locator("#marketScanTableWrap").evaluate((element) => {
    const header = element.querySelector("thead th:nth-child(2)");
    const stock = element.querySelector("tbody td:nth-child(2)");
    return {
      overflow: element.scrollWidth > element.clientWidth + 1,
      headerPosition: getComputedStyle(header).position,
      stockPosition: getComputedStyle(stock).position,
      detailLabels: Array.from(element.querySelectorAll("tbody tr:first-child td")).map((cell) => cell.dataset.label || ""),
    };
  });
  if (mobileProject) {
    expect(responsiveTable.overflow).toBe(false);
    expect(responsiveTable.detailLabels).toEqual([
      "排名", "股票", "市场 / 行业", "短线强势", "趋势", "涨跌幅", "换手率", "成交额", "质量", "状态 / 标签",
    ]);
  } else {
    expect(responsiveTable.headerPosition).toBe("sticky");
    expect(responsiveTable.stockPosition).toBe("sticky");
  }

  await page.locator("#marketScanNext").click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(1);
  await expect(page.locator("#marketScanTableWrap")).toBeFocused();
  expect(resultQueries.at(-1).page).toBe("2");
  await page.locator("#marketScanPrev").click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(100);

  await page.locator("#marketScanSort").selectOption("score");
  await expect(page.locator("#marketScanOrder")).toHaveValue("desc");
  await page.locator("#marketScanOrder").selectOption("asc");
  await page.locator("#marketScanFilters button[type=submit]").click();
  await expect.poll(() => resultQueries.at(-1)).toMatchObject({ sort: "score", order: "asc" });
  await expect(page.locator("#marketScanPagination")).toHaveAttribute("aria-busy", "false");
  const visibleScores = await page.locator("#marketScanRows tr.market-scan-result-row td:nth-child(4)").evaluateAll((cells) => cells.slice(0, 20).map((cell) => Number(cell.textContent)));
  expect(visibleScores).toEqual([...visibleScores].sort((left, right) => left - right));

  await page.locator("#marketScanMarket").selectOption(["SH", "BJ"]);
  await page.locator("#marketScanIndustry").fill("白酒，专用设备");
  await page.locator("#marketScanSt").selectOption("true");
  await page.locator("#marketScanNew").selectOption("true");
  await page.locator("#marketScanScoreMin").fill("90");
  await page.locator("#marketScanScoreMax").fill("100");
  await page.locator("#marketScanTrendMin").fill("90");
  await page.locator("#marketScanTrendMax").fill("100");
  await page.locator("#marketScanChangeMin").fill("1");
  await page.locator("#marketScanChangeMax").fill("2");
  await page.locator("#marketScanTurnoverMin").fill("2");
  await page.locator("#marketScanTurnoverMax").fill("3");
  await page.locator("#marketScanAmountMin").fill("100000000");
  await page.locator("#marketScanAmountMax").fill("130000000");
  await page.locator("#marketScanQuality").fill("90");
  await page.locator("#marketScanQualityMax").fill("100");
  await page.locator("#marketScanKeyword").fill("北交样本");
  await page.locator("#marketScanSort2").selectOption("trend_score");
  await page.locator("#marketScanOrder2").selectOption("desc");
  await page.locator("#marketScanSort3").selectOption("symbol");
  await page.locator("#marketScanOrder3").selectOption("asc");
  await page.locator("#marketScanFilters button[type=submit]").click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(1);
  expect(resultQueries.at(-1)).toMatchObject({
    is_st: "true",
    is_new: "true",
    min_score: "90",
    max_score: "100",
    min_trend_score: "90",
    max_trend_score: "100",
    min_change_pct: "1",
    max_change_pct: "2",
    min_turnover_rate: "2",
    max_turnover_rate: "3",
    min_amount: "100000000",
    max_amount: "130000000",
    min_data_quality_score: "90",
    max_data_quality_score: "100",
    keyword: "北交样本",
  });
  const resultQuery = new URLSearchParams(resultQueryStrings.at(-1));
  expect(resultQuery.getAll("market")).toEqual(["SH", "BJ"]);
  expect(resultQuery.getAll("industry")).toEqual(["白酒", "专用设备"]);
  expect(resultQuery.getAll("sort")).toEqual(["score", "trend_score", "symbol"]);
  expect(resultQuery.getAll("order")).toEqual(["asc", "desc", "asc"]);
  const downloadPromise = page.waitForEvent("download");
  await page.locator("#marketScanExport").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("AShareRadar-全市场榜单.xlsx");
  expect(exportQueries).toHaveLength(1);
  const expectedExportQuery = { ...resultQueries.at(-1) };
  delete expectedExportQuery.page;
  delete expectedExportQuery.page_size;
  expect(exportQueries[0]).toEqual(expectedExportQuery);
  const exportQuery = new URLSearchParams(exportQueryStrings[0]);
  expect(exportQuery.toString()).toBe(resultQuery.toString().replace(/^page=1&page_size=100&/, ""));
  await expect(page.locator("#marketScanExport")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#marketScanAnnouncement")).toContainText("Excel 榜单已导出");

  await page.locator("#marketScanFilters button[type=reset]").click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(100);
  await page.locator("#marketScanStatus").selectOption("missing");
  await page.locator("#marketScanFilters button[type=submit]").click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(1);
  await expect(page.locator("#marketScanRows")).toContainText("行情缺失样本");
  expect(resultQueries.at(-1).status).toBe("missing");

  const layout = await page.locator("#workspace-panel-market-scan").evaluate((panel) => {
    const rect = panel.getBoundingClientRect();
    const tableWrap = panel.querySelector("#marketScanTableWrap");
    return {
      left: rect.left,
      right: rect.right,
      viewport: document.documentElement.clientWidth,
      documentWidth: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth),
      tableScrollable: tableWrap.scrollWidth > tableWrap.clientWidth + 1,
    };
  });
  expect(layout.left).toBeGreaterThanOrEqual(-1);
  expect(layout.right).toBeLessThanOrEqual(layout.viewport + 1);
  expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewport);
  expect(layout.tableScrollable).toBe(false);

  await page.locator("#marketScanFilters button[type=reset]").click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(100);
  await page.locator('button[data-market-scan-symbol="920066.BJ"]').click();
  await expect(page.locator("#workspace-panel-overview")).toBeVisible();
  await expect(page.locator("#workspace-panel-market-scan")).toBeHidden();
  await expect(page.locator("#stockName")).toHaveText("北交样本");
  await expect(page.locator("#stockCode")).toHaveText("BJ920066");
  await expect(page.locator("#stockWorkbench")).toBeFocused();
  await expect(page.locator("#currentAnalysisContext")).toBeVisible();
  await expect(page.locator("#currentAnalysisContext")).toContainText("不是批次 #42 的冻结快照");
});

test("market-scan mode isolation and historical selection keep one explicit result batch", async ({ page }) => {
  const listQueries = [];
  const resultRunIds = [];
  const exportRunIds = [];
  let latestCalls = 0;
  const activeIntraday = marketScanRunPayload("running", 20, {
    id: 90,
    mode: "intraday",
    message: "盘中任务正在运行",
  });
  const officialLatest = marketScanRunPayload("success", 103, { id: 42, mode: "official" });
  const officialHistory = marketScanRunPayload("degraded", 103, {
    id: 40,
    mode: "official",
    quote_date: "2026-07-16",
    data_date: "2026-07-16",
  });
  const intradayLatest = marketScanRunPayload("degraded", 103, {
    id: 38,
    mode: "intraday",
    quote_date: "2026-07-15",
    data_date: "2026-07-14",
  });
  await mockApi(page, {
    async api(url) {
      if (url.pathname === "/api/market-scans/latest") {
        latestCalls += 1;
        return { payload: activeIntraday };
      }
      if (url.pathname === "/api/market-scans/latest-published") {
        return { payload: url.searchParams.get("mode") === "intraday" ? intradayLatest : officialLatest };
      }
      if (url.pathname === "/api/market-scans") {
        listQueries.push(url.searchParams.toString());
        const intraday = url.searchParams.get("mode") === "intraday";
        const items = intraday ? [intradayLatest] : [officialLatest, officialHistory];
        return {
          payload: {
            items,
            total: items.length,
            page: 1,
            page_size: 100,
            page_count: 1,
          },
        };
      }
      const resultMatch = url.pathname.match(/^\/api\/market-scans\/(\d+)\/results$/);
      if (resultMatch) {
        const runId = Number(resultMatch[1]);
        resultRunIds.push(runId);
        const run = runId === 40 ? officialHistory : runId === 38 ? intradayLatest : officialLatest;
        return { payload: marketScanResultPage(url.searchParams, run) };
      }
      const exportMatch = url.pathname.match(/^\/api\/market-scans\/(\d+)\/export\.xlsx$/);
      if (exportMatch) {
        exportRunIds.push(Number(exportMatch[1]));
        return {
          response: {
            status: 200,
            contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers: { "Content-Disposition": "attachment; filename=history.xlsx" },
            body: "history xlsx",
          },
        };
      }
      if (url.pathname === "/api/market-scans/90") return { payload: activeIntraday };
      return null;
    },
  });

  await page.goto("/");
  await page.locator("#marketScanModeOfficial").check();
  await selectPrimaryView(page, "market");
  await expect(page.locator("#marketScanBrowseContext")).toContainText("盘后正式 · 最近发布 #42");
  await expect(page.locator("#marketScanTaskContext")).toContainText("盘中临时 #90");
  await expect(page.locator("#marketScanTaskContext")).toContainText("不同");
  await expect(page.locator("#marketScanHistoryRun option")).toHaveCount(3);

  await page.locator("#marketScanHistoryRun").selectOption("40");
  await expect(page.locator("#marketScanBrowseContext")).toContainText("历史批次 #40");
  await expect(page.locator("#marketScanTableWrap")).toHaveAttribute("data-market-scan-run-id", "40");
  await page.locator("#marketScanKeyword").fill("北交样本");
  await page.locator("#marketScanFilters button[type=submit]").click();
  await expect.poll(() => resultRunIds.at(-1)).toBe(40);
  const downloadPromise = page.waitForEvent("download");
  await page.locator("#marketScanExport").click();
  await downloadPromise;
  expect(exportRunIds.at(-1)).toBe(40);

  await page.locator("#marketScanHistoryStatus").selectOption("success");
  await page.locator("#marketScanHistoryDate").fill("2026-07-17");
  await page.locator("#marketScanHistoryRefresh").click();
  await expect.poll(() => listQueries.some((query) => {
    const params = new URLSearchParams(query);
    return params.get("mode") === "official"
      && params.get("status") === "success"
      && params.get("data_date") === "2026-07-17";
  })).toBe(true);

  await page.locator("#marketScanModeIntraday").check();
  await expect(page.locator("#marketScanBrowseContext")).toContainText("盘中临时 · 最近发布 #38");
  await expect(page.locator("#marketScanTaskContext")).not.toContainText("不同");
  await expect(page.locator("#marketScanTableWrap")).toHaveAttribute("data-market-scan-run-id", "38");
  expect(resultRunIds).toContain(42);
  expect(resultRunIds).toContain(40);
  expect(resultRunIds).toContain(38);
  expect(latestCalls).toBeGreaterThanOrEqual(2);
});

for (const viewport of [
  { name: "360", width: 360, height: 800 },
  { name: "390", width: 390, height: 844 },
  { name: "430", width: 430, height: 860 },
  { name: "desktop", width: 1440, height: 900 },
]) {
  test(`discovery presets, research queue, and adjacent ranks work at ${viewport.name}`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-chromium", "viewport matrix runs once in desktop Chromium");
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const discovery = discoveryApiHarness();
    await mockApi(page, { api: discovery.api });
    page.on("dialog", (dialog) => dialog.accept());

    await page.goto("/");
    await expect(page.locator("#stockName")).toHaveText("贵州茅台");
    await selectPrimaryView(page, "market");
    await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(100);

    await page.locator("#marketScanMarket").selectOption("SH");
    await page.locator("#marketScanIndustry").fill("白酒");
    await page.locator("#marketScanSt").selectOption("false");
    await page.locator("#marketScanNew").selectOption("false");
    await page.locator("#marketScanQuality").fill("85");
    await page.locator("#marketScanStatus").selectOption("missing");
    await page.locator("#marketScanKeyword").fill("600519");
    await page.locator("#marketScanSort").selectOption("trend_score");
    await page.locator("#marketScanOrder").selectOption("desc");
    await page.locator("#discoveryPresetName").fill(`高质量白酒-${viewport.name}`);
    await page.locator("#discoveryPresetSave").click();
    await expect(page.locator("#discoveryPresetFeedback")).toContainText("暂不支持保存状态、搜索关键词");
    expect(discovery.calls.create).toHaveLength(0);

    await page.locator("#marketScanStatus").selectOption("success");
    await page.locator("#marketScanKeyword").fill("");
    await page.locator("#discoveryPresetSave").click();

    await expect(page.locator("#discoveryPresetFeedback")).toContainText("已保存");
    await expect(page.locator("#discoveryPresetSelect")).toHaveValue("7");
    expect(discovery.calls.create).toHaveLength(1);
    expect(discovery.calls.create[0]).toEqual({
      name: `高质量白酒-${viewport.name}`,
      criteria: {
        market: ["SH"],
        industry: ["白酒"],
        is_st: false,
        is_new: false,
        quality: { min: 85 },
      },
      sort: [{ field: "trend", order: "desc" }],
    });

    await page.locator("#discoveryPresetApply").click();
    await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(2);
    await expect(page.locator("#marketScanRows tr.market-scan-result-row").first()).toContainText("上升 1");
    await expect(page.locator("#marketScanRows tr.market-scan-result-row").nth(1)).toContainText("新进");
    await expect(page.locator("#discoveryRankSummary")).toContainText("批次 41 → 42");
    await expect(page.locator("#discoveryRankSummary")).toContainText("规则 leader-v2");
    expect(discovery.calls.apply.at(-1)).toEqual({ run_id: 42, page: 1, page_size: 100 });
    expect(discovery.calls.rankChanges).toBe(1);

    const queueButton = page.locator('button[data-discovery-enqueue-symbol="600519.SH"]');
    await expect(queueButton).toHaveText("加入研究队列");
    await queueButton.click();
    await expect(page.locator("#discoveryPresetFeedback")).toContainText("已加入研究队列");
    expect(discovery.calls.enqueue.at(-1)).toEqual({
      run_id: 42,
      expected_preset_revision: 1,
      symbols: ["600519.SH"],
    });
    await expect(queueButton).toBeDisabled();

    await page.locator("#discoveryPresetName").fill(`白酒观察-${viewport.name}`);
    await page.locator("#discoveryPresetRename").click();
    await expect(page.locator("#discoveryPresetSelect option:checked")).toHaveText(`白酒观察-${viewport.name}`);
    expect(discovery.calls.rename.at(-1)).toEqual({
      name: `白酒观察-${viewport.name}`,
      expected_revision: 1,
    });

    await page.locator("#discoveryPresetDelete").click();
    await expect(page.locator("#discoveryPresetFeedback")).toContainText("已删除");
    await expect(page.locator("#discoveryPresetSelect")).toHaveValue("");
    expect(discovery.calls.remove.at(-1)).toEqual({ presetId: 7, revision: 2 });

    const layout = await page.evaluate(() => {
      const controls = document.querySelector("#discoveryPresetControls").getBoundingClientRect();
      const viewportWidth = document.documentElement.clientWidth;
      return {
        controlsLeft: controls.left,
        controlsRight: controls.right,
        documentWidth: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth),
        viewportWidth,
      };
    });
    expect(layout.controlsLeft).toBeGreaterThanOrEqual(-1);
    expect(layout.controlsRight).toBeLessThanOrEqual(layout.viewportWidth + 1);
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth);
  });
}

for (const viewport of [
  { name: "mobile", width: 390, height: 844 },
  { name: "desktop", width: 1440, height: 900 },
]) {
  test(`advanced discovery editing and bulk queue preserve the plan at ${viewport.name}`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-chromium", "viewport matrix runs once in desktop Chromium");
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const discovery = discoveryApiHarness();
    await mockApi(page, { api: discovery.api });

    await page.goto("/");
    await selectPrimaryView(page, "market");
    await page.locator("#marketScanMarket").selectOption(["SH", "BJ"]);
    await page.locator("#marketScanIndustry").fill("白酒，专用设备");
    await page.locator("#marketScanSt").selectOption("false");
    await page.locator("#marketScanNew").selectOption("false");
    await page.locator("#marketScanScoreMin").fill("80");
    await page.locator("#marketScanScoreMax").fill("99");
    await page.locator("#marketScanTrendMin").fill("70");
    await page.locator("#marketScanTrendMax").fill("98");
    await page.locator("#marketScanChangeMin").fill("-2.5");
    await page.locator("#marketScanChangeMax").fill("8.5");
    await page.locator("#marketScanTurnoverMin").fill("0.5");
    await page.locator("#marketScanTurnoverMax").fill("12");
    await page.locator("#marketScanAmountMin").fill("100000000");
    await page.locator("#marketScanAmountMax").fill("5000000000");
    await page.locator("#marketScanQuality").fill("85");
    await page.locator("#marketScanQualityMax").fill("100");
    await page.locator("#marketScanSort").selectOption("score");
    await page.locator("#marketScanOrder").selectOption("desc");
    await page.locator("#marketScanSort2").selectOption("trend_score");
    await page.locator("#marketScanOrder2").selectOption("desc");
    await page.locator("#marketScanSort3").selectOption("symbol");
    await page.locator("#marketScanOrder3").selectOption("asc");
    await page.locator("#discoveryPresetName").fill(`完整方案-${viewport.name}`);
    await page.locator("#discoveryPresetSave").click();

    await expect(page.locator("#discoveryPresetFeedback")).toContainText("已保存");
    expect(discovery.calls.create[0]).toEqual({
      name: `完整方案-${viewport.name}`,
      criteria: {
        market: ["SH", "BJ"],
        industry: ["白酒", "专用设备"],
        is_st: false,
        is_new: false,
        score: { min: 80, max: 99 },
        trend: { min: 70, max: 98 },
        change: { min: -2.5, max: 8.5 },
        turnover: { min: 0.5, max: 12 },
        amount: { min: 100000000, max: 5000000000 },
        quality: { min: 85, max: 100 },
      },
      sort: [
        { field: "score", order: "desc" },
        { field: "trend", order: "desc" },
        { field: "symbol", order: "asc" },
      ],
    });

    await page.locator("#marketScanScoreMax").fill("98");
    await page.locator("#discoveryPresetSave").click();
    await expect(page.locator("#discoveryPresetFeedback")).toContainText("已更新");
    expect(discovery.calls.update).toHaveLength(1);
    expect(discovery.calls.update[0].expected_revision).toBe(1);
    expect(discovery.calls.update[0].criteria.score).toEqual({ min: 80, max: 98 });

    await page.locator("#discoveryPresetApply").click();
    await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(2);
    await expect(page.locator("#discoveryBulkControls")).toBeVisible();
    await page.locator("#discoverySelectPage").check();
    await expect(page.locator("#discoverySelectedCount")).toHaveText("已选 2 项");
    await page.locator("#discoveryEnqueueSelected").click();
    await expect(page.locator("#discoveryPresetFeedback")).toContainText("新增 2");
    expect(discovery.calls.enqueue.at(-1)).toEqual({
      run_id: 42,
      expected_preset_revision: 2,
      symbols: ["600519.SH", "600809.SH"],
    });

    await page.locator("#discoveryEnqueueAll").click();
    await expect(page.locator("#discoveryPresetFeedback")).toContainText("当前筛选结果处理完成");
    expect(discovery.calls.apply.at(-1)).toEqual({ run_id: 42, page: 1, page_size: 100 });
    expect(discovery.calls.enqueue.at(-1).symbols).toEqual(["600519.SH", "600809.SH"]);

    if (viewport.name === "desktop") {
      const downloadPromise = page.waitForEvent("download");
      await page.locator("#discoveryPresetExport").click();
      const download = await downloadPromise;
      expect(download.suggestedFilename()).toBe("完整方案-desktop.json");
      expect(discovery.calls.export).toBe(1);

      const archive = discoveryArchive(discovery.calls.create[0]);
      await page.locator("#discoveryPresetImportFile").setInputFiles({
        name: "imported-plan.json",
        mimeType: "application/json",
        buffer: Buffer.from(JSON.stringify(archive)),
      });
      await expect(page.locator("#discoveryPresetFeedback")).toContainText("已导入");
      expect(discovery.calls.import).toEqual([archive]);
      await expect(page.locator("#discoveryPresetSelect")).toHaveValue("8");
    }
  });
}

test("full-market scan cancellation stays unpublished and retry derives a new snapshot", async ({ page }) => {
  let starts = 0;
  let cancels = 0;
  let retries = 0;
  let polls = 0;
  let resultCalls = 0;
  await mockApi(page, {
    async api(url, request) {
      if (url.pathname === "/api/market-scans/latest") return { payload: null };
      if (url.pathname === "/api/market-scans" && request.method() === "POST") {
        starts += 1;
        await delay(100);
        return { payload: { accepted: true, deduplicated: false, run: marketScanRunPayload("running", 20, { id: 43 }) } };
      }
      if (url.pathname === "/api/market-scans/43/cancel" && request.method() === "POST") {
        cancels += 1;
        await delay(100);
        return { payload: marketScanRunPayload("cancelled", 20, { id: 43, message: "全市场扫描已取消，可从断点重试" }) };
      }
      if (url.pathname === "/api/market-scans/43/retry" && request.method() === "POST") {
        retries += 1;
        await delay(100);
        return {
          payload: {
            accepted: true,
            deduplicated: false,
            run: marketScanRunPayload("running", 20, {
              id: 44,
              retry_of_run_id: 43,
              trigger: "retry",
              retry_count: 1,
            }),
          },
        };
      }
      if (url.pathname === "/api/market-scans/44" && request.method() === "GET") {
        polls += 1;
        return {
          payload: marketScanRunPayload("degraded", 103, {
            id: 44,
            retry_of_run_id: 43,
            trigger: "retry",
            retry_count: 1,
          }),
        };
      }
      if (url.pathname === "/api/market-scans/44/results") {
        resultCalls += 1;
        return {
          payload: marketScanResultPage(url.searchParams, {
            id: 44,
            retry_of_run_id: 43,
            trigger: "retry",
            retry_count: 1,
          }),
        };
      }
      return null;
    },
  });

  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanStart").click();
  await expect(page.locator("#marketScanStart")).toBeDisabled();
  await page.locator("#marketScanStart").evaluate((button) => button.click());
  expect(starts).toBe(1);

  await page.locator("#marketScanCancel").click();
  await expect(page.locator("#workspace-panel-market-scan")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#marketScanCancel")).toBeDisabled();
  await page.locator("#marketScanCancel").evaluate((button) => button.click());
  await expect(page.locator("#marketScanHeadline")).toContainText("已取消");
  await expect(page.locator("#marketScanMarket")).toBeFocused();
  await expect(page.locator("#marketScanResultState")).toContainText("未发布盘后正式榜单");
  await expect(page.locator("#marketScanRetry")).toBeVisible();
  expect(cancels).toBe(1);
  expect(resultCalls).toBe(0);

  await page.locator("#marketScanRetry").click();
  await expect(page.locator("#workspace-panel-market-scan")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#marketScanRetry")).toBeDisabled();
  await page.locator("#marketScanRetry").evaluate((button) => button.click());
  await expect(page.locator("#marketScanStart")).toBeDisabled();
  await expect(page.locator("#marketScanMarket")).toBeFocused();
  await expect(page.locator("#marketScanProgressText")).toHaveText("103/103 · 100.0%", { timeout: 5000 });
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(100);
  expect(retries).toBe(1);
  expect(polls).toBe(1);
  expect(resultCalls).toBe(1);
});

test("full-market scan recovers a stale run, rejects malformed results, and resyncs online", async ({ page }) => {
  let latestCalls = 0;
  let staleRunCalls = 0;
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/market-scans/latest") {
        latestCalls += 1;
        if (latestCalls === 1) {
          return { payload: marketScanRunPayload("running", 10, { id: 91, message: "旧任务仍在运行" }) };
        }
        if (latestCalls === 2) {
          return { payload: marketScanRunPayload("success", 103, { id: 92, message: "最近任务已完成" }) };
        }
        return { payload: marketScanRunPayload("running", 30, { id: 93, message: "网络恢复后的任务" }) };
      }
      if (url.pathname === "/api/market-scans/91") {
        staleRunCalls += 1;
        return { status: 404, payload: { detail: "全市场扫描批次不存在：91" } };
      }
      if (url.pathname === "/api/market-scans/92/results") {
        return {
          payload: {
            run: marketScanRunPayload("success", 103, { id: 92, message: "最近任务已完成" }),
            total: 0,
            page: 1,
            page_size: 100,
            page_count: 0,
          },
        };
      }
      if (url.pathname === "/api/market-scans/93") {
        return { payload: marketScanRunPayload("running", 30, { id: 93, message: "网络恢复后的任务" }) };
      }
      return null;
    },
  });

  await page.goto("/");
  await selectPrimaryView(page, "market");
  await expect(page.locator("#marketScanProgressBar")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#marketScanResultState")).toContainText("响应格式异常", { timeout: 5000 });
  await expect(page.locator("#marketScanResultState")).toContainText("items 必须是数组");
  await expect(page.locator("#marketScanTableWrap")).toBeHidden();
  expect(staleRunCalls).toBe(1);
  expect(latestCalls).toBe(2);

  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect.poll(() => latestCalls).toBe(3);
  await expect(page.locator("#marketScanHeadline")).toHaveText("网络已恢复，正在同步最近扫描。");
  await expect(page.locator("#marketScanProgressBar")).toHaveAttribute("aria-busy", "true");
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
  await selectPrimaryView(page, "monitor");
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

test("historical watchlist scans stay read-only until current analysis is explicitly requested", async ({ page }) => {
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
      return {
        ...workbenchPayload(symbol),
        alert_rules: [{
          id: 7,
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

test("restored tabs and tools retain the last successful current stock", async ({ page }) => {
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
  await expect(page.locator("#symbolInput")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#searchForm button")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#quickList button").first()).toBeFocused();

  const overviewTab = page.locator("#workspace-tab-overview");
  const qaTab = page.locator("#workspace-tab-qa");
  const themeTab = page.locator("#workspace-tab-theme");
  const replayTab = page.locator("#workspace-tab-replay");
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
  await expect(toolsTab).toBeFocused();

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
  const monitorLayout = await narrowPrimaryLayout(page, [".control-panel", ".side-column"]);
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

function primaryViewButton(page, view) {
  return page.locator(`#primaryNavigation button[data-primary-view="${view}"]`);
}

async function selectPrimaryView(page, view) {
  await primaryViewButton(page, view).click();
  await expectPrimaryView(page, view);
}

async function expectPrimaryView(page, view) {
  await expect(page.locator("body")).toHaveAttribute("data-primary-view", view);
  await expect(primaryViewButton(page, view)).toHaveAttribute("aria-current", "page");
  await expect.poll(() => page.locator("#primaryNavigation button[data-primary-view]").evaluateAll(
    (buttons) => buttons.map((button) => ({
      view: button.dataset.primaryView,
      current: button.getAttribute("aria-current"),
    }))
  )).toEqual(["research", "market", "review", "monitor"].map((candidate) => ({
    view: candidate,
    current: candidate === view ? "page" : "false",
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

function discoveryApiHarness() {
  let preset = null;
  const calls = {
    apply: [], create: [], enqueue: [], export: 0, import: [],
    rankChanges: 0, remove: [], rename: [], update: [],
  };
  return {
    calls,
    async api(url, request) {
      const pathname = url.pathname;
      if (pathname === "/api/market-scans/latest") {
        return { payload: marketScanRunPayload("success", 103) };
      }
      if (pathname === "/api/market-scans/42/results") {
        return { payload: marketScanResultPage(url.searchParams) };
      }
      if (pathname === "/api/discovery/presets" && request.method() === "GET") {
        return {
          payload: {
            items: preset ? [preset] : [],
            total: preset ? 1 : 0,
            page: 1,
            page_size: 100,
            page_count: preset ? 1 : 0,
          },
        };
      }
      if (pathname === "/api/discovery/presets" && request.method() === "POST") {
        const payload = request.postDataJSON();
        calls.create.push(payload);
        preset = discoveryPreset(payload, 1);
        return { payload: preset, status: 201 };
      }
      if (pathname === "/api/discovery/presets/import" && request.method() === "POST") {
        const archive = request.postDataJSON();
        calls.import.push(archive);
        preset = { ...discoveryPreset(archive.preset, 1), id: 8 };
        return { payload: preset, status: 201 };
      }
      if (pathname === "/api/discovery/presets/7" && request.method() === "PUT") {
        const payload = request.postDataJSON();
        calls.update.push(payload);
        const { expected_revision: _expectedRevision, ...definition } = payload;
        preset = discoveryPreset(definition, preset.revision + 1);
        return { payload: preset };
      }
      if (pathname === "/api/discovery/presets/7" && request.method() === "PATCH") {
        const payload = request.postDataJSON();
        calls.rename.push(payload);
        preset = { ...preset, name: payload.name, revision: 2, updated_at: "2026-07-28T11:00:00Z" };
        return { payload: preset };
      }
      if (pathname === "/api/discovery/presets/7" && request.method() === "DELETE") {
        calls.remove.push({ presetId: 7, revision: Number(url.searchParams.get("expected_revision")) });
        preset = null;
        return { payload: { deleted: true, preset_id: 7 } };
      }
      if (pathname === "/api/discovery/presets/7/export" && request.method() === "GET") {
        calls.export += 1;
        return { payload: discoveryArchive(preset) };
      }
      if (pathname === "/api/discovery/presets/7/apply" && request.method() === "POST") {
        const payload = request.postDataJSON();
        calls.apply.push(payload);
        return { payload: discoveryLeaderboard(preset, payload) };
      }
      if (pathname === "/api/discovery/presets/7/research-queue" && request.method() === "POST") {
        const payload = request.postDataJSON();
        calls.enqueue.push(payload);
        return {
          payload: {
            items: payload.symbols.map((symbol) => ({
              symbol,
              source_run_id: payload.run_id,
              source_preset_id: 7,
              source_preset_revision: payload.expected_preset_revision,
              source_preset_name: preset.name,
              enqueued_at: "2026-07-28T11:10:00Z",
              added: true,
            })),
            added_count: payload.symbols.length,
            existing_count: 0,
          },
        };
      }
      if (pathname === "/api/discovery/runs/42/rank-changes") {
        calls.rankChanges += 1;
        return { payload: discoveryRankChanges() };
      }
      return null;
    },
  };
}

function discoveryArchive(definition) {
  return {
    format: "ashare-radar.discovery-preset",
    schema_version: 1,
    checksum_algorithm: "sha256",
    checksum: "a".repeat(64),
    exported_at: "2026-07-28T12:00:00Z",
    preset: {
      name: definition.name,
      criteria: definition.criteria,
      sort: definition.sort,
    },
  };
}

function discoveryPreset(payload, revision) {
  return {
    ...payload,
    id: 7,
    schema_version: 1,
    revision,
    created_at: "2026-07-28T10:00:00Z",
    updated_at: "2026-07-28T10:00:00Z",
  };
}

function discoveryLeaderboard(preset, payload) {
  return {
    preset,
    run_id: payload.run_id,
    rule_version: "leader-v2",
    items: [
      {
        position: 1, source_rank: 1, symbol: "600519.SH", code: "600519", market: "SH",
        name: "贵州茅台", industry: "白酒", is_st: false, is_new: false,
        quality: 96, trend: 91, change: 2.4, turnover: 1.2, amount: 1800000000, score: 95,
      },
      {
        position: 2, source_rank: 4, symbol: "600809.SH", code: "600809", market: "SH",
        name: "山西汾酒", industry: "白酒", is_st: false, is_new: false,
        quality: 93, trend: 89, change: 1.8, turnover: 0.9, amount: 920000000, score: 92,
      },
    ],
    total: 2,
    page: payload.page,
    page_size: payload.page_size,
    page_count: 1,
  };
}

function discoveryRankChanges() {
  return {
    current_run_id: 42,
    previous_run_id: 41,
    current_rule_version: "leader-v2",
    previous_rule_version: "leader-v2",
    comparable: true,
    reason: null,
    items: [
      { symbol: "600519.SH", code: "600519", market: "SH", name: "贵州茅台", previous_rank: 2, current_rank: 1, rank_delta: 1, movement: "up" },
      { symbol: "600809.SH", code: "600809", market: "SH", name: "山西汾酒", previous_rank: null, current_rank: 4, rank_delta: null, movement: "new" },
      { symbol: "000001.SZ", code: "000001", market: "SZ", name: "平安银行", previous_rank: 3, current_rank: null, rank_delta: null, movement: "exit" },
    ],
    total: 3,
    page: 1,
    page_size: 200,
    page_count: 1,
  };
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
    if (custom?.response) {
      await route.fulfill(custom.response);
      return;
    }
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
  if (pathname === "/api/discovery/presets") {
    return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
  }
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
    {
      symbol: "920066.BJ",
      code: "920066",
      market: "BJ",
      name: "北交样本",
      industry: "专用设备",
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
    "920066.BJ": { code: "920066", market: "BJ", name: "北交样本" },
  };
  return rows[symbol] || { code: "600519", market: "SH", name: "贵州茅台" };
}

function marketScanRunPayload(status, processedCount, overrides = {}) {
  const totalCount = 103;
  const published = status === "success" || status === "degraded";
  const terminal = published || ["failed", "cancelled", "interrupted"].includes(status);
  const successCount = status === "success" ? totalCount : status === "degraded" ? 101 : Math.min(processedCount, 20);
  const progressPct = terminal && published ? 100 : Number(((processedCount / totalCount) * 100).toFixed(2));
  const messages = {
    success: `全市场扫描完成：成功 ${totalCount}/${totalCount}`,
    degraded: "全市场扫描降级完成：成功 101/103，缺失 1，跳过 1",
    failed: "全市场扫描失败",
    cancelled: "全市场扫描已取消",
    interrupted: "应用重启中断扫描",
  };
  const elapsedSeconds = terminal ? 299 : status === "running" ? 12 : 0;
  const throughput = elapsedSeconds > 0 ? processedCount / elapsedSeconds : null;
  const marketTotals = [40, 40, 23];
  let remainingProcessed = processedCount;
  const marketProgress = ["SH", "SZ", "BJ"].map((market, index) => {
    const total = marketTotals[index];
    const processed = Math.min(total, Math.max(0, remainingProcessed));
    remainingProcessed -= processed;
    const missing = status === "degraded" && market === "BJ" ? 1 : 0;
    const skipped = status === "degraded" && market === "SZ" ? 1 : 0;
    const succeeded = Math.max(0, processed - missing - skipped);
    return {
      market,
      total_count: total,
      processed_count: processed,
      success_count: succeeded,
      missing_count: missing,
      skipped_count: skipped,
      coverage_pct: Number(((succeeded / total) * 100).toFixed(2)),
    };
  });
  return {
    id: 42,
    status,
    trigger: "manual",
    mode: "official",
    rule_version: "full-market-score-v1",
    as_of: "2026-07-17 16:30:00",
    data_date: "2026-07-17",
    quote_date: "2026-07-17",
    scope: "沪市 + 深市 + 北交所当前上市A股",
    total_count: totalCount,
    excluded_count: 2,
    processed_count: processedCount,
    success_count: successCount,
    missing_count: status === "degraded" ? 1 : 0,
    skipped_count: status === "degraded" ? 1 : 0,
    retry_count: 0,
    progress_pct: progressPct,
    coverage_pct: Number(((successCount / totalCount) * 100).toFixed(2)),
    created_at: "2026-07-17 16:30:00",
    updated_at: terminal ? "2026-07-17 16:35:00" : "2026-07-17 16:31:00",
    started_at: "2026-07-17 16:30:01",
    finished_at: terminal ? "2026-07-17 16:35:00" : null,
    duration_ms: terminal ? 299000 : null,
    current_stage: status === "running" ? "klines" : null,
    stage_started_at: status === "running" ? "2026-07-17 16:30:10" : null,
    stage_metrics: {
      stock_pool: { duration_ms: 120, work_duration_ms: 120, calls: 1, items: totalCount },
      bulk_quotes: { duration_ms: 800, work_duration_ms: 800, calls: 1, items: processedCount },
      klines: { duration_ms: 10000, work_duration_ms: 18000, calls: 1, items: processedCount },
    },
    market_progress: marketProgress,
    elapsed_seconds: elapsedSeconds,
    throughput_per_second: throughput,
    eta_seconds: status === "running" && processedCount >= 20 && throughput
      ? (totalCount - processedCount) / throughput
      : null,
    message: messages[status] || `已处理 ${processedCount} 只股票`,
    last_error: status === "degraded" ? "provider 数据源超时，已使用受控降级结果" : null,
    ...overrides,
  };
}

function marketScanResultPage(searchParams = new URLSearchParams(), runOverrides = {}) {
  const run = marketScanRunPayload("degraded", 103, runOverrides);
  let items = marketScanFixtureRows().map((item) => ({ ...item, run_id: run.id }));
  const status = searchParams.get("status") || "success";
  if (status !== "all") items = items.filter((item) => item.status === status);
  const markets = searchParams.getAll("market");
  const industries = searchParams.getAll("industry");
  const isSt = searchParams.get("is_st");
  const isNew = searchParams.get("is_new");
  const keyword = (searchParams.get("keyword") || "").trim();
  if (markets.length) items = items.filter((item) => markets.includes(item.market));
  if (industries.length) items = items.filter((item) => industries.some((industry) => item.industry.includes(industry)));
  if (isSt) items = items.filter((item) => item.is_st === (isSt === "true"));
  if (isNew) items = items.filter((item) => item.is_new === (isNew === "true"));
  items = filterMarketScanRange(items, searchParams, "score", "min_score", "max_score");
  items = filterMarketScanRange(items, searchParams, "trend_score", "min_trend_score", "max_trend_score");
  items = filterMarketScanRange(items, searchParams, "change_pct", "min_change_pct", "max_change_pct");
  items = filterMarketScanRange(items, searchParams, "turnover_rate", "min_turnover_rate", "max_turnover_rate");
  items = filterMarketScanRange(items, searchParams, "amount", "min_amount", "max_amount");
  items = filterMarketScanRange(items, searchParams, "data_quality_score", "min_data_quality_score", "max_data_quality_score");
  if (keyword) items = items.filter((item) => `${item.symbol} ${item.code} ${item.name}`.includes(keyword));

  const sort = searchParams.get("sort") || "rank";
  const direction = searchParams.get("order") === "desc" ? -1 : 1;
  items.sort((left, right) => marketScanResultCompare(left, right, sort, direction));
  const pageSize = Number(searchParams.get("page_size")) || 100;
  const page = Number(searchParams.get("page")) || 1;
  const total = items.length;
  const pageCount = total ? Math.ceil(total / pageSize) : 0;
  const offset = (page - 1) * pageSize;
  return {
    run,
    total,
    page,
    page_size: pageSize,
    page_count: pageCount,
    items: items.slice(offset, offset + pageSize),
  };
}

function filterMarketScanRange(items, searchParams, field, minimumName, maximumName) {
  const minimumText = searchParams.get(minimumName);
  const maximumText = searchParams.get(maximumName);
  const minimum = minimumText === null ? null : Number(minimumText);
  const maximum = maximumText === null ? null : Number(maximumText);
  return items.filter((item) => (
    (minimum === null || Number(item[field]) >= minimum)
    && (maximum === null || Number(item[field]) <= maximum)
  ));
}

function marketScanFixtureRows() {
  const successes = Array.from({ length: 101 }, (_, index) => {
    if (index === 0) {
      return marketScanResult("920066.BJ", "*ST北交样本", "BJ", 1, 99, {
        industry: "专用设备",
        isSt: true,
        isNew: true,
        quality: 96,
      });
    }
    if (index === 1) return marketScanResult("600519.SH", "贵州茅台", "SH", 2, 96, { industry: "白酒" });
    const market = ["SH", "SZ", "BJ"][index % 3];
    const code = market === "SH"
      ? String(600000 + index)
      : market === "SZ"
        ? String(index + 1).padStart(6, "0")
        : String(920000 + index);
    return marketScanResult(`${code}.${market}`, `排名样本${index + 1}`, market, index + 1, 55 + ((index * 17) % 44), {
      industry: index % 2 ? "银行" : "电子",
      isSt: index % 31 === 0,
      isNew: market === "BJ" && index % 4 === 0,
      quality: 70 + (index % 27),
    });
  });
  return [
    ...successes,
    marketScanResult("300999.SZ", "行情缺失样本", "SZ", null, null, { status: "missing", error: "批量行情缺失" }),
    marketScanResult("600999.SH", "停牌样本", "SH", null, null, { status: "skipped", reason: "日K停留在前一交易日" }),
  ];
}

function marketScanResultCompare(left, right, sort, direction) {
  const leftValue = left[sort];
  const rightValue = right[sort];
  if (leftValue == null && rightValue != null) return 1;
  if (leftValue != null && rightValue == null) return -1;
  let compared = typeof leftValue === "string"
    ? leftValue.localeCompare(rightValue)
    : Number(leftValue || 0) - Number(rightValue || 0);
  compared *= direction;
  return compared || (left.rank ?? Number.MAX_SAFE_INTEGER) - (right.rank ?? Number.MAX_SAFE_INTEGER) || left.symbol.localeCompare(right.symbol);
}

function marketScanResult(symbol, name, market, rank, score, options = {}) {
  const status = options.status || "success";
  const success = status === "success";
  return {
    run_id: 42,
    symbol,
    code: symbol.slice(0, 6),
    market,
    name,
    industry: options.industry || (market === "BJ" ? "专用设备" : "白酒"),
    is_st: Boolean(options.isSt),
    is_new: Boolean(options.isNew),
    status,
    rank,
    score: success ? score : null,
    raw_score: success ? score - 0.125 : null,
    trend_score: success ? score - 5 : null,
    leader_score: success ? score : null,
    data_quality_score: success ? (options.quality || 92) : null,
    price: success ? 10.5 : null,
    change_pct: success ? 1.2 : null,
    turnover_rate: success ? 2.4 : null,
    volume_ratio: success ? 1.1 : null,
    amount: success ? 120000000 : null,
    tags: success ? ["趋势向上"] : [],
    metrics: {},
    score_details: success ? {
      schema_version: 1,
      run_rule_version: "full-market-score-v1",
      score_spec_hash: "e2e-score-spec-0001",
      components: {
        leader_score: { base: score - 10, trend_delta: 5, rule_deltas: { change: 5 } },
        final_score: { base: score + 4, quality_penalty: 4, rank_discount: 0.125, raw: score - 0.125, score },
        rank_refinement: { score: 0.875, weighted_terms: { trend_delta: 0.5 } },
      },
      ranking: {
        tie_break: [["raw_score", "desc"], ["symbol", "asc"]],
        tie_break_values: { raw_score: score - 0.125, symbol },
      },
    } : {},
    reason: options.reason || (success ? `短线强势分 ${score}` : null),
    error: options.error || null,
    data_date: "2026-07-17",
    quote_timestamp: "2026-07-17 15:00:00",
    quote_source: "E2E行情",
    kline_source: "E2E日线",
    metadata_source: "E2E股票池",
    adjustment_mode: "qfq",
    quote_fallback_used: false,
    kline_fallback_used: false,
    metadata_degraded: false,
    degradation_reasons: [],
    updated_at: "2026-07-17 16:35:00",
  };
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
