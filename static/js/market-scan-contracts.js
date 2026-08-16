import { validateUiSymbol } from "./symbols.js";
import {
  normalizeMarketScanProbabilityResearch,
  normalizeMarketScanUpsideProbabilities,
} from "./market-scan-probability-view.js";
export {
  buildDiscoveryPresetDefinition,
  isDiscoveryPresetUiRepresentable,
} from "./market-scan-filters.js";

export const MARKET_SCAN_RUN_STATUSES = Object.freeze([
  "queued", "running", "cancelling", "success", "degraded", "failed", "cancelled", "interrupted",
]);
export const MARKET_SCAN_MODES = Object.freeze(["official", "intraday", "preopen"]);
export const MARKET_SCAN_PUBLICATION_DIAGNOSTICS_SCHEMA_VERSION = "market-scan-publication-diagnostics-v1";
export const MARKET_SCAN_PUBLICATION_DIAGNOSTIC_SEVERITIES = Object.freeze(["info", "warning", "error"]);
export const MARKET_SCAN_PUBLICATION_DIAGNOSTIC_FIELDS = Object.freeze(["code", "label", "detail", "severity"]);
export const MARKET_SCAN_PUBLICATION_DIAGNOSTICS_FIELDS = Object.freeze([
  "schema_version", "headline", "blockers", "passed_gates", "source_warnings",
]);

const ACTIVE_RUN_STATUSES = new Set(["queued", "running", "cancelling"]);
const PUBLISHED_RUN_STATUSES = new Set(["success", "degraded"]);
const RETRYABLE_RUN_STATUSES = new Set(["degraded", "failed", "cancelled", "interrupted"]);
const RUN_STATUSES = new Set(MARKET_SCAN_RUN_STATUSES);
const PUBLICATION_DIAGNOSTIC_SEVERITIES = new Set(MARKET_SCAN_PUBLICATION_DIAGNOSTIC_SEVERITIES);
const RUN_TRIGGERS = new Set(["manual", "scheduled", "retry"]);
const MARKET_SCAN_MODE_LABELS = Object.freeze({
  preopen: "盘前复盘",
  intraday: "盘中临时",
  official: "盘后正式",
});
const MARKET_SCAN_MODE_SET = new Set(MARKET_SCAN_MODES);
const MARKET_SCAN_FULL_MARKET_SCOPE = "沪市 + 深市 + 北交所当前上市A股";
const SNAPSHOT_SEAL_ORIGINS = new Set(["publication", "legacy_backfill"]);
const RESULT_STATUSES = new Set(["pending", "success", "missing", "skipped"]);
const MARKET_SCAN_STAGES = new Set(["stock_pool", "bulk_quotes", "klines", "scoring", "persistence", "publication"]);
export const MARKET_SCAN_TOP100_REFRESH_SCOPE = "TOP100快速更新评分";

export function isActiveMarketScanRun(run) {
  return Boolean(run && ACTIVE_RUN_STATUSES.has(run.status));
}

export function isPublishedMarketScanRun(run) {
  return Boolean(run && PUBLISHED_RUN_STATUSES.has(run.status));
}

export function isRetryableMarketScanRun(run) {
  return Boolean(run && RETRYABLE_RUN_STATUSES.has(run.status));
}

export function isMarketScanTop100RefreshRun(run) {
  return Boolean(run && String(run.scope || "").trim() === MARKET_SCAN_TOP100_REFRESH_SCOPE);
}

export function defaultMarketScanMode(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "official";
  const weekday = date.getDay() >= 1 && date.getDay() <= 5;
  const minutes = (date.getHours() * 60) + date.getMinutes();
  const preopenWindow = minutes < (9 * 60) + 15;
  const intradayWindow = minutes >= (9 * 60) + 30 && minutes < (15 * 60) + 15;
  if (weekday && preopenWindow) return "preopen";
  return weekday && intradayWindow ? "intraday" : "official";
}

export function marketScanModeLabel(mode) {
  return MARKET_SCAN_MODE_LABELS[mode] || "未知模式";
}

export function marketScanRunModeLabel(run) {
  const mode = marketScanModeLabel(run?.mode);
  return isMarketScanTop100RefreshRun(run) ? `${mode} · TOP100 快更` : mode;
}

