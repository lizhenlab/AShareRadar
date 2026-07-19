import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import {
  isActiveMarketScanRun,
  isPublishedMarketScanRun,
  isRetryableMarketScanRun,
  marketScanContractError,
  marketScanRunIdentityChanged,
  marketScanRunStateChanged,
  validateMarketScanRun,
  validateResultPage,
  validateStartResponse,
} from "./market-scan-contracts.js";
import { createMarketScanPolling } from "./market-scan-polling.js";
import { buildMarketScanResultsUrl, createMarketScanView } from "./market-scan-view.js";
export { buildMarketScanResultsUrl, marketScanResultsUrl } from "./market-scan-view.js";
export function createMarketScanController(options = {}) {
  const root = options.root || globalThis.document;
  const panel = root?.getElementById?.("workspace-panel-market-scan");
  if (!panel) return inertMarketScanController();
  const request = options.fetcher || fetchJson;
  const onSelectStock = typeof options.onSelectStock === "function" ? options.onSelectStock : () => {};
  const connectivityTarget = options.connectivityTarget || root?.defaultView || globalThis.window;
  const view = createMarketScanView(root);
  const { elements } = view;
  const state = {
    activated: false,
    actionBusy: false,
    visible: !root.hidden,
    run: null,
    page: 1,
    pageCount: 0,
    pollTimer: null,
    resetTimer: null,
    renderedResultRunId: null,
    consecutiveFailures: 0,
    runRequest: null,
    resultRequest: null,
    actionRequest: null,
    runRequestSeq: 0,
    resultRequestSeq: 0,
    actionRequestSeq: 0,
    onlineRecoveryPromise: null,
  };
  const polling = createMarketScanPolling({
    ...options,
    state,
    callbacks: { latest: loadLatest, results: loadResults, run: pollRun },
    isEnabled: () => state.activated && state.visible && !state.actionBusy,
  });
  const { abortRequest, beginRequest, finishRequest, isCurrentRequest } = polling;
  bindEvents();
  view.renderRun(null);
  function activate() {
    if (state.activated) return Promise.resolve(state.run);
    state.activated = true;
    return loadLatest();
  }
  function deactivate() {
    state.activated = false;
    clearControllerTimers();
    abortRequest("runRequest", "runRequestSeq");
    abortRequest("resultRequest", "resultRequestSeq");
  }
  function setVisible(visible) {
    state.visible = Boolean(visible);
    if (!state.visible) {
      clearControllerTimers();
      abortRequest("runRequest", "runRequestSeq");
      abortRequest("resultRequest", "resultRequestSeq");
      return;
    }
    if (!state.activated || state.actionBusy) return;
    if (isActiveMarketScanRun(state.run)) void pollRun();
    else void loadLatest();
  }
  async function loadLatest(options = {}) {
    if (state.actionBusy) return null;
    polling.clear();
    const sequence = beginRequest("runRequest", "runRequestSeq");
    if (!state.run) view.renderHeadline("正在读取最近扫描...", "loading");
    try {
      const payload = await request("/api/market-scans/latest", {
        signal: state.runRequest.signal,
        timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
      if (!isCurrentRequest("runRequestSeq", sequence)) return null;
      const run = validateMarketScanRun(payload, { allowNull: true, context: "最近扫描响应" });
      polling.resetFailures();
      const runChanged = applyRun(run, options.recoveryMessage || "");
      if (run && !isActiveMarketScanRun(run) && (runChanged || state.renderedResultRunId !== run.id)) {
        const outcome = await loadResultsOnce();
        if (!outcome.ok) {
          if (!outcome.aborted && state.run?.id === run.id) {
            polling.handleResultFailureAfterLatest(outcome.error);
          }
          return null;
        }
      }
      polling.scheduleDefault(state.run);
      return run;
    } catch (error) {
      if (!isAbortError(error) && isCurrentRequest("runRequestSeq", sequence)) {
        polling.retryLatest();
        const message = `最近扫描读取失败：${compactErrorMessage(error?.message)}`;
        view.renderHeadline(message, "error");
        view.announce(message, `latest-error:${state.consecutiveFailures}`);
      }
      return null;
    } finally {
      finishRequest("runRequest", "runRequestSeq", sequence);
    }
  }
  async function start() {
    return mutate("开始扫描", "/api/market-scans", { method: "POST" }, async (payload) => {
      const response = validateStartResponse(payload, "开始扫描响应");
      applyRun(
        response.run,
        response.deduplicated ? "已有扫描任务正在运行，已继续跟踪该任务。" : "任务已创建，正在准备股票池。",
        true
      );
      polling.scheduleDefault(state.run);
      return response;
    });
  }
  async function cancel() {
    if (!isActiveMarketScanRun(state.run)) return null;
    return mutate(
      "取消扫描",
      `/api/market-scans/${encodeURIComponent(state.run.id)}/cancel`,
      { method: "POST" },
      async (payload) => {
        const run = validateMarketScanRun(payload, { context: "取消扫描响应" });
        applyRun(run);
        if (!isActiveMarketScanRun(run)) await loadResults({ allowDuringAction: true });
        else polling.scheduleDefault(state.run);
        return run;
      }
    );
  }
  async function retry() {
    if (!isRetryableMarketScanRun(state.run)) return null;
    return mutate(
      "重试扫描",
      `/api/market-scans/${encodeURIComponent(state.run.id)}/retry`,
      { method: "POST" },
      async (payload) => {
        const response = validateStartResponse(payload, "重试扫描响应");
        applyRun(
          response.run,
          response.deduplicated ? "已有扫描任务正在运行，已切换到该任务。" : "正在重试未完成或降级项。",
          true
        );
        polling.scheduleDefault(state.run);
        return response;
      }
    );
  }

  async function mutate(label, url, init, apply) {
    if (state.actionBusy) return null;
    const previousRun = state.run ? { id: state.run.id, status: state.run.status } : null;
    state.actionBusy = true;
    polling.clear();
    abortRequest("runRequest", "runRequestSeq");
    abortRequest("resultRequest", "resultRequestSeq");
    const sequence = beginRequest("actionRequest", "actionRequestSeq");
    view.renderActionBusy(true, state.run, `${label}请求处理中。`);
    let completionMessage = "";
    try {
      const payload = await request(url, {
        ...init,
        headers: { "Content-Type": "application/json", ...(init.headers || {}) },
        signal: state.actionRequest.signal,
        timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
      if (!isCurrentRequest("actionRequestSeq", sequence)) return null;
      polling.resetFailures();
      const result = await apply(payload);
      completionMessage = `${label}请求已完成。`;
      return result;
    } catch (error) {
      if (!isAbortError(error) && isCurrentRequest("actionRequestSeq", sequence)) {
        const message = `${label}失败：${compactErrorMessage(error?.message)}`;
        view.renderHeadline(message, "error");
        view.announce(message, `action-error:${label}:${String(error?.message || "")}`);
        await reconcileMutation(previousRun);
      }
      return null;
    } finally {
      if (isCurrentRequest("actionRequestSeq", sequence)) {
        state.actionBusy = false;
        view.renderActionBusy(false, state.run, completionMessage);
      }
      finishRequest("actionRequest", "actionRequestSeq", sequence);
      if (!state.actionBusy) polling.scheduleDefault(state.run);
    }
  }

  async function reconcileMutation(previousRun) {
    polling.clear();
    const sequence = beginRequest("runRequest", "runRequestSeq");
    try {
      const payload = await request("/api/market-scans/latest", {
        signal: state.runRequest.signal,
        timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
      if (!isCurrentRequest("runRequestSeq", sequence)) return null;
      const run = validateMarketScanRun(payload, { allowNull: true, context: "任务状态恢复响应" });
      polling.resetFailures();
      if (marketScanRunStateChanged(previousRun, run)) {
        applyRun(run, "请求响应未确认，已从服务端恢复任务状态。");
        if (run && !isActiveMarketScanRun(run)) return await loadResults({ allowDuringAction: true });
      }
      polling.scheduleDefault(state.run);
      return run;
    } catch (error) {
      if (!isAbortError(error) && isCurrentRequest("runRequestSeq", sequence)) {
        polling.retryLatest();
      }
      return null;
    } finally {
      finishRequest("runRequest", "runRequestSeq", sequence);
    }
  }

  async function pollRun() {
    polling.clear();
    if (!state.activated || !state.visible || state.actionBusy || !isActiveMarketScanRun(state.run)) return null;
    const runId = state.run.id;
    const sequence = beginRequest("runRequest", "runRequestSeq");
    let recoveryError = null;
    try {
      const payload = await request(`/api/market-scans/${encodeURIComponent(runId)}`, {
        signal: state.runRequest.signal,
        timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
      if (!isCurrentRequest("runRequestSeq", sequence) || state.run?.id !== runId) return null;
      const run = validateMarketScanRun(payload, { context: "扫描进度响应" });
      if (run.id !== runId) throw marketScanContractError("扫描进度响应的运行批次不匹配");
      polling.resetFailures();
      applyRun(run);
      if (!isActiveMarketScanRun(run)) {
        const outcome = await loadResultsOnce();
        if (!outcome.ok) {
          if (outcome.aborted) return null;
          if (polling.handleScopedFailure(outcome.error, "results")) recoveryError = outcome.error;
        } else {
          polling.scheduleDefault(state.run);
        }
      } else {
        polling.scheduleDefault(state.run);
      }
      return run;
    } catch (error) {
      if (!isAbortError(error) && isCurrentRequest("runRequestSeq", sequence)) {
        const shouldRecover = polling.handleScopedFailure(error, "run");
        const message = `进度刷新失败：${compactErrorMessage(error?.message)}，稍后自动重试。`;
        view.renderHeadline(message, "error");
        view.announce(message, `run-error:${runId}:${state.consecutiveFailures}`);
        if (shouldRecover) recoveryError = error;
      }
      return null;
    } finally {
      finishRequest("runRequest", "runRequestSeq", sequence);
      if (recoveryError && state.run?.id === runId) await recoverLatest(recoveryError);
    }
  }

  async function loadResults(options = {}) {
    if (state.actionBusy && !options.allowDuringAction) return null;
    polling.clear();
    const runId = state.run?.id ?? null;
    const outcome = await loadResultsOnce();
    if (outcome.ok) {
      polling.resetFailures();
      polling.scheduleDefault(state.run);
      return outcome.payload;
    }
    if (!outcome.aborted) {
      if (runId !== null && polling.handleScopedFailure(outcome.error, "results")) {
        await recoverLatest(outcome.error);
      }
    }
    return null;
  }

  async function loadResultsOnce() {
    if (!state.run) {
      state.renderedResultRunId = null;
      view.renderResultState("暂无扫描记录");
      return { ok: true, payload: null };
    }
    if (isActiveMarketScanRun(state.run)) {
      state.renderedResultRunId = null;
      view.renderResultState("扫描进行中，任务完成后将发布稳定榜单。", "loading");
      return { ok: true, payload: null };
    }
    if (!isPublishedMarketScanRun(state.run)) {
      state.renderedResultRunId = state.run.id;
      view.renderResultState("该批次未发布正式榜单，可重试问题项或新建扫描。", "degraded");
      return { ok: true, payload: null };
    }
    const runId = state.run.id;
    const sequence = beginRequest("resultRequest", "resultRequestSeq");
    view.renderResultsLoading();
    try {
      const response = await request(buildMarketScanResultsUrl(runId, state.page, elements), {
        signal: state.resultRequest.signal,
        timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
      if (!isCurrentRequest("resultRequestSeq", sequence) || state.run?.id !== runId) {
        return { ok: false, aborted: true, error: null };
      }
      const payload = validateResultPage(response, runId);
      state.page = payload.page;
      state.pageCount = payload.page_count;
      view.renderResults(payload);
      state.renderedResultRunId = runId;
      return { ok: true, payload };
    } catch (error) {
      if (!isAbortError(error) && isCurrentRequest("resultRequestSeq", sequence)) {
        const message = `榜单读取失败：${compactErrorMessage(error?.message)}`;
        view.renderResultState(message, "error");
        view.announce(message, `results-error:${runId}:${String(error?.message || "")}`);
        return { ok: false, aborted: false, error };
      }
      return { ok: false, aborted: true, error };
    } finally {
      finishRequest("resultRequest", "resultRequestSeq", sequence);
    }
  }

  function applyRun(run, overrideMessage = "", clearResults = false) {
    const previousRun = state.run;
    const runChanged = marketScanRunIdentityChanged(previousRun, run);
    if (runChanged) {
      state.page = 1;
      state.pageCount = 0;
      state.renderedResultRunId = null;
    }
    state.run = run || null;
    view.renderRun(state.run, overrideMessage);
    view.announceRunUpdate(previousRun, state.run, overrideMessage);
    if (clearResults || runChanged) view.resetResultPresentation(state.run);
    return runChanged;
  }

  async function recoverLatest(error) {
    if (!state.activated || !state.visible || state.actionBusy) return null;
    const message = polling.recoveryMessage(error);
    view.announce(message, `recover-latest:${state.run?.id ?? "none"}:${state.consecutiveFailures}`);
    abortRequest("resultRequest", "resultRequestSeq");
    return loadLatest({ recoveryMessage: message });
  }

  function clearResetTimer() {
    if (state.resetTimer !== null) clearTimeout(state.resetTimer);
    state.resetTimer = null;
  }

  function clearControllerTimers() {
    polling.clear();
    clearResetTimer();
  }

  function bindEvents() {
    elements.start.addEventListener("click", () => void start());
    elements.cancel.addEventListener("click", () => void cancel());
    elements.retry.addEventListener("click", () => void retry());
    elements.filters.addEventListener("submit", (event) => {
      event.preventDefault();
      clearResetTimer();
      state.page = 1;
      void loadResults();
    });
    elements.filters.addEventListener("reset", () => {
      clearResetTimer();
      state.resetTimer = setTimeout(() => {
        state.resetTimer = null;
        if (!state.activated || !state.visible) return;
        state.page = 1;
        void loadResults();
      }, 0);
    });
    elements.sort.addEventListener("change", () => {
      elements.order.value = elements.sort.value === "rank" || elements.sort.value === "symbol" ? "asc" : "desc";
    });
    elements.prev.addEventListener("click", () => {
      if (state.page <= 1) return;
      state.page -= 1;
      void loadResults();
    });
    elements.next.addEventListener("click", () => {
      if (state.pageCount && state.page >= state.pageCount) return;
      state.page += 1;
      void loadResults();
    });
    elements.rows.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-market-scan-symbol]");
      if (!button) return;
      onSelectStock(button.dataset.marketScanSymbol);
    });
    connectivityTarget?.addEventListener?.("online", handleOnline);
  }

  function handleOnline() {
    if (!state.activated || !state.visible || state.actionBusy || state.onlineRecoveryPromise) return false;
    polling.clear();
    polling.resetFailures();
    abortRequest("runRequest", "runRequestSeq");
    abortRequest("resultRequest", "resultRequestSeq");
    const message = "网络已恢复，正在同步最近扫描。";
    view.renderHeadline(message, "loading");
    view.announce(message, "network:online");
    const recovery = Promise.resolve(loadLatest({ recoveryMessage: message })).finally(() => {
      if (state.onlineRecoveryPromise === recovery) state.onlineRecoveryPromise = null;
    });
    state.onlineRecoveryPromise = recovery;
    void recovery;
    return true;
  }

  return {
    activate,
    cancel,
    deactivate,
    loadLatest,
    loadResults,
    retry,
    setVisible,
    start,
    state,
  };
}

function inertMarketScanController() {
  const noOp = () => null;
  return {
    activate: async () => null,
    cancel: async () => null,
    deactivate: noOp,
    loadLatest: async () => null,
    loadResults: async () => null,
    retry: async () => null,
    setVisible: noOp,
    start: async () => null,
    state: { activated: false, run: null },
  };
}
