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
        if (String(url).startsWith("/api/advice/timeline")) {
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
      if (!element("adviceTimeline").innerHTML.includes("正在读取核心分析建议变化")) {
        throw new Error("current advice request did not claim the timeline with a loading state");
      }
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 12;
      element("adviceTimeline").innerHTML = "current timeline";
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

      let rejectStaleAdvice;
      globalThis.fetch = () => new Promise((resolve, reject) => {
        rejectStaleAdvice = reject;
      });
      element("adviceTimeline").innerHTML = "new symbol timeline";
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 13;
      const staleFailedLoad = __appTest.loadAdviceTimeline();
      await waitFor(() => typeof rejectStaleAdvice === "function", "stale advice rejection");
      if (!element("adviceTimeline").innerHTML.includes("正在读取核心分析建议变化")) {
        throw new Error("current failed advice request did not claim the timeline");
      }
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 14;
      element("adviceTimeline").innerHTML = "new symbol timeline";
      rejectStaleAdvice(new Error("旧股票请求失败"));
      await staleFailedLoad;
      if (element("adviceTimeline").innerHTML !== "new symbol timeline") {
        throw new Error("stale failed timeline replaced the current symbol panel");
      }
      if (Object.keys(__appTest.state.auxiliaryStatus.failures).length !== 0) {
        throw new Error("stale failed timeline surfaced as a current-symbol degradation");
      }
    '''
    _run_node_script(script)


def test_advice_timeline_a_b_a_switch_keeps_latest_request_ownership() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor } = await createAppHarness();
      const resolvers = [];
      globalThis.fetch = async (url) => ({
        ok: true,
        json() {
          return new Promise((resolve) => resolvers.push({ url: String(url), resolve }));
        },
      });

      const start = (symbol, loadSeq) => {
        __appTest.state.symbol = symbol;
        __appTest.state.loadSeq = loadSeq;
        return __appTest.loadAdviceTimeline({ symbol, loadSeq });
      };
      const firstA = start("600519.SH", 41);
      const loadB = start("000001.SZ", 42);
      const currentA = start("600519.SH", 43);
      await waitFor(() => resolvers.length === 3, "A-B-A advice requests");
      if (!element("adviceTimeline").innerHTML.includes("600519.SH")) {
        throw new Error("latest A request did not own the timeline loading state");
      }

      resolvers[1].resolve([timelineItem("B旧响应")]);
      resolvers[0].resolve([timelineItem("A首次旧响应")]);
      await Promise.all([firstA, loadB]);
      if (element("adviceTimeline").innerHTML.includes("B旧响应") || element("adviceTimeline").innerHTML.includes("A首次旧响应")) {
        throw new Error("stale A-B responses replaced the latest A loading state");
      }

      resolvers[2].resolve([timelineItem("A当前响应")]);
      if (!(await currentA) || !element("adviceTimeline").innerHTML.includes("A当前响应")) {
        throw new Error("latest A response did not finish the timeline");
      }

      function timelineItem(reason) {
        return {
          id: 1,
          action: "观察",
          confidence: 50,
          trend_score: 50,
          risk_level: "中等",
          created_at: "2026-07-15 10:00:00",
          market_time: "2026-07-15 10:00:00",
          comparison_status: "no_previous",
          changes: [],
          reason,
        };
      }
    '''
    _run_node_script(script)


def test_advice_timeline_failure_is_explicit_and_abort_is_silent() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element } = await createAppHarness();
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 31;
      globalThis.fetch = async () => ({
        ok: false,
        status: 503,
        async json() { return { detail: "建议比较服务停用" }; },
      });

      await __appTest.loadAdviceTimeline();
      if (!element("adviceTimeline").innerHTML.includes("核心分析建议变化暂不可用")) {
        throw new Error(`failed timeline was not explicit: ${element("adviceTimeline").innerHTML}`);
      }
      if (!element("adviceTimeline").innerHTML.includes("建议比较服务停用")) {
        throw new Error("failed timeline hid the service error");
      }
      if (!element("dataStatus").textContent.includes("建议变化时间线暂不可用")) {
        throw new Error(`timeline failure did not mark auxiliary degradation: ${element("dataStatus").textContent}`);
      }

      element("adviceTimeline").innerHTML = "current timeline";
      __appTest.state.auxiliaryStatus = { failures: {} };
      const controller = new AbortController();
      controller.abort();
      await __appTest.loadAdviceTimeline({
        symbol: "600519.SH",
        loadSeq: 31,
        signal: controller.signal,
      });
      if (element("adviceTimeline").innerHTML !== "current timeline") {
        throw new Error("aborted timeline request changed the current panel");
      }
      if (Object.keys(__appTest.state.auxiliaryStatus.failures).length !== 0) {
        throw new Error("aborted timeline request surfaced as an auxiliary failure");
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
      if (!element("dataStatus").textContent.includes("观察报价流数据异常")) {
        throw new Error("invalid SSE JSON did not update stream warning state");
      }
      element("quoteList").innerHTML = "existing quote rows";
      stream.onmessage({ data: '{"detail":"source down"}' });
      if (element("quoteList").innerHTML !== "existing quote rows") {
        throw new Error("non-array SSE payload cleared existing quotes");
      }
      if (!element("dataStatus").textContent.includes("观察报价流数据格式异常")) {
        throw new Error("non-array SSE payload did not surface a stream format warning");
      }

      element("quoteList").innerHTML = "current quote rows";
      __appTest.startStream();
      const currentStream = streams.at(-1);
      const currentStreamStatus = element("dataStatus").textContent;
      stream.onmessage({
        data: JSON.stringify([{ name: "旧行情", market: "SH", code: "600519", amount: 1000000, price: 10, change_pct: 1 }]),
      });
      if (element("quoteList").innerHTML !== "current quote rows" || element("dataStatus").textContent !== currentStreamStatus) {
        throw new Error("stale stream message mutated the current stream panel");
      }
      stream.listeners["quote-error"]({ data: '{"message":"旧连接失败"}' });
      if (element("dataStatus").textContent !== currentStreamStatus) {
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


def test_quote_stream_status_requires_current_valid_frame_and_preserves_degradation() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, streams } = await createAppHarness();
      __appTest.state.symbol = "600519.SH";
      __appTest.state.coreStatus = { phase: "ready", text: "核心数据已加载", kind: "" };
      __appTest.state.dataQualityStatus = { phase: "ready", text: "", kind: "" };

      __appTest.startStream();
      const cleanStream = streams.at(-1);
      if (element("dataStatus").textContent.includes("正常") || __appTest.state.sseStatus.hasValidFrame) {
        throw new Error(`new stream reported healthy before a frame: ${element("dataStatus").textContent}`);
      }
      cleanStream.onmessage({ data: "[]" });
      if (element("dataStatus").textContent.includes("正常") || __appTest.state.sseStatus.hasValidFrame) {
        throw new Error("empty SSE frame was treated as a valid quote frame");
      }
      cleanStream.onmessage({
        data: JSON.stringify([{ name: "贵州茅台", market: "SH", code: "600519", amount: 1, price: 10, change_pct: 1 }]),
      });
      if (element("dataStatus").textContent !== "核心分析快照已加载；观察报价流已收到有效帧" || !__appTest.state.sseStatus.hasValidFrame) {
        throw new Error(`valid current frame did not mark the stream ready: ${element("dataStatus").textContent}`);
      }
      if (element("dataStatus").textContent.includes("实时连接正常")) throw new Error("SSE exposed an unscoped global-real-time status");

      const validQuoteHtml = element("quoteList").innerHTML;
      cleanStream.onmessage({
        data: JSON.stringify([
          { name: "贵州茅台", market: "SH", code: "600519", amount: 1, price: 10, change_pct: 1 },
          { name: "脏行情", market: "SH", code: " 000001", amount: 1, price: 10, change_pct: 1 },
        ]),
      });
      if (element("quoteList").innerHTML !== validQuoteHtml || __appTest.state.sseStatus.phase !== "invalid") {
        throw new Error("mixed-validity SSE frame rendered before every row was validated");
      }
      if (!element("dataStatus").textContent.includes("帧含无效数据")) {
        throw new Error(`invalid SSE row did not degrade stream status: ${element("dataStatus").textContent}`);
      }

      const quoteList = element("quoteList");
      Object.defineProperty(quoteList, "innerHTML", {
        configurable: true,
        get() { return validQuoteHtml; },
        set() { throw new Error("quote renderer failed"); },
      });
      cleanStream.onmessage({
        data: JSON.stringify([{ name: "贵州茅台", market: "SH", code: "600519", amount: 1, price: 10, change_pct: 1 }]),
      });
      if (__appTest.state.sseStatus.phase !== "invalid" || !element("dataStatus").textContent.includes("显示异常")) {
        throw new Error(`SSE render failure did not become invalid/degraded state: ${element("dataStatus").textContent}`);
      }
      Object.defineProperty(quoteList, "innerHTML", { configurable: true, writable: true, value: validQuoteHtml });

      __appTest.state.dataQualityStatus = { phase: "degraded", text: "核心数据已加载，本地数据部分降级", kind: "warn" };
      __appTest.startStream();
      const degradedStream = streams.at(-1);
      degradedStream.onmessage({
        data: JSON.stringify([{ name: "贵州茅台", market: "SH", code: "600519", amount: 1, price: 10, change_pct: 1 }]),
      });
      if (!element("dataStatus").textContent.includes("本地数据部分降级") || element("dataStatus").textContent.includes("实时连接正常")) {
        throw new Error(`SSE success overwrote degradation: ${element("dataStatus").textContent}`);
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
      if (!element("dataStatus").textContent.includes("观察报价流连接波动")) {
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
        if (target.startsWith("/api/advice/timeline")) return jsonResponse([]);
        if (target.startsWith("/api/reviews?")) return jsonResponse([]);
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


def test_watchlist_loads_share_inflight_and_submit_blocks_duplicate_posts() -> None:
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
      const joinedLoad = loadWatchlist(state);
      if (getCalls !== 1) throw new Error(`watchlist inflight request was duplicated: ${getCalls}`);
      resolveFirstGet();
      await joinedLoad;
      await staleLoad;

      if (state.watchlist[0].symbol !== "600519.SH" || !element("watchList").innerHTML.includes("贵州茅台")) {
        throw new Error(`shared watchlist response did not render: ${element("watchList").innerHTML}`);
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


def test_global_plate_rank_request_survives_stock_switch() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor } = await createAppHarness();

      let resolvePlatesJson;
      let plateSignal;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url).startsWith("/api/plates")) {
          plateSignal = options.signal;
          return { ok: true, json() { return new Promise((resolve) => { resolvePlatesJson = resolve; }); } };
        }
        return { ok: true, async json() { return {}; } };
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 50;
      element("plateList").innerHTML = "old plate panel";
      const plateLoad = __appTest.loadPlateRank({ force: true });
      await waitFor(() => typeof resolvePlatesJson === "function", "plate json resolver");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 51;
      element("plateList").innerHTML = "new symbol plate panel";
      if (plateSignal.aborted) throw new Error("stock switch aborted the global plates request");
      resolvePlatesJson([{ name: "旧行业", rank: 1, change_pct: 1, source: "旧源" }]);
      await plateLoad;
      if (!element("plateList").innerHTML.includes("旧行业")) {
        throw new Error(`global plate rank did not complete after switching stocks: ${element("plateList").innerHTML}`);
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


def test_alert_and_note_row_actions_surface_failures_on_the_owning_row() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element } = await createAppHarness();
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 61;
      const alertFeedback = { hidden: true, textContent: "" };
      const noteFeedback = { hidden: true, textContent: "" };
      const alertRow = { querySelector(selector) { return selector === ".row-action-feedback" ? alertFeedback : null; } };
      const noteRow = { querySelector(selector) { return selector === ".row-action-feedback" ? noteFeedback : null; } };
      const alertButton = actionButton({ alertToggle: "rule-1", alertEnabled: "false" }, alertRow, "暂停");
      const noteButton = actionButton({ noteRemove: "note-1" }, noteRow, "删除");

      globalThis.fetch = async (url) => ({
        ok: false,
        status: 503,
        async json() { return { detail: String(url).includes("alerts") ? "预警服务繁忙" : "笔记服务繁忙" }; },
      });

      await element("alertList").listeners.click({
        target: { closest(selector) { return selector === "button[data-alert-toggle]" ? alertButton : null; } },
      });
      await element("noteList").listeners.click({
        target: { closest(selector) { return selector === "button[data-note-remove]" ? noteButton : null; } },
      });

      if (alertFeedback.hidden || !alertFeedback.textContent.includes("预警服务繁忙")) {
        throw new Error(`alert row failure was not local: ${alertFeedback.textContent}`);
      }
      if (noteFeedback.hidden || !noteFeedback.textContent.includes("笔记服务繁忙")) {
        throw new Error(`note row failure was not local: ${noteFeedback.textContent}`);
      }
      if (alertButton.disabled || noteButton.disabled) throw new Error("failed row action left its button disabled");

      function actionButton(dataset, row, textContent) {
        return {
          dataset,
          disabled: false,
          textContent,
          classList: { contains(value) { return value === "mini-button"; } },
          closest(selector) {
            if (selector === ".alert-row" && row === alertRow) return row;
            if (selector === ".note-row" && row === noteRow) return row;
            return null;
          },
        };
      }
    '''
    _run_node_script(script)


def test_market_panel_strong_stock_metadata_and_fallbacks_render_safely() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";
      import { clearCachedJsonRequests } from "./static/js/api.js";

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
      await __appTest.loadMarketPanels({ force: true });
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
      await __appTest.loadMarketPanels({ force: true });
      if (!element("leaderList").innerHTML.includes("观察池数据暂不可用") || !element("leaderList").innerHTML.includes("成功 0/2 个样本")) {
        throw new Error(`empty degraded strong-stock sample was not labelled: ${element("leaderList").innerHTML}`);
      }

      globalThis.fetch = async (url) => {
        if (String(url) === "/api/market") return jsonResponse({ indices: [{ name: "上证指数", price: 3000, change_pct: 0 }] });
        if (String(url) === "/api/strong-stocks") return jsonResponse({ scope: "脏样本", sample_count: 1, items: [null] });
        return jsonResponse({});
      };
      __appTest.state.loadSeq = 52;
      await __appTest.loadMarketPanels({ force: true });
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
      clearCachedJsonRequests();
      __appTest.state.loadSeq = 51;
      await __appTest.loadMarketPanels({ force: true });
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
      element("watchList").innerHTML = "existing watchlist rows";
      element("watchSymbolInput").value = "bad";
      const watchButton = element("watchForm-button");
      watchButton.textContent = "加入";
      let watchPostCalls = 0;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/watchlist" && options.method === "POST") {
          watchPostCalls += 1;
          return { ok: false, status: 400, async json() { return { detail: "股票代码格式错误" }; } };
        }
        return { ok: true, async json() { return {}; } };
      };
      await element("watchForm").listeners.submit({ preventDefault() {}, currentTarget: element("watchForm") });
      const watchHtml = element("watchList").innerHTML;
      if (watchHtml !== "existing watchlist rows" || watchPostCalls !== 0) {
        throw new Error(`invalid watchlist name reached the API or changed rows: calls=${watchPostCalls}, html=${watchHtml}`);
      }
      if (!element("watchlistFeedback").textContent.includes("股票代码应为6位数字") || element("watchSymbolInput").ariaInvalid !== "true") {
        throw new Error(`invalid watchlist name was not explained locally: ${element("watchlistFeedback").textContent}`);
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


def test_watchlist_write_error_clears_after_success_without_hiding_core_degradation() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse } = await createAppHarness();
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 61;
      __appTest.state.coreStatus = { phase: "ready", text: "核心数据已加载", kind: "" };
      __appTest.state.dataQualityStatus = { phase: "degraded", text: "核心数据质量降级", kind: "warn" };
      __appTest.state.sseStatus = { phase: "idle", text: "", kind: "", hasValidFrame: false };
      __appTest.state.watchlist = [];
      element("watchForm-button").textContent = "加入";
      element("watchSymbolInput").value = "600519";
      element("watchNoteInput").value = "第一次失败";

      let shouldFail = true;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/watchlist" && options.method === "POST") {
          if (shouldFail) {
            return { ok: false, status: 503, async json() { return { detail: "写入暂不可用" }; } };
          }
          return jsonResponse({});
        }
        if (String(url) === "/api/watchlist") {
          return jsonResponse([{ symbol: "600519.SH", code: "600519", name: "贵州茅台", latest_price: 10, latest_change_pct: 1 }]);
        }
        throw new Error(`unexpected request: ${url}`);
      };

      await element("watchForm").listeners.submit({ preventDefault() {} });
      if (__appTest.state.mutationStatus.phase !== "error") throw new Error("watchlist write error was not transiently recorded");
      if (!element("dataStatus").textContent.includes("核心数据质量降级") || !element("dataStatus").textContent.includes("自选股加入失败")) {
        throw new Error(`watchlist error overwrote core degradation: ${element("dataStatus").textContent}`);
      }

      shouldFail = false;
      element("watchNoteInput").value = "第二次成功";
      await element("watchForm").listeners.submit({ preventDefault() {} });

      if (__appTest.state.mutationStatus.phase !== "idle") throw new Error("successful watchlist write did not clear the old mutation error");
      if (__appTest.state.dataQualityStatus.phase !== "degraded") throw new Error("successful watchlist write cleared core data degradation");
      if (!element("dataStatus").textContent.includes("核心数据质量降级") || element("dataStatus").textContent.includes("自选股加入失败")) {
        throw new Error(`successful mutation produced the wrong composite status: ${element("dataStatus").textContent}`);
      }
    '''
    _run_node_script(script)


def test_watchlist_delete_readback_failure_is_degraded_not_delete_failure() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse } = await createAppHarness();
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 62;
      __appTest.state.coreStatus = { phase: "ready", text: "核心数据已加载", kind: "" };
      __appTest.state.dataQualityStatus = { phase: "ready", text: "", kind: "" };
      __appTest.state.watchlist = [
        { symbol: "600000.SH", code: "600000", name: "浦发银行", latest_price: 10, latest_change_pct: 1 },
      ];
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/watchlist/600000.SH" && options.method === "DELETE") return jsonResponse(null);
        if (String(url) === "/api/watchlist") {
          return { ok: false, status: 503, async json() { return { detail: "列表回读失败" }; } };
        }
        throw new Error(`unexpected request: ${url}`);
      };
      const button = {
        dataset: { action: "remove", symbol: "600000.SH" },
        disabled: false,
        textContent: "×",
        classList: { contains() { return false; } },
      };

      await element("watchList").listeners.click({ target: { closest() { return button; } } });

      const html = element("watchList").innerHTML;
      if (__appTest.state.watchlist.length !== 0 || !html.includes("暂无自选")) throw new Error(`DELETE was not locally confirmed: ${html}`);
      if (!html.includes("已删除，列表同步降级") || html.includes("删除失败")) throw new Error(`GET failure was misreported: ${html}`);
      if (__appTest.state.mutationStatus.phase !== "degraded" || !element("dataStatus").textContent.includes("自选股已删除")) {
        throw new Error(`readback degradation was not retained: ${element("dataStatus").textContent}`);
      }
      if (button.disabled) throw new Error("delete button stayed disabled after degraded readback");
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


