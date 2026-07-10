from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_symbol_inputs_and_stale_advice_request_are_scoped() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor } = await createAppHarness();
      let resolveAdviceJson;
      globalThis.fetch = async (url) => {
        if (String(url).startsWith("/api/advice/history")) {
          return {
            ok: true,
            json() {
              return new Promise((resolve) => {
                resolveAdviceJson = resolve;
              });
            },
          };
        }
        return { ok: true, async json() { return {}; } };
      };

      __appTest.setActiveSymbol("000001");
      if (__appTest.state.symbol !== "000001.SZ") {
        throw new Error(`state symbol not normalized: ${__appTest.state.symbol}`);
      }
      if (element("symbolInput").value !== "000001" || element("watchSymbolInput").value !== "000001") {
        throw new Error("active symbol inputs were not synchronized");
      }

      element("adviceTimeline").innerHTML = "current timeline";
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 11;
      const staleAdviceLoad = __appTest.loadAdviceTimeline();
      await waitFor(() => typeof resolveAdviceJson === "function", "advice json resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 12;
      resolveAdviceJson([
        {
          action: "控制风险",
          confidence: 60,
          created_at: "2026-07-01 10:00:00",
          updated_at: "2026-07-01 10:00:00",
          trend_score: 55,
          data_quality_level: "优秀",
          data_quality_score: 88,
          repeat_count: 1,
          reason: "旧请求",
        },
      ]);
      await staleAdviceLoad;
      if (element("adviceTimeline").innerHTML !== "current timeline") {
        throw new Error("stale advice timeline replaced the current symbol panel");
      }
    '''
    _run_node_script(script)


def test_quote_stream_ignores_dirty_symbols_and_stale_frames() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, streams } = await createAppHarness();

      __appTest.state.symbol = "600519.SH";
      __appTest.state.watchlist = [{ symbol: "600000.SH" }];
      __appTest.startStream();
      const stream = streams.at(-1);
      if (!stream.url.includes("600519.SH") || !stream.url.includes("600000.SH")) {
        throw new Error(`stream URL did not include current/watch symbols: ${stream.url}`);
      }
      stream.onmessage({ data: "{bad json" });
      if (!element("dataStatus").textContent.includes("实时行情数据异常")) {
        throw new Error("invalid SSE JSON did not update stream warning state");
      }
      element("quoteList").innerHTML = "existing quote rows";
      stream.onmessage({ data: '{"detail":"source down"}' });
      if (element("quoteList").innerHTML !== "existing quote rows") {
        throw new Error("non-array SSE payload cleared existing quotes");
      }
      if (!element("dataStatus").textContent.includes("实时行情数据格式异常")) {
        throw new Error("non-array SSE payload did not surface a stream format warning");
      }

      element("quoteList").innerHTML = "current quote rows";
      element("dataStatus").textContent = "current stream status";
      __appTest.startStream();
      const currentStream = streams.at(-1);
      stream.onmessage({
        data: JSON.stringify([{ name: "旧行情", market: "SH", code: "600519", amount: 1000000, price: 10, change_pct: 1 }]),
      });
      if (element("quoteList").innerHTML !== "current quote rows" || element("dataStatus").textContent !== "current stream status") {
        throw new Error("stale stream message mutated the current stream panel");
      }
      stream.listeners["quote-error"]({ data: '{"message":"旧连接失败"}' });
      if (element("dataStatus").textContent !== "current stream status") {
        throw new Error("stale stream quote-error mutated the current status");
      }
      stream.onerror();
      if (currentStream.closed || __appTest.state.stream !== currentStream) {
        throw new Error("stale stream error closed the active stream");
      }

      __appTest.state.watchlist = { stale: "bad shape" };
      __appTest.startStream();
      const malformedWatchlistStream = streams.at(-1);
      if (!malformedWatchlistStream.url.includes("600519.SH") || malformedWatchlistStream.url.includes("600000.SH")) {
        throw new Error(`malformed watchlist state leaked into stream URL: ${malformedWatchlistStream.url}`);
      }
      __appTest.state.watchlist = [{ symbol: "600000.SH&x=1" }, { symbol: "000001.SZ" }, { symbol: "bad" }];
      __appTest.startStream();
      const dirtyWatchlistStream = streams.at(-1);
      if (!dirtyWatchlistStream.url.includes("000001.SZ") || dirtyWatchlistStream.url.includes("&x=1") || dirtyWatchlistStream.url.includes("bad")) {
        throw new Error(`dirty stream symbols leaked into stream URL: ${dirtyWatchlistStream.url}`);
      }
      if (!dirtyWatchlistStream.url.includes("%2C")) {
        throw new Error(`stream symbols were not encoded as one query value: ${dirtyWatchlistStream.url}`);
      }
    '''
    _run_node_script(script)


def test_quote_stream_reconnect_timer_is_scoped_and_resets_after_success() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, streams } = await createAppHarness();
      const timers = [];
      const clearedTimers = [];
      globalThis.setTimeout = (callback, delay) => {
        const id = `timer-${timers.length + 1}`;
        timers.push({ id, callback, delay });
        return id;
      };
      globalThis.clearTimeout = (id) => {
        clearedTimers.push(id);
      };

      __appTest.state.symbol = "600519.SH";
      __appTest.startStream();
      const firstStream = streams.at(-1);
      firstStream.onerror();

      if (!firstStream.closed || __appTest.state.stream !== null) {
        throw new Error("current stream error did not close the failed stream before reconnect");
      }
      if (timers.length !== 1 || timers[0].delay !== 2000 || __appTest.state.streamRetryCount !== 1) {
        throw new Error(`first reconnect timer was wrong: ${JSON.stringify(timers)}`);
      }
      if (!element("dataStatus").textContent.includes("实时连接波动")) {
        throw new Error("current stream error did not surface reconnect status");
      }

      __appTest.startStream();
      if (!clearedTimers.includes("timer-1")) {
        throw new Error("manual stream restart did not clear pending retry timer");
      }
      const secondStream = streams.at(-1);
      secondStream.onmessage({
        data: JSON.stringify([{ name: "贵州茅台", market: "SH", code: "600519", amount: 1000000, price: 10, change_pct: 1 }]),
      });
      if (__appTest.state.streamRetryCount !== 0) {
        throw new Error("successful stream frame did not reset retry count");
      }

      document.hidden = true;
      secondStream.onerror();
      if (timers.length !== 1 || secondStream.closed || __appTest.state.stream !== secondStream) {
        throw new Error("hidden document should not schedule reconnect or close the active stream");
      }
      document.hidden = false;

      secondStream.onerror();
      if (timers.length !== 2 || timers[1].delay !== 2000) {
        throw new Error(`visible reconnect did not create expected timer: ${JSON.stringify(timers)}`);
      }
      timers[1].callback();
      const thirdStream = streams.at(-1);
      if (__appTest.state.stream !== thirdStream || thirdStream === secondStream) {
        throw new Error("reconnect timer did not start a fresh stream");
      }
    '''
    _run_node_script(script)


def test_quote_stream_constructor_failure_does_not_leave_closed_stream_active() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, streams } = await createAppHarness();

      __appTest.state.symbol = "600519.SH";
      __appTest.startStream();
      const oldStream = streams.at(-1);
      globalThis.EventSource = class {
        constructor() {
          throw new Error("stream constructor down");
        }
      };

      __appTest.startStream();

      if (!oldStream.closed || __appTest.state.stream !== null) {
        throw new Error("failed EventSource construction left the closed old stream active");
      }
      if (!element("dataStatus").textContent.includes("stream constructor down")) {
        throw new Error(`constructor failure was not surfaced: ${element("dataStatus").textContent}`);
      }
    '''
    _run_node_script(script)


