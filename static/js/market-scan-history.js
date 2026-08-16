import { DEFAULT_REQUEST_TIMEOUT_MS, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import {
  isPublishedMarketScanRun,
  marketScanContractError,
  validateMarketScanRunPage,
} from "./market-scan-contracts.js";

export function createMarketScanHistory(options) {
  const context = { ...options };
  return {
    abort: () => abortHistory(context),
    changeMode: () => changeHistoryMode(context),
    load: () => loadHistory(context),
    refresh: () => refreshHistory(context),
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
    state.historyRuns = page.items;
    if (!page.items.some((run) => run.id === state.selectedHistoryRunId)) {
      state.selectedHistoryRunId = null;
    }
    view.renderHistory(page, state.selectedHistoryRunId);
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
  return transitionHistory(context, () => selectHistoryRunOwned(context));
}

async function selectHistoryRunOwned(context) {
  const { state, view } = context;
  const runId = view.selectedHistoryRunId();
  state.selectedHistoryRunId = runId;
  if (runId === null) {
    context.applyPublishedRun(null);
    return context.loadLatestOwned({ forceTrusted: true, renderLoading: true });
  }
  const run = state.historyRuns.find((item) => item.id === runId) || null;
  if (!run) {
    view.renderHistoryError("所选历史批次已不在当前查询结果中，请重新查询。");
    return null;
  }
  context.applyPublishedRun(run);
  return context.loadResultsOwned();
}

function changeHistoryMode(context) {
  return transitionHistory(context, () => changeHistoryModeOwned(context));
}

async function changeHistoryModeOwned(context) {
  const { state, view } = context;
  const mode = view.selectedMode();
  if (mode === state.browseMode) return null;
  state.browseMode = mode;
  state.selectedHistoryRunId = null;
  state.historyRuns = [];
  context.applyPublishedRun(null);
  return Promise.all([
    context.loadLatestOwned({ forceTrusted: true, renderLoading: true }),
    loadHistory(context),
  ]);
}

function refreshHistory(context) {
  return transitionHistory(context, (owner) => refreshHistoryOwned(context, owner));
}

async function refreshHistoryOwned(context, owner) {
  context.state.selectedHistoryRunId = null;
  context.applyPublishedRun(null);
  const history = await loadHistory(context);
  if (!owner.isCurrent()) return null;
  await context.loadLatestOwned({ forceTrusted: true, renderLoading: true });
  return history;
}

function transitionHistory(context, operation) {
  context.clearPolling();
  abortHistory(context);
  return context.transitionReads(operation);
}

function historyQuery(filters, mode) {
  const params = new URLSearchParams({
    page: "1", page_size: "100", mode, status: filters.status,
  });
  if (filters.dataDate) params.set("data_date", filters.dataDate);
  return params;
}

function validateHistoryItems(items, mode) {
  if (items.some((run) => run.mode !== mode || !isPublishedMarketScanRun(run))) {
    throw marketScanContractError("历史扫描批次响应包含了其他模式或未发布批次");
  }
}