def test_pending_stock_switch_mutations_use_displayed_workbench_context() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse, waitFor } = await createAppHarness({ canvasContext: null });
      const displayedAnalysis = {
        quote: {
          code: "600519",
          market: "SH",
          name: "贵州茅台",
          price: 1234.5,
          timestamp: "2026-07-15 10:00:00",
        },
        klines: [],
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.lastAnalysis = displayedAnalysis;
      element("alertType").value = "price_below";
      element("alertThreshold").value = "1200";
      element("noteContent").value = "旧面板笔记";
      element("noteType").value = "观察";

      const fetchLog = [];
      let workbenchSignal;
      globalThis.fetch = async (url, options = {}) => {
        const target = String(url);
        const method = options.method || "GET";
        fetchLog.push([target, method, options.body || ""]);
        if (target.startsWith("/api/stock/workbench")) {
          workbenchSignal = options.signal;
          return new Promise(() => {});
        }
        if (target === "/api/alerts" && method === "POST") return jsonResponse({});
        if (target.startsWith("/api/alerts?symbol=") || target.startsWith("/api/alerts/events?symbol=")) {
          return jsonResponse([]);
        }
        if (target === "/api/stock/notes" && method === "POST") return jsonResponse({});
        if (target.startsWith("/api/stock/notes?symbol=")) return jsonResponse([]);
        if (target.startsWith("/api/stock/chart-marks?symbol=")) return jsonResponse({ marks: [], categories: [] });
        throw new Error(`unexpected request during pending switch: ${method} ${target}`);
      };

      __appTest.setActiveSymbol("000001");
      const pendingLoad = __appTest.loadAll();
      await waitFor(() => workbenchSignal, "pending workbench signal");
      await element("alertForm").listeners.submit({ preventDefault() {}, currentTarget: element("alertForm") });
      await element("noteForm").listeners.submit({ preventDefault() {}, currentTarget: element("noteForm") });

      const alertPost = fetchLog.find(([url, method]) => url === "/api/alerts" && method === "POST");
      const notePost = fetchLog.find(([url, method]) => url === "/api/stock/notes" && method === "POST");
      const alertBody = alertPost && JSON.parse(alertPost[2]);
      const noteBody = notePost && JSON.parse(notePost[2]);
      if (!alertBody || alertBody.symbol !== "600519.SH") {
        throw new Error(`pending alert write used the requested symbol: ${JSON.stringify(fetchLog)}`);
      }
      if (
        !noteBody ||
        noteBody.symbol !== "600519.SH" ||
        noteBody.price !== 1234.5 ||
        noteBody.trade_date !== "2026-07-15 10:00:00"
      ) {
        throw new Error(`pending note write mixed requested symbol and displayed metadata: ${JSON.stringify(noteBody)}`);
      }
      const contextualReads = fetchLog.filter(
        ([url, method]) =>
          method === "GET" &&
          ["/api/alerts?", "/api/alerts/events?", "/api/stock/notes?", "/api/stock/chart-marks?"].some((prefix) =>
            url.startsWith(prefix)
          )
      );
      if (!contextualReads.length || contextualReads.some(([url]) => url.includes("000001.SZ") || !url.includes("600519.SH"))) {
        throw new Error(`mutation readback escaped the displayed context: ${JSON.stringify(contextualReads)}`);
      }

      __appTest.state.loadRequest.abort();
      await pendingLoad;
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
        if (target.startsWith("/api/advice/timeline")) return jsonResponse([]);
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
      if (!status.includes("观察报价流") || status.includes("正常") || status.includes("加载失败") || status.includes("页面显示异常")) {
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


def test_new_stock_load_aborts_old_main_request_without_rendering_abort_error() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, waitFor } = await createAppHarness();
      let firstSignal;
      let workbenchCalls = 0;
      globalThis.fetch = (url, options = {}) => {
        if (!String(url).startsWith("/api/stock/workbench")) {
          throw new Error(`companion request should not start after failed latest load: ${url}`);
        }
        workbenchCalls += 1;
        if (workbenchCalls === 1) {
          firstSignal = options.signal;
          return new Promise(() => {});
        }
        return Promise.resolve({
          ok: false,
          status: 503,
          async json() {
            return { detail: "latest workbench down" };
          },
        });
      };

      __appTest.setActiveSymbol("600519");
      const staleLoad = __appTest.loadAll();
      await waitFor(() => firstSignal, "first workbench signal");
      __appTest.setActiveSymbol("000001");
      await __appTest.loadAll();
      await staleLoad;

      if (!firstSignal.aborted) throw new Error("new stock load did not abort the previous workbench request");
      if (!element("dataStatus").textContent.includes("000001.SZ 加载失败")) {
        throw new Error(`latest load failure was not preserved: ${element("dataStatus").textContent}`);
      }
      if (element("dataStatus").textContent.includes("请求已取消")) {
        throw new Error("AbortError leaked into the visible status");
      }
    '''
    _run_node_script(script)


def test_committed_watchlist_delete_survives_stock_switch_and_refreshes_global_list() -> None:
    script = r'''
      import { createRequestScope } from "./static/js/api.js";
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse, streams, waitFor } = await createAppHarness();
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 80;
      __appTest.state.loadRequest = createRequestScope();
      __appTest.state.watchlist = [{ symbol: "600000.SH", code: "600000", name: "浦发银行" }];
      element("watchList").innerHTML = "current watchlist";
      let deleteSignal;
      let resolveDelete;
      let getCalls = 0;
      globalThis.fetch = (url, options = {}) => {
        if (String(url).startsWith("/api/watchlist/") && options.method === "DELETE") {
          deleteSignal = options.signal;
          return new Promise((resolve) => {
            resolveDelete = () => resolve(jsonResponse(null));
          });
        }
        if (String(url) === "/api/watchlist") {
          getCalls += 1;
          return jsonResponse([]);
        }
        throw new Error(`stale delete unexpectedly requested ${url}`);
      };
      const button = {
        dataset: { action: "remove", symbol: "600000.SH" },
        disabled: false,
        textContent: "×",
        classList: { contains() { return false; } },
      };

      const deletion = element("watchList").listeners.click({ target: { closest() { return button; } } });
      await waitFor(() => deleteSignal, "delete signal");
      __appTest.state.symbol = "000001.SZ";
      __appTest.state.loadSeq = 81;
      __appTest.state.loadRequest.abort();
      if (deleteSignal.aborted) throw new Error("stock navigation aborted a committed watchlist delete");
      resolveDelete();
      await deletion;

      if (deleteSignal.aborted) throw new Error("committed watchlist delete was aborted after resolution");
      if (getCalls !== 1) throw new Error(`committed delete did not perform one global readback: ${getCalls}`);
      if (__appTest.state.watchlist.length !== 0 || !element("watchList").innerHTML.includes("暂无自选")) {
        throw new Error(`global delete result was not rendered: ${element("watchList").innerHTML}`);
      }
      if (streams.length !== 0) throw new Error("stale delete restarted SSE for the old stock context");
      if (button.disabled) throw new Error("delete button stayed disabled after controlled completion");
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


