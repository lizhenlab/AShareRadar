import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { auditTimestampEpoch, formatAuditTimestamp } from "./audit-time.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";
import { normalizeUiSymbol } from "./symbols.js";
import {
  cancelDueReviewQueue, dueReviewMode, navigateDueReviewQueue, refreshDueReviewQueue,
  renderDueReviewQueue, retryDueReviewQueue,
} from "./advice-review-due.js";
import {
  assertEvaluation,
  assertReviewBatch,
  assertReviewDetail,
  assertReviewPlan,
  assertReviewSummary,
  sameReviewIdentity,
  validAdviceSnapshot,
} from "./advice-review-contracts.js";

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
const REVIEW_PAGE_SIZE = 20;
const REVIEW_DASHBOARD_PAGE_SIZE = 100;
const REVIEW_DASHBOARD_MAX_PAGES = 100;
const REVIEW_BATCH_TIMEOUT_MS = 120000;
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
  const append = Boolean(options.append);
  const offset = append ? Number(state.adviceReviewNextOffset || 0) : 0;
  if (!append) {
    setReviewPageFeedback();
    resetReviewHistories(state);
    renderReviewLoading();
  }
  try {
    const offsetQuery = offset > 0 ? `&offset=${offset}` : "";
    const details = await fetchJson(
      `/api/reviews?symbol=${encodeURIComponent(symbol)}&limit=${REVIEW_PAGE_SIZE}${offsetQuery}`,
      { signal: options.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS }
    );
    if (!reviewReadIsCurrent(state, sequence, symbol, options)) return false;
    if (!Array.isArray(details)) throw new TypeError("复盘计划格式异常");
    details.forEach((detail) => assertReviewDetail(detail, symbol));
    refreshPinnedReviewDetail(state, details);
    updateReviewPageCursor(state, details, offset, append);
    const confirmedDetails = mergeConfirmedReviewPlan(details, options.confirmedPlan, pinnedReviewDetail(state)?.plan.id);
    state.adviceReviewDetails = append ? mergeReviewDetails(state.adviceReviewDetails, confirmedDetails) : confirmedDetails;
    state.adviceReviewHasMore = details.length === REVIEW_PAGE_SIZE;
    setReviewPageFeedback();
    renderAdviceReviewDetails(state.adviceReviewDetails, state);
    refreshReviewSnapshotForm(state);
    return true;
  } catch (error) {
    if (isAbortError(error) || !reviewReadIsCurrent(state, sequence, symbol, options)) return false;
    if (append && hasReviewPageForSymbol(state, symbol)) {
      setReviewPageFeedback(`加载更早计划失败，已保留当前列表；请再次点击“加载更早计划”重试。${reviewErrorMessage(error, "")}`);
      return false;
    }
    if (pinnedReviewDetail(state)) {
      renderAdviceReviewDetails(state.adviceReviewDetails || [], state);
      setReviewFeedback("普通计划列表暂不可用，已保留定位计划；请刷新页面重试列表", "error");
    } else renderReviewUnavailable(error);
    return false;
  }
}

function hasReviewPageForSymbol(state, symbol) {
  const details = state.adviceReviewDetails;
  return Array.isArray(details) && details.length > 0
    && details.every((detail) => sameSymbol(detail?.plan?.symbol, symbol));
}

function setReviewPageFeedback(message = "") {
  const target = $("reviewPlanPageFeedback");
  if (!target) return;
  target.textContent = message;
  target.hidden = !message;
}

export async function loadMoreAdviceReviews(state, options = {}) {
  if (!state.adviceReviewHasMore) return false;
  return loadAdviceReviews(state, { ...options, append: true });
}

