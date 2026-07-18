import { renderResearch } from "../static/js/research-panels.js";

export async function runResearchPanelSmoke() {
  const { element } = installStaticAssetDom();
  renderResearch(researchSmokeWorkbench(), { symbol: "600519" });

  const aiHtml = element("aiDashboard").innerHTML;
  const themeHtml = element("themePanel").innerHTML;
  assert(
    aiHtml.includes("本次问诊")
      && aiHtml.includes("&lt;script&gt;")
      && aiHtml.includes("回答可靠度 66/100")
      && !aiHtml.includes("置信度"),
    "AI dashboard did not render escaped question answer content with score semantics",
  );
  assert(aiHtml.includes("同行源&lt;script&gt;暂不可用") && !aiHtml.includes("同行源<script>"), "Peer source warning was not rendered and escaped");
  assert(!themeHtml.includes("<script>") && themeHtml.includes("&lt;script&gt;"), "Theme panel did not escape concept content");

  const factorHtml = element("factorLab").innerHTML;
  assert(
    factorHtml.includes("历史分位 72.5%")
      && factorHtml.includes("证据充分度 41/100")
      && factorHtml.includes("综合可信等级 较低")
      && !factorHtml.includes("校准置信")
      && !factorHtml.includes("低置信参考")
      && factorHtml.includes("证据充分度较低的参考")
      && factorHtml.includes("&lt;script&gt;")
      && factorHtml.includes("权重规则"),
    "Factor lab did not render escaped calibrated factor content",
  );

  const regimeHtml = element("marketRegime").innerHTML;
  assert(
    regimeHtml.includes('class="risk"')
      && regimeHtml.includes("证据充分度修正 +5")
      && regimeHtml.includes("证据充分度 40/100")
      && regimeHtml.includes("量价热度（衍生） 55 分")
      && !regimeHtml.includes("校准置信度")
      && regimeHtml.includes("先降仓"),
    "Market regime did not render risk tone, signed adjustment, and suggestions",
  );

  const alphaHtml = element("alphaEvidence").innerHTML;
  assert(
    alphaHtml.includes("Alpha证据充分度 60/100")
      && alphaHtml.includes("业绩改善&lt;script&gt; +4")
      && alphaHtml.includes("估值压力&lt;script&gt; -2")
      && alphaHtml.includes("待补数据：机构持仓&lt;script&gt;、现金流")
      && !alphaHtml.includes("置信度"),
    "Alpha evidence did not render signed, escaped evidence and missing data",
  );

  const featureHtml = element("featureSnapshot").innerHTML;
  const diagnosisHtml = element("diagnosisPanel").innerHTML;
  const validationHtml = element("signalValidation").innerHTML;
  assert(
    featureHtml.includes("60 · 震荡")
      && featureHtml.includes("1.20倍")
      && diagnosisHtml.includes("等待确认")
      && diagnosisHtml.includes("观察 · 诊断证据充分度 60/100")
      && diagnosisHtml.includes("证据充分度 40/100")
      && diagnosisHtml.includes("量价热度评分（衍生口径）")
      && !diagnosisHtml.includes("校准置信度")
      && validationHtml.includes("待确认")
      && validationHtml.includes("1项")
      && validationHtml.includes("验证强度 58/100")
      && !validationHtml.includes("置信度"),
    "Feature, diagnosis, or validation panel output changed",
  );

  const timeframeHtml = element("timeframeAlignment").innerHTML;
  assert(
    timeframeHtml.includes('class="risk"')
      && timeframeHtml.includes("短线&lt;script&gt;")
      && !timeframeHtml.includes("第四条不显示")
      && !timeframeHtml.includes("<script>"),
    "Timeframe alignment did not render conflict tone, limited suggestions, and escaped rows",
  );

  const riskRewardHtml = element("riskReward").innerHTML;
  assert(
    riskRewardHtml.includes('class="risk"')
      && riskRewardHtml.includes("防守情景&lt;script&gt;")
      && riskRewardHtml.includes("规则情景权重 56/100")
      && riskRewardHtml.includes("规则情景权重 30/100")
      && !riskRewardHtml.includes("55%")
      && riskRewardHtml.includes("收益风险比 0.80")
      && !riskRewardHtml.includes("第二条不显示"),
    "Risk/reward panel did not render risk tone, scenarios, metrics, and limited notes",
  );

  const chipHtml = element("chipPanel").innerHTML;
  const leadershipHtml = element("leadershipPanel").innerHTML;
  assert(
    chipHtml.includes("均衡 · 一般")
      && chipHtml.includes("成本中枢 10.00")
      && leadershipHtml.includes("40 · 普通"),
    "Chip analysis or leadership panel output changed",
  );

  const replayHtml = element("replayPanel").innerHTML;
  assert(
    replayHtml.includes("样本有效率 66.7%") && replayHtml.includes("5日 --"),
    "Replay panel did not render formatted stats and pending returns",
  );
}

