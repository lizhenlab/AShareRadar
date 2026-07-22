from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_research_panel_facade_preserves_public_api_and_key_outputs() -> None:
    script = r'''
      import * as researchPanels from "./static/js/research-panels.js";
      import { runResearchPanelSmoke } from "./tests/static_research_smoke_helpers.mjs";

      const exportNames = Object.keys(researchPanels).sort();
      if (JSON.stringify(exportNames) !== JSON.stringify(["renderResearch"])) {
        throw new Error(`research panel public API changed: ${exportNames.join(", ")}`);
      }
      await runResearchPanelSmoke();
    '''
    _run_node_script(script)


def test_research_panel_modules_are_thin_acyclic_and_share_formatters() -> None:
    module_names = {
        "research-panels.js",
        "research-formatters.js",
        "research-risk-reward.js",
        "research-factor-diagnostics.js",
        "research-qa-reports.js",
        "research-render-utils.js",
    }
    sources = {
        name: (ROOT / "static" / "js" / name).read_text(encoding="utf-8")
        for name in module_names
    }
    import_pattern = re.compile(r'from\s+["\']\./([^"\']+)["\']')
    graph = {
        name: {dependency for dependency in import_pattern.findall(source) if dependency in module_names}
        for name, source in sources.items()
    }

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str) -> None:
        assert module not in visiting, f"cyclic research panel import at {module}"
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module_name in graph:
        visit(module_name)

    facade = sources["research-panels.js"]
    assert len(facade.splitlines()) <= 80
    assert graph["research-panels.js"] == {
        "research-factor-diagnostics.js",
        "research-qa-reports.js",
        "research-render-utils.js",
        "research-risk-reward.js",
    }
    for module_name in (
        "research-risk-reward.js",
        "research-factor-diagnostics.js",
        "research-qa-reports.js",
    ):
        assert "research-formatters.js" in graph[module_name]
        assert 'from "./format.js"' not in sources[module_name]
    assert 'from "./format.js"' in sources["research-formatters.js"]
    assert "aiQuestionRequestSeq" not in "\n".join(sources.values())


def test_factor_lab_renders_all_seven_production_factors_and_participation_note() -> None:
    script = r'''
      import { renderFactorLab } from "./static/js/research-factor-diagnostics.js";

      const target = { innerHTML: "" };
      globalThis.document = {
        getElementById(id) {
          return id === "factorLab" ? target : null;
        },
      };
      const factorSpecs = [
        ["trend_momentum", "趋势动量"],
        ["volume_confirmation", "量价确认"],
        ["risk_pressure", "风险压力"],
        ["fund_flow_proxy", "量价连续性（衍生）"],
        ["chip_position", "筹码位置"],
        ["leadership_strength", "龙头强度"],
        ["valuation_anchor", "估值锚"],
      ];
      const factors = factorSpecs.map(([id, name], index) => ({
        id,
        name,
        score: 60 + index,
        weight: 1,
        value: index === 6 ? "估值中性" : "观察",
        direction: "正向",
        evidence: index === 0 ? ["证据<script>"] : [],
        calibration: {
          sample_count: index === 6 ? 0 : 12,
          confidence_level: index === 6 ? "待补数据" : "中等",
          expected_level: index === 6 ? "待补数据" : "偏正",
          participates_in_historical_aggregate: index !== 6,
        },
      }));

      renderFactorLab({
        total_score: 66,
        evidence_sufficiency: 62,
        composite_reliability_level: "中等",
        profile_label: "常规个股",
        calibration_sample_count: 12,
        positive_factor_count: 3,
        negative_factor_count: 1,
        top_positive: ["趋势动量"],
        summary: "七因子生产报告",
        factors,
        weight_policy: ["默认权重"],
        notes: ["第一条说明", "第二条说明", "第三条保持紧凑", "估值锚不参与历史校准"],
      });

      const html = target.innerHTML;
      const renderedCount = (html.match(/class="standard-factor(?:\s|\")/g) || []).length;
      if (renderedCount !== 7) {
        throw new Error(`expected seven rendered factors, got ${renderedCount}: ${html}`);
      }
      for (const [, name] of factorSpecs) {
        if (!html.includes(name)) throw new Error(`production factor was hidden: ${name}`);
      }
      if (!html.includes("历史聚合口径：估值锚仍参与当前评分，但不参与综合证据充分度、正负证据与历史样本聚合")) {
        throw new Error(`explicit participation note was missing: ${html}`);
      }
      if (html.includes("第三条保持紧凑") || html.includes("<script>") || !html.includes("证据&lt;script&gt;")) {
        throw new Error(`factor rendering was not compact and escaped: ${html}`);
      }
    '''
    _run_node_script(script)