export async function loadAdviceReviewPlan(state, planId, options = {}) {
  const key = planKey(planId);
  const symbol = reviewOwnerSymbol(state, options);
  if (!key || !symbol || !reviewOwnerIsCurrent(state, symbol, options)) return false;
  const sequence = Number(state.adviceReviewPlanReadSeq || 0) + 1;
  state.adviceReviewPlanReadSeq = sequence;
  state.adviceReviewPlanReadId = key;
  prepareReviewSymbolState(state, symbol);
  setReviewFeedback(`正在定位计划 #${key}`, "");
  setDashboardFeedback(`正在定位计划 #${key}`, "");
  const isCurrent = () => state.adviceReviewPlanReadSeq === sequence
    && reviewOwnerIsCurrent(state, symbol, options);
  try {
    const detail = await fetchJson(`/api/reviews/plans/${encodeURIComponent(key)}`, {
      signal: options.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (!isCurrent()) return false;
    assertReviewDetail(detail, symbol);
    if (planKey(detail.plan.id) !== key) throw new TypeError("定位计划身份与请求不一致");
    const existing = reviewDetail(state, key);
    if (Number(existing?.plan.revision || 0) > Number(detail.plan.revision)) throw new Error("计划版本已更新，请重试读取");
    if (existing && Number(existing.plan.revision) === Number(detail.plan.revision)
      && !sameReviewIdentity(existing.plan, detail.plan)) throw new Error("定位计划同版本摘要或参数不一致");
    state.adviceReviewPinnedDetail = detail;
    state.adviceReviewPinnedReadOnly = options.contextAvailable === false;
    syncPinnedReviewForm(state, key);
    renderAdviceReviewDetails(state.adviceReviewDetails || [], state);
    const contextNote = options.contextAvailable === false ? "；行情暂不可用，仅查看本地冻结计划；重新点击“查看计划”可重试行情" : "";
    const message = `已定位计划 #${key} · 当前版本 ${detail.plan.revision}${contextNote}`;
    setReviewFeedback(message, "ok");
    setDashboardFeedback(message, "ok");
    revealReviewPlan(key);
    return true;
  } catch (error) {
    if (isAbortError(error) || !isCurrent()) return false;
    const message = Number(error?.status) === 404 ? "计划不存在或已归档" : reviewErrorMessage(error, "计划读取暂不可用");
    setReviewFeedback(`计划 #${key}：${message}；请重新点击“查看计划”重试`, "error");
    setDashboardFeedback(`计划 #${key}：${message}；请重新点击“查看计划”重试`, "error");
    return false;
  }
}

function pinnedReviewDetail(state) {
  const detail = state?.adviceReviewPinnedDetail;
  const symbol = state?.adviceReviewHistorySymbol || state?.symbol;
  return detail?.plan && sameSymbol(detail.plan.symbol, symbol) ? detail : null;
}

function pinnedReviewIsReadOnly(state, planId) {
  return state?.adviceReviewPinnedReadOnly === true
    && planKey(state.adviceReviewPinnedDetail?.plan.id) === planKey(planId);
}

function invalidatePlanLookup(state, planId) {
  if (state.adviceReviewPlanReadId !== planKey(planId)) return;
  state.adviceReviewPlanReadSeq = Number(state.adviceReviewPlanReadSeq || 0) + 1;
}

function syncPinnedReviewForm(state, planId) {
  if (state.adviceReviewPinnedReadOnly && Number(state.adviceReviewEditingPlanId) === Number(planId)) {
    cancelAdviceReviewEdit(state);
  }
  refreshReviewSnapshotForm(state);
  if (state.adviceReviewPinnedReadOnly && $("reviewPlanSubmit")) $("reviewPlanSubmit").disabled = true;
}

function refreshPinnedReviewDetail(state, details) {
  const pinned = pinnedReviewDetail(state);
  if (!pinned) return;
  const latest = details.find((detail) => planKey(detail.plan.id) === planKey(pinned.plan.id));
  if (!latest || Number(latest.plan.revision) < Number(pinned.plan.revision)) return;
  if (Number(latest.plan.revision) === Number(pinned.plan.revision) && !sameReviewIdentity(latest.plan, pinned.plan)) {
    throw new TypeError("定位计划同版本摘要不一致");
  }
  state.adviceReviewPinnedDetail = latest;
}

function revealReviewPlan(key) {
  const item = $("reviewPlanList")?.querySelector?.(`[data-review-plan="${key}"]`);
  if (!item) return;
  const toggle = item.closest?.(".advice-review-panel")?.querySelector?.(".layout-collapse-toggle");
  if (toggle?.getAttribute?.("aria-expanded") === "false") toggle.click();
  item.setAttribute("tabindex", "-1");
  item.focus?.({ preventScroll: true });
  item.scrollIntoView?.({ block: "start", behavior: "smooth" });
}

export async function loadAdviceReviewDashboard(state, options = {}) {
  const sequence = Number(state.adviceReviewDashboardSeq || 0) + 1;
  state.adviceReviewDashboardSeq = sequence;
  renderAdviceReviewDashboardLoading();
  const dueRead = dueReviewMode() ? refreshAdviceReviewDue(state, options) : Promise.resolve(false);
  const results = await Promise.allSettled([
      fetchJson("/api/reviews/summary", { signal: options.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS }),
      loadAllAdviceReviewDetails(options),
  ]);
  if (state.adviceReviewDashboardSeq !== sequence || options.signal?.aborted || options.isCurrent?.() === false) return false;
  const dashboard = dashboardSettledResults(results);
  state.adviceReviewDashboardSummary = dashboard.summary;
  state.adviceReviewDashboardDetails = dashboard.details;
  state.adviceReviewDashboardFailures = dashboard.failures;
  renderDashboardFailures(dashboard.failures);
  renderAdviceReviewDashboard(state);
  const dueAvailable = await dueRead;
  return state.adviceReviewDashboardSeq === sequence && !options.signal?.aborted && options.isCurrent?.() !== false
    && (dashboard.availableCount > 0 || dueAvailable);
}

function dashboardSettledResults(results) {
  const failures = {};
  const summary = settledDashboardValue(results[0], assertReviewSummary, "统计", failures);
  const details = settledDashboardValue(results[1], (value) => value, "计划列表", failures);
  return {
    summary,
    details,
    failures,
    availableCount: [summary, details].filter((value) => value !== null).length,
  };
}

function settledDashboardValue(result, validator, label, failures) {
  if (result?.status !== "fulfilled") {
    failures[label] = reviewErrorMessage(result?.reason, "请稍后重试");
    return null;
  }
  try {
    return validator(result.value);
  } catch (error) {
    failures[label] = reviewErrorMessage(error, `${label}格式异常`);
    return null;
  }
}

async function loadAllAdviceReviewDetails(options) {
  const details = new Map();
  let offset = 0;
  for (let pageIndex = 0; pageIndex < REVIEW_DASHBOARD_MAX_PAGES; pageIndex += 1) {
    const offsetQuery = offset ? `&offset=${offset}` : "";
    const page = await fetchJson(`/api/reviews?limit=${REVIEW_DASHBOARD_PAGE_SIZE}${offsetQuery}`, {
      signal: options.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (!Array.isArray(page)) throw new TypeError("全局复盘计划格式异常");
    page.forEach((detail) => assertReviewDetail(detail));
    const previousSize = details.size;
    page.forEach((detail) => details.set(Number(detail.plan.id), detail));
    if (page.length < REVIEW_DASHBOARD_PAGE_SIZE) break;
    if (details.size === previousSize) throw new TypeError("全局复盘分页没有前进");
    offset += page.length;
    if (pageIndex === REVIEW_DASHBOARD_MAX_PAGES - 1) throw new TypeError("全局复盘计划超过展示上限");
  }
  return Array.from(details.values());
}

export async function evaluateDueAdviceReviews(state, options = {}) {
  const view = reviewBatchViewKey(state);
  const isCurrent = () => !options.signal?.aborted && options.isCurrent?.() !== false && reviewBatchViewKey(state) === view;
  const before = reviewDashboardReadIdentity(state);
  const result = assertReviewBatch(await fetchJson("/api/reviews/evaluate-due?limit=100", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
    timeoutMs: REVIEW_BATCH_TIMEOUT_MS, signal: options.signal,
  }));
  if (!isCurrent() || reviewDashboardReadIdentity(state) !== before) return result;
  const readback = loadAdviceReviewDashboard(state, { ...options, isCurrent });
  const reading = reviewDashboardReadIdentity(state);
  await readback;
  if (isCurrent() && reviewDashboardReadIdentity(state) === reading) renderReviewBatchReceipt(state, result);
  return result;
}

function reviewBatchViewKey(state) {
  return JSON.stringify([state.symbol, state.loadSeq, state.primaryView, state.workspaceView,
    ...["reviewDashboardStatus", "reviewDashboardSymbol", "reviewDashboardFrom", "reviewDashboardHorizon"].map(valueOf)]);
}

function reviewDashboardReadIdentity(state) {
  return `${state.adviceReviewDashboardSeq || 0}:${state.adviceReviewDue?.sequence || 0}`;
}

function renderReviewBatchReceipt(state, result) {
  const unavailable = result.insufficient_count + result.pending_count;
  const receipt = result.attempted_count
    ? `全局本次已处理 ${result.attempted_count} 条到期计划，正式完成 ${result.evaluated_count} 条，证据不足或待成熟 ${unavailable} 条，失败 ${result.failed_count} 条`
    : "全局本次没有到期计划";
  const failures = Object.entries(state.adviceReviewDashboardFailures || {}).map(([label, message]) => `${label}：${message}`);
  setDashboardFeedback([receipt, ...failures].join("；"), result.failed_count || unavailable || failures.length ? "error" : "ok");
}

export function updateAdviceReviewDashboardFilters(state, options = {}) {
  if (dueReviewMode()) return refreshAdviceReviewDue(state, options);
  cancelDueReviewQueue(state);
  renderAdviceReviewDashboard(state);
  return Promise.resolve(true);
}

export function refreshAdviceReviewDue(state, options = {}) {
  return refreshDueReviewQueue(state, () => {
    if (options.isCurrent?.() !== false) renderAdviceReviewDashboard(state);
  }, options);
}

export function changeAdviceReviewDuePage(state, direction) {
  return navigateDueReviewQueue(state, direction, () => renderAdviceReviewDashboard(state));
}

export function retryAdviceReviewDue(state) {
  return retryDueReviewQueue(state, () => renderAdviceReviewDashboard(state));
}

export function syncAdviceReviewSnapshots(state, items, analysis) {
  const previousIdentity = reviewSnapshotFormIdentity(state);
  state.adviceReviewSnapshots = Array.isArray(items)
    ? items.filter((item) => validAdviceSnapshot(item, state.symbol))
    : [];
  state.adviceReviewAnalysis = analysis || null;
  refreshReviewSnapshotForm(state, { previousIdentity });
}

export function selectAdviceReviewSnapshot(state) {
  if (state.adviceReviewEditingPlanId) return false;
  state.adviceReviewFormEpoch = Number(state.adviceReviewFormEpoch || 0) + 1;
  return applySelectedSnapshotDefaults(state, { preserveText: false });
}

export async function submitAdviceReviewPlan(state, options = {}) {
  if (pinnedReviewDetail(state) && state.adviceReviewPinnedReadOnly
    && (!state.adviceReviewEditingPlanId || pinnedReviewIsReadOnly(state, state.adviceReviewEditingPlanId))) {
    throw new Error("定位计划当前仅供查看，请重新加载行情后编辑");
  }
  const plan = editingPlan(state);
  if (state.adviceReviewEditingPlanId && !plan) throw new Error("编辑计划已不可用，请重新查看计划后编辑");
  const symbol = reviewOwnerSymbol(state, options, plan?.symbol);
  if (!symbol) throw new Error("当前复盘股票无效");
  if (plan && !sameSymbol(plan.symbol, symbol)) throw new Error("复盘计划不存在或已切换股票");
  const payload = reviewPlanPayload(state, plan, symbol);
  const submittedDraft = reviewFormDraft(state);
  const url = plan ? `/api/reviews/plans/${encodeURIComponent(plan.id)}` : "/api/reviews/plans";
  const method = plan ? "PATCH" : "POST";
  const saved = await fetchJson(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  });
  if (!reviewOwnerIsCurrent(state, symbol, options)) return false;
  assertReviewPlan(saved, symbol);
  if (plan && (Number(saved.id) !== Number(plan.id) || Number(saved.revision) <= Number(plan.revision))) {
    throw new TypeError("复盘计划更新响应版本异常");
  }
  invalidatePlanLookup(state, saved.id);
  if (Number(pinnedReviewDetail(state)?.plan.id) === Number(saved.id)) {
    state.adviceReviewPinnedDetail = { plan: saved, latest_evaluation: null };
  }
  state.adviceReviewDetails = mergeConfirmedReviewPlan(state.adviceReviewDetails || [], saved, pinnedReviewDetail(state)?.plan.id);
  const reset = reconcileSavedReviewDraft(state, submittedDraft, saved);
  setReviewFeedback(reset ? (plan ? "复盘计划已更新" : "复盘计划已建立")
    : `复盘计划 #${saved.id} ${plan ? "已更新" : "已建立"}；当前草稿已保留，尚未提交`, "ok");
  await loadAdviceReviews(state, { ...options, symbol, confirmedPlan: saved });
  return saved;
}

export async function deleteAdviceReviewPlan(state, planId, options = {}) {
  const detail = reviewDetail(state, planId);
  if (!detail?.plan) throw new Error("复盘计划不存在或已切换股票");
  const plan = detail.plan;
  const symbol = reviewOwnerSymbol(state, options, plan.symbol);
  if (!sameSymbol(plan.symbol, symbol)) throw new Error("复盘计划不存在或已切换股票");
  if (options.confirm && !options.confirm("归档该复盘计划？归档后将不再显示该计划及评估历史。")) {
    return false;
  }
  const result = await fetchJson(`/api/reviews/plans/${encodeURIComponent(plan.id)}?expected_revision=${encodeURIComponent(plan.revision)}`, {
    method: "DELETE",
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  });
  if (!result?.ok || !result?.removed) throw new TypeError("复盘计划删除结果异常");
  if (!reviewOwnerIsCurrent(state, symbol, options)) return true;

  discardAdviceReviewPlanState(state, plan.id);
  renderAdviceReviewDetails(state.adviceReviewDetails, state);
  refreshReviewSnapshotForm(state);
  setReviewFeedback("复盘计划已归档", "ok");
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
  const evaluation = assertEvaluation(await fetchJson(`/api/reviews/plans/${encodeURIComponent(plan.id)}/evaluate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision: Number(plan.revision), ...(asOf ? { as_of: asOf } : {}) }),
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    signal: options.signal,
  }), plan, { currentRevision: true });
  if (!evaluationRequestIsCurrent(state, plan, sequence, symbol, options)) return false;
  state.adviceReviewDetails = (state.adviceReviewDetails || []).map((detail) =>
    Number(detail?.plan?.id) === Number(plan.id) ? { ...detail, latest_evaluation: evaluation } : detail
  );
  if (Number(pinnedReviewDetail(state)?.plan.id) === Number(plan.id)) {
    state.adviceReviewPinnedDetail = { ...detail, latest_evaluation: evaluation };
  }
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
    evaluations.forEach((evaluation) => assertEvaluation(evaluation, plan));
    history.items = mergeEvaluationItems(evaluations, history.items);
    history.phase = history.items.length ? "ready" : "empty";
    renderAdviceReviewDetails(state.adviceReviewDetails || [], state);
    return true;
  } catch (error) {
    if (isAbortError(error) || !historyRequestIsCurrent(state, plan, history, sequence, epoch, symbol, options)) {
      return false;
    }
    history.phase = "error";
    history.error = reviewErrorMessage(error, "请稍后重试");
    renderAdviceReviewDetails(state.adviceReviewDetails || [], state);
    return false;
  }
}

export function beginAdviceReviewEdit(state, planId) {
  const detail = reviewDetail(state, planId);
  if (!detail?.plan) return false;
  const plan = detail.plan;
  state.adviceReviewFormEpoch = Number(state.adviceReviewFormEpoch || 0) + 1;
  state.adviceReviewEditingPlanId = plan.id;
  state.adviceReviewEditingPlanSnapshot = structuredClone(plan);
  setValue("reviewAdviceId", String(plan.advice_id));
  setValue("reviewHypothesis", plan.hypothesis);
  setValue("reviewTrigger", plan.trigger_condition);
  setValue("reviewInvalidation", plan.invalidation_condition);
  setValue("reviewTarget", plan.target_price);
  setValue("reviewStop", plan.stop_price);
  setValue("reviewHorizon", plan.horizon_days);
  syncAdviceReviewFormControls(state);
  setReviewFeedback("");
  $("reviewHypothesis")?.focus?.({ preventScroll: true });
  return true;
}

export function cancelAdviceReviewEdit(state) {
  state.adviceReviewFormEpoch = Number(state.adviceReviewFormEpoch || 0) + 1;
  state.adviceReviewEditingPlanId = null;
  state.adviceReviewEditingPlanSnapshot = null;
  syncAdviceReviewFormControls(state);
  applySelectedSnapshotDefaults(state, { preserveText: false });
  setReviewFeedback("");
}

export function syncAdviceReviewFormControls(state) {
  setReviewFormMode(editingPlan(state));
  renderSnapshotOptions(state);
  const busy = Boolean(state.adviceReviewSubmitOwner?.isCurrent());
  $("reviewPlanForm")?.setAttribute("aria-busy", String(busy));
  if (busy) {
    const submit = $("reviewPlanSubmit");
    if (submit) {
      submit.disabled = true;
      submit.textContent = "保存中";
    }
  }
}

function reviewFormDraft(state) {
  const plan = editingPlan(state);
  return {
    epoch: Number(state.adviceReviewFormEpoch || 0),
    planId: plan ? Number(plan.id) : null,
    adviceId: Number(plan?.advice_id || valueOf("reviewAdviceId")),
    values: ["reviewHypothesis", "reviewTrigger", "reviewInvalidation", "reviewTarget", "reviewStop", "reviewHorizon"]
      .map((id) => String($(id)?.value ?? "")),
  };
}

function reconcileSavedReviewDraft(state, submitted, saved) {
  const current = reviewFormDraft(state);
  if (JSON.stringify(current) === JSON.stringify(submitted)) {
    state.adviceReviewEditingPlanId = null;
    state.adviceReviewEditingPlanSnapshot = null;
    setReviewFormMode(null);
    refreshReviewSnapshotForm(state, { reset: true });
    return true;
  }
  const samePlan = current.planId === Number(saved.id);
  const continuedCreate = submitted.planId === null && current.planId === null
    && current.epoch === submitted.epoch && current.adviceId === submitted.adviceId;
  if ((samePlan && Number(editingPlan(state)?.revision) <= Number(saved.revision)) || continuedCreate) {
    // The acknowledged revision is the next PATCH baseline; input fields remain the user's draft.
    state.adviceReviewEditingPlanId = saved.id;
    state.adviceReviewEditingPlanSnapshot = structuredClone(saved);
    syncAdviceReviewFormControls(state);
  }
  return false;
}

export function renderAdviceReviewDetails(details, state = null) {
  const target = $("reviewPlanList");
  if (!target) return;
  const focusedPlan = focusedReviewPlanKey(target);
  const pinned = pinnedReviewDetail(state);
  const symbol = state?.adviceReviewHistorySymbol;
  const scoped = symbol ? details.filter((detail) => sameSymbol(detail?.plan?.symbol, symbol)) : details;
  const rows = pinned ? [pinned, ...scoped.filter((detail) => Number(detail?.plan?.id) !== Number(pinned.plan.id))] : scoped;
  const boundary = state?.adviceReviewPinnedReadOnly ? "行情未完成，仅查看冻结计划；请从全局看板重新点击查看计划重试。" : "置顶展示，不改变普通列表分页顺序。";
  const selected = pinned ? `<div class="review-plan-state" role="status"><strong>定位计划 #${escapeHtml(pinned.plan.id)} · ${escapeHtml(pinned.plan.symbol)}</strong><span>${boundary}</span></div>` : "";
  target.innerHTML = rows.length
    ? selected + rows.map((detail) => reviewDetailHtml(detail, state)).join("")
    : `<div class="review-plan-state"><strong>暂无复盘计划</strong><span>可从当前股票的保留建议快照建立计划。</span></div>`;
  restoreReviewPlanFocus(target, focusedPlan);
  const loadMore = $("reviewPlanLoadMore");
  if (loadMore) loadMore.hidden = !state?.adviceReviewHasMore || Boolean(pinned && state.adviceReviewPinnedReadOnly);
}

function focusedReviewPlanKey(target) {
  const active = document.activeElement;
  return target.contains?.(active) ? planKey(active?.dataset?.reviewPlan) : null;
}

function restoreReviewPlanFocus(target, key) {
  const item = key ? target.querySelector?.(`[data-review-plan="${key}"]`) : null;
  if (!item) return;
  item.setAttribute("tabindex", "-1");
  item.focus?.({ preventScroll: true });
}

function mergeReviewDetails(existing, incoming) {
  const merged = new Map((Array.isArray(existing) ? existing : []).map((item) => [Number(item?.plan?.id), item]));
  for (const item of incoming) merged.set(Number(item?.plan?.id), item);
  return Array.from(merged.values());
}

function mergeConfirmedReviewPlan(details, plan, pinnedPlanId = null) {
  if (!plan) return details;
  const existing = details.find((detail) => Number(detail.plan.id) === Number(plan.id));
  if (!existing && Number(plan.id) === Number(pinnedPlanId)) return details;
  if (existing && Number(existing.plan.revision) >= Number(plan.revision)) return details;
  return mergeReviewDetails(details, [{ plan, latest_evaluation: null }]);
}

function updateReviewPageCursor(state, details, offset, append) {
  const previous = append ? (state.adviceReviewPagePlanIds || []) : [];
  state.adviceReviewPagePlanIds = [...new Set([...previous, ...details.map((detail) => Number(detail.plan.id))])];
  state.adviceReviewNextOffset = offset + details.length;
}

function renderAdviceReviewDashboard(state) {
  const summaryTarget = $("reviewDashboardSummary");
  const queueTarget = $("reviewDashboardQueue");
  if (!summaryTarget || !queueTarget) return;
  const summary = state.adviceReviewDashboardSummary;
  const decidedCount = summary ? Number(summary.favorable_count) + Number(summary.unfavorable_count) : 0;
  summaryTarget.innerHTML = summary ? [
    ["计划总数", summary.total_plan_count],
    ["待评估", summary.pending_count],
    ["已评估", summary.evaluated_count],
    ["数据不足", summary.insufficient_count],
    ["有利率分母", decidedCount],
    ["有利比例", dashboardPercent(summary.favorable_rate_pct)],
    ["平均观察期价格变化（毛值）", dashboardPercent(summary.average_return_pct)],
    ["平均MFE", dashboardPercent(summary.average_mfe_pct)],
    ["平均MAE", dashboardPercent(summary.average_mae_pct)],
  ].map(([label, value]) => `<span><small>${escapeHtml(label)}</small><strong>${escapeHtml(value ?? "--")}</strong></span>`).join("")
    : dashboardUnavailableHtml("统计暂不可用");
  if (renderDueReviewQueue(state, item => dashboardDetailHtml(item, item))) return;
  const detailsAvailable = Array.isArray(state.adviceReviewDashboardDetails);
  const rows = detailsAvailable ? filteredDashboardDetails(state) : [];
  queueTarget.innerHTML = !detailsAvailable
    ? dashboardUnavailableHtml("计划列表暂不可用")
    : rows.length
    ? rows.map((detail) => dashboardDetailHtml(detail)).join("")
    : `<div class="review-plan-state"><strong>没有符合筛选条件的计划</strong><span>可调整状态、股票、日期或周期。</span></div>`;
}

function dashboardUnavailableHtml(message) {
  return `<div class="review-plan-state is-unavailable"><strong>${escapeHtml(message)}</strong><span>其他复盘板块仍可继续使用。</span></div>`;
}

function renderDashboardFailures(failures) {
  const messages = Object.entries(failures || {}).map(([label, message]) => `${label}：${message}`);
  setDashboardFeedback(messages.join("；"), messages.length ? "error" : "");
}

function filteredDashboardDetails(state) {
  const filters = {
    status: valueOf("reviewDashboardStatus") || "all",
    symbol: valueOf("reviewDashboardSymbol").toUpperCase(),
    from: valueOf("reviewDashboardFrom"),
    horizon: valueOf("reviewDashboardHorizon"),
  };
  return (state.adviceReviewDashboardDetails || []).filter((detail) => dashboardDetailMatches(detail, filters));
}

function dashboardDetailMatches(detail, filters) {
  const plan = detail?.plan || {};
  if (!dashboardStatusMatches(detail, filters.status)) return false;
  if (filters.symbol && !String(plan.symbol || "").toUpperCase().includes(filters.symbol)) return false;
  if (filters.from && String(plan.snapshot_market_time || "").slice(0, 10) < filters.from) return false;
  return filters.horizon === "all" || String(plan.horizon_days) === filters.horizon;
}

function dashboardStatusMatches(detail, status) {
  if (status === "all") return true;
  return (detail?.latest_evaluation?.status || "pending") === status;
}

function dashboardDetailHtml(detail, due) {
  const plan = detail?.plan || {};
  const evaluation = detail?.latest_evaluation;
  const conclusion = CONCLUSION_LABELS[evaluation?.conclusion] || "等待后续行情";
  const dueText = due ? `到期 ${due.due_date}${due.overdue_trading_days ? ` · 逾期 ${due.overdue_trading_days} 个交易日` : ""}` : `周期 ${plan.horizon_days || "--"} 日`;
  return `
    <article class="review-dashboard-item" data-review-dashboard-plan="${escapeHtml(plan.id)}">
      <span><strong>${escapeHtml(plan.symbol || "--")}</strong><small>${escapeHtml(plan.snapshot_market_time || "--")} · 版本 ${escapeHtml(plan.revision || 1)}</small></span>
      <span><b>${escapeHtml(conclusion)}</b><small>${escapeHtml(dueText)}</small></span>
      <button type="button" class="mini-button" data-review-open-plan="${escapeHtml(plan.id)}" data-review-open-symbol="${escapeHtml(plan.symbol || "")}">查看计划</button>
    </article>`;
}

function dashboardPercent(value) {
  return value === null || value === undefined || !Number.isFinite(Number(value)) ? "--" : `${formatNumber(value)}%`;
}

function renderAdviceReviewDashboardLoading() {
  const summary = $("reviewDashboardSummary");
  const queue = $("reviewDashboardQueue");
  if (summary) summary.innerHTML = `<div class="review-plan-state"><strong>正在读取全局复盘统计</strong></div>`;
  if (queue && !dueReviewMode()) queue.innerHTML = "";
  summary?.setAttribute?.("aria-busy", "true");
  queue?.setAttribute?.("aria-busy", "true");
}

function setDashboardFeedback(message, tone = "") {
  const target = $("reviewDashboardFeedback");
  if (!target) return;
  target.textContent = message;
  target.dataset.tone = tone;
  target.hidden = !message;
  $("reviewDashboardSummary")?.setAttribute?.("aria-busy", "false");
  $("reviewDashboardQueue")?.setAttribute?.("aria-busy", "false");
}

function reviewDetailHtml(detail, state) {
  const plan = detail.plan || {};
  const evaluation = detail.latest_evaluation;
  const key = planKey(plan.id);
  const history = key && state?.adviceReviewHistories?.[key];
  const disabled = pinnedReviewIsReadOnly(state, plan.id) ? " disabled" : "";
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
          <button type="button" class="mini-button primary" data-paper-from-review="${escapeHtml(plan.id)}"${disabled}>加入模拟</button>
          <button type="button" class="mini-button" data-review-edit="${escapeHtml(plan.id)}"${disabled}>编辑</button>
          <button type="button" class="mini-button" data-review-history="${escapeHtml(plan.id)}" aria-expanded="${historyExpanded}" aria-controls="review-history-${escapeHtml(plan.id)}"${disabled}>${historyExpanded ? "收起历史" : "评估历史"}</button>
          <button type="button" class="icon-button" title="归档复盘计划" aria-label="归档复盘计划" data-review-delete="${escapeHtml(plan.id)}"${disabled}>×</button>
        </div>
      </div>
      <p class="review-shadow-boundary"><b>研究属性</b>本地 Research Shadow · 价格路径观察，不代表真实成交或收益 · production_effect=none</p>
      <dl class="review-plan-levels">
        <div><dt>快照价</dt><dd>${escapeHtml(formatNumber(plan.snapshot_price))}</dd></div>
        <div><dt>目标</dt><dd>${escapeHtml(formatNumber(plan.target_price))}</dd></div>
        <div><dt>止损</dt><dd>${escapeHtml(formatNumber(plan.stop_price))}</dd></div>
        <div><dt>周期</dt><dd>${escapeHtml(plan.horizon_days)}日</dd></div>
      </dl>
      <p><b>触发说明</b>${escapeHtml(plan.trigger_condition || "--")}</p>
      <p><b>失效说明</b>${escapeHtml(plan.invalidation_condition || "--")}</p>
      <p class="review-execution-basis"><b>自动评估口径</b>${escapeHtml(executableBasisLabel(plan))}</p>
      ${evidenceRefsHtml(plan.evidence_refs)}
      ${evaluationHtml(evaluation)}
      <div class="review-evaluate-row">
        <label for="review-as-of-${escapeHtml(plan.id)}"><span>评估截至日</span><input id="review-as-of-${escapeHtml(plan.id)}" type="date" value="${escapeHtml(asOfValue)}" max="${escapeHtml(shanghaiDateText())}" data-review-as-of="${escapeHtml(plan.id)}" /></label>
        <button type="button" class="mini-button primary" data-review-evaluate="${escapeHtml(plan.id)}"${disabled}>评估</button>
      </div>
      ${historyHtml(plan, history)}
    </article>`;
}

function evaluationHtml(evaluation) {
  if (!evaluation) return `<p class="review-evaluation pending"><b>最新评估</b>尚未评估当前版本</p>`;
  const conclusion = CONCLUSION_LABELS[evaluation.conclusion] || evaluation.conclusion || "待确认";
  const returnText = evaluation.return_pct !== null && evaluation.return_pct !== undefined && Number.isFinite(Number(evaluation.return_pct))
    ? ` · 收盘价格变化 ${formatNumber(evaluation.return_pct)}%（未计成交/成本/基准）`
    : "";
  return `
    <div class="review-evaluation ${escapeHtml(evaluation.status || "pending")}">
      <p><b>最新评估</b>${escapeHtml(conclusion)}${escapeHtml(returnText)} · 截至 ${escapeHtml(evaluation.as_of || "--")}</p>
      ${evaluationEvidenceHtml(evaluation)}
    </div>`;
}

function executableBasisLabel(plan) {
  const trigger = plan?.trigger_basis === "daily_high_gte_target_price"
    ? "日最高价 ≥ 目标价"
    : plan?.trigger_basis || "未知触发口径";
  const invalidation = plan?.invalidation_basis === "daily_low_lte_stop_price"
    ? "日最低价 ≤ 止损价"
    : plan?.invalidation_basis || "未知失效口径";
  return `${trigger}；${invalidation}`;
}

function evaluationEvidenceHtml(evaluation) {
  const metric = (label, value, suffix = "") => `<span><small>${escapeHtml(label)}</small><strong>${escapeHtml(metricValue(value, suffix))}</strong></span>`;
  return `
    <div class="review-evaluation-evidence" aria-label="评估证据">
      ${metric("目标触达", evaluation.target_hit ? evaluation.target_hit_date || "是" : "否")}
      ${metric("止损触达", evaluation.stop_hit ? evaluation.stop_hit_date || "是" : "否")}
      ${metric("最大有利波动", evaluation.max_favorable_excursion_pct, "%")}
      ${metric("最大不利波动", evaluation.max_adverse_excursion_pct, "%")}
      ${metric("有效后续交易日", evaluation.available_forward_days, "日")}
      ${metric("可见K线", evaluation.visible_bar_count, "根")}
    </div>
    <details class="review-provenance">
      <summary>数据与复权证据</summary>
      <p>快照：${escapeHtml(evaluation.snapshot_adjustment_mode || "unknown")} · ${escapeHtml(evaluation.snapshot_data_version || "unknown")}</p>
      <p>评估：${escapeHtml(evaluation.evaluation_adjustment_mode || "unknown")} · ${escapeHtml(evaluation.evaluation_data_version || "unknown")}</p>
      <p>规则：${escapeHtml(evaluation.rule_version || "unknown")} · 覆盖 ${escapeHtml(evaluation.forward_start_date || "--")} 至 ${escapeHtml(evaluation.forward_end_date || "--")}</p>
    </details>`;
}

function metricValue(value, suffix) {
  if (value === null || value === undefined || value === "") return "--";
  if (typeof value === "number" && Number.isFinite(value)) return `${formatNumber(value)}${suffix}`;
  return `${value}${suffix}`;
}

function evidenceRefsHtml(items) {
  const rows = Array.isArray(items) ? items.filter((item) => item && typeof item === "object").slice(0, 12) : [];
  if (!rows.length) return "";
  return `<details class="review-plan-evidence"><summary>计划证据 ${escapeHtml(rows.length)} 项</summary><div>${rows.map((item) => `<span><small>${escapeHtml(item.id || "证据")}</small><strong>${escapeHtml(metricValue(item.value, ""))}</strong><em>${escapeHtml(item.data_date || "--")} · ${escapeHtml(item.nature || "unknown")} · ${escapeHtml(item.rule_version || "unknown")}</em></span>`).join("")}</div></details>`;
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
  const item = evaluation || {};
  const conclusion = CONCLUSION_LABELS[item.conclusion] || item.conclusion || "待确认";
  const status = EVALUATION_STATUS_LABELS[item.status] || item.status || "未知";
  const returnText = item.return_pct !== null && item.return_pct !== undefined && Number.isFinite(Number(item.return_pct))
    ? `${formatNumber(item.return_pct)}%`
    : "--";
  return `
    <li class="review-history-item">
      <span><small>计划版本</small><strong>${escapeHtml(item.plan_revision || "--")}</strong></span>
      <span><small>尝试 / 规则</small><strong>${escapeHtml(item.attempt || "--")} · ${escapeHtml(item.rule_version || "--")}</strong></span>
      <span><small>截至日</small><strong>${escapeHtml(item.as_of || "--")}</strong></span>
      <span><small>评估时间</small><strong>${escapeHtml(item.evaluated_at || "--")}</strong></span>
      <span><small>结论</small><strong>${escapeHtml(conclusion)}</strong></span>
      <span><small>收盘价格变化（毛值）</small><strong>${escapeHtml(returnText)}</strong></span>
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
    ...(plan ? { expected_revision: Number(plan.revision) } : {}),
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
  const pinned = pinnedReviewDetail(state);
  if (pinned) planned.add(Number(pinned.plan.advice_id));
  const previous = Number(select.value);
  select.innerHTML = snapshots.length
    ? snapshots.map((item) => snapshotOption(item, planned.has(Number(item.id)))).join("")
    : `<option value="">暂无可用建议快照</option>`;
  const plan = editingPlan(state);
  if (plan) {
    if (!snapshots.some((item) => Number(item.id) === Number(plan.advice_id))) {
      select.innerHTML += snapshotOption({ id: plan.advice_id, market_time: plan.snapshot_market_time }, true);
    }
    select.value = String(plan.advice_id);
    select.disabled = true;
    return;
  }
  const available = snapshots.find((item) => !planned.has(Number(item.id)) && Number(item.id) === previous)
    || snapshots.find((item) => !planned.has(Number(item.id)));
  select.value = available ? String(available.id) : "";
  select.disabled = Boolean(state.adviceReviewEditingPlanId) || !available;
  const submit = $("reviewPlanSubmit");
  if (submit && !state.adviceReviewEditingPlanId) submit.disabled = !available || Boolean(pinned && state.adviceReviewPinnedReadOnly);
}

function refreshReviewSnapshotForm(state, { previousIdentity = reviewSnapshotFormIdentity(state), reset = false } = {}) {
  syncAdviceReviewFormControls(state);
  if (state.adviceReviewEditingPlanId) return;
  // Read the current form at response time: typing during a request must survive.
  if (reset || previousIdentity !== reviewSnapshotFormIdentity(state)) {
    state.adviceReviewFormEpoch = Number(state.adviceReviewFormEpoch || 0) + 1;
    applySelectedSnapshotDefaults(state, { preserveText: false });
  }
}

function reviewSnapshotFormIdentity(state) {
  const snapshot = selectedSnapshot(state);
  if (!snapshot) return "";
  const symbol = normalizedSymbol(snapshot.symbol || state.adviceReviewHistorySymbol || state.symbol);
  return `${symbol}:${Number(snapshot.id)}`;
}

function snapshotOption(item, planned) {
  const snapshotTime = item.market_time || formatAuditTimestamp(item.created_at);
  const recordedAt = formatAuditTimestamp(item.created_at);
  const recordSuffix = recordedAt && recordedAt !== snapshotTime ? ` · 记录 ${recordedAt}` : ` · #${item.id}`;
  const label = `${snapshotTime} · ${item.action || "建议"}${recordSuffix}${planned ? " · 已建计划" : ""}`;
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
    setValue("reviewTrigger", `研究备注：关注价格对快照价 ${formatNumber(entry)} 的确认过程`);
    setValue("reviewInvalidation", "研究备注：原结论依据失效时重新审视计划");
  }
  return true;
}

function selectedSnapshot(state) {
  const adviceId = Number(valueOf("reviewAdviceId"));
  return (state.adviceReviewSnapshots || []).find((item) => Number(item.id) === adviceId) || null;
}

function editingPlan(state) {
  const planId = Number(state.adviceReviewEditingPlanId);
  const snapshot = state.adviceReviewEditingPlanSnapshot;
  if (snapshot && Number(snapshot.id) === planId) return snapshot;
  const detail = reviewDetail(state, planId);
  return detail?.plan || null;
}

function reviewDetail(state, planId) {
  const key = planKey(planId);
  if (!key) return null;
  const pinned = pinnedReviewDetail(state);
  if (planKey(pinned?.plan.id) === key) return pinnedReviewIsReadOnly(state, key) ? null : pinned;
  return (state.adviceReviewDetails || []).find((item) => planKey(item?.plan?.id) === key) || null;
}

function discardAdviceReviewPlanState(state, planId) {
  const key = planKey(planId);
  if (!key) return;
  invalidatePlanLookup(state, key);
  state.adviceReviewReadSeq = Number(state.adviceReviewReadSeq || 0) + 1;
  if (planKey(state.adviceReviewPinnedDetail?.plan?.id) === key) {
    state.adviceReviewPinnedDetail = null;
    state.adviceReviewPinnedReadOnly = false;
  }
  nextPlanSequence(state, "adviceReviewEvaluationSeqByPlan", planId);
  if (state.adviceReviewPagePlanIds?.includes(Number(planId))) {
    state.adviceReviewNextOffset = Math.max(0, Number(state.adviceReviewNextOffset || 0) - 1);
    state.adviceReviewPagePlanIds = state.adviceReviewPagePlanIds.filter((id) => id !== Number(planId));
  }
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
    state.adviceReviewEditingPlanSnapshot = null;
    setReviewFormMode(null);
  }
}

function prepareReviewSymbolState(state, symbol) {
  const owner = normalizedSymbol(symbol);
  if (state.adviceReviewHistorySymbol === owner) return;
  if (state.adviceReviewEditingPlanSnapshot && !sameSymbol(state.adviceReviewEditingPlanSnapshot.symbol, owner)) {
    state.adviceReviewEditingPlanId = null;
    state.adviceReviewEditingPlanSnapshot = null;
    setReviewFormMode(null);
  }
  if (state.adviceReviewPinnedDetail && !sameSymbol(state.adviceReviewPinnedDetail.plan.symbol, owner)) {
    state.adviceReviewPinnedDetail = null;
    state.adviceReviewPinnedReadOnly = false;
  }
  state.adviceReviewHistorySymbol = owner;
  state.adviceReviewNextOffset = null;
  state.adviceReviewPagePlanIds = null;
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
  appendUniqueEvaluations(merged, seen, primary);
  appendUniqueEvaluations(merged, seen, retained);
  return merged.sort(compareEvaluations);
}

function appendUniqueEvaluations(merged, seen, items) {
  (Array.isArray(items) ? items : []).forEach((item) => {
    const identity = evaluationIdentity(item);
    if (!item || seen.has(identity)) return;
    seen.add(identity);
    merged.push(item);
  });
}

function compareEvaluations(left, right) {
  const leftItem = left || {};
  const rightItem = right || {};
  const byAsOf = auditTimestampEpoch(String(rightItem.as_of || ""))
    - auditTimestampEpoch(String(leftItem.as_of || ""));
  const byAttempt = Number(rightItem.attempt || 0) - Number(leftItem.attempt || 0);
  const byEvaluated = auditTimestampEpoch(String(rightItem.evaluated_at || ""))
    - auditTimestampEpoch(String(leftItem.evaluated_at || ""));
  return byAsOf || byAttempt || byEvaluated || Number(rightItem.id || 0) - Number(leftItem.id || 0);
}

function evaluationIdentity(item) {
  if (Number.isSafeInteger(Number(item?.id)) && Number(item.id) > 0) return `id:${Number(item.id)}`;
  return [item?.plan_revision, item?.as_of, item?.rule_version].map((value) => String(value || "")).join(":");
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
    && sameReviewIdentity(currentPlan, plan)
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
    && sameReviewIdentity(reviewDetail(state, plan.id)?.plan, plan)
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
  if (target) target.innerHTML = `<div class="review-plan-state is-unavailable"><strong>复盘计划暂不可用</strong><span>${escapeHtml(reviewErrorMessage(error, "请稍后重试"))}</span></div>`;
}

function reviewErrorMessage(error, fallback) {
  return Number(error?.status) >= 500 ? fallback : error?.message || fallback;
}

function setReviewFeedback(message, tone = "") {
  const target = $("reviewPlanFeedback");
  if (!target) return;
  target.textContent = message;
  target.dataset.tone = tone;
  target.hidden = !message;
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
