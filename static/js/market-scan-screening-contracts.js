import { marketScanFilterElements, readMarketScanFilters } from "./market-scan-filters.js";

export const SCREEN_SPEC_SCHEMA_VERSION = "screen-spec-v2";
export const MARKET_SCAN_BREADTH_SCHEMA_VERSION = "market-scan-breadth-v1";
export const MARKET_SCAN_SCREEN_EVALUATION_SCHEMA_VERSION = "market-scan-screen-evaluation-v1";
export const MARKET_SCAN_DELTA_SCHEMA_VERSION = "market-scan-delta-v1";
export const MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION = "market-scan-screen-alert-v1";

const RANGE_FIELD_MAP = Object.freeze({
  score: "score",
  trend: "trend_score",
  change: "change_pct",
  turnover: "turnover_rate",
  amount: "amount",
  quality: "data_quality_score",
});

const SCREEN_SPEC_FIELDS = new Set([
  "schema_version", "status", "markets", "industries", "is_st", "is_new", "ranges", "keyword", "sort",
]);
const SCREEN_RANGE_LIMITS = Object.freeze({
  score: [0, 100],
  trend_score: [0, 100],
  change_pct: [-1000, 1000],
  turnover_rate: [0, 10000],
  amount: [0, 1_000_000_000_000_000],
  data_quality_score: [0, 100],
  confidence: [0, 100],
  risk: [0, 100],
  tradability: [0, 100],
});
const SCREEN_RANGE_FIELDS = new Set(Object.keys(SCREEN_RANGE_LIMITS));
const SCREEN_RANGE_BOUND_FIELDS = new Set(["min", "max"]);
const SCREEN_MARKETS = new Set(["SH", "SZ", "BJ"]);
const SCREEN_STATUSES = new Set(["success", "missing", "skipped", "pending"]);
const SCREEN_SORT_FIELDS = new Set([
  "rank", "score", "raw_score", "trend_score", "change_pct", "amount", "turnover_rate",
  "data_quality_score", "alpha_5d", "confidence", "risk", "tradability", "symbol", "market",
  "industry", "is_st", "is_new",
]);
const SCREEN_SORT_ORDERS = new Set(["asc", "desc"]);
const SCREEN_SORT_ITEM_FIELDS = new Set(["field", "order"]);
const SCREEN_RUN_STATUSES = new Set(["success", "degraded"]);
const SCREEN_MODES = new Set(["official", "intraday", "preopen"]);
const SCREEN_RESULT_STATUSES = new Set(["pending", "success", "missing", "skipped"]);
const FULL_MARKET_SCOPE = "沪市 + 深市 + 北交所当前上市A股";
const SCORE_PERCENTILES = Object.freeze(["p10", "p25", "p50", "p75", "p90"]);
const SCREEN_SPEC_DEFAULTS = Object.freeze({
  schema_version: SCREEN_SPEC_SCHEMA_VERSION,
  status: "success",
  markets: Object.freeze([]),
  industries: Object.freeze([]),
  is_st: null,
  is_new: null,
  ranges: Object.freeze({}),
  keyword: null,
  sort: Object.freeze([Object.freeze({ field: "rank", order: "asc" })]),
});

export function buildScreenSpecV2(root) {
  const elements = marketScanFilterElements(root, requiredElement);
  elements.probabilityMin = root.getElementById("marketScanProbabilityMin");
  const filters = readMarketScanFilters(elements);
  assertNoUnsupportedProbabilityFilter(filters.research);
  const spec = {
    schema_version: SCREEN_SPEC_SCHEMA_VERSION,
    status: filters.status === "all" ? null : filters.status,
    markets: [...filters.markets],
    industries: [...filters.industries],
    is_st: filters.isSt,
    is_new: filters.isNew,
    ranges: mapRanges(filters.ranges, filters.research),
    keyword: filters.keyword || null,
    sort: filters.sort.map(({ field, order }) => ({ field, order })),
  };
  return validateScreenSpec(spec, "可信筛选请求.spec");
}

export function validateMarketScanBreadth(value, expectedRunId) {
  const payload = objectValue(value, "市场宽度响应");
  requireSchema(payload, MARKET_SCAN_BREADTH_SCHEMA_VERSION, "市场宽度响应");
  validateEvidence(payload.evidence, expectedRunId, "市场宽度响应.evidence");
  const population = validatePopulation(payload.population, "市场宽度响应.population");
  const score = validateScoreSummary(payload.score, population.total, "市场宽度响应.score");
  validateChangeSummary(payload.change, population.total, "市场宽度响应.change");
  validateIndustries(payload.industries, population.total, score.present_count);
  requireDigest(payload.canonical_digest, "市场宽度响应.canonical_digest");
  return payload;
}

