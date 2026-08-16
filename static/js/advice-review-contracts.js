import { normalizeUiSymbol } from "./symbols.js";

const REVIEW_STATUSES = new Set(["pending", "insufficient", "evaluated"]);
const REVIEW_CONCLUSIONS = new Set([
  "pending", "insufficient_data", "target_hit", "stop_hit",
  "target_stop_ambiguous", "horizon_gain", "horizon_loss", "horizon_flat",
]);
const INVALID_VERSIONS = new Set(["", "unknown", "legacy"]);
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256_HEX = /^[0-9a-f]{64}$/;
const STATUS_CONCLUSIONS = Object.freeze({
  pending: new Set(["pending"]),
  insufficient: new Set(["insufficient_data"]),
  evaluated: new Set(["target_hit", "stop_hit", "target_stop_ambiguous", "horizon_gain", "horizon_loss", "horizon_flat"]),
});

export function assertReviewDetail(value, expectedSymbol = "") {
  if (!plainObject(value) || !plainObject(value.plan)) fail("复盘计划格式异常");
  const plan = assertReviewPlan(value.plan, expectedSymbol);
  const latest = value.latest_evaluation;
  if (latest !== null && latest !== undefined) assertEvaluation(latest, plan, { currentRevision: true });
  return value;
}

export function assertReviewPlan(plan, expectedSymbol = null) {
  if (!plainObject(plan)) fail("复盘计划格式异常");
  assertReviewPlanIdentity(plan, expectedSymbol);
  assertReviewPlanSnapshot(plan);
  assertReviewPlanParameters(plan);
  return plan;
}

function assertReviewPlanIdentity(plan, expectedSymbol) {
  positiveInteger(plan.id, "计划 id");
  positiveInteger(plan.advice_id, "建议 id");
  positiveInteger(plan.revision, "计划版本");
  const symbol = normalizedSymbol(plan.symbol);
  if (!symbol || (expectedSymbol && symbol !== normalizedSymbol(expectedSymbol))) fail("复盘计划股票身份不一致");
}

function assertReviewPlanSnapshot(plan) {
  requiredText(plan.snapshot_market_time, "快照时间");
  if (!Number.isFinite(Date.parse(plan.snapshot_market_time))) fail("快照时间无效");
  if (plan.snapshot_adjustment_mode !== "qfq" || !strictIsoDate(plan.snapshot_anchor_date)
    || !finitePositive(plan.snapshot_anchor_close) || !validVersion(plan.snapshot_data_version)
    || !validVersion(plan.snapshot_contract_version)) fail("复盘计划快照来源不可验证");
}

function assertReviewPlanParameters(plan) {
  requiredText(plan.hypothesis, "研究假设");
  requiredText(plan.trigger_condition, "触发说明");
  requiredText(plan.invalidation_condition, "失效说明");
  const [entry, target, stop] = reviewPrices(plan);
  if (!(target > entry && entry > stop)) fail("复盘计划价格关系无效");
  const horizon = Number(plan.horizon_days);
  if (!Number.isInteger(horizon) || horizon < 1 || horizon > 60) fail("复盘周期无效");
  if (plan.evidence_refs !== undefined && !Array.isArray(plan.evidence_refs)) fail("复盘证据格式异常");
  if (!SHA256_HEX.test(String(plan.plan_payload_digest || ""))) fail("复盘计划摘要无效");
  for (const field of ["created_at", "updated_at"]) {
    if (!Number.isFinite(Date.parse(plan[field]))) fail(`复盘计划 ${field} 无效`);
  }
}

function reviewPrices(plan) {
  return [
    positiveNumber(plan.snapshot_price, "快照价"),
    positiveNumber(plan.target_price, "目标价"),
    positiveNumber(plan.stop_price, "止损价"),
  ];
}

