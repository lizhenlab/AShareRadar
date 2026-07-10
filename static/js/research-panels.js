import { fetchJson } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { changeClass, formatNumber } from "./format.js";
import {
  asArray,
  asObject,
  renderInlineItems,
  renderLimitedItems,
  renderMetricPairs,
  renderMissingData,
  safeText,
  signedText,
  thresholdClass,
} from "./research-render-utils.js";

const AI_QUESTION_PRESETS = ["现在能不能买？", "风险在哪里？", "适不适合做T？", "明天重点看什么？"];
let aiQuestionRequestSeq = 0;

export function renderResearch(workbench, state) {
  const data = asObject(workbench);
  renderResearchPanel("aiDashboard", "AI单股驾驶舱", () => renderAiDashboard(data, state));
  renderResearchPanel("featureSnapshot", "特征快照", () => renderFeatureSnapshot(data.feature_snapshot));
  renderResearchPanel("diagnosisPanel", "个股诊断", () => renderDiagnosis(data.diagnosis));
  renderResearchPanel("alphaEvidence", "Alpha证据链", () => renderAlphaEvidence(data.alpha_evidence));
  renderResearchPanel("marketRegime", "市场环境", () => renderMarketRegime(data.market_regime));
  renderResearchPanel("signalValidation", "信号验证", () => renderSignalValidation(data.signal_validation));
  renderResearchPanel("timeframeAlignment", "多周期一致性", () => renderTimeframeAlignment(data.timeframe_alignment));
  renderResearchPanel("riskReward", "风险收益", () => renderRiskReward(data.risk_reward));
  renderResearchPanel("factorLab", "因子实验室", () => renderFactorLab(data.factor_lab));
  renderResearchPanel("themePanel", "题材背景", () => renderThemeContext(data.theme_context));
  renderResearchPanel("chipPanel", "筹码分析", () => renderChipAnalysis(data.chip_analysis));
  renderResearchPanel("leadershipPanel", "龙头识别", () => renderLeadership(data.leadership));
  renderResearchPanel("replayPanel", "历史回放", () => renderReplay(data.replay));
}

function renderResearchPanel(elementId, title, render) {
  try {
    render();
  } catch (error) {
    const el = $(elementId);
    if (!el) return;
    el.innerHTML = `
      <div class="empty-state">
        <strong>${escapeHtml(title)}暂不可用</strong>
        <span>${escapeHtml(error && error.message ? error.message : "该模块数据格式异常，主分析不受影响。")}</span>
      </div>`;
  }
}

function handleAiDashboardClick(event) {
  const question = aiQuestionFromEvent(event);
  if (!question) return;
  const { input, form } = aiQuestionControls();
  if (!input || !form) return;
  input.value = question;
  form.requestSubmit();
}

function aiQuestionFromEvent(event) {
  const button = event.target.closest("button[data-ai-question]");
  return button ? button.dataset.aiQuestion || "" : "";
}

async function handleAiQuestionSubmit(event, state) {
  event.preventDefault();
  const { input, form, button } = aiQuestionControls(event.currentTarget);
  const question = input ? input.value.trim() : "";
  if (!question || !form) return;
  if (button && button.disabled) return;
  const request = beginAiQuestionRequest(state, form);
  try {
    setAiQuestionBusy(button, true);
    const answer = await requestAiQuestion(request.symbol, question);
    if (!isCurrentAiQuestionRequest(request, state)) return;
    replaceAiAnswer(form, renderQuestionAnswerCard(answer));
  } catch (error) {
    if (!isCurrentAiQuestionRequest(request, state)) return;
    replaceAiAnswer(form, renderAiQuestionError(error));
  } finally {
    if (isCurrentAiQuestionRequest(request, state)) {
      setAiQuestionBusy(button, false);
    }
  }
}

function beginAiQuestionRequest(state, form) {
  return {
    id: ++aiQuestionRequestSeq,
    symbol: state.symbol,
    form,
  };
}

function isCurrentAiQuestionRequest(request, state) {
  return request.id === aiQuestionRequestSeq && request.symbol === state.symbol && isConnectedAiQuestionForm(request.form);
}

function isConnectedAiQuestionForm(form) {
  return Boolean(form) && form.isConnected !== false && $("aiQuestionForm") === form;
}

