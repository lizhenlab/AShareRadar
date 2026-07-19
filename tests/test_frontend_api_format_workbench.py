from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_fetch_json_normalizes_structured_fastapi_detail() -> None:
    script = r'''
      import { fetchJson } from "./static/js/api.js";

      async function messageFor(detail) {
        globalThis.fetch = async () => ({
          ok: false,
          async json() {
            return { detail };
          },
        });
        try {
          await fetchJson("/api/test");
        } catch (error) {
          return error.message;
        }
        throw new Error("expected fetchJson to throw");
      }

      const stringMessage = await messageFor("直接错误");
      if (stringMessage !== "直接错误") {
        throw new Error(`string detail changed: ${stringMessage}`);
      }

      const arrayMessage = await messageFor([
        { loc: ["body", "symbol"], msg: "字段必填", type: "missing" },
        { loc: ["query", "limit"], msg: "必须大于 0", type: "value_error" },
      ]);
      if (!arrayMessage.includes("body.symbol: 字段必填") || !arrayMessage.includes("query.limit: 必须大于 0")) {
        throw new Error(`FastAPI array detail was not readable: ${arrayMessage}`);
      }
      if (arrayMessage.includes("[object Object]")) {
        throw new Error(`array detail regressed to object string: ${arrayMessage}`);
      }

      const objectMessage = await messageFor({ reason: "数据源失败", retryable: false });
      if (!objectMessage.includes('"reason":"数据源失败"') || !objectMessage.includes('"retryable":false')) {
        throw new Error(`object detail was not stringified readably: ${objectMessage}`);
      }
      if (objectMessage.includes("[object Object]")) {
        throw new Error(`object detail regressed to object string: ${objectMessage}`);
      }
    '''
    _run_node_script(script)


def test_fetch_json_normalizes_network_and_non_json_errors() -> None:
    script = r'''
      import { fetchJson } from "./static/js/api.js";

      globalThis.fetch = async () => {
        throw new TypeError("Failed to fetch");
      };
      const networkMessage = await rejectedMessage(fetchJson("/api/test"));
      if (!networkMessage.includes("网络连接失败") || networkMessage.includes("Failed to fetch")) {
        throw new Error(`network error was not localized: ${networkMessage}`);
      }

      globalThis.fetch = async () => ({
        ok: false,
        status: 502,
        async json() {
          throw new SyntaxError("Unexpected token <");
        },
        async text() {
          return "<html>Bad Gateway</html>";
        },
      });
      const nonJsonMessage = await rejectedMessage(fetchJson("/api/test"));
      if (nonJsonMessage !== "请求失败（HTTP 502）") {
        throw new Error(`non-json error was not normalized: ${nonJsonMessage}`);
      }
      if (nonJsonMessage.includes("Unexpected") || nonJsonMessage.includes("<html>")) {
        throw new Error(`non-json error leaked raw details: ${nonJsonMessage}`);
      }

      globalThis.fetch = async () => ({
        ok: false,
        status: 404,
        async text() {
          return JSON.stringify({ detail: "文案可以变化" });
        },
      });
      try {
        await fetchJson("/api/missing");
        throw new Error("expected 404 to throw");
      } catch (error) {
        if (error.status !== 404 || error.message !== "文案可以变化") {
          throw new Error(`HTTP status was not preserved: ${error.status} ${error.message}`);
        }
      }

      async function rejectedMessage(promise) {
        try {
          await promise;
        } catch (error) {
          return error.message;
        }
        throw new Error("expected fetchJson to throw");
      }
    '''
    _run_node_script(script)