export function assertEvaluation(value, plan, { currentRevision = false } = {}) {
  if (!plainObject(value)) fail("复盘评估格式异常");
  positiveInteger(value.id, "评估 id");
  if (Number(value.plan_id) !== Number(plan.id)) fail("复盘评估计划身份不一致");
  if (Number(value.advice_id) !== Number(plan.advice_id)) fail("复盘评估建议身份不一致");
  if (normalizedSymbol(value.symbol) !== normalizedSymbol(plan.symbol)) fail("复盘评估股票身份不一致");
  const revision = positiveInteger(value.plan_revision, "评估计划版本");
  positiveInteger(value.attempt, "评估尝试序号");
  if (currentRevision && revision !== Number(plan.revision)) fail("复盘评估版本已过期");
  if (!currentRevision && revision > Number(plan.revision)) fail("复盘评估版本超前");
  if (!REVIEW_STATUSES.has(value.status) || !REVIEW_CONCLUSIONS.has(value.conclusion)
    || !STATUS_CONCLUSIONS[value.status]?.has(value.conclusion)) {
    fail("复盘评估状态无效");
  }
  const asOf = Date.parse(value.as_of);
  const evaluatedAt = Date.parse(value.evaluated_at);
  const snapshotAt = Date.parse(value.snapshot_market_time);
  if (![asOf, evaluatedAt, snapshotAt].every(Number.isFinite) || snapshotAt > asOf || asOf > evaluatedAt) {
    fail("复盘评估时间窗口无效");
  }
  if (![value.plan_payload_digest, value.input_digest, value.result_digest, value.source_window_digest]
    .every((digest) => SHA256_HEX.test(String(digest || "")))) fail("复盘评估摘要无效");
  const exactBindings = [
    [value.snapshot_market_time, plan.snapshot_market_time], [value.entry_price, plan.snapshot_price],
    [value.target_price, plan.target_price], [value.stop_price, plan.stop_price],
    [value.horizon_days, plan.horizon_days], [value.trigger_basis, plan.trigger_basis],
    [value.invalidation_basis, plan.invalidation_basis],
  ];
  if (revision === Number(plan.revision) && (value.plan_payload_digest !== plan.plan_payload_digest
    || exactBindings.some(([observed, expected]) => String(observed) !== String(expected)))) {
    fail("复盘评估与计划参数不一致");
  }
  const sourceSessions = nonNegativeInteger(value.source_session_count, "来源会话数");
  const expectedSessions = nonNegativeInteger(value.expected_session_count, "预期会话数");
  const forwardDays = nonNegativeInteger(value.available_forward_days, "可用前向会话数");
  if (forwardDays > sourceSessions || sourceSessions > expectedSessions) fail("复盘评估来源窗口无效");
  if (value.status === "evaluated" && (!expectedSessions || sourceSessions !== expectedSessions)) {
    fail("正式复盘来源窗口覆盖不足");
  }
  validateEvaluationMetrics(value);
  validateEvaluationDates(value);
  if (Boolean(value.target_hit) !== Boolean(value.target_hit_date)
    || Boolean(value.stop_hit) !== Boolean(value.stop_hit_date)) fail("复盘评估命中证据不一致");
  if ((value.conclusion === "target_hit" && !value.target_hit)
    || (value.conclusion === "stop_hit" && !value.stop_hit)
    || (value.conclusion === "target_stop_ambiguous" && !(value.target_hit && value.stop_hit))) {
    fail("复盘评估结论与证据不一致");
  }
  if (!["advice-review-evidence.v1", "advice-review-evidence.v2"].includes(value.evidence_contract_version)
    || value.observation_basis !== "gross_close_and_barrier_observation") fail("复盘评估观测口径无效");
  if (value.evidence_contract_version !== "advice-review-evidence.v2" && value.status === "evaluated") {
    fail("正式复盘评估缺少当前完整性合同");
  }
  return value;
}

function validateEvaluationMetrics(value) {
  const metrics = ["return_pct", "max_favorable_excursion_pct", "max_adverse_excursion_pct"];
  metrics.forEach((field) => optionalFinite(value[field], field));
  const hasMetric = metrics.some((field) => value[field] !== null && value[field] !== undefined);
  if (value.status === "evaluated") {
    if (Number(value.available_forward_days) <= 0 || !metrics.every((field) => Number.isFinite(Number(value[field])))) {
      fail("正式复盘指标不完整");
    }
    return;
  }
  if (hasMetric || value.target_hit || value.stop_hit || value.target_hit_date || value.stop_hit_date) {
    fail("未评估复盘不能携带结果指标");
  }
}

function validateEvaluationDates(value) {
  const fields = ["visible_start_date", "visible_end_date", "forward_start_date", "forward_end_date",
    "target_hit_date", "stop_hit_date"];
  fields.forEach((field) => {
    if (value[field] !== null && value[field] !== undefined && !strictIsoDate(value[field])) fail(`${field} 无效`);
  });
  if (value.visible_start_date && value.visible_end_date && value.visible_start_date > value.visible_end_date) {
    fail("可见窗口日期顺序无效");
  }
  if (value.forward_start_date && value.forward_end_date && value.forward_start_date > value.forward_end_date) {
    fail("前向窗口日期顺序无效");
  }
  for (const field of ["target_hit_date", "stop_hit_date"]) {
    if (value[field] && (!value.forward_start_date || !value.forward_end_date
      || value[field] < value.forward_start_date || value[field] > value.forward_end_date)) {
      fail("命中日期不在前向窗口内");
    }
  }
}

export function assertReviewSummary(value) {
  if (!plainObject(value)) fail("全局复盘统计格式异常");
  if (!Number.isFinite(Date.parse(value.generated_at))) fail("全局复盘生成时间无效");
  const countFields = [
    "total_plan_count", "pending_count", "insufficient_count", "evaluated_count",
    "favorable_count", "unfavorable_count", "ambiguous_count", "target_hit_count", "stop_hit_count",
  ];
  countFields.forEach((field) => nonNegativeInteger(value[field], field));
  if (value.pending_count + value.insufficient_count + value.evaluated_count !== value.total_plan_count) {
    fail("全局复盘统计数量不守恒");
  }
  ["favorable_rate_pct", "average_return_pct", "average_mfe_pct", "average_mae_pct"]
    .forEach((field) => optionalFinite(value[field], field));
  validateSummaryConclusions(value);
  return value;
}