function aiQuestionControls(submittedForm = null) {
  const form = submittedForm || $("aiQuestionForm");
  return {
    input: $("aiQuestionInput"),
    form,
    button: form ? form.querySelector("button") : null,
  };
}

function setAiQuestionBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  button.textContent = busy ? "分析中" : "问一下";
}

function requestAiQuestion(symbol, question) {
  return fetchJson("/api/stock/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol, question }),
  });
}

function replaceAiAnswer(form, html) {
  removeExistingAiAnswer(form);
  if (form) form.insertAdjacentHTML("afterend", html);
}

function removeExistingAiAnswer(form) {
  const dashboard = form && form.closest ? form.closest("#aiDashboard") : null;
  const old = dashboard ? dashboard.querySelector(".ai-card-wide") : document.querySelector(".ai-card-wide");
  if (old) old.remove();
}

function renderAiQuestionError(error) {
  return `<section class="ai-card ai-card-wide"><strong>本次问诊</strong><span class="risk">${escapeHtml(error.message)}</span></section>`;
}

function renderAiDashboard(workbench, state) {
  const el = $("aiDashboard");
  if (!el || !workbench) return;
  const view = aiDashboardView(workbench);
  el.innerHTML = `
    <div class="ai-dashboard-head">
      <div>
        <span>AI单股驾驶舱</span>
        <strong>${escapeHtml(view.qa.summary || "围绕当前个股生成可执行问诊。")}</strong>
      </div>
      <i>${escapeHtml(view.risk.overall_level || "风险待确认")}</i>
    </div>
    <form class="ai-question-bar" id="aiQuestionForm">
      <input id="aiQuestionInput" type="text" maxlength="120" placeholder="输入你想问这只个股的问题，例如：现在能不能买？" />
      <button type="submit">问一下</button>
    </form>
    <div class="ai-question-presets">${renderAiQuestionPresets()}</div>
    ${view.questionAnswer ? renderQuestionAnswerCard(view.questionAnswer) : ""}
    <div class="ai-dashboard-grid">${renderAiDashboardGrid(view)}</div>
  `;
  bindAiDashboard(el, state);
}

function aiDashboardView(workbench) {
  return {
    qa: workbench.qa_report || {},
    questionAnswer: workbench.question_answer || null,
    evidence: workbench.evidence_chain || {},
    risk: workbench.risk_radar || {},
    eventDigest: workbench.event_digest || {},
    peer: workbench.peer_comparison || {},
    tStrategy: workbench.t_strategy || {},
  };
}

function renderAiQuestionPresets() {
  return AI_QUESTION_PRESETS.map((item) => `<button type="button" data-ai-question="${escapeHtml(item)}">${escapeHtml(item)}</button>`).join("");
}

function renderAiDashboardGrid(view) {
  return [
    renderQaCard(view.qa),
    renderEvidenceChainCard(view.evidence),
    renderRiskRadarCard(view.risk),
    renderEventDigestCard(view.eventDigest),
    renderPeerCard(view.peer),
    renderTStrategyCard(view.tStrategy),
  ].join("");
}

function bindAiDashboard(el, state) {
  const questionForm = $("aiQuestionForm");
  if (questionForm) {
    questionForm.addEventListener("submit", (event) => handleAiQuestionSubmit(event, state));
  }
  el.onclick = handleAiDashboardClick;
}

function renderQuestionAnswerCard(report) {
  return `
    <section class="ai-card ai-card-wide">
      <div class="ai-answer-head">
        <strong>本次问诊</strong>
        <i class="${questionAnswerTone(report)}">${escapeHtml(questionAnswerSource(report))}</i>
      </div>
      <div>
        <b>${escapeHtml(report.question || "未输入问题")}</b>
        <span class="ai-answer-text">${escapeHtml(report.answer || report.conclusion || "暂无回答")}</span>
      </div>
      <span>主题：${escapeHtml(report.topic || "--")} · 置信度 ${escapeHtml(report.confidence ?? "--")}%</span>
      ${questionAnswerStatus(report)}
      ${renderInlineItems(report.evidence, "em", 3)}
      ${renderAnswerColumn("行动建议", report.actions)}
      ${renderAnswerColumn("失效条件", report.invalidations, "risk")}
      ${renderRelatedQuestions(report.related_questions)}
    </section>
  `;
}

