import { expect } from "@playwright/test";
import { marketScanPollingIdentity as marketScanPollingIdentityPayload } from "../frontend_app_flow_helpers.mjs";
import { dailyKlines, workbenchPayload } from "./workbench-api-fixtures.mjs";

export { dailyKlines, workbenchPayload } from "./workbench-api-fixtures.mjs";
export { marketScanPollingIdentityPayload };

export function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export async function selectPrimaryView(page, view) {
  await primaryViewButton(page, view).click();
  await expectPrimaryView(page, view);
}

export async function emitQuoteFrame(page) {
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

export async function mockApi(page, options = {}) {
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

export function primaryViewButton(page, view) {
  return page.locator(`#primaryNavigation button[data-primary-view="${view}"]`);
}

export async function expectPrimaryView(page, view) {
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

function apiPayload(url) {
  const pathname = url.pathname;
  if (pathname === "/api/market-scans/polling-identity") {
    return marketScanPollingIdentityPayload(null, null, url.searchParams.get("mode") || "official");
  }
  if (pathname === "/api/market-scans/latest" || pathname === "/api/market-scans/latest-published") return null;
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
  if (pathname === "/api/paper-trading") return paperTradingDashboard();
  return [];
}

export function paperTradingDashboard(overrides = {}) {
  return {
    account: {
      id: 1,
      name: "本地模拟账户",
      initial_cash: 1000000,
      modelled_one_way_friction_pct: 0.05,
      default_cost_profile: "base",
      created_at: "2026-07-15T00:00:00.000000Z",
      updated_at: "2026-07-15T00:00:00.000000Z",
    },
    performance: {
      strategy_count: 0, pending_count: 0, open_count: 0, closed_count: 0,
      skipped_count: 0, data_unavailable_count: 0, win_count: 0, win_rate_pct: null,
      cash_balance: 1000000, market_value: 0, total_equity: 1000000,
      realized_pnl: 0, unrealized_pnl: 0, total_return_pct: 0, max_drawdown_pct: 0,
    },
    strategies: [], positions: [], trades: [], events: [], equity_curve: [], latest_run: null,
    selected_run_id: null, runs: [], cost_profiles: [],
    notes: ["不连接券商，不发送真实委托"],
    ...overrides,
  };
}

export function stockSearchPayload(keyword) {
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

export function minuteAnalysisPayload(interval, symbol = "600519.SH") {
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

export function minuteKlines(interval, count) {
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
