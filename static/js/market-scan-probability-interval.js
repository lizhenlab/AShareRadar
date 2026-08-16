const BIAS_METHOD = "date_block_bootstrap_signed_calibration_bias";
const BIAS_SEMANTICS = "signed_observed_rate_minus_probability_bias";
const ADJUSTED_METHOD = "date_block_bootstrap_calibration_offset";
const ADJUSTED_SEMANTICS = "calibration_adjusted_probability_interval_not_individual_outcome_interval";

export function normalizedCalibrationIntervals(
  biasValue,
  adjustedValue,
  required,
  requireObject,
  optionalProbability,
  optionalFinite,
  contractError,
) {
  if ((biasValue === null || biasValue === undefined) && (adjustedValue === null || adjustedValue === undefined)) {
    if (required) throw contractError("calibrated_shadow 概率缺少校准偏差与调整区间");
    return { bias: null, adjusted: null };
  }
  if (biasValue === null || biasValue === undefined || adjustedValue === null || adjustedValue === undefined) {
    throw contractError("calibration_bias_interval 与 calibration_adjusted_probability_interval 必须同时存在");
  }
  return {
    bias: normalizedBias(biasValue, requireObject, optionalProbability, optionalFinite, contractError),
    adjusted: normalizedAdjusted(adjustedValue, requireObject, optionalProbability, contractError),
  };
}

function normalizedBias(value, requireObject, optionalProbability, optionalFinite, contractError) {
  const raw = requireObject(value, "calibration_bias_interval");
  const lower = optionalFinite(raw.lower, "calibration_bias_interval.lower");
  const upper = optionalFinite(raw.upper, "calibration_bias_interval.upper");
  const level = optionalProbability(raw.level, "calibration_bias_interval.level");
  if (lower === null || upper === null || lower < -1 || upper > 1 || lower > upper || level !== 0.95
      || raw.method !== BIAS_METHOD || raw.semantics !== BIAS_SEMANTICS) {
    throw contractError("calibration_bias_interval 必须是有符号 [-1,1] 偏差区间，且不要求覆盖 0");
  }
  return { ...raw, level, lower, upper };
}

function normalizedAdjusted(value, requireObject, optionalProbability, contractError) {
  const raw = requireObject(value, "calibration_adjusted_probability_interval");
  const lower = optionalProbability(raw.lower, "calibration_adjusted_probability_interval.lower");
  const upper = optionalProbability(raw.upper, "calibration_adjusted_probability_interval.upper");
  const level = optionalProbability(raw.level, "calibration_adjusted_probability_interval.level");
  if (lower === null || upper === null || lower > upper || level !== 0.95
      || raw.method !== ADJUSTED_METHOD || raw.semantics !== ADJUSTED_SEMANTICS) {
    throw contractError("calibration_adjusted_probability_interval 必须是 0–1 群体校准调整区间");
  }
  return { ...raw, level, lower, upper };
}