export function validateScreenEvaluation(value, expectedRunId) {
  const payload = objectValue(value, "可信筛选评估响应");
  requireSchema(payload, MARKET_SCAN_SCREEN_EVALUATION_SCHEMA_VERSION, "可信筛选评估响应");
  validateEvidence(payload.evidence, expectedRunId, "可信筛选评估响应.evidence");
  validateScreenSpec(payload.spec, "可信筛选评估响应.spec");
  requireString(payload.spec_digest, "可信筛选评估响应.spec_digest");
  requireDigest(payload.spec_digest, "可信筛选评估响应.spec_digest");
  requireDigest(payload.canonical_digest, "可信筛选评估响应.canonical_digest");
  requireCount(payload.population_count, "可信筛选评估响应.population_count");
  requireCount(payload.matched_count, "可信筛选评估响应.matched_count");
  if (payload.matched_count > payload.population_count) throw contractError("可信筛选评估响应.matched_count 不能大于 population_count");
  const conditionCodes = screenConditionCodes(payload.spec);
  validateFunnel(payload.funnel, conditionCodes, payload.population_count, payload.matched_count);
  validateExclusionReasons(payload.exclusion_reasons, conditionCodes, payload.population_count);
  const matchedSymbols = validateMatchedPage(payload.matched, payload.matched_count, expectedRunId);
  validateMatchedExplanations(payload.matched_explanations, matchedSymbols, conditionCodes);
  validateNearMisses(payload.near_misses, matchedSymbols, expectedRunId, conditionCodes);
  return payload;
}

export function screenEvaluationRequest(spec, options = {}) {
  validateScreenSpec(spec, "可信筛选请求.spec");
  return {
    spec,
    page: positiveInteger(options.page, 1),
    page_size: boundedInteger(options.pageSize, 100, 1, 200),
    near_miss_limit: boundedInteger(options.nearMissLimit, 20, 0, 100),
    near_miss_max_failures: boundedInteger(options.nearMissMaxFailures, 1, 1, 3),
  };
}

export function validateMarketScanDelta(value, expectedRunId) {
  const payload = objectValue(value, "同 cohort 变化响应");
  requireSchema(payload, MARKET_SCAN_DELTA_SCHEMA_VERSION, "同 cohort 变化响应");
  if (!["ready", "unavailable"].includes(payload.status)) throw contractError("同 cohort 变化响应.status 不受支持");
  const current = validateDeltaRunRef(payload.current, "同 cohort 变化响应.current");
  if (current.run_id !== positiveInteger(expectedRunId, null)) {
    throw contractError("同 cohort 变化响应.current.run_id 与当前批次不一致");
  }
  const cohort = validateDeltaCohort(payload.cohort, current);
  const summary = validateDeltaSummary(payload.summary);
  const details = deltaDetailArrays(payload);
  if (payload.status === "ready") validateReadyDelta(payload, current, cohort, summary, details);
  else validateUnavailableDelta(payload, current, summary, details);
  requireDigest(payload.canonical_digest, "同 cohort 变化响应.canonical_digest");
  return payload;
}

function validateDeltaRunRef(value, context) {
  const run = objectValue(value, context);
  const runId = positiveInteger(run.run_id, null);
  if (runId === null) throw contractError(`${context}.run_id 必须是正整数`);
  if (!new Set(["queued", "running", "cancelling", "success", "degraded", "failed", "cancelled", "interrupted"]).has(run.status)) {
    throw contractError(`${context}.status 不受支持`);
  }
  if (!SCREEN_MODES.has(run.mode)) throw contractError(`${context}.mode 不受支持`);
  for (const field of ["scope", "rule_version"]) requireString(run[field], `${context}.${field}`);
  requireIsoDate(run.data_date, `${context}.data_date`);
  validateDeltaRunSeal(run, context);
  return run;
}

function validateDeltaRunSeal(run, context) {
  const published = SCREEN_RUN_STATUSES.has(run.status);
  const sealFields = [run.snapshot_digest, run.snapshot_seal_origin, run.snapshot_sealed_at];
  if (!published) {
    if (run.finished_at !== null || sealFields.some((value) => value !== null)) throw contractError(`${context} 未发布批次不能包含完成时间或快照封印`);
    return;
  }
  requireIsoTimestamp(run.finished_at, `${context}.finished_at`);
  requireDigest(run.snapshot_digest, `${context}.snapshot_digest`);
  if (!["publication", "legacy_backfill"].includes(run.snapshot_seal_origin)) throw contractError(`${context}.snapshot_seal_origin 不受支持`);
  requireIsoTimestamp(run.snapshot_sealed_at, `${context}.snapshot_sealed_at`);
  if (Date.parse(run.snapshot_sealed_at) < Date.parse(run.finished_at)) throw contractError(`${context}.snapshot_sealed_at 不能早于 finished_at`);
}

function validateDeltaCohort(value, current) {
  const cohort = objectValue(value, "同 cohort 变化响应.cohort");
  if (!SCREEN_MODES.has(cohort.mode)) throw contractError("同 cohort 变化响应.cohort.mode 不受支持");
  for (const field of ["scope", "rule_version"]) requireString(cohort[field], `同 cohort 变化响应.cohort.${field}`);
  if (cohort.mode !== current.mode || cohort.scope !== current.scope || cohort.rule_version !== current.rule_version) {
    throw contractError("同 cohort 变化响应.cohort 与当前批次不一致");
  }
  return cohort;
}

function validateDeltaSummary(value) {
  const summary = objectValue(value, "同 cohort 变化响应.summary");
  for (const field of ["previous_present_count", "current_present_count", "compared_symbol_count"]) requireCount(summary[field], `同 cohort 变化响应.summary.${field}`);
  if (summary.compared_symbol_count > Math.min(summary.previous_present_count, summary.current_present_count)) {
    throw contractError("同 cohort 变化响应.summary.compared_symbol_count 越界");
  }
  if (summary.evidence_detail_scope !== "top100_union") throw contractError("同 cohort 变化响应.summary.evidence_detail_scope 不受支持");
  const reasonCounts = arrayValue(summary.evidence_change_reason_counts, "同 cohort 变化响应.summary.evidence_change_reason_counts");
  reasonCounts.forEach((item, index) => {
    requireString(item?.code, `同 cohort 变化响应.summary.evidence_change_reason_counts[${index}].code`);
    if (!Number.isInteger(item?.count) || item.count < 1) throw contractError(`同 cohort 变化响应.summary.evidence_change_reason_counts[${index}].count 无效`);
  });
  requireUnique(reasonCounts.map((item) => item.code), "同 cohort 变化响应.summary.evidence_change_reason_counts.code");
  return summary;
}

