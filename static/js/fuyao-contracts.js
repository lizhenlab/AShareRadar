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
  return raw;
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
