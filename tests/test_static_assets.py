from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
JS_FUNCTION_PATTERNS = [
    re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?[A-Za-z_$][\w$]*\s*=>"),
]
JS_BRANCH_RE = re.compile(r"\b(if|for|while|catch|switch|case)\b|\?|&&|\|\|")
JS_STRING_RE = re.compile(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)')


def test_index_links_css_entrypoint() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="/static/styles.css" />' in html


def test_css_entrypoint_imports_existing_modules_in_order() -> None:
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    imports = re.findall(r'@import url\("/static/css/([^"]+)"\);', css)

    assert imports == [
        "base.css",
        "sidebar.css",
        "workspace-core.css",
        "research-panels.css",
        "interactions.css",
        "side-footer.css",
        "responsive.css",
    ]
    for filename in imports:
        path = STATIC_DIR / "css" / filename
        assert path.exists(), f"missing CSS module: {filename}"
        assert path.read_text(encoding="utf-8").strip(), f"empty CSS module: {filename}"


def test_side_leader_rows_do_not_force_cjk_letter_breaks() -> None:
    css = (STATIC_DIR / "css" / "side-footer.css").read_text(encoding="utf-8")

    assert ".leader-row.leader-scope" in css
    assert "overflow-wrap: break-word;" in css
    assert "word-break: normal;" in css
    assert "overflow-wrap: anywhere;" not in css


def test_frontend_js_functions_stay_small_enough_to_review() -> None:
    hotspots = [
        function
        for path in sorted((STATIC_DIR / "js").glob("*.js")) + [STATIC_DIR / "app.js"]
        for function in _js_functions(path)
        if function["lines"] >= 60 or function["branches"] >= 20
    ]

    assert hotspots == []


def test_ui_symbol_validation_matches_backend_zero_code_rule() -> None:
    script = r'''
      import { normalizeUiSymbol, validateUiSymbol, UI_SYMBOL_ERROR_MESSAGE } from "./static/js/symbols.js";

      if (normalizeUiSymbol("600519") !== "600519.SH") {
        throw new Error("expected SH normalization for 600519");
      }
      if (validateUiSymbol("sz000001") !== "000001.SZ") {
        throw new Error("expected SZ prefix normalization");
      }
      let rejected = false;
      try {
        validateUiSymbol("000000");
      } catch (error) {
        rejected = true;
        if (error.message !== UI_SYMBOL_ERROR_MESSAGE || !error.message.includes("不能全为0")) {
          throw error;
        }
      }
      if (!rejected) {
        throw new Error("expected all-zero symbol to be rejected");
      }
    '''
    _run_node_script(script)



def test_research_panel_renderer_escapes_and_formats_core_panels() -> None:
    script = r'''
      import { runResearchPanelSmoke } from "./tests/static_research_smoke_helpers.mjs";

      await runResearchPanelSmoke();
    '''

    _run_node_script(script)


def test_research_panel_ai_question_submit_posts_and_escapes_answer() -> None:
    script = r'''
      import { runAiQuestionSubmitSmoke } from "./tests/static_research_smoke_helpers.mjs";

      await runAiQuestionSubmitSmoke();
    '''

    _run_node_script(script)