function deltaDetailArrays(payload) {
  return Object.fromEntries(["top_buckets", "rank_score_changes", "exposure_changes", "evidence_changes"].map((field) => [field, arrayValue(payload[field], `同 cohort 变化响应.${field}`)]));
}

function validateUnavailableDelta(payload, current, summary, details) {
  const reasons = new Set(["current_not_published", "current_not_full_market", "previous_same_cohort_not_found"]);
  if (!reasons.has(payload.unavailable_reason)) throw contractError("同 cohort 变化响应.unavailable_reason 不受支持");
  if (payload.previous !== null || Object.values(details).some((items) => items.length)) throw contractError("不可用同 cohort 变化响应不能包含上一批次或变化明细");
  if (summary.previous_present_count || summary.compared_symbol_count || summary.evidence_change_reason_counts.length) throw contractError("不可用同 cohort 变化响应摘要不能包含对比证据");
  const published = SCREEN_RUN_STATUSES.has(current.status);
  if (payload.unavailable_reason === "current_not_published" && published) throw contractError("不可用原因与当前发布状态矛盾");
  if (payload.unavailable_reason === "current_not_full_market" && (!published || current.scope === FULL_MARKET_SCOPE)) throw contractError("不可用原因与当前范围矛盾");
  if (payload.unavailable_reason === "previous_same_cohort_not_found" && (!published || current.scope !== FULL_MARKET_SCOPE)) throw contractError("缺少上一批次原因要求正式全市场快照");
}

function validateReadyDelta(payload, current, cohort, summary, details) {
  if (payload.unavailable_reason !== null) throw contractError("可用同 cohort 变化响应不能包含不可用原因");
  const previous = validateDeltaRunRef(payload.previous, "同 cohort 变化响应.previous");
  if (current.scope !== FULL_MARKET_SCOPE || previous.scope !== cohort.scope || previous.mode !== cohort.mode || previous.rule_version !== cohort.rule_version) {
    throw contractError("可用同 cohort 变化响应前后批次不属于同一完整全市场 cohort");
  }
  if (previous.run_id === current.run_id || Date.parse(previous.finished_at) >= Date.parse(current.finished_at)) throw contractError("同 cohort 变化响应前后批次时序无效");
  validateTopBuckets(details.top_buckets);
  validateRankScoreChanges(details.rank_score_changes);
  validateExposureChanges(details.exposure_changes);
  validateEvidenceChanges(details.evidence_changes, summary.evidence_change_reason_counts);
}

function validateTopBuckets(value) {
  if (!sameValues(value.map((bucket) => bucket?.top_n), [20, 50, 100])) throw contractError("同 cohort 变化响应.top_buckets 必须完整包含 Top20/50/100");
  value.forEach((bucket, index) => validateTopBucket(bucket, index));
}

function validateTopBucket(value, index) {
  const context = `同 cohort 变化响应.top_buckets[${index}]`;
  const bucket = objectValue(value, context);
  for (const field of ["previous_count", "current_count", "retained_count"]) requireCount(bucket[field], `${context}.${field}`);
  const entrants = validateMembershipItems(bucket.entrants, `${context}.entrants`);
  const exits = validateMembershipItems(bucket.exits, `${context}.exits`);
  const unrankable = validateMembershipItems(bucket.present_but_unrankable, `${context}.present_but_unrankable`);
  if (bucket.previous_count > bucket.top_n || bucket.current_count > bucket.top_n || bucket.retained_count > Math.min(bucket.previous_count, bucket.current_count)) throw contractError(`${context} Top-N 计数越界`);
  if (bucket.current_count !== bucket.retained_count + entrants.length || bucket.previous_count !== bucket.retained_count + exits.length + unrankable.length) throw contractError(`${context} Top-N 计数不守恒`);
  requireUnique([...entrants, ...exits, ...unrankable], `${context} 股票`);
}

function validateMembershipItems(value, context) {
  const items = arrayValue(value, context);
  return items.map((item, index) => {
    const current = objectValue(item, `${context}[${index}]`);
    for (const field of ["symbol", "name", "market"]) requireString(current[field], `${context}[${index}].${field}`);
    if (!SCREEN_MARKETS.has(current.market) || !current.symbol.endsWith(`.${current.market}`)) throw contractError(`${context}[${index}] symbol/market 不一致`);
    const reasons = arrayValue(current.reason_codes, `${context}[${index}].reason_codes`);
    if (!reasons.length) throw contractError(`${context}[${index}].reason_codes 不能为空`);
    requireUnique(reasons, `${context}[${index}].reason_codes`);
    return current.symbol;
  });
}

function validateRankScoreChanges(value) {
  const symbols = value.map((item, index) => {
    const context = `同 cohort 变化响应.rank_score_changes[${index}]`;
    const change = objectValue(item, context);
    for (const field of ["previous_rank", "current_rank"]) if (positiveInteger(change[field], null) === null) throw contractError(`${context}.${field} 无效`);
    if (change.rank_change !== change.previous_rank - change.current_rank) throw contractError(`${context}.rank_change 不一致`);
    if (!nullableDifferenceMatches(change.previous_raw_score, change.current_raw_score, change.raw_score_change)) throw contractError(`${context}.raw_score_change 不一致`);
    return requireString(change.symbol, `${context}.symbol`);
  });
  requireUnique(symbols, "同 cohort 变化响应.rank_score_changes.symbol");
}