export function marketScanRunIdentityChanged(previousRun, nextRun) {
  return (previousRun?.id ?? null) !== (nextRun?.id ?? null);
}

export function marketScanRunStateChanged(previousRun, nextRun) {
  if (marketScanRunIdentityChanged(previousRun, nextRun)) return true;
  return (previousRun?.status ?? null) !== (nextRun?.status ?? null);
}

export function validateMarketScanRun(value, options = {}) {
  const context = options.context || "扫描运行响应";
  if (value === null && options.allowNull) return null;
  const run = requireObject(value, context);
  requireInteger(run.id, `${context}.id`, { min: 1 });
  requireEnum(run.status, RUN_STATUSES, `${context}.status`);
  requireEnum(run.trigger, RUN_TRIGGERS, `${context}.trigger`);
  requireEnum(run.mode, MARKET_SCAN_MODE_SET, `${context}.mode`);
  requireIsoDate(run.data_date, `${context}.data_date`);
  requireIsoDate(run.quote_date, `${context}.quote_date`);
  for (const field of ["rule_version", "scope"]) {
    requireString(run[field], `${context}.${field}`);
  }
  for (const field of ["as_of", "created_at", "updated_at"]) requireIsoTimestamp(run[field], `${context}.${field}`);
  for (const field of [
    "total_count",
    "excluded_count",
    "processed_count",
    "success_count",
    "missing_count",
    "skipped_count",
    "retry_count",
  ]) {
    requireInteger(run[field], `${context}.${field}`, { min: 0 });
  }
  for (const field of ["progress_pct", "coverage_pct"]) {
    requireNumber(run[field], `${context}.${field}`, { min: 0, max: 100 });
  }
  for (const field of ["task_run_id", "retry_of_run_id"]) {
    requireNullableInteger(run[field], `${context}.${field}`, { min: 1 });
  }
  requireNullableInteger(run.duration_ms, `${context}.duration_ms`, { min: 0 });
  if (run.quote_capture_count !== undefined) requireInteger(run.quote_capture_count, `${context}.quote_capture_count`, { min: 0 });
  if (run.quote_capture_duration_ms !== undefined) requireNullableInteger(run.quote_capture_duration_ms, `${context}.quote_capture_duration_ms`, { min: 0 });
  if (run.current_stage !== undefined && run.current_stage !== null) {
    requireEnum(run.current_stage, MARKET_SCAN_STAGES, `${context}.current_stage`);
  }
  if (run.stage_started_at !== undefined) requireNullableString(run.stage_started_at, `${context}.stage_started_at`);
  for (const field of ["elapsed_seconds", "throughput_per_second", "eta_seconds"]) {
    if (run[field] !== undefined && run[field] !== null) requireNumber(run[field], `${context}.${field}`, { min: 0 });
  }
  if (run.stage_metrics !== undefined) requireObject(run.stage_metrics, `${context}.stage_metrics`);
  if (run.market_progress === undefined) run.market_progress = [];
  if (!Array.isArray(run.market_progress)) throw marketScanContractError(`${context}.market_progress 必须是数组`);
  run.market_progress.forEach((item, index) => validateMarketProgress(item, `${context}.market_progress[${index}]`));
  if (run.publication_diagnostics !== undefined && run.publication_diagnostics !== null) {
    validatePublicationDiagnostics(run.publication_diagnostics, `${context}.publication_diagnostics`);
  }
  for (const field of [
    "stock_pool_source",
    "started_at",
    "finished_at",
    "message",
    "last_error",
    "cancel_requested_at",
  ]) {
    requireNullableString(run[field], `${context}.${field}`);
  }
  for (const field of ["started_at", "finished_at", "quote_capture_started_at", "quote_capture_finished_at", "stage_started_at", "cancel_requested_at"]) {
    if (run[field] !== null && run[field] !== undefined) requireIsoTimestamp(run[field], `${context}.${field}`);
  }
  validateRunTimeOrder(run, context);
  validateMarketScanRunCounts(run, context);
  validateRunSnapshotSeal(run, context);
  return run;
}

