const SCORE_RULE_VERSION = "full-market-score-v6";
const PROJECTION_VERSION = "market-scan-probability-ranking-projection-v1";

export function normalizeProbabilityRankingEvidence(raw, jointStatus) {
  const rankingStatus = String(raw.probability_ranking_status || "shadow_not_available").trim();
  const statuses = new Set([
    "shadow_not_available", "shadow_evaluation_unavailable", "shadow_gates_failed",
    "waiting_explicit_human_promotion", "promotion_verified_waiting_new_official_batch",
    "rollback_active", "shadow_or_control_verification_failed",
    "production_ranking_ready", "production_ranking_publication_failed",
  ]);
  if (!statuses.has(rankingStatus)) fail(`joint execution probability_ranking_status 无效：${rankingStatus}`);
  const rankingCount = integer(raw.probability_ranking_count ?? 0, "joint_execution_evidence.probability_ranking_count", 0);
  const rankingRunId = raw.probability_ranking_run_id === null || raw.probability_ranking_run_id === undefined
    ? null : integer(raw.probability_ranking_run_id, "joint_execution_evidence.probability_ranking_run_id", 1);
  const rankingReady = rankingStatus === "production_ranking_ready";
  if (rankingReady !== (rankingCount > 0 && rankingRunId !== null)) {
    fail("joint execution v6 排名状态与发布计数不一致");
  }
  const rankingEffect = String(raw.production_ranking_effect || "none_without_v6_manual_promotion");
  const expectedEffect = rankingReady ? "v6_active_for_exact_published_run"
    : rankingStatus === "rollback_active" ? "rollback_active_v5_only" : "none_without_v6_manual_promotion";
  if (rankingEffect !== expectedEffect) fail("joint execution 生产排名作用与 v6 状态不一致");
  if (rankingReady && (jointStatus !== "current_prediction_ready"
      || raw.probability_ranking_shadow_qualified !== true
      || raw.probability_ranking_control_verified !== true
      || rankingRunId !== raw.current_prediction_run_id)) {
    fail("joint execution v6 生产排名缺少筛选、Shadow 或人工晋级绑定");
  }
  return {
    probability_ranking_status: rankingStatus,
    probability_ranking_run_id: rankingRunId,
    probability_ranking_count: rankingCount,
    production_ranking_effect: rankingEffect,
  };
}

export function normalizeProductionRanking(value, run) {
  const inactive = {
    contract_version: PROJECTION_VERSION,
    status: "inactive",
    run_id: run.id,
    base_v5_mutated: false,
    historical_ranks_mutated: false,
  };
  if (value === null || value === undefined) return inactive;
  const raw = objectValue(value, "扫描榜单响应.production_ranking");
  if (raw.contract_version !== PROJECTION_VERSION) fail("production_ranking.contract_version 不受支持");
  if (!new Set(["inactive", "active"]).has(raw.status)) fail("production_ranking.status 不受支持");
  if (integer(raw.run_id, "production_ranking.run_id", 1) !== run.id) {
    fail("production_ranking.run_id 与请求批次不一致");
  }
  if (raw.base_v5_mutated !== false || raw.historical_ranks_mutated !== false) {
    fail("production_ranking 不得改写 v5 或历史排名");
  }
  if (raw.status === "inactive") return { ...inactive, ...raw };
  if (raw.score_rule_version !== SCORE_RULE_VERSION) {
    fail("production_ranking.score_rule_version 必须是 full-market-score-v6");
  }
  for (const field of ["score_spec_hash", "artifact_digest", "promotion_digest", "base_snapshot_digest"]) {
    digest(raw[field], `production_ranking.${field}`);
  }
  if (raw.base_snapshot_digest !== run.snapshot_digest) {
    fail("production_ranking.base_snapshot_digest 与榜单快照不一致");
  }
  if (integer(raw.record_count, "production_ranking.record_count", 1) !== run.success_count) {
    fail("production_ranking.record_count 与 success_count 不一致");
  }
  timestamp(raw.generated_at, "production_ranking.generated_at");
  if (raw.rollback_available !== true) fail("production_ranking 必须保留显式回滚能力");
  return raw;
}

