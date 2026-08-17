const FULL_MARKET_SCOPE = "沪市 + 深市 + 北交所当前上市A股";
const DIGEST = /^[0-9a-f]{64}$/;
const SYMBOL = /^\d{6}\.(SH|SZ|BJ)$/;
const BOARDS = new Set(["sh_main", "star", "sz_main", "chinext", "beijing"]);

const REPORT_FIELDS = [
  "schema_version", "status", "efficacy_status", "production_effect",
  "production_ranking_mutated", "database_write_performed", "evidence",
  "strategy_contract_version", "strategy_fingerprint", "strategy_spec", "gate_policy",
  "summary", "selected", "candidate_preview", "candidate_total", "exposure_audit",
  "draft_result_digest", "limitations", "canonical_digest",
];
const EVIDENCE_FIELDS = [
  "run_id", "status", "mode", "scope", "data_date", "quote_date", "scan_rule_version",
  "production_score_rule_version", "production_score_spec_hash", "result_count",
  "successful_result_count", "verified_point_in_time_count",
];
const GATE_FIELDS = [
  "exclude_st", "exclude_new", "suspension_evidence", "price_limit_evidence",
  "minimum_listing_days", "minimum_history_sessions", "minimum_amount_cny",
  "minimum_tradability_score", "maximum_risk_score", "adv_evidence_status",
  "capacity_basis", "maximum_notional_share_of_session_amount",
];
const SUMMARY_FIELDS = [
  "status", "no_trade", "no_trade_reasons", "evaluated_count", "eligible_count",
  "selected_count", "rejected_count", "adjusted_count", "unfilled_count",
  "target_invested_weight", "estimated_turnover", "estimated_round_trip_cost_cny",
  "residual_cash_cny", "evidence_verified_count", "replacement_attempt_count",
  "pool_exhausted", "underinvested_reason", "notes",
];
const CANDIDATE_FIELDS = [
  "symbol", "code", "name", "board", "industry", "original_rank", "utility_rank",
  "utility_score", "alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk",
  "tradability", "status", "target_weight", "target_quantity",
  "estimated_gross_amount_cny", "estimated_round_trip_cost_cny", "evidence_verified",
  "hard_filter_failures", "reasons", "rank_change_reason",
];
const EXPOSURE_FIELDS = [
  "selected_count", "selected_weight", "top10_weight", "industry_weights", "board_weights",
  "average_risk_score", "average_tradability_score", "estimated_round_trip_cost_cny",
  "estimated_turnover",
];

export function validateExecutableShadowReport(value, expectedRunId = null) {
  const report = exactObject(value, "可执行候选 Shadow", REPORT_FIELDS);
  literal(report.schema_version, "market-scan-executable-candidate-shadow-v2", "schema_version");
  literal(report.status, "research_shadow", "status");
  literal(report.efficacy_status, "not_generated", "efficacy_status");
  literal(report.production_effect, "none", "production_effect");
  literal(report.production_ranking_mutated, false, "production_ranking_mutated");
  literal(report.database_write_performed, false, "database_write_performed");
  validateEvidence(report.evidence, expectedRunId);
  literal(report.strategy_contract_version, "executable-candidate-shadow-spec-v2", "strategy_contract_version");
  digest(report.strategy_fingerprint, "strategy_fingerprint");
  validateStrategySpec(report.strategy_spec);
  validateGate(report.gate_policy);
  validateSummary(report.summary);
  validateCandidates(report.selected, "selected", true);
  validateCandidates(report.candidate_preview, "candidate_preview", false);
  integer(report.candidate_total, "candidate_total", 0);
  validateExposure(report.exposure_audit);
  digest(report.draft_result_digest, "draft_result_digest");
  stringArray(report.limitations, "limitations", 1);
  digest(report.canonical_digest, "canonical_digest");
  validateReportConsistency(report);
  return report;
}