function validateMarketProgress(value, context) {
  const item = requireObject(value, context);
  requireEnum(item.market, new Set(["SH", "SZ", "BJ"]), `${context}.market`);
  for (const field of ["total_count", "processed_count", "success_count", "missing_count", "skipped_count"]) {
    requireInteger(item[field], `${context}.${field}`, { min: 0 });
  }
  requireNumber(item.coverage_pct, `${context}.coverage_pct`, { min: 0, max: 100 });
  if (item.processed_count !== item.success_count + item.missing_count + item.skipped_count || item.processed_count > item.total_count) {
    throw marketScanContractError(`${context} 状态计数不守恒`);
  }
  if (!closePercentage(item.coverage_pct, percentage(item.success_count, Math.max(0, item.total_count - item.skipped_count)))) {
    throw marketScanContractError(`${context}.coverage_pct 与计数不一致`);
  }
}

function validateRunTimeOrder(run, context) {
  const created = timestampMillis(run.created_at);
  const updated = timestampMillis(run.updated_at);
  const started = run.started_at == null ? null : timestampMillis(run.started_at);
  const finished = run.finished_at == null ? null : timestampMillis(run.finished_at);
  if (timestampsComparable(run.updated_at, run.created_at) && updated < created) throw marketScanContractError(`${context}.updated_at 不能早于 created_at`);
  if (started !== null && timestampsComparable(run.started_at, run.created_at) && started < created) throw marketScanContractError(`${context}.started_at 不能早于 created_at`);
  const startText = run.started_at ?? run.created_at;
  if (finished !== null && timestampsComparable(run.finished_at, startText) && finished < (started ?? created)) throw marketScanContractError(`${context}.finished_at 不能早于运行开始时间`);
  validateQuoteCaptureTimes(run, context);
}

function validateQuoteCaptureTimes(run, context) {
  const started = run.quote_capture_started_at == null ? null : timestampMillis(run.quote_capture_started_at);
  const finished = run.quote_capture_finished_at == null ? null : timestampMillis(run.quote_capture_finished_at);
  if (finished !== null && started === null) throw marketScanContractError(`${context}.quote_capture_finished_at 缺少开始时点`);
  if (finished !== null && timestampsComparable(run.quote_capture_finished_at, run.quote_capture_started_at) && finished < started) throw marketScanContractError(`${context}.quote_capture_finished_at 不能早于开始时点`);
  if (run.quote_capture_duration_ms != null && (started === null || finished === null)) {
    throw marketScanContractError(`${context}.quote_capture_duration_ms 缺少完整采集时点`);
  }
}

function validateMarketScanRunCounts(run, context) {
  if (run.processed_count !== run.success_count + run.missing_count + run.skipped_count || run.processed_count > run.total_count) {
    throw marketScanContractError(`${context} 状态计数不守恒`);
  }
  if (Number(run.quote_capture_count || 0) > run.total_count) throw marketScanContractError(`${context}.quote_capture_count 不能大于 total_count`);
  const expectedProgress = !run.total_count && isPublishedMarketScanRun(run) ? 100 : percentage(run.processed_count, run.total_count);
  if (!closePercentage(run.progress_pct, expectedProgress)) throw marketScanContractError(`${context}.progress_pct 与计数不一致`);
  if (!closePercentage(run.coverage_pct, percentage(run.success_count, Math.max(0, run.total_count - run.skipped_count)))) {
    throw marketScanContractError(`${context}.coverage_pct 与计数不一致`);
  }
  if (run.market_progress.length) {
    requireUniqueBy(run.market_progress, (item) => item.market, `${context}.market_progress`, "市场");
    for (const field of ["total_count", "processed_count", "success_count", "missing_count", "skipped_count"]) {
      if (run.market_progress.reduce((sum, item) => sum + item[field], 0) !== run[field]) {
        throw marketScanContractError(`${context}.market_progress.${field} 与运行总计不守恒`);
      }
    }
  }
  if (isPublishedMarketScanRun(run) && (
    run.processed_count !== run.total_count || run.progress_pct !== 100 || run.finished_at == null
    || run.current_stage != null || run.stage_started_at != null
  )) throw marketScanContractError(`${context} 已发布批次必须完成全部股票并包含完成时间`);
}