function questionAnswerSource(report) {
  return report.answer_source || (report.llm_used ? "大模型解释增强" : "规则问诊");
}

function questionAnswerTone(report) {
  return report.llm_used ? "good" : "";
}

function questionAnswerStatus(report) {
  const status = report.llm_status || "";
  return status ? `<em class="${questionAnswerTone(report)}">${escapeHtml(status)}</em>` : "";
}

function renderAnswerColumn(title, items, className = "") {
  const content = renderInlineItems(items, "span", 3, className);
  return content ? `<div class="ai-answer-columns"><b>${escapeHtml(title)}</b>${content}</div>` : "";
}

function renderRelatedQuestions(items) {
  const buttons = renderLimitedItems(
    items,
    3,
    (item) => `<button type="button" data-ai-question="${escapeHtml(item)}">${escapeHtml(item)}</button>`
  );
  return buttons ? `<div class="ai-related-questions">${buttons}</div>` : "";
}

function renderQaCard(report) {
  report = asObject(report);
  const items = asArray(report.items);
  return `
    <section class="ai-card">
      <strong>个股问诊</strong>
      ${items.length ? renderLimitedItems(items, 4, renderQaItem) : `<span>问诊结果待生成。</span>`}
    </section>
  `;
}

function renderQaItem(item) {
  item = asObject(item);
  return `<div><b>${escapeHtml(item.question)}</b><span>${escapeHtml(item.answer)}</span></div>`;
}

function renderEvidenceChainCard(report) {
  report = asObject(report);
  return `
    <section class="ai-card">
      <strong>证据链</strong>
      <p>${escapeHtml(report.summary || "")}</p>
      ${renderInlineItems(report.support, "span", 2, "good")}
      ${renderInlineItems(report.opposition, "span", 2, "risk")}
      ${renderInlineItems(report.invalidations, "em", 2) || `<span>失效条件待确认。</span>`}
    </section>
  `;
}

function renderRiskRadarCard(report) {
  report = asObject(report);
  const items = asArray(report.items);
  return `
    <section class="ai-card">
      <strong>风险雷达</strong>
      <p>${escapeHtml(report.summary || "")}</p>
      <div class="radar-list">
        ${items.length ? renderLimitedItems(items, 6, renderRiskRadarItem) : `<span>风险项待确认</span>`}
      </div>
    </section>
  `;
}

function renderRiskRadarItem(item) {
  item = asObject(item);
  return `<span class="${thresholdClass(item.score, { higherIsRisk: true, riskAt: 68, goodAt: 35 })}">${escapeHtml(item.name)} ${escapeHtml(item.level)} · ${escapeHtml(item.score)}</span>`;
}

function renderEventDigestCard(report) {
  return `
    <section class="ai-card">
      <strong>事件摘要</strong>
      <p>${escapeHtml(report.summary || "")}</p>
      ${renderInlineItems(report.negative_events, "span", 2, "risk")}
      ${renderInlineItems(report.positive_events, "span", 2, "good")}
      ${renderInlineItems(report.watch_events, "span", 2)}
    </section>
  `;
}

function renderPeerCard(report) {
  return `
    <section class="ai-card">
      <strong>同行对比</strong>
      <p>${escapeHtml(report.summary || "同行样本待确认。")}</p>
      <span>${escapeHtml(report.industry || "行业待确认")} · 样本 ${escapeHtml(report.sample_count || 0)}</span>
      <span>${escapeHtml(report.valuation_position || "")} / ${escapeHtml(report.strength_position || "")}</span>
      ${renderInlineItems(report.metrics, "em", 3)}
      ${renderInlineItems(report.warnings, "em", 2, "risk")}
    </section>
  `;
}

function renderTStrategyCard(report) {
  return `
    <section class="ai-card">
      <strong>做T助手</strong>
      <p>${escapeHtml(report.summary || "做T建议待生成。")}</p>
      <span>低吸区：${escapeHtml(report.low_zone || "--")}</span>
      <span>高抛区：${escapeHtml(report.high_zone || "--")}</span>
      ${renderInlineItems(report.stop_conditions, "em", 2)}
    </section>
  `;
}