def test_invalid_search_stays_in_query_panel_and_preserves_successful_result() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element } = await createAppHarness({ canvasContext: null });
      const successfulAnalysis = {
        quote: { code: "600519", market: "SH", name: "贵州茅台" },
        klines: [],
      };
      __appTest.state.symbol = "600519.SH";
      __appTest.state.lastAnalysis = successfulAnalysis;
      __appTest.state.coreStatus = { phase: "ready", text: "核心数据已加载", kind: "" };
      __appTest.state.dataQualityStatus = { phase: "ready", text: "", kind: "" };
      element("stockName").textContent = "贵州茅台";
      element("summary").textContent = "上次成功分析";
      element("symbolError").hidden = true;
      element("symbolInput").ariaInvalid = "false";

      let focusOptions;
      let scrollCount = 0;
      globalThis.matchMedia = () => ({ matches: true });
      element("symbolInput").focus = (options) => { focusOptions = options; };
      element("stockCode").scrollIntoView = () => { scrollCount += 1; };
      element("symbolInput").value = "bad-code";
      element("searchForm").listeners.submit({ preventDefault() {} });

      if (element("symbolError").hidden || !element("symbolError").textContent.includes("股票代码应为6位数字")) {
        throw new Error(`local validation was not shown in the query panel: ${element("symbolError").textContent}`);
      }
      if (element("symbolInput").ariaInvalid !== "true" || !focusOptions || focusOptions.preventScroll !== true) {
        throw new Error("invalid input did not expose aria-invalid and receive non-scrolling focus");
      }
      if (__appTest.state.lastAnalysis !== successfulAnalysis || __appTest.state.symbol !== "600519.SH") {
        throw new Error("local validation changed the last successful analysis state");
      }
      if (element("stockName").textContent !== "贵州茅台" || element("summary").textContent !== "上次成功分析") {
        throw new Error("local validation replaced the last successful workbench DOM");
      }
      if (scrollCount !== 0) throw new Error("local validation scrolled to the main card");

      element("symbolInput").value = "000001";
      element("symbolInput").listeners.input({ currentTarget: element("symbolInput") });
      if (!element("symbolError").hidden || element("symbolError").textContent || element("symbolInput").ariaInvalid !== "false") {
        throw new Error("valid input did not clear the local validation state");
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
      if (element("symbolError").hidden || !element("symbolError").textContent.includes("股票代码应为6位数字")) {
        throw new Error(`invalid search was not retained in the query panel: ${element("symbolError").textContent}`);
      }
      if (element("dataStatus").textContent.includes("BAD-CODE") || element("summary").textContent.includes("股票代码应为6位数字")) {
        throw new Error("local validation leaked into the global status or workbench summary");
      }
      const panelHtml = element("insightOverview").innerHTML;
      if (panelHtml.includes('class="minute-state loading"') || !panelHtml.includes("本次加载已取消")) {
        throw new Error(`cancelled first load remained in a loading placeholder: ${panelHtml}`);
      }
      if (
        __appTest.state.coreStatus.phase !== "idle" ||
        element("stockName").textContent !== "未加载" ||
        !element("summary").textContent.includes("加载已取消")
      ) {
        throw new Error(`cancelled first load did not render an explicit idle state: ${element("summary").textContent}`);
      }
      if (element("symbolError").hidden || !element("symbolError").textContent.includes("股票代码应为6位数字")) {
        throw new Error("idle workbench render cleared the query validation error");
      }
    '''
    _run_node_script(script)


def test_auxiliary_failures_are_composed_and_clear_per_source() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse } = await createAppHarness();
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 91;
      __appTest.state.coreStatus = { phase: "ready", text: "核心数据已加载", kind: "" };
      __appTest.state.dataQualityStatus = { phase: "ready", text: "", kind: "" };
      __appTest.state.sseStatus = { phase: "ready", text: "核心分析快照已加载；观察报价流已收到有效帧", kind: "ok", hasValidFrame: true };

      let degraded = true;
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target === "/api/market" || target === "/api/strong-stocks") {
          if (degraded) {
            return { ok: false, status: 503, async json() { return { detail: `${target} down` }; } };
          }
          return jsonResponse(target === "/api/market" ? { indices: [] } : { items: [] });
        }
        if (target === "/api/data/status") {
          if (degraded) {
            return { ok: false, status: 503, async json() { return { detail: "status down" }; } };
          }
          return jsonResponse({ providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] });
        }
        throw new Error(`unexpected request: ${url}`);
      };

      await __appTest.loadMarketPanels(91, "600519.SH");
      await __appTest.refreshDataStatus();
      const degradedStatus = element("dataStatus").textContent;
      if (!degradedStatus.includes("市场概览暂不可用") || !degradedStatus.includes("强股排行暂不可用") || !degradedStatus.includes("数据源状态暂不可用")) {
        throw new Error(`auxiliary failures were not composed: ${degradedStatus}`);
      }
      if (degradedStatus.includes("实时连接正常") || !element("dataStatus").className.includes("warn")) {
        throw new Error(`SSE success hid auxiliary degradation: ${degradedStatus}`);
      }

      degraded = false;
      await __appTest.loadMarketPanels(91, "600519.SH");
      if (!element("dataStatus").textContent.includes("数据源状态暂不可用")) {
        throw new Error("market recovery cleared an unrelated data-status failure");
      }
      await __appTest.refreshDataStatus();
      if (element("dataStatus").textContent !== "核心分析快照已加载；观察报价流已收到有效帧") {
        throw new Error(`recovered auxiliary sources did not clear their degradation: ${element("dataStatus").textContent}`);
      }
    '''
    _run_node_script(script)


def test_visibility_restore_retries_cancelled_data_status_request() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, jsonResponse, waitFor } = await createAppHarness();
      let dataStatusCalls = 0;
      let firstSignal;
      globalThis.fetch = async (url, options = {}) => {
        const target = String(url);
        if (target === "/api/data/status") {
          dataStatusCalls += 1;
          if (dataStatusCalls === 1) {
            firstSignal = options.signal;
            return new Promise((resolve, reject) => {
              options.signal.addEventListener("abort", () => {
                const error = new Error("cancelled while hidden");
                error.name = "AbortError";
                reject(error);
              }, { once: true });
            });
          }
          return jsonResponse({ providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] });
        }
        if (target === "/api/tasks/status") return jsonResponse({ enabled: false, running: false, tasks: [] });
        if (target === "/api/tasks/runs?limit=8" || target === "/api/monitor/events?limit=8") return jsonResponse([]);
        throw new Error(`unexpected request: ${url}`);
      };

      const pending = __appTest.refreshDataStatus();
      await waitFor(() => firstSignal, "first data-status signal");
      document.hidden = true;
      __appTest.handleVisibilityChange();
      await pending;
      if (!firstSignal.aborted || !__appTest.state.visibilityRefreshSources.has("data-status")) {
        throw new Error("hidden page did not retain the cancelled data-status refresh");
      }

      document.hidden = false;
      __appTest.handleVisibilityChange();
      await waitFor(
        () => dataStatusCalls === 2 && !__appTest.state.visibilityRefreshSources.has("data-status"),
        "visible data-status recovery",
      );
      if (Object.prototype.hasOwnProperty.call(__appTest.state.auxiliaryStatus.failures, "data-status")) {
        throw new Error("successful visible refresh retained the data-status degradation");
      }
    '''
    _run_node_script(script)


def test_hidden_delayed_load_tail_defers_quote_stream_until_visible() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, jsonResponse, streams, waitFor } = await createAppHarness({ canvasContext: null });
      let resolveWatchlist;
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target.startsWith("/api/stock/workbench")) return jsonResponse(workbench());
        if (target === "/api/watchlist") {
          return {
            ok: true,
            json() {
              return new Promise((resolve) => {
                resolveWatchlist = () => resolve([]);
              });
            },
          };
        }
        if (target === "/api/market") return jsonResponse({ indices: [] });
        if (target === "/api/strong-stocks") return jsonResponse({ items: [] });
        if (target.startsWith("/api/advice/timeline") || target.startsWith("/api/plates")) return jsonResponse([]);
        if (target.startsWith("/api/stock/minute-analysis")) {
          return jsonResponse({ sample_count: 0, missing_data: ["分钟K线"], t_plan: { suitability: "不适合主动做T" } });
        }
        if (target === "/api/data/status") {
          return jsonResponse({ providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] });
        }
        if (target === "/api/tasks/status") return jsonResponse({ enabled: false, running: false, tasks: [] });
        if (target.startsWith("/api/tasks/runs") || target.startsWith("/api/monitor/events")) return jsonResponse([]);
        throw new Error(`unexpected request during delayed hidden load: ${target}`);
      };

      __appTest.setActiveSymbol("600519");
      const pendingLoad = __appTest.loadAll();
      await waitFor(() => typeof resolveWatchlist === "function", "watchlist tail resolver");
      document.hidden = true;
      __appTest.handleVisibilityChange();
      resolveWatchlist();
      await pendingLoad;

      if (streams.length !== 0 || __appTest.state.stream !== null) {
        throw new Error("a delayed load tail created EventSource while the page was hidden");
      }

      document.hidden = false;
      __appTest.handleVisibilityChange();
      if (streams.length !== 1 || __appTest.state.stream !== streams[0]) {
        throw new Error("visibility restore did not start the deferred quote stream");
      }

      function workbench() {
        return {
          analysis: {
            quote: {
              code: "600519",
              market: "SH",
              name: "贵州茅台",
              price: 10,
              change_pct: 0,
              change: 0,
              source: "测试行情",
              timestamp: "2026-07-15 10:00:00",
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


def test_workspace_tabs_and_mark_filters_sync_accessibility_state() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse } = await createAppHarness();
      const views = ["overview", "qa", "strategy", "finance", "theme", "replay", "tools"];
      let focused;
      const tabs = views.map((view) => {
        const tab = element(`tab-${view}`);
        tab.dataset.view = view;
        tab.attributes = {};
        tab.setAttribute = (name, value) => { tab.attributes[name] = value; };
        tab.closest = () => tab;
        tab.focus = () => { focused = tab; };
        return tab;
      });
      const panels = views.map((view) => {
        const panel = element(`panel-${view}`);
        panel.dataset.viewPanel = view;
        panel.hidden = false;
        return panel;
      });
      document.querySelectorAll = (selector) => {
        if (selector === ".workspace-tabs button[data-view]") return tabs;
        if (selector === ".workspace-view[data-view-panel]") return panels;
        return [];
      };

      __appTest.setWorkspaceView("qa");
      if (tabs[1].attributes["aria-selected"] !== "true" || tabs[1].tabIndex !== 0 || panels[1].hidden) {
        throw new Error("active tab and tabpanel accessibility state did not synchronize");
      }
      if (tabs[0].attributes["aria-selected"] !== "false" || tabs[0].tabIndex !== -1 || !panels[0].hidden) {
        throw new Error("inactive tab and tabpanel accessibility state did not synchronize");
      }

      let prevented = 0;
      const keydown = element("workspaceTabs").listeners.keydown;
      keydown({ target: tabs[1], key: "ArrowRight", preventDefault() { prevented += 1; } });
      if (focused !== tabs[2] || tabs[2].attributes["aria-selected"] !== "true") throw new Error("ArrowRight did not select the next tab");
      keydown({ target: tabs[2], key: "ArrowLeft", preventDefault() { prevented += 1; } });
      if (focused !== tabs[1] || tabs[1].attributes["aria-selected"] !== "true") throw new Error("ArrowLeft did not select the previous tab");
      keydown({ target: tabs[1], key: "End", preventDefault() { prevented += 1; } });
      if (focused !== tabs.at(-1) || tabs.at(-1).attributes["aria-selected"] !== "true") throw new Error("End did not select the last tab");
      keydown({ target: tabs.at(-1), key: "Home", preventDefault() { prevented += 1; } });
      if (focused !== tabs[0] || tabs[0].attributes["aria-selected"] !== "true" || prevented !== 4) throw new Error("Home did not select the first tab");

      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 92;
      globalThis.fetch = async () => jsonResponse({
        marks: [{ category: "买点" }, { category: "卖点" }],
        categories: ["买点", "卖点"],
      });
      await __appTest.loadChartMarks();
      if (!element("markFilters").innerHTML.includes('data-mark-category="买点" aria-pressed="true"')) {
        throw new Error(`active mark filter did not expose aria-pressed: ${element("markFilters").innerHTML}`);
      }
      const markButton = { dataset: { markCategory: "买点" }, closest() { return this; } };
      element("markFilters").listeners.click({ target: markButton });
      if (!element("markFilters").innerHTML.includes('data-mark-category="买点" aria-pressed="false"')) {
        throw new Error(`toggled mark filter did not update aria-pressed: ${element("markFilters").innerHTML}`);
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


def test_watchlist_add_preserves_stale_form_but_refreshes_global_list() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse, waitFor, streams } = await createAppHarness();
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
        if (String(url) === "/api/watchlist") {
          return jsonResponse([{ symbol: "600519.SH", code: "600519", name: "贵州茅台" }]);
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

      if (!String(fetchLog[0][2]).includes('"symbol":"600519"') || fetchLog.length !== 2 || fetchLog[1][0] !== "/api/watchlist") {
        throw new Error(`watchlist mutation did not stay scoped: ${JSON.stringify(fetchLog)}`);
      }
      if (element("watchNoteInput").value !== "新页面关注" || !element("watchList").innerHTML.includes("贵州茅台")) {
        throw new Error("stale watchlist mutation changed the new form or skipped the global readback");
      }
      if (streams.length !== 0) {
        throw new Error("stale watchlist mutation restarted the quote stream");
      }
    '''
    _run_node_script(script)


