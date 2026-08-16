const BOARD_ORDER = ["sh_main", "star", "sz_main", "chinext", "beijing"];
const BASELINE_PRODUCTION_SCORE_RULE_VERSION = "full-market-score-v4";
const BASELINE_PRODUCTION_SCORE_SPEC_HASH = "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a";
const PRODUCTION_SCORE_RULE_VERSIONS = new Set([
  "full-market-score-v4",
  "full-market-score-v5",
]);
const PROFILE_OBJECTIVES = {
  conservative: { alpha_1d: .03, alpha_5d: .12, alpha_20d: .25, confidence: .20, risk: .25, tradability: .15 },
  balanced: { alpha_1d: .05, alpha_5d: .20, alpha_20d: .35, confidence: .15, risk: .15, tradability: .10 },
  aggressive: { alpha_1d: .10, alpha_5d: .25, alpha_20d: .40, confidence: .10, risk: .05, tradability: .10 },
};

export function validateStrategyPage(value) {
  const page = objectValue(value, "策略列表");
  if (!Array.isArray(page.items)) throw new Error("策略列表缺少 items");
  page.items.forEach(validateStrategy);
  return page;
}

export function validateStrategy(value) {
  const strategy = objectValue(value, "策略");
  positiveInteger(strategy.strategy_id, "strategy_id");
  positiveInteger(strategy.strategy_version, "strategy_version");
  fingerprint(strategy.fingerprint, "fingerprint");
  objectValue(strategy.spec, "StrategySpec");
  return strategy;
}

export function validateParsedStrategy(value) {
  const parsed = objectValue(value, "策略解析结果");
  objectValue(parsed.draft, "draft");
  objectValue(parsed.compile, "compile");
  if (!Array.isArray(parsed.ambiguities) || !Array.isArray(parsed.unsupported_clauses)) {
    throw new Error("策略解析结果缺少歧义或未支持条件");
  }
  return parsed;
}

export function validatePortfolioDraft(value) {
  const draft = objectValue(value, "组合草案");
  objectValue(draft.context, "execution context");
  objectValue(draft.summary, "portfolio summary");
  if (!Array.isArray(draft.selected)) throw new Error("组合草案缺少 selected");
  fingerprint(draft.context.strategy_fingerprint, "strategy_fingerprint");
  fingerprint(draft.context.execution_fingerprint, "execution_fingerprint");
  return draft;
}

export function validateCandidatePage(value) {
  const page = objectValue(value, "候选分页");
  if (!Array.isArray(page.items)) throw new Error("候选分页缺少 items");
  positiveInteger(page.page, "page");
  return page;
}