function validateEvidence(value, expectedRunId) {
  const item = exactObject(value, "evidence", EVIDENCE_FIELDS);
  integer(item.run_id, "evidence.run_id", 1);
  oneOf(item.status, ["success", "degraded"], "evidence.status");
  literal(item.mode, "official", "evidence.mode");
  literal(item.scope, FULL_MARKET_SCOPE, "evidence.scope");
  isoDate(item.data_date, "evidence.data_date");
  isoDate(item.quote_date, "evidence.quote_date");
  nonempty(item.scan_rule_version, "evidence.scan_rule_version");
  oneOf(
    item.production_score_rule_version,
    ["full-market-score-v4", "full-market-score-v5"],
    "evidence.production_score_rule_version",
  );
  digest(item.production_score_spec_hash, "evidence.production_score_spec_hash");
  integer(item.result_count, "evidence.result_count", 0);
  integer(item.successful_result_count, "evidence.successful_result_count", 0);
  integer(item.verified_point_in_time_count, "evidence.verified_point_in_time_count", 0);
  if (expectedRunId !== null && item.run_id !== expectedRunId) fail("响应 run_id 与显式请求不一致");
  if (item.successful_result_count > item.result_count) fail("成功结果数超过冻结结果数");
  if (item.verified_point_in_time_count > item.successful_result_count) fail("PIT 校验数超过成功结果数");
}

function validateGate(value) {
  const gate = exactObject(value, "gate_policy", GATE_FIELDS);
  literal(gate.exclude_st, true, "gate_policy.exclude_st");
  literal(gate.exclude_new, true, "gate_policy.exclude_new");
  literal(gate.suspension_evidence, "frozen_daily_amount_and_reason_proxy", "gate_policy.suspension_evidence");
  literal(gate.price_limit_evidence, "frozen_daily_single_price_proxy", "gate_policy.price_limit_evidence");
  integer(gate.minimum_listing_days, "gate_policy.minimum_listing_days", 0);
  integer(gate.minimum_history_sessions, "gate_policy.minimum_history_sessions", 61);
  finite(gate.minimum_amount_cny, "gate_policy.minimum_amount_cny", 0);
  bounded(gate.minimum_tradability_score, "gate_policy.minimum_tradability_score", 0, 100);
  bounded(gate.maximum_risk_score, "gate_policy.maximum_risk_score", 0, 100);
  literal(gate.adv_evidence_status, "unavailable", "gate_policy.adv_evidence_status");
  literal(gate.capacity_basis, "frozen_session_amount_participation_proxy", "gate_policy.capacity_basis");
  bounded(gate.maximum_notional_share_of_session_amount, "gate_policy.maximum_notional_share_of_session_amount", 0, .05, true);
}

function validateSummary(value) {
  const summary = exactObject(value, "summary", SUMMARY_FIELDS);
  oneOf(summary.status, ["ready", "no_trade", "blocked"], "summary.status");
  boolean(summary.no_trade, "summary.no_trade");
  stringArray(summary.no_trade_reasons, "summary.no_trade_reasons");
  for (const field of ["evaluated_count", "eligible_count", "selected_count", "rejected_count", "adjusted_count", "unfilled_count", "evidence_verified_count"]) {
    integer(summary[field], `summary.${field}`, 0);
  }
  bounded(summary.target_invested_weight, "summary.target_invested_weight", 0, 1);
  bounded(summary.estimated_turnover, "summary.estimated_turnover", 0, 2);
  finite(summary.estimated_round_trip_cost_cny, "summary.estimated_round_trip_cost_cny", 0);
  finite(summary.residual_cash_cny, "summary.residual_cash_cny", 0);
  integer(summary.replacement_attempt_count, "summary.replacement_attempt_count", 0);
  boolean(summary.pool_exhausted, "summary.pool_exhausted");
  nullableText(summary.underinvested_reason, "summary.underinvested_reason");
  stringArray(summary.notes, "summary.notes");
  if (summary.no_trade !== (summary.status === "no_trade")) fail("summary.no_trade 与状态不一致");
  if (summary.pool_exhausted && summary.underinvested_reason === null) fail("候选池耗尽却缺少未充分投资原因");
  if (summary.target_invested_weight < .999999 && summary.underinvested_reason === null) fail("未充分投资却缺少结构化原因");
}

