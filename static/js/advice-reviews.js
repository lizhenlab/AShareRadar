import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";
import { normalizeUiSymbol } from "./symbols.js";

const CONCLUSION_LABELS = Object.freeze({
  pending: "等待后续行情",
  insufficient_data: "后续数据不足",
  target_hit: "目标价先触达",
  stop_hit: "止损价先触达",
  target_stop_ambiguous: "同日触达目标与止损",
  horizon_gain: "观察期收益为正",
  horizon_loss: "观察期收益为负",
  horizon_flat: "观察期基本持平",
});

const EVALUATION_STATUS_LABELS = Object.freeze({
  pending: "等待行情",
  insufficient: "数据不足",
  evaluated: "已评估",
});

const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const SHANGHAI_DATE_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

export async function loadAdviceReviews(state, options = {}) {
  const sequence = Number(state.adviceReviewReadSeq || 0) + 1;
  const symbol = reviewOwnerSymbol(state, options);
  state.adviceReviewReadSeq = sequence;
  prepareReviewSymbolState(state, symbol);
  resetReviewHistories(state);
  renderReviewLoading();
  try {
    const details = await fetchJson(
      `/api/reviews?symbol=${encodeURIComponent(symbol)}&limit=20`,
      { signal: options.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS }
    );
    if (!reviewReadIsCurrent(state, sequence, symbol, options)) return false;
    if (!Array.isArray(details)) throw new TypeError("复盘计划格式异常");
    state.adviceReviewDetails = details;
    renderAdviceReviewDetails(details, state);
    renderSnapshotOptions(state);
    if (!state.adviceReviewEditingPlanId) applySelectedSnapshotDefaults(state, { preserveText: false });
    return true;
  } catch (error) {
    if (isAbortError(error) || !reviewReadIsCurrent(state, sequence, symbol, options)) return false;
    renderReviewUnavailable(error);
    return false;
  }
}

export function syncAdviceReviewSnapshots(state, items, analysis) {
  state.adviceReviewSnapshots = Array.isArray(items) ? items.filter(validSnapshot) : [];
  state.adviceReviewAnalysis = analysis || null;
  renderSnapshotOptions(state);
  if (!state.adviceReviewEditingPlanId) applySelectedSnapshotDefaults(state, { preserveText: false });
}

export function selectAdviceReviewSnapshot(state) {
  if (state.adviceReviewEditingPlanId) return false;
  return applySelectedSnapshotDefaults(state, { preserveText: false });
}

export async function submitAdviceReviewPlan(state, options = {}) {
  const plan = editingPlan(state);
  const symbol = reviewOwnerSymbol(state, options, plan?.symbol);
  if (!symbol) throw new Error("当前复盘股票无效");
  if (plan && !sameSymbol(plan.symbol, symbol)) throw new Error("复盘计划不存在或已切换股票");
  const payload = reviewPlanPayload(state, plan, symbol);
  const url = plan ? `/api/reviews/plans/${encodeURIComponent(plan.id)}` : "/api/reviews/plans";
  const method = plan ? "PATCH" : "POST";
  const saved = await fetchJson(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  });
  if (!reviewOwnerIsCurrent(state, symbol, options)) return false;
  state.adviceReviewEditingPlanId = null;
  setReviewFormMode(null);
  setReviewFeedback(plan ? "复盘计划已更新" : "复盘计划已建立", "ok");
  await loadAdviceReviews(state, { ...options, symbol });
  return saved;
}

