import {
  legacyProbabilityRunBinding,
  normalizeProbabilityRunBinding,
  probabilityBindingLimitations,
} from "./market-scan-probability-binding.js";
import { normalizedCalibrationIntervals } from "./market-scan-probability-interval.js";
import { normalizeProbabilityRankingEvidence } from "./market-scan-ranking-contracts.js";

export const MARKET_SCAN_PROBABILITY_HORIZONS = Object.freeze([1, 5, 20]);
export const MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON = 5;
export const CALIBRATED_PROBABILITY_STATUS = "calibrated_shadow";
const ALLOWED_STATUSES = new Set([
  CALIBRATED_PROBABILITY_STATUS, "insufficient_data", "insufficient_evidence", "not_generated",
]);
const PENDING_PROBABILITY_STAGES = new Set([
  "source_capture_pending", "source_index_verification_pending", "maintenance_pending",
]);
const unavailableAuthorityStage = (stage) => PENDING_PROBABILITY_STAGES.has(stage) || stage === "maintenance_failed";

export function normalizeMarketScanProbabilityResearch(value, expectedRunId) {
  if (value === null || value === undefined) return emptyProbabilityResearch(expectedRunId);
  const payload = requireObject(value, "扫描榜单响应.probability_research");
  if (payload.run_id !== null && payload.run_id !== undefined) {
    const runId = requirePositiveInteger(payload.run_id, "扫描榜单响应.probability_research.run_id");
    if (runId !== expectedRunId) throw probabilityContractError("probability_research.run_id 与请求批次不匹配");
  }
  const rawHorizons = payload.horizons === undefined
    ? payload
    : requireObject(payload.horizons, "扫描榜单响应.probability_research.horizons");
  const availability = optionalText(payload.availability, "扫描榜单响应.probability_research.availability");
  if (unavailableAuthorityStage(availability)
      && (normalizeStatus(payload.status) !== "not_generated" || payload.filter_qualified === true)) {
    throw probabilityContractError("概率证据校验或维护未完成时，不得保留已生成概率或筛选授权");
  }
  const pipelineStage = optionalText(payload.pipeline_stage, "扫描榜单响应.probability_research.pipeline_stage");
  const topLimitations = stringList(payload.limitations, "扫描榜单响应.probability_research.limitations");
  const runBinding = normalizeProbabilityRunBinding(
    payload.run_binding, expectedRunId, requireObject, probabilityContractError, requirePositiveInteger,
  );
  const historicalContext = normalizeHistoricalProbabilityContext(payload.historical_context);
  const officialExecutionEvidence = normalizeOfficialExecutionEvidence(payload.official_execution_evidence);
  const jointExecutionEvidence = normalizeJointExecutionEvidence(payload.joint_execution_evidence);
  const horizons = Object.fromEntries(MARKET_SCAN_PROBABILITY_HORIZONS.map((horizon) => [
    String(horizon),
    normalizeArtifact(
      primaryTarget(rawHorizons[String(horizon)]),
      horizon,
      runBinding,
      {
        availability,
        pipelineStage,
        limitations: topLimitations,
        officialExecutionEvidence,
        jointExecutionEvidence,
      },
    ),
  ]));
  return {
    ...payload,
    schema_version: String(payload.schema_version || "market-scan-probability-not-generated-v1"),
    run_id: expectedRunId,
    default_horizon: MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON,
    primary_target: "net_excess_positive",
    status: normalizeStatus(payload.status),
    availability,
    pipeline_stage: pipelineStage,
    limitations: topLimitations,
    run_binding: runBinding,
    historical_context: historicalContext,
    official_execution_evidence: officialExecutionEvidence,
    joint_execution_evidence: jointExecutionEvidence,
    horizons,
  };
}