function validateCandidates(value, label, selectedOnly) {
  if (!Array.isArray(value) || value.length > 100) fail(`${label} 必须是最多 100 行的数组`);
  const seen = new Set();
  value.forEach((candidate, index) => {
    validateCandidate(candidate, `${label}[${index}]`);
    if (seen.has(candidate.symbol)) fail(`${label} 包含重复股票`);
    if (selectedOnly && !["selected", "constraint_adjusted"].includes(candidate.status)) fail(`${label} 含非入选状态`);
    seen.add(candidate.symbol);
  });
}

function validateCandidate(value, label) {
  const item = exactObject(value, label, CANDIDATE_FIELDS);
  pattern(item.symbol, SYMBOL, `${label}.symbol`);
  literal(item.code, item.symbol.slice(0, 6), `${label}.code`);
  text(item.name, `${label}.name`);
  nonempty(item.board, `${label}.board`);
  nullableText(item.industry, `${label}.industry`);
  nullableInteger(item.original_rank, `${label}.original_rank`, 1);
  nullableInteger(item.utility_rank, `${label}.utility_rank`, 1);
  for (const field of ["utility_score", "alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"]) {
    nullableBounded(item[field], `${label}.${field}`, 0, 100);
  }
  oneOf(item.status, ["selected", "rejected", "constraint_adjusted", "unfilled"], `${label}.status`);
  bounded(item.target_weight, `${label}.target_weight`, 0, 1);
  integer(item.target_quantity, `${label}.target_quantity`, 0);
  finite(item.estimated_gross_amount_cny, `${label}.estimated_gross_amount_cny`, 0);
  finite(item.estimated_round_trip_cost_cny, `${label}.estimated_round_trip_cost_cny`, 0);
  boolean(item.evidence_verified, `${label}.evidence_verified`);
  stringArray(item.hard_filter_failures, `${label}.hard_filter_failures`);
  stringArray(item.reasons, `${label}.reasons`);
  nonempty(item.rank_change_reason, `${label}.rank_change_reason`);
}

function validateExposure(value) {
  const audit = exactObject(value, "exposure_audit", EXPOSURE_FIELDS);
  integer(audit.selected_count, "exposure_audit.selected_count", 0);
  bounded(audit.selected_weight, "exposure_audit.selected_weight", 0, 1);
  bounded(audit.top10_weight, "exposure_audit.top10_weight", 0, 1);
  ratioMap(audit.industry_weights, "exposure_audit.industry_weights");
  ratioMap(audit.board_weights, "exposure_audit.board_weights");
  nullableBounded(audit.average_risk_score, "exposure_audit.average_risk_score", 0, 100);
  nullableBounded(audit.average_tradability_score, "exposure_audit.average_tradability_score", 0, 100);
  finite(audit.estimated_round_trip_cost_cny, "exposure_audit.estimated_round_trip_cost_cny", 0);
  bounded(audit.estimated_turnover, "exposure_audit.estimated_turnover", 0, 2);
}

function validateReportConsistency(report) {
  if (report.candidate_total < report.candidate_preview.length) fail("候选总数小于预览数量");
  if (report.candidate_total !== report.evidence.result_count) fail("候选总数与冻结结果总数不一致");
  if (report.summary.evaluated_count !== report.candidate_total) fail("评估数量与候选总数不一致");
  if (report.summary.selected_count !== report.selected.length) fail("入选数量与 selected 不一致");
  if (report.exposure_audit.selected_count !== report.selected.length) fail("暴露审计入选数不一致");
  if (report.summary.rejected_count + report.summary.selected_count + report.summary.unfilled_count !== report.summary.evaluated_count) fail("候选状态数量无法重建评估总数");
  if (report.summary.adjusted_count > report.summary.selected_count) fail("约束调整数超过入选数");
  if (report.summary.target_invested_weight !== report.exposure_audit.selected_weight) fail("入选权重摘要不一致");
  if (report.summary.estimated_turnover !== report.exposure_audit.estimated_turnover) fail("换手率摘要不一致");
  if (report.summary.estimated_round_trip_cost_cny !== report.exposure_audit.estimated_round_trip_cost_cny) fail("成本摘要不一致");
  if (report.summary.evidence_verified_count !== report.evidence.verified_point_in_time_count) fail("组合 PIT 数与批次 PIT 数不一致");
}

