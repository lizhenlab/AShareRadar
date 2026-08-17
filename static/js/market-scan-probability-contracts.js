import {
  legacyProbabilityRunBinding,
  normalizeProbabilityRunBinding,
  probabilityBindingLimitations,
} from "./market-scan-probability-binding.js";
import { normalizedCalibrationIntervals } from "./market-scan-probability-interval.js";

export const MARKET_SCAN_PROBABILITY_HORIZONS = Object.freeze([1, 5, 20]);
export const MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON = 5;
export const CALIBRATED_PROBABILITY_STATUS = "calibrated_shadow";
const ALLOWED_STATUSES = new Set([
  CALIBRATED_PROBABILITY_STATUS,
  "insufficient_data",
  "insufficient_evidence",
  "not_generated",
]);

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
  const pipelineStage = optionalText(payload.pipeline_stage, "扫描榜单响应.probability_research.pipeline_stage");
  const topLimitations = stringList(payload.limitations, "扫描榜单响应.probability_research.limitations");
  const runBinding = normalizeProbabilityRunBinding(
    payload.run_binding, expectedRunId, requireObject, probabilityContractError, requirePositiveInteger,
  );
  const horizons = Object.fromEntries(MARKET_SCAN_PROBABILITY_HORIZONS.map((horizon) => [
    String(horizon),
    normalizeArtifact(
      primaryTarget(rawHorizons[String(horizon)]),
      horizon,
      runBinding,
      { availability, pipelineStage, limitations: topLimitations },
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
    horizons,
  };
}

export function isMarketScanProbabilitySourceCapturePending(research) {
  const payload = objectValue(research);
  return payload.status === "not_generated"
    && payload.availability === "source_capture_pending"
    && payload.pipeline_stage === "source_capture_pending";
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
    horizons: Object.fromEntries(MARKET_SCAN_PROBABILITY_HORIZONS.map((horizon) => [String(horizon), { ...emptyArtifact(horizon), run_binding: runBinding }])),
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
    };
  }
  const raw = requireObject(value, `probability_research.horizons.${horizon}`);
  const status = normalizeStatus(raw.status);
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

function requirePositiveInteger(value, path) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1) throw probabilityContractError(`${path} 必须是正整数`);
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
