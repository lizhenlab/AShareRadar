import { DEFAULT_REQUEST_TIMEOUT_MS, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import {
  isMarketScanTop100RefreshRun,
  isPublishedMarketScanRun,
} from "./market-scan-contracts.js";
import {
  marketScanFutureRangeElements,
  normalizeMarketScanFutureRangeResponse,
  renderFutureRangeFailure,
  renderFutureRangeLoading,
  renderFutureRangeRun,
  renderMarketScanFutureRange,
  selectedFutureRangeOptions,
} from "./market-scan-future-range-view.js";

const RECORD_PAGE_SIZE = 20;

export function createMarketScanFutureRangeController(options) {
  const context = {
    elements: marketScanFutureRangeElements(options.root), getRun: options.getRun,
    payload: null, request: options.request, requestScope: null, sequence: 0,
    run: null, page: 1,
  };
  bindEvents(context);
  renderFutureRangeRun(context.elements, null);
  return {
    abort: () => abortRequest(context),
    refresh: () => loadResearch(context, { force: true, page: 1 }),
    sync: (run) => syncRun(context, run),
  };
}

function syncRun(context, run) {
  const previousId = context.run?.id ?? null;
  const nextId = run?.id ?? null;
  const nextMode = run?.mode ?? null;
  if (previousId === nextId && context.run?.mode === nextMode) return false;
  abortRequest(context);
  context.run = run || null;
  context.payload = null;
  context.page = 1;
  renderFutureRangeRun(context.elements, context.run);
  if (context.elements.research.open && eligibleRun(context.run)) void loadResearch(context);
  return true;
}

function bindEvents(context) {
  const { elements } = context;
  elements.research.addEventListener("toggle", () => {
    if (elements.research.open && !context.payload) void loadResearch(context);
  });
  elements.refresh.addEventListener("click", () => void loadResearch(context, { force: true, page: 1 }));
  elements.offsetInputs.forEach((input) => input.addEventListener("change", () => {
    if (input.checked) { context.page = 1; void loadResearch(context, { page: 1 }); }
  }));
  elements.pathInputs.forEach((input) => input.addEventListener("change", () => {
    if (input.checked) renderCurrent(context);
  }));
  elements.group.addEventListener("change", () => renderCurrent(context));
  elements.keyword.addEventListener("input", () => renderCurrent(context));
  elements.keyword.addEventListener("change", () => changeKeyword(context));
  elements.prev.addEventListener("click", () => changePage(context, -1));
  elements.next.addEventListener("click", () => changePage(context, 1));
}

async function loadResearch(context, requestOptions = {}) {
  const run = context.getRun?.() || context.run;
  if (!eligibleRun(run)) { syncRun(context, run); return null; }
  context.run = run;
  abortRequest(context);
  const controller = new AbortController();
  const sequence = ++context.sequence;
  context.requestScope = controller;
  const page = positivePage(requestOptions.page ?? context.page);
  const selected = selectedFutureRangeOptions(context.elements);
  const includeResearch = Boolean(requestOptions.force || !context.payload?.research);
  renderFutureRangeLoading(context.elements, Boolean(context.payload));
  try {
    const response = await context.request(futureRangeUrl(run.id, page, selected, includeResearch), {
      signal: controller.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (sequence !== context.sequence || context.run?.id !== run.id) return null;
    const payload = normalizeMarketScanFutureRangeResponse(response, run.id);
    if (!payload.research && context.payload?.research) payload.research = context.payload.research;
    if (!payload.research && payload.generation_status !== "not_generated") throw new Error("未来区间响应缺少聚合研究摘要");
    validateSelectedPage(payload.record_page, selected.offset);
    context.payload = payload;
    context.page = payload.record_page.page;
    renderCurrent(context);
    return payload;
  } catch (error) {
    if (!isAbortError(error) && sequence === context.sequence) {
      renderFutureRangeFailure(context.elements, compactErrorMessage(error?.message));
    }
    return null;
  } finally {
    if (sequence === context.sequence) context.requestScope = null;
  }
}

function futureRangeUrl(runId, page, options, includeResearch) {
  const params = new URLSearchParams({
    page: String(page), page_size: String(RECORD_PAGE_SIZE), session_offset: String(options.offset),
    include_research: includeResearch ? "true" : "false",
  });
  const symbol = exactSymbolQuery(options.keyword);
  if (symbol) params.set("symbol", symbol);
  return `/api/market-scans/${encodeURIComponent(runId)}/future-range-research?${params.toString()}`;
}

function changeKeyword(context) {
  const selected = selectedFutureRangeOptions(context.elements);
  const previous = String(context.payload?.record_page?.symbol || "");
  if (exactSymbolQuery(selected.keyword) || (!selected.keyword && previous)) {
    context.page = 1;
    void loadResearch(context, { page: 1 });
  } else {
    renderCurrent(context);
  }
}

function changePage(context, delta) {
  const page = context.payload?.record_page;
  if (!page) return;
  const target = context.page + delta;
  if (target < 1 || target > page.page_count) return;
  context.page = target;
  void loadResearch(context, { page: target });
}

function renderCurrent(context) {
  if (!context.payload) return;
  renderMarketScanFutureRange(context.elements, context.payload, selectedFutureRangeOptions(context.elements));
}

function abortRequest(context) {
  context.requestScope?.abort?.();
  context.requestScope = null;
  context.sequence += 1;
}

function eligibleRun(run) {
  return Boolean(
    run
    && run.mode === "official"
    && isPublishedMarketScanRun(run)
    && !isMarketScanTop100RefreshRun(run),
  );
}

function exactSymbolQuery(value) {
  const text = String(value || "").trim().toUpperCase();
  return /^[A-Z.\-]*\d{6}(?:[.\-]?(?:SH|SZ|BJ))?$/.test(text) ? text : "";
}

function positivePage(value) {
  const page = Number(value);
  return Number.isInteger(page) && page > 0 ? page : 1;
}

function validateSelectedPage(page, offset) {
  if (page.session_offset !== null && Number(page.session_offset) !== offset) {
    throw new Error("未来区间接口响应格式异常：明细周期与请求不匹配");
  }
}