function validateStrategySpec(value) {
  const fields = ["name", "description", "schema_version", "universe", "exclusions", "hard_filters", "objectives", "profile", "portfolio_constraints", "rebalance_policy", "execution_policy", "evidence_policy"];
  const spec = exactObject(value, "strategy_spec", fields);
  nonempty(spec.name, "strategy_spec.name");
  text(spec.description, "strategy_spec.description");
  literal(spec.schema_version, 1, "strategy_spec.schema_version");
  validateUniverse(spec.universe);
  validateExclusions(spec.exclusions);
  validateHardFilters(spec.hard_filters);
  validateObjectives(spec.objectives);
  literal(spec.profile, "custom", "strategy_spec.profile");
  validatePortfolioConstraints(spec.portfolio_constraints);
  validateRebalance(spec.rebalance_policy);
  validateExecutionPolicy(spec.execution_policy);
  validateEvidencePolicy(spec.evidence_policy);
}

function validateUniverse(value) {
  const universe = exactObject(value, "strategy_spec.universe", ["boards"]);
  if (!Array.isArray(universe.boards) || !universe.boards.length) fail("strategy_spec.universe.boards 无效");
  universe.boards.forEach((board) => oneOf(board, BOARDS, "strategy_spec.universe.boards[]"));
  if (new Set(universe.boards).size !== universe.boards.length) fail("strategy_spec.universe.boards 重复");
}

function validateExclusions(value) {
  const fields = ["exclude_st", "exclude_new", "min_listing_days", "exclude_suspended", "min_history_sessions", "min_data_quality_score", "min_amount_cny"];
  const item = exactObject(value, "strategy_spec.exclusions", fields);
  literal(item.exclude_st, true, "strategy_spec.exclusions.exclude_st");
  literal(item.exclude_new, true, "strategy_spec.exclusions.exclude_new");
  literal(item.exclude_suspended, true, "strategy_spec.exclusions.exclude_suspended");
  integer(item.min_listing_days, "strategy_spec.exclusions.min_listing_days", 0);
  integer(item.min_history_sessions, "strategy_spec.exclusions.min_history_sessions", 61);
  integer(item.min_data_quality_score, "strategy_spec.exclusions.min_data_quality_score", 0, 100);
  finite(item.min_amount_cny, "strategy_spec.exclusions.min_amount_cny", 0);
}

function validateHardFilters(value) {
  if (!Array.isArray(value) || value.length > 30) fail("strategy_spec.hard_filters 无效");
  value.forEach((filter, index) => {
    const label = `strategy_spec.hard_filters[${index}]`;
    const item = exactObject(filter, label, ["field", "operator", "value", "period_sessions"]);
    pattern(item.field, /^[a-z][a-z0-9_]{1,63}$/, `${label}.field`);
    oneOf(item.operator, ["eq", "ne", "gt", "gte", "lt", "lte", "between", "in"], `${label}.operator`);
    jsonValue(item.value, `${label}.value`);
    nullableInteger(item.period_sessions, `${label}.period_sessions`, 1);
  });
}

function validateObjectives(value) {
  const fields = ["alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"];
  const item = exactObject(value, "strategy_spec.objectives", fields);
  fields.forEach((field) => bounded(item[field], `strategy_spec.objectives.${field}`, 0, 1));
  if (fields.reduce((sum, field) => sum + item[field], 0) <= 0) fail("目标权重不能全为零");
}