def test_alert_evaluation_flow_keeps_partial_summary_separate_from_event_empty_state() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse } = await createAppHarness();
      const { evaluateAlerts } = await import("./static/js/alerts.js");
      __appTest.state.symbol = "600519.SH";
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target.startsWith("/api/alerts/evaluate")) {
          return jsonResponse({
            checked_at: "2026-07-10 10:00:00",
            checked_count: 3,
            triggered_count: 1,
            new_event_count: 1,
            failed_count: 1,
          });
        }
        if (target.startsWith("/api/alerts/events")) return jsonResponse([]);
        if (target.startsWith("/api/alerts")) return jsonResponse([]);
        throw new Error(`unexpected request: ${target}`);
      };

      const completed = await evaluateAlerts(__appTest.state, {
        symbol: "600519.SH",
        isCurrent: () => __appTest.state.symbol === "600519.SH",
      });
      const html = element("alertEvaluation").innerHTML;
      if (!completed) throw new Error("alert evaluation flow did not complete");
      if (!html.includes("检查部分完成") || !html.includes("成功 2 / 3") || !html.includes("失败 1 条")) {
        throw new Error(`partial alert result was not explicit: ${html}`);
      }
      if (!element("alertEvents").innerHTML.includes("暂无触发记录") || element("alertEvents").innerHTML.includes("检查部分完成")) {
        throw new Error(`event empty state was replaced by the evaluation summary: ${element("alertEvents").innerHTML}`);
      }
    '''
    _run_node_script(script)


def test_three_stock_loads_keep_global_requests_cached_across_stock_switches() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, jsonResponse, streams } = await createAppHarness({ canvasContext: null });
      const fetchCalls = [];
      globalThis.fetch = async (url) => {
        const target = String(url);
        fetchCalls.push(target);
        if (target.startsWith("/api/stock/workbench")) {
          return jsonResponse(workbench(new URL(target, "http://local").searchParams.get("symbol")));
        }
        if (target.startsWith("/api/stock/minute-analysis")) {
          return jsonResponse({ sample_count: 0, missing_data: ["分钟K线"], t_plan: { suitability: "不适合主动做T" } });
        }
        if (target.startsWith("/api/advice/timeline")) return jsonResponse([]);
        if (target === "/api/market") return jsonResponse({ indices: [] });
        if (target === "/api/strong-stocks") return jsonResponse({ items: [] });
        if (target === "/api/data/status") {
          return jsonResponse({ providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] });
        }
        if (target === "/api/system/diagnostics") {
          return jsonResponse({ storage: {}, warnings: [], suggestions: [] });
        }
        if (target === "/api/tasks/status") return jsonResponse({ enabled: false, running: false, tasks: [] });
        if (target === "/api/tasks/runs?limit=8" || target === "/api/monitor/events?limit=8") return jsonResponse([]);
        if (target === "/api/watchlist" || target === "/api/plates?limit=8") return jsonResponse([]);
        throw new Error(`unexpected request ${target}`);
      };

      const snapshots = [];
      for (const symbol of ["600519", "000001", "300750"]) {
        __appTest.setActiveSymbol(symbol);
        await __appTest.loadAll();
        snapshots.push({ http: fetchCalls.length, sse: streams.length });
      }

      if (!fetchCalls[0].startsWith("/api/stock/workbench")) {
        throw new Error(`cold load did not prioritize the core workbench request: ${JSON.stringify(fetchCalls.slice(0, 4))}`);
      }

      const deltas = snapshots.map((item, index) => ({
        http: item.http - (snapshots[index - 1]?.http || 0),
        sse: item.sse - (snapshots[index - 1]?.sse || 0),
      }));
      if (JSON.stringify(deltas) !== JSON.stringify([{ http: 13, sse: 1 }, { http: 4, sse: 1 }, { http: 4, sse: 1 }])) {
        throw new Error(`request deltas exceeded the expected global/stock budget: ${JSON.stringify(deltas)}`);
      }

      for (const endpoint of __appTest.GLOBAL_ENDPOINTS) {
        const count = fetchCalls.filter((url) => url === endpoint).length;
        if (count !== 1) throw new Error(`global endpoint ${endpoint} fetched ${count} times`);
      }
      const stockKinds = {
        advice: (url) => url.startsWith("/api/advice/timeline"),
        reviews: (url) => url.startsWith("/api/reviews?"),
        minute: (url) => url.startsWith("/api/stock/minute-analysis"),
        workbench: (url) => url.startsWith("/api/stock/workbench"),
      };
      const globalEndpoints = new Set(__appTest.GLOBAL_ENDPOINTS);
      for (const [kind, matches] of Object.entries(stockKinds)) {
        const count = fetchCalls.filter(matches).length;
        if (count !== 3) throw new Error(`${kind} fetched ${count} times`);
      }
      const unexpected = fetchCalls.filter(
        (url) => !globalEndpoints.has(url) && !Object.values(stockKinds).some((matches) => matches(url))
      );
      if (unexpected.length || streams.length !== 3 || streams.some((stream) => !stream.url.startsWith("/api/stream/quotes?symbols="))) {
        throw new Error(`stock switch emitted unexpected request classes: ${JSON.stringify({ unexpected, streams: streams.map((item) => item.url) })}`);
      }

      function workbench(symbol) {
        const [code, market] = String(symbol).split(".");
        return {
          analysis: {
            quote: { code, market, name: symbol, price: 10, change: 0, change_pct: 0, source: "测试", timestamp: "2026-07-15" },
            data_quality: {},
            signal_snapshot: { label: "观察", summary: "测试" },
            review: {},
            klines: [],
          },
          insights: { overview: {} },
          chart_marks: { marks: [], categories: [] },
        };
      }
    '''
    _run_node_script(script)


