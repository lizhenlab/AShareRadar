import { createRequestScope, fetchJson, isAbortError } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import {
  conceptChangeClass,
  formatNumber,
  formatReplayReturn,
  replayHeadline,
} from "./research-formatters.js";
import {
  asArray,
  asObject,
  renderInlineItems,
  renderLimitedItems,
  renderMissingData,
  thresholdClass,
} from "./research-render-utils.js";

const AI_QUESTION_PRESETS = ["现在能不能买？", "风险在哪里？", "适不适合做T？", "明天重点看什么？"];
const AI_QUESTION_REQUEST_TIMEOUT_MS = 35000;

export function renderAiDashboard(workbench, state, options = {}) {
  cancelAiQuestionRequest(state);
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
  bindAiDashboard(el, state, options);
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

function bindAiDashboard(el, state, options) {
  const questionForm = $("aiQuestionForm");
  if (questionForm) {
    const requestContext = { current: null, options };
    questionForm.addEventListener("submit", (event) => handleAiQuestionSubmit(event, state, requestContext));
  }
  el.onclick = handleAiDashboardClick;
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

async function handleAiQuestionSubmit(event, state, requestContext) {
  event.preventDefault();
  const { input, form, button } = aiQuestionControls(event.currentTarget);
  const question = input ? input.value.trim() : "";
  if (!question || !form) return;
  if (button && button.disabled) return;
  const request = beginAiQuestionRequest(state, form, requestContext);
  try {
    setAiQuestionBusy(button, true);
    const answer = await requestAiQuestion(request, question);
    if (!isCurrentAiQuestionRequest(request, state, requestContext)) return;
    replaceAiAnswer(form, renderQuestionAnswerCard(answer));
  } catch (error) {
    if (isAbortError(error)) return;
    if (!isCurrentAiQuestionRequest(request, state, requestContext)) return;
    replaceAiAnswer(form, renderAiQuestionError(error));
  } finally {
    if (isCurrentAiQuestionRequest(request, state, requestContext)) {
      setAiQuestionBusy(button, false);
    }
    finishAiQuestionRequest(request, state, requestContext);
  }
}

function beginAiQuestionRequest(state, form, requestContext) {
  const options = requestContext.options || {};
  const parentSignal = options.signal || (state.loadRequest && state.loadRequest.signal);
  const scope = createRequestScope(state.aiQuestionRequest, parentSignal);
  const request = {
    symbol: state.symbol,
    loadRequest: state.loadRequest || null,
    loadSeq: state.loadSeq,
    form,
    scope,
    signal: scope.signal,
    isCurrent: options.isCurrent,
  };
  state.aiQuestionRequest = scope;
  requestContext.current = request;
  return request;
}

function isCurrentAiQuestionRequest(request, state, requestContext) {
  return (
    requestContext.current === request &&
    state.aiQuestionRequest === request.scope &&
    !request.signal.aborted &&
    request.symbol === state.symbol &&
    isCurrentAiLoad(request, state) &&
    (!request.isCurrent || request.isCurrent()) &&
    isConnectedAiQuestionForm(request.form)
  );
}

function isCurrentAiLoad(request, state) {
  if (request.loadRequest) return state.loadRequest === request.loadRequest;
  return request.loadSeq === state.loadSeq;
}

function finishAiQuestionRequest(request, state, requestContext) {
  if (requestContext.current === request) requestContext.current = null;
  if (state.aiQuestionRequest === request.scope) state.aiQuestionRequest = null;
  request.scope.dispose();
}

function cancelAiQuestionRequest(state) {
  const request = state && state.aiQuestionRequest;
  if (!request || typeof request.abort !== "function") return;
  request.abort();
  if (state.aiQuestionRequest === request) state.aiQuestionRequest = null;
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

function requestAiQuestion(request, question) {
  return fetchJson("/api/stock/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol: request.symbol, question }),
    signal: request.signal,
    timeoutMs: AI_QUESTION_REQUEST_TIMEOUT_MS,
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
      <span>主题：${escapeHtml(report.topic || "--")} · 回答可靠度 ${escapeHtml(report.confidence ?? "--")}/100</span>
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

export function renderThemeContext(report) {
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

function conceptLeaderText(item) {
  item = asObject(item);
  return item.leading_stock ? ` · 领涨 ${escapeHtml(item.leading_stock)}` : "";
}

export function renderReplay(replay) {
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