export function normalizeJointExecutionEvidence(value) {
  if (value === null || value === undefined) return emptyJointExecutionEvidence();
  const raw = requireObject(value, "probability_research.joint_execution_evidence");
  if (raw.contract_version !== "market-scan-joint-execution-maintenance-v1") {
    throw probabilityContractError("joint execution evidence contract_version 不受支持");
  }
  const status = String(raw.status || "").trim();
  const statuses = new Set([
    "maintenance_not_run",
    "maintenance_pending",
    "maintenance_failed",
    "official_execution_unavailable",
    "waiting_mature_official_h5",
    "selection_evidence_accumulating",
    "selection_passed_waiting_authorization",
    "authorization_or_deployment_blocked",
    "deployment_ready_waiting_new_official_batch",
    "current_prediction_ready",
    "store_unavailable",
  ]);
  if (!statuses.has(status)) throw probabilityContractError(`joint execution evidence status 无效：${status}`);
  const filterReady = raw.filter_ready === true;
  if (raw.filter_ready !== undefined && typeof raw.filter_ready !== "boolean") {
    throw probabilityContractError("joint execution evidence filter_ready 必须是 boolean");
  }
  if (filterReady !== (status === "current_prediction_ready")) {
    throw probabilityContractError("joint execution evidence filter_ready 与状态不一致");
  }
  if (["maintenance_pending", "maintenance_failed"].includes(status)
      && (["selection_qualified", "authorization_verified", "deployment_verified", "probability_ranking_shadow_qualified", "probability_ranking_control_verified"]
        .some((name) => raw[name] === true)
        || Number(raw.current_prediction_count ?? 0) !== 0 || raw.current_prediction_run_id != null)) {
    throw probabilityContractError("joint execution 维护未完成时不得保留旧授权或当前预测");
  }
  const ranking = normalizeProbabilityRankingEvidence(raw, status);
  return {
    ...raw,
    reported: true,
    status,
    filter_ready: filterReady,
    ...ranking,
    mature_h5_session_count: requireNonNegativeInteger(raw.mature_h5_session_count ?? 0, "joint_execution_evidence.mature_h5_session_count"),
    selection_minimum_session_count: requireNonNegativeInteger(raw.selection_minimum_session_count ?? 0, "joint_execution_evidence.selection_minimum_session_count"),
    blockers: stringList(raw.blockers, "joint_execution_evidence.blockers"),
    failures: stringList(raw.failures, "joint_execution_evidence.failures"),
  };
}

export function normalizeOfficialExecutionEvidence(value) {
  if (value === null || value === undefined) return emptyOfficialExecutionEvidence();
  const raw = requireObject(value, "probability_research.official_execution_evidence");
  if (raw.contract_version !== "official-execution-store-status-v1") {
    throw probabilityContractError("official execution evidence contract_version 不受支持");
  }
  const status = String(raw.status || "").trim();
  if (!["unconfigured_pinned_registry", "verification_failed", "waiting_sessions", "maintenance_pending", "ready", "store_unavailable"].includes(status)) {
    throw probabilityContractError(`official execution evidence status 无效：${status}`);
  }
  if (typeof raw.configured !== "boolean" || typeof raw.formal_evidence_available !== "boolean") {
    throw probabilityContractError("official execution evidence configured/available 必须是 boolean");
  }
  const count = requireNonNegativeInteger(raw.verified_session_count, "official_execution_evidence.verified_session_count");
  if (status === "maintenance_pending" && count !== 0) {
    throw probabilityContractError("官方执行证据维护中不得使用旧校验计数");
  }
  const failures = stringList(raw.failures, "official_execution_evidence.failures");
  if (raw.public_vendor_auto_upgrade_forbidden !== true) {
    throw probabilityContractError("公开供应商数据不得自动升级为官方执行证据");
  }
  if (raw.formal_evidence_available !== (raw.configured && status === "ready" && count > 0)) {
    throw probabilityContractError("official execution evidence availability 与状态不一致");
  }
  return {
    ...raw,
    reported: true,
    status,
    verified_session_count: count,
    failures,
  };
}

