import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import { isActiveMarketScanRun, isPublishedMarketScanRun, isRetryableMarketScanRun, marketScanContractError, marketScanRunIdentityChanged, marketScanRunStateChanged, validateMarketScanRun, validateResultPage, validateStartResponse } from "./market-scan-contracts.js";
import { createMarketScanPolling } from "./market-scan-polling.js";
import { createMarketScanHistory } from "./market-scan-history.js";
import { createMarketScanSurface } from "./market-scan-surface.js";
import { exportTimeoutScope, MARKET_SCAN_XLSX_MEDIA_TYPE, marketScanExportError, marketScanExportMediaType } from "./market-scan-export-client.js";
import { bindMarketScanProbabilityHorizon } from "./market-scan-probability-view.js";
import { createMarketScanFutureRangeController } from "./market-scan-future-range-controller.js";
import { inertMarketScanController } from "./market-scan-controller-inert.js";
import { createMarketScanRowClickHandler } from "./market-scan-row-actions.js";
import { createMarketScanTop100Refresh } from "./market-scan-top100-refresh.js";
import { buildMarketScanExportUrl, buildMarketScanResultsUrl, createMarketScanView } from "./market-scan-view.js";
export { buildMarketScanExportUrl, buildMarketScanResultsUrl, marketScanResultsUrl } from "./market-scan-view.js";

