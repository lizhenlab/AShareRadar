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
  const context = { ...options };
  return {
    abort: () => abortHistory(context),
    changeMode: () => changeHistoryMode(context),
    load: () => loadHistory(context),
    refresh: () => context.scheduleReads(() => loadHistory(context)),
    select: () => selectHistoryRun(context),
  };
}

async function loadHistory(context) {
  const { request, state, view } = context;
  if (!state.activated || !state.visible) return null;
  abortHistory(context);
  const controller = new AbortController();
  const sequence = ++state.historyRequestSeq;
  state.historyRequest = controller;
  view.renderHistoryLoading();
  const params = historyQuery(view.historyFilters(), state.browseMode);
  try {
    const payload = await request(`/api/market-scans?${params.toString()}`, {
      signal: controller.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (sequence !== state.historyRequestSeq) return null;
    const page = validateMarketScanRunPage(payload, { context: "历史扫描批次响应" });
    validateHistoryItems(page.items, state.browseMode);
    const retained = retainedHistorySelection(state, page.items);
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

function abortHistory({ state }) {
  state.historyRequest?.abort?.();
  state.historyRequest = null;
  state.historyRequestSeq += 1;
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
  if (mode === state.browseMode) return null;
  state.browseMode = mode;
  state.selectedHistoryRunId = null;
  state.historyRuns = [];
  view.renderHistory({ items: [], total: 0 }, null);
  context.applyPublishedRun(null);
  return Promise.all([
    context.loadLatestOwned({ forceTrusted: true, renderLoading: true }),
    loadHistory(context),
  ]);
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

function historyQuery(filters, mode) {
  const params = new URLSearchParams({
    page: "1", page_size: "100", mode, status: filters.status, authority: "navigation",
  });
  if (filters.dataDate) params.set("data_date", filters.dataDate);
  return params;
}

function validateHistoryItems(items, mode) {
  if (items.some((run) => run.mode !== mode || !isPublishedMarketScanRun(run))) {
    throw marketScanContractError("历史扫描批次响应包含了其他模式或未发布批次");
  }
}