export function validateEvidence(value) {
  if (value === null) return null;
  const evidence = objectValue(value, "证据中心");
  fingerprint(evidence.strategy_fingerprint, "strategy_fingerprint");
  const boundary = objectValue(evidence.research_boundary, "research_boundary");
  if (
    boundary.status !== "shadow_only"
    || boundary.baseline_kind !== "offline_evaluation_baseline"
    || boundary.baseline_production_score_rule_version !== BASELINE_PRODUCTION_SCORE_RULE_VERSION
    || boundary.baseline_production_score_spec_hash !== BASELINE_PRODUCTION_SCORE_SPEC_HASH
    || !["not_available", "compatible", "incompatible"].includes(boundary.execution_contract_compatibility)
    || boundary.production_ranking_mutated !== false
    || boundary.statement !== "影子研究，不改变生产排名"
  ) {
    throw new Error("证据中心研究边界无效，已拒绝把 Shadow 结果展示为生产评分");
  }
  const execution = objectValue(evidence.execution, "execution");
  validateEvidenceExecutionContract(execution, boundary.execution_contract_compatibility);
  if (!Array.isArray(evidence.coverage) || !Array.isArray(evidence.top_n)) {
    throw new Error("证据中心缺少覆盖率或 Top N 统计");
  }
  if (!Array.isArray(evidence.shadow_candidates)) throw new Error("证据中心缺少 Shadow 候选列表");
  evidence.shadow_candidates.forEach((candidate) => {
    objectValue(candidate, "Shadow 候选");
    evidenceAvailability(candidate.evidence_status, "candidate.evidence_status");
    evidenceAvailability(objectValue(candidate.coverage, "candidate.coverage").status, "coverage.status");
    evidenceAvailability(objectValue(candidate.rank_delta_vs_production, "candidate.rank_delta_vs_production").status, "rank_delta.status");
    evidenceAvailability(objectValue(candidate.constraints, "candidate.constraints").status, "constraints.status");
    evidenceAvailability(objectValue(candidate.exposure, "candidate.exposure").status, "exposure.status");
    evidenceAvailability(objectValue(candidate.promotion_gate, "candidate.promotion_gate").status, "promotion_gate.status");
    if (!Array.isArray(candidate.top_n)) throw new Error("Shadow 候选缺少 Top N 证据");
    candidate.top_n.forEach((item) => evidenceAvailability(objectValue(item, "candidate.top_n[]").status, "top_n.status"));
  });
  const promotion = objectValue(evidence.promotion, "promotion");
  if (promotion.pbo_status !== "not_computed" || promotion.deflated_sharpe_status !== "not_computed") {
    throw new Error("证据中心不得暗示未执行的 PBO / DSR 已计算");
  }
  if (promotion.pbo_ready !== false) throw new Error("兼容字段 pbo_ready 必须保持 false");
  return evidence;
}

function validateEvidenceExecutionContract(execution, compatibility) {
  const ruleVersion = execution.production_score_rule_version;
  const specHash = execution.production_score_spec_hash;
  const hasRuleVersion = ruleVersion !== null && ruleVersion !== undefined;
  const hasSpecHash = specHash !== null && specHash !== undefined;
  if (hasRuleVersion !== hasSpecHash) {
    throw new Error("证据中心执行评分合同不完整");
  }
  if (!hasRuleVersion) {
    const expected = execution.execution_id === null || execution.execution_id === undefined
      ? "not_available"
      : "incompatible";
    if (compatibility !== expected) throw new Error("证据中心执行评分合同兼容性不一致");
    return;
  }
  if (!PRODUCTION_SCORE_RULE_VERSIONS.has(ruleVersion)) {
    throw new Error("证据中心执行评分规则版本无效");
  }
  fingerprint(specHash, "production_score_spec_hash");
  const expected = ruleVersion === BASELINE_PRODUCTION_SCORE_RULE_VERSION
    && specHash === BASELINE_PRODUCTION_SCORE_SPEC_HASH
    ? "compatible"
    : "incompatible";
  if (compatibility !== expected) throw new Error("证据中心执行评分合同兼容性不一致");
}

function evidenceAvailability(value, label) {
  if (!["available", "insufficient_data", "unavailable"].includes(value)) {
    throw new Error(`${label} 不是可识别的证据状态`);
  }
}

export function validateHistory(value) {
  const history = objectValue(value, "执行历史");
  if (!Array.isArray(history.items)) throw new Error("执行历史缺少 items");
  return history;
}

export function validateVersionPage(value) {
  const page = objectValue(value, "策略版本列表");
  if (!Array.isArray(page.items)) throw new Error("策略版本列表缺少 items");
  page.items.forEach((item) => {
    positiveInteger(item.revision, "revision");
    fingerprint(item.fingerprint, "version fingerprint");
  });
  return page;
}

export function validateVersionDiff(value) {
  const comparison = objectValue(value, "策略版本差异");
  positiveInteger(comparison.left_revision, "left_revision");
  positiveInteger(comparison.right_revision, "right_revision");
  fingerprint(comparison.left_fingerprint, "left_fingerprint");
  fingerprint(comparison.right_fingerprint, "right_fingerprint");
  if (!Array.isArray(comparison.changed_paths)) throw new Error("策略版本差异缺少 changed_paths");
  return comparison;
}

