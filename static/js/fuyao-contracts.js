import { validateUiSymbol } from "./symbols.js";

export const FUYAO_JOB_LABELS = Object.freeze({
  financials: "同步财报", valuations: "同步估值", sectors: "同步板块", sentiment: "同步市场情绪",
  history_full: "下载完整历史", history_incremental: "同步历史增量",
});

export function canonicalFinancialSymbol(raw) {
  const symbol = validateUiSymbol(raw);
  const [code, market] = symbol.split(".");
  const valid = market === "SH" ? code.startsWith("6") : market === "SZ" ? /^[03]/.test(code) : /^(43|83|87|88|92)/.test(code);
  if (!valid) throw new Error("请输入 A 股股票代码");
  return symbol;
}

export function financialJobRequest(kind, options = {}) {
  if (!Object.hasOwn(FUYAO_JOB_LABELS, kind)) throw new Error("不支持的扶摇任务");
  const symbols = ["financials", "valuations", "sentiment"].includes(kind)
    ? tokenList(options.symbols || options.currentSymbol).map(canonicalFinancialSymbol) : [];
  const indexSymbols = kind === "sectors" ? tokenList(options.indexSymbols).map((value) => value.toUpperCase()) : [];
  if (symbols.length > 100 || indexSymbols.length > 20) throw new Error("每次最多 100 只股票、20 个板块");
  if (["financials", "valuations"].includes(kind) && !symbols.length) throw new Error("请填写本次同步的股票代码");
  if (indexSymbols.some((value) => !/^\d{6}\.(TI|SH|SZ)$/.test(value))) throw new Error("板块代码需包含 .TI、.SH 或 .SZ 后缀");
  const period = options.period === "quarterly" ? "quarterly" : "annual";
  return { kind, symbols: [...new Set(symbols)], index_symbols: [...new Set(indexSymbols)], period, limit: 4 };
}

function tokenList(value) {
  return String(value || "").trim().split(/[\s,，;；]+/).filter(Boolean);
}

export function verifiedStockObservation(raw, symbol) {
  if (!raw || canonicalFinancialSymbol(raw.symbol) !== symbol) throw new Error("财务响应与当前股票不一致");
  const bundle = raw.financials;
  if (bundle && (canonicalFinancialSymbol(bundle.symbol) !== symbol || !Array.isArray(bundle.periods))) {
    throw new Error("财务记录身份或报告期格式异常");
  }
  if (bundle && bundle.periods.some((period) => !validPeriod(period))) throw new Error("财务报告期或数值格式异常");
  if (raw.valuation_score && !validValuationScore(raw.valuation_score, symbol)) throw new Error("估值辅助评分身份或口径不一致");
  if (raw.value_research != null && !validValueResearch(raw.value_research, symbol)) throw new Error("价值研究摘要身份、口径或数值异常");
  return raw;
}

function validValueResearch(report, symbol) {
  if (!report || canonicalFinancialSymbol(report.symbol) !== symbol || report.schema_version !== "value-research-v1"
      || report.metric_scope !== "current_value_research" || report.ranking_effect !== "none" || report.point_in_time !== false) return false;
  if (!validTimestamp(report.evaluated_at) || !validValueValuation(report.valuation) || !Array.isArray(report.periods)
      || !Array.isArray(report.limitations) || !report.limitations.every(value => typeof value === "string")) return false;
  return report.periods.every(validValuePeriod)
    && new Set(report.periods.map(financialPeriodKey)).size === report.periods.length;
}

function validValueValuation(valuation) {
  if (!valuation || !Number.isInteger(valuation.available_inputs) || valuation.required_inputs !== 2
      || valuation.available_inputs < 0 || valuation.available_inputs > 2) return false;
  const coverage = ["unavailable", "partial", "complete"][valuation.available_inputs];
  return valuation.coverage === coverage && validValueSource(valuation) && validValuationChecks(valuation)
    && validPositiveRatio(valuation.earnings_yield_pct) && validPositiveRatio(valuation.book_to_price_pct)
    && typeof valuation.earnings_yield_reason === "string" && typeof valuation.book_to_price_reason === "string"
    && validRatioInput(valuation, "pe_ttm", valuation.earnings_yield_pct)
    && validRatioInput(valuation, "pb_mrq", valuation.book_to_price_pct)
    && (valuation.available_inputs > 0 || valuation.earnings_yield_pct === null && valuation.book_to_price_pct === null);
}

