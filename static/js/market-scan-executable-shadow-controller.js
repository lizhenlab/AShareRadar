import { DEFAULT_REQUEST_TIMEOUT_MS, createRequestScope, fetchJson, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import {
  EXECUTABLE_SHADOW_FULL_MARKET_SCOPE,
  validateExecutableShadowReport,
} from "./market-scan-executable-shadow-contracts.js";
import {
  executableShadowElements,
  renderExecutableShadowCancelled,
  renderExecutableShadowError,
  renderExecutableShadowIdle,
  renderExecutableShadowLoading,
  renderExecutableShadowReport,
} from "./market-scan-executable-shadow-view.js";

const ENDPOINT = "/api/strategy-lab/executable-candidate-shadow";
const REQUEST_TIMEOUT_MS = Math.max(DEFAULT_REQUEST_TIMEOUT_MS, 120000);

export function createMarketScanExecutableShadowController(options = {}) {
  const root = options.root || globalThis.document;
  const elements = executableShadowElements(root);
  if (!elements) return inertController();
  const request = options.fetcher || fetchJson;
  const getCurrentRun = options.getCurrentRun || (() => null);
  const state = { requestSequence: 0, requestScope: null, report: null };
  bindEvents();
  renderExecutableShadowIdle(elements);

  function useCurrentRun() {
    const run = getCurrentRun();
    if (!isEligibleCurrentRun(run)) {
      renderExecutableShadowError(elements, "当前没有已发布的盘后正式全市场批次；请先完成或载入正式全市场扫描。");
      return null;
    }
    elements.executableShadowRunId.value = String(run.id);
    elements.executableShadowStatus.textContent = `已填入当前正式全市场批次 #${run.id}；尚未发起计算。`;
    elements.executableShadowStatus.dataset.kind = "idle";
    return run.id;
  }

  async function load() {
    const input = requestInput(elements);
    if (input.error) {
      renderExecutableShadowError(elements, input.error);
      return null;
    }
    const requestId = ++state.requestSequence;
    const scope = createRequestScope(state.requestScope);
    state.requestScope = scope;
    renderExecutableShadowLoading(elements, input.runId);
    try {
      const payload = await request(requestUrl(input), { signal: scope.signal, timeoutMs: REQUEST_TIMEOUT_MS });
      if (!isCurrent(state, requestId, scope)) return null;
      const report = validateExecutableShadowReport(payload, input.runId);
      state.report = report;
      renderExecutableShadowReport(elements, report);
      return report;
    } catch (error) {
      if (!isCurrent(state, requestId, scope)) return null;
      if (isAbortError(error)) renderExecutableShadowCancelled(elements);
      else renderExecutableShadowError(elements, compactErrorMessage(error.message));
      return null;
    } finally {
      finishRequest(state, requestId, scope);
    }
  }

  function cancel() {
    if (!state.requestScope) return false;
    state.requestSequence += 1;
    state.requestScope.abort();
    state.requestScope = null;
    renderExecutableShadowCancelled(elements);
    return true;
  }

  function bindEvents() {
    elements.executableShadowForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void load();
    });
    elements.executableShadowUseCurrent.addEventListener("click", useCurrentRun);
    elements.executableShadowCancel.addEventListener("click", cancel);
    const lab = root.getElementById("strategyLab");
    lab?.addEventListener("toggle", () => { if (!lab.open) cancel(); });
    globalThis.addEventListener?.("pagehide", cancel);
  }

  function destroy() {
    cancel();
    globalThis.removeEventListener?.("pagehide", cancel);
  }

  return { state, load, cancel, useCurrentRun, destroy };
}

function requestInput(elements) {
  const runId = Number(elements.executableShadowRunId.value);
  const notional = Number(elements.executableShadowNotional.value);
  if (!Number.isSafeInteger(runId) || runId < 1) return { error: "请输入有效的正式全市场 run_id。" };
  if (!Number.isFinite(notional) || notional < 10000 || notional > 1000000000) {
    return { error: "名义资金必须位于 10,000 至 1,000,000,000 元。" };
  }
  return { runId, notional, error: null };
}

function requestUrl(input) {
  const query = new URLSearchParams({
    run_id: String(input.runId),
    notional_cash_cny: String(input.notional),
  });
  return `${ENDPOINT}?${query}`;
}

function isEligibleCurrentRun(run) {
  return Boolean(
    run
    && Number.isSafeInteger(run.id)
    && ["success", "degraded"].includes(run.status)
    && run.mode === "official"
    && run.scope === EXECUTABLE_SHADOW_FULL_MARKET_SCOPE
  );
}

function isCurrent(state, requestId, scope) {
  return state.requestSequence === requestId && state.requestScope === scope && !scope.signal.aborted;
}

function finishRequest(state, requestId, scope) {
  scope.dispose();
  if (state.requestSequence === requestId && state.requestScope === scope) state.requestScope = null;
}

function inertController() {
  return {
    state: { requestSequence: 0, requestScope: null, report: null },
    load: async () => null,
    cancel: () => false,
    useCurrentRun: () => null,
    destroy() {},
  };
}