def test_stock_switch_does_not_abort_or_duplicate_inflight_global_requests() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, jsonResponse, waitFor } = await createAppHarness({ canvasContext: null });
      const globalEndpoints = new Set(__appTest.GLOBAL_ENDPOINTS);
      const globalCalls = new Map();
      const globalResolvers = new Map();
      const globalSignals = new Map();
      globalThis.fetch = (url, options = {}) => {
        const target = String(url);
        if (globalEndpoints.has(target)) {
          globalCalls.set(target, Number(globalCalls.get(target) || 0) + 1);
          globalSignals.set(target, options.signal);
          return new Promise((resolve) => {
            globalResolvers.set(target, () => resolve(jsonResponse(globalPayload(target))));
          });
        }
        if (target.startsWith("/api/stock/workbench")) {
          const symbol = new URL(target, "http://local").searchParams.get("symbol");
          return Promise.resolve(jsonResponse(workbench(symbol)));
        }
        if (target.startsWith("/api/stock/minute-analysis")) {
          return Promise.resolve(jsonResponse({ sample_count: 0, missing_data: [], t_plan: {} }));
        }
        if (target.startsWith("/api/advice/timeline")) return Promise.resolve(jsonResponse([]));
        throw new Error(`unexpected request ${target}`);
      };

      __appTest.setActiveSymbol("600519");
      const firstLoad = __appTest.loadAll();
      await waitFor(() => globalResolvers.size === __appTest.GLOBAL_ENDPOINTS.length, "cold global requests");
      __appTest.setActiveSymbol("000001");
      const secondLoad = __appTest.loadAll();
      await Promise.resolve();

      for (const endpoint of __appTest.GLOBAL_ENDPOINTS) {
        if (globalCalls.get(endpoint) !== 1) throw new Error(`${endpoint} was duplicated during stock switch`);
        if (globalSignals.get(endpoint)?.aborted) throw new Error(`${endpoint} was aborted by stock loadSeq`);
      }
      globalResolvers.forEach((resolve) => resolve());
      await Promise.all([firstLoad, secondLoad]);

      function globalPayload(url) {
        if (url === "/api/market") return { indices: [] };
        if (url === "/api/strong-stocks") return { items: [] };
        if (url === "/api/data/status") return { providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] };
        if (url === "/api/tasks/status") return { enabled: false, running: false, tasks: [] };
        return [];
      }

      function workbench(symbol) {
        const [code, market] = String(symbol).split(".");
        return {
          analysis: {
            quote: { code, market, name: symbol, price: 10, change: 0, change_pct: 0, source: "测试", timestamp: "2026-07-15" },
            data_quality: {},
            signal_snapshot: { label: "观察", summary: "测试" },
            review: {},
            klines: [],
          },
          insights: { overview: {} },
          chart_marks: { marks: [], categories: [] },
        };
      }
    '''
    _run_node_script(script)


def test_failed_global_panel_refresh_keeps_last_market_and_plate_values() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse } = await createAppHarness();
      let failing = false;
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (failing) return { ok: false, status: 503, async json() { return { detail: `${target} 刷新失败` }; } };
        if (target === "/api/market") {
          return jsonResponse({ indices: [{ name: "保留指数", price: 3000, change_pct: 1 }] });
        }
        if (target === "/api/strong-stocks") {
          return jsonResponse({ scope: "保留强股", sample_count: 1, items: [{ name: "保留股票", code: "600519", rank: 1 }] });
        }
        if (target === "/api/plates?limit=8") {
          return jsonResponse([{ name: "保留行业", rank: 1, change_pct: 1, source: "测试" }]);
        }
        throw new Error(`unexpected URL ${target}`);
      };

      await Promise.all([__appTest.loadMarketPanels({ force: true }), __appTest.loadPlateRank({ force: true })]);
      failing = true;
      await Promise.all([__appTest.loadMarketPanels({ force: true }), __appTest.loadPlateRank({ force: true })]);

      if (!element("marketStrip").innerHTML.includes("保留指数")) throw new Error("market failure cleared the cached index");
      if (!element("leaderList").innerHTML.includes("保留股票")) throw new Error("strong-stock failure cleared the cached ranking");
      if (!element("plateList").innerHTML.includes("保留行业")) throw new Error("plate failure cleared the cached ranking");
      if (!element("dataStatus").textContent.includes("市场概览暂不可用") || !element("dataStatus").textContent.includes("行业背景暂不可用")) {
        throw new Error(`cached global panels did not surface degradation: ${element("dataStatus").textContent}`);
      }
    '''
    _run_node_script(script)