function validRatioInput(valuation, key, ratio) {
  const input = valuation.checks.find(check => check.key === key);
  return ratio === null || input.status !== "unavailable" && input.value > 0;
}

function validValuationChecks(valuation) {
  const checks = valuation.checks;
  return validValueChecks(checks) && checks.length === 2
    && checks.every(check => ["pe_ttm", "pb_mrq"].includes(check.key))
    && checks.filter(check => check.status !== "unavailable").length === valuation.available_inputs;
}

function validValuePeriod(period) {
  return period && /^\d{4}-\d{2}-\d{2}$/.test(period.period_end) && ["annual", "quarterly"].includes(period.period_type)
    && typeof period.observation_available === "boolean" && typeof period.summary === "string"
    && validValueSource(period) && validValueChecks(period.checks);
}

function validValueChecks(checks) {
  return Array.isArray(checks) && checks.every(check => check && typeof check.key === "string" && check.key.length > 0
    && typeof check.label === "string" && ["observed", "attention", "unavailable"].includes(check.status)
    && (check.value === null || typeof check.value === "number" && Number.isFinite(check.value))
    && (check.unit === null || typeof check.unit === "string") && typeof check.summary === "string"
    && typeof check.action === "string" && check.action.trim().length > 0)
    && new Set(checks.map(check => check.key)).size === checks.length;
}

function validValueSource(value) {
  return typeof value.source === "string" && value.source.trim().length > 0
    && (value.fetched_at === null || typeof value.fetched_at === "string");
}

function validTimestamp(value) {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

function validPositiveRatio(value) {
  return value === null || typeof value === "number" && Number.isFinite(value) && value > 0;
}

function validValuationScore(score, symbol) {
  return canonicalFinancialSymbol(score.symbol) === symbol && score.score_semantics === "heuristic_valuation_pressure"
    && score.ranking_effect === "individual_research_only" && score.point_in_time === false
    && (score.score_available !== true || typeof score.score === "number" && Number.isFinite(score.score)
      && score.score >= 0 && score.score <= 100);
}

function validPeriod(period) {
  if (!period || !/^\d{4}-\d{2}-\d{2}$/.test(period.period_end) || !["annual", "quarterly"].includes(period.period_type)) return false;
  if (!Array.isArray(period.metrics)) return false;
  return period.metrics.every((fact) => fact && typeof fact.key === "string" && typeof fact.label === "string"
    && (fact.value === null || typeof fact.value === "number" && Number.isFinite(fact.value))
    && (fact.raw_value === null || typeof fact.raw_value === "string"));
}

export function financialPeriodKey(period) {
  return `${period.period_end}|${period.period_type}`;
}

export function activeFuyaoJob(status, accepted = null) {
  const jobs = Array.isArray(status?.jobs) ? status.jobs : [];
  const current = accepted ? jobs.find((job) => job.id === accepted.id) || accepted : null;
  const active = jobs.find(isActiveFuyaoJob);
  if (active) return active;
  if (Array.isArray(status?.active_jobs) && !status.active_jobs.includes(current?.id)) return null;
  return isActiveFuyaoJob(current) ? current : null;
}

export function sourceValue(value, suffix = "") {
  return typeof value === "number" && Number.isFinite(value) ? `${value}${suffix}` : "未返回";
}

export function isActiveFuyaoJob(job) {
  return job?.status === "running" || job?.status === "cancelling";
}

export function canRetryFuyaoJob(job) {
  if (!job?.request || isActiveFuyaoJob(job) || job.status === "completed") return false;
  if (!["financials", "valuations"].includes(job.kind)) return true;
  const completed = new Set(job.completed_symbols || []);
  return (job.request.symbols || []).some((symbol) => !completed.has(symbol));
}

export function verifiedFuyaoJob(value, id = null) {
  if (!value || typeof value.id !== "string" || !value.id || id !== null && value.id !== id
      || !["running", "cancelling", "completed", "degraded", "failed", "cancelled", "interrupted"].includes(value.status)) {
    throw new Error("任务响应身份或状态异常");
  }
  return value;
}
