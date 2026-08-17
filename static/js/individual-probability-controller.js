import { DEFAULT_REQUEST_TIMEOUT_MS, createRequestScope, fetchJson, isAbortError } from "./api.js";
import { validateIndividualProbabilityReport } from "./individual-probability-contracts.js";
import { createIndividualProbabilityView } from "./individual-probability-view.js";
import { compactErrorMessage } from "./errors.js";

export function createIndividualProbabilityController(options = {}) {
  const request = options.fetcher || fetchJson;
  const view = options.view || createIndividualProbabilityView(options.root || globalThis.document);
  const state = { sequence: 0, requestScope: null, lastContext: null };

  async function load(context = {}) {
    cancel();
    if (!view.available()) return false;
    const symbol = String(context.symbol || "").trim();
    if (!symbol) {
      state.lastContext = null;
      view.renderUnavailable("当前股票代码无效");
      return false;
    }
    const sequence = state.sequence;
    const scope = createRequestScope(null, context.signal);
    state.requestScope = scope;
    state.lastContext = { ...context, symbol };
    view.renderLoading(symbol);
    try {
      const payload = await request(`/api/stock/upside-probability?symbol=${encodeURIComponent(symbol)}`, {
        signal: scope.signal,
        timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
      if (!isCurrent(state, sequence, scope, context)) return false;
      const report = validateIndividualProbabilityReport(payload, symbol);
      validateReportContext(report, context);
      view.renderReport(report);
      return true;
    } catch (error) {
      if (isAbortError(error) || !isCurrent(state, sequence, scope, context)) return false;
      view.renderUnavailable(compactErrorMessage(error?.message || "个股上涨概率研究暂不可用"));
      return false;
    } finally {
      if (state.requestScope === scope) state.requestScope = null;
      scope.dispose();
    }
  }

  function cancel() {
    state.sequence += 1;
    state.requestScope?.abort();
    state.requestScope = null;
  }

  function retry() {
    if (!state.lastContext) return Promise.resolve(false);
    return load(state.lastContext);
  }

  view.bindRetry(() => { void retry(); });
  return { cancel, load, retry, state, view };
}

function isCurrent(state, sequence, scope, context) {
  return (
    state.sequence === sequence
    && state.requestScope === scope
    && !scope.signal.aborted
    && (typeof context.isCurrent !== "function" || context.isCurrent())
  );
}

function validateReportContext(report, context) {
  const signalDate = normalizedIsoDate(context.signalDate);
  if (!signalDate || report.signal_date === null) return;
  if (report.signal_date > signalDate) {
    throw new Error("个股上涨概率正式证据晚于当前工作台信号日");
  }
  if (report.status === "calibrated_shadow" && report.signal_date !== signalDate) {
    throw new Error("个股上涨概率与当前工作台信号日不一致");
  }
}

function normalizedIsoDate(value) {
  const text = String(value || "").trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(text) && !Number.isNaN(Date.parse(`${text}T00:00:00Z`)) ? text : "";
}
