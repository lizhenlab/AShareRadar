import { createRequestScope, DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { $ } from "./dom.js";
import { compactErrorMessage } from "./errors.js";
import { assertDuePage } from "./advice-review-contracts.js";

export const DUE_REVIEW_PAGE_SIZE = 50;

export function dueReviewMode() {
  return $("reviewDashboardStatus")?.value === "due";
}

function dueQueueState(state) {
  if (!state.adviceReviewDue) state.adviceReviewDue = {
    page: null, pageFilters: null, phase: "idle", scope: null, sequence: 0,
    active: false, lastAttempt: null, error: "", restart: false,
  };
  return state.adviceReviewDue;
}

function currentDueFilters() {
  const horizon = $("reviewDashboardHorizon")?.value;
  return {
    symbol: String($("reviewDashboardSymbol")?.value || "").trim().toUpperCase(),
    from_date: $("reviewDashboardFrom")?.value || "",
    horizon_days: !horizon || horizon === "all" ? null : Number(horizon),
  };
}

function sameFilters(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function cancelDueReviewQueue(state) {
  const due = state.adviceReviewDue;
  if (!due) return;
  due.sequence += 1;
  due.scope?.abort();
  due.scope = null;
  due.active = false;
  due.phase = due.page ? "ready" : "idle";
}

export function refreshDueReviewQueue(state, onChange, options = {}) {
  if (!dueReviewMode()) { cancelDueReviewQueue(state); onChange(); return Promise.resolve(false); }
  return readDueReviewPage(state, { page: 1, page_size: DUE_REVIEW_PAGE_SIZE, filters: currentDueFilters() }, onChange, options);
}

export function navigateDueReviewQueue(state, direction, onChange) {
  const due = dueQueueState(state);
  if (!dueReviewMode() || due.phase === "loading" || due.restart || !due.page
    || !sameFilters(due.pageFilters, currentDueFilters())) return Promise.resolve(false);
  const page = due.page.page + direction;
  if (page < 1 || page > due.page.page_count) return Promise.resolve(false);
  const attempt = { page, page_size: DUE_REVIEW_PAGE_SIZE, filters: due.pageFilters,
    as_of: due.page.as_of, snapshot_token: due.page.snapshot_token };
  return readDueReviewPage(state, attempt, onChange);
}

export function retryDueReviewQueue(state, onChange) {
  const due = dueQueueState(state);
  if (!dueReviewMode() || due.phase !== "error") return Promise.resolve(false);
  if (due.restart || !sameFilters(due.lastAttempt?.filters, currentDueFilters())) {
    return refreshDueReviewQueue(state, onChange);
  }
  return readDueReviewPage(state, due.lastAttempt, onChange);
}

async function readDueReviewPage(state, attempt, onChange, options = {}) {
  const due = dueQueueState(state);
  const sequence = ++due.sequence;
  const scope = createRequestScope(due.scope, options.signal);
  Object.assign(due, { scope, active: true, phase: "loading", error: "", restart: false, lastAttempt: attempt });
  onChange();
  try {
    if (options.debounce) await waitForDueFilter(scope.signal);
    if (!ownsDueRead(due, sequence, attempt) || scope.signal.aborted || options.isCurrent?.() === false) return false;
    const payload = await fetchJson(duePageUrl(attempt), {
      signal: scope.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS, cache: "no-store",
    });
    const page = assertDuePage(payload, attempt);
    if (!ownsDueRead(due, sequence, attempt) || scope.signal.aborted || options.isCurrent?.() === false) return false;
    Object.assign(due, { page, pageFilters: attempt.filters, phase: "ready" });
    return true;
  } catch (error) {
    if (!ownsDueRead(due, sequence, attempt) || options.isCurrent?.() === false) return false;
    if (isAbortError(error) || scope.signal.aborted) return false;
    due.phase = "error";
    due.restart = Number(error.status) === 409;
    due.error = dueReadError(error);
    return false;
  } finally {
    scope.dispose();
    if (due.sequence === sequence) {
      due.scope = null;
      if (due.phase === "loading") due.phase = due.page ? "ready" : "idle";
      onChange();
    }
  }
}

function ownsDueRead(due, sequence, attempt) {
  return due.sequence === sequence && due.active && dueReviewMode()
    && sameFilters(attempt.filters, currentDueFilters());
}

function duePageUrl(attempt) {
  const query = new URLSearchParams({ page: attempt.page, page_size: attempt.page_size });
  for (const [key, value] of Object.entries(attempt.filters)) if (value !== null && value !== "") query.set(key, value);
  if (attempt.snapshot_token) {
    query.set("as_of", attempt.as_of);
    query.set("snapshot_token", attempt.snapshot_token);
  }
  return `/api/reviews/due?${query}`;
}

function waitForDueFilter(signal) {
  return new Promise(resolve => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener("abort", finish); resolve(); };
    const timer = setTimeout(finish, 250);
    signal.addEventListener("abort", finish, { once: true });
    if (signal.aborted) finish();
  });
}