export function validateSchedule(value) {
  const schedule = objectValue(value, "策略定时任务");
  positiveInteger(schedule.schedule_id, "schedule_id");
  positiveInteger(schedule.strategy_version, "schedule strategy_version");
  fingerprint(schedule.strategy_fingerprint, "schedule strategy_fingerprint");
  if (typeof schedule.enabled !== "boolean") throw new Error("策略定时任务缺少 enabled 状态");
  return schedule;
}

export function validateSimulationPlan(value) {
  const plan = objectValue(value, "模拟交易计划");
  positiveInteger(plan.plan_id, "plan_id");
  if (!Array.isArray(plan.orders) || !Array.isArray(plan.disclaimers)) {
    throw new Error("模拟交易计划缺少委托或研究边界");
  }
  fingerprint(plan.plan_digest, "plan_digest");
  fingerprint(plan.strategy_fingerprint, "plan strategy_fingerprint");
  fingerprint(plan.execution_fingerprint, "plan execution_fingerprint");
  return plan;
}

export function strategySpecFromEditor(root, base = null) {
  const spec = structuredClone(base || defaultStrategySpec(root));
  const boards = Array.from(root.querySelectorAll("[data-strategy-board]:checked"), (item) => item.value);
  if (!boards.length) throw new Error("至少选择一个上市板块");
  spec.name = textValue(root, "strategyName");
  spec.profile = root.getElementById("strategyProfile").value;
  spec.objectives = spec.profile === "custom" ? customObjectivesValue(root) : PROFILE_OBJECTIVES[spec.profile];
  spec.universe.boards = BOARD_ORDER.filter((board) => boards.includes(board));
  const quality = numberValue(root, "strategyMinQuality", 0, 100);
  spec.exclusions.min_data_quality_score = quality;
  spec.evidence_policy.minimum_quality_score = quality;
  spec.portfolio_constraints.stock_count = numberValue(root, "strategyStockCount", 1, 100);
  spec.portfolio_constraints.weighting_method = root.getElementById("strategyWeightingMethod").value;
  spec.portfolio_constraints.max_stock_weight = percentValue(root, "strategyMaxStockWeight");
  spec.portfolio_constraints.max_industry_positions = numberValue(root, "strategyIndustryCount", 1, 100);
  spec.portfolio_constraints.max_industry_weight = percentValue(root, "strategyMaxIndustryWeight");
  spec.portfolio_constraints.max_board_weight = percentValue(root, "strategyMaxBoardWeight");
  spec.portfolio_constraints.min_position_amount_cny = numberValue(root, "strategyMinPositionAmount", 0, 1e9);
  spec.portfolio_constraints.max_notional_share_of_daily_amount = percentValue(root, "strategyCapacityPct", 0.0001, 5);
  spec.portfolio_constraints.custom_weights = spec.portfolio_constraints.weighting_method === "custom"
    ? customWeightsValue(root)
    : {};
  if (spec.portfolio_constraints.weighting_method !== "custom"
      && spec.portfolio_constraints.max_stock_weight * spec.portfolio_constraints.stock_count < .999999) {
    throw new Error("组合股票数与单股权重上限无法形成满仓组合");
  }
  spec.rebalance_policy.hold_sessions = numberValue(root, "strategyHoldSessions", 1, 60);
  spec.rebalance_policy.rebalance_every_sessions = spec.rebalance_policy.hold_sessions;
  spec.rebalance_policy.buy_utility_threshold = numberValue(root, "strategyBuyThreshold", 0, 100);
  spec.rebalance_policy.hold_utility_threshold = numberValue(root, "strategyHoldThreshold", 0, 100);
  if (spec.rebalance_policy.hold_utility_threshold > spec.rebalance_policy.buy_utility_threshold) {
    throw new Error("继续持有效用阈值不能高于新买入阈值");
  }
  spec.hard_filters = amountFilter(spec.hard_filters, numberValue(root, "strategyMinAmount", 0, 1e15));
  return spec;
}