export function createMarketScanController(options = {}) {
  const root = options.root || globalThis.document;
  const panel = root?.getElementById?.("workspace-panel-market-scan");
  if (!panel) return inertMarketScanController();
  const request = options.fetcher || fetchJson;
  const exportRequest = options.exportFetcher || globalThis.fetch;
  const onSelectStock = typeof options.onSelectStock === "function" ? options.onSelectStock : () => {};
  const onOpen = typeof options.onOpen === "function" ? options.onOpen : () => {};
  const connectivityTarget = options.connectivityTarget || root?.defaultView || globalThis.window;
  const view = createMarketScanView(root, options.now);
  const { elements } = view;
  const state = {
    activated: false, actionBusy: false, exportBusy: false, visible: !root.hidden,
    surfaceActive: options.surfaceActive !== false,
    run: null, publishedRun: null,
    browseMode: view.selectedMode(), selectedHistoryRunId: null, historyRuns: [],
    page: 1, pageCount: 0,
    pollTimer: null, resetTimer: null,
    renderedResultRunId: null, consecutiveFailures: 0,
    runRequest: null, resultRequest: null, actionRequest: null,
    runRequestSeq: 0, resultRequestSeq: 0, actionRequestSeq: 0,
    historyRequest: null, historyRequestSeq: 0,
    onlineRecoveryPromise: null,
  };
  const polling = createMarketScanPolling({
    ...options,
    state,
    callbacks: { latest: loadLatest, results: loadResults, run: pollRun },
    isEnabled: () => state.activated && state.visible && !state.actionBusy,
  });
  const { abortRequest, beginRequest, finishRequest, isCurrentRequest } = polling;
  const history = createMarketScanHistory({
    applyPublishedRun,
    abortResults: () => abortRequest("resultRequest", "resultRequestSeq"),
    loadLatest,
    loadResults,
    request,
    state,
    view,
  });
  const surface = createMarketScanSurface({
    abortHistory: history.abort,
    abortResults: () => abortRequest("resultRequest", "resultRequestSeq"),
    elements,
    loadResults,
    refresh: () => Promise.all([
      isActiveMarketScanRun(state.run) ? pollRun() : state.publishedRun ? loadResults() : loadLatest(),
      history.load(),
    ]),
    releaseResults,
    state,
  });
  const handleRowClick = createMarketScanRowClickHandler({ onSelectStock, view });
  const top100Refresh = createMarketScanTop100Refresh({ applyRun, elements, mutate, polling, resultRun, state, view });
  const futureRange = createMarketScanFutureRangeController({ root, request, getRun: resultRun });
  bindEvents();
  view.renderRun(null);
  view.resetProbabilityResearch(null);
  view.renderExportBusy(false, null);
  syncBrowsingContext();
  function activate() {
    if (state.activated) return Promise.resolve(state.run);
    state.activated = true;
    return Promise.all([loadLatest(), history.load()]).then(([run]) => run);
  }
  function deactivate() {
    state.activated = false;
    futureRange.abort();
    clearControllerTimers();
    abortRequest("runRequest", "runRequestSeq");
    abortRequest("resultRequest", "resultRequestSeq");
    history.abort();
    releaseResults();
  }
  function releaseResults() {
    elements.rows.innerHTML = "";
    elements.tableWrap.hidden = true;
    elements.pagination.hidden = true;
    state.renderedResultRunId = null;
  }
  function setVisible(visible) {
    state.visible = Boolean(visible);
    if (!state.visible) {
      futureRange.abort();
      clearControllerTimers();
      abortRequest("runRequest", "runRequestSeq");
      abortRequest("resultRequest", "resultRequestSeq");
      history.abort();
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
      applyRun(run, options.recoveryMessage || "");
      const publishedRun = await resolvePublishedRun(run, state.runRequest.signal);
      if (!isCurrentRequest("runRequestSeq", sequence)) return null;
      polling.resetFailures();
      const publishedChanged = applyPublishedRun(publishedRun);
      if (state.surfaceActive && publishedRun && (publishedChanged || state.renderedResultRunId !== publishedRun.id)) {
        const outcome = await loadResultsOnce();
        if (!outcome.ok) {
          if (!outcome.aborted && state.publishedRun?.id === publishedRun.id) {
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
    const requestedMode = view.selectedMode();
    state.browseMode = requestedMode;
    return mutate("开始扫描", "/api/market-scans", { method: "POST", body: JSON.stringify({ mode: requestedMode }) }, async (payload) => {
      const response = validateStartResponse(payload, "开始扫描响应");
      const reusedOtherMode = response.deduplicated && response.run.mode !== requestedMode;
      applyRun(
        response.run,
        reusedOtherMode
          ? `请求的是${view.modeLabel(requestedMode)}；当前已有${view.modeLabel(response.run.mode)}任务，已继续跟踪该任务。`
          : response.deduplicated
            ? "已有同模式扫描任务正在运行，已继续跟踪该任务。"
            : "任务已创建，正在准备股票池。"
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
        if (!isActiveMarketScanRun(run)) {
          applyPublishedRun(isPublishedMarketScanRun(run) ? run : state.publishedRun);
          await loadResults({ allowDuringAction: true });
        }
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
          response.deduplicated ? "已有扫描任务正在运行，已切换到该任务。" : "正在重试未完成或降级项。"
        );
        polling.scheduleDefault(state.run);
        return response;
      }
    );
  }
  async function exportResults() {
    const publishedRun = resultRun();
    if (!publishedRun || state.exportBusy) return null;
    state.exportBusy = true;
    view.renderExportBusy(true, publishedRun);
    view.announce("正在导出当前筛选条件下的 Excel 榜单。", `export:start:${publishedRun.id}`);
    const timeout = exportTimeoutScope();
    try {
      const response = await exportRequest(buildMarketScanExportUrl(publishedRun.id, elements), {
        headers: { Accept: MARKET_SCAN_XLSX_MEDIA_TYPE },
        signal: timeout.signal,
      });
      if (!response?.ok) throw new Error(await marketScanExportError(response));
      if (marketScanExportMediaType(response) !== MARKET_SCAN_XLSX_MEDIA_TYPE) {
        throw new Error("服务返回的不是 Excel 文件");
      }
      const blob = await response.blob();
      if (!blob?.size) throw new Error("服务返回了空的 Excel 文件");
      const filename = view.saveExport(
        blob,
        response.headers?.get?.("content-disposition") || "",
        publishedRun,
      );
      view.announce(`Excel 榜单已导出：${filename}`, `export:success:${publishedRun.id}:${filename}`);
      return filename;
    } catch (error) {
      const detail = timeout.didTimeout() ? "请求超时，请稍后重试" : compactErrorMessage(error?.message);
      const message = `导出 Excel 失败：${detail}`;
      view.announce(message, `export:error:${publishedRun.id}:${String(error?.message || "")}`);
      return null;
    } finally {
      timeout.dispose();
      state.exportBusy = false;
      view.renderExportBusy(false, resultRun());
    }
  }
  async function mutate(label, url, init, apply) {
    if (state.actionBusy) return null;
    const previousRun = state.run ? { id: state.run.id, status: state.run.status } : null;
    state.actionBusy = true;
    polling.clear();
    abortRequest("runRequest", "runRequestSeq");
    const sequence = beginRequest("actionRequest", "actionRequestSeq");
    view.renderActionBusy(true, state.run, `${label}请求处理中。`);
    top100Refresh.sync();
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
        top100Refresh.sync();
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
        if (isPublishedMarketScanRun(run)) applyPublishedRun(run);
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
      const run = await requestPolledRun(runId, sequence);
      if (!run) return null;
      recoveryError = await processPolledRun(run);
      return run;
    } catch (error) {
      recoveryError = handlePollRunError(error, runId, sequence);
      return null;
    } finally {
      finishRequest("runRequest", "runRequestSeq", sequence);
      if (recoveryError && state.run?.id === runId) await recoverLatest(recoveryError);
    }
  }
  async function requestPolledRun(runId, sequence) {
    const payload = await request(`/api/market-scans/${encodeURIComponent(runId)}`, {
      signal: state.runRequest.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS });
    if (!isCurrentRequest("runRequestSeq", sequence) || state.run?.id !== runId) return null;
    const run = validateMarketScanRun(payload, { context: "扫描进度响应" });
    if (run.id !== runId) throw marketScanContractError("扫描进度响应的运行批次不匹配");
    return run;
  }
  async function processPolledRun(run) {
    polling.resetFailures(); applyRun(run);
    if (isActiveMarketScanRun(run)) { polling.scheduleDefault(state.run); return null; }
    if (
      state.selectedHistoryRunId === null
      && isPublishedMarketScanRun(run)
      && run.mode === state.browseMode
    ) {
      applyPublishedRun(run);
      if (state.surfaceActive) void history.load();
    }
    if (!state.surfaceActive) { polling.scheduleDefault(state.run); return null; }
    const outcome = await loadResultsOnce();
    if (outcome.ok) { polling.scheduleDefault(state.run); return null; }
    if (outcome.aborted) return null;
    return polling.handleScopedFailure(outcome.error, "results") ? outcome.error : null;
  }
  function handlePollRunError(error, runId, sequence) {
    if (isAbortError(error) || !isCurrentRequest("runRequestSeq", sequence)) return null;
    const shouldRecover = polling.handleScopedFailure(error, "run");
    const message = `进度刷新失败：${compactErrorMessage(error?.message)}，稍后自动重试。`;
    view.renderHeadline(message, "error");
    view.announce(message, `run-error:${runId}:${state.consecutiveFailures}`);
    return shouldRecover ? error : null;
  }
  async function loadResults(options = {}) {
    if (!state.surfaceActive) return null;
    if (state.actionBusy && !options.allowDuringAction) return null;
    polling.clear();
    const runId = resultRun()?.id ?? null;
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
    if (!state.surfaceActive) return { ok: true, payload: null, skipped: true };
    const publishedRun = resultRun();
    if (!publishedRun) {
      state.renderedResultRunId = null;
      view.resetResultPresentation(state.run);
      return { ok: true, payload: null };
    }
    const runId = publishedRun.id;
    const sequence = beginRequest("resultRequest", "resultRequestSeq");
    view.renderResultsLoading();
    try {
      const response = await request(buildMarketScanResultsUrl(runId, state.page, elements), {
        signal: state.resultRequest.signal,
        timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
      if (!isCurrentRequest("resultRequestSeq", sequence) || resultRun()?.id !== runId) {
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
  function applyRun(run, overrideMessage = "") {
    const previousRun = state.run;
    const runChanged = marketScanRunIdentityChanged(previousRun, run);
    state.run = run || null;
    view.renderRun(state.run, overrideMessage);
    view.renderExportBusy(state.exportBusy, resultRun());
    syncBrowsingContext();
    view.announceRunUpdate(previousRun, state.run, overrideMessage);
    if (runChanged && !resultRun()) {
      state.page = 1; state.pageCount = 0; state.renderedResultRunId = null;
      view.resetResultPresentation(state.run);
    }
    return runChanged;
  }
  function applyPublishedRun(run) {
    if (run && run.mode !== state.browseMode) return false;
    const changed = marketScanRunIdentityChanged(state.publishedRun, run);
    state.publishedRun = run || null;
    view.renderExportBusy(state.exportBusy, resultRun());
    syncBrowsingContext();
    if (changed) {
      state.page = 1; state.pageCount = 0;
      state.renderedResultRunId = null;
      view.resetResultPresentation(state.publishedRun || state.run);
    }
    return changed;
  }
  function resultRun() {
    if (state.publishedRun?.mode === state.browseMode) return state.publishedRun;
    if (
      state.selectedHistoryRunId === null
      && isPublishedMarketScanRun(state.run)
      && state.run.mode === state.browseMode
    ) return state.run;
    return null;
  }
  async function resolvePublishedRun(run, signal) {
    if (state.selectedHistoryRunId !== null) return resultRun();
    if (isPublishedMarketScanRun(run) && run.mode === state.browseMode) return run;
    try {
      const payload = await request(`/api/market-scans/latest-published?mode=${encodeURIComponent(state.browseMode)}`, {
        signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
      const published = validateMarketScanRun(payload, { allowNull: true, context: "最近已发布扫描响应" });
      return isPublishedMarketScanRun(published) && published.mode === state.browseMode ? published : null;
    } catch (error) {
      if (isAbortError(error)) throw error;
      return state.publishedRun?.mode === state.browseMode ? state.publishedRun : null;
    }
  }

  function syncBrowsingContext() {
    view.renderBrowsingContext(
      state.run,
      resultRun(),
      state.browseMode,
      state.selectedHistoryRunId !== null,
    );
    top100Refresh.sync();
    futureRange.sync(resultRun());
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
  function clearControllerTimers() { polling.clear(); clearResetTimer(); }
  function bindEvents() {
    elements.modeInputs.forEach((input) => input.addEventListener("change", () => void history.changeMode()));
    elements.historyRun.addEventListener("change", () => void history.select());
    elements.historyRefresh.addEventListener("click", () => void history.refresh());
    elements.start.addEventListener("click", () => void start());
    elements.cancel.addEventListener("click", () => void cancel());
    elements.retry.addEventListener("click", () => void retry());
    elements.exportButton.addEventListener("click", () => void exportResults());
    bindMarketScanProbabilityHorizon(elements, () => {
      clearResetTimer(); state.page = 1; view.resetProbabilityResearch(resultRun()?.id); void loadResults();
    });
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
      view.focusResults();
      void loadResults();
    });
    elements.next.addEventListener("click", () => {
      if (state.pageCount && state.page >= state.pageCount) return;
      state.page += 1;
      view.focusResults();
      void loadResults();
    });
    elements.globalOpen.addEventListener("click", () => onOpen());
    elements.globalCancel.addEventListener("click", () => void cancel());
    elements.rows.addEventListener("click", handleRowClick);
    connectivityTarget?.addEventListener?.("online", handleOnline);
  }

  function handleOnline() {
    if (!state.activated || !state.visible || state.actionBusy || state.onlineRecoveryPromise) return false;
    polling.clear();
    polling.resetFailures();
    abortRequest("runRequest", "runRequestSeq");
    abortRequest("resultRequest", "resultRequestSeq");
    history.abort();
    const message = "网络已恢复，正在同步最近扫描。";
    view.renderHeadline(message, "loading");
    view.announce(message, "network:online");
    const recovery = Promise.all([
      loadLatest({ recoveryMessage: message }),
      history.load(),
    ]).finally(() => {
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
    exportResults,
    loadHistory: history.load,
    loadLatest,
    loadResults,
    releaseResults,
    retry,
    refreshTop100: top100Refresh.refresh,
    setSurfaceActive: surface.setActive,
    setVisible,
    start,
    state,
  };
}
