import { expect, test } from "@playwright/test";
import { emitQuoteFrame, mockApi, stockSearchPayload, workbenchPayload } from "./frontend-flow-api-fixtures.mjs";

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
test("three stock loads reuse global requests and add only six stock requests", async ({ page }) => {
  const apiRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) apiRequests.push(`${url.pathname}${url.search}`);
  });
  await mockApi(page);

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect.poll(() => apiRequests.length).toBe(15);

  const input = page.locator("#symbolInput");
  await input.fill("000001");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await expect.poll(() => apiRequests.length).toBe(21);

  await input.fill("300750");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#stockName")).toHaveText("宁德时代");
  await expect.poll(() => apiRequests.length).toBe(27);

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
    "/api/stock/upside-probability?",
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

test("successful stock loads build a recent history that switches back to refreshed research", async ({ page }) => {
  const workbenchSymbols = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/stock/workbench") workbenchSymbols.push(url.searchParams.get("symbol"));
  });
  await mockApi(page);

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator("#symbolInput").fill("000001");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await page.locator("#symbolInput").fill("300750");
  await page.locator("#searchForm button").click();
  await expect(page.locator("#stockName")).toHaveText("宁德时代");

  await page.locator("#primary-nav-review").click();
  if (!(await page.locator("#stockSearchHistory").isVisible())) {
    await page.locator("#queryPanelToggle").click();
  }
  await expect(page.locator("#stockSearchHistory")).toBeVisible();
  await expect(page.locator("#stockSearchHistoryCount")).toHaveText("3");
  await expect(page.locator("#stockSearchHistoryList .stock-search-history-item").first()).toContainText("宁德时代");
  await page.locator('[data-stock-history-symbol="600519.SH"]').click();

  await expect(page.locator("#primary-nav-research")).toHaveAttribute("aria-current", "page");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect.poll(() => workbenchSymbols).toEqual(["600519", "000001.SZ", "300750.SZ", "600519.SH"]);
  await expect(page.locator("#stockSearchHistory")).toBeVisible();
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
  await expect.poll(() => apiRequests.length).toBe(15);

  const input = page.locator("#symbolInput");
  const suggestions = page.locator("#symbolSuggestions");
  await input.fill("000001");
  await page.waitForTimeout(350);
  expect(searchKeywords).toEqual([]);
  expect(apiRequests).toHaveLength(15);

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
  await expect.poll(() => apiRequests.length).toBe(22);
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
    "/api/stock/upside-probability?",
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