function validatePortfolioConstraints(value) {
  const fields = ["stock_count", "weighting_method", "max_stock_weight", "max_industry_positions", "max_industry_weight", "max_board_weight", "min_position_amount_cny", "max_notional_share_of_daily_amount", "custom_weights"];
  const item = exactObject(value, "strategy_spec.portfolio_constraints", fields);
  integer(item.stock_count, "strategy_spec.portfolio_constraints.stock_count", 1, 100);
  oneOf(item.weighting_method, ["equal", "risk_adjusted", "custom"], "strategy_spec.portfolio_constraints.weighting_method");
  bounded(item.max_stock_weight, "strategy_spec.portfolio_constraints.max_stock_weight", 0, 1, true);
  integer(item.max_industry_positions, "strategy_spec.portfolio_constraints.max_industry_positions", 1, 100);
  bounded(item.max_industry_weight, "strategy_spec.portfolio_constraints.max_industry_weight", 0, 1, true);
  bounded(item.max_board_weight, "strategy_spec.portfolio_constraints.max_board_weight", 0, 1, true);
  finite(item.min_position_amount_cny, "strategy_spec.portfolio_constraints.min_position_amount_cny", 0);
  bounded(item.max_notional_share_of_daily_amount, "strategy_spec.portfolio_constraints.max_notional_share_of_daily_amount", 0, .05, true);
  weightMap(item.custom_weights, "strategy_spec.portfolio_constraints.custom_weights");
}

function validateRebalance(value) {
  const fields = ["hold_sessions", "cadence", "rebalance_every_sessions", "buy_utility_threshold", "hold_utility_threshold"];
  const item = exactObject(value, "strategy_spec.rebalance_policy", fields);
  integer(item.hold_sessions, "strategy_spec.rebalance_policy.hold_sessions", 1, 60);
  oneOf(item.cadence, ["manual", "daily_after_close", "trading_day_intraday"], "strategy_spec.rebalance_policy.cadence");
  integer(item.rebalance_every_sessions, "strategy_spec.rebalance_policy.rebalance_every_sessions", 1, 60);
  bounded(item.buy_utility_threshold, "strategy_spec.rebalance_policy.buy_utility_threshold", 0, 100);
  bounded(item.hold_utility_threshold, "strategy_spec.rebalance_policy.hold_utility_threshold", 0, 100);
  if (item.hold_utility_threshold > item.buy_utility_threshold) fail("持有阈值高于买入阈值");
}

function validateExecutionPolicy(value) {
  const fields = ["t_plus_one", "respect_price_limits", "respect_suspensions", "cost_profile", "commission_rate", "minimum_commission_cny", "sell_stamp_duty_rate", "transfer_fee_rate", "buy_slippage_bps", "sell_slippage_bps"];
  const item = exactObject(value, "strategy_spec.execution_policy", fields);
  literal(item.t_plus_one, true, "strategy_spec.execution_policy.t_plus_one");
  literal(item.respect_price_limits, true, "strategy_spec.execution_policy.respect_price_limits");
  literal(item.respect_suspensions, true, "strategy_spec.execution_policy.respect_suspensions");
  literal(item.cost_profile, "conservative", "strategy_spec.execution_policy.cost_profile");
  for (const field of ["commission_rate", "sell_stamp_duty_rate"]) bounded(item[field], `strategy_spec.execution_policy.${field}`, 0, .02);
  bounded(item.transfer_fee_rate, "strategy_spec.execution_policy.transfer_fee_rate", 0, .01);
  bounded(item.minimum_commission_cny, "strategy_spec.execution_policy.minimum_commission_cny", 0, 1000);
  bounded(item.buy_slippage_bps, "strategy_spec.execution_policy.buy_slippage_bps", 0, 1000);
  bounded(item.sell_slippage_bps, "strategy_spec.execution_policy.sell_slippage_bps", 0, 1000);
}

function validateEvidencePolicy(value) {
  const fields = ["minimum_quality_score", "maximum_market_data_age_days", "maximum_fundamental_data_age_days", "allowed_sources", "blocked_sources", "require_verified_point_in_time_evidence"];
  const item = exactObject(value, "strategy_spec.evidence_policy", fields);
  integer(item.minimum_quality_score, "strategy_spec.evidence_policy.minimum_quality_score", 0, 100);
  integer(item.maximum_market_data_age_days, "strategy_spec.evidence_policy.maximum_market_data_age_days", 0, 365);
  integer(item.maximum_fundamental_data_age_days, "strategy_spec.evidence_policy.maximum_fundamental_data_age_days", 0, 730);
  stringArray(item.allowed_sources, "strategy_spec.evidence_policy.allowed_sources");
  stringArray(item.blocked_sources, "strategy_spec.evidence_policy.blocked_sources");
  literal(item.require_verified_point_in_time_evidence, true, "strategy_spec.evidence_policy.require_verified_point_in_time_evidence");
}