export function normalizeHistoricalProbabilityContext(value) {
  if (value === null || value === undefined) return emptyHistoricalProbabilityContext();
  const raw = requireObject(value, "probability_research.historical_context");
  const status = String(raw.status || "not_generated").trim();
  if (!["ready", "not_generated", "unavailable"].includes(status)) {
    throw probabilityContractError(`probability_research.historical_context.status 无效：${status}`);
  }
  if (raw.filter_qualified !== false || raw.selection_qualified !== false || raw.production_ranking_effect !== "none") {
    throw probabilityContractError("历史研究上下文不能获得筛选、选股或生产排名授权");
  }
  const schemaVersion = String(raw.schema_version || "market-scan-probability-historical-context-v1");
  if (schemaVersion !== "market-scan-probability-historical-context-v1") {
    throw probabilityContractError("历史研究上下文 schema_version 不受支持");
  }
  const base = {
    ...raw,
    schema_version: schemaVersion,
    status,
    availability: optionalText(raw.availability, "probability_research.historical_context.availability"),
    generated_at: optionalText(raw.generated_at, "probability_research.historical_context.generated_at"),
    target: String(raw.target || "net_return_positive"),
    limitations: stringList(raw.limitations, "probability_research.historical_context.limitations"),
    filter_qualified: false,
    selection_qualified: false,
    production_ranking_effect: "none",
  };
  if (status !== "ready") return { ...emptyHistoricalProbabilityContext(), ...base, horizons: {} };
  if (base.target !== "net_return_positive") {
    throw probabilityContractError("历史研究上下文仅支持绝对净收益为正目标");
  }
  if (!["historical_shadow_calibrated_reference_only", "historical_replay_no_verified_predictive_skill", "historical_replay_insufficient_evidence"].includes(base.availability)) {
    throw probabilityContractError("历史研究上下文 availability 无效");
  }
  if (!base.generated_at) throw probabilityContractError("历史研究上下文 generated_at 缺失");
  const cohort = requireObject(raw.cohort, "probability_research.historical_context.cohort");
  if (cohort.mode !== "historical_replay_v1" || cohort.official !== false || cohort.live_cohort_compatible !== false) {
    throw probabilityContractError("历史研究上下文 cohort 边界无效");
  }
  const sample = normalizeHistoricalSample(raw.sample);
  const sourceArtifact = requireObject(raw.source_artifact, "probability_research.historical_context.source_artifact");
  if (sourceArtifact.full_replay_verified !== true) {
    throw probabilityContractError("历史研究上下文未绑定已验证完整重放");
  }
  const rawHorizons = requireObject(raw.horizons, "probability_research.historical_context.horizons");
  const horizons = Object.fromEntries(MARKET_SCAN_PROBABILITY_HORIZONS.map((horizon) => [
    String(horizon),
    normalizeHistoricalHorizon(rawHorizons[String(horizon)], horizon),
  ]));
  return { ...base, cohort, sample, source_artifact: sourceArtifact, horizons };
}

export function isMarketScanProbabilitySourceCapturePending(research) {
  const payload = objectValue(research);
  return payload.status === "not_generated"
    && PENDING_PROBABILITY_STAGES.has(payload.availability)
    && payload.pipeline_stage === payload.availability;
}

export function normalizeMarketScanUpsideProbabilities(value, research) {
  const payload = value === null || value === undefined
    ? {}
    : requireObject(value, "扫描榜单响应.items[].upside_probabilities");
  return Object.fromEntries(MARKET_SCAN_PROBABILITY_HORIZONS.map((horizon) => {
    const artifact = probabilityArtifact(research, horizon);
    const raw = primaryTarget(payload[String(horizon)]);
    return [String(horizon), normalizePrediction(raw, artifact, horizon)];
  }));
}

export function emptyProbabilityResearch(runId) {
  const runBinding = legacyProbabilityRunBinding(runId);
  return {
    schema_version: "market-scan-probability-not-generated-v1",
    run_id: runId,
    default_horizon: MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON,
    primary_target: "net_excess_positive",
    status: "not_generated",
    availability: null,
    pipeline_stage: null,
    limitations: [],
    run_binding: runBinding,
    historical_context: emptyHistoricalProbabilityContext(),
    official_execution_evidence: emptyOfficialExecutionEvidence(),
    joint_execution_evidence: emptyJointExecutionEvidence(),
    horizons: Object.fromEntries(MARKET_SCAN_PROBABILITY_HORIZONS.map((horizon) => [String(horizon), { ...emptyArtifact(horizon), run_binding: runBinding }])),
  };
}

export function emptyJointExecutionEvidence() {
  return {
    contract_version: "market-scan-joint-execution-maintenance-v1",
    reported: false,
    status: "store_unavailable",
    mature_h5_session_count: 0,
    selection_minimum_session_count: 292,
    selection_qualified: false,
    authorization_configured: false,
    authorization_verified: false,
    deployment_verified: false,
    current_prediction_run_id: null,
    current_prediction_count: 0,
    filter_ready: false,
    probability_ranking_status: "shadow_not_available",
    probability_ranking_shadow_digest: null,
    probability_ranking_shadow_qualified: false,
    probability_ranking_control_configured: false,
    probability_ranking_control_verified: false,
    probability_ranking_run_id: null,
    probability_ranking_count: 0,
    probability_ranking_rule_version: "full-market-score-v6",
    production_ranking_effect: "none_without_v6_manual_promotion",
    blockers: ["joint_execution_store_unavailable"],
    failures: [],
  };
}

