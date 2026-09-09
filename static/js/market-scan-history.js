import { DEFAULT_REQUEST_TIMEOUT_MS, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import {
  isPublishedMarketScanRun,
  marketScanContractError,
  validateMarketScanRun,
  validateMarketScanRunPage,
} from "./market-scan-contracts.js";
import {
  MARKET_SCAN_TRUSTED_READ_TIMEOUT_MS,
  samePublishedMarketScanRun,
} from "./market-scan-latest-loader.js";

export function createMarketScanHistory(options) {
  const context = { ...options, lastPage: null };
  const controller = {
    abort: () => abortHistory(context),
    changeMode: () => changeHistoryMode(context),
    load: () => loadHistory(context),
    refresh: () => context.scheduleReads(() => loadHistory(context, { page: 1 })),
    previousPage: () => changeHistoryPage(context, -1),
    nextPage: () => changeHistoryPage(context, 1),
    select: () => selectHistoryRun(context),
  };
  const { elements } = context.view;
  elements.historyRun.addEventListener("change", () => { context.onNavigationChanged?.(); void controller.select(); });
  elements.historyRefresh.addEventListener("click", () => void controller.refresh());
  elements.historyPrev.addEventListener("click", () => void controller.previousPage());
  elements.historyNext.addEventListener("click", () => void controller.nextPage());
  return controller;
}

async function loadHistory(context, options = {}) {
  const { request, state, view } = context;
  if (!state.activated || !state.visible) return null;
  abortHistory(context);
  const controller = new AbortController();
  const sequence = ++state.historyRequestSeq;
  state.historyRequest = controller;
  view.renderHistoryLoading();
  const filters = view.historyFilters();
  const queryKey = JSON.stringify([state.browseMode, filters.status, filters.dataDate]);
  const requestedPage = queryKey === state.historyQueryKey ? options.page || state.historyPage || 1 : 1;
  const params = historyQuery(filters, state.browseMode, requestedPage);
  try {
    const payload = await request(`/api/market-scans?${params.toString()}`, {
      signal: controller.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (sequence !== state.historyRequestSeq) return null;
    const page = validateMarketScanRunPage(payload, { context: "历史扫描批次响应" });
    if (page.page !== requestedPage || page.page_size !== 100) throw marketScanContractError("历史扫描分页响应与请求不一致");
    validateHistoryItems(page.items, state.browseMode);
    if (page.page > Math.max(1, page.page_count)) {
      if (options.correctOverflow === false) throw marketScanContractError("历史批次范围持续变化，请重新查询");
      return loadHistory(context, { page: Math.max(1, page.page_count), correctOverflow: false });
    }
    const retained = retainedHistorySelection(state, page.items);
    state.historyPage = page.page;
    state.historyPageCount = page.page_count;
    state.historyQueryKey = queryKey;
    context.lastPage = page;
    state.historyRuns = retained ? [retained, ...page.items] : page.items;
    const previousSelection = view.selectedHistoryRunId();
    view.renderHistory(page, state.selectedHistoryRunId, retained);
    if (view.selectedHistoryRunId() !== previousSelection) context.onNavigationChanged?.();
    return page;
  } catch (error) {
    if (!isAbortError(error) && sequence === state.historyRequestSeq) {
      view.renderHistoryError(`历史批次读取失败：${compactErrorMessage(error?.message)}`);
    }
    return null;
  } finally {
    if (sequence === state.historyRequestSeq) state.historyRequest = null;
  }
}

function abortHistory({ state, view }) {
  const wasLoading = Boolean(state.historyRequest);
  state.historyRequest?.abort?.();
  state.historyRequest = null;
  state.historyRequestSeq += 1;
  if (wasLoading) view.renderHistoryCancelled();
}


function changeHistoryPage(context, delta) {
  const { state } = context;
  if (state.historyRequest) return Promise.resolve(null);
  const page = (state.historyPage || 1) + delta;
  if (page < 1 || page > (state.historyPageCount || 0)) return Promise.resolve(null);
  return context.scheduleReads(() => loadHistory(context, { page }));
}

function selectHistoryRun(context) {
  return transitionHistory(context, (owner) => selectHistoryRunOwned(context, owner));
}

async function selectHistoryRunOwned(context, owner) {
  const { state, view } = context;
  const runId = view.selectedHistoryRunId();
  if (runId === null) {
    state.selectedHistoryRunId = null;
    context.applyPublishedRun(null);
    return context.loadLatestOwned({ forceTrusted: true, renderLoading: true });
  }
  const identity = state.historyRuns.find((item) => item.id === runId) || null;
  if (!identity) {
    restoreHistorySelection(context, "所选历史批次已不在当前查询结果中，请重新查询。");
    return null;
  }
  try {
    const payload = await context.request(`/api/market-scans/${encodeURIComponent(runId)}`, {
      timeoutMs: MARKET_SCAN_TRUSTED_READ_TIMEOUT_MS,
    });
    if (!owner.isCurrent()) return null;
    const run = validateMarketScanRun(payload, { context: "历史扫描可信批次响应" });
    if (!isPublishedMarketScanRun(run) || !samePublishedMarketScanRun(run, identity)) {
      throw marketScanContractError("历史扫描可信批次与导航身份不一致");
    }
    state.selectedHistoryRunId = runId;
    context.applyPublishedRun(run);
    return context.loadResultsOwned();
  } catch (error) {
    if (owner.isCurrent()) {
      restoreHistorySelection(context, `历史批次可信读取失败：${compactErrorMessage(error?.message)}`);
    }
    return null;
  }
}

function changeHistoryMode(context) {
  if (context.view.selectedMode() !== context.state.browseMode) {
    context.view.renderHistory({ items: [], total: 0 }, null);
    context.onNavigationChanged?.();
  }
  return transitionHistory(context, () => changeHistoryModeOwned(context));
}

async function changeHistoryModeOwned(context) {
  const { state, view } = context;
  const mode = view.selectedMode();
  if (mode === state.browseMode) {
    restoreCurrentHistory(context);
    return null;
  }
  state.browseMode = mode;
  state.selectedHistoryRunId = null;
  state.historyRuns = [];
  state.historyPage = 1;
  state.historyPageCount = 0;
  state.historyQueryKey = null;
  context.lastPage = null;
  view.renderHistory({ items: [], total: 0 }, null);
  context.applyPublishedRun(null);
  return Promise.all([
    context.loadLatestOwned({ forceTrusted: true, renderLoading: true }),
    loadHistory(context),
  ]);
}

function restoreCurrentHistory(context) {
  const page = context.lastPage || { items: [], total: 0, page: 1, page_count: 0 };
  const retained = retainedHistorySelection(context.state, page.items);
  context.view.renderHistory(page, context.state.selectedHistoryRunId, retained);
  context.onNavigationChanged?.();
  context.resumeTracking?.();
}

function restoreHistorySelection(context, message) {
  context.view.elements.historyRun.value = context.state.selectedHistoryRunId === null ? "" : String(context.state.selectedHistoryRunId);
  context.onNavigationChanged?.();
  context.view.renderHistoryError(message);
  context.resumeTracking?.();
}

function retainedHistorySelection(state, items) {
  if (state.selectedHistoryRunId === null || items.some((run) => run.id === state.selectedHistoryRunId)) return null;
  const run = state.publishedRun;
  if (run?.id !== state.selectedHistoryRunId || run.mode !== state.browseMode || !isPublishedMarketScanRun(run)) {
    throw marketScanContractError("历史导航缺少当前已校验批次，请刷新后重新选择");
  }
  return run;
}

function transitionHistory(context, operation) {
  context.clearPolling();
  abortHistory(context);
  return context.transitionReads(operation);
}

function historyQuery(filters, mode, page) {
  const params = new URLSearchParams({
    page: String(page), page_size: "100", mode, status: filters.status, authority: "navigation",
  });
  if (filters.dataDate) params.set("data_date", filters.dataDate);
  return params;
}

function validateHistoryItems(items, mode) {
  if (items.some((run) => run.mode !== mode || !isPublishedMarketScanRun(run))) {
    throw marketScanContractError("历史扫描批次响应包含了其他模式或未发布批次");
  }
}
