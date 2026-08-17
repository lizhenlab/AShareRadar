import { validateUiSymbol } from "./symbols.js";

const CURRENT_SCHEMA_VERSION = "stock-workbench-v2";
const SYMBOL_PATHS = Object.freeze([
  ["feature_snapshot"], ["factor_lab"], ["market_regime"], ["signal_validation"],
  ["risk_reward"], ["timeframe_alignment"], ["alpha_evidence"], ["diagnosis"],
  ["evidence_chain"], ["qa_report"], ["event_digest"], ["peer_comparison"],
  ["t_strategy"], ["risk_radar"],
  ["chip_analysis"], ["leadership"], ["theme_context"], ["replay"], ["chart_marks"],
  ["insights", "overview"], ["insights", "fund_flow"], ["insights", "order_pressure"],
  ["insights", "events"], ["insights", "financial_health"], ["insights", "valuation"],
  ["insights", "lhb"], ["insights", "abnormal_events"], ["insights", "rule_matches"],
]);
const RESEARCH_CHILD_PATHS = Object.freeze(SYMBOL_PATHS.filter(
  (path) => path[0] !== "chart_marks",
));

export function validateStockWorkbenchResponse(value, expectedSymbol) {
  const workbench = objectValue(value, "个股工作台响应");
  const expected = canonicalAshareSymbol(expectedSymbol, "请求股票");
  const analysis = objectValue(workbench.analysis, "analysis");
  const quote = objectValue(analysis.quote, "analysis.quote");
  const observed = canonicalAshareSymbol(`${requiredString(quote.code, "quote.code")}.${requiredString(quote.market, "quote.market")}`, "行情股票");
  if (observed !== expected) throw contractError("个股工作台行情与请求股票不一致");

  if (workbench.schema_version !== CURRENT_SCHEMA_VERSION) {
    throw contractError("个股工作台响应版本不受支持");
  }
  const symbol = canonicalAshareSymbol(workbench.symbol, "workbench.symbol");
  if (symbol !== expected) throw contractError("个股工作台响应与请求股票不一致");
  if (workbench.research_mode !== "interactive_shadow"
      || workbench.production_effect !== "none"
      || workbench.diagnosis_production_effect !== "none") {
    throw contractError("个股工作台研究/生产边界无效");
  }

  const cohort = validateResearchCohort(workbench.research_cohort, expected, quote, analysis);
  const generatedAt = requiredTimestamp(workbench.generated_at, "generated_at");
  const contextGeneratedAt = requiredTimestamp(workbench.context_generated_at, "context_generated_at");
  if (generatedAt < contextGeneratedAt || workbench.context_generated_at !== cohort.decision_time) {
    throw contractError("个股工作台响应时间与研究上下文不一致");
  }
  for (const path of SYMBOL_PATHS) validateNestedSymbol(workbench, path, expected);
  for (const path of RESEARCH_CHILD_PATHS) validateResearchChildTime(workbench, path, cohort);
  if (!Array.isArray(workbench.insights.strategy_cards)) {
    throw contractError("insights.strategy_cards 必须是数组");
  }
  workbench.insights.strategy_cards.forEach((item, index) => {
    const label = `insights.strategy_cards[${index}]`;
    validateOwnedSymbol(item, expected, label);
    validateResearchChildTimeValue(item, label, cohort);
  });
  for (const [label, item] of [
    ["analysis.stock_profile", analysis.stock_profile], ["analysis.review", analysis.review],
  ]) {
    if (item !== null && item !== undefined) validateOwnedSymbol(item, expected, label);
  }
  for (const [label, items] of [
    ["alert_rules", workbench.alert_rules], ["alert_events", workbench.alert_events], ["notes", workbench.notes],
  ]) {
    if (!Array.isArray(items)) throw contractError(`${label} 必须是数组`);
    items.forEach((item, index) => validateOwnedSymbol(item, expected, `${label}[${index}]`));
  }
  return { ...workbench, symbol, research_cohort: cohort };
}