def test_failed_main_load_closes_stream_and_keeps_failure_status() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, streams } = await createAppHarness();

      element("dataStatus").textContent = "current stream status";
      __appTest.state.symbol = "600519.SH";
      __appTest.startStream();
      const streamBeforeFailedLoad = streams.at(-1);
      globalThis.fetch = async (url) => {
        if (String(url).startsWith("/api/stock/workbench")) {
          return { ok: false, status: 503, async json() { return { detail: "workbench down" }; } };
        }
        throw new Error(`unexpected request after failed workbench load: ${url}`);
      };
      await __appTest.loadAll();
      if (!streamBeforeFailedLoad.closed || __appTest.state.stream !== null) {
        throw new Error("failed main load did not close the previous quote stream");
      }
      streamBeforeFailedLoad.onmessage({
        data: JSON.stringify([{ name: "旧行情", market: "SH", code: "600519", amount: 1000000, price: 10, change_pct: 1 }]),
      });
      if (!element("dataStatus").textContent.includes("600519.SH 加载失败")) {
        throw new Error(`stale stream overwrote the failed load status: ${element("dataStatus").textContent}`);
      }
    '''
    _run_node_script(script)


def test_overlapping_main_load_does_not_render_stale_workbench_or_companions() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse, waitFor } = await createAppHarness({ canvasContext: null });
      let resolveFirstWorkbench;
      let workbenchCalls = 0;
      const minuteUrls = [];
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target.startsWith("/api/stock/workbench")) {
          workbenchCalls += 1;
          if (workbenchCalls === 1) {
            return {
              ok: true,
              json() {
                return new Promise((resolve) => {
                  resolveFirstWorkbench = () => resolve(workbench("600519", "SH", "贵州茅台"));
                });
              },
            };
          }
          const current = workbench("000001", "SZ", "平安银行");
          current.local_data_warnings = [
            { component: "notes", message: "个股笔记暂不可用，当前显示空列表。" },
            { component: "alert_rules", message: "预警规则暂不可用，当前显示空列表。" },
            { component: "bad", message: { unsafe: true } },
          ];
          return jsonResponse(current);
        }
        if (target.startsWith("/api/stock/minute-analysis")) {
          minuteUrls.push(target);
          return jsonResponse({ sample_count: 0, missing_data: ["分钟K线"], t_plan: { suitability: "不适合主动做T" } });
        }
        if (target === "/api/market") return jsonResponse({ indices: [] });
        if (target === "/api/strong-stocks") return jsonResponse({ items: [] });
        if (target === "/api/watchlist") return jsonResponse([]);
        if (target.startsWith("/api/advice/history")) return jsonResponse([]);
        if (target.startsWith("/api/plates")) return jsonResponse([]);
        if (target === "/api/data/status") {
          return jsonResponse({
            providers: [],
            source_plan: {},
            cache: { quote_count: 0, kline_count: 0, stock_count: 0, plate_count: 0, quote_history_count: 0 },
            capabilities: [],
            capability_statuses: [],
          });
        }
        if (target === "/api/tasks/status") return jsonResponse({ enabled: false, running: false, tasks: [] });
        if (target.startsWith("/api/tasks/runs")) return jsonResponse([]);
        if (target.startsWith("/api/monitor/events")) return jsonResponse([]);
        throw new Error(`unexpected request during overlapping load: ${url}`);
      };

      __appTest.setActiveSymbol("600519");
      const staleLoad = __appTest.loadAll();
      await waitFor(() => typeof resolveFirstWorkbench === "function", "first workbench resolver");
      __appTest.setActiveSymbol("000001");
      await __appTest.loadAll();
      if (element("stockCode").textContent !== "SZ000001" || element("stockName").textContent !== "平安银行") {
        throw new Error(`newer workbench did not render first: ${element("stockCode").textContent} ${element("stockName").textContent}`);
      }
      if (!element("sourceLine").textContent.includes("本地数据提示：个股笔记暂不可用") || element("sourceLine").textContent.includes("unsafe")) {
        throw new Error(`local data warnings were not rendered safely: ${element("sourceLine").textContent}`);
      }

      resolveFirstWorkbench();
      await staleLoad;
      if (element("stockCode").textContent !== "SZ000001" || element("stockName").textContent !== "平安银行") {
        throw new Error(`stale workbench replaced the current symbol: ${element("stockCode").textContent} ${element("stockName").textContent}`);
      }
      if (!element("sourceLine").textContent.includes("本地数据提示：个股笔记暂不可用")) {
        throw new Error(`stale workbench replaced local data warning state: ${element("sourceLine").textContent}`);
      }
      if (minuteUrls.some((item) => item.includes("600519.SH"))) {
        throw new Error(`stale companion panel request was started: ${minuteUrls.join(",")}`);
      }

      function workbench(code, market, name) {
        return {
          analysis: {
            quote: {
              code,
              market,
              name,
              price: 10,
              change_pct: 0,
              change: 0,
              source: "测试行情",
              timestamp: "2026-07-09 10:00:00",
            },
            data_quality: {},
            signal_snapshot: { label: "待确认", summary: "测试" },
            review: {},
            klines: [],
          },
          insights: { overview: {} },
        };
      }
    '''
    _run_node_script(script)