def test_ai_question_form_has_accessible_name_description_and_idle_state() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      const dom = installResearchPanelDom();
      renderResearch(workbench(), { symbol: "600519.SH" });

      const html = document.getElementById("aiDashboard").innerHTML;
      for (const marker of [
        '<label class="ai-visually-hidden" for="aiQuestionInput">个股问诊问题</label>',
        'aria-describedby="aiQuestionHelp"',
        'aria-errormessage="aiQuestionFeedback"',
        'id="aiQuestionHelp">请输入一个不超过120个字符的具体问题。</span>',
        'id="aiQuestionFeedback" hidden',
        'id="aiQuestionForm" aria-busy="false"',
      ]) {
        if (!html.includes(marker)) throw new Error(`missing accessible AI question markup: ${marker}`);
      }
      if (dom.form().getAttribute("aria-busy") !== "false" || dom.input().getAttribute("aria-invalid") !== "false") {
        throw new Error("AI question form did not begin in an accessible idle state");
      }
      if (html.includes("aria-live=")) throw new Error("idle AI question form should not contain a live announcement region");
    '''
    _run_node_script(script)


def test_blank_ai_question_reports_inline_error_focuses_input_and_clears_on_edit() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      const dom = installResearchPanelDom();
      let fetchCalls = 0;
      globalThis.fetch = async () => {
        fetchCalls += 1;
        return answerResponse("不应请求");
      };

      renderResearch(workbench(), { symbol: "600519.SH" });
      dom.input().value = "   ";
      await dom.form().listener.handler({ preventDefault() {}, currentTarget: dom.form() });

      if (fetchCalls !== 0) throw new Error("blank AI question issued a request");
      if (dom.feedback().hidden || dom.feedback().textContent !== "请输入要问的问题。") {
        throw new Error("blank AI question did not show the inline error");
      }
      if (dom.input().getAttribute("aria-invalid") !== "true" || document.activeElement !== dom.input()) {
        throw new Error("blank AI question did not expose invalid state and restore input focus");
      }
      if (dom.form().getAttribute("aria-busy") !== "false" || dom.button().disabled) {
        throw new Error("validation error incorrectly entered the request busy state");
      }

      dom.input().value = "风险在哪里？";
      dom.input().listener.handler({ currentTarget: dom.input() });
      if (!dom.feedback().hidden || dom.feedback().textContent || dom.input().getAttribute("aria-invalid") !== "false") {
        throw new Error("editing did not clear the AI question validation error");
      }
    '''
    _run_node_script(script)