export async function deleteAdviceReviewPlan(state, planId, options = {}) {
  const detail = reviewDetail(state, planId);
  if (!detail?.plan) throw new Error("复盘计划不存在或已切换股票");
  const plan = detail.plan;
  const symbol = reviewOwnerSymbol(state, options, plan.symbol);
  if (!sameSymbol(plan.symbol, symbol)) throw new Error("复盘计划不存在或已切换股票");
  if (options.confirm && !options.confirm("删除该复盘计划及全部评估历史？此操作不可撤销。")) {
    return false;
  }
  const result = await fetchJson(`/api/reviews/plans/${encodeURIComponent(plan.id)}`, {
    method: "DELETE",
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  });
  if (!result?.ok || !result?.removed) throw new TypeError("复盘计划删除结果异常");
  if (!reviewOwnerIsCurrent(state, symbol, options)) return true;

  discardAdviceReviewPlanState(state, plan.id);
  renderAdviceReviewDetails(state.adviceReviewDetails, state);
  renderSnapshotOptions(state);
  if (!state.adviceReviewEditingPlanId) applySelectedSnapshotDefaults(state, { preserveText: false });
  setReviewFeedback("复盘计划及评估历史已删除", "ok");
  return true;
}

export async function evaluateAdviceReviewPlan(state, planId, options = {}) {
  const detail = reviewDetail(state, planId);
  if (!detail?.plan) throw new Error("复盘计划不存在或已切换股票");
  const plan = detail.plan;
  const symbol = options.symbol || plan.symbol || state.symbol;
  prepareReviewSymbolState(state, symbol);
  const now = resolvedNow(options.now);
  const asOfDate = normalizeEvaluationDate(
    Object.prototype.hasOwnProperty.call(options, "asOf") ? options.asOf : reviewAsOfInputValue(plan.id),
    plan.snapshot_market_time,
    now
  );
  const asOf = shanghaiAsOfTimestamp(asOfDate, now);
  setAdviceReviewEvaluationAsOf(state, plan.id, asOfDate || "");
  const sequence = nextPlanSequence(state, "adviceReviewEvaluationSeqByPlan", plan.id);
  const evaluation = await fetchJson(`/api/reviews/plans/${encodeURIComponent(plan.id)}/evaluate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(asOf ? { as_of: asOf } : {}),
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    signal: options.signal,
  });
  if (!evaluationRequestIsCurrent(state, plan, sequence, symbol, options)) return false;
  state.adviceReviewDetails = (state.adviceReviewDetails || []).map((detail) =>
    Number(detail?.plan?.id) === Number(plan.id) ? { ...detail, latest_evaluation: evaluation } : detail
  );
  mergeEvaluationIntoLoadedHistory(state, plan.id, evaluation);
  renderAdviceReviewDetails(state.adviceReviewDetails, state);
  setReviewFeedback("复盘评估已更新", "ok");
  return true;
}

export function setAdviceReviewEvaluationAsOf(state, planId, value) {
  const key = planKey(planId);
  if (!key) return false;
  if (!state.adviceReviewAsOfByPlan || typeof state.adviceReviewAsOfByPlan !== "object") {
    state.adviceReviewAsOfByPlan = {};
  }
  state.adviceReviewAsOfByPlan[key] = String(value || "").trim();
  return true;
}

export async function toggleAdviceReviewHistory(state, planId, options = {}) {
  const detail = reviewDetail(state, planId);
  if (!detail?.plan) return false;
  prepareReviewSymbolState(state, options.symbol || detail.plan.symbol || state.symbol);
  const history = reviewHistoryRecord(state, detail.plan.id);
  if (history.expanded) {
    history.expanded = false;
    renderAdviceReviewDetails(state.adviceReviewDetails || [], state);
    return true;
  }
  history.expanded = true;
  if (["ready", "empty", "loading"].includes(history.phase)) {
    renderAdviceReviewDetails(state.adviceReviewDetails || [], state);
    return true;
  }
  return loadAdviceReviewHistory(state, detail.plan.id, options);
}

export async function retryAdviceReviewHistory(state, planId, options = {}) {
  const detail = reviewDetail(state, planId);
  if (!detail?.plan) return false;
  prepareReviewSymbolState(state, options.symbol || detail.plan.symbol || state.symbol);
  const history = reviewHistoryRecord(state, detail.plan.id);
  history.expanded = true;
  return loadAdviceReviewHistory(state, detail.plan.id, { ...options, force: true });
}

export async function loadAdviceReviewHistory(state, planId, options = {}) {
  const detail = reviewDetail(state, planId);
  if (!detail?.plan) return false;
  const plan = detail.plan;
  const symbol = options.symbol || plan.symbol || state.symbol;
  prepareReviewSymbolState(state, symbol);
  const history = reviewHistoryRecord(state, plan.id);
  if (!options.force && ["ready", "empty"].includes(history.phase)) return true;
  const sequence = Number(history.sequence || 0) + 1;
  const epoch = Number(state.adviceReviewHistoryEpoch || 0);
  history.sequence = sequence;
  history.phase = "loading";
  history.error = "";
  renderAdviceReviewDetails(state.adviceReviewDetails || [], state);
  try {
    const evaluations = await fetchJson(
      `/api/reviews/plans/${encodeURIComponent(plan.id)}/evaluations?limit=100`,
      { signal: options.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS }
    );
    if (!historyRequestIsCurrent(state, plan, history, sequence, epoch, symbol, options)) return false;
    if (!Array.isArray(evaluations)) throw new TypeError("评估历史格式异常");
    history.items = mergeEvaluationItems(evaluations, history.items);
    history.phase = history.items.length ? "ready" : "empty";
    renderAdviceReviewDetails(state.adviceReviewDetails || [], state);
    return true;
  } catch (error) {
    if (isAbortError(error) || !historyRequestIsCurrent(state, plan, history, sequence, epoch, symbol, options)) {
      return false;
    }
    history.phase = "error";
    history.error = error?.message || "请稍后重试";
    renderAdviceReviewDetails(state.adviceReviewDetails || [], state);
    return false;
  }
}

export function beginAdviceReviewEdit(state, planId) {
  const detail = (state.adviceReviewDetails || []).find((item) => Number(item?.plan?.id) === Number(planId));
  if (!detail?.plan) return false;
  const plan = detail.plan;
  state.adviceReviewEditingPlanId = plan.id;
  setValue("reviewAdviceId", String(plan.advice_id));
  setValue("reviewHypothesis", plan.hypothesis);
  setValue("reviewTrigger", plan.trigger_condition);
  setValue("reviewInvalidation", plan.invalidation_condition);
  setValue("reviewTarget", plan.target_price);
  setValue("reviewStop", plan.stop_price);
  setValue("reviewHorizon", plan.horizon_days);
  setReviewFormMode(plan);
  setReviewFeedback("");
  $("reviewHypothesis")?.focus?.({ preventScroll: true });
  return true;
}

export function cancelAdviceReviewEdit(state) {
  state.adviceReviewEditingPlanId = null;
  setReviewFormMode(null);
  renderSnapshotOptions(state);
  applySelectedSnapshotDefaults(state, { preserveText: false });
  setReviewFeedback("");
}

export function renderAdviceReviewDetails(details, state = null) {
  const target = $("reviewPlanList");
  if (!target) return;
  target.innerHTML = details.length
    ? details.map((detail) => reviewDetailHtml(detail, state)).join("")
    : `<div class="review-plan-state"><strong>暂无复盘计划</strong><span>可从当前股票的保留建议快照建立计划。</span></div>`;
}

function reviewDetailHtml(detail, state) {
  const plan = detail.plan || {};
  const evaluation = detail.latest_evaluation;
  const key = planKey(plan.id);
  const history = key && state?.adviceReviewHistories?.[key];
  const historyExpanded = Boolean(history?.expanded);
  const asOfValue = key ? state?.adviceReviewAsOfByPlan?.[key] || "" : "";
  return `
    <article class="review-plan-item" data-review-plan="${escapeHtml(plan.id)}">
      <div class="review-plan-heading">
        <div>
          <strong>${escapeHtml(plan.hypothesis || "复盘计划")}</strong>
          <span>快照 ${escapeHtml(plan.snapshot_market_time || "--")} · 版本 ${escapeHtml(plan.revision || 1)}</span>
        </div>
        <div class="row-actions">
          <button type="button" class="mini-button" data-review-edit="${escapeHtml(plan.id)}">编辑</button>
          <button type="button" class="mini-button" data-review-history="${escapeHtml(plan.id)}" aria-expanded="${historyExpanded}" aria-controls="review-history-${escapeHtml(plan.id)}">${historyExpanded ? "收起历史" : "评估历史"}</button>
          <button type="button" class="icon-button" title="删除复盘计划" aria-label="删除复盘计划" data-review-delete="${escapeHtml(plan.id)}">×</button>
        </div>
      </div>
      <dl class="review-plan-levels">
        <div><dt>快照价</dt><dd>${escapeHtml(formatNumber(plan.snapshot_price))}</dd></div>
        <div><dt>目标</dt><dd>${escapeHtml(formatNumber(plan.target_price))}</dd></div>
        <div><dt>止损</dt><dd>${escapeHtml(formatNumber(plan.stop_price))}</dd></div>
        <div><dt>周期</dt><dd>${escapeHtml(plan.horizon_days)}日</dd></div>
      </dl>
      <p><b>触发</b>${escapeHtml(plan.trigger_condition || "--")}</p>
      <p><b>失效</b>${escapeHtml(plan.invalidation_condition || "--")}</p>
      ${evaluationHtml(evaluation)}
      <div class="review-evaluate-row">
        <label for="review-as-of-${escapeHtml(plan.id)}"><span>评估截至日</span><input id="review-as-of-${escapeHtml(plan.id)}" type="date" value="${escapeHtml(asOfValue)}" max="${escapeHtml(shanghaiDateText())}" data-review-as-of="${escapeHtml(plan.id)}" /></label>
        <button type="button" class="mini-button primary" data-review-evaluate="${escapeHtml(plan.id)}">评估</button>
      </div>
      ${historyHtml(plan, history)}
    </article>`;
}

function evaluationHtml(evaluation) {
  if (!evaluation) return `<p class="review-evaluation pending"><b>最新评估</b>尚未评估当前版本</p>`;
  const conclusion = CONCLUSION_LABELS[evaluation.conclusion] || evaluation.conclusion || "待确认";
  const returnText = evaluation.return_pct !== null && evaluation.return_pct !== undefined && Number.isFinite(Number(evaluation.return_pct))
    ? ` · 收益 ${formatNumber(evaluation.return_pct)}%`
    : "";
  return `<p class="review-evaluation ${escapeHtml(evaluation.status || "pending")}"><b>最新评估</b>${escapeHtml(conclusion)}${escapeHtml(returnText)} · 截至 ${escapeHtml(evaluation.as_of || "--")}</p>`;
}

function historyHtml(plan, history) {
  const expanded = Boolean(history?.expanded);
  const hidden = expanded ? "" : " hidden";
  return `<section class="review-history" id="review-history-${escapeHtml(plan.id)}" aria-label="评估历史" aria-live="polite"${hidden}>${historyContentHtml(plan.id, history)}</section>`;
}

function historyContentHtml(planId, history) {
  if (!history || history.phase === "idle") {
    return `<div class="review-history-state"><strong>尚未读取评估历史</strong></div>`;
  }
  if (history.phase === "loading") {
    return `<div class="review-history-state" aria-busy="true"><strong>评估历史加载中</strong></div>`;
  }
  if (history.phase === "error") {
    return `<div class="review-history-state is-unavailable"><strong>评估历史加载失败</strong><span>${escapeHtml(history.error || "请稍后重试")}</span><button type="button" class="mini-button" data-review-history-retry="${escapeHtml(planId)}">重试</button></div>`;
  }
  if (history.phase === "empty" || !Array.isArray(history.items) || !history.items.length) {
    return `<div class="review-history-state"><strong>暂无评估历史</strong><span>完成一次评估后会保留在这里。</span></div>`;
  }
  return `<ol class="review-history-list">${history.items.map(evaluationHistoryItemHtml).join("")}</ol>`;
}

function evaluationHistoryItemHtml(evaluation) {
  const conclusion = CONCLUSION_LABELS[evaluation?.conclusion] || evaluation?.conclusion || "待确认";
  const status = EVALUATION_STATUS_LABELS[evaluation?.status] || evaluation?.status || "未知";
  const returnText = evaluation?.return_pct !== null && evaluation?.return_pct !== undefined && Number.isFinite(Number(evaluation.return_pct))
    ? `${formatNumber(evaluation.return_pct)}%`
    : "--";
  return `
    <li class="review-history-item">
      <span><small>计划版本</small><strong>${escapeHtml(evaluation?.plan_revision || "--")}</strong></span>
      <span><small>截至日</small><strong>${escapeHtml(evaluation?.as_of || "--")}</strong></span>
      <span><small>结论</small><strong>${escapeHtml(conclusion)}</strong></span>
      <span><small>收益</small><strong>${escapeHtml(returnText)}</strong></span>
      <span><small>状态</small><strong>${escapeHtml(status)}</strong></span>
    </li>`;
}

function reviewPlanPayload(state, plan, symbol) {
  const targetPrice = positiveNumber("reviewTarget", "请输入有效目标价");
  const stopPrice = positiveNumber("reviewStop", "请输入有效止损价");
  const snapshot = plan ? null : selectedSnapshot(state);
  const entryPrice = Number(plan?.snapshot_price ?? snapshot?.price);
  if (!Number.isFinite(entryPrice) || !(targetPrice > entryPrice && entryPrice > stopPrice)) {
    throw new Error("价格需满足：目标价 > 快照价 > 止损价");
  }
  const payload = {
    hypothesis: requiredValue("reviewHypothesis", "请输入研究假设"),
    trigger_condition: requiredValue("reviewTrigger", "请输入触发条件"),
    invalidation_condition: requiredValue("reviewInvalidation", "请输入失效条件"),
    target_price: targetPrice,
    stop_price: stopPrice,
    horizon_days: integerInRange("reviewHorizon", 1, 60, "观察周期应为1到60日"),
  };
  if (plan) return payload;
  if (!snapshot) throw new Error("请选择可复盘的建议快照");
  if (snapshot.symbol && !sameSymbol(snapshot.symbol, symbol)) {
    throw new Error("建议快照不存在或已切换股票");
  }
  return { ...payload, advice_id: Number(snapshot.id), symbol, evidence_refs: [] };
}

function renderSnapshotOptions(state) {
  const select = $("reviewAdviceId");
  if (!select) return;
  const snapshots = state.adviceReviewSnapshots || [];
  const planned = new Set((state.adviceReviewDetails || []).map((item) => Number(item?.plan?.advice_id)));
  const previous = Number(select.value);
  select.innerHTML = snapshots.length
    ? snapshots.map((item) => snapshotOption(item, planned.has(Number(item.id)))).join("")
    : `<option value="">暂无可用建议快照</option>`;
  const available = snapshots.find((item) => !planned.has(Number(item.id)) && Number(item.id) === previous)
    || snapshots.find((item) => !planned.has(Number(item.id)));
  select.value = available ? String(available.id) : "";
  select.disabled = Boolean(state.adviceReviewEditingPlanId) || !available;
  const submit = $("reviewPlanSubmit");
  if (submit && !state.adviceReviewEditingPlanId) submit.disabled = !available;
}

function snapshotOption(item, planned) {
  const label = `${item.market_time || item.created_at || "--"} · ${item.action || "建议"}${planned ? " · 已建计划" : ""}`;
  return `<option value="${escapeHtml(item.id)}"${planned ? " disabled" : ""}>${escapeHtml(label)}</option>`;
}

function applySelectedSnapshotDefaults(state, { preserveText }) {
  const snapshot = selectedSnapshot(state);
  if (!snapshot) return false;
  const entry = Number(snapshot.price ?? state.adviceReviewAnalysis?.quote?.price);
  if (!Number.isFinite(entry) || entry <= 0) return false;
  const resistance = Number(snapshot.resistance ?? state.adviceReviewAnalysis?.resistance);
  const support = Number(snapshot.support ?? state.adviceReviewAnalysis?.support);
  setValue("reviewTarget", resistance > entry ? resistance : roundedPrice(entry * 1.05));
  setValue("reviewStop", support > 0 && support < entry ? support : roundedPrice(entry * 0.95));
  if (!preserveText || !valueOf("reviewHypothesis")) {
    setValue("reviewHypothesis", snapshot.summary || snapshot.reason || "当前建议在观察周期内得到价格验证");
    setValue("reviewTrigger", `价格确认当前建议，快照价 ${formatNumber(entry)}`);
    setValue("reviewInvalidation", "价格触及止损位或原结论依据失效");
  }
  return true;
}

function selectedSnapshot(state) {
  const adviceId = Number(valueOf("reviewAdviceId"));
  return (state.adviceReviewSnapshots || []).find((item) => Number(item.id) === adviceId) || null;
}

function editingPlan(state) {
  const planId = Number(state.adviceReviewEditingPlanId);
  const detail = (state.adviceReviewDetails || []).find((item) => Number(item?.plan?.id) === planId);
  return detail?.plan || null;
}

function reviewDetail(state, planId) {
  const key = planKey(planId);
  if (!key) return null;
  return (state.adviceReviewDetails || []).find((item) => planKey(item?.plan?.id) === key) || null;
}

function discardAdviceReviewPlanState(state, planId) {
  const key = planKey(planId);
  if (!key) return;
  nextPlanSequence(state, "adviceReviewEvaluationSeqByPlan", planId);
  state.adviceReviewDetails = (state.adviceReviewDetails || []).filter(
    (item) => planKey(item?.plan?.id) !== key
  );
  if (state.adviceReviewHistories && typeof state.adviceReviewHistories === "object") {
    delete state.adviceReviewHistories[key];
  }
  if (state.adviceReviewAsOfByPlan && typeof state.adviceReviewAsOfByPlan === "object") {
    delete state.adviceReviewAsOfByPlan[key];
  }
  if (Number(state.adviceReviewEditingPlanId) === Number(planId)) {
    state.adviceReviewEditingPlanId = null;
    setReviewFormMode(null);
  }
}

function prepareReviewSymbolState(state, symbol) {
  const owner = normalizedSymbol(symbol);
  if (state.adviceReviewHistorySymbol === owner) return;
  state.adviceReviewHistorySymbol = owner;
  state.adviceReviewHistories = {};
  state.adviceReviewAsOfByPlan = {};
  state.adviceReviewEvaluationSeqByPlan = {};
  state.adviceReviewHistoryEpoch = Number(state.adviceReviewHistoryEpoch || 0) + 1;
}

function resetReviewHistories(state) {
  state.adviceReviewHistories = {};
  state.adviceReviewHistoryEpoch = Number(state.adviceReviewHistoryEpoch || 0) + 1;
}

function reviewHistoryRecord(state, planId) {
  const key = planKey(planId);
  if (!key) return null;
  if (!state.adviceReviewHistories || typeof state.adviceReviewHistories !== "object") {
    state.adviceReviewHistories = {};
  }
  if (!state.adviceReviewHistories[key]) {
    state.adviceReviewHistories[key] = {
      phase: "idle",
      expanded: false,
      error: "",
      items: [],
      sequence: 0,
    };
  }
  return state.adviceReviewHistories[key];
}

function mergeEvaluationIntoLoadedHistory(state, planId, evaluation) {
  const key = planKey(planId);
  const history = key ? state.adviceReviewHistories?.[key] : null;
  if (!history || !["loading", "ready", "empty"].includes(history.phase)) return;
  history.items = mergeEvaluationItems([evaluation], history.items);
  if (history.phase !== "loading") history.phase = "ready";
}

function mergeEvaluationItems(primary, retained = []) {
  const merged = [];
  const seen = new Set();
  [...(Array.isArray(primary) ? primary : []), ...(Array.isArray(retained) ? retained : [])].forEach((item) => {
    const identity = evaluationIdentity(item);
    if (!item || seen.has(identity)) return;
    seen.add(identity);
    merged.push(item);
  });
  return merged.sort((left, right) => evaluationSortKey(right).localeCompare(evaluationSortKey(left)));
}

function evaluationIdentity(item) {
  if (Number.isSafeInteger(Number(item?.id)) && Number(item.id) > 0) return `id:${Number(item.id)}`;
  return [item?.plan_revision, item?.as_of, item?.rule_version].map((value) => String(value || "")).join(":");
}

function evaluationSortKey(item) {
  return `${String(item?.evaluated_at || item?.as_of || "").padEnd(24, " ")}:${String(Number(item?.id) || 0).padStart(12, "0")}`;
}

function nextPlanSequence(state, field, planId) {
  const key = planKey(planId);
  if (!state[field] || typeof state[field] !== "object") state[field] = {};
  const sequence = Number(state[field][key] || 0) + 1;
  state[field][key] = sequence;
  return sequence;
}

function evaluationRequestIsCurrent(state, plan, sequence, symbol, options) {
  const key = planKey(plan.id);
  const currentPlan = reviewDetail(state, plan.id)?.plan;
  return (
    state.adviceReviewEvaluationSeqByPlan?.[key] === sequence
    && sameSymbol(state.symbol, symbol)
    && Number(currentPlan?.revision) === Number(plan.revision)
    && (!options.isCurrent || options.isCurrent())
  );
}

function historyRequestIsCurrent(state, plan, history, sequence, epoch, symbol, options) {
  const key = planKey(plan.id);
  return (
    state.adviceReviewHistoryEpoch === epoch
    && state.adviceReviewHistorySymbol === normalizedSymbol(symbol)
    && state.adviceReviewHistories?.[key] === history
    && history.sequence === sequence
    && sameSymbol(state.symbol, symbol)
    && Boolean(reviewDetail(state, plan.id))
    && (!options.isCurrent || options.isCurrent())
  );
}

function planKey(planId) {
  const id = Number(planId);
  return Number.isSafeInteger(id) && id > 0 ? String(id) : "";
}

function normalizedSymbol(symbol) {
  return normalizeUiSymbol(String(symbol || ""));
}

function reviewOwnerSymbol(state, options = {}, fallback = "") {
  return normalizedSymbol(options.symbol || options.context?.symbol || fallback || state.symbol);
}

function sameSymbol(left, right) {
  return Boolean(normalizedSymbol(left)) && normalizedSymbol(left) === normalizedSymbol(right);
}

function reviewAsOfInputValue(planId) {
  const key = planKey(planId);
  return key ? String($(`review-as-of-${key}`)?.value || "").trim() : "";
}

function normalizeEvaluationDate(value, snapshotMarketTime, now = new Date()) {
  const text = String(value || "").trim();
  if (!text) return null;
  if (!strictIsoDate(text)) throw new Error("评估截至日格式无效");
  const today = shanghaiDateText(now);
  if (text > today) throw new Error("评估截至日不能晚于今天");
  const snapshotDate = String(snapshotMarketTime || "").slice(0, 10);
  if (strictIsoDate(snapshotDate) && text < snapshotDate) {
    throw new Error("评估截至日不能早于建议快照日期");
  }
  return text;
}

function strictIsoDate(value) {
  if (!ISO_DATE_PATTERN.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function shanghaiAsOfTimestamp(value, now) {
  if (!value || value === shanghaiDateText(now)) return null;
  // Offset-free datetimes are interpreted as Shanghai market time by the backend.
  return `${value}T23:59:59`;
}

function resolvedNow(value) {
  const current = value === undefined
    ? new Date()
    : new Date(value instanceof Date ? value.getTime() : value);
  if (Number.isNaN(current.getTime())) throw new Error("当前时间格式无效");
  return current;
}

function shanghaiDateText(value = new Date()) {
  const parts = Object.fromEntries(
    SHANGHAI_DATE_FORMATTER.formatToParts(value).map((part) => [part.type, part.value])
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function setReviewFormMode(plan) {
  const select = $("reviewAdviceId");
  const submit = $("reviewPlanSubmit");
  const cancel = $("reviewPlanCancel");
  if (select) select.disabled = Boolean(plan);
  if (submit) {
    submit.textContent = plan ? "更新计划" : "建立计划";
    submit.disabled = false;
  }
  if (cancel) cancel.hidden = !plan;
}

function reviewReadIsCurrent(state, sequence, symbol, options) {
  return state.adviceReviewReadSeq === sequence && reviewOwnerIsCurrent(state, symbol, options);
}

function reviewOwnerIsCurrent(state, symbol, options = {}) {
  const suppliedSymbol = options.symbol || options.context?.symbol;
  if (suppliedSymbol && !sameSymbol(suppliedSymbol, symbol)) return false;
  if (options.signal?.aborted) return false;
  if (typeof options.isCurrent === "function") return Boolean(options.isCurrent());
  const loadSeq = options.loadSeq ?? options.context?.loadSeq;
  if (loadSeq !== undefined && Number(loadSeq) !== Number(state.loadSeq)) return false;
  return sameSymbol(state.symbol, symbol);
}

function renderReviewLoading() {
  const target = $("reviewPlanList");
  if (target) target.innerHTML = `<div class="review-plan-state"><strong>复盘计划加载中</strong></div>`;
}

function renderReviewUnavailable(error) {
  const target = $("reviewPlanList");
  if (target) target.innerHTML = `<div class="review-plan-state is-unavailable"><strong>复盘计划暂不可用</strong><span>${escapeHtml(error?.message || "请稍后重试")}</span></div>`;
}

function setReviewFeedback(message, tone = "") {
  const target = $("reviewPlanFeedback");
  if (!target) return;
  target.textContent = message;
  target.dataset.tone = tone;
  target.hidden = !message;
}

function validSnapshot(item) {
  return Boolean(
    item
    && Number.isSafeInteger(Number(item.id))
    && Number(item.id) > 0
    && item.market_time
    && Number(item.price) > 0
    && item.kline_adjustment_mode === "qfq"
    && item.kline_anchor_date
    && Number(item.kline_anchor_close) > 0
    && !["", "unknown", "legacy"].includes(String(item.kline_data_version || ""))
    && !["", "unknown", "legacy"].includes(String(item.kline_contract_version || ""))
  );
}

function requiredValue(id, message) {
  const value = valueOf(id);
  if (!value) throw new Error(message);
  return value;
}

function positiveNumber(id, message) {
  const number = Number(valueOf(id));
  if (!Number.isFinite(number) || number <= 0) throw new Error(message);
  return number;
}

function integerInRange(id, minimum, maximum, message) {
  const number = Number(valueOf(id));
  if (!Number.isInteger(number) || number < minimum || number > maximum) throw new Error(message);
  return number;
}

function valueOf(id) {
  return String($(id)?.value || "").trim();
}

function setValue(id, value) {
  const element = $(id);
  if (element) element.value = value ?? "";
}

function roundedPrice(value) {
  return Math.round(value * 100) / 100;
}