def test_watchlist_subscription_change_rebuilds_sse_once() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse, streams } = await createAppHarness();
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 7;
      __appTest.state.lastAnalysis = { quote: { code: "600519", market: "SH" }, klines: [] };
      element("watchSymbolInput").value = "600000";
      element("watchNoteInput").value = "观察";
      element("watchForm-button").textContent = "加入";
      let reads = 0;
      globalThis.fetch = async (url, options = {}) => {
        const target = String(url);
        if (target === "/api/watchlist" && options.method === "POST") return jsonResponse({});
        if (target === "/api/watchlist") {
          reads += 1;
          return jsonResponse([{ symbol: "600000.SH", code: "600000", name: "浦发银行" }]);
        }
        throw new Error(`unexpected request ${target}`);
      };

      __appTest.startStream();
      const firstStream = streams[0];
      await element("watchForm").listeners.submit({ preventDefault() {}, currentTarget: element("watchForm") });
      if (streams.length !== 2 || !firstStream.closed || !streams[1].url.includes("600000.SH")) {
        throw new Error(`watchlist change did not rebuild exactly one subscription: ${streams.map((item) => item.url).join(" | ")}`);
      }

      element("watchNoteInput").value = "重复";
      await element("watchForm").listeners.submit({ preventDefault() {}, currentTarget: element("watchForm") });
      if (reads !== 2 || streams.length !== 2) {
        throw new Error(`unchanged watchlist rebuilt SSE or skipped readback: reads=${reads}, streams=${streams.length}`);
      }
    '''
    _run_node_script(script)


def test_excluded_watchlist_symbols_leave_observation_pool_but_active_symbol_stays_streamed() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, jsonResponse, streams } = await createAppHarness();
      __appTest.state.symbol = "600036.SH";
      __appTest.state.loadSeq = 91;
      __appTest.state.lastAnalysis = { quote: { code: "600036", market: "SH" }, klines: [] };
      __appTest.state.watchlist = [
        { symbol: "600036.SH", research_status: "excluded" },
        { symbol: "000001.SZ", research_status: "excluded" },
        { symbol: "600000.SH", research_status: "watching" },
      ];

      __appTest.startStream();
      const activeExcludedStream = decodeURIComponent(streams.at(-1).url);
      if (!activeExcludedStream.includes("600036.SH")) {
        throw new Error(`current excluded stock was removed from its active stream: ${activeExcludedStream}`);
      }
      if (activeExcludedStream.includes("000001.SZ") || !activeExcludedStream.includes("600000.SH")) {
        throw new Error(`excluded observation pool was not filtered: ${activeExcludedStream}`);
      }

      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 92;
      __appTest.state.watchlist = [{ symbol: "600000.SH", research_status: "watching" }];
      __appTest.startStream();
      const beforeTransition = streams.at(-1);
      globalThis.fetch = async (url) => {
        if (String(url) === "/api/watchlist") {
          return jsonResponse([{ symbol: "600000.SH", code: "600000", name: "浦发银行", research_status: "excluded" }]);
        }
        throw new Error(`unexpected request ${url}`);
      };

      await __appTest.refreshWatchlist({ force: true });
      const afterTransition = decodeURIComponent(streams.at(-1).url);
      if (streams.at(-1) === beforeTransition || !beforeTransition.closed) {
        throw new Error("switching a row to excluded did not coordinate a new stream");
      }
      if (afterTransition.includes("600000.SH")) {
        throw new Error(`newly excluded stock stayed in observation stream: ${afterTransition}`);
      }
    '''
    _run_node_script(script)