def test_watchlist_loads_are_scoped_and_submit_blocks_duplicate_posts() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";
      import { loadWatchlist } from "./static/js/watchlist.js";

      const { element, jsonResponse, waitFor } = await createAppHarness();
      const state = { watchlist: [{ symbol: "旧状态" }] };
      let getCalls = 0;
      let resolveFirstGet;
      globalThis.fetch = async (url) => {
        if (String(url) !== "/api/watchlist") throw new Error(`unexpected URL ${url}`);
        getCalls += 1;
        if (getCalls === 1) {
          return {
            ok: true,
            json() {
              return new Promise((resolve) => {
                resolveFirstGet = () => resolve([{ symbol: "600519.SH", name: "贵州茅台", code: "600519" }]);
              });
            },
          };
        }
        return jsonResponse([{ symbol: "000001.SZ", name: "平安银行", code: "000001" }]);
      };

      const staleLoad = loadWatchlist(state);
      await waitFor(() => typeof resolveFirstGet === "function", "watchlist stale resolver");
      await loadWatchlist(state);
      resolveFirstGet();
      await staleLoad;

      if (state.watchlist[0].symbol !== "000001.SZ" || !element("watchList").innerHTML.includes("平安银行")) {
        throw new Error(`stale watchlist response replaced newer rows: ${element("watchList").innerHTML}`);
      }

      let postCalls = 0;
      let resolvePost;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/watchlist" && options.method === "POST") {
          postCalls += 1;
          return {
            ok: true,
            json() {
              return new Promise((resolve) => {
                resolvePost = () => resolve({});
              });
            },
          };
        }
        if (String(url) === "/api/watchlist") {
          return jsonResponse([{ symbol: "000001.SZ", name: "平安银行", code: "000001" }]);
        }
        throw new Error(`unexpected URL ${url}`);
      };
      element("watchSymbolInput").value = "000001";
      element("watchNoteInput").value = "观察";
      const button = element("watchForm-button");
      button.textContent = "加入";
      const submit = element("watchForm").listeners.submit;
      const firstSubmit = submit({ preventDefault() {}, currentTarget: element("watchForm") });
      await waitFor(() => typeof resolvePost === "function", "watchlist post resolver");
      const secondSubmit = submit({ preventDefault() {}, currentTarget: element("watchForm") });

      if (postCalls !== 1 || !button.disabled || button.textContent !== "加入中") {
        throw new Error("watchlist submit did not block duplicate posts while pending");
      }
      resolvePost();
      await firstSubmit;
      await secondSubmit;
      if (button.disabled || button.textContent !== "加入") {
        throw new Error("watchlist submit button did not recover");
      }
    '''
    _run_node_script(script)


def test_stale_minute_and_chart_mark_requests_do_not_replace_current_panels() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor } = await createAppHarness();

      element("minuteAnalysis").innerHTML = "current minute panel";
      let resolveMinuteJson;
      globalThis.fetch = async (url) => {
        if (String(url).startsWith("/api/stock/minute-analysis")) {
          return { ok: true, json() { return new Promise((resolve) => { resolveMinuteJson = resolve; }); } };
        }
        return { ok: true, async json() { return {}; } };
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 20;
      const staleMinuteLoad = __appTest.loadMinuteAnalysis();
      await waitFor(() => typeof resolveMinuteJson === "function", "minute json resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 21;
      element("minuteAnalysis").innerHTML = "new symbol minute panel";
      resolveMinuteJson({ sample_count: 0 });
      await staleMinuteLoad;
      if (element("minuteAnalysis").innerHTML !== "new symbol minute panel") {
        throw new Error("stale minute analysis replaced the current symbol panel");
      }

      let resolveMarksJson;
      globalThis.fetch = async (url) => {
        if (String(url).startsWith("/api/stock/chart-marks")) {
          return { ok: true, json() { return new Promise((resolve) => { resolveMarksJson = resolve; }); } };
        }
        return { ok: true, async json() { return {}; } };
      };
      const preservedMarks = [{ category: "当前", visible: true }];
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 30;
      __appTest.state.chartMarks = preservedMarks;
      const staleMarksLoad = __appTest.loadChartMarks();
      await waitFor(() => typeof resolveMarksJson === "function", "chart mark json resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 31;
      resolveMarksJson({ marks: [{ category: "旧请求" }], categories: ["旧请求"] });
      await staleMarksLoad;
      if (__appTest.state.chartMarks !== preservedMarks) {
        throw new Error("stale chart marks replaced the current symbol marks");
      }

      globalThis.fetch = async (url) => {
        if (String(url).startsWith("/api/stock/chart-marks")) {
          return { ok: false, status: 503, async json() { return { detail: "chart marks down" }; } };
        }
        return { ok: true, async json() { return {}; } };
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 32;
      __appTest.state.chartMarks = preservedMarks;
      await __appTest.loadChartMarks();
      if (__appTest.state.chartMarks !== preservedMarks) {
        throw new Error("chart mark failure cleared the current symbol marks");
      }
      if (!element("dataStatus").textContent.includes("图表标注暂不可用")) {
        throw new Error("chart mark failure did not surface a scoped warning");
      }
    '''
    _run_node_script(script)