function renderFeatureSnapshot(feature) {
  const el = $("featureSnapshot");
  if (!el || !feature) {
    if (el) el.innerHTML = "";
    return;
  }
  const chips = [
    ["趋势", joinedMetric(feature.trend_score, feature.trend_label)],
    ["资金", fallbackText(feature.fund_flow_score)],
    ["龙头", joinedMetric(feature.leader_score, feature.leader_level)],
    ["量能", `${formatNumber(feature.volume_ratio)}倍`],
    ["估值", fallbackText(feature.valuation_score)],
    ["质量", joinedMetric(feature.data_quality_level, feature.data_quality_score, " ")],
  ];
  el.innerHTML = `
    ${chips
      .map(
        ([label, value]) => `
        <div>
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>`
      )
      .join("")}
    <div class="feature-tags">${renderInlineItems(feature.tags, "i")}</div>
  `;
}

function renderDiagnosis(diagnosis) {
  const el = $("diagnosisPanel");
  if (!el || !diagnosis) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="diagnosis-head">
      <div>
        <span>个股诊断</span>
        <strong>${escapeHtml(diagnosis.headline)}</strong>
      </div>
      <i>${escapeHtml(diagnosis.action)} · ${escapeHtml(diagnosis.confidence)}%</i>
    </div>
    <p>${escapeHtml(diagnosis.beginner_summary)}</p>
    <small>${escapeHtml(diagnosis.professional_summary)}</small>
    <div class="diagnosis-grid">
      <div>
        <strong>确认信号</strong>
        ${renderInlineItems(diagnosis.confirmation_signals, "span", 4)}
      </div>
      <div>
        <strong>硬风险</strong>
        ${renderInlineItems(diagnosis.hard_risks, "span", 4, "risk")}
      </div>
    </div>
  `;
}

function renderAlphaEvidence(report) {
  const el = $("alphaEvidence");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const view = alphaEvidenceView(report);
  el.innerHTML = `
    <div class="alpha-head">
      <strong>Alpha证据链</strong>
      <span>${escapeHtml(view.verdict)} · 置信度 ${escapeHtml(view.confidence)}%</span>
    </div>
    <p>${escapeHtml(view.summary)}</p>
    <div class="alpha-grid">
      ${renderAlphaColumn("支持证据", view.positives, "good", "等待更多积极证据。")}
      ${renderAlphaColumn("风险证据", view.negatives, "risk", "当前未识别核心风险证据。")}
    </div>
    ${renderAlphaMissingData(view.missingData)}
  `;
}

function alphaEvidenceView(report) {
  report = asObject(report);
  return {
    verdict: report.verdict,
    confidence: report.confidence,
    summary: report.summary,
    positives: asArray(report.positives).slice(0, 4),
    negatives: asArray(report.negatives).slice(0, 4),
    missingData: asArray(report.missing_data).slice(0, 6),
  };
}

function renderAlphaColumn(title, items, className, emptyText) {
  return `
      <div>
        <strong>${escapeHtml(title)}</strong>
        ${items.length ? items.map((item) => renderAlphaItem(item, className)).join("") : renderAlphaEmpty(emptyText)}
      </div>`;
}

function renderAlphaItem(item, className) {
  item = asObject(item);
  return `<span class="${className}"><b>${escapeHtml(item.title)} ${escapeHtml(signedText(item.impact))}</b><small>${escapeHtml(item.reason)}</small></span>`;
}

function renderAlphaEmpty(text) {
  return `<span><b>暂无</b><small>${escapeHtml(text)}</small></span>`;
}

function renderAlphaMissingData(items) {
  return renderMissingData(items, { tagName: "em", prefix: "待补数据：" });
}

function renderMarketRegime(report) {
  const el = $("marketRegime");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="regime-head">
      <div>
        <span>市场环境</span>
        <strong>${escapeHtml(report.market_label)} · ${escapeHtml(report.stock_state)}</strong>
      </div>
      <i class="${marketRegimeRiskClass(report.risk_multiplier)}">风险倍率 ${formatNumber(report.risk_multiplier, 2)}</i>
    </div>
    <div class="regime-tags">${renderMarketRegimeTags(report)}</div>
    <div class="regime-grid">
      ${renderMarketRegimeColumn("操作提醒", report.suggestions, 3)}
      ${renderMarketRegimeColumn("判断依据", report.evidence, 4)}
    </div>
  `;
}