function validateRunSnapshotSeal(run, context) {
  const fields = [run.snapshot_digest, run.snapshot_seal_origin, run.snapshot_sealed_at];
  if (!isPublishedMarketScanRun(run)) {
    // Rolling upgrades may omit non-authorizing seal fields on an active run.
    if (fields.some((value) => value != null)) {
      throw marketScanContractError(`${context} 未发布批次不得包含快照封印字段`);
    }
    return;
  }
  if (typeof run.snapshot_digest !== "string" || !/^[0-9a-f]{64}$/.test(run.snapshot_digest)) {
    throw marketScanContractError(`${context}.snapshot_digest 必须是小写 SHA-256`);
  }
  requireEnum(run.snapshot_seal_origin, SNAPSHOT_SEAL_ORIGINS, `${context}.snapshot_seal_origin`);
  requireIsoTimestamp(run.snapshot_sealed_at, `${context}.snapshot_sealed_at`);
  if (timestampsComparable(run.updated_at, run.finished_at)
      && timestampMillis(run.updated_at) < timestampMillis(run.finished_at)) {
    throw marketScanContractError(`${context}.updated_at 不能早于 finished_at`);
  }
  if (timestampsComparable(run.snapshot_sealed_at, run.finished_at)
      && timestampMillis(run.snapshot_sealed_at) < timestampMillis(run.finished_at)) {
    throw marketScanContractError(`${context}.snapshot_sealed_at 不能早于 finished_at`);
  }
  if (timestampsComparable(run.snapshot_sealed_at, run.updated_at)
      && timestampMillis(run.snapshot_sealed_at) < timestampMillis(run.updated_at)) {
    throw marketScanContractError(`${context}.snapshot_sealed_at 不能早于 updated_at`);
  }
  validateCurrentFullMarketProgress(run, context);
}

function validateCurrentFullMarketProgress(run, context) {
  const digest = run.rule_version.startsWith("full-market-scan-v6:")
    ? run.rule_version.slice("full-market-scan-v6:".length) : "";
  const current = run.snapshot_seal_origin === "publication"
    && run.scope === MARKET_SCAN_FULL_MARKET_SCOPE && /^[0-9a-f]{64}$/.test(digest);
  if (current && new Set(run.market_progress.map((item) => item.market)).size !== 3) {
    throw marketScanContractError(`${context}.market_progress 必须完整覆盖 SH/SZ/BJ`);
  }
}

function validatePublicationDiagnostics(value, context) {
  const diagnostics = requireObject(value, context);
  if (diagnostics.schema_version !== MARKET_SCAN_PUBLICATION_DIAGNOSTICS_SCHEMA_VERSION) {
    throw marketScanContractError(`${context}.schema_version 的值不受支持`);
  }
  requireString(diagnostics.headline, `${context}.headline`);
  for (const field of ["blockers", "passed_gates", "source_warnings"]) {
    if (!Array.isArray(diagnostics[field])) {
      throw marketScanContractError(`${context}.${field} 必须是数组`);
    }
    diagnostics[field].forEach((item, index) => {
      validatePublicationDiagnostic(item, `${context}.${field}[${index}]`);
    });
  }
  return diagnostics;
}

function validatePublicationDiagnostic(value, context) {
  const diagnostic = requireObject(value, context);
  for (const field of ["code", "label", "detail"]) {
    requireString(diagnostic[field], `${context}.${field}`);
  }
  if (!/^[a-z][a-z0-9_.-]*$/.test(diagnostic.code)) {
    throw marketScanContractError(`${context}.code 格式不受支持`);
  }
  requireEnum(diagnostic.severity, PUBLICATION_DIAGNOSTIC_SEVERITIES, `${context}.severity`);
  return diagnostic;
}

export function validateStartResponse(value, context = "扫描任务响应") {
  const response = requireObject(value, context);
  requireBoolean(response.accepted, `${context}.accepted`);
  requireBoolean(response.deduplicated, `${context}.deduplicated`);
  if (response.accepted === response.deduplicated) {
    throw marketScanContractError(`${context}.accepted 与 deduplicated 必须且只能有一个为 true`);
  }
  validateMarketScanRun(response.run, { context: `${context}.run` });
  return response;
}