def test_stale_plate_rank_request_does_not_replace_current_panel() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor } = await createAppHarness();

      let resolvePlatesJson;
      globalThis.fetch = async (url) => {
        if (String(url).startsWith("/api/plates")) {
          return { ok: true, json() { return new Promise((resolve) => { resolvePlatesJson = resolve; }); } };
        }
        return { ok: true, async json() { return {}; } };
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 50;
      element("plateList").innerHTML = "old plate panel";
      const stalePlateLoad = __appTest.loadPlateRank({ symbol: "600519.SH", loadSeq: 50 });
      await waitFor(() => typeof resolvePlatesJson === "function", "plate json resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 51;
      element("plateList").innerHTML = "new symbol plate panel";
      resolvePlatesJson([{ name: "旧行业", rank: 1, change_pct: 1, source: "旧源" }]);
      await stalePlateLoad;
      if (element("plateList").innerHTML !== "new symbol plate panel") {
        throw new Error(`stale plate rank replaced current panel: ${element("plateList").innerHTML}`);
      }
    '''
    _run_node_script(script)


def test_alert_button_click_ignores_duplicate_while_pending() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor } = await createAppHarness();
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 60;
      const button = {
        disabled: false,
        textContent: "暂停",
        dataset: { alertToggle: "rule-1", alertEnabled: "false" },
        classList: { contains(value) { return value === "mini-button"; } },
      };
      let patchCalls = 0;
      let resolvePatch;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/alerts/rule-1" && options.method === "PATCH") {
          patchCalls += 1;
          return {
            ok: true,
            json() {
              return new Promise((resolve) => {
                resolvePatch = () => resolve({});
              });
            },
          };
        }
        if (String(url).startsWith("/api/alerts")) return { ok: true, async json() { return []; } };
        throw new Error(`unexpected URL ${url}`);
      };

      const handler = element("alertList").listeners.click;
      const firstClick = handler({ target: { closest(selector) { return selector === "button[data-alert-toggle]" ? button : null; } } });
      await waitFor(() => typeof resolvePatch === "function", "alert patch resolver");
      const secondClick = handler({ target: { closest(selector) { return selector === "button[data-alert-toggle]" ? button : null; } } });
      if (patchCalls !== 1 || !button.disabled || button.textContent !== "处理中") {
        throw new Error(`duplicate alert button click was not blocked: calls=${patchCalls}, disabled=${button.disabled}, text=${button.textContent}`);
      }
      resolvePatch();
      await firstClick;
      await secondClick;
      if (button.disabled || button.textContent !== "暂停") {
        throw new Error("alert button did not recover after pending action");
      }
    '''
    _run_node_script(script)


def test_market_panel_strong_stock_metadata_and_fallbacks_render_safely() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse } = await createAppHarness();

      globalThis.fetch = async (url) => {
        if (String(url) === "/api/market") {
          return jsonResponse({
            indices: [{ name: "上证指数", price: 3000, change_pct: 0 }],
            index_meta: { degraded: true, warnings: ["市场指数样本行情部分缺失，成功 1/3 个样本。"] },
          });
        }
        if (String(url) === "/api/strong-stocks") {
          return jsonResponse({
            scope: "自定义列表",
            sample_count: 12,
            updated_at: "2026-07-01 10:00:00",
            items: [{ name: "测试强股", code: "600519", reason: "样本领先", tags: ["趋势"], rank: 1, leader_score: 88, change_pct: 2.5 }],
          });
        }
        return jsonResponse({});
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 50;
      await __appTest.loadMarketPanels(50, "600519.SH");
      if (!element("leaderList").innerHTML.includes("自定义列表 · 样本 12")) {
        throw new Error(`strong stock meta was not rendered: ${element("leaderList").innerHTML}`);
      }
      if (!element("marketStrip").innerHTML.includes("指数数据提示") || !element("marketStrip").innerHTML.includes("成功 1/3 个样本")) {
        throw new Error(`market degradation was not rendered: ${element("marketStrip").innerHTML}`);
      }

      globalThis.fetch = async (url) => {
        if (String(url) === "/api/market") return jsonResponse({ indices: [] });
        if (String(url) === "/api/strong-stocks") {
          return jsonResponse({
            scope: "默认观察池",
            requested_count: 2,
            sample_count: 0,
            missing_count: 2,
            degraded: true,
            warnings: ["默认观察池行情暂不可用，成功 0/2 个样本，请稍后重试。"],
            items: [],
          });
        }
        return jsonResponse({});
      };
      __appTest.state.loadSeq = 53;
      await __appTest.loadMarketPanels(53, "600519.SH");
      if (!element("leaderList").innerHTML.includes("观察池数据暂不可用") || !element("leaderList").innerHTML.includes("成功 0/2 个样本")) {
        throw new Error(`empty degraded strong-stock sample was not labelled: ${element("leaderList").innerHTML}`);
      }

      globalThis.fetch = async (url) => {
        if (String(url) === "/api/market") return jsonResponse({ indices: [{ name: "上证指数", price: 3000, change_pct: 0 }] });
        if (String(url) === "/api/strong-stocks") return jsonResponse({ scope: "脏样本", sample_count: 1, items: [null] });
        return jsonResponse({});
      };
      __appTest.state.loadSeq = 52;
      await __appTest.loadMarketPanels(52, "600519.SH");
      if (!element("marketStrip").innerHTML.includes("上证指数") || !element("leaderList").innerHTML.includes("脏样本 · 样本 1") || !element("leaderList").innerHTML.includes("--")) {
        throw new Error(`dirty strong-stock rows were not degraded safely: ${element("leaderList").innerHTML}`);
      }

      globalThis.fetch = async (url) => {
        if (String(url) === "/api/market") {
          return jsonResponse({
            indices: [],
            strong_stocks_meta: { scope: "市场概览强股样本", sample_count: 5 },
            strong_stocks: [{ name: "市场样本", code: "600000", reason: "市场概览兜底", tags: ["降级"], rank: 1, leader_score: 60, change_pct: 0.5 }],
          });
        }
        if (String(url) === "/api/strong-stocks") {
          return { ok: false, status: 503, async json() { return { detail: "强股接口暂不可用" }; } };
        }
        return jsonResponse({});
      };
      __appTest.state.loadSeq = 51;
      await __appTest.loadMarketPanels(51, "600519.SH");
      const fallbackLeaderHtml = element("leaderList").innerHTML;
      if (!fallbackLeaderHtml.includes("市场概览强股样本 · 样本 5") || !fallbackLeaderHtml.includes("强股接口暂不可用，显示市场概览样本")) {
        throw new Error(`strong stock fallback was not labelled: ${fallbackLeaderHtml}`);
      }
    '''
    _run_node_script(script)


def test_watch_alert_and_note_forms_preserve_state_and_block_duplicate_submits() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor } = await createAppHarness();

      __appTest.state.watchlist = [
        { symbol: "600519.SH", name: "贵州茅台", code: "600519", latest_price: 1200, latest_change_pct: 1.2, note: "已有关注" },
      ];
      element("watchSymbolInput").value = "bad";
      const watchButton = element("watchForm-button");
      watchButton.textContent = "加入";
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/watchlist" && options.method === "POST") {
          return { ok: false, status: 400, async json() { return { detail: "股票代码格式错误" }; } };
        }
        return { ok: true, async json() { return {}; } };
      };
      await element("watchForm").listeners.submit({ preventDefault() {}, currentTarget: element("watchForm") });
      const watchHtml = element("watchList").innerHTML;
      if (!watchHtml.includes("贵州茅台") || !watchHtml.includes("加入失败") || !watchHtml.includes("股票代码格式错误")) {
        throw new Error(`watchlist add failure replaced existing rows: ${watchHtml}`);
      }
      if (watchButton.disabled || watchButton.textContent !== "加入") {
        throw new Error("watchlist submit button did not recover after failure");
      }

      element("alertType").value = "price_below";
      element("alertThreshold").value = "1200";
      const alertButton = element("alertForm-button");
      alertButton.textContent = "添加";
      let alertPostCalls = 0;
      let resolveAlertPost;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/alerts" && options.method === "POST") {
          alertPostCalls += 1;
          return { ok: true, json() { return new Promise((resolve) => { resolveAlertPost = () => resolve({}); }); } };
        }
        if (String(url).startsWith("/api/alerts")) return { ok: true, async json() { return []; } };
        return { ok: true, async json() { return {}; } };
      };
      const alertSubmit = element("alertForm").listeners.submit;
      const firstAlertSubmit = alertSubmit({ preventDefault() {}, currentTarget: element("alertForm") });
      await waitFor(() => typeof resolveAlertPost === "function", "alert post resolver");
      const secondAlertSubmit = alertSubmit({ preventDefault() {}, currentTarget: element("alertForm") });
      if (alertPostCalls !== 1 || !alertButton.disabled || alertButton.textContent !== "添加中") {
        throw new Error("alert submit did not guard duplicate clicks");
      }
      resolveAlertPost();
      await firstAlertSubmit;
      await secondAlertSubmit;
      if (alertButton.disabled || alertButton.textContent !== "添加") {
        throw new Error("alert submit button did not recover");
      }

      element("noteContent").value = "观察";
      element("noteType").value = "复盘";
      const noteButton = element("noteForm-button");
      noteButton.textContent = "保存";
      let notePostCalls = 0;
      let resolveNotePost;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/stock/notes" && options.method === "POST") {
          notePostCalls += 1;
          return { ok: true, json() { return new Promise((resolve) => { resolveNotePost = () => resolve({}); }); } };
        }
        if (String(url).startsWith("/api/stock/notes")) return { ok: true, async json() { return []; } };
        if (String(url).startsWith("/api/stock/chart-marks")) return { ok: true, async json() { return { marks: [], categories: [] }; } };
        return { ok: true, async json() { return {}; } };
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 40;
      __appTest.state.lastAnalysis = null;
      const noteSubmit = element("noteForm").listeners.submit;
      const firstNoteSubmit = noteSubmit({ preventDefault() {}, currentTarget: element("noteForm") });
      await waitFor(() => typeof resolveNotePost === "function", "note post resolver");
      const secondNoteSubmit = noteSubmit({ preventDefault() {}, currentTarget: element("noteForm") });
      if (notePostCalls !== 1 || !noteButton.disabled || noteButton.textContent !== "保存中") {
        throw new Error("note submit did not guard duplicate clicks");
      }
      resolveNotePost();
      await firstNoteSubmit;
      await secondNoteSubmit;
      if (noteButton.disabled || noteButton.textContent !== "保存") {
        throw new Error("note submit button did not recover");
      }
    '''
    _run_node_script(script)


def test_alert_and_note_mutations_ignore_stale_symbol_after_navigation() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor } = await createAppHarness();

      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 10;
      element("alertType").value = "price_below";
      element("alertThreshold").value = "1200";
      element("alertList").innerHTML = "当前预警面板";
      element("alertForm-button").textContent = "添加";
      const fetchLog = [];
      let resolveAlertPost;
      globalThis.fetch = async (url, options = {}) => {
        fetchLog.push([String(url), options.method || "GET", options.body || ""]);
        if (String(url) === "/api/alerts" && options.method === "POST") {
          return { ok: true, json() { return new Promise((resolve) => { resolveAlertPost = () => resolve({}); }); } };
        }
        throw new Error(`stale alert mutation unexpectedly refreshed ${url}`);
      };

      const alertSubmit = element("alertForm").listeners.submit({ preventDefault() {}, currentTarget: element("alertForm") });
      await waitFor(() => typeof resolveAlertPost === "function", "alert post resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 11;
      resolveAlertPost();
      await alertSubmit;

      const alertPost = fetchLog.find(([url, method]) => url === "/api/alerts" && method === "POST");
      if (!alertPost || !String(alertPost[2]).includes('"symbol":"600519.SH"')) {
        throw new Error(`alert mutation did not capture the original symbol: ${JSON.stringify(fetchLog)}`);
      }
      if (fetchLog.some(([url, method]) => method === "GET" && String(url).startsWith("/api/alerts"))) {
        throw new Error(`stale alert mutation refreshed the new symbol: ${JSON.stringify(fetchLog)}`);
      }
      if (element("alertThreshold").value !== "1200" || element("alertList").innerHTML !== "当前预警面板") {
        throw new Error("stale alert mutation changed the current form or panel");
      }

      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 20;
      __appTest.state.lastAnalysis = { quote: { price: 1200, timestamp: "2026-07-10 10:00:00" } };
      element("noteContent").value = "切换前笔记";
      element("noteType").value = "观察";
      element("noteList").innerHTML = "当前笔记面板";
      element("noteForm-button").textContent = "保存";
      fetchLog.length = 0;
      let resolveNotePost;
      globalThis.fetch = async (url, options = {}) => {
        fetchLog.push([String(url), options.method || "GET", options.body || ""]);
        if (String(url) === "/api/stock/notes" && options.method === "POST") {
          return { ok: true, json() { return new Promise((resolve) => { resolveNotePost = () => resolve({}); }); } };
        }
        throw new Error(`stale note mutation unexpectedly refreshed ${url}`);
      };

      const noteSubmit = element("noteForm").listeners.submit({ preventDefault() {}, currentTarget: element("noteForm") });
      await waitFor(() => typeof resolveNotePost === "function", "note post resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 21;
      resolveNotePost();
      await noteSubmit;

      const notePost = fetchLog.find(([url, method]) => url === "/api/stock/notes" && method === "POST");
      if (!notePost || !String(notePost[2]).includes('"symbol":"600519.SH"')) {
        throw new Error(`note mutation did not capture the original symbol: ${JSON.stringify(fetchLog)}`);
      }
      if (fetchLog.some(([url, method]) => method === "GET" && (String(url).startsWith("/api/stock/notes") || String(url).startsWith("/api/stock/chart-marks")))) {
        throw new Error(`stale note mutation refreshed the new symbol: ${JSON.stringify(fetchLog)}`);
      }
      if (element("noteContent").value !== "切换前笔记" || element("noteList").innerHTML !== "当前笔记面板") {
        throw new Error("stale note mutation changed the current form or panel");
      }
    '''
    _run_node_script(script)


def test_watchlist_malformed_payload_stays_local_to_watchlist_panel() -> None:
    script = r'''
      import { loadWatchlist } from "./static/js/watchlist.js";

      const elements = new Map();
      function element(id) {
        if (!elements.has(id)) {
          elements.set(id, {
            id,
            innerHTML: "",
            textContent: "",
            value: "",
            querySelector() {
              return null;
            },
          });
        }
        return elements.get(id);
      }

      globalThis.document = { getElementById: element };
      globalThis.fetch = async (url) => {
        if (String(url) !== "/api/watchlist") {
          throw new Error(`unexpected URL ${url}`);
        }
        return {
          ok: true,
          async json() {
            return { rows: [] };
          },
        };
      };

      const state = { watchlist: [{ symbol: "600519.SH" }] };
      let message = "";
      try {
        await loadWatchlist(state);
      } catch (error) {
        message = error.message;
      }
      if (message !== "自选股数据格式异常") {
        throw new Error(`malformed watchlist payload did not reject with readable error: ${message}`);
      }
      if (!Array.isArray(state.watchlist) || state.watchlist.length !== 1 || state.watchlist[0].symbol !== "600519.SH") {
        throw new Error("malformed watchlist payload should preserve the last known watchlist state");
      }
      if (!element("watchList").innerHTML.includes("自选股读取失败") || !element("watchList").innerHTML.includes("自选股数据格式异常")) {
        throw new Error(`malformed watchlist payload did not render local error: ${element("watchList").innerHTML}`);
      }
    '''
    _run_node_script(script)


def test_workbench_missing_quote_source_renders_placeholder_without_load_failure() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse } = await createAppHarness({ canvasContext: null });
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target.startsWith("/api/stock/workbench")) {
          return {
            ok: true,
            async json() {
              return {
                analysis: {
                  data_quality: {},
                  signal_snapshot: { label: "待确认", summary: "测试" },
                  review: {},
                },
                insights: { overview: {} },
              };
            },
          };
        }
        if (target === "/api/market") return jsonResponse({ indices: [] });
        if (target === "/api/strong-stocks") return jsonResponse({ items: [] });
        if (target === "/api/watchlist") return jsonResponse([]);
        if (target.startsWith("/api/advice/history")) return jsonResponse([]);
        if (target.startsWith("/api/plates")) return jsonResponse([]);
        if (target.startsWith("/api/stock/minute-analysis")) return jsonResponse({ sample_count: 0, missing_data: ["分钟K线"], t_plan: { suitability: "不适合主动做T" } });
        if (target === "/api/data/status") {
          return jsonResponse({
            providers: [],
            source_plan: {},
            cache: { quote_count: 0, kline_count: 0, stock_count: 0, plate_count: 0, quote_history_count: 0 },
            capabilities: [],
            capability_statuses: [],
          });
        }
        if (target === "/api/tasks/status") return jsonResponse({ enabled: false, running: false, tasks: [] });
        if (target.startsWith("/api/tasks/runs")) return jsonResponse([]);
        if (target.startsWith("/api/monitor/events")) return jsonResponse([]);
        throw new Error(`unexpected side request after workbench success: ${url}`);
      };
      __appTest.setActiveSymbol("600706");
      await __appTest.loadAll();
      const status = element("dataStatus").textContent;
      if (status !== "实时连接正常" || status.includes("加载失败") || status.includes("页面显示异常")) {
        throw new Error(`missing quote source was treated as load/render failure: ${status}`);
      }
      const sourceLine = element("sourceLine").textContent;
      if (sourceLine !== "数据源：--，更新时间：--") {
        throw new Error(`missing quote source did not render placeholders: ${sourceLine}`);
      }
      if (document.body.classList.contains("is-stale")) {
        throw new Error("successful placeholder render left the page stale");
      }
    '''
    _run_node_script(script)


def test_workbench_load_failure_clears_previous_stock_content() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element } = await createAppHarness();

      const workbenchUrls = [];
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target.startsWith("/api/stock/workbench")) {
          workbenchUrls.push(target);
          return {
            ok: false,
            status: 503,
            async json() {
              return { detail: "所有K线数据源均不可用" };
            },
          };
        }
        throw new Error(`side request should not run after workbench failure: ${url}`);
      };

      __appTest.state.lastAnalysis = { quote: { code: "600519", market: "SH" }, klines: [] };
      element("stockName").textContent = "贵州茅台";
      element("insightOverview").innerHTML = "旧股票内容";

      __appTest.setActiveSymbol("600706");
      await __appTest.loadAll();

      if (workbenchUrls.length !== 1 || !workbenchUrls[0].includes("symbol=600706.SH")) {
        throw new Error(`workbench request did not use normalized symbol: ${workbenchUrls.join(",")}`);
      }
      if (element("dataStatus").textContent !== "600706.SH 加载失败") {
        throw new Error(`status did not show requested symbol failure: ${element("dataStatus").textContent}`);
      }
      if (element("stockCode").textContent !== "600706.SH" || element("stockName").textContent !== "加载失败") {
        throw new Error(`main card did not switch to failure shell: ${element("stockCode").textContent} ${element("stockName").textContent}`);
      }
      if (!element("summary").textContent.includes("所有K线数据源均不可用")) {
        throw new Error(`failure detail was not shown in summary: ${element("summary").textContent}`);
      }
      if (!element("insightOverview").innerHTML.includes("600706.SH 未加载成功") || element("insightOverview").innerHTML.includes("旧股票内容")) {
        throw new Error(`previous stock content leaked into failure shell: ${element("insightOverview").innerHTML}`);
      }
      if (!element("sourceLine").textContent.includes("已隔离 600519.SH")) {
        throw new Error(`previous stock isolation was not explained: ${element("sourceLine").textContent}`);
      }
      if (__appTest.state.lastAnalysis !== null || !document.body.classList.contains("is-stale")) {
        throw new Error("failed workbench did not clear analysis state and mark the page stale");
      }
    '''
    _run_node_script(script)


def test_invalid_search_invalidates_pending_valid_load() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor } = await createAppHarness({ canvasContext: null });
      let resolveWorkbench;
      globalThis.fetch = async (url) => {
        if (!String(url).startsWith("/api/stock/workbench")) {
          throw new Error(`stale valid load unexpectedly requested companion data: ${url}`);
        }
        return {
          ok: true,
          json() {
            return new Promise((resolve) => {
              resolveWorkbench = () => resolve({ analysis: {} });
            });
          },
        };
      };

      __appTest.setActiveSymbol("600519");
      const pendingLoad = __appTest.loadAll();
      await waitFor(() => typeof resolveWorkbench === "function", "workbench resolver");
      element("symbolInput").value = "bad-code";
      element("searchForm").listeners.submit({ preventDefault() {} });
      const failedSeq = __appTest.state.loadSeq;

      resolveWorkbench();
      await pendingLoad;
      if (__appTest.state.loadSeq !== failedSeq || __appTest.state.lastAnalysis !== null) {
        throw new Error("pending valid request survived invalid search submission");
      }
      if (!element("dataStatus").textContent.includes("BAD-CODE 加载失败")) {
        throw new Error(`invalid search failure was overwritten: ${element("dataStatus").textContent}`);
      }
      if (!element("summary").textContent.includes("股票代码应为6位数字")) {
        throw new Error(`invalid symbol detail was not preserved: ${element("summary").textContent}`);
      }
    '''
    _run_node_script(script)