function marketRegimeRiskClass(value) {
  return thresholdClass(value, { higherIsRisk: true, riskAt: 1.15, goodAt: 0.92 });
}

function renderMarketRegimeTags(report) {
  return renderInlineItems([
    report.industry_label,
    `${report.breadth_label || "市场宽度待确认"} · ${report.breadth_score ?? "--"}分`,
    `置信修正 ${signedText(report.confidence_adjustment)}`,
  ], "span");
}

function renderMarketRegimeColumn(title, items, limit) {
  return `
    <div>
      <strong>${escapeHtml(title)}</strong>
      ${renderInlineItems(items, "span", limit)}
    </div>`;
}

function renderSignalValidation(report) {
  const el = $("signalValidation");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const items = asArray(report.items);
  el.innerHTML = `
    <div class="validation-head">
      <div>
        <span>信号验证闭环</span>
        <strong>${escapeHtml(report.overall_status)}</strong>
      </div>
      <i>${escapeHtml(items.length)}项</i>
    </div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="validation-grid">
      ${renderLimitedItems(items, 4, renderValidationItem)}
    </div>
    ${renderInlineItems(report.notes, "small", 1)}
  `;
}

function renderValidationItem(item) {
  item = asObject(item);
  const status = safeText(item.status);
  const statusClass = status.includes("风险") || status.includes("压制") ? "risk" : status.includes("确认") ? "good" : "";
  return `
    <div class="validation-item ${statusClass}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.status)} · ${escapeHtml(item.confidence)}%</span>
      </div>
      <small>触发：${escapeHtml(item.trigger_condition)}</small>
      <small>确认：${escapeHtml(item.confirmation_condition)}</small>
      <small>失效：${escapeHtml(item.invalidation_condition)}</small>
      <em>${escapeHtml(item.historical_reference)}</em>
    </div>
  `;
}

function renderTimeframeAlignment(report) {
  const el = $("timeframeAlignment");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const view = timeframeAlignmentView(report);
  el.innerHTML = `
    <div class="timeframe-head">
      <div>
        <span>多周期一致性</span>
        <strong>${escapeHtml(view.label)} · ${escapeHtml(view.score)}分</strong>
      </div>
      <i class="${view.conflictClass}">${escapeHtml(view.conflictLevel)}</i>
    </div>
    <p>${escapeHtml(view.summary)}</p>
    <div class="timeframe-grid">
      ${view.timeframes.map(renderTimeframeItem).join("")}
    </div>
    ${renderTimeframeSuggestions(view.suggestions)}
  `;
}

function timeframeAlignmentView(report) {
  report = asObject(report);
  return {
    label: report.alignment_label,
    score: report.alignment_score,
    conflictLevel: report.conflict_level,
    conflictClass: timeframeConflictClass(report),
    summary: report.summary,
    timeframes: asArray(report.timeframes),
    suggestions: asArray(report.suggestions),
  };
}

function timeframeConflictClass(report) {
  report = asObject(report);
  const conflictLevel = safeText(report.conflict_level);
  const alignmentLabel = safeText(report.alignment_label);
  if (conflictLevel.includes("冲突") || alignmentLabel.includes("偏弱")) return "risk";
  if (alignmentLabel.includes("共振")) return "good";
  return "";
}

function renderTimeframeItem(item) {
  item = asObject(item);
  return `
    <div class="timeframe-item ${timeframeItemClass(item)}">
      <div>
        <strong>${escapeHtml(fallbackText(item.name))}</strong>
        <span>${escapeHtml(joinedMetric(item.score, item.label))}</span>
      </div>
      <small>${escapeHtml(item.window_days)}日 · 涨跌 ${formatNumber(item.return_pct)}% · 回撤 ${formatNumber(item.max_drawdown_pct)}%</small>
      <small>${escapeHtml(timeframeMaText(item))}</small>
    </div>
  `;
}

function joinedMetric(left, right, separator = " · ") {
  return `${fallbackText(left)}${separator}${fallbackText(right)}`;
}