export function validateMarketScanRunPage(value, options = {}) {
  const context = options.context || "扫描批次列表响应";
  const payload = requireObject(value, context);
  if (!Array.isArray(payload.items)) throw marketScanContractError(`${context}.items 必须是数组`);
  const items = payload.items.map((item, index) => validateMarketScanRun(item, {
    context: `${context}.items[${index}]`,
  }));
  const total = requireInteger(payload.total, `${context}.total`, { min: 0 });
  const page = requireInteger(payload.page, `${context}.page`, { min: 1 });
  const pageSize = requireInteger(payload.page_size, `${context}.page_size`, { min: 1 });
  const pageCount = requireInteger(payload.page_count, `${context}.page_count`, { min: 0 });
  validatePageShape({ items, total, page, pageSize, pageCount, context });
  requireUniqueBy(items, (item) => item.id, `${context}.items`, "批次 id");
  return { ...payload, items, total, page, page_size: pageSize, page_count: pageCount };
}

export function validateResultPage(value, expectedRunId) {
  const context = "扫描榜单响应";
  const page = requireObject(value, context);
  const run = validateMarketScanRun(page.run, { context: `${context}.run` });
  requireInteger(page.total, `${context}.total`, { min: 0 });
  requireInteger(page.page, `${context}.page`, { min: 1 });
  requireInteger(page.page_size, `${context}.page_size`, { min: 1 });
  requireInteger(page.page_count, `${context}.page_count`, { min: 0 });
  if (!Array.isArray(page.items)) throw marketScanContractError(`${context}.items 必须是数组`);
  if (run.id !== expectedRunId) throw marketScanContractError(`${context}.run.id 与请求批次不匹配`);
  validatePageShape({
    items: page.items,
    total: page.total,
    page: page.page,
    pageSize: page.page_size,
    pageCount: page.page_count,
    context,
  });
  const probabilityResearch = normalizeMarketScanProbabilityResearch(page.probability_research, expectedRunId);
  page.probability_research = probabilityResearch;
  page.items.forEach((item, index) => {
    validateResultItem(item, expectedRunId, `${context}.items[${index}]`);
    if (item.status === "success" && item.data_date !== run.data_date) {
      throw marketScanContractError(`${context}.items[${index}].data_date 与批次不一致`);
    }
    if (timestampsComparable(item.updated_at, run.updated_at)
        && timestampMillis(item.updated_at) > timestampMillis(run.updated_at)) {
      throw marketScanContractError(`${context}.items[${index}].updated_at 不能晚于批次 updated_at`);
    }
    item.upside_probabilities = normalizeMarketScanUpsideProbabilities(item.upside_probabilities, probabilityResearch);
  });
  requireUniqueBy(page.items, (item) => item.symbol, `${context}.items`, "股票");
  return page;
}

export {
  normalizeDiscoveryLeaderboard,
  rankChangeLabel,
  validateDiscoveryPreset,
  validateDiscoveryPresetPage,
  validateDiscoveryRankChanges,
  validateDiscoveryResearchQueueResponse,
} from "./discovery-contracts.js";

export function marketScanContractError(message) {
  const error = new Error(`扫描接口响应格式异常：${message}`);
  error.name = "MarketScanContractError";
  return error;
}

export function isMarketScanNotFoundError(error) {
  const status = Number(error?.status ?? error?.response?.status);
  if (status === 404) return true;
  return /HTTP\s*404|批次不存在|记录不存在|not\s+found/i.test(String(error?.message || ""));
}

function validateResultItem(value, expectedRunId, context) {
  const item = requireObject(value, context);
  requireInteger(item.run_id, `${context}.run_id`, { min: 1 });
  if (item.run_id !== expectedRunId) throw marketScanContractError(`${context}.run_id 与请求批次不匹配`);
  for (const field of ["symbol", "code", "market", "name", "updated_at"]) {
    requireString(item[field], `${context}.${field}`);
  }
  let canonicalSymbol;
  try {
    canonicalSymbol = validateUiSymbol(item.symbol);
  } catch {
    throw marketScanContractError(`${context}.symbol 不是有效的 A 股代码`);
  }
  if (canonicalSymbol !== item.symbol || canonicalSymbol !== `${item.code}.${item.market}`) {
    throw marketScanContractError(`${context}.symbol/code/market 不一致`);
  }
  requireEnum(item.status, RESULT_STATUSES, `${context}.status`);
  requireBoolean(item.is_st, `${context}.is_st`);
  requireBoolean(item.is_new, `${context}.is_new`);
  validateResultScalars(item, context);
  validateResultCollections(item, context);
  validateResultStatusFields(item, context);
  return item;
}

