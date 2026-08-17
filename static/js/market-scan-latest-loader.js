import { DEFAULT_REQUEST_TIMEOUT_MS } from "./api.js";
import {
  isPublishedMarketScanRun,
  marketScanContractError,
  validateMarketScanRun,
  validateResultPage,
} from "./market-scan-contracts.js";
import { marketScanPollingTokenChanged } from "./market-scan-polling-identity.js";

const FULL_MARKET_SCOPE = "沪市 + 深市 + 北交所当前上市A股";
export const MARKET_SCAN_TRUSTED_READ_TIMEOUT_MS = Math.max(DEFAULT_REQUEST_TIMEOUT_MS, 60000);
const PUBLISHED_BINDING_FIELDS = Object.freeze([
  "id", "status", "mode", "scope", "rule_version", "data_date", "quote_date", "updated_at",
  "finished_at", "snapshot_digest", "snapshot_seal_origin", "snapshot_sealed_at",
]);

export function createMarketScanLatestLoader(options) {
  const context = { options };
  return { stage: (identity, baseline, sequence, syncOptions) => (
    stageLatestSnapshot(context, identity, baseline, sequence, syncOptions)
  ) };
}

async function stageLatestSnapshot(context, identity, baseline, sequence, syncOptions = {}) {
  const { options } = context;
  const publishedTokenChanged = baseline === null
    || marketScanPollingTokenChanged(baseline?.latest_published, identity.latest_published);
  const latest = await requestNullableLatest(context, "latest", "最近扫描响应", sequence);
  const published = await requestNullableLatest(
    context, "latest-published", "最近已发布扫描响应", sequence
  );
  requireTrustedSelectorPair(latest, published, options.state.browseMode);
  if (options.state.selectedHistoryRunId !== null) {
    return {
      publishedLoaded: false,
      publishedRun: options.state.publishedRun,
      resultPage: null,
      resultQuery: null,
      run: latest,
    };
  }
  const publishedChanged = publishedTokenChanged
    || !samePublishedMarketScanRun(options.state.publishedRun, published);
  const shouldReadResults = published && options.state.surfaceActive
    && (publishedChanged || syncOptions.forceTrusted);
  const result = shouldReadResults
    ? await readPublishedPage(context, published, { resetQuery: publishedChanged }, sequence)
    : null;
  return {
    publishedLoaded: true,
    publishedRun: published,
    resultPage: result?.page ?? null,
    resultQuery: result?.query ?? null,
    run: latest,
  };
}

async function readPublishedPage({ options }, published, queryOptions, sequence) {
  try {
    requireCurrentRequest(options, sequence);
    const query = options.resultsUrl(published.id, queryOptions);
    options.beforeResultsRead?.(query, published);
    const payload = await options.request(query, requestOptions(options));
    requireCurrentRequest(options, sequence);
    const page = validateResultPage(payload, published.id);
    requireSamePublishedRun(page.run, published);
    return { page, query };
  } catch (error) {
    if (error && typeof error === "object") error.marketScanResultsRead = true;
    throw error;
  }
}

async function requestNullableLatest({ options }, kind, context, sequence) {
  const suffix = kind === "latest-published"
    ? `latest-published?mode=${encodeURIComponent(options.state.browseMode)}`
    : "latest";
  requireCurrentRequest(options, sequence);
  const payload = await options.request(`/api/market-scans/${suffix}`, requestOptions(options));
  requireCurrentRequest(options, sequence);
  return validateMarketScanRun(payload, { allowNull: true, context });
}

function requireCurrentRequest(options, sequence) {
  if (!options.isCurrentRequest("runRequestSeq", sequence)) {
    throw new DOMException("扫描可信读取已失效", "AbortError");
  }
}

function requestOptions(options) {
  return { signal: options.state.runRequest.signal, timeoutMs: MARKET_SCAN_TRUSTED_READ_TIMEOUT_MS };
}

function requireTrustedSelectorPair(latest, published, mode) {
  if (published) requirePublishedRun(published, published.id, mode);
  if ((!latest && published) || (latest && published && published.id > latest.id)) {
    throw marketScanContractError("可信最近批次选择器返回了不可能的顺序");
  }
}

function requirePublishedRun(run, runId, mode) {
  if (
    run.id !== runId
    || !isPublishedMarketScanRun(run)
    || run.mode !== mode
    || run.scope !== FULL_MARKET_SCOPE
  ) {
    throw marketScanContractError("可信最近已发布选择器返回的批次不符合全市场合同");
  }
}

function requireSamePublishedRun(actual, selected) {
  if (!samePublishedMarketScanRun(actual, selected)) {
    throw marketScanContractError("榜单批次与可信最近已发布选择器不一致");
  }
}

export function samePublishedMarketScanRun(left, right) {
  if (!left || !right) return left === right;
  return PUBLISHED_BINDING_FIELDS.every((field) => (left[field] ?? null) === (right[field] ?? null));
}