function fallbackText(value, fallback = "--") {
  const text = safeText(value).trim();
  return text || fallback;
}

function timeframeMaText(item) {
  if (item.above_ma === true) return `高于均线 ${formatNumber(item.ma_value)}`;
  if (item.above_ma === false) return `低于均线 ${formatNumber(item.ma_value)}`;
  return "均线关系待确认";
}

function timeframeItemClass(item) {
  return thresholdClass(item.score, { goodAt: 62, riskAt: 45 });
}

function renderTimeframeSuggestions(items) {
  return `<div class="timeframe-suggestions">${renderInlineItems(items, "span", 3)}</div>`;
}

function renderRiskReward(report) {
  const el = $("riskReward");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const view = riskRewardView(report);
  el.innerHTML = `
    <div class="risk-reward-head">
      <div>
        <span>风险收益与情景</span>
        <strong>${escapeHtml(view.rating)}</strong>
      </div>
      <i class="${view.ratingClass}">收益风险比 ${formatNumber(view.rewardRiskRatio, 2)}</i>
    </div>
    <div class="risk-reward-metrics">${renderRiskRewardMetrics(view)}</div>
    <p>${escapeHtml(view.summary)}</p>
    <div class="scenario-grid">
      ${view.scenarios.map(renderScenarioPlan).join("")}
    </div>
    ${renderInlineItems(view.notes, "small", 1)}
  `;
}

function riskRewardView(report) {
  report = asObject(report);
  return {
    rating: report.rating,
    ratingClass: riskRewardRatingClass(report.rating),
    rewardRiskRatio: report.reward_risk_ratio,
    currentPrice: report.current_price,
    upsideTarget: report.upside_target,
    upsidePct: report.upside_pct,
    downsideStop: report.downside_stop,
    downsidePct: report.downside_pct,
    atrPct: report.atr_pct,
    volatilityPct: report.volatility_pct,
    summary: report.summary,
    scenarios: asArray(report.scenarios),
    notes: asArray(report.notes),
  };
}

function riskRewardRatingClass(rating) {
  rating = safeText(rating);
  if (rating.includes("风险") || rating.includes("不足")) return "risk";
  if (rating.includes("较好")) return "good";
  return "";
}

function renderRiskRewardMetrics(view) {
  return renderMetricPairs([
    ["现价", formatNumber(view.currentPrice)],
    ["上方目标", `${formatNumber(view.upsideTarget)} / ${formatNumber(view.upsidePct)}%`],
    ["下方防守", `${formatNumber(view.downsideStop)} / ${formatNumber(view.downsidePct)}%`],
    ["ATR / 波动", `${formatNumber(view.atrPct, 2)}% / ${formatNumber(view.volatilityPct, 2)}%`],
  ]);
}

function renderScenarioPlan(item) {
  item = asObject(item);
  const name = safeText(item.name);
  const scenarioClass = name.includes("防守") ? "risk" : name.includes("积极") ? "good" : "";
  return `
    <div class="scenario-item ${scenarioClass}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.probability)}%</span>
      </div>
      <small>触发：${escapeHtml(item.trigger)}</small>
      <small>预期：${escapeHtml(item.expected_move)}</small>
      <small>应对：${escapeHtml(item.response)}</small>
      <em>失效：${escapeHtml(item.invalidation)}</em>
    </div>
  `;
}

