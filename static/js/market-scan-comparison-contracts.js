const DIGEST = /^[0-9a-f]{64}$/;
const SYMBOL = /^[0-9]{6}\.(SH|SZ|BJ)$/;
const EVIDENCE_FIELDS = ["status", "mode", "scope", "data_date", "quote_date", "rule_version", "finished_at", "snapshot_digest", "snapshot_seal_origin", "snapshot_sealed_at"];
const TEXT_FIELDS = ["industry", "list_date", "reason", "error", "data_date", "quote_timestamp", "quote_observed_at", "quote_source", "kline_source", "metadata_source", "adjustment_mode"];
const SCORE_FIELDS = ["score", "trend_score", "leader_score", "data_quality_score"];
const DIMENSION_FIELDS = ["raw_score", "confidence", "risk", "tradability"];
const BOOLEAN_FIELDS = ["is_st", "is_new", "quote_fallback_used", "kline_fallback_used", "metadata_degraded"];
const ITEM_FIELDS = ["run_id", "symbol", "code", "market", "name", "board", "status", "rank", "price", "change_pct", "turnover_rate", "amount", "degradation_reasons", "updated_at", ...TEXT_FIELDS, ...SCORE_FIELDS, ...DIMENSION_FIELDS, ...BOOLEAN_FIELDS];

export function comparisonRunKey(run) {
  if (!run || !Number.isInteger(run.id) || run.id < 1 || !DIGEST.test(run.snapshot_digest)) return null;
  if (!["success", "degraded"].includes(run.status) || !["official", "intraday", "preopen"].includes(run.mode)) return null;
  if (run.scope !== "沪市 + 深市 + 北交所当前上市A股" || !EVIDENCE_FIELDS.every((key) => typeof run[key] === "string" && run[key])) return null;
  return JSON.stringify([run.id, ...EVIDENCE_FIELDS.map((key) => run[key]), run.updated_at]);
}

export function comparisonSymbol(value) { return typeof value === "string" && SYMBOL.test(value); }

export function validateMarketScanComparison(value, run, symbols) {
  requireObject(value, ["schema_version", "evidence", "requested_symbols", "audit_only", "ranking_basis", "action_source_eligible", "items", "canonical_digest"]);
  requireComparison(comparisonRunKey(run) !== null, "冻结批次身份不可用");
  requireComparison(value.schema_version === "market-scan-comparison-v1" && value.audit_only === true && value.ranking_basis === "frozen_base", "响应版本或用途不一致");
  requireObject(value.evidence, ["run_id", ...EVIDENCE_FIELDS]);
  requireComparison(value.evidence.run_id === run.id && EVIDENCE_FIELDS.every((key) => value.evidence[key] === run[key]), "冻结批次身份不一致");
  requireComparison(typeof value.action_source_eligible === "boolean", "来源资格缺失");
  requireComparison(!value.action_source_eligible || value.evidence.snapshot_seal_origin === "publication", "来源资格与封印冲突");
  requireComparison(typeof value.canonical_digest === "string" && DIGEST.test(value.canonical_digest), "响应摘要缺失");
  requireComparison(Array.isArray(symbols) && symbols.length >= 2 && symbols.length <= 4 && symbols.every(comparisonSymbol) && new Set(symbols).size === symbols.length, "请求股票无效");
  requireComparison(JSON.stringify(value.requested_symbols) === JSON.stringify(symbols), "请求股票或顺序不一致");
  requireComparison(Array.isArray(value.items) && value.items.length === symbols.length, "对比股票不完整");
  value.items.forEach((item, index) => validateComparisonItem(item, run, symbols[index]));
  return value;
}

function validateComparisonItem(item, run, symbol) {
  requireObject(item, ITEM_FIELDS);
  requireComparison(item.run_id === run.id && item.symbol === symbol && `${item.code}.${item.market}` === symbol, "股票归属不一致");
  for (const key of ["name", "board", "updated_at"]) requireComparison(typeof item[key] === "string", `${key} 无效`);
  for (const key of TEXT_FIELDS) requireComparison(item[key] === null || typeof item[key] === "string", `${key} 无效`);
  for (const key of BOOLEAN_FIELDS) requireComparison(typeof item[key] === "boolean", `${key} 无效`);
  for (const key of SCORE_FIELDS) requireNumber(item[key], key, 0, 100, true);
  for (const key of DIMENSION_FIELDS) requireNumber(item[key], key, 0, 100);
  requireNumber(item.rank, "rank", 1, Infinity, true);
  requireNumber(item.price, "price", Number.MIN_VALUE, Infinity);
  requireNumber(item.change_pct, "change_pct", -1000, 1000);
  for (const key of ["turnover_rate", "amount"]) requireNumber(item[key], key, 0, Infinity);
  requireComparison(["success", "missing", "skipped", "pending"].includes(item.status), "股票状态无效");
  requireComparison(Array.isArray(item.degradation_reasons) && item.degradation_reasons.every((reason) => typeof reason === "string"), "降级原因无效");
  validateComparisonStatus(item, run);
}

function validateComparisonStatus(item, run) {
  const scoreFields = ["rank", "raw_score", ...SCORE_FIELDS];
  if (item.status === "success") {
    const required = [...scoreFields, "price", "data_date", "quote_timestamp", "quote_observed_at", "quote_source", "kline_source"];
    requireComparison(required.every((key) => item[key] !== null && item[key] !== ""), "有效排名缺少冻结评分或来源");
    requireComparison(item.error === null && item.adjustment_mode === "qfq", "有效排名错误或复权方式无效");
    requireComparison(item.data_date === run.data_date, "有效排名数据日期与批次不一致");
    return;
  }
  requireComparison(scoreFields.every((key) => item[key] === null), "不可排名股票包含分数");
  if (["missing", "skipped"].includes(item.status)) requireComparison(Boolean((item.reason || item.error || "").trim()), "缺失股票没有原因");
  if (item.status === "pending") requireComparison(item.reason === null && item.error === null, "待处理股票包含失败原因");
}

function requireObject(value, fields) {
  requireComparison(value !== null && typeof value === "object" && !Array.isArray(value), "对象无效");
  requireComparison(Object.keys(value).length === fields.length && fields.every((field) => Object.hasOwn(value, field)), "字段不完整或不受支持");
}

function requireNumber(value, field, min, max, integer = false) {
  if (value === null) return;
  requireComparison(typeof value === "number" && Number.isFinite(value) && value >= min && value <= max && (!integer || Number.isInteger(value)), `${field} 无效`);
}

function requireComparison(condition, message) {
  if (!condition) throw new Error(`候选对比校验失败：${message}`);
}