function validateResearchCohort(value, expected, quote, analysis) {
  const cohort = objectValue(value, "research_cohort");
  const requested = canonicalAshareSymbol(cohort.requested_symbol, "research_cohort.requested_symbol");
  const observed = canonicalAshareSymbol(cohort.observed_symbol, "research_cohort.observed_symbol");
  if (requested !== expected || observed !== expected) throw contractError("个股工作台研究 cohort 股票身份冲突");
  if (cohort.mode !== "interactive_shadow" || cohort.production_effect !== "none" || cohort.advice_persistence !== "disabled") {
    throw contractError("个股工作台研究 cohort 边界无效");
  }
  const decisionTime = requiredTimestamp(cohort.decision_time, "research_cohort.decision_time");
  const quoteEventTime = requiredTimestamp(cohort.quote_event_time, "research_cohort.quote_event_time");
  if (quoteEventTime > decisionTime) throw contractError("个股工作台行情时间晚于研究决策时点");
  if (cohort.quote_event_time !== quote.timestamp) throw contractError("个股工作台行情时间未绑定研究 cohort");
  const signalDate = requiredIsoDate(cohort.signal_date, "research_cohort.signal_date");
  const dailyBarCutoff = requiredIsoDate(cohort.daily_bar_cutoff, "research_cohort.daily_bar_cutoff");
  if (dailyBarCutoff > signalDate) throw contractError("个股工作台日K截止晚于信号日");
  if (!Array.isArray(analysis.klines)) throw contractError("analysis.klines 必须是数组");
  const klineDates = analysis.klines.map((row, index) => (
    requiredIsoDate(objectValue(row, `analysis.klines[${index}]`).date, `analysis.klines[${index}].date`)
  ));
  for (let index = 0; index < klineDates.length; index += 1) {
    if (klineDates[index] > dailyBarCutoff) throw contractError("个股工作台日K晚于研究截止日");
    if (index > 0 && klineDates[index] <= klineDates[index - 1]) {
      throw contractError("个股工作台日K必须严格递增");
    }
  }
  if (klineDates.length && klineDates.at(-1) !== dailyBarCutoff) {
    throw contractError("个股工作台日K截止与研究 cohort 不一致");
  }
  const quoteDate = marketIsoDate(quoteEventTime);
  if (quoteDate !== signalDate) {
    throw contractError("个股工作台信号日与行情事件日不一致");
  }
  return { ...cohort, requested_symbol: requested, observed_symbol: observed };
}

function marketIsoDate(timestamp) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(timestamp));
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function validateNestedSymbol(root, path, expected) {
  const label = path.join(".");
  validateOwnedSymbol(nestedObject(root, path), expected, label);
}

function validateResearchChildTime(root, path, cohort) {
  const label = path.join(".");
  const child = nestedObject(root, path);
  validateResearchChildTimeValue(child, label, cohort);
}

function validateResearchChildTimeValue(child, label, cohort) {
  const updatedAt = requiredTimestamp(child.updated_at, `${label}.updated_at`);
  const decisionTime = requiredTimestamp(cohort.decision_time, "research_cohort.decision_time");
  if (updatedAt > decisionTime) throw contractError(`${label} 更新时间晚于研究决策时点`);
  if (marketIsoDate(updatedAt) !== cohort.signal_date) {
    throw contractError(`${label} 更新时间与信号日不一致`);
  }
}

function nestedObject(root, path) {
  let value = root;
  for (const part of path) value = objectValue(value?.[part], path.join("."));
  return value;
}

function validateOwnedSymbol(value, expected, label) {
  const item = objectValue(value, label);
  const symbol = canonicalAshareSymbol(item.symbol, `${label}.symbol`);
  if (symbol !== expected) throw contractError(`${label} 与工作台股票不一致`);
}

function canonicalAshareSymbol(value, label) {
  let symbol;
  try {
    symbol = validateUiSymbol(requiredString(value, label));
  } catch {
    throw contractError(`${label} 不是有效 A 股代码`);
  }
  const [code, market] = symbol.split(".");
  const valid = market === "SH"
    ? code.startsWith("6")
    : market === "SZ"
      ? code.startsWith("0") || code.startsWith("3")
      : market === "BJ" && ["43", "83", "87", "88", "92"].some((prefix) => code.startsWith(prefix));
  if (!valid) throw contractError(`${label} 的代码与交易所不一致`);
  return symbol;
}

function requiredTimestamp(value, label) {
  const text = requiredString(value, label);
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$/);
  if (!match || !isCalendarDate(match[1], match[2], match[3])) {
    throw contractError(`${label} 必须是有效时间`);
  }
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/.test(text);
  const timestamp = Date.parse(hasTimezone ? text.replace(" ", "T") : `${text.replace(" ", "T")}+08:00`);
  if (!Number.isFinite(timestamp) || timestamp > Date.now() + 5 * 60 * 1000) {
    throw contractError(`${label} 必须是不晚于当前时间的有效时间`);
  }
  return timestamp;
}

function requiredIsoDate(value, label) {
  const text = requiredString(value, label);
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match || !isCalendarDate(match[1], match[2], match[3])) {
    throw contractError(`${label} 必须是 ISO 日期`);
  }
  return text;
}

function isCalendarDate(year, month, day) {
  const timestamp = Date.UTC(Number(year), Number(month) - 1, Number(day));
  const parsed = new Date(timestamp);
  return parsed.getUTCFullYear() === Number(year)
    && parsed.getUTCMonth() + 1 === Number(month)
    && parsed.getUTCDate() === Number(day);
}

function requiredString(value, label) {
  if (typeof value !== "string" || !value.trim()) throw contractError(`${label} 必须是非空字符串`);
  return value.trim();
}

function objectValue(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw contractError(`${label} 必须是对象`);
  return value;
}

function contractError(message) {
  const error = new Error(message);
  error.name = "StockWorkbenchContractError";
  return error;
}