def test_chart_renderer_draws_active_marks_with_fake_canvas() -> None:
    script = r'''
      import { drawKlineChart } from "./static/js/chart.js";

      globalThis.window = { devicePixelRatio: 1 };
      const calls = [];
      const ctx = {
        scale: (...args) => calls.push(["scale", ...args]),
        clearRect: (...args) => calls.push(["clearRect", ...args]),
        beginPath: () => calls.push(["beginPath"]),
        moveTo: (...args) => calls.push(["moveTo", ...args]),
        lineTo: (...args) => calls.push(["lineTo", ...args]),
        stroke: () => calls.push(["stroke"]),
        fillRect: (...args) => calls.push(["fillRect", ...args]),
        arc: (...args) => calls.push(["arc", ...args]),
        fill: () => calls.push(["fill"]),
        fillText: (...args) => calls.push(["fillText", ...args]),
        set fillStyle(value) { calls.push(["fillStyle", value]); },
        get fillStyle() { return ""; },
        set strokeStyle(value) { calls.push(["strokeStyle", value]); },
        get strokeStyle() { return ""; },
        set lineWidth(value) { calls.push(["lineWidth", value]); },
        get lineWidth() { return 1; },
        set font(value) { calls.push(["font", value]); },
        get font() { return ""; },
      };
      const canvas = {
        clientWidth: 640,
        clientHeight: 320,
        getContext(type) {
          if (type !== "2d") throw new Error("unexpected context type");
          return ctx;
        },
      };
      const rows = Array.from({ length: 25 }, (_, index) => ({
        date: `2026-05-${String(index + 1).padStart(2, "0")}`,
        open: 100 + index,
        close: 101 + index,
        high: 103 + index,
        low: 99 + index,
      }));
      const marks = [
        { category: "买点", kline_date: "2026/05/10", label: "低吸确认", level: "积极", visible: true },
        { category: "风险", kline_date: "2026-05-11", label: "风险", level: "风险", visible: true },
        { category: "买点", kline_date: "2026-05-12", label: "隐藏", level: "积极", visible: false },
      ];

      drawKlineChart({
        canvas,
        rows,
        marks,
        activeCategories: new Set(["买点"]),
        formatNumber: (value) => Number(value).toFixed(1),
      });

      const textCalls = calls.filter((item) => item[0] === "fillText").map((item) => String(item[1]));
      const arcCount = calls.filter((item) => item[0] === "arc").length;
      if (!textCalls.includes("低吸确认")) {
        throw new Error(`active chart mark label was not drawn: ${textCalls.join(",")}`);
      }
      if (textCalls.includes("风险") || textCalls.includes("隐藏")) {
        throw new Error(`inactive or hidden chart mark was drawn: ${textCalls.join(",")}`);
      }
      if (arcCount !== 1) {
        throw new Error(`expected exactly one active chart mark arc, got ${arcCount}`);
      }
    '''

    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


def test_chart_renderer_filters_dirty_rows_before_canvas_math() -> None:
    script = r'''
      import { drawKlineChart } from "./static/js/chart.js";

      globalThis.window = { devicePixelRatio: 1 };
      const calls = [];
      const ctx = {
        scale: (...args) => calls.push(["scale", ...args]),
        clearRect: (...args) => calls.push(["clearRect", ...args]),
        beginPath: () => calls.push(["beginPath"]),
        moveTo: (...args) => calls.push(["moveTo", ...args]),
        lineTo: (...args) => calls.push(["lineTo", ...args]),
        stroke: () => calls.push(["stroke"]),
        fillRect: (...args) => calls.push(["fillRect", ...args]),
        arc: (...args) => calls.push(["arc", ...args]),
        fill: () => calls.push(["fill"]),
        fillText: (...args) => calls.push(["fillText", ...args]),
        set fillStyle(value) { calls.push(["fillStyle", value]); },
        get fillStyle() { return ""; },
        set strokeStyle(value) { calls.push(["strokeStyle", value]); },
        get strokeStyle() { return ""; },
        set lineWidth(value) { calls.push(["lineWidth", value]); },
        get lineWidth() { return 1; },
        set font(value) { calls.push(["font", value]); },
        get font() { return ""; },
      };
      const canvas = {
        clientWidth: 640,
        clientHeight: 320,
        getContext(type) {
          if (type !== "2d") throw new Error("unexpected context type");
          return ctx;
        },
      };
      const rows = [
        { date: "2026-05-01", open: 100, close: 101, high: Infinity, low: 99 },
        { date: "2026-05-02", open: 100, close: 101, high: 102, low: 99 },
        { date: "2026-05-03", open: 100, close: 101, high: 99, low: 102 },
        { date: "2026-05-04", open: "102", close: "103", high: "104", low: "101" },
      ];

      drawKlineChart({
        canvas,
        rows,
        ma5: Infinity,
        ma20: "bad",
        marks: [{ category: "买点", kline_date: "2026-05-02", label: "有效", price: Infinity, visible: true }],
        activeCategories: ["买点"],
      });

      for (const call of calls) {
        for (const arg of call.slice(1)) {
          if (typeof arg === "number" && !Number.isFinite(arg)) {
            throw new Error(`non-finite canvas argument from ${call[0]}: ${String(arg)}`);
          }
        }
      }
      const candleCount = calls.filter((item) => item[0] === "fillRect").length;
      if (candleCount !== 2) {
        throw new Error(`expected only two valid candles, got ${candleCount}`);
      }
      const labels = calls.filter((item) => item[0] === "fillText").map((item) => String(item[1]));
      if (!labels.includes("有效")) {
        throw new Error(`valid mark was not drawn after dirty-row filtering: ${labels.join(",")}`);
      }
      if (labels.join(" ").includes("Infinity") || labels.join(" ").includes("NaN")) {
        throw new Error(`non-finite label leaked into chart: ${labels.join(",")}`);
      }
    '''

    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


