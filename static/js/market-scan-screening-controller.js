import { createRequestScope, fetchJson, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import { requestMarketScanRead } from "./market-scan-read-client.js";
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
  const observer = observeDisplayedRun(view.elements.tableWrap, () => {
    if (state.disposed) return;
    abortActiveRequest(state);
    state.loaded = false;
    state.lastKey = "";
    view.renderNoRun();
    scheduleRefresh(state, refresh);
  });
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
  const reads = [
    ["breadth", `/api/market-scans/${encodeURIComponent(runId)}/breadth`, requestOptions(requestState), renderBreadthOutcome],
    ["evaluation", `/api/market-scans/${encodeURIComponent(runId)}/screen/evaluate`, evaluationOptions(prepared.spec, requestState), renderEvaluationOutcome],
    ["delta", `/api/market-scans/${encodeURIComponent(runId)}/delta`, requestOptions(requestState), renderDeltaOutcome],
  ];
  const result = {};
  for (const [key, url, options, render] of reads) {
    if (!isCurrentRefresh(state, requestState)) return null;
    const outcome = key === "evaluation" && !prepared.spec
      ? { status: "rejected", reason: prepared.error }
      : await settledRead(request, url, options);
    if (!isCurrentRefresh(state, requestState)) return null;
    result[key] = render(view, outcome, runId);
  }
  state.loaded = Object.values(result).every(Boolean);
  return result;
}

async function settledRead(request, url, options) {
  try {
    return { status: "fulfilled", value: await requestMarketScanRead(request, url, options) };
  } catch (error) {
    return { status: "rejected", reason: error };
  }
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
  state.loaded = false;
  state.lastKey = key;
  const requestState = { scope: state.requestScope, sequence: state.requestSequence, runId, tableWrap: view.elements.tableWrap };
  view.renderLoading(runId);
  return requestState;
}

function finishRefresh(state, requestState, view) {
  if (!ownsRefresh(state, requestState)) return;
  requestState.scope.dispose();
  state.requestScope = null;
  if (displayedRunId(requestState.tableWrap) !== requestState.runId) {
    state.loaded = false;
    state.lastKey = "";
    view.renderNoRun();
    return;
  }
  view.renderRequestFinished(state.loaded);
}

function renderBreadthOutcome(view, outcome, runId) {
  if (outcome.status === "fulfilled") {
    try {
      const payload = validateMarketScanBreadth(outcome.value, runId);
      view.renderBreadth(payload);
      return payload;
    } catch (error) {
      view.renderBreadthError(compactErrorMessage(error?.message));
    }
    return null;
  }
  if (!isAbortError(outcome.reason)) view.renderBreadthError(`市场宽度读取失败：${compactErrorMessage(outcome.reason?.message)}`);
  return null;
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
      const payload = validateMarketScanDelta(outcome.value, runId);
      view.renderCohortDiff(payload);
      return payload;
    } catch (error) {
      view.renderCohortDiffError(compactErrorMessage(error?.message));
    }
    return null;
  }
  if (!isAbortError(outcome.reason)) view.renderCohortDiffError(`同 cohort 变化读取失败：${compactErrorMessage(outcome.reason?.message)}`);
  return null;
}

function bindEvents(root, view, state, refresh) {
  view.elements.shell.addEventListener("toggle", () => {
    if (!view.elements.shell.open && state.requestScope) {
      abortActiveRequest(state);
      state.loaded = false;
      view.renderRequestCancelled();
    }
  });
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
  let previousRunId = displayedRunId(element);
  const observer = new Observer((records) => {
    if (!records.some((record) => record.attributeName === "data-market-scan-run-id")) return;
    const runId = displayedRunId(element);
    if (runId === previousRunId) return;
    previousRunId = runId;
    callback();
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
  return { signal: requestState.scope.signal };
}

function evaluationOptions(spec, requestState) {
  return {
    ...requestOptions(requestState), method: "POST", headers: { "Content-Type": "application/json" },
    body: spec ? JSON.stringify(screenEvaluationRequest(spec)) : null,
  };
}

function isCurrentRefresh(state, requestState) {
  return ownsRefresh(state, requestState) && displayedRunId(requestState.tableWrap) === requestState.runId;
}

function ownsRefresh(state, requestState) {
  return state.requestSequence === requestState.sequence && state.requestScope === requestState.scope && !requestState.scope.signal.aborted;
}