function renderFactorLab(report) {
  const el = $("factorLab");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    ${renderFactorLabHead(report)}
    <div class="factor-lab-metrics">${renderFactorLabMetrics(report)}</div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="factor-lab-grid">${renderFactorLabItems(report.factors)}</div>
    ${renderInlineItems(report.weight_policy, "em", 2)}
    ${renderInlineItems(report.notes, "small", 2)}
  `;
}

function renderFactorLabHead(report) {
  return `
    <div class="factor-lab-head">
      <div>
        <span>因子实验室</span>
        <strong>${escapeHtml(report.total_score)}分 · 校准置信 ${escapeHtml(report.calibrated_confidence)}%</strong>
      </div>
      <i>${escapeHtml(firstText(report.top_positive, "等待确认"))}</i>
    </div>`;
}

function renderFactorLabMetrics(report) {
  return renderMetricPairs([
    ["个股画像", report.profile_label || "常规个股"],
    ["历史样本", report.calibration_sample_count || 0],
    ["正向因子", report.positive_factor_count || 0],
    ["拖累因子", report.negative_factor_count || 0],
  ]);
}

function renderFactorLabItems(items) {
  return renderLimitedItems(items, 6, renderStandardFactor);
}

function firstText(items, fallback) {
  return asArray(items)[0] || fallback;
}

function renderStandardFactor(item) {
  item = asObject(item);
  const calibration = asObject(item.calibration);
  const bucket = asArray(item.calibration_buckets)[0];
  return `
    <div class="standard-factor ${factorDirectionClass(item)}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.score)} · 权重 ${formatNumber(item.weight, 2)}</span>
      </div>
      <div class="score-bar"><i style="width:${Math.max(0, Math.min(100, Number(item.score) || 0))}%"></i></div>
      <p>${escapeHtml(item.value)}</p>
      <small>${factorCalibrationSampleText(calibration)}</small>
      ${factorCalibrationReturnLine(calibration)}
      ${factorPercentileLine(item)}
      ${factorBucketLine(bucket)}
      ${renderInlineItems(item.evidence, "small", 1)}
    </div>
  `;
}

function factorDirectionClass(item) {
  item = asObject(item);
  if (item.direction === "负向") return "risk";
  if (item.direction === "正向") return "good";
  return "";
}

function factorCalibrationSampleText(calibration) {
  calibration = asObject(calibration);
  if (!calibration.sample_count) {
    return escapeHtml(calibration.confidence_level || "待补数据");
  }
  return `样本 ${escapeHtml(calibration.sample_count)} · ${escapeHtml(calibration.confidence_level || "观察")} / ${escapeHtml(calibration.expected_level || "观察")}`;
}

function factorCalibrationReturnLine(calibration) {
  calibration = asObject(calibration);
  if (!calibration.sample_count) return "";
  const text = `胜率 ${formatNumber(calibration.win_rate, 1)}% · 5日 ${formatNumber(calibration.avg_forward_5d_return)}% · 最大不利 ${formatNumber(calibration.max_adverse_return)}%`;
  return `<small>${escapeHtml(text)}</small>`;
}

function factorPercentileLine(item) {
  if (item.percentile === null || item.percentile === undefined) return "";
  return `<em>${escapeHtml(`历史分位 ${formatNumber(item.percentile, 1)}%`)}</em>`;
}

function factorBucketLine(bucket) {
  bucket = asObject(bucket);
  if (!Object.keys(bucket).length) return "";
  return `<em>${escapeHtml(bucket.name)}：${escapeHtml(bucket.sample_count)}样本 / 5日 ${formatNumber(bucket.avg_forward_5d_return)}%</em>`;
}

function renderChipAnalysis(chip) {
  const el = $("chipPanel");
  if (!el || !chip) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="chip-head">
      <strong>${escapeHtml(chip.distribution_label)} · ${escapeHtml(chip.concentration)}</strong>
      <span>成本中枢 ${formatNumber(chip.center_price)}</span>
    </div>
    <p>${escapeHtml(chip.summary)}</p>
    <div class="band-grid">
      <div>
        <strong>支撑区</strong>
        ${renderChipBands(chip.support_bands)}
      </div>
      <div>
        <strong>压力区</strong>
        ${renderChipBands(chip.pressure_bands)}
      </div>
    </div>
    ${renderInlineItems(chip.notes, "small", 2)}
  `;
}

function renderChipBands(items) {
  return asArray(items).length
    ? renderLimitedItems(
        items,
        3,
        (item) => `<span><b>${formatNumber(item.low)} - ${formatNumber(item.high)}</b><small>${formatNumber(item.share, 1)}% · ${escapeHtml(item.note)}</small></span>`
      )
    : `<span><b>暂无</b><small>当前价格附近缺少明显成交密集区。</small></span>`;
}