function validateExposureChanges(value) {
  value.forEach((item, index) => {
    const context = `同 cohort 变化响应.exposure_changes[${index}]`;
    const change = objectValue(item, context);
    for (const field of ["previous_count", "current_count"]) requireCount(change[field], `${context}.${field}`);
    if (change.count_change !== change.current_count - change.previous_count) throw contractError(`${context}.count_change 不一致`);
    for (const field of ["previous_share", "current_share", "share_change"]) requireNullableNumber(change[field], `${context}.${field}`);
    if (!closeNumber(change.share_change, change.current_share - change.previous_share)) throw contractError(`${context}.share_change 不一致`);
  });
}

function validateEvidenceChanges(value, expectedCounts) {
  const counts = new Map();
  const symbols = value.map((item, index) => {
    const context = `同 cohort 变化响应.evidence_changes[${index}]`;
    const change = objectValue(item, context);
    const reasons = arrayValue(change.reason_codes, `${context}.reason_codes`);
    if (!reasons.length) throw contractError(`${context}.reason_codes 不能为空`);
    requireUnique(reasons, `${context}.reason_codes`);
    reasons.forEach((reason) => counts.set(reason, (counts.get(reason) || 0) + 1));
    return requireString(change.symbol, `${context}.symbol`);
  });
  requireUnique(symbols, "同 cohort 变化响应.evidence_changes.symbol");
  const observed = expectedCounts.map((item) => [item.code, item.count]);
  if (JSON.stringify(observed) !== JSON.stringify([...counts.entries()].sort(([left], [right]) => left.localeCompare(right)))) throw contractError("证据变化原因计数与明细不一致");
}

function nullableDifferenceMatches(before, after, change) {
  if (before === null || after === null) return change === null;
  return closeNumber(change, after - before);
}

function closeNumber(left, right) {
  return typeof left === "number" && Number.isFinite(left) && Math.abs(left - right) <= 1e-12;
}

export function validateMarketScanScreenAlert(value, expectedPresetId, expectedRunId) {
  const payload = objectValue(value, "筛选变化提醒响应");
  requireSchema(payload, MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION, "筛选变化提醒响应");
  if (!["ready", "unavailable"].includes(payload.status)) throw contractError("筛选变化提醒响应.status 不受支持");
  const preset = objectValue(payload.preset, "筛选变化提醒响应.preset");
  const current = objectValue(payload.current, "筛选变化提醒响应.current");
  if (positiveInteger(preset.preset_id, null) !== positiveInteger(expectedPresetId, null)) {
    throw contractError("筛选变化提醒响应.preset_id 与当前方案不一致");
  }
  if (positiveInteger(current.run_id, null) !== positiveInteger(expectedRunId, null)) {
    throw contractError("筛选变化提醒响应.current.run_id 与当前批次不一致");
  }
  if (positiveInteger(preset.preset_revision, null) === null) {
    throw contractError("筛选变化提醒响应.preset_revision 无效");
  }
  requireDigest(preset.spec_digest, "筛选变化提醒响应.preset.spec_digest");
  const symbolSets = [];
  for (const field of ["entered_symbols", "exited_symbols", "suppressed_unrankable_symbols"]) {
    const values = arrayValue(payload[field], `筛选变化提醒响应.${field}`);
    values.forEach((symbol, index) => {
      requireString(symbol, `筛选变化提醒响应.${field}[${index}]`);
    });
    const unique = new Set(values);
    if (unique.size !== values.length) throw contractError(`筛选变化提醒响应.${field} 不能重复`);
    symbolSets.push(unique);
  }
  if (symbolSets.some((left, index) => symbolSets.slice(index + 1).some((right) => [...left].some((symbol) => right.has(symbol))))) {
    throw contractError("筛选变化提醒响应的股票集合不能重叠");
  }
  requireDigest(payload.event_digest, "筛选变化提醒响应.event_digest");
  if (typeof payload.created !== "boolean") throw contractError("筛选变化提醒响应.created 必须是布尔值");
  if (payload.status === "ready" && !payload.previous) throw contractError("筛选变化提醒响应.ready 缺少 previous");
  if (payload.status === "unavailable" && payload.created) throw contractError("不可用筛选变化提醒不能标记为已创建");
  return payload;
}

export function validateScreenSpec(value, context = "ScreenSpecV2") {
  const spec = objectValue(value, context);
  assertOnlyFields(spec, SCREEN_SPEC_FIELDS, context);
  const resolved = screenSpecDefaultsView(spec);
  requireSchema(resolved, SCREEN_SPEC_SCHEMA_VERSION, context);
  if (resolved.status !== null && !SCREEN_STATUSES.has(resolved.status)) {
    throw contractError(`${context}.status 不受支持`);
  }
  validateScreenMarkets(resolved.markets, `${context}.markets`);
  validateScreenIndustries(resolved.industries, `${context}.industries`);
  if (resolved.is_st !== null && typeof resolved.is_st !== "boolean") throw contractError(`${context}.is_st 必须是布尔值或 null`);
  if (resolved.is_new !== null && typeof resolved.is_new !== "boolean") throw contractError(`${context}.is_new 必须是布尔值或 null`);
  validateScreenRanges(resolved.ranges, `${context}.ranges`);
  validateScreenKeyword(resolved.keyword, `${context}.keyword`);
  validateScreenSort(resolved.sort, `${context}.sort`);
  return spec;
}

