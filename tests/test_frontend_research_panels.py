from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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

      if (!dom.button().disabled || dom.button().textContent !== "分析中") {
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
      if (dom.button().disabled || dom.button().textContent !== "问一下") {
        throw new Error("current rerendered button should remain idle");
      }
    '''
    _run_node_script(script)


def test_older_ai_request_does_not_remove_latest_answer_on_same_form() -> None:
    script = r'''
      import { renderResearch } from "./static/js/research-panels.js";

      const dom = installResearchPanelDom();
      const state = { symbol: "600519.SH" };
      const firstReply = deferredReply();
      const secondReply = deferredReply();
      const replies = [firstReply, secondReply];
      globalThis.fetch = async () => replies.shift().promise;

      renderResearch(workbench(), state);
      dom.input().value = "第一问";
      const firstSubmit = dom.form().listener.handler({ preventDefault() {}, currentTarget: dom.form() });
      dom.input().value = "第二问";
      const secondSubmit = dom.form().listener.handler({ preventDefault() {}, currentTarget: dom.form() });

      secondReply.resolve(answerResponse("最新回答"));
      await secondSubmit;
      const latestAnswer = dom.answerHtml();
      if (!latestAnswer.includes("最新回答")) {
        throw new Error("latest answer was not inserted");
      }
      firstReply.resolve(answerResponse("旧回答"));
      await firstSubmit;

      if (!dom.answerHtml().includes("最新回答")) {
        throw new Error("older request removed latest answer");
      }
      if (dom.answerHtml().includes("旧回答") || dom.insertedHtml.some((html) => html.includes("旧回答"))) {
        throw new Error("older request was inserted after latest answer");
      }
      if (dom.button().disabled || dom.button().textContent !== "问一下") {
        throw new Error("button did not recover after latest request");
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
          return { detail: "问诊暂不可用" };
        },
      });

      renderResearch(workbench(), state);
      dom.input().value = "错误展示";
      await dom.form().listener.handler({ preventDefault() {}, currentTarget: dom.form() });

      if (!dom.answerHtml().includes("问诊暂不可用")) {
        throw new Error("current request error was not rendered");
      }
      if (dom.button().disabled || dom.button().textContent !== "问一下") {
        throw new Error("button did not recover after error");
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
      elements.set("aiQuestionForm", currentForm);
      elements.set("aiQuestionInput", currentInput);
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
    answerHtml: () => (currentAnswer ? currentAnswer.innerHTML : ""),
  };
}
'''