def test_workbench_renderer_escapes_events_and_signal_evidence_with_fake_dom() -> None:
    script = r'''
      import { renderAnalysis, renderInsights } from "./static/js/workbench.js";

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
            closest(selector) {
              return selector === ".metric-card" ? card : null;
            },
          });
        }
        return elements.get(id);
      }

      globalThis.document = { getElementById: element };
      const drawCalls = [];
      const analysis = {
        quote: {
          market: "SH",
          code: "600519",
          name: "贵州茅台<script>",
          price: 1200,
          change: 12,
          change_pct: 1,
        },
        trend_score: 72,
        trend_label: "偏强",
        action_advice: { action: "观察", confidence: 66 },
        support: 1180,
        resistance: 1260,
        ma5: 1190,
        ma20: 1170,
        data_quality: {
          level: "优秀",
          score: 91,
          consistency_level: "一致",
          source: "本地<script>",
          notes: ["报价完整<script>"],
          anomalies: ["无异常<script>"],
        },
        beginner_summary: "摘要<script>",
        signal_snapshot: {
          label: "偏多<script>",
          confidence: 64,
          summary: "信号摘要<script>",
          positive: [{ name: "趋势<script>", impact: 3, reason: "站上均线<script>" }],
          negative: [{ name: "风险<script>", impact: -2, reason: "接近压力<script>" }],
          neutral: [],
          risk_notes: ["不要追高<script>"],
        },
        buy_points: [{ title: "低吸<script>", level: "积极", reason: "靠近支撑<script>" }],
        sell_points: [],
        t_plan: [],
        review: {
          review_summary: "复盘摘要<script>",
          key_points: [{ label: "胜率<script>", value: "80%<script>", level: "积极" }],
          events: [{ title: "大涨<script>", date: "2026-05-01", level: "积极", description: "事件<script>" }],
        },
        klines: [],
      };
      const insights = {
        overview: {
          total_score: 61,
          total_level: "中性",
          main_conflict: "等待确认<script>",
          beginner_takeaways: [],
          key_prices: [],
        },
        fund_flow: {
          overall_score: 50,
          level: "普通",
          source: "估算",
          price_volume_relation: "量价一般",
          windows: [],
          notes: [],
        },
        order_pressure: {
          pressure_level: "普通",
          source: "fallback",
          summary: "盘口暂缺",
          bid_ask_ratio: null,
          spread_pct: null,
          notes: [],
        },
        strategy_cards: [],
        events: {
          events: [{
            title: "公告<script>",
            level: "关注",
            date: "2026-05-01",
            category: "公告<script>",
            source: "交易所<script>",
            description: "事件说明<script>",
            reliability: "高<script>",
            action_hint: "核查原文<script>",
          }],
          next_steps: ["第一步<script>", "第二步", "第三步", "第四步", "第五步不显示"],
          missing_sources: ["研报<script>", "互动易"],
        },
        financial_health: { score: 60, level: "普通", source: "本地", summary: "财务摘要", metrics: [] },
        valuation: { score: 55, level: "中性", source: "本地", summary: "估值摘要", evidence: [], watch_points: [] },
        abnormal_events: { main_signal: "无明显异动", level: "普通", score: 50, events: [] },
        lhb: { available: false, level: "待确认", source: "本地", summary: "龙虎榜待接入", reasons: [], action_items: [] },
        rule_matches: { matches: [] },
      };

      renderAnalysis(analysis, { state: {}, drawKline: (...args) => drawCalls.push(args) });
      renderInsights(insights, {});

      const signalHtml = element("signalEvidence").innerHTML;
      if (!signalHtml.includes("趋势&lt;script&gt; +3") || !signalHtml.includes("风险&lt;script&gt; -2")) {
        throw new Error("signal evidence did not render signed escaped impact");
      }
      if (!signalHtml.includes("当前没有明显中性观察") || signalHtml.includes("<script>")) {
        throw new Error("signal evidence did not render empty neutral state or escaped text");
      }
      const eventHtml = element("stockEvents").innerHTML;
      if (!eventHtml.includes("公告&lt;script&gt;") || !eventHtml.includes("待补数据：研报&lt;script&gt; / 互动易")) {
        throw new Error("stock events did not render escaped event and missing source details");
      }
      if (eventHtml.includes("第五步不显示") || eventHtml.includes("<script>")) {
        throw new Error("stock event follow-up list was not limited or escaped");
      }
      if (!element("qualityPanel").innerHTML.includes("报价完整&lt;script&gt;")) {
        throw new Error("quality panel did not render escaped data-quality notes");
      }
      const reviewHtml = element("reviewPoints").innerHTML + element("reviewEvents").innerHTML;
      if (!reviewHtml.includes("80%&lt;script&gt;") || reviewHtml.includes("<script>")) {
        throw new Error("review panel did not escape dynamic values");
      }
      if (drawCalls.length !== 1) {
        throw new Error(`expected one chart draw call, got ${drawCalls.length}`);
      }
    '''

    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


