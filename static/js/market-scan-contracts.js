import { validateUiSymbol } from "./symbols.js";
export {
  buildDiscoveryPresetDefinition,
  isDiscoveryPresetUiRepresentable,
} from "./market-scan-filters.js";

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
const MARKET_SCAN_MODES = new Set(["official", "intraday"]);
const RESULT_STATUSES = new Set(["pending", "success", "missing", "skipped"]);
const MARKET_SCAN_STAGES = new Set(["stock_pool", "bulk_quotes", "klines", "scoring", "persistence", "publication"]);

export function isActiveMarketScanRun(run) {
  return Boolean(run && ACTIVE_RUN_STATUSES.has(run.status));
}

export function isPublishedMarketScanRun(run) {
  return Boolean(run && PUBLISHED_RUN_STATUSES.has(run.status));
}

export function isRetryableMarketScanRun(run) {
  return Boolean(run && RETRYABLE_RUN_STATUSES.has(run.status));
}

export function defaultMarketScanMode(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "official";
  const weekday = date.getDay() >= 1 && date.getDay() <= 5;
  const minutes = (date.getHours() * 60) + date.getMinutes();
  const intradayWindow = minutes >= (9 * 60) + 30 && minutes < (15 * 60) + 15;
  return weekday && intradayWindow ? "intraday" : "official";
}

export function marketScanModeLabel(mode) {
  return mode === "intraday" ? "盘中临时" : "盘后正式";
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
  requireEnum(run.mode, MARKET_SCAN_MODES, `${context}.mode`);
  requireIsoDate(run.quote_date, `${context}.quote_date`);
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
  if (run.current_stage !== undefined && run.current_stage !== null) {
    requireEnum(run.current_stage, MARKET_SCAN_STAGES, `${context}.current_stage`);
  }
  if (run.stage_started_at !== undefined) requireNullableString(run.stage_started_at, `${context}.stage_started_at`);
  for (const field of ["elapsed_seconds", "throughput_per_second", "eta_seconds"]) {
    if (run[field] !== undefined && run[field] !== null) requireNumber(run[field], `${context}.${field}`, { min: 0 });
  }
  if (run.stage_metrics !== undefined) requireObject(run.stage_metrics, `${context}.stage_metrics`);
  if (run.market_progress !== undefined && !Array.isArray(run.market_progress)) {
    throw marketScanContractError(`${context}.market_progress 必须是数组`);
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

export function normalizeDiscoveryLeaderboard(value) {
  const payload = discoveryObject(value, "筛选榜单响应");
  const preset = validateDiscoveryPreset(payload.preset);
  if (!Array.isArray(payload.items)) throw new Error("筛选榜单响应的 items 必须是数组");
  const runId = discoveryPositiveInteger(payload.run_id, "筛选榜单运行批次");
  return {
    preset,
    run_id: runId,
    rule_version: String(payload.rule_version || ""),
    items: payload.items.map((value) => {
      const item = discoveryObject(value, "筛选榜单项目");
      return {
        ...item,
        run_id: runId,
        rank: item.position,
        status: "success",
        trend_score: item.trend,
        change_pct: item.change,
        turnover_rate: item.turnover,
        data_quality_score: item.quality,
      };
    }),
    total: discoveryNonNegativeInteger(payload.total, "筛选榜单总数"),
    page: discoveryPositiveInteger(payload.page, "筛选榜单页码"),
    page_size: discoveryPositiveInteger(payload.page_size, "筛选榜单分页大小"),
    page_count: discoveryNonNegativeInteger(payload.page_count, "筛选榜单总页数"),
  };
}

export function rankChangeLabel(value) {
  const movement = value?.movement;
  if (movement === "up") return `全市场排名上升 ${Math.abs(Number(value.rank_delta) || 0)}`;
  if (movement === "down") return `全市场排名下降 ${Math.abs(Number(value.rank_delta) || 0)}`;
  if (movement === "unchanged") return "全市场排名持平";
  if (movement === "new") return "全市场排名新进";
  if (movement === "exit") return "全市场排名离榜";
  return "全市场排名变化不可用";
}

export function validateDiscoveryPresetPage(value) {
  const payload = discoveryObject(value, "筛选方案列表响应");
  if (!Array.isArray(payload.items)) throw new Error("筛选方案列表响应的 items 必须是数组");
  return {
    ...payload,
    items: payload.items.map(validateDiscoveryPreset),
    total: discoveryNonNegativeInteger(payload.total, "筛选方案总数"),
  };
}

export function validateDiscoveryPreset(value) {
  const preset = discoveryObject(value, "筛选方案响应");
  const name = String(preset.name || "").trim().slice(0, 80);
  if (!name) throw new Error("筛选方案响应缺少名称");
  if (!preset.criteria || typeof preset.criteria !== "object" || Array.isArray(preset.criteria)) {
    throw new Error("筛选方案响应缺少筛选条件");
  }
  if (!Array.isArray(preset.sort) || !preset.sort.length) throw new Error("筛选方案响应缺少排序规则");
  return {
    ...preset,
    id: discoveryPositiveInteger(preset.id, "筛选方案 ID"),
    name,
    revision: discoveryPositiveInteger(preset.revision, "筛选方案修订号"),
  };
}

export function validateDiscoveryRankChanges(value, runId) {
  const payload = discoveryObject(value, "排名变化响应");
  if (discoveryPositiveInteger(payload.current_run_id, "当前排名批次") !== runId) {
    throw new Error("排名变化响应的运行批次不匹配");
  }
  if (!Array.isArray(payload.items)) throw new Error("排名变化响应的 items 必须是数组");
  return payload;
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
  for (const field of ["raw_score", "price", "change_pct", "turnover_rate", "volume_ratio", "amount"]) {
    requireNullableNumber(item[field], `${context}.${field}`);
  }
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
  return item;
}

function requireObject(value, path) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw marketScanContractError(`${path} 必须是对象`);
  }
  return value;
}

function discoveryObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label}格式异常`);
  return value;
}

function discoveryPositiveInteger(value, label) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1) throw new Error(`${label}格式异常`);
  return number;
}

function discoveryNonNegativeInteger(value, label) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 0) throw new Error(`${label}格式异常`);
  return number;
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
