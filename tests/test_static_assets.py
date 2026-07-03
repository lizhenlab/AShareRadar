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


def test_research_panel_renderer_runs_with_fake_dom() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      const elements = new Map();
      const insertedHtml = [];
      const fetchCalls = [];
      function element(id) {
        if (!elements.has(id)) {
          elements.set(id, {
            id,
            innerHTML: "",
            onclick: null,
            value: "",
            disabled: false,
            textContent: "问一下",
            addEventListener(type, handler) {
              this.listener = { type, handler };
            },
            querySelector(selector) {
              if (selector === "button") return element(`${id}-button`);
              return null;
            },
            insertAdjacentHTML(position, html) {
              insertedHtml.push({ position, html });
            },
            requestSubmit() {
              if (this.listener) this.listener.handler({ preventDefault() {} });
            },
          });
        }
        return elements.get(id);
      }

      globalThis.document = {
        getElementById: element,
        querySelector() {
          return null;
        },
        createElement() {
          return { innerHTML: "", firstElementChild: null };
        },
      };
      globalThis.fetch = async (url, options = {}) => {
        fetchCalls.push({ url, options });
        return {
          ok: true,
          async json() {
            return {
              question: "风险在哪里？",
              answer: "风险已识别<script>",
              topic: "风险",
              confidence: 61,
              evidence: ["风险证据"],
              actions: ["降仓"],
              invalidations: ["站回压力"],
              related_questions: [],
            };
          },
        };
      };

      const workbench = {
        qa_report: { summary: "规则问诊", items: [] },
        question_answer: {
          question: "能买<script>吗？",
          answer: "先等确认",
          topic: "buy",
          confidence: 66,
          llm_used: true,
          llm_status: "已增强",
          evidence: ["证据<script>"],
          actions: ["只观察"],
          invalidations: ["跌破支撑"],
          related_questions: ["风险<script>在哪里？"],
        },
        evidence_chain: { summary: "证据", support: [], opposition: [], invalidations: [] },
        risk_radar: { overall_level: "中性", summary: "风险", items: [] },
        event_digest: { summary: "事件", negative_events: [], positive_events: [], watch_events: [] },
        peer_comparison: { summary: "同行", industry: "白酒", sample_count: 3, metrics: [] },
        t_strategy: { summary: "做T", low_zone: "10", high_zone: "12", stop_conditions: [] },
        feature_snapshot: {
          trend_score: 60,
          trend_label: "震荡",
          fund_flow_score: 55,
          leader_score: 40,
          leader_level: "普通",
          volume_ratio: 1.2,
          valuation_score: 57,
          data_quality_level: "优秀",
          data_quality_score: 90,
          tags: ["测试"],
        },
        diagnosis: {
          headline: "等待确认",
          action: "观察",
          confidence: 60,
          beginner_summary: "摘要",
          professional_summary: "专业摘要",
          confirmation_signals: [],
          hard_risks: [],
        },
        alpha_evidence: {
          verdict: "偏强<script>",
          confidence: 60,
          summary: "Alpha<script>",
          positives: [{ title: "业绩改善<script>", impact: 4, reason: "盈利修复<script>" }],
          negatives: [{ title: "估值压力<script>", impact: -2, reason: "接近历史高位<script>" }],
          missing_data: ["机构持仓<script>", "现金流"],
        },
        market_regime: {
          market_label: "风险环境",
          stock_state: "风险优先",
          risk_multiplier: 1.2,
          industry_label: "行业震荡",
          breadth_label: "中性",
          breadth_score: 50,
          confidence_adjustment: 5,
          suggestions: ["先降仓"],
          evidence: ["风险证据"],
        },
        signal_validation: { overall_status: "待确认", summary: "验证", items: [], notes: [] },
        timeframe_alignment: {
          conflict_level: "短线冲突<script>",
          alignment_label: "偏弱分歧",
          alignment_score: 42,
          summary: "周期<script>",
          timeframes: [
            { name: "短线<script>", score: 38, label: "偏弱", window_days: 20, return_pct: -3.2, max_drawdown_pct: -6.4, above_ma: false, ma_value: 10.2 },
            { name: "中线", score: 68, label: "共振", window_days: 60, return_pct: 8.1, max_drawdown_pct: -2.4, above_ma: true, ma_value: 11.2 },
          ],
          suggestions: ["先等短线修复<script>", "只保留底仓", "第三条", "第四条不显示"],
        },
        risk_reward: {
          rating: "风险不足<script>",
          reward_risk_ratio: 0.8,
          current_price: 10,
          upside_target: 12,
          upside_pct: 20,
          downside_stop: 9,
          downside_pct: -10,
          atr_pct: 2,
          volatility_pct: 3,
          summary: "收益风险<script>",
          scenarios: [
            { name: "防守情景<script>", probability: 55, trigger: "跌破支撑<script>", expected_move: "-6%", response: "降仓<script>", invalidation: "收回支撑<script>" },
            { name: "积极情景", probability: 30, trigger: "突破压力", expected_move: "+8%", response: "小仓跟随", invalidation: "跌回平台" },
          ],
          notes: ["仓位要轻<script>", "第二条不显示"],
        },
        factor_lab: {
          total_score: 55,
          calibrated_confidence: 40,
          top_positive: ["趋势因子<script>"],
          profile_label: "常规",
          calibration_sample_count: 0,
          positive_factor_count: 0,
          negative_factor_count: 0,
          summary: "因子",
          factors: [{
            name: "趋势因子<script>",
            score: 66,
            weight: 1.2,
            value: "偏强",
            direction: "正向",
            percentile: 72.5,
            calibration: {
              sample_count: 8,
              confidence_level: "中等",
              expected_level: "偏正",
              win_rate: 62.5,
              avg_forward_5d_return: 1.2,
              max_adverse_return: -2.3,
            },
            calibration_buckets: [{ name: "强趋势", sample_count: 5, avg_forward_5d_return: 1.1 }],
            evidence: ["因子证据"],
          }],
          weight_policy: ["权重规则"],
          notes: ["因子备注"],
        },
        theme_context: {
          level: "中性",
          score: 50,
          style: "观察",
          relative_strength: "待确认",
          industry: "白酒<script>",
          industry_change_pct: 1.23,
          summary: "主题",
          concepts: [{ name: "高端<script>", change_pct: 2.5, leading_stock: "龙头<script>", match_reason: "匹配" }],
          opportunities: ["机会"],
          risks: ["风险"],
          evidence: ["证据"],
          missing_data: [],
        },
        chip_analysis: { distribution_label: "均衡", concentration: "一般", center_price: 10, summary: "筹码", support_bands: [], pressure_bands: [], notes: [] },
        leadership: { score: 40, level: "普通", summary: "龙头", tags: [], evidence: [], missing_data: [] },
        replay: {
          sample_count: 6,
          window_days: 120,
          success_rate: 66.7,
          summary: "复盘",
          pattern_stats: [{ pattern: "放量突破<script>", sample_count: 6, win_rate: 66.7, avg_forward_5d_return: 1.8, note: "偏正" }],
          cases: [{ date: "2026-05-01", pattern: "放量突破", outcome: "有效", forward_3d_return: 1.2, forward_5d_return: null }],
        },
      };

      renderResearch(workbench, { symbol: "600519" });
      const aiHtml = element("aiDashboard").innerHTML;
      const themeHtml = element("themePanel").innerHTML;
      if (!aiHtml.includes("本次问诊") || !aiHtml.includes("&lt;script&gt;")) {
        throw new Error("AI dashboard did not render escaped question answer content");
      }
      if (themeHtml.includes("<script>") || !themeHtml.includes("&lt;script&gt;")) {
        throw new Error("Theme panel did not escape concept content");
      }
      const factorHtml = element("factorLab").innerHTML;
      if (!factorHtml.includes("历史分位 72.5%") || !factorHtml.includes("&lt;script&gt;") || !factorHtml.includes("权重规则")) {
        throw new Error("Factor lab did not render escaped calibrated factor content");
      }
      const regimeHtml = element("marketRegime").innerHTML;
      if (!regimeHtml.includes('class="risk"') || !regimeHtml.includes("置信修正 +5") || !regimeHtml.includes("先降仓")) {
        throw new Error("Market regime did not render risk tone, signed adjustment, and suggestions");
      }
      const alphaHtml = element("alphaEvidence").innerHTML;
      if (!alphaHtml.includes("业绩改善&lt;script&gt; +4") || !alphaHtml.includes("估值压力&lt;script&gt; -2") || !alphaHtml.includes("待补数据：机构持仓&lt;script&gt;、现金流")) {
        throw new Error("Alpha evidence did not render signed, escaped evidence and missing data");
      }
      const timeframeHtml = element("timeframeAlignment").innerHTML;
      if (!timeframeHtml.includes('class="risk"') || !timeframeHtml.includes("短线&lt;script&gt;") || timeframeHtml.includes("第四条不显示") || timeframeHtml.includes("<script>")) {
        throw new Error("Timeframe alignment did not render conflict tone, limited suggestions, and escaped rows");
      }
      const riskRewardHtml = element("riskReward").innerHTML;
      if (!riskRewardHtml.includes('class="risk"') || !riskRewardHtml.includes("防守情景&lt;script&gt;") || !riskRewardHtml.includes("收益风险比 0.80") || riskRewardHtml.includes("第二条不显示")) {
        throw new Error("Risk/reward panel did not render risk tone, scenarios, metrics, and limited notes");
      }
      if (!element("aiQuestionForm").listener) {
        throw new Error("AI question form listener was not registered");
      }
      element("aiQuestionInput").value = "风险在哪里？";
      await element("aiQuestionForm").listener.handler({ preventDefault() {} });
      const button = element("aiQuestionForm-button");
      if (button.disabled || button.textContent !== "问一下") {
        throw new Error("AI question button did not recover after submit");
      }
      if (fetchCalls.length !== 1 || !fetchCalls[0].url.endsWith("/api/stock/ask")) {
        throw new Error("AI question request was not sent");
      }
      const body = JSON.parse(fetchCalls[0].options.body);
      if (body.symbol !== "600519" || body.question !== "风险在哪里？") {
        throw new Error(`AI question request body was wrong: ${fetchCalls[0].options.body}`);
      }
      if (!insertedHtml.at(-1).html.includes("风险已识别&lt;script&gt;")) {
        throw new Error("AI question answer was not inserted and escaped");
      }
      const replayHtml = element("replayPanel").innerHTML;
      if (!replayHtml.includes("样本有效率 66.7%") || !replayHtml.includes("5日 --")) {
        throw new Error("Replay panel did not render formatted stats and pending returns");
      }
    '''

    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


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