function validateResultScalars(item, context) {
  for (const field of [
    "industry",
    "list_date",
    "metadata_source",
    "reason",
    "error",
    "data_date",
    "quote_timestamp",
    "quote_observed_at",
    "quote_source",
    "kline_source",
    "adjustment_mode",
  ]) {
    requireNullableString(item[field], `${context}.${field}`);
  }
  requireNullableInteger(item.rank, `${context}.rank`, { min: 1 });
  for (const field of ["score", "trend_score", "leader_score", "data_quality_score"]) {
    requireNullableInteger(item[field], `${context}.${field}`, { min: 0, max: 100 });
  }
  requireNullableNumber(item.raw_score, `${context}.raw_score`, { min: 0, max: 100 });
  requireNullableNumber(item.price, `${context}.price`);
  requireNullableNumber(item.change_pct, `${context}.change_pct`, { min: -1000, max: 1000 });
  for (const field of ["turnover_rate", "volume_ratio", "amount"]) {
    requireNullableNumber(item[field], `${context}.${field}`, { min: 0 });
  }
  if (item.price !== null && item.price !== undefined && item.price <= 0) {
    throw marketScanContractError(`${context}.price 必须大于 0`);
  }
  requireIsoTimestamp(item.updated_at, `${context}.updated_at`);
}

function validateResultCollections(item, context) {
  if (!Array.isArray(item.tags) || item.tags.some((tag) => typeof tag !== "string")) {
    throw marketScanContractError(`${context}.tags 必须是字符串数组`);
  }
  const metrics = requireObject(item.metrics, `${context}.metrics`);
  if (Object.values(metrics).some((metric) => typeof metric !== "number" || !Number.isFinite(metric))) {
    throw marketScanContractError(`${context}.metrics 必须只包含有限数值`);
  }
  if (item.score_details !== undefined) requireObject(item.score_details, `${context}.score_details`);
  for (const field of ["quote_fallback_used", "kline_fallback_used", "metadata_degraded"]) {
    if (item[field] !== undefined) requireBoolean(item[field], `${context}.${field}`);
  }
  if (
    item.degradation_reasons !== undefined
    && (!Array.isArray(item.degradation_reasons) || item.degradation_reasons.some((reason) => typeof reason !== "string"))
  ) throw marketScanContractError(`${context}.degradation_reasons 必须是字符串数组`);
}

function validateResultStatusFields(item, context) {
  if (item.status === "success") {
    validateSuccessResultFields(item, context);
  } else {
    validateNonSuccessResultFields(item, context);
  }
}

function validateSuccessResultFields(item, context) {
  const required = ["rank", "score", "raw_score", "trend_score", "leader_score", "data_quality_score"];
  for (const field of required) {
    if (item[field] === null || item[field] === undefined) {
      throw marketScanContractError(`${context}.${field} 在 success 状态不能为空`);
    }
  }
  const provenance = ["price", "data_date", "quote_timestamp", "quote_observed_at", "quote_source", "kline_source", "adjustment_mode"];
  for (const field of provenance) {
    if (item[field] === null || item[field] === undefined || item[field] === "") {
      throw marketScanContractError(`${context}.${field} 在 success 状态不能为空`);
    }
  }
  if (item.adjustment_mode !== "qfq") throw marketScanContractError(`${context}.adjustment_mode 必须是 qfq`);
  if (item.error !== null) throw marketScanContractError(`${context} 的 success 状态不能包含 error`);
  requireIsoDate(item.data_date, `${context}.data_date`);
  requireIsoTimestamp(item.quote_timestamp, `${context}.quote_timestamp`);
  requireIsoTimestamp(item.quote_observed_at, `${context}.quote_observed_at`);
}

function validateNonSuccessResultFields(item, context) {
  const rankingFields = ["rank", "score", "raw_score", "trend_score", "leader_score", "data_quality_score"];
  for (const field of rankingFields) {
    if (item[field] !== null && item[field] !== undefined) {
      throw marketScanContractError(`${context}.${field} 在非 success 状态必须为空`);
    }
  }
  if (item.status === "pending" && (item.reason !== null || item.error !== null)) {
    throw marketScanContractError(`${context} 的 pending 状态不能包含 reason/error`);
  }
  if (["missing", "skipped"].includes(item.status) && !String(item.reason || item.error || "").trim()) {
    throw marketScanContractError(`${context} 的 ${item.status} 状态必须包含 reason 或 error`);
  }
}