export function screeningContractError(message) {
  return contractError(message);
}

function mapRanges(ranges, research) {
  const mapped = Object.fromEntries(Object.entries(ranges || {}).map(([field, value]) => [RANGE_FIELD_MAP[field] || field, { ...value }]));
  if (research.confidenceMin !== null) mapped.confidence = { min: research.confidenceMin };
  if (research.riskMax !== null) mapped.risk = { max: research.riskMax };
  if (research.tradabilityMin !== null) mapped.tradability = { min: research.tradabilityMin };
  return mapped;
}

function assertNoUnsupportedProbabilityFilter(research) {
  if (research?.probabilityMin === null || research?.probabilityMin === undefined) return;
  throw contractError("可信筛选首版暂不执行上涨概率条件；请清除该条件后重试，其他冻结研究分仍可使用");
}

function screenSpecDefaultsView(spec) {
  return Object.fromEntries(Object.entries(SCREEN_SPEC_DEFAULTS).map(([field, fallback]) => {
    const value = Object.hasOwn(spec, field) ? spec[field] : undefined;
    return [field, value === undefined ? fallback : value];
  }));
}

function validateScreenMarkets(value, context) {
  const markets = arrayValue(value, context);
  if (markets.length > 3) throw contractError(`${context} 最多允许 3 项`);
  markets.forEach((market, index) => {
    if (typeof market !== "string" || !SCREEN_MARKETS.has(market)) {
      throw contractError(`${context}[${index}] 不受支持`);
    }
  });
  requireUnique(markets, context);
}

function validateScreenIndustries(value, context) {
  const industries = arrayValue(value, context);
  if (industries.length > 20) throw contractError(`${context} 最多允许 20 项`);
  const normalized = industries.map((industry, index) => {
    if (typeof industry !== "string") throw contractError(`${context}[${index}] 必须是字符串`);
    const item = normalizeScreenText(industry);
    if (!item) throw contractError(`${context}[${index}] 不能为空`);
    if (hasControlCharacter(item)) throw contractError(`${context}[${index}] 不能包含控制字符`);
    return item;
  });
  requireUnique(normalized, context);
}

function validateScreenRanges(value, context) {
  const ranges = objectValue(value, context);
  assertOnlyFields(ranges, SCREEN_RANGE_FIELDS, context);
  Object.entries(ranges).forEach(([field, range]) => {
    if (range === null) return;
    validateScreenRange(range, SCREEN_RANGE_LIMITS[field], `${context}.${field}`);
  });
}

function validateScreenRange(value, limits, context) {
  const range = objectValue(value, context);
  assertOnlyFields(range, SCREEN_RANGE_BOUND_FIELDS, context);
  const lower = range.min ?? null;
  const upper = range.max ?? null;
  if (lower === null && upper === null) throw contractError(`${context} 至少需要一个边界`);
  validateRangeBoundary(lower, limits, `${context}.min`);
  validateRangeBoundary(upper, limits, `${context}.max`);
  if (lower !== null && upper !== null && lower > upper) throw contractError(`${context} 下限不能大于上限`);
}

function validateRangeBoundary(value, limits, context) {
  if (value === null) return;
  if (typeof value !== "number" || !Number.isFinite(value)) throw contractError(`${context} 必须是有限数值或 null`);
  if (value < limits[0] || value > limits[1]) throw contractError(`${context} 超出允许范围`);
}

function validateScreenKeyword(value, context) {
  if (value === null) return;
  if (typeof value !== "string") throw contractError(`${context} 必须是字符串或 null`);
  if ([...value.trim()].length > 80) throw contractError(`${context} 最多允许 80 个字符`);
}

function validateScreenSort(value, context) {
  const sort = arrayValue(value, context);
  if (sort.length < 1 || sort.length > 3) throw contractError(`${context} 必须包含 1 至 3 项`);
  const fields = sort.map((value, index) => {
    const itemContext = `${context}[${index}]`;
    const item = objectValue(value, itemContext);
    assertOnlyFields(item, SCREEN_SORT_ITEM_FIELDS, itemContext);
    if (!SCREEN_SORT_FIELDS.has(item.field)) throw contractError(`${itemContext}.field 不受支持`);
    if (!SCREEN_SORT_ORDERS.has(item.order)) throw contractError(`${itemContext}.order 不受支持`);
    return item.field;
  });
  requireUnique(fields, `${context}字段`);
}

function assertOnlyFields(value, allowed, context) {
  const unsupported = Object.keys(value).find((field) => !allowed.has(field));
  if (unsupported) throw contractError(`${context}.${unsupported} 不受支持`);
}

function requireUnique(values, context) {
  if (new Set(values).size !== values.length) throw contractError(`${context} 不能重复`);
}

function normalizeScreenText(value) {
  return value.trim().split(/\s+/u).filter(Boolean).join(" ");
}

function hasControlCharacter(value) {
  return /[\u0000-\u001f\u007f]/u.test(value);
}