def test_watchlist_open_marks_viewed_only_after_current_workbench_success() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse, waitFor } = await createAppHarness({ canvasContext: null });
      const firstWorkbench = deferred();
      const markUrls = [];
      const markBodies = [];
      const watchlist = [
        queueItem("000001.SZ", "平安银行", 4),
        queueItem("300750.SZ", "宁德时代", 2),
        queueItem("600000.SH", "浦发银行", 5),
        queueItem("002594.SZ", "比亚迪", 3),
        queueItem("601318.SH", "中国平安", 6),
      ];
      let firstWorkbenchRequested = false;
      globalThis.fetch = async (url, options = {}) => {
        const target = String(url);
        if (target.startsWith("/api/stock/workbench")) {
          const symbol = new URL(target, "http://local").searchParams.get("symbol");
          if (symbol === "000001.SZ") {
            firstWorkbenchRequested = true;
            return { ok: true, json() { return firstWorkbench.promise; } };
          }
          if (symbol === "600000.SH") {
            return { ok: false, status: 503, async json() { return { detail: "研究台加载失败" }; } };
          }
          return jsonResponse(workbench(symbol));
        }
        if (target.endsWith("/mark-viewed") && options.method === "POST") {
          markUrls.push(target);
          markBodies.push(JSON.parse(options.body));
          if (target.includes("002594.SZ")) {
            return { ok: false, status: 503, async json() { return { detail: "已读服务繁忙" }; } };
          }
          const symbol = decodeURIComponent(target.split("/").at(-2));
          const item = watchlist.find((row) => row.symbol === symbol);
          item.unread_change_count = 0;
          item.last_viewed_at = "2026-07-15 12:00:00";
          return jsonResponse({ ...item });
        }
        if (target === "/api/watchlist") return jsonResponse(watchlist.map((item) => ({ ...item })));
        if (target === "/api/market") return jsonResponse({ indices: [] });
        if (target === "/api/strong-stocks") return jsonResponse({ items: [] });
        if (target === "/api/data/status") {
          return jsonResponse({ providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] });
        }
        if (target === "/api/tasks/status") return jsonResponse({ enabled: false, running: false, tasks: [] });
        if (target === "/api/tasks/runs?limit=8" || target === "/api/monitor/events?limit=8") return jsonResponse([]);
        if (target.startsWith("/api/plates")) return jsonResponse([]);
        if (target.startsWith("/api/advice/timeline")) {
          const symbol = new URL(target, "http://local").searchParams.get("symbol");
          if (symbol === "601318.SH") {
            return { ok: false, status: 503, async json() { return { detail: "建议时间线繁忙" }; } };
          }
          return jsonResponse([timelineItem(symbol)]);
        }
        if (target.startsWith("/api/stock/minute-analysis")) {
          return jsonResponse({ sample_count: 0, missing_data: ["分钟K线"], t_plan: { suitability: "不适合主动做T" } });
        }
        throw new Error(`unexpected request ${target}`);
      };

      const firstOpen = openButton("000001.SZ");
      await element("watchList").listeners.click({ target: { closest() { return firstOpen; } } });
      await waitFor(() => firstWorkbenchRequested, "delayed first workbench");
      if (markUrls.length !== 0) throw new Error("mark-viewed ran before workbench success");

      const secondOpen = openButton("300750.SZ");
      await element("watchList").listeners.click({ target: { closest() { return secondOpen; } } });
      await waitFor(() => markUrls.length === 1, "current mark-viewed request");
      if (!markUrls[0].includes("300750.SZ") || markUrls[0].includes("000001.SZ")) {
        throw new Error(`rapid switch marked the wrong stock: ${JSON.stringify(markUrls)}`);
      }
      if (markBodies[0].clear_unread !== true || markBodies[0].viewed_through_advice_id !== 73) {
        throw new Error(`mark-viewed did not use the rendered advice watermark: ${JSON.stringify(markBodies[0])}`);
      }
      firstWorkbench.resolve(workbench("000001.SZ"));
      await Promise.resolve();
      await Promise.resolve();
      if (markUrls.some((url) => url.includes("000001.SZ"))) {
        throw new Error(`stale workbench completion cleared old unread state: ${JSON.stringify(markUrls)}`);
      }
      await waitFor(
        () => __appTest.state.watchlist.find((item) => item.symbol === "300750.SZ").unread_change_count === 0,
        "local unread clear"
      );

      const marksBeforeFailure = markUrls.length;
      const failingOpen = openButton("600000.SH");
      await element("watchList").listeners.click({ target: { closest() { return failingOpen; } } });
      await waitFor(() => element("dataStatus").textContent.includes("600000.SH 加载失败"), "failed workbench status");
      await Promise.resolve();
      if (markUrls.length !== marksBeforeFailure || watchlist.find((item) => item.symbol === "600000.SH").unread_change_count !== 5) {
        throw new Error("failed workbench load incorrectly cleared unread state");
      }

      const markFailureOpen = openButton("002594.SZ");
      await element("watchList").listeners.click({ target: { closest() { return markFailureOpen; } } });
      await waitForLong(() => element("watchlistFeedback").textContent.includes("已读服务繁忙"), "mark-viewed failure feedback");
      if (element("stockName").textContent !== "比亚迪") throw new Error("mark-viewed failure blocked the successful workbench");
      if (watchlist.find((item) => item.symbol === "002594.SZ").unread_change_count !== 3) {
        throw new Error("failed mark-viewed incorrectly cleared unread state");
      }

      const marksBeforeTimelineFailure = markUrls.length;
      const timelineFailureOpen = openButton("601318.SH");
      await element("watchList").listeners.click({ target: { closest() { return timelineFailureOpen; } } });
      await waitForLong(() => element("watchlistFeedback").textContent.includes("未读状态保持"), "timeline failure unread feedback");
      if (markUrls.length !== marksBeforeTimelineFailure || watchlist.find((item) => item.symbol === "601318.SH").unread_change_count !== 6) {
        throw new Error("incomplete advice timeline incorrectly cleared unread state");
      }

      function openButton(symbol) {
        return { dataset: { action: "open", symbol } };
      }

      function queueItem(symbol, name, unread) {
        return {
          symbol,
          code: symbol.slice(0, 6),
          name,
          group_name: "跟踪",
          research_status: "watching",
          priority: "medium",
          unread_change_count: unread,
          latest_price: 10,
          latest_change_pct: 0,
        };
      }

      function timelineItem(symbol) {
        return {
          id: symbol === "300750.SZ" ? 73 : symbol === "002594.SZ" ? 94 : 11,
          action: "观察",
          confidence: 50,
          trend_score: 50,
          risk_level: "中等",
          created_at: "2026-07-15 10:00:00",
          market_time: "2026-07-15 10:00:00",
          comparison_status: "comparable",
          has_changes: true,
          changes: [{ category: "action", field: "action", before: "等待", after: "观察" }],
        };
      }

      function workbench(symbol) {
        const item = watchlist.find((row) => row.symbol === symbol) || queueItem(symbol, symbol, 0);
        return {
          analysis: {
            quote: { code: item.code, market: symbol.endsWith(".SH") ? "SH" : "SZ", name: item.name, price: 10, change: 0, change_pct: 0, source: "测试", timestamp: "2026-07-15 10:00:00" },
            data_quality: {},
            signal_snapshot: { label: "观察", summary: "测试" },
            review: {},
            klines: [],
          },
          insights: { overview: {} },
          chart_marks: { marks: [], categories: [] },
        };
      }

      function deferred() {
        let resolve;
        const promise = new Promise((settle) => { resolve = settle; });
        return { promise, resolve };
      }

      async function waitForLong(condition, label) {
        for (let index = 0; index < 200; index += 1) {
          if (condition()) return;
          await Promise.resolve();
        }
        throw new Error(`timed out waiting for ${label}`);
      }
    '''
    _run_node_script(script)


def test_committed_local_data_import_refreshes_all_runtime_owned_browser_state() -> None:
    script = r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

      const { __appTest, element, jsonResponse, streams } = await createAppHarness({ canvasContext: null });
      __appTest.state.symbol = "600519.SH";
      __appTest.state.loadSeq = 70;
      __appTest.state.lastAnalysis = workbench().analysis;
      __appTest.state.watchlist = [{ symbol: "601318.SH", code: "601318", name: "旧自选" }];
      __appTest.state.adviceReviewDetails = [{ plan: { id: 9, symbol: "600519.SH" } }];
      __appTest.state.localDataImportBundle = { kind: "ashare-radar-user-data", version: 1 };
      __appTest.state.localDataImportFileKey = "replace.json:10:1";
      __appTest.state.localDataImportSelectionGeneration = 1;
      __appTest.state.localDataImportPreviewRequestGeneration = 1;
      __appTest.state.localDataImportPreviewMode = "replace";
      __appTest.state.localDataImportPreviewFileKey = "replace.json:10:1";
      __appTest.state.localDataImportPreviewSelectionGeneration = 1;
      __appTest.state.localDataImportPreviewGeneration = 1;
      __appTest.state.localDataImportPreview = {
        preview_token: "x".repeat(40),
        preview_expires_at: "2099-01-01T00:00:00Z",
      };
      element("localDataImportMode").value = "replace";
      element("commitLocalDataImport").disabled = false;
      element("commitLocalDataImport").textContent = "提交导入";
      element("alertList").innerHTML = "旧预警";
      element("noteList").innerHTML = "旧笔记";
      element("reviewPlanList").innerHTML = "旧复盘";
      __appTest.startStream();
      const oldStream = streams.at(-1);
      const calls = [];

      globalThis.fetch = async (url, options = {}) => {
        const target = String(url);
        calls.push(`${options.method || "GET"} ${target}`);
        if (target.startsWith("/api/local-data/import") && options.method === "POST") {
          return jsonResponse({
            bundle_version: 1,
            mode: "replace",
            dry_run: false,
            committed: true,
            conflict_strategy: "remap_surrogate_ids_source_wins_on_stable_keys",
            tables: {},
            totals: { incoming: 0, inserted: 0, updated: 0, unchanged: 0, deleted: 4, remapped: 0 },
            rollback_backup_path: "/tmp/backup",
          });
        }
        if (target.startsWith("/api/stock/workbench")) return jsonResponse(workbench());
        if (target === "/api/watchlist") return jsonResponse([]);
        if (target === "/api/market") return jsonResponse({ indices: [] });
        if (target === "/api/strong-stocks") return jsonResponse({ items: [] });
        if (target === "/api/data/status") return jsonResponse({ providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] });
        if (target === "/api/tasks/status") return jsonResponse({ enabled: false, running: false, tasks: [] });
        if (target === "/api/tasks/runs?limit=8" || target === "/api/monitor/events?limit=8") return jsonResponse([]);
        if (target.startsWith("/api/plates") || target.startsWith("/api/advice/timeline") || target.startsWith("/api/reviews?")) return jsonResponse([]);
        if (target === "/api/system/diagnostics") return jsonResponse({});
        if (target.startsWith("/api/stock/minute-analysis")) {
          return jsonResponse({ symbol: "600519.SH", interval: "5m", sample_count: 0, missing_data: ["分钟K线"], t_plan: {} });
        }
        throw new Error(`unexpected request ${target}`);
      };

      await element("commitLocalDataImport").listeners.click({ preventDefault() {} });

      if (!oldStream.closed) throw new Error("import refresh left the pre-import quote stream open");
      if (!streams.at(-1).url.includes("600519.SH") || streams.at(-1).url.includes("601318.SH")) {
        throw new Error(`import refresh reused the old watchlist subscription: ${streams.at(-1).url}`);
      }
      if (__appTest.state.watchlist.length !== 0) throw new Error("import refresh kept the old watchlist in memory");
      if (!element("alertList").innerHTML.includes("暂无预警") || !element("noteList").innerHTML.includes("暂无笔记")) {
        throw new Error(`import refresh kept old local rows: ${element("alertList").innerHTML} | ${element("noteList").innerHTML}`);
      }
      if (__appTest.state.adviceReviewDetails.length !== 0 || !element("reviewPlanList").innerHTML.includes("暂无复盘计划")) {
        throw new Error(`import refresh kept old review state: ${element("reviewPlanList").innerHTML}`);
      }
      if (!element("localDataFeedback").textContent.includes("用户数据导入已提交") || element("localDataFeedback").dataset.tone !== "ok") {
        throw new Error(`successful import was mislabeled after refresh: ${element("localDataFeedback").textContent}`);
      }
      const importIndex = calls.findIndex((call) => call.startsWith("POST /api/local-data/import"));
      const reloadIndex = calls.findIndex((call) => call.startsWith("GET /api/stock/workbench"));
      if (importIndex < 0 || reloadIndex <= importIndex) throw new Error(`browser refreshed before import commit: ${JSON.stringify(calls)}`);

      function workbench() {
        return {
          analysis: {
            quote: { code: "600519", market: "SH", name: "贵州茅台", price: 10, change: 0, change_pct: 0, source: "测试", timestamp: "2026-07-15 10:00:00" },
            data_quality: {},
            signal_snapshot: { label: "观察", summary: "测试" },
            review: {},
            klines: [],
          },
          insights: { overview: {} },
          chart_marks: { marks: [], categories: [] },
          alert_rules: [],
          alert_events: [],
          notes: [],
        };
      }
    '''
    _run_node_script(script)


def test_alert_evaluation_uses_an_accessible_dedicated_status_region() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="alertEvaluation"' in html
    assert 'id="alertEvaluation" role="status" aria-live="polite" aria-atomic="true" aria-busy="false" hidden' in html


def _run_node_script(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