def test_diagnostics_renderer_runs_with_fake_dom() -> None:
    script = r'''
      import { loadDataStatus, loadMonitoring } from "./static/js/diagnostics.js";

      const elements = new Map();
      function element(id) {
        if (!elements.has(id)) {
          elements.set(id, {
            id,
            innerHTML: "",
            textContent: "",
          });
        }
        return elements.get(id);
      }

      globalThis.document = {
        hidden: true,
        getElementById: element,
        querySelectorAll() {
          return [];
        },
      };

      const responses = {
        "/api/tasks/status": {
          running: true,
          enabled: true,
          tasks: [
            {
              name: "refresh_quotes",
              display_name: "刷新报价<script>",
              running: true,
              last_status: "running",
              last_message: "正在执行",
              next_run_at: "2026-05-13 10:05:00",
            },
            {
              name: "refresh_kline",
              display_name: "刷新K线",
              running: false,
              last_status: "failed",
              last_message: "",
              next_run_at: null,
            },
          ],
        },
        "/api/tasks/runs?limit=8": [
          { task_name: "refresh_kline", status: "failed", message: "网络失败<script>" },
        ],
        "/api/monitor/events?limit=8": [
          {
            category: "provider",
            symbol: "600519.SH",
            level: "warning",
            message: "数据源失败<script>",
            created_at: "2026-05-13 10:00:00",
            last_seen_at: "2026-05-13 10:01:00",
            repeat_count: 2,
          },
        ],
        "/api/data/status": {
          source_plan: {
            health_level: "高风险",
            summary: "主源失败<script>",
            primary_quote_source: "腾讯",
            primary_kline_source: "",
            primary_minute_source: null,
            warnings: ["报价源失败<script>"],
            suggestions: ["检查网络", "切换备用源", "第三条不显示"],
          },
          providers: [
            {
              name: "akshare<script>",
              enabled: true,
              healthy: false,
              success_count: 1,
              failure_count: 2,
              last_error: "ProxyError<script>",
              last_success: "2026-05-12 15:00:00",
              latency_ms: null,
            },
          ],
          cache: {
            quote_count: 3,
            kline_count: 4,
            stock_count: 5,
            plate_count: 6,
            quote_history_count: 7,
          },
          capabilities: [
            { name: "报价", reliability_level: "公开源", enabled: true },
          ],
          capability_statuses: [
            {
              name: "akshare",
              kind: "quote",
              enabled: true,
              healthy: false,
              last_success: "",
              last_error: "失败",
              success_count: 0,
              failure_count: 1,
            },
          ],
        },
      };

      globalThis.fetch = async (url) => ({
        ok: true,
        async json() {
          return responses[url];
        },
      });

      await loadMonitoring({ monitorTimer: null });
      await loadDataStatus();

      if (element("schedulerState").textContent !== "运行中") {
        throw new Error("scheduler status was not rendered");
      }
      const taskHtml = element("taskCards").innerHTML;
      if (!taskHtml.includes("执行中") || !taskHtml.includes("网络失败&lt;script&gt;")) {
        throw new Error("task cards did not render running and recent failure states");
      }
      const eventHtml = element("monitorEvents").innerHTML;
      if (!eventHtml.includes("数据源") || !eventHtml.includes("重复 2 次") || eventHtml.includes("<script>")) {
        throw new Error("monitor event did not render escaped warning content");
      }
      const sourcePlanHtml = element("sourcePlan").innerHTML;
      if (!sourcePlanHtml.includes("source-plan-head risk") || !sourcePlanHtml.includes("<b>日K</b>缺失")) {
        throw new Error("source plan did not render risk tone and missing source fallback");
      }
      if (sourcePlanHtml.includes("第三条不显示") || sourcePlanHtml.includes("<script>")) {
        throw new Error("source plan did not limit suggestions or escape text");
      }
      const providerHtml = element("providerStatus").innerHTML;
      if (!providerHtml.includes("akshare&lt;script&gt;") || !providerHtml.includes("最近失败")) {
        throw new Error("provider status did not render escaped failure state");
      }
      if (!element("cacheStats").innerHTML.includes("能力状态：akshare·报价·失败")) {
        throw new Error("capability status summary was not rendered");
      }
    '''

    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