def test_fetch_json_accepts_empty_success_and_message_error_payloads() -> None:
    script = r'''
      import { fetchJson } from "./static/js/api.js";

      globalThis.fetch = async () => ({ ok: true, status: 204 });
      const noContent = await fetchJson("/api/delete");
      if (noContent !== null) {
        throw new Error(`204 response should resolve to null: ${String(noContent)}`);
      }

      globalThis.fetch = async () => ({
        ok: true,
        status: 200,
        headers: { get(name) { return name.toLowerCase() === "content-length" ? "0" : null; } },
      });
      const emptyBody = await fetchJson("/api/empty");
      if (emptyBody !== null) {
        throw new Error(`empty response should resolve to null: ${String(emptyBody)}`);
      }

      globalThis.fetch = async () => ({
        ok: false,
        status: 409,
        async text() {
          return JSON.stringify({ message: "已经存在" });
        },
      });
      const message = await rejectedMessage(fetchJson("/api/conflict"));
      if (message !== "已经存在") {
        throw new Error(`message payload was not surfaced: ${message}`);
      }

      globalThis.fetch = async () => ({
        ok: false,
        status: 502,
        async text() {
          return "上游行情源暂不可用";
        },
      });
      const textMessage = await rejectedMessage(fetchJson("/api/plain-error"));
      if (textMessage !== "上游行情源暂不可用") {
        throw new Error(`plain text error was not surfaced: ${textMessage}`);
      }

      async function rejectedMessage(promise) {
        try {
          await promise;
        } catch (error) {
          return error.message;
        }
        throw new Error("expected fetchJson to throw");
      }
    '''
    _run_node_script(script)


def test_fetch_json_keeps_abort_silent_capable_and_surfaces_timeout() -> None:
    script = r'''
      import { fetchJson, isAbortError } from "./static/js/api.js";

      globalThis.fetch = () => new Promise(() => {});
      const controller = new AbortController();
      const cancelled = fetchJson("/api/cancelled", { signal: controller.signal, timeoutMs: 1000 });
      controller.abort();
      let cancelledError;
      try {
        await cancelled;
      } catch (error) {
        cancelledError = error;
      }
      if (!isAbortError(cancelledError) || cancelledError.name !== "AbortError") {
        throw new Error(`request cancellation was masked: ${cancelledError && cancelledError.message}`);
      }

      let timeoutMessage = "";
      try {
        await fetchJson("/api/timeout", { timeoutMs: 5 });
      } catch (error) {
        timeoutMessage = error.message;
      }
      if (!timeoutMessage.includes("请求超时")) {
        throw new Error(`timeout did not surface a readable error: ${timeoutMessage}`);
      }
    '''
    _run_node_script(script)


def test_change_class_returns_neutral_for_zero_and_invalid_numbers() -> None:
    script = r'''
      import { changeClass } from "./static/js/format.js";

      const cases = [
        [null, "neutral"],
        [undefined, "neutral"],
        [Number.NaN, "neutral"],
        [Infinity, "neutral"],
        [-Infinity, "neutral"],
        ["abc", "neutral"],
        [2, "up"],
        ["0", "neutral"],
        [-0.1, "down"],
      ];

      for (const [value, expected] of cases) {
        const actual = changeClass(value);
        if (actual !== expected) {
          throw new Error(`changeClass(${String(value)}) returned ${actual}, expected ${expected}`);
        }
      }
    '''
    _run_node_script(script)


def test_number_formatters_hide_non_finite_values_and_clamp_digits() -> None:
    script = r'''
      import { formatAmount, formatNumber } from "./static/js/format.js";

      const numberCases = [
        [null, undefined, "--"],
        [undefined, undefined, "--"],
        [Number.NaN, undefined, "--"],
        [Infinity, undefined, "--"],
        [-Infinity, undefined, "--"],
        ["abc", undefined, "--"],
        [1.234, 2, "1.23"],
        [1.234, "bad", "1.23"],
        [1.234, -1, "1"],
        [1.234, 200, "1.23399999999999998579"],
      ];

      for (const [value, digits, expected] of numberCases) {
        const actual = digits === undefined ? formatNumber(value) : formatNumber(value, digits);
        if (actual !== expected) {
          throw new Error(`formatNumber(${String(value)}, ${String(digits)}) returned ${actual}, expected ${expected}`);
        }
      }

      const amountCases = [
        [null, "--"],
        [0, "--"],
        [-1, "--"],
        [Infinity, "--"],
        [9999, "9999"],
        [10000, "1.0万"],
        [120000000, "1.2亿"],
      ];

      for (const [value, expected] of amountCases) {
        const actual = formatAmount(value);
        if (actual !== expected) {
          throw new Error(`formatAmount(${String(value)}) returned ${actual}, expected ${expected}`);
        }
      }
    '''
    _run_node_script(script)


