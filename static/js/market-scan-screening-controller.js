import { createRequestScope, fetchJson, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import { requestMarketScanRead } from "./market-scan-read-client.js";
import { screenEvaluationRequest, validateMarketScanBreadth, validateMarketScanDelta, validateScreenEvaluation } from "./market-scan-screening-contracts.js";
import { createMarketScanScreeningView } from "./market-scan-screening-view.js";
import { clearAppliedScreenContext, readAppliedScreenContext, requireScreenEvidenceBinding, screenContextMatches, subscribeAppliedScreenContext } from "./market-scan-screen-context.js";

export function createMarketScanScreeningController(options = {}) {
  const root = options.root || globalThis.document;
  const view = createMarketScanScreeningView(root);
  const context = { root, request: options.fetcher || fetchJson, view,
    state: { requestScope: null, requestSequence: 0, lastKey: "", loaded: false, disposed: false, timer: null } };
  const update = () => appliedContextChanged(context);
  const unsubscribe = subscribeAppliedScreenContext(view.elements.tableWrap, update);
  const observer = observeDisplayedRun(view.elements.tableWrap);
  const unbind = bindEvents(context);
  return {
    state: context.state, open: () => refresh(context), refresh: () => refresh(context, true),
    dispose() { context.state.disposed = true; abortActiveRequest(context); unsubscribe(); observer.disconnect(); unbind(); },
  };
}

async function refresh(context, force = false) {
  const { state, view } = context;
  if (state.disposed || !view.elements.shell.open) return null;
  const applied = readAppliedScreenContext(view.elements.tableWrap);
  if (!applied || displayedRunId(view.elements.tableWrap) !== applied.run.id) {
    abortActiveRequest(context); state.loaded = false; state.lastKey = ""; view.renderNoRun(); return null;
  }
  if (!force && (state.loaded || state.requestScope) && state.lastKey === applied.identity) return null;
  abortActiveRequest(context);
  const requestState = { scope: createRequestScope(), sequence: ++state.requestSequence, applied };
  state.requestScope = requestState.scope; state.lastKey = applied.identity; state.loaded = false;
  view.renderLoading(applied.run.id); view.renderAppliedContext(applied);
  try {
    return await loadWorkbench(context, requestState);
  } finally {
    if (ownsRefresh(context, requestState)) {
      requestState.scope.dispose(); state.requestScope = null; view.renderRequestFinished(state.loaded);
    }
  }
}

async function loadWorkbench(context, requestState) {
  const { applied, scope } = requestState;
  const runId = applied.run.id;
  const options = { signal: scope.signal };
  const reads = [
    ["breadth", `/api/market-scans/${runId}/breadth`, options, validateMarketScanBreadth, "renderBreadth", "renderBreadthError"],
    ["evaluation", `/api/market-scans/${runId}/screen/evaluate`, { ...options, method: "POST", headers: { "Content-Type": "application/json" }, body: applied.spec ? JSON.stringify(screenEvaluationRequest(applied.spec)) : null }, validateScreenEvaluation, "renderEvaluation", "renderEvaluationError"],
    ["delta", `/api/market-scans/${runId}/delta`, options, validateMarketScanDelta, "renderCohortDiff", "renderCohortDiffError"],
  ];
  const result = {};
  for (const [key, url, requestOptions, validate, render, renderError] of reads) {
    if (!ownsRefresh(context, requestState)) return null;
    try {
      if (key === "evaluation" && !applied.spec) throw new Error(applied.error || "当前已应用条件不可解释");
      const payload = await requestMarketScanRead(context.request, url, requestOptions);
      if (!ownsRefresh(context, requestState)) return null;
      const valid = validate(payload, runId);
      requireScreenEvidenceBinding(key === "delta" ? valid.current : valid.evidence, applied, key === "evaluation" ? valid.spec : undefined, key === "delta");
      if (key === "evaluation") {
        if (valid.matched_count !== applied.total) throw new Error("筛选命中数量与已应用榜单不一致");
        context.view.renderScreenSpec(valid.spec);
      }
      context.view[render](valid); result[key] = valid;
    } catch (error) {
      if (!ownsRefresh(context, requestState)) return null;
      if (!isAbortError(error)) context.view[renderError](`冻结证据读取失败：${compactErrorMessage(error?.message)}`);
      result[key] = null;
    }
  }
  context.state.loaded = Object.values(result).every(Boolean);
  return result;
}

function ownsRefresh(context, requestState) {
  const { state, view } = context;
  const owned = !state.disposed && state.requestScope === requestState.scope && state.requestSequence === requestState.sequence && !requestState.scope.signal.aborted;
  if (!owned) return false;
  if (displayedRunId(view.elements.tableWrap) !== requestState.applied.run.id) {
    clearAppliedScreenContext(view.elements.tableWrap);
    return false;
  }
  return screenContextMatches(view.elements.tableWrap, requestState.applied);
}

function appliedContextChanged(context) {
  if (context.state.disposed) return;
  abortActiveRequest(context);
  context.state.loaded = false; context.state.lastKey = "";
  context.view.renderNoRun();
  scheduleRefresh(context);
}

function abortActiveRequest(context) {
  const { state } = context;
  state.requestScope?.abort(); state.requestScope?.dispose(); state.requestScope = null;
  state.requestSequence += 1;
  if (state.timer !== null) clearTimeout(state.timer);
  state.timer = null;
}

function scheduleRefresh(context) {
  const { state, view } = context;
  if (state.disposed || !view.elements.shell.open) return;
  if (state.timer !== null) clearTimeout(state.timer);
  state.timer = setTimeout(() => { state.timer = null; void refresh(context); }, 0);
}

function bindEvents(context) {
  const { view } = context;
  const listeners = [];
  const on = (element, event, handler) => { element.addEventListener(event, handler); listeners.push(() => element.removeEventListener?.(event, handler)); };
  on(view.elements.shell, "toggle", () => {
    if (!view.elements.shell.open) { abortActiveRequest(context); context.state.loaded = false; view.renderRequestCancelled(); }
  });
  on(view.elements.refresh, "click", () => void refresh(context, true));
  view.elements.columnInputs.forEach((input) => on(input, "change", () => view.setColumnView(input.value)));
  return () => listeners.forEach((remove) => remove());
}

function observeDisplayedRun(element) {
  const Observer = globalThis.MutationObserver;
  if (typeof Observer !== "function") return { disconnect() {} };
  const observer = new Observer(() => {
    const applied = readAppliedScreenContext(element);
    if (applied && displayedRunId(element) !== applied.run.id) clearAppliedScreenContext(element);
  });
  observer.observe(element, { attributes: true, attributeFilter: ["data-market-scan-run-id"] });
  return observer;
}

function displayedRunId(element) {
  const value = Number(element?.dataset?.marketScanRunId || element?.getAttribute?.("data-market-scan-run-id"));
  return Number.isInteger(value) && value > 0 ? value : null;
}