export function emptyOfficialExecutionEvidence() {
  return {
    contract_version: "official-execution-store-status-v1",
    reported: false,
    configured: false,
    status: "store_unavailable",
    registry_digest: null,
    verified_session_count: 0,
    first_session_date: null,
    latest_session_date: null,
    failures: ["official_execution_store_unavailable"],
    formal_evidence_available: false,
    public_vendor_auto_upgrade_forbidden: true,
  };
}

export function emptyHistoricalProbabilityContext() {
  return {
    schema_version: "market-scan-probability-historical-context-v1",
    status: "not_generated",
    availability: "historical_context_not_generated",
    generated_at: null,
    target: "net_return_positive",
    cohort: null,
    sample: null,
    horizons: {},
    source_artifact: null,
    production_ranking_effect: "none",
    selection_qualified: false,
    filter_qualified: false,
    limitations: [],
  };
}

export function probabilityArtifact(research, horizon) {
  const artifact = objectValue(objectValue(research?.horizons)[String(horizon)]);
  return Object.keys(artifact).length ? artifact : emptyArtifact(horizon);
}

export function normalizedIntervals(record, required) {
  return normalizedCalibrationIntervals(
    record.calibration_bias_interval,
    record.calibration_adjusted_probability_interval,
    required,
    requireObject,
    optionalProbability,
    optionalFinite,
    probabilityContractError,
  );
}

export function finiteProbability(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 && number <= 1 ? number : null;
}

function normalizeArtifact(
  value,
  horizon,
  runBinding = legacyProbabilityRunBinding(null),
  inherited = {},
) {
  if (value === null || value === undefined) {
    const empty = emptyArtifact(horizon);
    return {
      ...empty,
      run_binding: runBinding,
      availability: inherited.availability ?? null,
      pipeline_stage: inherited.pipelineStage ?? null,
      limitations: inherited.limitations?.length
        ? [...inherited.limitations]
        : empty.limitations,
      official_execution_evidence: inherited.officialExecutionEvidence
        ?? emptyOfficialExecutionEvidence(),
      joint_execution_evidence: inherited.jointExecutionEvidence
        ?? emptyJointExecutionEvidence(),
    };
  }
  const raw = requireObject(value, `probability_research.horizons.${horizon}`);
  const status = normalizeStatus(raw.status);
  if ((unavailableAuthorityStage(inherited.availability)
      || ["maintenance_pending", "maintenance_failed"].includes(inherited.jointExecutionEvidence?.status)) && status !== "not_generated") {
    throw probabilityContractError("概率证据校验或维护未完成时，不得保留旧周期概率");
  }
  if (status !== CALIBRATED_PROBABILITY_STATUS && raw.probability !== null && raw.probability !== undefined) {
    throw probabilityContractError(`probability_research.horizons.${horizon}.probability 证据不足时必须为空`);
  }
  if (status !== CALIBRATED_PROBABILITY_STATUS && raw.filter_qualified === true) {
    throw probabilityContractError(`probability_research.horizons.${horizon}.filter_qualified 证据不足时必须为 false`);
  }
  const selection = normalizeSelectionQualification(raw, horizon, status);
  if (raw.filter_qualified !== null && raw.filter_qualified !== undefined && typeof raw.filter_qualified !== "boolean") {
    throw probabilityContractError(`probability_research.horizons.${horizon}.filter_qualified 必须是 boolean`);
  }
  const versions = objectValue(raw.versions);
  if (raw.horizon !== null && raw.horizon !== undefined && Number(raw.horizon) !== horizon) {
    throw probabilityContractError(`probability_research.horizons.${horizon}.horizon 不匹配`);
  }
  const localLimitations = stringList(raw.limitations, `probability_research.horizons.${horizon}.limitations`);
  const inheritedLimitations = Array.isArray(inherited.limitations) ? inherited.limitations : [];
  return {
    ...raw,
    ...selection,
    filter_qualified: raw.filter_qualified === true,
    status,
    horizon,
    target_definition: String(raw.target || raw.target_definition || "net_excess_positive"),
    base_rate: optionalProbability(raw.base_rate, `probability_research.horizons.${horizon}.base_rate`),
    model_version: raw.model_version || versions.model || null,
    feature_version: raw.feature_version || versions.feature || null,
    label_version: raw.label_version || versions.label || null,
    cost_model_version: raw.cost_model_version || versions.cost_model || null,
    availability: optionalText(raw.availability, `probability_research.horizons.${horizon}.availability`)
      ?? inherited.availability ?? null,
    pipeline_stage: optionalText(raw.pipeline_stage, `probability_research.horizons.${horizon}.pipeline_stage`)
      ?? inherited.pipelineStage ?? null,
    run_binding: runBinding,
    official_execution_evidence: inherited.officialExecutionEvidence
      ?? emptyOfficialExecutionEvidence(),
    joint_execution_evidence: inherited.jointExecutionEvidence
      ?? emptyJointExecutionEvidence(),
    limitations: probabilityBindingLimitations(
      [...new Set([...inheritedLimitations, ...localLimitations])],
      runBinding,
    ),
  };
}