def test_delayed_workspace_redraw_ignores_cleared_analysis() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest } = await createAppHarness({ canvasContext: null });
      let redraw;
      globalThis.requestAnimationFrame = (callback) => {
        redraw = callback;
        return 1;
      };
      __appTest.state.lastAnalysis = { klines: [], ma5: 1, ma20: 2 };
      __appTest.setWorkspaceView("research");
      __appTest.state.lastAnalysis = null;
      redraw();
    '''
    _run_node_script(script)


def test_new_quote_stream_session_resets_previous_symbol_backoff() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, streams } = await createAppHarness();
      const timers = [];
      globalThis.setTimeout = (callback, delay) => {
        timers.push({ callback, delay });
        return timers.length;
      };
      globalThis.clearTimeout = () => {};

      __appTest.state.symbol = "600519.SH";
      __appTest.state.streamRetryCount = 3;
      __appTest.setActiveSymbol("000001");
      __appTest.startStream();
      if (__appTest.state.streamRetryCount !== 0) {
        throw new Error("new symbol inherited previous stream retry count");
      }
      streams.at(-1).onerror();
      if (timers.length !== 1 || timers[0].delay !== 2000) {
        throw new Error(`new symbol first retry did not restart at 2 seconds: ${JSON.stringify(timers)}`);
      }
    '''
    _run_node_script(script)