def test_delayed_ai_answer_does_not_replace_rerendered_symbol_answer() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      const dom = installResearchPanelDom();
      const state = { symbol: "600519.SH" };
      const firstReply = deferredReply();
      const fetchCalls = [];
      globalThis.fetch = async (url, options = {}) => {
        fetchCalls.push({ url, options });
        return firstReply.promise;
      };

      renderResearch(workbench(), state);
      dom.input().value = "旧个股问题";
      const pendingSubmit = dom.form().listener.handler({ preventDefault() {}, currentTarget: dom.form() });

      if (!dom.button().disabled || dom.button().textContent !== "分析中" || dom.form().getAttribute("aria-busy") !== "true") {
        throw new Error("submit did not enter busy state");
      }
      state.symbol = "000001.SZ";
      renderResearch(workbench({ answer: "新个股已存在回答" }), state);
      const newAnswerBeforeOldReply = dom.answerHtml();
      firstReply.resolve(answerResponse("旧请求回答"));
      await pendingSubmit;

      if (fetchCalls.length !== 1) {
        throw new Error(`expected one ask request, got ${fetchCalls.length}`);
      }
      if (!fetchCalls[0].options.signal || !fetchCalls[0].options.signal.aborted) {
        throw new Error("rerender did not abort the stale AI question fetch");
      }
      const body = JSON.parse(fetchCalls[0].options.body);
      if (body.symbol !== "600519.SH" || body.question !== "旧个股问题") {
        throw new Error(`request did not capture original symbol/question: ${fetchCalls[0].options.body}`);
      }
      if (!newAnswerBeforeOldReply.includes("新个股已存在回答")) {
        throw new Error("rerendered symbol answer was not present before stale reply");
      }
      if (!dom.answerHtml().includes("新个股已存在回答")) {
        throw new Error("stale reply removed the rerendered symbol answer");
      }
      if (dom.answerHtml().includes("旧请求回答") || dom.insertedHtml.some((html) => html.includes("旧请求回答"))) {
        throw new Error("stale reply was inserted after symbol rerender");
      }
      if (dom.removedAnswers !== 0) {
        throw new Error("stale reply removed the current answer");
      }
      if (dom.button().disabled || dom.button().textContent !== "问一下" || dom.form().getAttribute("aria-busy") !== "false") {
        throw new Error("current rerendered button should remain idle");
      }
    '''
    _run_node_script(script)


def test_ai_question_submit_ignores_duplicate_submit_while_pending() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      const dom = installResearchPanelDom();
      const state = { symbol: "600519.SH" };
      const firstReply = deferredReply();
      let fetchCalls = 0;
      globalThis.fetch = async () => {
        fetchCalls += 1;
        return firstReply.promise;
      };

      renderResearch(workbench(), state);
      dom.input().value = "第一问";
      const firstSubmit = dom.form().listener.handler({ preventDefault() {}, currentTarget: dom.form() });
      dom.input().value = "第二问";
      const secondSubmit = dom.form().listener.handler({ preventDefault() {}, currentTarget: dom.form() });
      await secondSubmit;

      if (fetchCalls !== 1) {
        throw new Error(`duplicate submit should not issue another request, got ${fetchCalls}`);
      }
      if (!dom.button().disabled || dom.button().textContent !== "分析中") {
        throw new Error("button should stay busy while the first request is pending");
      }
      firstReply.resolve(answerResponse("第一问回答"));
      await firstSubmit;

      if (!dom.answerHtml().includes("第一问回答")) {
        throw new Error("first request answer was not inserted after duplicate submit was ignored");
      }
      if (!dom.answerHtml().includes('role="status" aria-live="polite" aria-atomic="true"')) {
        throw new Error("successful AI answer was not announced as a single polite status");
      }
      if ((dom.answerHtml().match(/aria-live=/g) || []).length !== 1) {
        throw new Error("successful AI answer contains duplicate live regions");
      }
      if (dom.button().disabled || dom.button().textContent !== "问一下" || dom.form().getAttribute("aria-busy") !== "false") {
        throw new Error("button did not recover after first request");
      }
    '''
    _run_node_script(script)