function normalizeSelectionQualification(raw, horizon, status) {
  const qualified = raw.selection_qualified;
  if (qualified !== null && qualified !== undefined && typeof qualified !== "boolean") {
    throw probabilityContractError(`probability_research.horizons.${horizon}.selection_qualified 必须是 boolean`);
  }
  const qualification = raw.selection_qualification === null || raw.selection_qualification === undefined
    ? null
    : requireObject(raw.selection_qualification, `probability_research.horizons.${horizon}.selection_qualification`);
  if (qualification && typeof qualification.passed !== "boolean") {
    throw probabilityContractError(`probability_research.horizons.${horizon}.selection_qualification.passed 必须是 boolean`);
  }
  if (qualified === true && (status !== CALIBRATED_PROBABILITY_STATUS || qualification?.passed !== true)) {
    throw probabilityContractError(`probability_research.horizons.${horizon} 的选股效力资格与证据状态不一致`);
  }
  return { selection_qualified: qualified === true, selection_qualification: qualification };
}

function normalizePrediction(value, artifact, horizon) {
  if (value === null || value === undefined) return emptyPrediction(artifact, horizon);
  const raw = requireObject(value, `upside_probabilities.${horizon}`);
  const status = normalizeStatus(raw.status || artifact.status);
  const probability = optionalProbability(raw.probability, `upside_probabilities.${horizon}.probability`);
  if (status === CALIBRATED_PROBABILITY_STATUS && artifact.status !== CALIBRATED_PROBABILITY_STATUS) {
    throw probabilityContractError(`upside_probabilities.${horizon} 不能超越批次研究证据状态`);
  }
  if (status === CALIBRATED_PROBABILITY_STATUS && probability === null) {
    throw probabilityContractError(`upside_probabilities.${horizon}.probability 校准后不能为空`);
  }
  if (status !== CALIBRATED_PROBABILITY_STATUS && probability !== null) {
    throw probabilityContractError(`upside_probabilities.${horizon}.probability 证据不足时必须为空`);
  }
  const intervals = normalizedIntervals(raw, status === CALIBRATED_PROBABILITY_STATUS);
  return {
    ...artifact,
    ...raw,
    status,
    horizon,
    probability,
    calibration_bias_interval: intervals.bias,
    calibration_adjusted_probability_interval: intervals.adjusted,
    base_rate: optionalProbability(raw.base_rate ?? artifact.base_rate, `upside_probabilities.${horizon}.base_rate`),
    limitations: stringList(raw.limitations ?? artifact.limitations, `upside_probabilities.${horizon}.limitations`),
  };
}

function primaryTarget(value) {
  if (value === null || value === undefined) return value;
  const source = objectValue(value);
  return source.net_excess_positive ?? source;
}

function emptyArtifact(horizon) {
  return {
    status: "not_generated",
    probability: null,
    horizon,
    target_definition: "net_excess_positive",
    base_rate: null,
    filter_qualified: false,
    limitations: ["旧批次或当前批次未持久化上涨概率证据"],
  };
}

function emptyPrediction(artifact, horizon) {
  return {
    ...artifact,
    status: "not_generated",
    horizon,
    probability: null,
    calibration_bias_interval: null,
    calibration_adjusted_probability_interval: null,
  };
}

function normalizeHistoricalSample(value) {
  const raw = requireObject(value, "probability_research.historical_context.sample");
  return {
    ...raw,
    start_date: requireText(raw.start_date, "historical_context.sample.start_date"),
    end_date: requireText(raw.end_date, "historical_context.sample.end_date"),
    independent_session_count: requireNonNegativeInteger(raw.independent_session_count, "historical_context.sample.independent_session_count"),
    record_count: requireNonNegativeInteger(raw.record_count, "historical_context.sample.record_count"),
    symbol_count: requirePositiveInteger(raw.symbol_count, "historical_context.sample.symbol_count"),
    label_coverage: requireProbability(raw.label_coverage, "historical_context.sample.label_coverage"),
  };
}

