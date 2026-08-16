import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import { isActiveMarketScanRun, isPublishedMarketScanRun, isRetryableMarketScanRun, marketScanContractError, marketScanRunIdentityChanged, marketScanRunStateChanged, validateMarketScanRun, validateStartResponse } from "./market-scan-contracts.js";
import { createMarketScanPolling, isMarketScanReadBusy } from "./market-scan-polling.js";
import { createMarketScanLatestLoader, samePublishedMarketScanRun } from "./market-scan-latest-loader.js";
import { createMarketScanLatestSync } from "./market-scan-latest-sync.js";
import { createMarketScanHistory } from "./market-scan-history.js";
import { createMarketScanSurface } from "./market-scan-surface.js";
import { createMarketScanExportAction } from "./market-scan-export-action.js";
import { bindMarketScanProbabilityHorizon } from "./market-scan-probability-view.js";
import { createMarketScanProbabilityHorizonController } from "./market-scan-probability-horizon-controller.js";
import { createMarketScanProbabilityPolling } from "./market-scan-probability-polling.js";
import { createMarketScanReadTransition } from "./market-scan-read-transition.js";
import { createMarketScanFutureRangeController } from "./market-scan-future-range-controller.js";
import { inertMarketScanController } from "./market-scan-controller-inert.js";
import { createMarketScanRowClickHandler } from "./market-scan-row-actions.js";
import { createMarketScanTop100Refresh } from "./market-scan-top100-refresh.js";
import { buildMarketScanResultsUrl, createMarketScanView } from "./market-scan-view.js";
export { buildMarketScanExportUrl, buildMarketScanResultsUrl, marketScanResultsUrl } from "./market-scan-view.js";
export function createMarketScanController(options = {}) {
  const root = options.root || globalThis.document;
  const panel = root?.getElementById?.("workspace-panel-market-scan");
  if (!panel) return inertMarketScanController();
  const request = options.fetcher || fetchJson;
  const exportRequest = options.exportFetcher || globalThis.fetch.bind(globalThis);
  const onSelectStock = typeof options.onSelectStock === "function" ? options.onSelectStock : () => {};
  const onOpen = typeof options.onOpen === "function" ? options.onOpen : () => {};
  const connectivityTarget = options.connectivityTarget || root?.defaultView || globalThis.window;
  const view = createMarketScanView(root, options.now);
  const { elements } = view;
  const state = {
    activated: false, actionBusy: false, exportBusy: false, visible: !root.hidden,
    surfaceActive: options.surfaceActive !== false,
    run: null, publishedRun: null, pollingIdentity: null,
    browseMode: view.selectedMode(), selectedHistoryRunId: null, historyRuns: [],
    page: 1, pageCount: 0,
    pollTimer: null, resetTimer: null,
    renderedResultRunId: null, consecutiveFailures: 0,
    runRequest: null, resultRequest: null, actionRequest: null,
    runRequestSeq: 0, resultRequestSeq: 0, actionRequestSeq: 0,
    historyRequest: null, historyRequestSeq: 0,
    onlineRecoveryPromise: null,
  };
  let probabilityHorizonController = null;
  let readTransition = null;
  let pollRunPromise = null;
  const exportResults = createMarketScanExportAction({ elements, exportRequest, resultRun, state, view });
  const polling = createMarketScanPolling({
    ...options,
    state,
    callbacks: { latest: pollLatestIdentity, probabilityResults: () => probabilityPolling.poll(loadResults), results: loadResults, run: pollRun },
    isEnabled: () => state.activated && state.visible && !state.actionBusy,
  });
  const probabilityPolling = createMarketScanProbabilityPolling({ options, polling, resultRun, state });
  const { abortRequest, beginRequest, finishRequest, isCurrentRequest } = polling;
  const latestLoader = createMarketScanLatestLoader({
    beforeResultsRead: (query, run) => probabilityHorizonController?.trustedReadStarted(query, run),
    isCurrentRequest,
    request,
    resultsUrl: (runId, queryOptions = {}) => buildMarketScanResultsUrl(
      runId,
      queryOptions.resetQuery ? 1 : state.page,
      elements,
      { includeProbability: !queryOptions.resetQuery },
    ),
    state,
  });
  const latestSync = createMarketScanLatestSync({
    abortRequest,
    beginRequest,
    commit: commitLatestSnapshot,
    finishRequest,
    handleError: handleLatestSyncError,
    handleStaleError: (error) => probabilityHorizonController?.staleTrustedFailure(error),
    isCurrentRequest,
    polling,
    renderLoading: () => view.renderHeadline("正在读取最近扫描...", "loading"),
    request,
    stage: latestLoader.stage,
    state,
  });
  probabilityHorizonController = createMarketScanProbabilityHorizonController({
    abortLatest: latestSync.abort,
    beginRequest,
    clearResetTimer,
    detachOwnedRead: (detachOptions) => readTransition.invalidateOwner(detachOptions),
    elements,
    finishRequest,
    isCurrentRequest,
    polling,
    probabilityPolling,
    recoverLatest,
    request,
    resultErrorMessage: (error) => `榜单读取失败：${compactErrorMessage(error?.message)}`,
    resultRun,
    resultsUrl: (runId, page) => buildMarketScanResultsUrl(runId, page, elements),
    state,
    view,
    withHeavyRead: (operation) => readTransition.run(operation),
  });
  readTransition = createMarketScanReadTransition({
    latestSync, probabilityHorizon: probabilityHorizonController, state,
  });
  const history = createMarketScanHistory({
    applyPublishedRun,
    clearPolling: polling.clear,
    loadLatestOwned: syncLatestWithOwnership,
    loadResultsOwned: probabilityHorizonController.loadWithinGate,
    request,
    state,
    transitionReads: readTransition.transition,
    view,
  });
  const surface = createMarketScanSurface({
    abortHistory: history.abort, scheduleTracking: () => polling.scheduleDefault(state.run),
    elements,
    loadResults,
    refreshOwned: () => Promise.all([
      isActiveMarketScanRun(state.run)
        ? pollRunOnce().then((outcome) => finishPolledRun(outcome, { withinHeavyRead: true }))
        : state.publishedRun
          ? probabilityHorizonController.loadWithinGate()
          : syncLatestWithOwnership({ forceTrusted: true, renderLoading: !state.run }),
      history.load(),
    ]),
    releaseResults: () => releaseResults({ preserveCache: true }),
    state,
    transitionReads: readTransition.transition,
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
    void readTransition.transition(() => null);
    history.abort();
    releaseResults();
  }
  function releaseResults(options = {}) {
    probabilityHorizonController.supersede(options);
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
      void readTransition.transition(() => null, { preserveCache: true });
      history.abort();
      return;
    }
    if (!state.activated || state.actionBusy) return;
    void readTransition.transition(() => {
      if (!state.activated || !state.visible || state.actionBusy) return;
      if (isActiveMarketScanRun(state.run)) {
        return pollRunOnce().then((outcome) => finishPolledRun(outcome, { withinHeavyRead: true }));
      }
      return syncLatestWithOwnership({
        forceTrusted: probabilityHorizonController.needsTrustedRefresh(), renderLoading: !state.run,
      });
    }, { preserveCache: true });
  }
  function loadLatest(options = {}) {
    return readTransition.transition(() => {
      if (!state.activated || !state.visible || state.actionBusy) return null;
      return syncLatestWithOwnership({
        forceTrusted: true,
        recoveryMessage: options.recoveryMessage || "",
        renderLoading: true,
      });
    }, { preserveCache: true });
  }
  function pollLatestIdentity() {
    return readTransition.run(() => {
      if (!state.activated || !state.visible || state.actionBusy) return null;
      return syncLatestWithOwnership({ renderLoading: !state.run });
    });
  }
  function syncLatestWithOwnership(syncOptions) {
    probabilityHorizonController.trustedChainStarted();
    return latestSync.sync(syncOptions).finally(finishLatestRead);
  }
  function commitLatestSnapshot(staged, identity, syncOptions = {}) {
    const acceptResult = Boolean(
      staged.resultPage && probabilityHorizonController.acceptTrusted(staged.resultQuery)
    );
    applyRun(staged.run, syncOptions.recoveryMessage || "");
    applyPublishedRun(staged.publishedRun);
    if (acceptResult) {
      state.page = staged.resultPage.page;
      state.pageCount = staged.resultPage.page_count;
      view.renderResults(staged.resultPage);
      state.renderedResultRunId = staged.resultPage.run.id;
      probabilityHorizonController.remember(staged.resultPage, staged.resultQuery, identity);
    }
    probabilityPolling.schedule(acceptResult ? staged.resultPage : null);
  }
  function finishLatestRead() {
    probabilityHorizonController.trustedReadFinished();
    probabilityHorizonController.requestFinished();
  }
  function handleLatestSyncError(error, syncOptions = {}) {
    const resultError = Boolean(error?.marketScanResultsRead);
    if (isMarketScanReadBusy(error)) {
      polling.retryBusy(error, "latest"); probabilityHorizonController.presentBusy(error); return;
    }
    probabilityHorizonController.invalidate({ clearLastGood: true });
    if (syncOptions.deterministicFailure) { polling.resetFailures(); polling.scheduleDefault(state.run); } else polling.retryLatest();
    const prefix = resultError ? "榜单读取失败" : "最近扫描读取失败";
    const message = `${prefix}：${compactErrorMessage(error?.message)}`;
    if (resultError) {
      view.resetProbabilityResearch(state.publishedRun?.id ?? null, { readError: true });
      view.renderResultState(message, "error");
    } else view.renderHeadline(message, "error");
    view.announce(message, `latest-error:${state.consecutiveFailures}`);
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
  async function mutate(label, url, init, apply) {
    if (state.actionBusy) return null;
    const previousRun = state.run ? { id: state.run.id, status: state.run.status } : null;
    state.actionBusy = true;
    polling.clear();
    void readTransition.transition(() => null);
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
  function reconcileMutation(previousRun) {
    polling.clear();
    return readTransition.transition(() => reconcileMutationOnce(previousRun));
  }
  async function reconcileMutationOnce(previousRun) {
    const sequence = beginRequest("runRequest", "runRequestSeq");
    const requestScope = state.runRequest;
    let loadTerminalResults = false;
    let recoveredRun = null;
    try {
      const payload = await request("/api/market-scans/latest", {
        signal: state.runRequest.signal,
        timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
      if (!isCurrentRequest("runRequestSeq", sequence)) return null;
      const run = validateMarketScanRun(payload, { allowNull: true, context: "任务状态恢复响应" });
      recoveredRun = run;
      polling.resetFailures();
      if (marketScanRunStateChanged(previousRun, run)) {
        applyRun(run, "请求响应未确认，已从服务端恢复任务状态。");
        if (isPublishedMarketScanRun(run)) applyPublishedRun(run);
        loadTerminalResults = Boolean(run && !isActiveMarketScanRun(run));
      }
      polling.scheduleDefault(state.run);
    } catch (error) {
      if (!isAbortError(error) && isCurrentRequest("runRequestSeq", sequence)) {
        polling.retryLatest();
      }
      return null;
    } finally {
      finishRequest("runRequest", "runRequestSeq", sequence);
      releaseStaleRunScope(requestScope);
      probabilityHorizonController.requestFinished();
    }
    if (loadTerminalResults) {
      await probabilityHorizonController.loadWithinGate({ allowDuringAction: true });
    }
    return recoveredRun;
  }
  function pollRun() {
    if (pollRunPromise) return pollRunPromise;
    const operation = readTransition.run(() => pollRunOnce()).then(finishPolledRun);
    const owned = operation.finally(() => {
      if (pollRunPromise === owned) pollRunPromise = null;
    });
    pollRunPromise = owned;
    return owned;
  }
  async function pollRunOnce() {
    polling.clear();
    if (!state.activated || !state.visible || state.actionBusy || !isActiveMarketScanRun(state.run)) return null;
    const runId = state.run.id;
    const sequence = beginRequest("runRequest", "runRequestSeq");
    const requestScope = state.runRequest;
    let recoveryError = null;
    try {
      const run = await requestPolledRun(runId, sequence);
      if (!run) return { recoveryError: null, run: null, runId };
      recoveryError = await processPolledRun(run);
      return { recoveryError, run, runId };
    } catch (error) {
      recoveryError = handlePollRunError(error, runId, sequence);
      return { recoveryError, run: null, runId };
    } finally {
      finishRequest("runRequest", "runRequestSeq", sequence);
      releaseStaleRunScope(requestScope);
      probabilityHorizonController.requestFinished();
    }
  }
  async function finishPolledRun(outcome, options = {}) {
    if (outcome?.recoveryError && state.run?.id === outcome.runId) {
      const recovery = recoverLatest(outcome.recoveryError);
      if (options.withinHeavyRead) void recovery;
      else await recovery;
    }
    return outcome?.run ?? null;
  }
  function releaseStaleRunScope(scope) {
    if (state.runRequest !== scope) return;
    scope?.dispose?.();
    state.runRequest = null;
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
    const publishedForBrowse = (
      state.selectedHistoryRunId === null
      && isPublishedMarketScanRun(run)
      && run.mode === state.browseMode
    );
    if (publishedForBrowse) {
      applyPublishedRun(run);
      if (state.surfaceActive) void history.load();
    }
    if (!state.surfaceActive) { polling.scheduleDefault(state.run); return null; }
    if (!publishedForBrowse) {
      probabilityPolling.schedule(null);
      return null;
    }
    const outcome = await loadResultsOnce({ withinHeavyRead: true });
    if (outcome.ok) { probabilityPolling.schedule(outcome.payload); return null; }
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
  function loadResults(options = {}) { return probabilityHorizonController.load(options); }
  function loadResultsOnce(options = {}) { return probabilityHorizonController.loadOnce(options); }
  function applyRun(run, overrideMessage = "") {
    const previousRun = state.run;
    const runChanged = marketScanRunIdentityChanged(previousRun, run);
    state.run = run || null;
    view.renderRun(state.run, overrideMessage);
    view.renderExportBusy(state.exportBusy, resultRun());
    syncBrowsingContext();
    view.announceRunUpdate(previousRun, state.run, overrideMessage);
    if (runChanged && !resultRun()) {
      probabilityHorizonController.supersede();
      state.page = 1; state.pageCount = 0; state.renderedResultRunId = null;
      view.resetResultPresentation(state.run);
    }
    return runChanged;
  }
  function applyPublishedRun(run) {
    if (run && run.mode !== state.browseMode) return false;
    const changed = !samePublishedMarketScanRun(state.publishedRun, run);
    state.publishedRun = run || null;
    view.renderExportBusy(state.exportBusy, resultRun());
    syncBrowsingContext();
    if (changed) {
      probabilityHorizonController.publicationChanged(run);
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
    bindMarketScanProbabilityHorizon(elements, probabilityHorizonController.change);
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
    history.abort();
    const message = "网络已恢复，正在同步最近扫描。";
    view.renderHeadline(message, "loading");
    view.announce(message, "network:online");
    const recovery = readTransition.transition(() => Promise.all([
      syncLatestWithOwnership({ forceTrusted: true, recoveryMessage: message, renderLoading: true }),
      history.load(),
    ])).finally(() => {
      if (state.onlineRecoveryPromise === recovery) state.onlineRecoveryPromise = null;
    });
    state.onlineRecoveryPromise = recovery;
    void recovery;
    return true;
  }
  return {
    activate, cancel, deactivate, exportResults, loadHistory: history.load, loadLatest, loadResults,
    releaseResults, retry, refreshTop100: top100Refresh.refresh,
    setSurfaceActive: surface.setActive, setVisible, start, state,
  };
}