function validateEvidence(value, expectedRunId, context) {
  const evidence = objectValue(value, context);
  if (positiveInteger(evidence.run_id, null) !== positiveInteger(expectedRunId, null)) {
    throw contractError(`${context}.run_id 与当前冻结批次不一致`);
  }
  if (!SCREEN_RUN_STATUSES.has(evidence.status)) throw contractError(`${context}.status 必须是已发布状态`);
  if (!SCREEN_MODES.has(evidence.mode)) throw contractError(`${context}.mode 不受支持`);
  if (evidence.scope !== FULL_MARKET_SCOPE) throw contractError(`${context}.scope 不是完整全市场范围`);
  for (const field of ["data_date", "quote_date"]) requireIsoDate(evidence[field], `${context}.${field}`);
  requireString(evidence.rule_version, `${context}.rule_version`);
  requireIsoTimestamp(evidence.finished_at, `${context}.finished_at`);
  requireDigest(evidence.snapshot_digest, `${context}.snapshot_digest`);
  if (!["publication", "legacy_backfill"].includes(evidence.snapshot_seal_origin)) {
    throw contractError(`${context}.snapshot_seal_origin 不受支持`);
  }
  requireIsoTimestamp(evidence.snapshot_sealed_at, `${context}.snapshot_sealed_at`);
  if (Date.parse(evidence.snapshot_sealed_at) < Date.parse(evidence.finished_at)) throw contractError(`${context}.snapshot_sealed_at 不能早于 finished_at`);
}

function validatePopulation(value, context) {
  const population = objectValue(value, context);
  requireCount(population.total, `${context}.total`);
  const byStatus = validateCountMap(population.by_status, SCREEN_RESULT_STATUSES, `${context}.by_status`);
  const byMarket = validateCountMap(population.by_market, SCREEN_MARKETS, `${context}.by_market`);
  if (sumValues(byStatus) !== population.total || sumValues(byMarket) !== population.total) {
    throw contractError(`${context} 分类计数与 total 不守恒`);
  }
  return population;
}

function validateScoreSummary(value, total, context) {
  const score = objectValue(value, context);
  requireCount(score.present_count, `${context}.present_count`);
  requireCount(score.missing_count, `${context}.missing_count`);
  if (score.present_count + score.missing_count !== total) throw contractError(`${context} 可用/缺失计数与总体不守恒`);
  for (const field of ["min", "max", "mean"]) requireNullableNumber(score[field], `${context}.${field}`);
  validateScoreAvailability(score, context);
  validatePercentiles(score, context);
  validateScoreBins(score, context);
  return score;
}

function validateChangeSummary(value, total, context) {
  const change = objectValue(value, context);
  for (const field of ["advancing", "flat", "declining", "missing"]) requireCount(change[field], `${context}.${field}`);
  if (sumValues(change) !== total) throw contractError(`${context} 与总体数量不守恒`);
}

function validateIndustry(value, context) {
  const industry = objectValue(value, context);
  if (industry.industry !== null && typeof industry.industry !== "string") throw contractError(`${context}.industry 必须是字符串或 null`);
  requireCount(industry.count, `${context}.count`);
  requireCount(industry.score_present_count, `${context}.score_present_count`);
  requireNullableNumber(industry.average_score, `${context}.average_score`);
}

function validateIndustries(value, total, scorePresentCount) {
  const industries = arrayValue(value, "市场宽度响应.industries");
  industries.forEach((item, index) => validateIndustry(item, `市场宽度响应.industries[${index}]`));
  const keys = industries.map((item) => item.industry);
  requireUnique(keys, "市场宽度响应.industries.industry");
  if (industries.reduce((sum, item) => sum + item.count, 0) !== total) throw contractError("市场宽度响应.industries 数量与总体不守恒");
  if (industries.reduce((sum, item) => sum + item.score_present_count, 0) !== scorePresentCount) {
    throw contractError("市场宽度响应.industries 评分数量与总体不守恒");
  }
  industries.forEach((item, index) => validateIndustryAvailability(item, index));
}

function validateIndustryAvailability(industry, index) {
  const context = `市场宽度响应.industries[${index}]`;
  if (industry.score_present_count > industry.count) throw contractError(`${context}.score_present_count 不能大于 count`);
  if ((industry.score_present_count === 0) !== (industry.average_score === null)) {
    throw contractError(`${context}.average_score 可用性与评分数量不一致`);
  }
  if (industry.average_score !== null && (industry.average_score < 0 || industry.average_score > 100)) {
    throw contractError(`${context}.average_score 必须位于 0 至 100`);
  }
}

function validateCountMap(value, allowedKeys, context) {
  const counts = objectValue(value, context);
  Object.entries(counts).forEach(([key, count]) => {
    if (!allowedKeys.has(key)) throw contractError(`${context}.${key} 不受支持`);
    requireCount(count, `${context}.${key}`);
  });
  return counts;
}

function validateScoreAvailability(score, context) {
  const values = [score.min, score.max, score.mean];
  if (score.present_count === 0 && values.some((value) => value !== null)) throw contractError(`${context} 无可用评分时统计值必须为空`);
  if (score.present_count > 0 && values.some((value) => value === null)) throw contractError(`${context} 存在可用评分时统计值不能为空`);
  if (values.some((value) => value !== null && (value < 0 || value > 100))) throw contractError(`${context} 统计值必须位于 0 至 100`);
}