def test_current_ai_question_error_still_renders() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      const dom = installResearchPanelDom();
      const state = { symbol: "600519.SH" };
      globalThis.fetch = async () => ({
        ok: false,
        async json() {
          return { detail: "问诊暂不可用<script>" };
        },
      });

      renderResearch(workbench(), state);
      dom.input().value = "错误展示";
      await dom.form().listener.handler({ preventDefault() {}, currentTarget: dom.form() });

      if (!dom.answerHtml().includes("问诊暂不可用&lt;script&gt;") || dom.answerHtml().includes("问诊暂不可用<script>")) {
        throw new Error("current request error was not rendered and escaped");
      }
      if (!dom.answerHtml().includes('role="alert" aria-live="assertive" aria-atomic="true"')) {
        throw new Error("AI request failure was not announced as an assertive alert");
      }
      if ((dom.answerHtml().match(/aria-live=/g) || []).length !== 1) {
        throw new Error("AI request failure contains duplicate live regions");
      }
      if (dom.button().disabled || dom.button().textContent !== "问一下") {
        throw new Error("button did not recover after error");
      }
    '''
    _run_node_script(script)


def test_ai_question_timeout_allows_llm_correction_and_recovers_current_form() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      const dom = installResearchPanelDom();
      const state = { symbol: "600519.SH" };
      const delays = [];
      let fetchSignal = null;
      globalThis.setTimeout = (callback, delay) => {
        delays.push(delay);
        queueMicrotask(callback);
        return delays.length;
      };
      globalThis.clearTimeout = () => {};
      globalThis.fetch = async (url, options = {}) => {
        fetchSignal = options.signal;
        return new Promise(() => {});
      };

      renderResearch(workbench(), state);
      dom.input().value = "超时测试";
      await dom.form().listener.handler({ preventDefault() {}, currentTarget: dom.form() });

      if (delays.length !== 1 || delays[0] !== 35000) {
        throw new Error(`AI question timeout did not allow the server correction budget: ${delays}`);
      }
      if (!fetchSignal || !fetchSignal.aborted) {
        throw new Error("timed out AI question did not abort its fetch signal");
      }
      if (!dom.answerHtml().includes("请求超时，请稍后重试")) {
        throw new Error(`current AI timeout was not rendered: ${dom.answerHtml()}`);
      }
      if (dom.button().disabled || dom.button().textContent !== "问一下") {
        throw new Error("AI question form did not recover after timeout");
      }
    '''
    _run_node_script(script)


def test_concept_strip_uses_neutral_class_for_zero_and_missing_change() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      installResearchPanelDom();
      renderResearch({
        ...workbench(),
        theme_context: {
          level: "中性",
          score: 50,
          style: "观察",
          relative_strength: "持平",
          industry: "测试行业",
          industry_change_pct: 0,
          summary: "概念中性",
          concepts: [
            { name: "零涨跌", change_pct: 0, match_reason: "持平" },
            { name: "缺失涨跌", source: "测试源" },
          ],
          opportunities: [],
          risks: [],
          evidence: [],
          missing_data: [],
        },
      }, { symbol: "600519.SH" });

      const html = document.getElementById("themePanel").innerHTML;
      const neutralCount = (html.match(/class="neutral"/g) || []).length;
      if (neutralCount !== 2) {
        throw new Error(`expected both concepts to be neutral: ${html}`);
      }
      if (html.includes('class="up-bg"') || html.includes('class="down-bg"')) {
        throw new Error(`neutral concepts received directional classes: ${html}`);
      }
    '''
    _run_node_script(script)


def test_feature_snapshot_and_timeframe_unknown_fields_use_neutral_placeholders() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      installResearchPanelDom();
      renderResearch({
        ...workbench(),
        feature_snapshot: { tags: [] },
        timeframe_alignment: {
          alignment_label: "待确认",
          alignment_score: 50,
          conflict_level: "中性",
          summary: "周期数据待确认",
          timeframes: [
            {
              name: "短线",
              score: 50,
              label: "观察",
              window_days: 5,
              return_pct: 0,
              max_drawdown_pct: 0,
            },
          ],
          suggestions: [],
        },
      }, { symbol: "600519.SH" });

      const featureHtml = document.getElementById("featureSnapshot").innerHTML;
      const timeframeHtml = document.getElementById("timeframeAlignment").innerHTML;
      const combined = `${featureHtml}${timeframeHtml}`;
      if (combined.includes("undefined")) {
        throw new Error(`research placeholders leaked undefined: ${combined}`);
      }
      if (!featureHtml.includes("--")) {
        throw new Error(`missing feature fields did not render placeholders: ${featureHtml}`);
      }
      if (timeframeHtml.includes("低于均线") || !timeframeHtml.includes("均线关系待确认")) {
        throw new Error(`unknown MA relation was rendered directionally: ${timeframeHtml}`);
      }
    '''
    _run_node_script(script)


