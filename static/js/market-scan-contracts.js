import { validateUiSymbol } from "./symbols.js";

const ACTIVE_RUN_STATUSES = new Set(["queued", "running", "cancelling"]);
const PUBLISHED_RUN_STATUSES = new Set(["success", "degraded"]);
const RETRYABLE_RUN_STATUSES = new Set(["degraded", "failed", "cancelled", "interrupted"]);
const RUN_STATUSES = new Set([
  ...ACTIVE_RUN_STATUSES,
  ...PUBLISHED_RUN_STATUSES,
  "failed",
  "cancelled",
  "interrupted",
]);
const RUN_TRIGGERS = new Set(["manual", "scheduled", "retry"]);
const RESULT_STATUSES = new Set(["pending", "success", "missing", "skipped"]);

export function isActiveMarketScanRun(run) {
  return Boolean(run && ACTIVE_RUN_STATUSES.has(run.status));
}

export function isPublishedMarketScanRun(run) {
  return Boolean(run && PUBLISHED_RUN_STATUSES.has(run.status));
}

export function isRetryableMarketScanRun(run) {
  return Boolean(run && RETRYABLE_RUN_STATUSES.has(run.status));
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
  for (const field of ["rule_version", "as_of", "data_date", "scope", "created_at", "updated_at"]) {
    requireString(run[field], `${context}.${field}`);
  }
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
  if (run.processed_count > run.total_count) {
    throw marketScanContractError(`${context}.processed_count 不能大于 total_count`);
  }
  return run;
}

export function validateStartResponse(value, context = "扫描任务响应") {
  const response = requireObject(value, context);
  requireBoolean(response.accepted, `${context}.accepted`);
  requireBoolean(response.deduplicated, `${context}.deduplicated`);
  validateMarketScanRun(response.run, { context: `${context}.run` });
  return response;
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
  if (page.items.length > page.page_size || page.items.length > page.total) {
    throw marketScanContractError(`${context}.items 数量与分页信息不一致`);
  }
  const expectedPageCount = page.total === 0 ? 0 : Math.ceil(page.total / page.page_size);
  if (page.page_count !== expectedPageCount) {
    throw marketScanContractError(`${context}.page_count 与 total/page_size 不一致`);
  }
  page.items.forEach((item, index) => validateResultItem(item, expectedRunId, `${context}.items[${index}]`));
  return page;
}

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
  for (const field of [
    "industry",
    "list_date",
    "metadata_source",
    "reason",
    "error",
    "data_date",
    "quote_timestamp",
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
  for (const field of ["price", "change_pct", "turnover_rate", "volume_ratio", "amount"]) {
    requireNullableNumber(item[field], `${context}.${field}`);
  }
  if (!Array.isArray(item.tags) || item.tags.some((tag) => typeof tag !== "string")) {
    throw marketScanContractError(`${context}.tags 必须是字符串数组`);
  }
  const metrics = requireObject(item.metrics, `${context}.metrics`);
  if (Object.values(metrics).some((metric) => typeof metric !== "number" || !Number.isFinite(metric))) {
    throw marketScanContractError(`${context}.metrics 必须只包含有限数值`);
  }
  return item;
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