function dueReadError(error) {
  if (Number(error.status) === 409) return "到期队列已变化，请从首页重新读取；已保留上次成功结果。";
  const reason = Number(error.status) >= 500 ? "到期队列暂不可用" : compactErrorMessage(error.message || "到期队列读取失败");
  return `${reason}；请重试，已保留上次成功结果。`;
}

export function renderDueReviewQueue(state, renderRow) {
  const active = dueReviewMode();
  const controls = $("reviewDueControls");
  if (controls) controls.hidden = !active;
  if (!active) return false;
  const due = dueQueueState(state);
  const target = $("reviewDashboardQueue");
  if (target) {
    target.innerHTML = due.page?.items.length ? due.page.items.map(renderRow).join("") : dueEmptyHtml(due);
    target.setAttribute?.("aria-busy", String(due.phase === "loading"));
  }
  renderDuePaging(due);
  renderDueFeedback(due);
  return true;
}

function dueEmptyHtml(due) {
  const current = sameFilters(due.pageFilters, currentDueFilters());
  const empty = due.phase === "ready" && current && due.page?.total === 0;
  const message = empty ? "没有符合筛选条件的到期计划"
    : due.phase === "loading" ? "正在读取到期队列" : "到期队列暂不可用";
  return `<div class="review-plan-state"><strong>${message}</strong><span>可调整筛选或使用到期队列刷新与重试。</span></div>`;
}

function renderDuePaging(due) {
  const page = due.page;
  const busy = due.phase === "loading";
  const stale = !sameFilters(due.pageFilters, currentDueFilters());
  const blocked = busy || due.restart || stale || !page;
  const previous = $("reviewDuePrev"), next = $("reviewDueNext"), refresh = $("reviewDueRefresh");
  if (previous) previous.disabled = blocked || page.page <= 1;
  if (next) next.disabled = blocked || page.page >= page.page_count;
  if (refresh) refresh.disabled = busy;
  const status = $("reviewDuePageStatus");
  if (status) status.textContent = page ? `${stale ? "上次筛选结果 · " : ""}第 ${page.page} / ${Math.max(1, page.page_count)} 页 · 本筛选共 ${page.total} 个到期计划 · 截至 ${page.as_of}` : "尚未读取到期队列";
}

function renderDueFeedback(due) {
  const retry = $("reviewDueRetry"), feedback = $("reviewDueFeedback");
  if (retry) { retry.hidden = due.phase !== "error"; retry.textContent = due.restart ? "从首页重新读取" : "重试本次读取"; }
  if (!feedback) return;
  const stale = due.page && !sameFilters(due.pageFilters, currentDueFilters());
  feedback.textContent = due.phase === "error" ? due.error : stale ? "正在更新筛选；下方保留上次成功结果。" : "";
  feedback.hidden = !feedback.textContent;
}