def test_diagnostics_renderer_tolerates_malformed_partial_payloads() -> None:
    script = r'''
      import { loadDataStatus, loadMonitoring } from "./static/js/diagnostics.js";

      const elements = new Map();
      function element(id) {
        if (!elements.has(id)) {
          elements.set(id, {
            id,
            innerHTML: "",
            textContent: "",
          });
        }
        return elements.get(id);
      }

      let timerStarted = 0;
      globalThis.setInterval = () => {
        timerStarted += 1;
        return 99;
      };
      globalThis.clearInterval = () => {};
      globalThis.document = {
        hidden: false,
        getElementById: element,
        querySelectorAll() {
          return [];
        },
      };

      const responses = {
        "/api/tasks/status": { enabled: true, running: false, tasks: { bad: "shape" } },
        "/api/tasks/runs?limit=8": { rows: [] },
        "/api/monitor/events?limit=8": { items: [] },
        "/api/data/status": {
          source_plan: {
            health_level: "未知",
            summary: "",
            warnings: { bad: "shape" },
            suggestions: "检查网络",
          },
          providers: { bad: "shape" },
          cache: null,
          capabilities: "bad",
          capability_statuses: { bad: "shape" },
        },
      };

      globalThis.fetch = async (url) => ({
        ok: true,
        async json() {
          return responses[url];
        },
      });

      const state = { monitorTimer: null };
      await loadMonitoring(state);
      await loadDataStatus();

      if (state.monitorTimer !== 99 || timerStarted !== 1) {
        throw new Error("monitor timer was not maintained after malformed payload rendering");
      }
      if (!element("taskCards").innerHTML.includes("暂无调度任务")) {
        throw new Error(`malformed tasks did not render empty task state: ${element("taskCards").innerHTML}`);
      }
      if (!element("monitorEvents").innerHTML.includes("暂无事件")) {
        throw new Error(`malformed events did not render empty event state: ${element("monitorEvents").innerHTML}`);
      }
      if (!element("providerStatus").innerHTML.includes("暂无数据源状态")) {
        throw new Error(`malformed providers did not render empty provider state: ${element("providerStatus").innerHTML}`);
      }
      if (!element("cacheStats").innerHTML.includes("报价 0 条") || !element("cacheStats").innerHTML.includes("能力：等待探测")) {
        throw new Error(`malformed cache/capabilities did not render safe defaults: ${element("cacheStats").innerHTML}`);
      }
    '''

    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