function validatePageShape({ items, total, page, pageSize, pageCount, context }) {
  const expectedPageCount = total === 0 ? 0 : Math.ceil(total / pageSize);
  if (pageCount !== expectedPageCount) throw marketScanContractError(`${context}.page_count 与 total/page_size 不一致`);
  const expectedItems = page > pageCount ? 0 : Math.min(pageSize, total - ((page - 1) * pageSize));
  if (items.length !== expectedItems) throw marketScanContractError(`${context}.items 数量与当前分页不一致`);
}

function requireUniqueBy(values, selector, context, label) {
  if (new Set(values.map(selector)).size !== values.length) {
    throw marketScanContractError(`${context} 的${label}不能重复`);
  }
}

function requireObject(value, path) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw marketScanContractError(`${path} 必须是对象`);
  }
  return value;
}

function requireString(value, path) {
  if (typeof value !== "string" || !value.trim()) throw marketScanContractError(`${path} 必须是非空字符串`);
  return value;
}

function requireNullableString(value, path) {
  if (value !== null && value !== undefined && typeof value !== "string") {
    throw marketScanContractError(`${path} 必须是字符串或 null`);
  }
  return value;
}

function requireIsoDate(value, path) {
  requireString(value, path);
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) throw marketScanContractError(`${path} 必须是 YYYY-MM-DD 日期`);
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthDays = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (month < 1 || month > 12 || day < 1 || day > monthDays[month - 1]) {
    throw marketScanContractError(`${path} 不是有效日期`);
  }
  return value;
}

function requireIsoTimestamp(value, path) {
  requireString(value, path);
  if (!/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/.test(value)) {
    throw marketScanContractError(`${path} 必须是有效 ISO 时间`);
  }
  const parsed = Date.parse(value.includes(" ") ? value.replace(" ", "T") : value);
  if (!Number.isFinite(parsed)) throw marketScanContractError(`${path} 必须是有效 ISO 时间`);
  return value;
}

function timestampMillis(value) {
  return Date.parse(value.includes(" ") ? value.replace(" ", "T") : value);
}

function timestampsComparable(left, right) {
  const hasZone = (value) => /(?:Z|[+-]\d{2}:\d{2})$/.test(value);
  return hasZone(left) === hasZone(right);
}

function percentage(numerator, denominator) {
  if (denominator <= 0) return 0;
  return Math.round(Math.min(100, Math.max(0, (numerator / denominator) * 100)) * 100) / 100;
}

function closePercentage(actual, expected) {
  return Math.abs(actual - expected) <= 0.011;
}

function requireBoolean(value, path) {
  if (typeof value !== "boolean") throw marketScanContractError(`${path} 必须是布尔值`);
  return value;
}

function requireEnum(value, allowed, path) {
  if (!allowed.has(value)) throw marketScanContractError(`${path} 的值不受支持`);
  return value;
}

function requireInteger(value, path, options = {}) {
  if (!Number.isInteger(value)) throw marketScanContractError(`${path} 必须是整数`);
  if (options.min !== undefined && value < options.min) {
    throw marketScanContractError(`${path} 不能小于 ${options.min}`);
  }
  if (options.max !== undefined && value > options.max) {
    throw marketScanContractError(`${path} 不能大于 ${options.max}`);
  }
  return value;
}

function requireNullableInteger(value, path, options = {}) {
  if (value === null || value === undefined) return value;
  return requireInteger(value, path, options);
}

function requireNumber(value, path, options = {}) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw marketScanContractError(`${path} 必须是有限数值`);
  }
  if (options.min !== undefined && value < options.min) {
    throw marketScanContractError(`${path} 不能小于 ${options.min}`);
  }
  if (options.max !== undefined && value > options.max) {
    throw marketScanContractError(`${path} 不能大于 ${options.max}`);
  }
  return value;
}

function requireNullableNumber(value, path, options = {}) {
  if (value === null || value === undefined) return value;
  return requireNumber(value, path, options);
}