def test_watchlist_add_ignores_stale_form_after_navigation() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor, streams } = await createAppHarness();
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 70;
      element("watchSymbolInput").value = "600519";
      element("watchNoteInput").value = "切换前关注";
      element("watchList").innerHTML = "新页面自选状态";
      element("watchForm-button").textContent = "加入";
      const fetchLog = [];
      let resolvePost;
      globalThis.fetch = async (url, options = {}) => {
        fetchLog.push([String(url), options.method || "GET", options.body || ""]);
        if (String(url) === "/api/watchlist" && options.method === "POST") {
          return { ok: true, json() { return new Promise((resolve) => { resolvePost = () => resolve({}); }); } };
        }
        throw new Error(`stale watchlist mutation unexpectedly refreshed ${url}`);
      };

      const submission = element("watchForm").listeners.submit({ preventDefault() {} });
      await waitFor(() => typeof resolvePost === "function", "watchlist post resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 71;
      element("watchSymbolInput").value = "000001";
      element("watchNoteInput").value = "新页面关注";
      resolvePost();
      await submission;

      if (!String(fetchLog[0][2]).includes('"symbol":"600519"') || fetchLog.length !== 1) {
        throw new Error(`watchlist mutation did not stay scoped: ${JSON.stringify(fetchLog)}`);
      }
      if (element("watchNoteInput").value !== "新页面关注" || element("watchList").innerHTML !== "新页面自选状态") {
        throw new Error("stale watchlist mutation changed the new page form or panel");
      }
      if (streams.length !== 0) {
        throw new Error("stale watchlist mutation restarted the quote stream");
      }
    '''
    _run_node_script(script)


def test_alert_evaluation_renders_partial_failure_count() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { element } = await createAppHarness();
      const { renderAlertEvaluation } = await import("./static/js/alerts.js");
      renderAlertEvaluation({
        checked_at: "2026-07-10 10:00:00",
        checked_count: 3,
        triggered_count: 1,
        new_event_count: 1,
        failed_count: 1,
      });
      const html = element("alertEvents").innerHTML;
      if (!html.includes("检查部分完成") || !html.includes("成功 2 / 3") || !html.includes("失败 1 条")) {
        throw new Error(`partial alert result was not explicit: ${html}`);
      }
    '''
    _run_node_script(script)


def _run_node_script(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
