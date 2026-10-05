import { parseResultsQuery } from "./market-scan-result-query.js";
import { validateScreenSpec } from "./market-scan-screening-contracts.js";

const RANGE_QUERIES = { score: ["min_score", "max_score"], trend_score: ["min_trend_score", "max_trend_score"], change_pct: ["min_change_pct", "max_change_pct"], turnover_rate: ["min_turnover_rate", "max_turnover_rate"], amount: ["min_amount", "max_amount"], data_quality_score: ["min_data_quality_score", "max_data_quality_score"], confidence: ["min_confidence", null], risk: [null, "max_risk"], tradability: ["min_tradability", null] };
const PRESET_FIELDS = { score: "score", trend: "trend_score", change: "change_pct", turnover: "turnover_rate", amount: "amount", quality: "data_quality_score", confidence: "confidence", risk: "risk", tradability: "tradability" };
const PRESET_SORT = { ...PRESET_FIELDS, rank: "rank", raw_score: "raw_score", symbol: "symbol", market: "market", industry: "industry", is_st: "is_st", is_new: "is_new" };

export function screenSpecFromResultsQuery(query, runId) {
  const parsed = parseResultsQuery(query);
  if (parsed?.runId !== runId) throw new Error("已提交筛选与冻结批次不一致");
  const params = parsed.params;
  const allowed = new Set(["page", "page_size", "status", "market", "industry", "is_st", "is_new", "keyword", "sort", "order", "probability_horizon", ...Object.values(RANGE_QUERIES).flat().filter(Boolean)]);
  if (params.has("min_upside_probability")) throw new Error("已应用的上涨概率条件暂不支持冻结条件解释");
  if ([...params.keys()].some((key) => !allowed.has(key))) throw new Error("已提交筛选含尚不支持解释的条件");
  const ranges = Object.fromEntries(Object.entries(RANGE_QUERIES).map(([field, bounds]) => [field, queryRange(params, bounds)]).filter(([, value]) => value !== null));
  const fields = params.getAll("sort"), orders = params.getAll("order");
  if (fields.length !== orders.length) throw new Error("已提交排序字段与方向不一致");
  return normalizeScreenSpec({ schema_version: "screen-spec-v2", status: params.get("status") === "all" ? null : params.get("status") || "success",
    markets: params.getAll("market"), industries: params.getAll("industry"), is_st: queryBoolean(params, "is_st"), is_new: queryBoolean(params, "is_new"),
    keyword: params.get("keyword"), ranges, sort: fields.length ? fields.map((field, index) => ({ field, order: orders[index] })) : [{ field: "rank", order: "asc" }] });
}

export function screenSpecFromPreset(preset) {
  const criteria = preset.criteria;
  if (!criteria || typeof criteria !== "object" || Array.isArray(criteria)) throw new Error("已应用方案缺少完整条件");
  const allowed = new Set(["market", "industry", "is_st", "is_new", "keyword", ...Object.keys(PRESET_FIELDS)]);
  if (Object.keys(criteria).some((key) => criteria[key] !== null && !allowed.has(key))) throw new Error("已应用方案含尚不支持解释的条件");
  const ranges = Object.fromEntries(Object.entries(PRESET_FIELDS).filter(([field]) => criteria[field] != null).map(([field, target]) => [target, criteria[field]]));
  return normalizeScreenSpec({ schema_version: "screen-spec-v2", status: "success", markets: criteria.market || [], industries: criteria.industry || [],
    is_st: criteria.is_st ?? null, is_new: criteria.is_new ?? null, keyword: criteria.keyword ?? null, ranges,
    sort: preset.sort.map(({ field, order }) => ({ field: PRESET_SORT[field] || field, order })) });
}

export function normalizedScreenSpecKey(spec) {
  return JSON.stringify(canonicalValue(normalizeScreenSpec(spec)));
}

function normalizeScreenSpec(spec) {
  validateScreenSpec(spec, "已应用筛选条件");
  return { schema_version: "screen-spec-v2", status: spec.status === undefined ? "success" : spec.status,
    markets: [...(spec.markets || [])], industries: (spec.industries || []).map(normalizeText), is_st: spec.is_st ?? null, is_new: spec.is_new ?? null,
    keyword: spec.keyword == null ? null : normalizeText(spec.keyword) || null,
    ranges: Object.fromEntries(Object.entries(spec.ranges || {}).filter(([, value]) => value !== null)),
    sort: structuredClone(spec.sort || [{ field: "rank", order: "asc" }]) };
}

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.keys(value).sort().filter((key) => value[key] != null).map((key) => [key, canonicalValue(value[key])]));
}

function queryRange(params, bounds) {
  const values = bounds.map((key) => key && params.has(key) ? Number(params.get(key)) : null);
  return values.every((value) => value === null) ? null : { min: values[0], max: values[1] };
}

function queryBoolean(params, key) {
  if (!params.has(key)) return null;
  const value = params.get(key);
  if (!["true", "false"].includes(value)) throw new Error("已提交布尔条件无效");
  return value === "true";
}

function normalizeText(value) { return value.trim().split(/\s+/u).filter(Boolean).join(" "); }