export async function runAiQuestionSubmitSmoke() {
  const fetchCalls = [];
  const { element, insertedHtml } = installStaticAssetDom();
  globalThis.fetch = async (url, options = {}) => {
    fetchCalls.push({ url, options });
    return jsonResponse({
      question: "风险在哪里？",
      answer: "风险已识别<script>",
      topic: "风险",
      confidence: 61,
      evidence: ["风险证据"],
      actions: ["降仓"],
      invalidations: ["站回压力"],
      related_questions: [],
    });
  };

  renderResearch(researchSmokeWorkbench(), { symbol: "600519" });
  assert(element("aiQuestionForm").listener, "AI question form listener was not registered");
  element("aiQuestionInput").value = "风险在哪里？";
  await element("aiQuestionForm").listener.handler({ preventDefault() {} });

  const button = element("aiQuestionForm-button");
  assert(!button.disabled && button.textContent === "问一下", "AI question button did not recover after submit");
  assert(fetchCalls.length === 1 && fetchCalls[0].url.endsWith("/api/stock/ask"), "AI question request was not sent");

  const body = JSON.parse(fetchCalls[0].options.body);
  assert(body.symbol === "600519" && body.question === "风险在哪里？", `AI question request body was wrong: ${fetchCalls[0].options.body}`);
  const answerHtml = insertedHtml.at(-1).html;
  assert(
    answerHtml.includes("风险已识别&lt;script&gt;")
      && answerHtml.includes("回答可靠度 61/100")
      && !answerHtml.includes("置信度"),
    "AI question answer was not inserted, escaped, or labeled with score semantics",
  );
}

function installStaticAssetDom() {
  const elements = new Map();
  const insertedHtml = [];

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
  return { element, insertedHtml };
}

function researchSmokeWorkbench() {
  return {
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
    peer_comparison: { summary: "同行", industry: "白酒", sample_count: 3, metrics: [], warnings: ["同行源<script>暂不可用"] },
    t_strategy: { summary: "做T", low_zone: "10", high_zone: "12", stop_conditions: [] },
    feature_snapshot: {
      trend_score: 60,
      trend_label: "震荡",
      fund_flow_score: 55,
      fund_flow_data_nature: "derived",
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
      professional_summary: "因子证据充分度 40/100，量价热度（衍生） 55 分。",
      confirmation_signals: ["量价热度评分（衍生口径）维持在 60 分以上。"],
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
      evidence: ["证据充分度 40/100，量价热度（衍生） 55 分。"],
    },
    signal_validation: {
      overall_status: "待确认",
      summary: "验证",
      items: [{
        name: "突破验证",
        category: "价格",
        status: "待确认",
        confidence: 58,
        trigger_condition: "接近压力",
        confirmation_condition: "放量突破",
        invalidation_condition: "跌回平台",
        historical_reference: "历史样本仅作参考",
      }],
      notes: [],
    },
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
        { name: "防守情景<script>", probability: 55, rule_weight: 56, trigger: "跌破支撑<script>", expected_move: "-6%", response: "降仓<script>", invalidation: "收回支撑<script>" },
        { name: "积极情景", probability: 30, trigger: "突破压力", expected_move: "+8%", response: "小仓跟随", invalidation: "跌回平台" },
      ],
      notes: ["仓位要轻<script>", "第二条不显示"],
    },
    factor_lab: {
      total_score: 55,
      calibrated_confidence: 40,
      evidence_sufficiency: 41,
      composite_reliability_level: "较低",
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
      notes: ["样本较少，只作证据充分度较低的参考。"],
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
}

function jsonResponse(payload) {
  return {
    ok: true,
    async json() {
      return payload;
    },
  };
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
