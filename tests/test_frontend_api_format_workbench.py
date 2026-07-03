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
      import { renderAnalysis, renderInsights, renderMarket, renderQuotes, renderStrongStocks } from "./static/js/workbench.js";

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
        data_quality: {},
        signal_snapshot: {
          label: "待确认",
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


def _run_node_script(script: str) -> None:
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)
