import { validateCandidatePage, validateHistory, validatePortfolioDraft, validateStrategyPage } from "./strategy-lab-contracts.js";

export const STRATEGY_PAGE_SIZE = 100;
const FINGERPRINT = /^[0-9a-f]{64}$/;
const EXECUTION_IDENTITY = [
  "execution_id", "strategy_id", "strategy_version", "strategy_fingerprint", "execution_fingerprint",
  "market_scan_run_id", "source_snapshot_digest", "source_snapshot_seal_origin", "cost_rule_fingerprint",
  "rule_version", "data_as_of", "data_date", "kind", "status", "point_in_time", "created_at",
];

export function strategyPageQuery(page) {
  return new URLSearchParams({ page, page_size: STRATEGY_PAGE_SIZE });
}

export async function readBoundedStrategyPage(load, requestedPage) {
  const page = await load(requestedPage);
  if (page.page <= Math.max(1, page.page_count)) return page;
  const corrected = await load(Math.max(1, page.page_count));
  if (corrected.page > Math.max(1, corrected.page_count)) throw new Error("列表在读取期间变化，请刷新重试");
  return corrected;
}

export function validateSavedStrategyPage(value, requestedPage) {
  const page = validateStrategyPage(value);
  validatePage(page, requestedPage, STRATEGY_PAGE_SIZE, "strategy_id");
  return page;
}

export function validateExecutionHistoryPage(value, strategyId, requestedPage) {
  const page = validateHistory(value);
  validatePage(page, requestedPage, STRATEGY_PAGE_SIZE, "execution_id");
  page.items.forEach(item => {
    validateExecutionContext(item);
    if (item.strategy_id !== strategyId) throw new Error("历史执行不属于当前策略");
  });
  return page;
}

export function validateSavedExecution(value, selected) {
  const draft = validatePortfolioDraft(value);
  validateExecutionContext(draft.context);
  if (EXECUTION_IDENTITY.some(field => draft.context[field] !== selected[field])) {
    throw new Error("已保存执行身份与所选历史不一致");
  }
  return draft;
}

export function validateSavedCandidatePage(value, executionId, requestedPage) {
  const page = validateCandidatePage(value);
  if (page.execution_id !== executionId || page.page !== requestedPage) throw new Error("候选分页与所选执行不一致");
  return page;
}

export function syncStrategyPaging(elements, state, busy) {
  syncPage(elements.strategyListPrev, elements.strategyListNext, state.strategyPage, busy || state.strategyListStale);
  syncPage(elements.strategyHistoryPrev, elements.strategyHistoryNext, state.historyPage, busy);
  elements.strategyExecutionLoad.disabled = busy || !state.historyPage?.items.length;
  elements.strategyExecutionSelect.disabled = busy || !state.historyPage?.items.length;
  elements.strategyListRefresh.disabled = busy;
}

export function renderPageStatus(element, page) {
  element.textContent = page ? (page.total ? `第 ${page.page} / ${page.page_count} 页 · 共 ${page.total} 条` : "暂无记录 · 共 0 条") : "尚未读取";
}

function syncPage(previous, next, page, disabled) {
  previous.disabled = disabled || !page || page.page <= 1;
  next.disabled = disabled || !page || page.page >= page.page_count;
}

function validatePage(page, requestedPage, pageSize, identity) {
  if (page.page !== requestedPage || page.page_size !== pageSize
    || !Number.isSafeInteger(page.total) || page.total < 0
    || page.page_count !== Math.ceil(page.total / pageSize)
    || page.items.length > Math.min(pageSize, page.total)
    || new Set(page.items.map(item => item[identity])).size !== page.items.length) {
    throw new Error("列表分页身份或计数异常，请刷新重试");
  }
}

function validateExecutionContext(context) {
  for (const field of ["execution_id", "strategy_id", "strategy_version", "market_scan_run_id"]) {
    if (!Number.isSafeInteger(context[field]) || context[field] <= 0) throw new Error("历史执行身份无效");
  }
  for (const field of ["strategy_fingerprint", "execution_fingerprint", "source_snapshot_digest", "cost_rule_fingerprint"]) {
    if (!FINGERPRINT.test(context[field] || "")) throw new Error("历史执行指纹无效");
  }
  if (context.point_in_time !== true || !["publication", "legacy_backfill"].includes(context.source_snapshot_seal_origin)
    || !["latest_scan", "historical_replay"].includes(context.kind) || !["ready", "no_trade", "blocked"].includes(context.status)) {
    throw new Error("历史执行来源或状态无效");
  }
  for (const field of ["rule_version", "data_as_of", "data_date", "created_at"]) {
    if (typeof context[field] !== "string" || !context[field]) throw new Error("历史执行缺少时点或规则");
  }
}