def test_research_render_utils_escape_limits_missing_data_and_tones() -> None:
    script = r'''
      import {
        renderInlineItems,
        renderMetricPairs,
        renderMissingData,
        signedText,
        thresholdClass,
      } from "./static/js/research-render-utils.js";

      const inline = renderInlineItems(["一<script>", "二", "三"], "span", 2, "risk");
      if (inline !== '<span class="risk">一&lt;script&gt;</span><span class="risk">二</span>') {
        throw new Error(`inline items were not escaped and limited: ${inline}`);
      }

      const missing = renderMissingData(["机构<script>", "现金流"]);
      if (missing !== "<small>待补：机构&lt;script&gt;、现金流</small>") {
        throw new Error(`missing data fallback changed: ${missing}`);
      }

      const alphaMissing = renderMissingData(["机构<script>"], { tagName: "em", prefix: "待补数据：" });
      if (alphaMissing !== "<em>待补数据：机构&lt;script&gt;</em>") {
        throw new Error(`custom missing data fallback changed: ${alphaMissing}`);
      }

      const metrics = renderMetricPairs([["收益<script>", "1<2"]]);
      if (metrics !== "<span>收益&lt;script&gt; <b>1&lt;2</b></span>") {
        throw new Error(`metric pairs were not escaped: ${metrics}`);
      }

      if (signedText(3) !== "+3" || signedText(-2) !== "-2" || signedText(null) !== "") {
        throw new Error("signed text formatting changed");
      }
      if (thresholdClass(70, { goodAt: 62, riskAt: 45 }) !== "good") {
        throw new Error("higher-good threshold did not return good");
      }
      if (thresholdClass(70, { higherIsRisk: true, riskAt: 68, goodAt: 35 }) !== "risk") {
        throw new Error("higher-risk threshold did not return risk");
      }
      if (thresholdClass("bad", { goodAt: 62, riskAt: 45 }) !== "") {
        throw new Error("invalid threshold input should be neutral");
      }
      if (thresholdClass(70) !== "" || renderInlineItems({ bad: "shape" }, "span", 2) !== "") {
        throw new Error("research render helpers should tolerate missing options and non-array inputs");
      }
      const malformedMetrics = renderMetricPairs(["坏格式<script>"]);
      if (malformedMetrics !== "<span>坏格式&lt;script&gt; <b></b></span>") {
        throw new Error(`malformed metric pair was not safely escaped: ${malformedMetrics}`);
      }
    '''
    _run_node_script(script)


def _run_node_script(test_body: str) -> None:
    script = f"{test_body}\n{FAKE_DOM_SCRIPT}"
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)