export function syncStrategyEditor(root, spec) {
  root.getElementById("strategyName").value = spec.name || "";
  root.getElementById("strategyProfile").value = spec.profile || "balanced";
  syncObjectiveEditor(root, spec.objectives || PROFILE_OBJECTIVES.balanced);
  syncPortfolioEditor(root, spec.portfolio_constraints || {});
  syncRebalanceEditor(root, spec.rebalance_policy || {});
  root.getElementById("strategyMinQuality").value = spec.exclusions?.min_data_quality_score ?? 70;
  root.getElementById("strategyMinAmount").value = amountThreshold(spec.hard_filters);
  root.querySelectorAll("[data-strategy-board]").forEach((input) => {
    input.checked = Boolean(spec.universe?.boards?.includes(input.value));
  });
}

function syncPortfolioEditor(root, constraints) {
  root.getElementById("strategyStockCount").value = constraints.stock_count ?? 20;
  root.getElementById("strategyWeightingMethod").value = constraints.weighting_method ?? "equal";
  root.getElementById("strategyMaxStockWeight").value = ratioToPercent(constraints.max_stock_weight, 10);
  root.getElementById("strategyIndustryCount").value = constraints.max_industry_positions ?? 3;
  root.getElementById("strategyMaxIndustryWeight").value = ratioToPercent(constraints.max_industry_weight, 30);
  root.getElementById("strategyMaxBoardWeight").value = ratioToPercent(constraints.max_board_weight, 50);
  root.getElementById("strategyMinPositionAmount").value = constraints.min_position_amount_cny ?? 5000;
  root.getElementById("strategyCapacityPct").value = ratioToPercent(constraints.max_notional_share_of_daily_amount, .1);
  root.getElementById("strategyCustomWeights").value = JSON.stringify(constraints.custom_weights || {}, null, 2);
  syncCustomWeightsVisibility(root);
}

function syncObjectiveEditor(root, objectives) {
  const fields = {
    strategyObjectiveAlpha1d: "alpha_1d",
    strategyObjectiveAlpha5d: "alpha_5d",
    strategyObjectiveAlpha20d: "alpha_20d",
    strategyObjectiveConfidence: "confidence",
    strategyObjectiveRisk: "risk",
    strategyObjectiveTradability: "tradability",
  };
  for (const [id, name] of Object.entries(fields)) {
    root.getElementById(id).value = ratioToPercent(objectives[name], 0);
  }
  syncCustomObjectivesVisibility(root);
}

function syncRebalanceEditor(root, policy) {
  root.getElementById("strategyHoldSessions").value = policy.hold_sessions ?? 5;
  root.getElementById("strategyBuyThreshold").value = policy.buy_utility_threshold ?? 0;
  root.getElementById("strategyHoldThreshold").value = policy.hold_utility_threshold ?? 0;
}

function defaultStrategySpec(root) {
  return {
    name: textValue(root, "strategyName"), description: "", schema_version: 1,
    universe: { boards: [...BOARD_ORDER] },
    exclusions: { exclude_st: true, exclude_new: false, min_listing_days: 120, exclude_suspended: true, min_history_sessions: 61, min_data_quality_score: 70, min_amount_cny: 0 },
    hard_filters: [], objectives: PROFILE_OBJECTIVES.balanced, profile: "balanced",
    portfolio_constraints: { stock_count: 20, weighting_method: "equal", max_stock_weight: .1, max_industry_positions: 3, max_industry_weight: .3, max_board_weight: .5, min_position_amount_cny: 5000, max_notional_share_of_daily_amount: .001, custom_weights: {} },
    rebalance_policy: { hold_sessions: 5, cadence: "manual", rebalance_every_sessions: 5, buy_utility_threshold: 0, hold_utility_threshold: 0 },
    execution_policy: { t_plus_one: true, respect_price_limits: true, respect_suspensions: true, cost_profile: "base", commission_rate: .0003, minimum_commission_cny: 5, sell_stamp_duty_rate: .0005, transfer_fee_rate: .00001, buy_slippage_bps: 5, sell_slippage_bps: 5 },
    evidence_policy: { minimum_quality_score: 70, maximum_market_data_age_days: 1, maximum_fundamental_data_age_days: 120, allowed_sources: [], blocked_sources: [], require_verified_point_in_time_evidence: true },
  };
}