def test_workbench_renderers_tolerate_missing_quote_ma_and_arrays() -> None:
    script = r'''
      import { renderAnalysis, renderInsights, renderMarket, renderMinuteAnalysis, renderQuotes, renderStrongStocks } from "./static/js/workbench.js";

      const elements = new Map();
      function element(id) {
        if (!elements.has(id)) {
          const card = {
            classes: new Set(),
            classList: {
              remove(...names) {
                names.forEach((name) => card.classes.delete(name));
              },
              add(name) {
                card.classes.add(name);
              },
            },
          };
          elements.set(id, {
            id,
            innerHTML: "",
            textContent: "",
            className: "",
            card,
            closest(selector) {
              return selector === ".metric-card" ? card : null;
            },
          });
        }
        return elements.get(id);
      }

      globalThis.document = { getElementById: element };

      const drawCalls = [];
      const state = {};
      const analysis = {
        action_advice: {
          action: "观察",
          confidence: 68,
        },
        data_quality: {},
        signal_snapshot: {
          label: "待确认",
          confidence: 61,
          summary: "缺少数组字段",
        },
        review: {},
      };

      renderAnalysis(analysis, { state, drawKline: (...args) => drawCalls.push(args) });

      if (state.lastAnalysis !== analysis) {
        throw new Error("analysis state was not preserved");
      }
      if (element("stockCode").textContent !== "--" || element("stockName").textContent !== "--") {
        throw new Error("missing quote did not render fallback labels");
      }
      if (element("stockChange").className !== "neutral") {
        throw new Error(`missing quote change got tone ${element("stockChange").className}`);
      }
      if (element("actionAdvice").textContent !== "观察 · 建议强度 68/100") {
        throw new Error(`action advice used ambiguous score semantics: ${element("actionAdvice").textContent}`);
      }
      const signalEvidenceHtml = element("signalEvidence").innerHTML;
      if (!signalEvidenceHtml.includes("信号证据充分度 61/100") || signalEvidenceHtml.includes("置信度")) {
        throw new Error(`signal evidence used ambiguous score semantics: ${signalEvidenceHtml}`);
      }
      if (element("ma5").textContent !== "--" || element("ma20").textContent !== "--") {
        throw new Error("missing MA values did not render as empty numbers");
      }
      for (const id of ["ma5", "ma20"]) {
        const classes = element(id).card.classes;
        if (classes.has("good") || classes.has("warn") || classes.has("risk")) {
          throw new Error(`${id} received a tone despite missing quote or MA`);
        }
      }
      if (drawCalls.length !== 1 || !Array.isArray(drawCalls[0][0]) || drawCalls[0][0].length !== 0) {
        throw new Error("missing klines did not draw with an empty array");
      }
      for (const id of ["buySignals", "sellSignals", "tSignals"]) {
        if (element(id).innerHTML !== "") {
          throw new Error(`${id} should stay empty when the signal array is missing`);
        }
      }
      if (!element("reviewEvents").innerHTML.includes("暂无异常事件")) {
        throw new Error("review event empty state was not rendered");
      }
      if (!element("signalEvidence").innerHTML.includes("当前没有明显加分依据")) {
        throw new Error("signal evidence empty state was not rendered");
      }

      renderInsights({ overview: {} }, {});
      if (!element("stockEvents").innerHTML.includes("暂无事件")) {
        throw new Error("stock event empty state was not rendered");
      }
      if (!element("abnormalPanel").innerHTML.includes("暂无明显异动")) {
        throw new Error("abnormal event empty state was not rendered");
      }
      if (!element("ruleMatches").innerHTML.includes("暂无规则")) {
        throw new Error("rule match empty state was not rendered");
      }

      renderInsights({
        overview: {},
        valuation: {
          summary: "估值<script>",
          valuation_anchor_label: "历史锚<script>",
          peer_sample_count: "2<script>",
          evidence: ["证据<script>"],
          watch_points: ["关注<script>"],
        },
      }, {});
      const valuationHtml = element("valuationPanel").innerHTML;
      if (!valuationHtml.includes("估值&lt;script&gt;") || !valuationHtml.includes("历史锚&lt;script&gt;")) {
        throw new Error(`valuation text was not escaped: ${valuationHtml}`);
      }
      if (valuationHtml.includes("<script>")) {
        throw new Error("valuation renderer leaked raw script HTML");
      }

      renderMarket(undefined);
      renderStrongStocks(undefined);
      renderQuotes(undefined);
      if (!element("marketStrip").innerHTML.includes("暂无数据")) {
        throw new Error("market empty state was not rendered");
      }
      if (!element("leaderList").innerHTML.includes("暂无观察池排序")) {
        throw new Error("leader empty state was not rendered");
      }
      if (!element("quoteList").innerHTML.includes("实时观察等待中")) {
        throw new Error("quote empty state was not rendered");
      }
      renderStrongStocks([null], { scope: "脏样本", sample_count: 1 });
      if (!element("leaderList").innerHTML.includes("脏样本 · 样本 1") || !element("leaderList").innerHTML.includes("--")) {
        throw new Error(`dirty strong stock row was not safely degraded: ${element("leaderList").innerHTML}`);
      }
      renderMinuteAnalysis({
        sample_count: 12,
        missing_data: ["分钟K线"],
        t_plan: { suitability: "仅底仓可做T", execution_steps: [], stop_conditions: [] },
        supports: [],
        resistances: [],
        warnings: [],
      });
      if (!element("minuteAnalysis").innerHTML.includes("数据不可用") || element("minuteAnalysis").innerHTML.includes("仅底仓可做T")) {
        throw new Error(`legacy minute missing-data state was not conservative: ${element("minuteAnalysis").innerHTML}`);
      }

      renderQuotes([
        {
          name: "缓存行情",
          market: "SH",
          code: "600519",
          amount: 1000000,
          price: 10,
          change_pct: 1,
          from_cache: true,
        },
        {
          name: "兜底行情",
          market: "SZ",
          code: "000001",
          amount: 2000000,
          price: 11,
          change_pct: -1,
          from_cache: true,
          fallback_used: true,
        },
      ]);
      const quoteHtml = element("quoteList").innerHTML;
      if (!quoteHtml.includes("缓存行情") || !quoteHtml.includes(" · 缓存") || !quoteHtml.includes("兜底行情") || !quoteHtml.includes(" · 兜底缓存")) {
        throw new Error(`quote cache labels were not rendered: ${quoteHtml}`);
      }

      renderStrongStocks(
        [{
          name: "龙头<script>",
          code: "600519<script>",
          reason: "资金强<script>",
          tags: ["白酒<script>", "放量"],
          rank: 1,
          leader_score: 88,
          change_pct: 3.2,
        }],
        { scope: "观察池<script>", sample_count: 12 }
      );
      const leaderHtml = element("leaderList").innerHTML;
      if (!leaderHtml.includes("观察池&lt;script&gt; · 样本 12")) {
        throw new Error("strong stock meta scope/sample was not escaped or rendered");
      }
      if (!leaderHtml.includes("龙头&lt;script&gt;") || !leaderHtml.includes("白酒&lt;script&gt; / 放量")) {
        throw new Error("strong stock row content was not escaped");
      }
      if (leaderHtml.includes("<script>")) {
        throw new Error("strong stock renderer leaked raw script HTML");
      }
    '''
    _run_node_script(script)