function normalizeHistoricalHorizon(value, horizon) {
  const raw = requireObject(value, `probability_research.historical_context.horizons.${horizon}`);
  if (Number(raw.horizon) !== horizon || raw.probability !== null) {
    throw probabilityContractError(`历史研究 H${horizon} 不能发布逐股概率或错配周期`);
  }
  const assessmentStatus = String(raw.assessment_status || "").trim();
  if (!["insufficient_data", CALIBRATED_PROBABILITY_STATUS].includes(assessmentStatus)) {
    throw probabilityContractError(`历史研究 H${horizon} 评估状态无效`);
  }
  return {
    ...raw,
    horizon,
    assessment_status: assessmentStatus,
    probability: null,
    base_rate: optionalProbability(raw.base_rate, `historical_context.horizons.${horizon}.base_rate`),
    available_independent_session_count: requireNonNegativeInteger(raw.available_independent_session_count, `historical_context.horizons.${horizon}.available_independent_session_count`),
    minimum_required_independent_session_count: requirePositiveInteger(raw.minimum_required_independent_session_count, `historical_context.horizons.${horizon}.minimum_required_independent_session_count`),
    out_of_sample_session_count: requireNonNegativeInteger(raw.out_of_sample_session_count, `historical_context.horizons.${horizon}.out_of_sample_session_count`),
    evaluated_fold_count: requireNonNegativeInteger(raw.evaluated_fold_count, `historical_context.horizons.${horizon}.evaluated_fold_count`),
    observation_count: requireNonNegativeInteger(raw.observation_count, `historical_context.horizons.${horizon}.observation_count`),
    auc: optionalProbability(raw.auc, `historical_context.horizons.${horizon}.auc`),
    brier_score: optionalFinite(raw.brier_score, `historical_context.horizons.${horizon}.brier_score`),
    brier_skill_score: optionalFinite(raw.brier_skill_score, `historical_context.horizons.${horizon}.brier_skill_score`),
    ece: optionalProbability(raw.ece, `historical_context.horizons.${horizon}.ece`),
    bin_monotonic: optionalBoolean(raw.bin_monotonic, `historical_context.horizons.${horizon}.bin_monotonic`),
    highest_bin_above_base_rate: optionalBoolean(raw.highest_bin_above_base_rate, `historical_context.horizons.${horizon}.highest_bin_above_base_rate`),
    training_cutoff: optionalText(raw.training_cutoff, `historical_context.horizons.${horizon}.training_cutoff`),
    limitations: stringList(raw.limitations, `historical_context.horizons.${horizon}.limitations`),
  };
}

function normalizeStatus(value) {
  const status = String(value || "not_generated").trim();
  if (!ALLOWED_STATUSES.has(status)) throw probabilityContractError(`未知上涨概率状态：${status}`);
  return status;
}

function optionalProbability(value, path) {
  if (value === null || value === undefined) return null;
  const number = finiteProbability(value);
  if (number === null) throw probabilityContractError(`${path} 必须是 0–1 的有限数值`);
  return number;
}

function optionalFinite(value, path) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) throw probabilityContractError(`${path} 必须是有限数值`);
  return number;
}

function stringList(value, path) {
  if (value === null || value === undefined) return [];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw probabilityContractError(`${path} 必须是字符串数组`);
  }
  return value;
}

function optionalText(value, path) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !value.trim()) {
    throw probabilityContractError(`${path} 必须是非空字符串或 null`);
  }
  return value.trim();
}

function requireText(value, path) {
  const text = optionalText(value, path);
  if (text === null) throw probabilityContractError(`${path} 必须是非空字符串`);
  return text;
}

function requireProbability(value, path) {
  const number = optionalProbability(value, path);
  if (number === null) throw probabilityContractError(`${path} 必须是 0–1 的有限数值`);
  return number;
}

function optionalBoolean(value, path) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "boolean") throw probabilityContractError(`${path} 必须是 boolean 或 null`);
  return value;
}

function requirePositiveInteger(value, path) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1) throw probabilityContractError(`${path} 必须是正整数`);
  return number;
}

function requireNonNegativeInteger(value, path) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 0) throw probabilityContractError(`${path} 必须是非负整数`);
  return number;
}

function requireObject(value, path) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw probabilityContractError(`${path} 必须是对象`);
  return value;
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function probabilityContractError(message) {
  const error = new Error(`扫描接口响应格式异常：${message}`);
  error.name = "MarketScanContractError";
  return error;
}