function validatePercentiles(score, context) {
  const percentiles = objectValue(score.percentiles, `${context}.percentiles`);
  if (!sameValues(Object.keys(percentiles), SCORE_PERCENTILES)) throw contractError(`${context}.percentiles 字段不完整或顺序异常`);
  const values = SCORE_PERCENTILES.map((field) => {
    requireNullableNumber(percentiles[field], `${context}.percentiles.${field}`);
    return percentiles[field];
  });
  if (score.present_count === 0 && values.some((value) => value !== null)) throw contractError(`${context}.percentiles 无评分时必须为空`);
  if (score.present_count > 0 && values.some((value) => value === null)) throw contractError(`${context}.percentiles 有评分时不能为空`);
  const present = values.filter((value) => value !== null);
  if (present.some((value, index) => value < 0 || value > 100 || (index > 0 && value < present[index - 1]))) {
    throw contractError(`${context}.percentiles 必须有序且位于 0 至 100`);
  }
}

function validateScoreBins(score, context) {
  const bins = arrayValue(score.bins, `${context}.bins`);
  if (bins.length !== 10) throw contractError(`${context}.bins 必须完整包含 10 个区间`);
  bins.forEach((bin, index) => {
    const item = objectValue(bin, `${context}.bins[${index}]`);
    requireNullableNumber(item.lower, `${context}.bins[${index}].lower`);
    requireNullableNumber(item.upper, `${context}.bins[${index}].upper`);
    requireCount(item.count, `${context}.bins[${index}].count`);
    if (item.lower !== index * 10 || item.upper !== (index + 1) * 10) throw contractError(`${context}.bins[${index}] 区间边界不正确`);
  });
  if (bins.reduce((sum, item) => sum + item.count, 0) !== score.present_count) throw contractError(`${context}.bins 与评分数量不守恒`);
}

function screenConditionCodes(spec) {
  const codes = [];
  if (spec.status !== null && spec.status !== undefined) codes.push("status");
  if (spec.markets?.length) codes.push("market");
  if (spec.industries?.length) codes.push("industry");
  if (spec.is_st !== null && spec.is_st !== undefined) codes.push("is_st");
  if (spec.is_new !== null && spec.is_new !== undefined) codes.push("is_new");
  Object.entries(spec.ranges || {}).forEach(([field, value]) => { if (value !== null) codes.push(`range.${field}`); });
  if (spec.keyword) codes.push("keyword");
  return codes;
}

function validateFunnel(value, conditionCodes, populationCount, matchedCount) {
  const steps = arrayValue(value, "可信筛选评估响应.funnel");
  if (!sameValues(steps.map((step) => step?.condition_code), conditionCodes)) throw contractError("可信筛选评估响应.funnel 与筛选规则不一致");
  let previous = populationCount;
  steps.forEach((step, index) => { previous = validateFunnelStep(step, index, previous); });
  if (previous !== matchedCount) throw contractError("可信筛选评估响应.funnel 最终数量与命中数量不一致");
}

function validateFunnelStep(value, index, previous) {
  const context = `可信筛选评估响应.funnel[${index}]`;
  const step = objectValue(value, context);
  for (const field of ["index", "input_count", "matched_count", "excluded_count", "missing_count"]) requireCount(step[field], `${context}.${field}`);
  for (const field of ["condition_code", "label"]) requireString(step[field], `${context}.${field}`);
  if (step.index !== index + 1 || step.input_count !== previous) throw contractError(`${context} 序号或输入数量不连续`);
  if (step.matched_count + step.excluded_count !== step.input_count) throw contractError(`${context} 命中与排除数量不守恒`);
  if (step.missing_count > step.excluded_count) throw contractError(`${context}.missing_count 不能大于 excluded_count`);
  return step.matched_count;
}

function validateExclusionReasons(value, conditionCodes, populationCount) {
  const reasons = arrayValue(value, "可信筛选评估响应.exclusion_reasons");
  const codes = reasons.map((reason, index) => {
    const context = `可信筛选评估响应.exclusion_reasons[${index}]`;
    const item = objectValue(reason, context);
    for (const field of ["code", "label"]) requireString(item[field], `${context}.${field}`);
    for (const field of ["count", "missing_count"]) requireCount(item[field], `${context}.${field}`);
    if (!conditionCodes.includes(item.code)) throw contractError(`${context}.code 不属于筛选规则`);
    if (item.count > populationCount || item.missing_count > item.count) throw contractError(`${context} 计数越界`);
    return item.code;
  });
  requireUnique(codes, "可信筛选评估响应.exclusion_reasons.code");
}

function validateScreenResultItem(value, expectedRunId, context) {
  const item = objectValue(value, context);
  if (positiveInteger(item.run_id, null) !== positiveInteger(expectedRunId, null)) throw contractError(`${context}.run_id 与冻结批次不一致`);
  for (const field of ["symbol", "code", "market"]) requireString(item[field], `${context}.${field}`);
  if (!SCREEN_MARKETS.has(item.market) || item.symbol !== `${item.code}.${item.market}`) throw contractError(`${context} symbol/code/market 不一致`);
  return item.symbol;
}

function validateFailedCondition(value, index, context) {
  const failure = objectValue(value, `${context}.failed_conditions[${index}]`);
  for (const field of ["code", "label"]) requireString(failure[field], `${context}.failed_conditions[${index}].${field}`);
  if (typeof failure.missing !== "boolean") throw contractError(`${context}.failed_conditions[${index}].missing 必须是布尔值`);
  const suffix = ".missing";
  return failure.code.endsWith(suffix) ? failure.code.slice(0, -suffix.length) : failure.code;
}