function amountFilter(filters, amount) {
  const others = Array.isArray(filters) ? filters.filter((item) => item.field !== "amount") : [];
  return amount > 0 ? [...others, { field: "amount", operator: "gte", value: amount, period_sessions: null }] : others;
}

function amountThreshold(filters) {
  const item = Array.isArray(filters) ? filters.find((filter) => filter.field === "amount") : null;
  return item ? Number(item.value) : 0;
}

function textValue(root, id) {
  const value = String(root.getElementById(id)?.value || "").trim();
  if (!value) throw new Error("策略名称不能为空");
  return value;
}

function numberValue(root, id, minimum, maximum) {
  const value = Number(root.getElementById(id)?.value);
  if (!Number.isFinite(value) || value < minimum || value > maximum) throw new Error(`${id} 数值无效`);
  return value;
}

function percentValue(root, id, minimum = 0.01, maximum = 100) {
  return numberValue(root, id, minimum, maximum) / 100;
}

function ratioToPercent(value, fallback) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric * 100 : fallback;
}

function customWeightsValue(root) {
  let value;
  try {
    value = JSON.parse(String(root.getElementById("strategyCustomWeights").value || "{}"));
  } catch {
    throw new Error("自定义权重必须是合法 JSON 对象");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("自定义权重必须是股票代码到权重的 JSON 对象");
  }
  for (const [symbol, weight] of Object.entries(value)) {
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol) || !Number.isFinite(Number(weight)) || Number(weight) <= 0 || Number(weight) > 1) {
      throw new Error(`自定义权重条目无效：${symbol}`);
    }
    value[symbol] = Number(weight);
  }
  return value;
}

function customObjectivesValue(root) {
  const value = {
    alpha_1d: percentValue(root, "strategyObjectiveAlpha1d", 0, 100),
    alpha_5d: percentValue(root, "strategyObjectiveAlpha5d", 0, 100),
    alpha_20d: percentValue(root, "strategyObjectiveAlpha20d", 0, 100),
    confidence: percentValue(root, "strategyObjectiveConfidence", 0, 100),
    risk: percentValue(root, "strategyObjectiveRisk", 0, 100),
    tradability: percentValue(root, "strategyObjectiveTradability", 0, 100),
  };
  if (Object.values(value).every((weight) => weight === 0)) {
    throw new Error("自定义画像至少需要一个非零目标权重");
  }
  return value;
}

export function syncCustomWeightsVisibility(root) {
  const custom = root.getElementById("strategyWeightingMethod")?.value === "custom";
  root.getElementById("strategyCustomWeightsField").hidden = !custom;
}

export function syncCustomObjectivesVisibility(root) {
  const custom = root.getElementById("strategyProfile")?.value === "custom";
  root.getElementById("strategyCustomObjectivesField").hidden = !custom;
}

function objectValue(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label}响应格式异常`);
  return value;
}

function positiveInteger(value, label) {
  if (!Number.isInteger(value) || value < 1) throw new Error(`${label}无效`);
}

function fingerprint(value, label) {
  if (!/^[0-9a-f]{64}$/.test(String(value || ""))) throw new Error(`${label}无效`);
}