def test_minute_workbench_uses_availability_contract_and_escapes_status_content() -> None:
    script = r'''
      import { minuteAvailabilityState, renderMinuteAnalysis } from "./static/js/workbench.js";

      const elements = new Map();
      globalThis.document = {
        getElementById(id) {
          if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "" });
          return elements.get(id);
        },
      };

      const explicitOk = minuteAvailabilityState({
        availability: "ok",
        sample_count: 0,
        missing_data: ["旧字段不应覆盖显式状态"],
      });
      if (explicitOk.status !== "ok") {
        throw new Error(`explicit availability was ignored: ${JSON.stringify(explicitOk)}`);
      }

      const legacyShort = minuteAvailabilityState({ sample_count: 7, missing_data: [] });
      const legacyAnalyzable = minuteAvailabilityState({ sample_count: 8, missing_data: [] });
      const legacyCoreMissing = minuteAvailabilityState({ sample_count: 120, missing_data: ["分钟K线"] });
      const unknown = minuteAvailabilityState({ availability: "mystery", sample_count: 120 });
      const uppercaseUnknown = minuteAvailabilityState({ availability: "OK", sample_count: 120 });
      const emptyUnknown = minuteAvailabilityState({ availability: "", sample_count: 120 });
      if (legacyShort.status !== "unavailable") {
        throw new Error(`legacy short payload was not unavailable: ${JSON.stringify(legacyShort)}`);
      }
      if (legacyAnalyzable.status !== "degraded") {
        throw new Error(`legacy payload was unsafely promoted: ${JSON.stringify(legacyAnalyzable)}`);
      }
      if (legacyCoreMissing.status !== "unavailable") {
        throw new Error(`legacy missing core data was not unavailable: ${JSON.stringify(legacyCoreMissing)}`);
      }
      if ([unknown, uppercaseUnknown, emptyUnknown].some((state) => state.status !== "unavailable")) {
        throw new Error(`unknown availability was not conservative: ${JSON.stringify([unknown, uppercaseUnknown, emptyUnknown])}`);
      }

      renderMinuteAnalysis({
        availability: "unavailable",
        availability_reason: '数据源失败<img src=x onerror="alert(1)">',
        sample_count: 99,
        missing_data: ["分钟K线<script>alert(1)</script>"],
        summary: "HTTP 200 也不代表业务可用",
        t_plan: {
          suitability: "仅底仓可做T",
          low_zone: "9.00-9.10",
          high_zone: "10.00-10.10",
          execution_steps: ["立即买入<script>"],
        },
        supports: [],
        resistances: [],
        warnings: [],
      });
      const unavailableHtml = elements.get("minuteAnalysis").innerHTML;
      if (!unavailableHtml.includes("数据不可用") || !unavailableHtml.includes("数据源失败&lt;img")) {
        throw new Error(`unavailable status was not rendered safely: ${unavailableHtml}`);
      }
      if (!unavailableHtml.includes("分钟K线&lt;script&gt;") || unavailableHtml.includes("<script>")) {
        throw new Error(`unavailable missing data was not escaped: ${unavailableHtml}`);
      }
      if (unavailableHtml.includes("9.00-9.10") || unavailableHtml.includes("10.00-10.10") || unavailableHtml.includes("立即买入")) {
        throw new Error(`unavailable payload leaked executable guidance: ${unavailableHtml}`);
      }

      renderMinuteAnalysis({
        availability: "degraded",
        availability_reason: "量能缺失<script>alert(2)</script>；趋势、支撑压力和价格区间仍可用。",
        sample_count: 8,
        missing_data: ["有效分钟成交量<script>"],
        trend_label: "震荡偏强<script>",
        momentum_label: "动量平稳",
        interval: "5m",
        source: "缓存分钟线",
        summary: "价格结构仍可参考。",
        latest_price: 10,
        intraday_change_pct: 0.2,
        intraday_range_pct: 1.1,
        volume_pulse: "量能待确认",
        t_plan: {
          suitability: "仅底仓可做T",
          low_zone: "9.80-9.90",
          high_zone: "10.10-10.20",
          execution_steps: [],
          stop_conditions: [],
        },
        supports: [],
        resistances: [],
        warnings: [],
      });
      const degradedHtml = elements.get("minuteAnalysis").innerHTML;
      if (!degradedHtml.includes("分钟分析降级") || !degradedHtml.includes("数据降级")) {
        throw new Error(`degraded status was not visible: ${degradedHtml}`);
      }
      if (!degradedHtml.includes("9.80-9.90") || !degradedHtml.includes("仍可用")) {
        throw new Error(`degraded usable conclusions were hidden: ${degradedHtml}`);
      }
      if (!degradedHtml.includes("量能缺失&lt;script&gt;") || !degradedHtml.includes("震荡偏强&lt;script&gt;")) {
        throw new Error(`degraded status text was not escaped: ${degradedHtml}`);
      }
      if (degradedHtml.includes("<script>")) {
        throw new Error(`degraded renderer leaked raw HTML: ${degradedHtml}`);
      }

      renderMinuteAnalysis({
        availability: "ok",
        availability_reason: "完整有效数据",
        sample_count: 16,
        missing_data: [],
        trend_label: "盘中转弱",
        momentum_label: "短线走弱",
        interval: "5m",
        source: "实时分钟线",
        summary: "当前不适合主动做T。",
        t_plan: {
          suitability: "不适合主动做T",
          low_zone: "9.80-9.90",
          high_zone: "10.10-10.20",
          execution_steps: [],
          stop_conditions: [],
        },
        supports: [],
        resistances: [],
        warnings: [],
      });
      const okRiskHtml = elements.get("minuteAnalysis").innerHTML;
      if (!okRiskHtml.includes("数据可用") || !okRiskHtml.includes("结论 不适合主动做T")) {
        throw new Error(`business risk incorrectly changed availability: ${okRiskHtml}`);
      }
      if (okRiskHtml.includes("分钟分析降级") || okRiskHtml.includes("分钟分析不可用")) {
        throw new Error(`business risk was rendered as a data failure: ${okRiskHtml}`);
      }
    '''
    _run_node_script(script)