function exactObject(value, label, fields) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`${label} 必须是对象`);
  const actual = Object.keys(value).sort();
  const expected = [...fields].sort();
  if (actual.length !== expected.length || actual.some((field, index) => field !== expected[index])) {
    fail(`${label} 字段集合不匹配（拒绝缺失或额外字段）`);
  }
  return value;
}

function ratioMap(value, label) {
  const map = exactObjectDynamic(value, label);
  Object.entries(map).forEach(([key, amount]) => {
    nonempty(key, `${label} key`);
    bounded(amount, `${label}.${key}`, 0, 1);
  });
}

function weightMap(value, label) {
  const map = exactObjectDynamic(value, label);
  Object.entries(map).forEach(([key, amount]) => {
    pattern(key, SYMBOL, `${label} key`);
    bounded(amount, `${label}.${key}`, 0, 1, true);
  });
}

function exactObjectDynamic(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`${label} 必须是对象`);
  return value;
}

function jsonValue(value, label) {
  if (["string", "boolean"].includes(typeof value)) return;
  if (typeof value === "number") return finite(value, label);
  if (!Array.isArray(value) || !value.length) fail(`${label} 不是允许的 JSON 标量或非空数组`);
  value.forEach((item, index) => {
    if (!["string", "number"].includes(typeof item) || (typeof item === "number" && !Number.isFinite(item))) fail(`${label}[${index}] 无效`);
  });
}

function stringArray(value, label, minimum = 0) {
  if (!Array.isArray(value) || value.length < minimum) fail(`${label} 必须是字符串数组`);
  value.forEach((item, index) => nonempty(item, `${label}[${index}]`));
}

function nullableInteger(value, label, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  if (value === null) return;
  integer(value, label, minimum, maximum);
}

function nullableBounded(value, label, minimum, maximum) {
  if (value === null) return;
  bounded(value, label, minimum, maximum);
}

function nullableText(value, label) {
  if (value === null) return;
  text(value, label);
}

function integer(value, label, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) fail(`${label} 必须是有效整数`);
}

function bounded(value, label, minimum, maximum, openMinimum = false) {
  finite(value, label);
  if ((openMinimum ? value <= minimum : value < minimum) || value > maximum) fail(`${label} 超出允许范围`);
}

function finite(value, label, minimum = -Infinity) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum) fail(`${label} 必须是有限数值`);
}

function boolean(value, label) {
  if (typeof value !== "boolean") fail(`${label} 必须是布尔值`);
}

function isoDate(value, label) {
  pattern(value, /^\d{4}-\d{2}-\d{2}$/, label);
}

function digest(value, label) {
  pattern(value, DIGEST, `${label}（必须为 64 位小写 SHA-256）`);
}

function nonempty(value, label) {
  if (typeof value !== "string" || !value.trim()) fail(`${label} 必须是非空字符串`);
}

function text(value, label) {
  if (typeof value !== "string") fail(`${label} 必须是字符串`);
}

function pattern(value, expression, label) {
  if (typeof value !== "string" || !expression.test(value)) fail(`${label} 格式无效`);
}

function oneOf(value, allowed, label) {
  if (!allowed.has ? !allowed.includes(value) : !allowed.has(value)) fail(`${label} 枚举值无效`);
}

function literal(value, expected, label) {
  if (value !== expected) fail(`${label} 必须为 ${String(expected)}`);
}

function fail(message) {
  throw new Error(`可执行候选 Shadow 合同失败：${message}`);
}

export const EXECUTABLE_SHADOW_FULL_MARKET_SCOPE = FULL_MARKET_SCOPE;
