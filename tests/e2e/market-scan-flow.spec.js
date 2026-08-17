import { expect, test } from "@playwright/test";
import { filterMarketScanRange, filterMarketScanResearchRange, marketScanResultCompare } from "./market-scan-fixture-filters.mjs";
import { delay, mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("full-market scan runs in background and renders a bounded responsive snapshot", async ({ page }, testInfo) => {
  testInfo.setTimeout(45000);
  const mobileProject = Boolean(testInfo.project.use.isMobile);
  const expectedPageSize = mobileProject ? 30 : 100;
  const expectedPageCount = Math.ceil(101 / expectedPageSize);
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
        await delay(500);
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
  await expect(page.locator("#marketScanFilterPanel")).toBeHidden();
  await expect(page.locator("#marketScanDetails")).toBeHidden();
  await expect(page.locator("#strategyLab")).toBeHidden();
  await page.locator("#marketScanFilterToggle").click();
  await expect(page.locator("#marketScanFilterPanel")).toBeVisible();
  await expect(page.locator("#marketScanHeadline")).toHaveText("尚无全市场扫描记录");
  await expect(page.locator("#marketScanExport")).toBeDisabled();
  await expect(page.locator("#marketScanProgressBar")).toHaveAttribute("aria-label", "全市场扫描进度");
  await expect(page.locator("#marketScanProgressBar")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#marketScanHistory")).toBeHidden();
  await expect(page.locator("#workspace-panel-market-scan [aria-live=polite]")).toHaveCount(2);
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
  expect(versionedResources.some((url) => url.includes("/static/css/market-scan-research.css?v="))).toBe(true);
  expect(versionedResources.some((url) => url.includes("/static/js/market-scan.js?v="))).toBe(true);
  expect(versionedResources.some((url) => url.includes("/static/js/market-scan-controller.js?v="))).toBe(true);
  expect(versionedResources.some((url) => url.includes("/static/js/market-scan-contracts.js?v="))).toBe(true);
  expect(versionedResources.some((url) => url.includes("/static/js/market-scan-message-view.js?v="))).toBe(true);
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
  await expect(page.locator("#marketScanCoverage")).toHaveText("99.0%");
  await expect(page.locator("#marketScanStage")).toHaveText("已结束");
  await expect(page.locator("#marketScanMarketProgress")).toContainText("SH");
  await expect(page.locator("#marketScanMarketProgress")).toContainText("BJ");
  await expect(page.locator("#marketScanDiagnostic")).toContainText("等待数据源恢复后从断点重试");
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(expectedPageSize);
  await expect(page.locator("#marketScanExport")).toBeEnabled();
  await expect(page.locator("#marketScanPageText")).toHaveText(`第 1/${expectedPageCount} 页 · 共 101 条`);
  await expect(page.locator("#marketScanAnnouncement")).toHaveText(`盘后正式榜单加载完成，第 1/${expectedPageCount} 页，本页 ${expectedPageSize} 条，共 101 条。`);
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
  expect(resultQueries[0]).toMatchObject({ page: "1", page_size: String(expectedPageSize), status: "success", sort: "rank", order: "asc" });

  await expect(page.locator("#marketScanTableWrap")).toHaveAttribute("tabindex", "0");
  const responsiveTable = await page.locator("#marketScanTableWrap").evaluate((element) => {
    const header = element.querySelector("thead th:nth-child(2)");
    const stock = element.querySelector("tbody td:nth-child(2)");
    return {
      overflow: element.scrollWidth > element.clientWidth + 1,
      headerPosition: getComputedStyle(header).position,
      stockPosition: getComputedStyle(stock).position,
      detailLabels: Array.from(element.querySelectorAll(":scope > .market-scan-table > tbody > tr:first-child > td")).map((cell) => cell.dataset.label || ""),
    };
  });
  if (mobileProject) {
    expect(responsiveTable.overflow).toBe(false);
    expect(responsiveTable.detailLabels).toEqual([
      "排名", "股票", "上市板块 / 行业", "趋势强度", "研究信号", "涨跌幅", "换手率", "成交额", "质量", "状态 / 标签",
    ]);
  } else {
    expect(responsiveTable.headerPosition).toBe("sticky");
    expect(responsiveTable.stockPosition).toBe("sticky");
  }

  await page.locator("#marketScanNext").click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(Math.min(expectedPageSize, 101 - expectedPageSize));
  await expect(page.locator("#marketScanTableWrap")).toBeFocused();
  expect(resultQueries.at(-1).page).toBe("2");
  await page.locator("#marketScanPrev").click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(expectedPageSize);

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
  await page.locator("#marketScanAdvancedFilters summary").click();
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
  await page.locator("#marketScanConfidenceMin").fill("80");
  await page.locator("#marketScanRiskMax").fill("40");
  await page.locator("#marketScanTradabilityMin").fill("60");
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
    min_confidence: "80",
    max_risk: "40",
    min_tradability: "60",
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
  expect(exportQuery.toString()).toBe(resultQuery.toString().replace(new RegExp(`^page=1&page_size=${expectedPageSize}&`), ""));
  await expect(page.locator("#marketScanExport")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#marketScanAnnouncement")).toContainText("Excel 榜单已导出");

  await page.locator("#marketScanFilters button[type=reset]").click();
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(expectedPageSize);
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
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(expectedPageSize);
  await page.locator('button[data-market-scan-symbol="920066.BJ"]').click();
  await expect(page.locator("#workspace-panel-overview")).toBeVisible();
  await expect(page.locator("#workspace-panel-market-scan")).toBeHidden();
  await expect(page.locator("#stockName")).toHaveText("北交样本");
  await expect(page.locator("#stockCode")).toHaveText("BJ920066");
  await expect(page.locator("#stockWorkbench")).toBeFocused();
  await expect(page.locator("#currentAnalysisContext")).toBeVisible();
  await expect(page.locator("#currentAnalysisContext")).toContainText("不是批次 #42 的冻结快照");
  await expect(page.locator("#marketScanRows tr")).toHaveCount(0);
});

test("market-scan mode isolation and historical selection keep one explicit result batch", async ({ page }, testInfo) => {
  testInfo.setTimeout(45000);
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
  const preopenLatest = marketScanRunPayload("success", 103, {
    id: 36,
    mode: "preopen",
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
        const mode = url.searchParams.get("mode");
        return { payload: mode === "intraday" ? intradayLatest : mode === "preopen" ? preopenLatest : officialLatest };
      }
      if (url.pathname === "/api/market-scans") {
        listQueries.push(url.searchParams.toString());
        const mode = url.searchParams.get("mode");
        const items = mode === "intraday"
          ? [intradayLatest]
          : mode === "preopen" ? [preopenLatest] : [officialLatest, officialHistory];
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
        const run = runId === 40
          ? officialHistory
          : runId === 38 ? intradayLatest : runId === 36 ? preopenLatest : officialLatest;
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
      const runMatch = url.pathname.match(/^\/api\/market-scans\/(\d+)$/);
      if (runMatch) {
        const runs = { 36: preopenLatest, 38: intradayLatest, 40: officialHistory, 90: activeIntraday };
        const run = runs[Number(runMatch[1])] || officialLatest;
        return { payload: run };
      }
      return null;
    },
  });

  await page.goto("/");
  await page.locator("#marketScanModeOfficial").evaluate((input) => {
    input.checked = true;
    document.querySelector("#marketScanModeIntraday").checked = false;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanFilterToggle").click();
  await page.locator("#marketScanDetailsToggle").click();
  await page.locator("#marketScanHistoryToggle").click();
  await expect(page.locator("#marketScanHistory")).toBeVisible();
  await expect(page.locator("#marketScanBrowseContext")).toContainText("盘后正式 · 最近发布 #42");
  await expect(page.locator("#marketScanTaskContext")).toContainText("盘中临时 #90");
  await expect(page.locator("#marketScanTaskContext")).toContainText("不同");
  await expect(page.locator("#marketScanHistoryRun option")).toHaveCount(3);

  await page.locator("#marketScanHistoryRun").selectOption("40");
  await expect(page.locator("#marketScanBrowseContext")).toContainText("历史批次 #40");
  await expect(page.locator("#marketScanTableWrap")).toHaveAttribute("data-market-scan-run-id", "40");
  await expect(page.locator("#marketScanProbabilityStatus")).toHaveText("尚未生成研究证据");
  await expect(page.locator("#marketScanProbabilityMin")).toBeDisabled();
  await expect(page.locator("#marketScanProbabilityMin")).toHaveValue("");
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
      && params.get("data_date") === "2026-07-17"
      && params.get("authority") === "navigation";
  })).toBe(true);

  await page.locator("#marketScanModeIntraday").check();
  await expect(page.locator("#marketScanBrowseContext")).toContainText("盘中临时 · 最近发布 #38");
  await expect(page.locator("#marketScanTaskContext")).not.toContainText("不同");
  await expect(page.locator("#marketScanTableWrap")).toHaveAttribute("data-market-scan-run-id", "38");
  await expect(page.locator("#marketScanProbabilityMin")).toBeDisabled();
  await expect(page.locator("#marketScanProbabilityMin")).toHaveValue("");
  expect(resultRunIds).toContain(42);
  expect(resultRunIds).toContain(40);
  expect(resultRunIds).toContain(38);
  expect(latestCalls).toBeGreaterThanOrEqual(2);

  await page.locator("#marketScanModePreopen").check();
  await expect(page.locator("#marketScanBrowseContext")).toContainText("盘前复盘 · 最近发布 #36");
  await expect(page.locator("#marketScanTaskContext")).toContainText("盘中临时 #90");
  await expect(page.locator("#marketScanTaskContext")).toContainText("不同");
  await expect(page.locator("#marketScanTableWrap")).toHaveAttribute("data-market-scan-run-id", "36");
  await expect.poll(() => listQueries.some((query) => new URLSearchParams(query).get("mode") === "preopen")).toBe(true);
  expect(resultRunIds).toContain(36);
});

for (const viewport of [
  { name: "360", width: 360, height: 800 },
  { name: "390", width: 390, height: 844 },
  { name: "430", width: 430, height: 860 },
  { name: "desktop", width: 1440, height: 900 },
]) {
  test(`discovery presets, research queue, and adjacent ranks work at ${viewport.name}`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-chromium", "viewport matrix runs once in desktop Chromium");
    const expectedPageSize = viewport.width <= 820 ? 30 : 100;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const discovery = discoveryApiHarness();
    await mockApi(page, { api: discovery.api });
    page.on("dialog", (dialog) => dialog.accept());

    await page.goto("/");
    await expect(page.locator("#stockName")).toHaveText("贵州茅台");
    await selectPrimaryView(page, "market");
    await page.locator("#marketScanModeOfficial").check();
    await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(expectedPageSize);
    await page.locator("#marketScanFilterToggle").click();

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
    await expect(page.locator("#discoveryPresetFeedback")).toContainText("暂不支持保存状态");
    await expect(page.locator("#discoveryPresetFeedback")).not.toContainText("搜索关键词");
    expect(discovery.calls.create).toHaveLength(0);

    await page.locator("#marketScanStatus").selectOption("success");
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
        keyword: "600519",
      },
      sort: [{ field: "trend", order: "desc" }],
      column_view: "overview",
    });

    await page.locator("#discoveryPresetApply").click();
    await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(2);
    await expect(page.locator("#marketScanRows tr.market-scan-result-row").first()).toContainText("上升 1");
    await expect(page.locator("#marketScanRows tr.market-scan-result-row").nth(1)).toContainText("新进");
    await expect(page.locator("#discoveryRankSummary")).toContainText("批次 41 → 42");
    await expect(page.locator("#discoveryRankSummary")).toContainText("规则 leader-v2");
    expect(discovery.calls.apply.at(-1)).toEqual({ run_id: 42, page: 1, page_size: expectedPageSize });
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

    await page.locator("#discoveryPresetMore summary").click();
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
    const expectedPageSize = viewport.width <= 820 ? 30 : 100;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const discovery = discoveryApiHarness();
    await mockApi(page, { api: discovery.api });

    await page.goto("/");
    await selectPrimaryView(page, "market");
    await page.locator("#marketScanModeOfficial").check();
    await expect(page.locator("html")).toHaveAttribute("data-layout-optimizations", "ready");
    const filterPanel = page.locator("#marketScanFilterPanel");
    if (await filterPanel.isHidden()) await page.locator("#marketScanFilterToggle").click();
    await expect(filterPanel).toBeVisible();
    await page.locator("#marketScanMarket").selectOption(["SH", "BJ"]);
    await page.locator("#marketScanIndustry").fill("白酒，专用设备");
    await page.locator("#marketScanSt").selectOption("false");
    await page.locator("#marketScanNew").selectOption("false");
    await page.locator("#marketScanScoreMin").fill("80");
    await page.locator("#marketScanAdvancedFilters summary").click();
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
    await page.locator("#marketScanConfidenceMin").fill("72");
    await page.locator("#marketScanRiskMax").fill("45");
    await page.locator("#marketScanTradabilityMin").fill("66");
    await page.locator("#marketScanSort").selectOption("score");
    await page.locator("#marketScanOrder").selectOption("desc");
    await page.locator("#marketScanSort2").selectOption("trend_score");
    await page.locator("#marketScanOrder2").selectOption("desc");
    await page.locator("#marketScanSort3").selectOption("symbol");
    await page.locator("#marketScanOrder3").selectOption("asc");
    await page.locator('label:has(#marketScanColumnRisk)').click();
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
        confidence: { min: 72 },
        risk: { max: 45 },
        tradability: { min: 66 },
      },
      sort: [
        { field: "score", order: "desc" },
        { field: "trend", order: "desc" },
        { field: "symbol", order: "asc" },
      ],
      column_view: "risk",
    });

    await page.locator("#marketScanScoreMax").fill("98");
    await page.locator("#discoveryPresetSave").click();
    await expect(page.locator("#discoveryPresetFeedback")).toContainText("已更新");
    expect(discovery.calls.update).toHaveLength(1);
    expect(discovery.calls.update[0].expected_revision).toBe(1);
    expect(discovery.calls.update[0].criteria.score).toEqual({ min: 80, max: 98 });

    await page.locator("#discoveryPresetApply").click();
    await expect(page.locator("#marketScanTable")).toHaveAttribute("data-column-view", "risk");
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
    expect(discovery.calls.apply.at(-1)).toEqual({ run_id: 42, page: 1, page_size: expectedPageSize });
    expect(discovery.calls.enqueue.at(-1).symbols).toEqual(["600519.SH", "600809.SH"]);

    if (viewport.name === "desktop") {
      await page.locator("#discoveryPresetMore summary").click();
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

test("full-market scan cancellation stays unpublished and retry derives a new snapshot", async ({ page }, testInfo) => {
  const expectedPageSize = testInfo.project.use.isMobile ? 30 : 100;
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
  await page.locator("#marketScanModeOfficial").check();
  await page.locator("#marketScanStart").click();
  await expect(page.locator("#marketScanStart")).toBeDisabled();
  await page.locator("#marketScanStart").evaluate((button) => button.click());
  expect(starts).toBe(1);

  await page.locator("#marketScanCancel").click();
  await expect(page.locator("#workspace-panel-market-scan")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#marketScanCancel")).toBeDisabled();
  await page.locator("#marketScanCancel").evaluate((button) => button.click());
  await expect(page.locator("#marketScanHeadline")).toContainText("已取消");
  await expect(page.locator("#marketScanStart")).toBeFocused();
  await expect(page.locator("#marketScanResultState")).toContainText("未发布盘后正式榜单");
  await expect(page.locator("#marketScanRetry")).toBeVisible();
  expect(cancels).toBe(1);
  expect(resultCalls).toBe(0);

  await page.locator("#marketScanRetry").click();
  await expect(page.locator("#workspace-panel-market-scan")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#marketScanRetry")).toBeDisabled();
  await page.locator("#marketScanRetry").evaluate((button) => button.click());
  await expect(page.locator("#marketScanStart")).toBeDisabled();
  await expect(page.locator("#marketScanCancel")).toBeFocused();
  await expect(page.locator("#marketScanProgressText")).toHaveText("103/103 · 100.0%", { timeout: 5000 });
  await expect(page.locator("#marketScanRows tr.market-scan-result-row")).toHaveCount(expectedPageSize);
  expect(retries).toBe(1);
  expect(polls).toBe(1);
  expect(resultCalls).toBe(1);
});

test("full-market scan recovers a stale run, rejects malformed results, and resyncs online", async ({ page }) => {
  await page.addInitScript(() => {
    const NativeDate = Date;
    const fixedTime = NativeDate.parse("2026-07-31T18:00:00Z");
    globalThis.Date = class extends NativeDate {
      constructor(...args) { super(...(args.length ? args : [fixedTime])); }
      static now() { return fixedTime; }
    };
  });
  let latestCalls = 0;
  let result92Calls = 0, staleRunCalls = 0;
  const published92 = marketScanRunPayload("success", 103, { id: 92, message: "最近任务已完成" });
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/market-scans/latest") {
        latestCalls += 1;
        if (latestCalls === 1) {
          return { payload: marketScanRunPayload("running", 10, { id: 91, message: "旧任务仍在运行" }) };
        }
        if (latestCalls === 2) return { payload: published92 };
        return { payload: marketScanRunPayload("running", 30, { id: 93, message: "网络恢复后的任务" }) };
      }
      if (url.pathname === "/api/market-scans/latest-published") return { payload: latestCalls >= 2 ? published92 : null };
      if (url.pathname === "/api/market-scans/91") {
        staleRunCalls += 1;
        return { status: 404, payload: { detail: "全市场扫描批次不存在：91" } };
      }
      if (url.pathname === "/api/market-scans/92/results") {
        result92Calls += 1;
        if (result92Calls > 1) return { payload: marketScanResultPage(url.searchParams, published92) };
        return {
          payload: {
            run: published92,
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
      if (pathname === "/api/market-scans/latest") return { payload: marketScanRunPayload("degraded", 103) };
      if (pathname === "/api/market-scans/latest-published") return { payload: marketScanRunPayload("degraded", 103) };
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
    schema_version: 2,
    checksum_algorithm: "sha256",
    checksum: "a".repeat(64),
    exported_at: "2026-07-28T12:00:00Z",
    preset: {
      name: definition.name,
      criteria: definition.criteria,
      sort: definition.sort,
      column_view: definition.column_view || "overview",
    },
  };
}

function discoveryPreset(payload, revision) {
  return {
    ...payload,
    id: 7,
    schema_version: 2,
    column_view: payload.column_view || "overview",
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
        quality: 96, trend: 91, change: 2.4, turnover: 1.2, amount: 1800000000, score: 95, raw_score: 94.8,
      },
      {
        position: 2, source_rank: 4, symbol: "600809.SH", code: "600809", market: "SH",
        name: "山西汾酒", industry: "白酒", is_st: false, is_new: false,
        quality: 93, trend: 89, change: 1.8, turnover: 0.9, amount: 920000000, score: 92, raw_score: 91.8,
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

function marketScanRunPayload(status, processedCount, overrides = {}) {
  const totalCount = 103;
  const published = status === "success" || status === "degraded";
  const terminal = published || ["failed", "cancelled", "interrupted"].includes(status);
  const successCount = status === "success" ? totalCount : status === "degraded" ? 101 : processedCount;
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
      coverage_pct: Number(((succeeded / Math.max(1, total - skipped)) * 100).toFixed(2)),
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
    coverage_pct: Number(((successCount / (totalCount - (status === "degraded" ? 1 : 0))) * 100).toFixed(2)),
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
    snapshot_digest: published ? "a".repeat(64) : null, snapshot_seal_origin: published ? "publication" : null, snapshot_sealed_at: published ? "2026-07-17 16:35:00" : null,
    ...overrides,
  };
}

function marketScanResultPage(searchParams = new URLSearchParams(), runOverrides = {}) {
  const run = marketScanRunPayload("degraded", 103, runOverrides);
  let items = marketScanFixtureRows().map((item) => ({ ...item, run_id: run.id, data_date: run.data_date, quote_timestamp: `${run.quote_date} 15:00:00`, quote_observed_at: item.status === "success" ? `${run.quote_date} 15:00:01` : null }));
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
  items = filterMarketScanResearchRange(items, searchParams, "confidence", "min_confidence", null);
  items = filterMarketScanResearchRange(items, searchParams, "risk", null, "max_risk");
  items = filterMarketScanResearchRange(items, searchParams, "tradability", "min_tradability", null);
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

function marketScanFixtureRows() {
  const successes = Array.from({ length: 101 }, (_, index) => {
    if (index === 0) {
      return marketScanResult("920066.BJ", "*ST北交样本", "BJ", 1, 99, {
        industry: "专用设备",
        isSt: true,
        isNew: true,
        quality: 96,
        confidence: 96,
        risk: 28,
        tradability: 82,
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

function marketScanResult(symbol, name, market, rank, score, options = {}) {
  const status = options.status || "success";
  const success = status === "success";
  const confidence = options.confidence ?? options.quality ?? 92;
  const risk = options.risk ?? 35;
  const tradability = options.tradability ?? 70;
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
        score_dimensions: { scores: { alpha_5d: score - 10, confidence, risk, tradability } },
      },
      ranking: {
        tie_break: [["raw_score", "desc"], ["symbol", "asc"]],
        tie_break_values: { raw_score: score - 0.125, symbol },
      },
    } : {},
    reason: options.reason || (success ? `短线强势分 ${score}` : null),
    error: options.error || null,
    data_date: "2026-07-17",
    quote_timestamp: "2026-07-17 15:00:00", quote_observed_at: success ? "2026-07-17 15:00:01" : null,
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