function validateMatchedPage(value, matchedCount, expectedRunId) {
  const context = "可信筛选评估响应.matched";
  const page = objectValue(value, context);
  const items = arrayValue(page.items, `${context}.items`);
  for (const field of ["total", "page", "page_size", "page_count"]) requireCount(page[field], `${context}.${field}`);
  if (page.page < 1 || page.page_size < 1 || page.page_size > 200 || page.total !== matchedCount) {
    throw contractError(`${context} 分页参数或 total 与命中数量不一致`);
  }
  const expectedPageCount = page.total ? Math.ceil(page.total / page.page_size) : 0;
  const expectedItems = page.page > expectedPageCount ? 0 : Math.min(page.page_size, page.total - ((page.page - 1) * page.page_size));
  if (page.page_count !== expectedPageCount || items.length !== expectedItems) throw contractError(`${context} 分页计数不守恒`);
  const symbols = items.map((item, index) => validateScreenResultItem(item, expectedRunId, `${context}.items[${index}]`));
  requireUnique(symbols, `${context}.items.symbol`);
  return symbols;
}

function validateMatchedExplanations(value, matchedSymbols, conditionCodes) {
  const explanations = arrayValue(value, "可信筛选评估响应.matched_explanations");
  if (explanations.length !== matchedSymbols.length) throw contractError("可信筛选评估响应.matched_explanations 与当前命中分页数量不一致");
  const expectedConditions = conditionCodes.length ? conditionCodes : ["all_conditions_passed"];
  explanations.forEach((value, index) => {
    const context = `可信筛选评估响应.matched_explanations[${index}]`;
    const explanation = objectValue(value, context);
    requireString(explanation.symbol, `${context}.symbol`);
    if (explanation.symbol !== matchedSymbols[index]) throw contractError(`${context}.symbol 与当前命中分页顺序不一致`);
    const conditions = arrayValue(explanation.passed_conditions, `${context}.passed_conditions`);
    conditions.forEach((condition, conditionIndex) => requireString(condition, `${context}.passed_conditions[${conditionIndex}]`));
    if (!sameValues(conditions, expectedConditions)) throw contractError(`${context}.passed_conditions 与筛选规则不一致`);
  });
}

function validateNearMisses(value, matchedSymbols, expectedRunId, conditionCodes) {
  const nearMisses = arrayValue(value, "可信筛选评估响应.near_misses");
  const symbols = nearMisses.map((nearMiss, index) => validateNearMiss(nearMiss, index, expectedRunId, conditionCodes));
  requireUnique(symbols, "可信筛选评估响应.near_misses.symbol");
  if (symbols.some((symbol) => matchedSymbols.includes(symbol))) throw contractError("近似命中股票不能与命中分页重叠");
}

function validateNearMiss(value, index, expectedRunId, conditionCodes) {
  const context = `可信筛选评估响应.near_misses[${index}]`;
  const nearMiss = objectValue(value, context);
  const symbol = validateScreenResultItem(nearMiss.item, expectedRunId, `${context}.item`);
  const failures = arrayValue(nearMiss.failed_conditions, `${context}.failed_conditions`);
  if (!failures.length) throw contractError(`${context}.failed_conditions 不能为空`);
  const codes = failures.map((condition, conditionIndex) => validateFailedCondition(condition, conditionIndex, context));
  requireUnique(codes, `${context}.failed_conditions`);
  if (codes.some((code) => !conditionCodes.includes(code))) throw contractError(`${context}.failed_conditions 包含未知条件`);
  return symbol;
}

function requiredElement(root, id) {
  const element = root?.getElementById?.(id);
  if (!element) throw contractError(`页面缺少 ${id}`);
  return element;
}

function requireSchema(value, expected, context) {
  if (value.schema_version !== expected) throw contractError(`${context}.schema_version 不受支持`);
}

function requireString(value, context) {
  if (typeof value !== "string" || !value.trim()) throw contractError(`${context} 必须是非空字符串`);
  return value;
}

function requireCount(value, context) {
  if (!Number.isInteger(value) || value < 0) throw contractError(`${context} 必须是非负整数`);
  return value;
}

function requireNullableNumber(value, context) {
  if (value !== null && (typeof value !== "number" || !Number.isFinite(value))) throw contractError(`${context} 必须是有限数值或 null`);
  return value;
}

function requireIsoDate(value, context) {
  requireString(value, context);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw contractError(`${context} 必须是规范日期`);
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    throw contractError(`${context} 必须是有效日期`);
  }
  return value;
}

function requireIsoTimestamp(value, context) {
  requireString(value, context);
  if (!/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}/.test(value)) throw contractError(`${context} 必须包含日期与时间`);
  if (Number.isNaN(Date.parse(value))) throw contractError(`${context} 必须是有效 ISO 时间`);
  return value;
}

function requireDigest(value, context) {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) throw contractError(`${context} 必须是 64 位十六进制摘要`);
  return value;
}

function arrayValue(value, context) {
  if (!Array.isArray(value)) throw contractError(`${context} 必须是数组`);
  return value;
}

function objectValue(value, context) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw contractError(`${context} 必须是对象`);
  return value;
}

function positiveInteger(value, fallback) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : fallback;
}

function sumValues(value) {
  return Object.values(value).reduce((sum, item) => sum + item, 0);
}

function sameValues(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function boundedInteger(value, fallback, minimum, maximum) {
  const number = Number(value);
  return Number.isInteger(number) && number >= minimum && number <= maximum ? number : fallback;
}

function contractError(message) {
  const error = new Error(`可信筛选接口响应格式异常：${message}`);
  error.name = "MarketScanScreeningContractError";
  return error;
}