function renderLeadership(report) {
  const el = $("leadershipPanel");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="leader-head">
      <strong>${escapeHtml(report.score)} · ${escapeHtml(report.level)}</strong>
      <span>${escapeHtml(report.summary)}</span>
    </div>
    <div class="feature-tags">${renderInlineItems(report.tags, "i")}</div>
    ${renderInlineItems(report.evidence, "p", 4)}
    ${renderMissingData(report.missing_data)}
  `;
}

function renderThemeContext(report) {
  const el = $("themePanel");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="theme-head">
      <div>
        <strong>${escapeHtml(report.level)} · ${escapeHtml(report.score)}分</strong>
        <span>${escapeHtml(report.style)} · ${escapeHtml(report.relative_strength || "强弱待确认")}</span>
      </div>
      <i>${themeIndustryText(report)}</i>
    </div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="concept-strip">${renderConceptStrip(report.concepts)}</div>
    <div class="theme-grid">
      <div>
        <strong>机会</strong>
        ${renderInlineItems(report.opportunities, "small", 3)}
      </div>
      <div>
        <strong>风险</strong>
        ${renderInlineItems(report.risks, "small", 3)}
      </div>
    </div>
    ${renderInlineItems(report.evidence, "em", 2)}
    ${renderMissingData(report.missing_data)}
  `;
}

function themeIndustryText(report) {
  const change = report.industry_change_pct;
  const suffix = change === null || change === undefined ? "" : ` ${formatNumber(change)}%`;
  return `${escapeHtml(report.industry)}${suffix}`;
}

function renderConceptStrip(concepts) {
  const items = asArray(concepts);
  if (!items.length) {
    return `<span><b>概念待确认</b><small>等待公开源或本地缓存补齐。</small></span>`;
  }
  return renderLimitedItems(items, 6, renderConceptPill);
}

function renderConceptPill(item) {
  item = asObject(item);
  return `
    <span class="${conceptChangeClass(item.change_pct)}">
      <b>${escapeHtml(item.name)}</b>
      <small>${formatNumber(item.change_pct)}%${conceptLeaderText(item)}</small>
      <em>${escapeHtml(item.match_reason || item.source || "概念成分匹配")}</em>
    </span>`;
}

function conceptChangeClass(value) {
  const className = changeClass(value);
  if (className === "up") return "up-bg";
  if (className === "down") return "down-bg";
  return "neutral";
}

function conceptLeaderText(item) {
  item = asObject(item);
  return item.leading_stock ? ` · 领涨 ${escapeHtml(item.leading_stock)}` : "";
}

function renderReplay(replay) {
  const el = $("replayPanel");
  if (!el || !replay) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="replay-head">
      <strong>${escapeHtml(replayHeadline(replay))}</strong>
      <span>样本 ${escapeHtml(replay.sample_count)} / ${escapeHtml(replay.window_days)} 日</span>
    </div>
    <p>${escapeHtml(replay.summary)}</p>
    <div class="replay-stats">${renderReplayStats(replay.pattern_stats)}</div>
    <div class="replay-cases">${renderReplayCases(replay.cases)}</div>
  `;
}

function replayHeadline(replay) {
  return Number(replay.sample_count || 0) >= 5 ? `样本有效率 ${formatNumber(replay.success_rate, 1)}%` : "样本偏少";
}

function renderReplayStats(items) {
  const rows = renderLimitedItems(items, 4, renderReplayStat);
  return rows || `<div><strong>暂无样本</strong><span>等待更多历史信号。</span></div>`;
}

function renderReplayStat(item) {
  return `
    <div>
      <strong>${escapeHtml(item.pattern)}</strong>
      <span>${escapeHtml(item.sample_count)}次 · 胜率 ${formatNumber(item.win_rate, 1)}% · 5日 ${formatNumber(item.avg_forward_5d_return)}%</span>
      <small>${escapeHtml(item.note)}</small>
    </div>`;
}

function renderReplayCases(items) {
  return asArray(items).slice(-5).map(renderReplayCase).join("");
}

function renderReplayCase(item) {
  item = asObject(item);
  return `
    <span>
      <b>${escapeHtml(item.date)} · ${escapeHtml(item.pattern)} · ${escapeHtml(item.outcome)}</b>
      <small>3日 ${formatReplayReturn(item.forward_3d_return)} / 5日 ${formatReplayReturn(item.forward_5d_return)}</small>
    </span>`;
}

function formatReplayReturn(value) {
  return value === null || value === undefined ? "--" : `${formatNumber(value)}%`;
}
