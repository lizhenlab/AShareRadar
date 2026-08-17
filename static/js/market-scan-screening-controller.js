import { DEFAULT_REQUEST_TIMEOUT_MS, createRequestScope, fetchJson, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import {
  buildScreenSpecV2,
  screenEvaluationRequest,
  validateMarketScanBreadth,
  validateMarketScanDelta,
  validateScreenEvaluation,
} from "./market-scan-screening-contracts.js";
import { createMarketScanScreeningView } from "./market-scan-screening-view.js";

export function createMarketScanScreeningController(options = {}) {
  const root = options.root || globalThis.document;
  const request = options.fetcher || fetchJson;
  const view = createMarketScanScreeningView(root);
  const state = { requestScope: null, requestSequence: 0, lastKey: "", loaded: false, disposed: false };
  const observer = observeDisplayedRun(view.elements.tableWrap, () => scheduleRefresh(state, refresh));
  bindEvents(root, view, state, refresh);

  async function refresh(refreshOptions = {}) {
    if (state.disposed || !view.elements.shell.open) return null;
    const runId = displayedRunId(view.elements.tableWrap);
    if (!runId) {
      abortActiveRequest(state);
      state.loaded = false;
      state.lastKey = "";
      view.renderNoRun();
      return null;
    }
    const prepared = prepareRequest(root, runId);
    const key = `${runId}:${prepared.specKey}`;
    if (!refreshOptions.force && state.loaded && state.lastKey === key) return null;
    const requestState = beginRefresh(state, runId, key, view);
    try {
      return await loadWorkbench({ prepared, request, requestState, runId, state, view });
    } finally {
      finishRefresh(state, requestState, view);
    }
  }

  function dispose() {
    state.disposed = true;
    abortActiveRequest(state);
    observer.disconnect();
  }

  return { dispose, open: () => refresh(), refresh: () => refresh({ force: true }), state };
}

async function loadWorkbench(context) {
  const { prepared, request, requestState, runId, state, view } = context;
  const breadthPromise = request(`/api/market-scans/${encodeURIComponent(runId)}/breadth`, requestOptions(requestState));
  const deltaPromise = request(`/api/market-scans/${encodeURIComponent(runId)}/delta`, requestOptions(requestState));
  const evaluationPromise = prepared.spec
    ? request(`/api/market-scans/${encodeURIComponent(runId)}/screen/evaluate`, evaluationOptions(prepared.spec, requestState))
    : Promise.reject(prepared.error);
  const [breadth, evaluation, delta] = await Promise.allSettled([breadthPromise, evaluationPromise, deltaPromise]);
  if (!isCurrentRefresh(state, requestState)) return null;
  renderBreadthOutcome(view, breadth, runId);
  const evaluationPayload = renderEvaluationOutcome(view, evaluation, runId);
  renderDeltaOutcome(view, delta, runId);
  state.loaded = true;
  return { breadth: settledValue(breadth), evaluation: evaluationPayload, delta: settledValue(delta) };
}

function prepareRequest(root, runId) {
  try {
    const spec = buildScreenSpecV2(root);
    return { spec, specKey: JSON.stringify(spec), error: null };
  } catch (error) {
    return { spec: null, specKey: `invalid:${String(error?.message || "")}`, error };
  }
}

function beginRefresh(state, runId, key, view) {
  abortActiveRequest(state);
  state.requestScope = createRequestScope();
  state.requestSequence += 1;
  state.lastKey = key;
  const requestState = { scope: state.requestScope, sequence: state.requestSequence };
  view.renderLoading(runId);
  return requestState;
}

function finishRefresh(state, requestState, view) {
  if (!isCurrentRefresh(state, requestState)) return;
  requestState.scope.dispose();
  state.requestScope = null;
  view.renderRequestFinished();
}

function renderBreadthOutcome(view, outcome, runId) {
  if (outcome.status === "fulfilled") {
    try {
      view.renderBreadth(validateMarketScanBreadth(outcome.value, runId));
    } catch (error) {
      view.renderBreadthError(compactErrorMessage(error?.message));
    }
    return;
  }
  if (!isAbortError(outcome.reason)) view.renderBreadthError(`市场宽度读取失败：${compactErrorMessage(outcome.reason?.message)}`);
}

function renderEvaluationOutcome(view, outcome, runId) {
  if (outcome.status === "fulfilled") {
    try {
      const payload = validateScreenEvaluation(outcome.value, runId);
      view.renderScreenSpec(payload.spec);
      view.renderEvaluation(payload);
      return payload;
    } catch (error) {
      view.renderEvaluationError(compactErrorMessage(error?.message));
      return null;
    }
  }
  if (!isAbortError(outcome.reason)) {
    view.renderEvaluationError(`筛选评估失败：${compactErrorMessage(outcome.reason?.message)}`);
  }
  return null;
}

function renderDeltaOutcome(view, outcome, runId) {
  if (outcome.status === "fulfilled") {
    try {
      view.renderCohortDiff(validateMarketScanDelta(outcome.value, runId));
    } catch (error) {
      view.renderCohortDiffError(compactErrorMessage(error?.message));
    }
    return;
  }
  if (!isAbortError(outcome.reason)) view.renderCohortDiffError(`同 cohort 变化读取失败：${compactErrorMessage(outcome.reason?.message)}`);
}

function bindEvents(root, view, state, refresh) {
  view.elements.refresh.addEventListener("click", () => void refresh({ force: true }));
  view.elements.columnInputs.forEach((input) => input.addEventListener("change", () => view.setColumnView(input.value)));
  const filters = root.getElementById("marketScanFilters");
  filters?.addEventListener("submit", () => scheduleRefresh(state, refresh, true));
  filters?.addEventListener("reset", () => scheduleRefresh(state, refresh, true));
  root.querySelectorAll('input[name="marketScanMode"]').forEach((input) => {
    input.addEventListener("change", () => { state.loaded = false; scheduleRefresh(state, refresh, true); });
  });
}

function observeDisplayedRun(element, callback) {
  const Observer = globalThis.MutationObserver;
  if (typeof Observer !== "function") return { disconnect() {} };
  const observer = new Observer((records) => {
    if (records.some((record) => record.attributeName === "data-market-scan-run-id")) callback();
  });
  observer.observe(element, { attributes: true, attributeFilter: ["data-market-scan-run-id"] });
  return observer;
}

function scheduleRefresh(state, refresh, force = false) {
  if (state.disposed) return;
  setTimeout(() => void refresh({ force }), 0);
}

function abortActiveRequest(state) {
  if (state.requestScope) state.requestScope.abort();
  state.requestScope = null;
}

function displayedRunId(element) {
  const value = Number(element?.dataset?.marketScanRunId || element?.getAttribute?.("data-market-scan-run-id"));
  return Number.isInteger(value) && value > 0 ? value : null;
}

function requestOptions(requestState) {
  return { signal: requestState.scope.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS };
}

function evaluationOptions(spec, requestState) {
  return {
    ...requestOptions(requestState), method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(screenEvaluationRequest(spec)),
  };
}

function isCurrentRefresh(state, requestState) {
  return state.requestSequence === requestState.sequence && state.requestScope === requestState.scope && !requestState.scope.signal.aborted;
}

function settledValue(outcome) {
  return outcome.status === "fulfilled" ? outcome.value : null;
}