export function validateProbabilityRankingItem(item, ranking, context) {
  const details = item.probability_ranking_details === undefined
    ? {}
    : objectValue(item.probability_ranking_details, `${context}.probability_ranking_details`);
  const scalarFields = [
    "base_production_rank", "base_production_score", "base_production_raw_score",
    "production_score_rule_version", "probability_ranking_adjustment",
    "probability_ranking_artifact_digest",
  ];
  if (ranking.status !== "active" || item.status !== "success") {
    if (scalarFields.some((field) => item[field] !== null && item[field] !== undefined)
        || Object.keys(details).length) {
      fail(`${context} 在 v6 未启用或非 success 时不得包含概率生产排名`);
    }
    return;
  }
  integer(item.base_production_rank, `${context}.base_production_rank`, 1);
  integer(item.base_production_score, `${context}.base_production_score`, 0, 100);
  number(item.base_production_raw_score, `${context}.base_production_raw_score`, 0, 100);
  number(item.probability_ranking_adjustment, `${context}.probability_ranking_adjustment`, -6, 6);
  if (item.production_score_rule_version !== SCORE_RULE_VERSION) {
    fail(`${context}.production_score_rule_version 必须是 full-market-score-v6`);
  }
  digest(item.probability_ranking_artifact_digest, `${context}.probability_ranking_artifact_digest`);
  if (item.probability_ranking_artifact_digest !== ranking.artifact_digest) {
    fail(`${context}.probability_ranking_artifact_digest 与页面排名产物不一致`);
  }
  const exact = {
    run_id: item.run_id,
    symbol: item.symbol,
    base_rank: item.base_production_rank,
    base_score: item.base_production_score,
    rank: item.rank,
    score: item.score,
    score_rule_version: item.production_score_rule_version,
    score_spec_hash: ranking.score_spec_hash,
  };
  if (Object.entries(exact).some(([field, expected]) => details[field] !== expected)) {
    fail(`${context}.probability_ranking_details 与公开排名字段不一致`);
  }
  for (const [field, expected] of [
    ["base_raw_score", item.base_production_raw_score],
    ["raw_score", item.raw_score],
    ["probability_adjustment", item.probability_ranking_adjustment],
  ]) {
    number(details[field], `${context}.probability_ranking_details.${field}`);
    if (Math.abs(details[field] - expected) > 1e-9) {
      fail(`${context}.probability_ranking_details.${field} 与公开字段不一致`);
    }
  }
  number(details.probability, `${context}.probability_ranking_details.probability`, 0, 1);
  number(details.reference_base_rate, `${context}.probability_ranking_details.reference_base_rate`, 0, 1);
  for (const field of ["source_record_digest", "prediction_record_digest", "record_digest"]) {
    digest(details[field], `${context}.probability_ranking_details.${field}`);
  }
}

function objectValue(value, path) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`${path} 必须是对象`);
  return value;
}

function integer(value, path, minimum, maximum) {
  if (!Number.isInteger(value)) fail(`${path} 必须是整数`);
  if (minimum !== undefined && value < minimum) fail(`${path} 不能小于 ${minimum}`);
  if (maximum !== undefined && value > maximum) fail(`${path} 不能大于 ${maximum}`);
  return value;
}

function number(value, path, minimum, maximum) {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(`${path} 必须是有限数值`);
  if (minimum !== undefined && value < minimum) fail(`${path} 不能小于 ${minimum}`);
  if (maximum !== undefined && value > maximum) fail(`${path} 不能大于 ${maximum}`);
  return value;
}

function digest(value, path) {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) fail(`${path} 必须是小写 SHA-256`);
  return value;
}

function timestamp(value, path) {
  if (typeof value !== "string"
      || !/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/.test(value)
      || !Number.isFinite(Date.parse(value.includes(" ") ? value.replace(" ", "T") : value))) {
    fail(`${path} 必须是有效 ISO 时间`);
  }
  return value;
}

function fail(message) {
  const error = new Error(`扫描接口响应格式异常：${message}`);
  error.name = "MarketScanContractError";
  throw error;
}