FAKE_DOM_SCRIPT = r'''
function deferredReply() {
  let resolve;
  const promise = new Promise((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function answerResponse(answer) {
  return {
    ok: true,
    async json() {
      return {
        question: "测试问题",
        answer,
        topic: "测试",
        confidence: 70,
        evidence: [],
        actions: [],
        invalidations: [],
        related_questions: [],
      };
    },
  };
}

function workbench({ answer = null } = {}) {
  return {
    qa_report: { summary: "规则问诊", items: [] },
    question_answer: answer
      ? {
          question: "新个股问题",
          answer,
          topic: "测试",
          confidence: 80,
          evidence: [],
          actions: [],
          invalidations: [],
          related_questions: [],
        }
      : null,
    risk_radar: { overall_level: "中性", summary: "", items: [] },
    evidence_chain: { summary: "", support: [], opposition: [], invalidations: [] },
    event_digest: { summary: "", negative_events: [], positive_events: [], watch_events: [] },
    peer_comparison: { summary: "", industry: "", sample_count: 0, metrics: [] },
    t_strategy: { summary: "", low_zone: "", high_zone: "", stop_conditions: [] },
  };
}

function installResearchPanelDom() {
  const elements = new Map();
  const insertedHtml = [];
  let currentForm = null;
  let currentInput = null;
  let currentButton = null;
  let currentAnswer = null;
  let removedAnswers = 0;

  class BaseElement {
    constructor(id) {
      this.id = id;
      this._innerHTML = "";
      this.onclick = null;
      this.value = "";
      this.disabled = false;
      this.textContent = "";
      this.isConnected = true;
      this.hidden = false;
      this.attributes = new Map();
    }

    get innerHTML() {
      return this._innerHTML;
    }

    set innerHTML(value) {
      this._innerHTML = value;
    }

    addEventListener(type, handler) {
      this.listener = { type, handler };
    }

    setAttribute(name, value) {
      this.attributes.set(name, String(value));
    }

    getAttribute(name) {
      return this.attributes.get(name) ?? null;
    }

    focus() {
      document.activeElement = this;
    }

    querySelector() {
      return null;
    }

    closest() {
      return null;
    }
  }

  class DashboardElement extends BaseElement {
    set innerHTML(value) {
      this._innerHTML = value;
      if (currentForm) currentForm.isConnected = false;
      currentInput = new InputElement();
      currentButton = new ButtonElement();
      currentForm = new FormElement(this);
      const feedback = new BaseElement("aiQuestionFeedback");
      feedback.hidden = true;
      elements.set("aiQuestionForm", currentForm);
      elements.set("aiQuestionInput", currentInput);
      elements.set("aiQuestionFeedback", feedback);
      currentAnswer = value.includes("ai-card-wide") ? new AnswerElement(value) : null;
    }

    get innerHTML() {
      return this._innerHTML;
    }

    querySelector(selector) {
      if (selector === ".ai-card-wide") return currentAnswer;
      return null;
    }
  }

  class FormElement extends BaseElement {
    constructor(dashboard) {
      super("aiQuestionForm");
      this.dashboard = dashboard;
      this.setAttribute("aria-busy", "false");
    }

    querySelector(selector) {
      if (selector === "button") return currentButton;
      return null;
    }

    closest(selector) {
      return selector === "#aiDashboard" ? this.dashboard : null;
    }

    insertAdjacentHTML(position, html) {
      insertedHtml.push(html);
      currentAnswer = new AnswerElement(html);
    }

    requestSubmit() {
      if (this.listener) this.listener.handler({ preventDefault() {}, currentTarget: this });
    }
  }

  class InputElement extends BaseElement {
    constructor() {
      super("aiQuestionInput");
      this.setAttribute("aria-invalid", "false");
    }
  }

  class ButtonElement extends BaseElement {
    constructor() {
      super("aiQuestionButton");
      this.textContent = "问一下";
    }
  }

  class AnswerElement extends BaseElement {
    constructor(html) {
      super("answer");
      this._innerHTML = html;
    }

    remove() {
      removedAnswers += 1;
      currentAnswer = null;
    }
  }

  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, id === "aiDashboard" ? new DashboardElement(id) : new BaseElement(id));
    }
    return elements.get(id);
  }

  globalThis.document = {
    activeElement: null,
    getElementById: element,
    querySelector(selector) {
      if (selector === ".ai-card-wide") return currentAnswer;
      return null;
    },
    createElement() {
      return { innerHTML: "", firstElementChild: null };
    },
  };

  return {
    insertedHtml,
    get removedAnswers() {
      return removedAnswers;
    },
    form: () => currentForm,
    input: () => currentInput,
    button: () => currentButton,
    feedback: () => elements.get("aiQuestionFeedback"),
    answerHtml: () => (currentAnswer ? currentAnswer.innerHTML : ""),
  };
}
'''