function validateSummaryConclusions(value) {
  if (!plainObject(value.conclusion_counts)) fail("复盘结论计数格式异常");
  const entries = Object.entries(value.conclusion_counts);
  const conclusionTotal = entries.reduce((total, [, count]) => total + Number(count), 0);
  if (entries.some(([conclusion, count]) => !REVIEW_CONCLUSIONS.has(conclusion)
    || !Number.isSafeInteger(Number(count)) || Number(count) < 0)
    || conclusionTotal !== value.total_plan_count) {
    fail("复盘结论计数不守恒");
  }
  const favorable = Number(value.conclusion_counts.target_hit || 0) + Number(value.conclusion_counts.horizon_gain || 0);
  const unfavorable = Number(value.conclusion_counts.stop_hit || 0) + Number(value.conclusion_counts.horizon_loss || 0);
  const decided = favorable + unfavorable;
  const expectedRate = decided ? Math.round(favorable / decided * 10000) / 100 : null;
  validateSummaryMapping(value, favorable, unfavorable, expectedRate);
}

function validateSummaryMapping(value, favorable, unfavorable, expectedRate) {
  if (favorable !== value.favorable_count || unfavorable !== value.unfavorable_count
    || Number(value.conclusion_counts.target_stop_ambiguous || 0) !== value.ambiguous_count
    || Number(value.conclusion_counts.target_hit || 0) !== value.target_hit_count
    || Number(value.conclusion_counts.stop_hit || 0) !== value.stop_hit_count
    || (expectedRate === null ? value.favorable_rate_pct !== null : Number(value.favorable_rate_pct) !== expectedRate)) {
    fail("复盘统计分母或结论映射不一致");
  }
}

export function assertDueItem(value) {
  const detail = assertReviewDetail(value);
  if (!strictIsoDate(value.due_date)) fail("复盘到期日格式异常");
  nonNegativeInteger(value.overdue_trading_days, "逾期交易日");
  return detail;
}

export function assertReviewBatch(value) {
  if (!plainObject(value) || !Array.isArray(value.items)) fail("批量复盘结果格式异常");
  const candidate = nonNegativeInteger(value.candidate_count, "候选数");
  const attempted = nonNegativeInteger(value.attempted_count, "处理数");
  const evaluated = nonNegativeInteger(value.evaluated_count, "成功数");
  const insufficient = nonNegativeInteger(value.insufficient_count, "数据不足数");
  const pending = nonNegativeInteger(value.pending_count, "待成熟数");
  const failed = nonNegativeInteger(value.failed_count, "失败数");
  if (attempted !== evaluated + insufficient + pending + failed
    || attempted !== value.items.length || candidate < attempted) {
    fail("批量复盘结果数量不守恒");
  }
  value.items.forEach((item) => {
    if (!plainObject(item) || !["evaluated", "insufficient", "pending", "failed"].includes(item.status)) {
      fail("批量复盘条目状态无效");
    }
  });
  return value;
}

export function validAdviceSnapshot(item, ownerSymbol = "") {
  if (!plainObject(item) || !positiveIntegerOrNull(item.id)) return false;
  const symbol = normalizedSymbol(item.symbol);
  if (!symbol || (ownerSymbol && symbol !== normalizedSymbol(ownerSymbol))) return false;
  return Boolean(
    requiredTextOrEmpty(item.market_time)
    && finitePositive(item.price)
    && item.kline_adjustment_mode === "qfq"
    && strictIsoDate(item.kline_anchor_date)
    && finitePositive(item.kline_anchor_close)
    && validVersion(item.kline_data_version)
    && validVersion(item.kline_contract_version)
    && validVersion(item.snapshot_contract_version)
    && validVersion(item.rule_version)
  );
}

export function sameReviewIdentity(left, right) {
  return Boolean(
    plainObject(left) && plainObject(right)
    && Number(left.id) === Number(right.id)
    && Number(left.advice_id) === Number(right.advice_id)
    && Number(left.revision) === Number(right.revision)
    && normalizedSymbol(left.symbol) === normalizedSymbol(right.symbol)
  );
}

function normalizedSymbol(value) {
  return normalizeUiSymbol(String(value || ""));
}

function positiveInteger(value, label) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number <= 0) fail(`${label} 无效`);
  return number;
}

function nonNegativeInteger(value, label) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 0) fail(`${label} 无效`);
  return number;
}

function positiveNumber(value, label) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) fail(`${label} 无效`);
  return number;
}

function optionalFinite(value, label) {
  if (value !== null && value !== undefined && !Number.isFinite(Number(value))) fail(`${label} 无效`);
}

function requiredText(value, label) {
  if (!requiredTextOrEmpty(value)) fail(`${label} 无效`);
}

function requiredTextOrEmpty(value) {
  return typeof value === "string" && Boolean(value.trim());
}

function strictIsoDate(value) {
  if (typeof value !== "string" || !ISO_DATE.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function validVersion(value) {
  return typeof value === "string" && !INVALID_VERSIONS.has(value.trim());
}

function finitePositive(value) {
  return Number.isFinite(Number(value)) && Number(value) > 0;
}

function positiveIntegerOrNull(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0;
}

function plainObject(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function fail(message) {
  throw new TypeError(message);
}