def test_research_panels_tolerate_malformed_optional_collections() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      const elements = new Map();
      function element(id) {
        if (!elements.has(id)) {
          elements.set(id, {
            id,
            innerHTML: "",
            textContent: "",
            value: "",
            isConnected: true,
            onclick: null,
            addEventListener(type, handler) {
              this.listener = { type, handler };
            },
            querySelector(selector) {
              if (selector === "button") return element(`${id}-button`);
              if (selector === ".ai-card-wide") return null;
              return null;
            },
            closest(selector) {
              if (selector === "#aiDashboard") return element("aiDashboard");
              return null;
            },
            insertAdjacentHTML() {},
            remove() {},
          });
        }
        return elements.get(id);
      }

      globalThis.document = {
        getElementById: element,
        querySelector() {
          return null;
        },
      };

      renderResearch({
        qa_report: { items: { bad: true } },
        evidence_chain: { support: { bad: true }, opposition: null, invalidations: { bad: true } },
        risk_radar: { summary: "风险", items: { bad: true } },
        event_digest: { negative_events: { bad: true }, positive_events: null, watch_events: { bad: true } },
        peer_comparison: { metrics: { bad: true } },
        t_strategy: { stop_conditions: { bad: true } },
        feature_snapshot: { tags: { bad: true } },
        diagnosis: { confirmation_signals: { bad: true }, hard_risks: { bad: true } },
        alpha_evidence: { positives: { bad: true }, negatives: "bad", missing_data: { bad: true } },
        market_regime: { suggestions: { bad: true }, evidence: { bad: true } },
        signal_validation: { items: [{ status: null }, null, "bad"], notes: { bad: true } },
        timeframe_alignment: { timeframes: { bad: true }, suggestions: { bad: true } },
        risk_reward: { rating: null, scenarios: [{ name: null }, null], notes: { bad: true } },
        factor_lab: {
          top_positive: "bad",
          factors: [null, { calibration_buckets: { bad: true }, calibration: null, evidence: { bad: true } }],
          weight_policy: { bad: true },
          notes: { bad: true },
        },
        theme_context: { concepts: { bad: true }, opportunities: { bad: true }, risks: null },
        chip_analysis: { support_bands: { bad: true }, pressure_bands: null, notes: { bad: true } },
        leadership: { tags: { bad: true }, evidence: { bad: true }, missing_data: { bad: true } },
        replay: { pattern_stats: { bad: true }, cases: { bad: true } },
      }, { symbol: "600706.SH" });

      if (!element("alphaEvidence").innerHTML.includes("等待更多积极证据")) {
        throw new Error("malformed alpha collections did not render empty state");
      }
      if (!element("timeframeAlignment").innerHTML.includes("timeframe-grid")) {
        throw new Error("malformed timeframe collections stopped rendering");
      }
      if (!element("riskReward").innerHTML.includes("scenario-grid")) {
        throw new Error("malformed scenario collections stopped rendering");
      }
      if (!element("replayPanel").innerHTML.includes("replay-cases")) {
        throw new Error("malformed replay cases stopped rendering");
      }
    '''
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


def test_research_panel_failure_isolated_to_single_panel() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      const elements = new Map();
      function element(id) {
        if (!elements.has(id)) {
          elements.set(id, {
            id,
            innerHTML: "",
            textContent: "",
            value: "",
            isConnected: true,
            addEventListener() {},
            querySelector() {
              return null;
            },
            closest() {
              return null;
            },
            insertAdjacentHTML() {},
          });
        }
        return elements.get(id);
      }

      globalThis.document = {
        getElementById: element,
        querySelector() {
          return null;
        },
      };

      const workbench = {
        qa_report: { summary: "问诊", items: [] },
        risk_radar: { overall_level: "中性", items: [] },
        replay: {
          sample_count: 1,
          window_days: 120,
          summary: "历史回放仍应渲染",
          pattern_stats: [],
          cases: [],
        },
      };
      Object.defineProperty(workbench, "alpha_evidence", {
        get() {
          throw new Error("alpha payload broken");
        },
      });

      renderResearch(workbench, { symbol: "600706.SH" });

      if (!element("alphaEvidence").innerHTML.includes("Alpha证据链暂不可用")) {
        throw new Error(`alpha panel did not render isolated fallback: ${element("alphaEvidence").innerHTML}`);
      }
      if (!element("replayPanel").innerHTML.includes("历史回放仍应渲染")) {
        throw new Error(`later panels did not keep rendering: ${element("replayPanel").innerHTML}`);
      }
    '''
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


def _run_node_script(script: str) -> None:
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


def _js_functions(path: Path) -> list[dict[str, int | str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    functions: list[dict[str, int | str]] = []
    seen: set[tuple[int, int]] = set()
    for line_no, line in enumerate(lines):
        for pattern in JS_FUNCTION_PATTERNS:
            for match in pattern.finditer(line):
                key = (line_no, match.start())
                if key in seen:
                    continue
                seen.add(key)
                bounds = _js_function_bounds(lines, line_no, match.end())
                if not bounds:
                    continue
                start, end = bounds
                body = "\n".join(_strip_js_strings(item) for item in lines[line_no : end + 1])
                functions.append(
                    {
                        "path": str(path.relative_to(ROOT)),
                        "line": line_no + 1,
                        "name": match.group(1),
                        "lines": end - line_no + 1,
                        "branches": len(JS_BRANCH_RE.findall(body)),
                    }
                )
    return functions


def _js_function_bounds(lines: list[str], line_no: int, start_col: int) -> tuple[int, int] | None:
    for open_line in range(line_no, min(len(lines), line_no + 8)):
        open_col = lines[open_line].find("{", start_col if open_line == line_no else 0)
        if open_col >= 0:
            close_line = _matching_js_close_brace(lines, open_line, open_col)
            return (open_line, close_line) if close_line is not None else None
    return None


def _matching_js_close_brace(lines: list[str], open_line: int, open_col: int) -> int | None:
    depth = 0
    for line_no in range(open_line, len(lines)):
        text = _strip_js_strings(lines[line_no])
        start_col = open_col if line_no == open_line else 0
        for char in text[start_col:]:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return line_no
    return None


def _strip_js_strings(line: str) -> str:
    return JS_STRING_RE.sub("", line)