def test_workbench_distinguishes_proxy_orderbook_and_financial_data_semantics() -> None:
    script = r'''
      import { renderInsights } from "./static/js/workbench.js";

      const elements = new Map();
      globalThis.document = {
        getElementById(id) {
          if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "" });
          return elements.get(id);
        },
      };

      renderInsights({
        overview: {},
        fund_flow: {
          data_nature: "derived",
          overall_score: 61,
          level: "偏强",
          source: "行情源·量价衍生指标（非真实资金流）",
          price_volume_relation: "量价配合偏积极。",
          windows: [],
          notes: [],
        },
        order_pressure: {
          data_nature: "observed",
          pressure_level: "买盘偏强",
          source: "实时盘口",
          summary: "当前挂单买盘较强。",
          notes: [],
        },
        financial_health: {
          score: 91,
          level: "强",
          summary: "旧数据错误地带有分数。",
          source: "行情字段",
          metrics: [],
        },
      });

      const fundHtml = elements.get("fundFlowPanel").innerHTML;
      const orderHtml = elements.get("orderPressurePanel").innerHTML;
      const financialHtml = elements.get("financialPanel").innerHTML;
      if (!fundHtml.includes("衍生（derived）") || !fundHtml.includes("非真实资金流")) {
        throw new Error(`derived proxy semantics were missing: ${fundHtml}`);
      }
      if (!orderHtml.includes("实测（observed）") || !orderHtml.includes("订单压力")) {
        throw new Error(`observed order semantics were missing: ${orderHtml}`);
      }
      if (!financialHtml.includes("市场估值与交易体征 · 财务体检分不可用") || financialHtml.includes("财务体检 91")) {
        throw new Error(`legacy market fields leaked a financial health score: ${financialHtml}`);
      }

      renderInsights({
        overview: {},
        fund_flow: { data_nature: "unavailable", overall_score: 50, level: "不可用", windows: [], notes: [] },
        order_pressure: { data_nature: "estimated", pressure_level: "区间承压", notes: [] },
      });
      if (!elements.get("fundFlowPanel").innerHTML.includes("不可用（unavailable）")) {
        throw new Error("unavailable proxy status was not persistent");
      }
      if (!elements.get("orderPressurePanel").innerHTML.includes("估算（estimated）")) {
        throw new Error("estimated order-pressure status was not persistent");
      }
    '''
    _run_node_script(script)


def _run_node_script(script: str) -> None:
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)
