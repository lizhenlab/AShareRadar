import { isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import {
  isMarketScanTop100RefreshRun,
  isPublishedMarketScanRun,
} from "./market-scan-contracts.js";
import { samePublishedMarketScanRun } from "./market-scan-latest-loader.js";
import { requestMarketScanRead } from "./market-scan-read-client.js";
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
    abort: () => abortRequest(context, true),
    refresh: () => loadResearch(context, { force: true, page: 1 }),
    sync: (run) => syncRun(context, run),
  };
}

function syncRun(context, run, loadOnOpen = true) {
  if (samePublishedMarketScanRun(context.run, run || null)) return false;
  abortRequest(context);
  context.run = run ? { ...run } : null;
  context.payload = null;
  context.page = 1;
  renderFutureRangeRun(context.elements, context.run);
  if (loadOnOpen && context.elements.research.open && eligibleRun(context.run)) void loadResearch(context);
  return true;
}

function bindEvents(context) {
  const { elements } = context;
  elements.research.addEventListener("toggle", () => {
    if (!elements.research.open) abortRequest(context, true);
    else if (!context.payload) void loadResearch(context);
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
  const run = selectedRun(context);
  if (!eligibleRun(run)) { syncRun(context, run); return null; }
  syncRun(context, run, false);
  const publication = { ...run };
  abortRequest(context);
  const controller = new AbortController();
  const sequence = ++context.sequence;
  context.requestScope = controller;
  const page = positivePage(requestOptions.page ?? context.page);
  const selected = selectedFutureRangeOptions(context.elements);
  const includeResearch = Boolean(requestOptions.force || !context.payload?.research);
  renderFutureRangeLoading(context.elements, Boolean(context.payload));
  try {
    const response = await requestMarketScanRead(context.request, futureRangeUrl(run.id, page, selected, includeResearch), {
      signal: controller.signal,
    });
    if (!currentRequest(context, sequence, publication)) return null;
    const payload = completeResearch(context, normalizeMarketScanFutureRangeResponse(response, run.id), includeResearch);
    validateSelectedPage(payload, page, selected.offset);
    context.payload = payload;
    context.page = payload.record_page.page;
    context.requestScope = null;
    renderCurrent(context);
    return payload;
  } catch (error) {
    if (!isAbortError(error) && currentRequest(context, sequence, publication)) {
      context.payload = null;
      context.page = 1;
      renderFutureRangeFailure(context.elements, compactErrorMessage(error?.message));
    }
    return null;
  } finally {
    if (sequence === context.sequence) {
      context.requestScope = null;
      if (!samePublishedMarketScanRun(context.run, selectedRun(context))) syncRun(context, selectedRun(context), false);
    }
  }
}

function selectedRun(context) {
  return context.getRun ? context.getRun() : context.run;
}

function currentRequest(context, sequence, run) {
  return sequence === context.sequence
    && samePublishedMarketScanRun(context.run, run)
    && samePublishedMarketScanRun(selectedRun(context), run);
}

function completeResearch(context, payload, includeResearch) {
  if (payload.research || payload.generation_status === "not_generated") return payload;
  const previous = context.payload;
  if (includeResearch || !previous?.research) throw new Error("未来区间响应缺少聚合研究摘要");
  if (!sameResearchArtifact(previous, payload)) throw new Error("未来区间研究版本已变化，请刷新后重新分页");
  return { ...payload, research: previous.research };
}

function sameResearchArtifact(left, right) {
  return left.schema_version === right.schema_version && left.generation_status === right.generation_status
    && ["schema_version", "integrity_digest", "generated_at"].every((field) => (
      left.artifact?.[field] === right.artifact?.[field]
    ));
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
  if (!page || context.requestScope) return;
  const target = context.page + delta;
  if (target < 1 || target > page.page_count) return;
  void loadResearch(context, { page: target });
}

function renderCurrent(context) {
  if (!context.payload || context.requestScope) return;
  renderMarketScanFutureRange(context.elements, context.payload, selectedFutureRangeOptions(context.elements));
}

function abortRequest(context, restoreView = false) {
  const interrupted = Boolean(context.requestScope);
  context.requestScope?.abort?.();
  context.requestScope = null;
  context.sequence += 1;
  if (!restoreView) return;
  if (interrupted) { context.payload = null; context.page = 1; }
  if (context.payload) renderCurrent(context);
  else renderFutureRangeRun(context.elements, context.run);
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

function validateSelectedPage(payload, requestedPage, offset) {
  if (payload.generation_status === "not_generated") return;
  const page = payload.record_page;
  if (page.page !== requestedPage || page.page_size !== RECORD_PAGE_SIZE || page.session_offset !== offset) {
    throw new Error("未来区间接口响应格式异常：明细页码、大小或周期与请求不匹配");
  }
}
