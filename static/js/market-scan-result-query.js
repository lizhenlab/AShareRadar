import { compactErrorMessage } from "./errors.js";
import { marketScanPageSize } from "./layout-optimizations.js";
import { samePublishedMarketScanRun } from "./market-scan-latest-loader.js";

// This retains filter intent only; every response still requires the full trusted run checks.
export function createAppliedMarketScanQueries(options) {
  let applied = null;
  function refresh(runId, page, queryOptions = {}) {
    let query = applied
      ? paginatedResultsQuery(rebaseResultsQuery(applied.query, runId), page)
      : options.resultsUrl(runId, page);
    const parsed = parseResultsQuery(query);
    if (!parsed) return null;
    parsed.params.set("page_size", String(marketScanPageSize(options.elements)));
    query = formatResultsQuery(parsed);
    if (queryOptions.includeProbability === false || (applied && (
      applied.run.id !== runId || !samePublishedMarketScanRun(applied.run, options.resultRun())
    ))) return unfilteredResultsQuery(query, runId, { resetPage: false });
    return query;
  }
  return {
    capture(query, run) {
      if (run && validResultsQuery(query, run.id)) applied = { query, run: { ...run } };
    },
    clearProbability() {
      if (applied) applied.query = unfilteredResultsQuery(applied.query, applied.run.id);
    },
    matchesFilters: (query) => !applied || sameResultFilters(applied.query, query),
    refresh,
    request(run, loadOptions = {}) {
      try {
        return loadOptions.query || (loadOptions.applied
          ? refresh(run.id, options.state.page)
          : options.resultsUrl(run.id, options.state.page));
      } catch (error) {
        renderInvalidQuery(options, error, run);
        return null;
      }
    },
  };
}

function sameResultFilters(left, right) {
  const values = [left, right].map(parseResultsQuery);
  if (values.some((value) => !value) || values[0].runId !== values[1].runId) return false;
  for (const { params } of values) {
    params.delete("page");
    params.delete("page_size");
    params.sort();
  }
  return values[0].params.toString() === values[1].params.toString();
}

function renderInvalidQuery(options, error, run) {
  const page = Number(parseResultsQuery(options.getBaseline()?.query)?.params.get("page"));
  if (Number.isSafeInteger(page) && page > 0) options.state.page = page;
  const message = `筛选条件无效：${compactErrorMessage(error?.message || "请检查输入")}`;
  if (options.state.renderedResultRunId !== run.id) options.view.renderResultState(message, "error");
  options.view.announce(message, `results-query-error:${run.id}:${message}`);
  if (options.state.pollTimer == null) options.polling.scheduleDefault?.(options.state.run);
}

export function unfilteredResultsQuery(query, runId, options = {}) {
  if (!validResultsQuery(query, runId)) return null;
  const parsed = parseResultsQuery(query);
  parsed.params.delete("probability_horizon");
  parsed.params.delete("min_upside_probability");
  if (options.resetPage !== false) parsed.params.set("page", "1");
  return formatResultsQuery(parsed);
}

export function validResultsQuery(query, runId) {
  if (typeof query !== "string" || !query || !Number.isSafeInteger(Number(runId)) || Number(runId) <= 0) return false;
  return parseResultsQuery(query)?.runId === Number(runId);
}

export function queryRunId(query) { return parseResultsQuery(query)?.runId ?? null; }

export function queryHasProbabilityMinimum(query) {
  return parseResultsQuery(query)?.params.has("min_upside_probability") ?? false;
}

export function rebaseResultsQuery(query, runId) {
  const parsed = parseResultsQuery(query);
  if (!parsed || !Number.isSafeInteger(Number(runId)) || Number(runId) <= 0) return null;
  return formatResultsQuery({ ...parsed, path: `/api/market-scans/${encodeURIComponent(runId)}/results` });
}

export function paginatedResultsQuery(query, page) {
  const parsed = parseResultsQuery(query);
  if (!parsed || !Number.isSafeInteger(page) || page < 1) return null;
  parsed.params.set("page", String(page));
  return formatResultsQuery(parsed);
}

export function parseResultsQuery(query) {
  if (typeof query !== "string" || query.includes("#")) return null;
  const separator = query.indexOf("?");
  const path = separator === -1 ? query : query.slice(0, separator);
  const search = separator === -1 ? "" : query.slice(separator + 1);
  const match = path.match(/^\/api\/market-scans\/(\d+)\/results$/);
  if (!match || !Number.isSafeInteger(Number(match[1])) || Number(match[1]) <= 0) return null;
  return { path, params: new URLSearchParams(search), runId: Number(match[1]) };
}

function formatResultsQuery({ path, params }) {
  const search = params.toString();
  return `${path}${search ? `?${search}` : ""}`;
}
