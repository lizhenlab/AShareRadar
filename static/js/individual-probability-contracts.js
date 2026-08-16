const HORIZON_DAYS = Object.freeze([2, 3, 4]);
const REQUIRED_OFFICIAL_PIT_SESSIONS = 288;
const MINIMUM_SELECTION_SESSIONS = Object.freeze({ 2: 284, 3: 286, 4: 288 });
const MINIMUM_TEST_SESSIONS_PER_FOLD = 60;
const MINIMUM_CALIBRATION_BIN_SESSIONS = 20;
const REGISTERED_MODEL_VERSION = "shadow-up-probability-logit-l2-v2-convergence-required";
const REGISTERED_FEATURE_VERSION = "historical-replay-common-ohlcv-v1";
const REGISTERED_SIGNAL_YEAR = 2026;
const REGISTERED_WEEKDAY_CLOSURES = new Set([
  "2026-01-01", "2026-01-02", "2026-02-16", "2026-02-17", "2026-02-18",
  "2026-02-19", "2026-02-20", "2026-02-23", "2026-04-06", "2026-05-01",
  "2026-05-04", "2026-05-05", "2026-06-19", "2026-09-25", "2026-10-01",
  "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
]);
const REPORT_FIELDS = new Set(["schema_version", "symbol", "signal_date", "generated_at", "status", "target_contract", "horizons", "evidence", "limitations", "production_effect"]);
const TARGET_FIELDS = new Set(["version", "signal_cutoff", "entry", "exits", "target", "cost_profile", "execution_notional", "feature_version", "point_in_time_required"]);
const HORIZON_FIELDS = new Set(["display_day", "holding_sessions", "status", "probability", "confidence_interval", "base_rate", "counts", "calibration_metrics", "training_cutoff", "model_version", "feature_version", "evidence_digest", "gate_reasons"]);
const COUNT_FIELDS = new Set(["observation_count", "eligible_observation_count", "independent_session_count", "out_of_sample_observation_count", "out_of_sample_session_count", "evaluated_fold_count"]);
const METRIC_FIELDS = new Set([
  "brier_score", "reference_brier_score", "brier_skill_score", "ece", "auc",
  "actual_positive_rate", "actual_positive_rate_ci_95", "bin_monotonic",
  "highest_bin_above_base_rate", "selection_gate_version", "calibration_bin_count",
  "minimum_calibration_bin_session_count", "all_folds_positive_brier_skill",
]);
const EVIDENCE_FIELDS = new Set(["assessment_digest", "history_manifest_digest", "history_database_sha256", "official_pit_session_count", "required_official_pit_session_count", "historical_replay_session_count", "historical_replay_official", "selection_qualified"]);
const INTERVAL_FIELDS = new Set(["lower", "upper", "level"]);
const RESPONSE_STATUSES = new Set([
  "calibrated_shadow",
  "insufficient_data",
  "not_generated",
]);

export function validateIndividualProbabilityReport(value, expectedSymbol = "") {
  const report = objectValue(value, "个股上涨概率响应");
  assertExactFields(report, REPORT_FIELDS, "个股上涨概率响应");
  if (report.schema_version !== "individual-upside-probability-v1") {
    throw contractError("个股上涨概率响应.schema_version 不受支持");
  }
  const symbol = canonicalAshareSymbol(report.symbol, "symbol");
  if (expectedSymbol && !sameSymbol(symbol, expectedSymbol)) {
    throw contractError("个股上涨概率响应与当前股票不一致");
  }
  if (!RESPONSE_STATUSES.has(report.status)) {
    throw contractError("个股上涨概率响应.status 不受支持");
  }
  const signalDate = nullableIsoDate(report.signal_date, "signal_date");
  const generatedAt = requiredTimestamp(report.generated_at, "generated_at");
  const targetContract = objectValue(report.target_contract, "target_contract");
  validateTargetContract(targetContract);
  const horizons = arrayValue(report.horizons, "horizons");
  if (horizons.length !== HORIZON_DAYS.length) {
    throw contractError("个股上涨概率响应必须包含 D+2、D+3、D+4 三个周期");
  }
  const evidence = objectValue(report.evidence, "evidence");
  validateEvidence(evidence);
  if ((evidence.official_pit_session_count === 0) !== (signalDate === null)) {
    throw contractError("signal_date 必须且只能来自正式 PIT 证据");
  }
  if (signalDate !== null && generatedAt < Date.parse(`${signalDate}T15:15:00+08:00`)) {
    throw contractError("正式 PIT 证据必须在信号日 15:15 后成熟");
  }
  const normalizedHorizons = horizons
    .map((horizon, index) => validateHorizon(horizon, index, report.status, evidence.selection_qualified))
    .sort((left, right) => left.display_day - right.display_day);
  if (normalizedHorizons.some((item, index) => item.display_day !== HORIZON_DAYS[index])) {
    throw contractError("个股上涨概率周期必须且只能为 D+2、D+3、D+4");
  }
  const calibratedCount = normalizedHorizons.filter((item) => item.status === "calibrated_shadow").length;
  if (signalDate !== null && !isRegisteredExchangeSession(signalDate)) {
    throw contractError("signal_date 必须是已注册的可信交易所交易日");
  }
  for (const horizon of normalizedHorizons.filter((item) => item.status === "calibrated_shadow")) {
    if (signalDate === null || horizon.training_cutoff >= signalDate
        || !isRegisteredExchangeSession(horizon.training_cutoff)) {
      throw contractError(`D+${horizon.display_day}.training_cutoff 必须是早于 signal_date 的可信交易日`);
    }
  }
  if (report.status === "calibrated_shadow") {
    if (!evidence.selection_qualified || calibratedCount < 1) {
      throw contractError("calibrated_shadow 报告必须通过 selection 门禁且至少一个周期可用");
    }
    if (evidence.official_pit_session_count < evidence.required_official_pit_session_count) {
      throw contractError("calibrated_shadow 的正式 PIT 日数未达到注册门槛");
    }
    if (evidence.historical_replay_official !== true) {
      throw contractError("calibrated_shadow 必须绑定正式历史回放证据");
    }
    if (evidence.historical_replay_session_count < REQUIRED_OFFICIAL_PIT_SESSIONS) {
      throw contractError("calibrated_shadow 的正式历史回放日数未达到注册门槛");
    }
    for (const field of ["assessment_digest", "history_manifest_digest", "history_database_sha256"]) {
      if (!isSha256(evidence[field])) throw contractError(`calibrated_shadow 的 ${field} 无效`);
    }
  } else if (evidence.selection_qualified || calibratedCount > 0) {
    throw contractError("未校准报告不得声明 selection 门禁通过或携带校准周期");
  }
  const limitations = stringArray(report.limitations, "limitations");
  validateProductionEffect(report.production_effect);
  return {
    ...report,
    symbol,
    signal_date: signalDate,
    target_contract: targetContract,
    horizons: normalizedHorizons,
    evidence,
    limitations,
  };
}

export function evidenceStatusLabel(status) {
  return {
    calibrated_shadow: "样本外已校准",
    insufficient_data: "样本不足",
    not_generated: "尚未生成",
    unavailable: "暂不可用",
  }[status] || "暂不可用";
}

export function individualProbabilityContractError(message) {
  return contractError(message);
}

function validateHorizon(value, index, reportStatus, selectionQualified) {
  const horizon = objectValue(value, `horizons[${index}]`);
  assertExactFields(horizon, HORIZON_FIELDS, `horizons[${index}]`);
  const displayDay = integerInRange(horizon.display_day, 2, 4, `horizons[${index}].display_day`);
  const holdingSessions = integerInRange(horizon.holding_sessions, 1, 3, `horizons[${index}].holding_sessions`);
  if (holdingSessions !== displayDay - 1) {
    throw contractError(`D+${displayDay} 的 holding_sessions 必须为 ${displayDay - 1}`);
  }
  if (!RESPONSE_STATUSES.has(horizon.status)) {
    throw contractError(`D+${displayDay} 的证据状态不受支持`);
  }
  const gateReasons = stringArray(horizon.gate_reasons, `D+${displayDay}.gate_reasons`);
  const baseRate = nullableProbability(horizon.base_rate, `D+${displayDay}.base_rate`);
  const counts = objectValue(horizon.counts, `D+${displayDay}.counts`);
  validateCounts(counts, displayDay);
  nullableIsoDate(horizon.training_cutoff, `D+${displayDay}.training_cutoff`);
  nullableString(horizon.model_version, `D+${displayDay}.model_version`);
  nonEmptyString(horizon.feature_version, `D+${displayDay}.feature_version`);
  nullableString(horizon.evidence_digest, `D+${displayDay}.evidence_digest`);
  const calibrationMetrics = horizon.calibration_metrics === null
    ? null
    : validateMetrics(objectValue(horizon.calibration_metrics, `D+${displayDay}.calibration_metrics`), displayDay);
  if (horizon.status !== "calibrated_shadow") {
    return validateUnavailableHorizon(
      horizon, displayDay, holdingSessions, baseRate, counts, calibrationMetrics, gateReasons,
    );
  }
  return validateCalibratedHorizon(
    horizon, displayDay, holdingSessions, baseRate, counts, calibrationMetrics,
    gateReasons, reportStatus, selectionQualified,
  );
}

function validateUnavailableHorizon(
  horizon, displayDay, holdingSessions, baseRate, counts, calibrationMetrics, gateReasons,
) {
  if (horizon.probability !== null || horizon.confidence_interval !== null) {
    throw contractError(`D+${displayDay} 非校准状态不得携带概率或置信区间`);
  }
  return {
    ...horizon,
    display_day: displayDay,
    holding_sessions: holdingSessions,
    probability: null,
    confidence_interval: null,
    base_rate: baseRate,
    counts,
    calibration_metrics: calibrationMetrics,
    gate_reasons: gateReasons,
  };
}

function validateCalibratedHorizon(
  horizon, displayDay, holdingSessions, baseRate, counts, calibrationMetrics,
  gateReasons, reportStatus, selectionQualified,
) {
  if (reportStatus !== "calibrated_shadow" || selectionQualified !== true) {
    throw contractError(`D+${displayDay} 只有报告通过 selection gate 后才能展示校准概率`);
  }
  validateCalibratedCounts(counts, displayDay);
  validateCalibratedEvidence(horizon, baseRate, calibrationMetrics, displayDay);
  validateCalibratedMetrics(calibrationMetrics, displayDay);
  if (gateReasons.length) throw contractError(`D+${displayDay} 校准周期不能携带阻断或限制原因`);
  const trainingCutoff = nullableIsoDate(horizon.training_cutoff, `D+${displayDay}.training_cutoff`);
  if (trainingCutoff === null) throw contractError(`D+${displayDay}.training_cutoff 不能为空`);
  const probability = requiredProbability(horizon.probability, `D+${displayDay}.probability`);
  const interval = validateConfidenceInterval(horizon.confidence_interval, displayDay, probability);
  return {
    ...horizon,
    display_day: displayDay,
    holding_sessions: holdingSessions,
    probability,
    confidence_interval: interval,
    base_rate: baseRate,
    counts,
    calibration_metrics: calibrationMetrics,
    gate_reasons: gateReasons,
  };
}

function validateTargetContract(contract) {
  assertExactFields(contract, TARGET_FIELDS, "target_contract");
  if (contract.version !== "individual-upside-net-return-label-v1") throw contractError("target_contract.version 不受支持");
  if (contract.signal_cutoff !== "completed_session_D_close") throw contractError("target_contract.signal_cutoff 不受支持");
  if (contract.entry !== "D_plus_1_official_daily_open_proxy_no_shift") throw contractError("target_contract.entry 不受支持");
  const exits = objectValue(contract.exits, "target_contract.exits");
  const expectedExits = {
    "D+2": "D_plus_2_close_holding_session_1",
    "D+3": "D_plus_3_close_holding_session_2",
    "D+4": "D_plus_4_close_holding_session_3",
  };
  const exitKeys = Object.keys(exits).sort();
  if (
    JSON.stringify(exitKeys) !== JSON.stringify(Object.keys(expectedExits).sort())
    || exitKeys.some((key) => exits[key] !== expectedExits[key])
  ) {
    throw contractError("target_contract.exits 不受支持");
  }
  if (contract.target !== "round_trip_net_return_after_declared_costs_gt_0_daily_bar_proxy") throw contractError("target_contract.target 不受支持");
  if (contract.cost_profile !== "base-a0441d84df44") throw contractError("target_contract.cost_profile 不受支持");
  if (contract.feature_version !== REGISTERED_FEATURE_VERSION) throw contractError("target_contract.feature_version 不受支持");
  if (contract.execution_notional !== 100000) throw contractError("target_contract.execution_notional 不受支持");
  if (contract.point_in_time_required !== true) throw contractError("target_contract.point_in_time_required 必须为 true");
}

function validateCounts(counts, displayDay) {
  assertExactFields(counts, COUNT_FIELDS, `D+${displayDay}.counts`);
  for (const field of [
    "observation_count", "eligible_observation_count", "independent_session_count",
    "out_of_sample_observation_count", "out_of_sample_session_count", "evaluated_fold_count",
  ]) {
    nonNegativeInteger(counts[field], `D+${displayDay}.counts.${field}`);
  }
  if (counts.eligible_observation_count > counts.observation_count) {
    throw contractError(`D+${displayDay}.counts eligible observations 不能超过 observations`);
  }
  if (counts.out_of_sample_observation_count > counts.eligible_observation_count) {
    throw contractError(`D+${displayDay}.counts OOS observations 不能超过 eligible observations`);
  }
  if (counts.out_of_sample_session_count > counts.independent_session_count) {
    throw contractError(`D+${displayDay}.counts OOS sessions 不能超过 independent sessions`);
  }
  if (counts.independent_session_count > counts.eligible_observation_count) {
    throw contractError(`D+${displayDay}.counts independent sessions 不能超过 eligible observations`);
  }
  if (counts.out_of_sample_session_count > counts.out_of_sample_observation_count) {
    throw contractError(`D+${displayDay}.counts OOS sessions 不能超过 OOS observations`);
  }
}

function validateEvidence(evidence) {
  assertExactFields(evidence, EVIDENCE_FIELDS, "evidence");
  for (const field of ["assessment_digest", "history_manifest_digest", "history_database_sha256"]) {
    nullableString(evidence[field], `evidence.${field}`);
  }
  nonNegativeInteger(evidence.official_pit_session_count, "evidence.official_pit_session_count");
  positiveInteger(evidence.required_official_pit_session_count, "evidence.required_official_pit_session_count");
  if (evidence.required_official_pit_session_count !== REQUIRED_OFFICIAL_PIT_SESSIONS) {
    throw contractError("evidence.required_official_pit_session_count 与注册门槛冲突");
  }
  nonNegativeInteger(evidence.historical_replay_session_count, "evidence.historical_replay_session_count");
  if (typeof evidence.historical_replay_official !== "boolean" || typeof evidence.selection_qualified !== "boolean") {
    throw contractError("evidence 的 replay/selection 状态必须是布尔值");
  }
}

function validateMetrics(metrics, displayDay) {
  assertExactFields(metrics, METRIC_FIELDS, `D+${displayDay}.calibration_metrics`);
  for (const field of ["brier_score", "reference_brier_score", "ece", "auc", "actual_positive_rate"]) {
    nullableProbability(metrics[field], `D+${displayDay}.calibration_metrics.${field}`);
  }
  nullableFiniteNumber(metrics.brier_skill_score, `D+${displayDay}.calibration_metrics.brier_skill_score`);
  nullableString(metrics.selection_gate_version, `D+${displayDay}.calibration_metrics.selection_gate_version`);
  nullableNonNegativeInteger(metrics.calibration_bin_count, `D+${displayDay}.calibration_metrics.calibration_bin_count`);
  nullableNonNegativeInteger(metrics.minimum_calibration_bin_session_count, `D+${displayDay}.calibration_metrics.minimum_calibration_bin_session_count`);
  if (metrics.actual_positive_rate_ci_95 !== null) {
    const interval = objectValue(metrics.actual_positive_rate_ci_95, `D+${displayDay}.calibration_metrics.actual_positive_rate_ci_95`);
    assertExactFields(interval, INTERVAL_FIELDS, `D+${displayDay}.calibration_metrics.actual_positive_rate_ci_95`);
    const lower = requiredProbability(interval.lower, `D+${displayDay}.calibration_metrics.actual_positive_rate_ci_95.lower`);
    const upper = requiredProbability(interval.upper, `D+${displayDay}.calibration_metrics.actual_positive_rate_ci_95.upper`);
    if (interval.level !== 0.95) throw contractError(`D+${displayDay} 历史正例率区间 level 必须为 0.95`);
    if (lower > upper) throw contractError(`D+${displayDay} 历史正例率区间上下界颠倒`);
    if (metrics.actual_positive_rate !== null && !(lower <= metrics.actual_positive_rate && metrics.actual_positive_rate <= upper)) {
      throw contractError(`D+${displayDay} 历史正例率必须位于其区间内`);
    }
  }
  for (const field of ["bin_monotonic", "highest_bin_above_base_rate", "all_folds_positive_brier_skill"]) {
    if (metrics[field] !== null && typeof metrics[field] !== "boolean") {
      throw contractError(`D+${displayDay}.calibration_metrics.${field} 必须是布尔值或 null`);
    }
  }
  validateBrierIdentity(metrics, displayDay);
  return metrics;
}

function validateCalibratedCounts(counts, displayDay) {
  const folds = counts.evaluated_fold_count;
  const requiredSessions = MINIMUM_SELECTION_SESSIONS[displayDay]
    + Math.max(0, folds - 2) * MINIMUM_TEST_SESSIONS_PER_FOLD;
  if (folds < 2 || counts.independent_session_count < requiredSessions
      || counts.out_of_sample_observation_count <= 0
      || counts.out_of_sample_session_count < folds * MINIMUM_TEST_SESSIONS_PER_FOLD) {
    throw contractError(`D+${displayDay} 校准周期未达到注册的独立交易日、OOS样本和folds门槛`);
  }
}

function validateCalibratedEvidence(horizon, baseRate, metrics, displayDay) {
  if (baseRate === null || metrics === null || horizon.training_cutoff === null
      || horizon.model_version !== REGISTERED_MODEL_VERSION
      || horizon.feature_version !== REGISTERED_FEATURE_VERSION
      || !isSha256(horizon.evidence_digest)) {
    throw contractError(`D+${displayDay} 校准周期的 OOS 模型、校准与摘要证据不完整`);
  }
}

function validateCalibratedMetrics(metrics, displayDay) {
  if (metrics.brier_score === null || metrics.reference_brier_score === null
      || metrics.reference_brier_score <= 0 || metrics.brier_skill_score === null
      || metrics.brier_skill_score <= 0 || metrics.actual_positive_rate === null
      || metrics.actual_positive_rate_ci_95 === null || metrics.bin_monotonic !== true
      || metrics.highest_bin_above_base_rate !== true
      || metrics.selection_gate_version !== "market-scan-probability-selection-gates-v1"
      || metrics.calibration_bin_count < 2
      || metrics.minimum_calibration_bin_session_count < MINIMUM_CALIBRATION_BIN_SESSIONS
      || metrics.all_folds_positive_brier_skill !== true) {
    throw contractError(`D+${displayDay} OOS校准摘要未证明 selection 门禁通过`);
  }
}

function validateBrierIdentity(metrics, displayDay) {
  const values = [metrics.brier_score, metrics.reference_brier_score, metrics.brier_skill_score];
  if (values.some((value) => value === null)) return;
  const expected = 1 - metrics.brier_score / metrics.reference_brier_score;
  if (metrics.reference_brier_score <= 0
      || Math.abs(metrics.brier_skill_score - expected) > Math.max(1e-12, Math.abs(expected) * 1e-9)) {
    throw contractError(`D+${displayDay} Brier skill 无法由 Brier/reference Brier 重建`);
  }
}

function validateConfidenceInterval(value, displayDay, probability) {
  const interval = objectValue(value, `D+${displayDay}.confidence_interval`);
  assertExactFields(interval, INTERVAL_FIELDS, `D+${displayDay}.confidence_interval`);
  const lower = requiredProbability(interval.lower, `D+${displayDay}.confidence_interval.lower`);
  const upper = requiredProbability(interval.upper, `D+${displayDay}.confidence_interval.upper`);
  if (lower > probability || probability > upper) {
    throw contractError(`D+${displayDay} 概率必须位于置信区间内`);
  }
  if (interval.level !== 0.95) {
    throw contractError(`D+${displayDay}.confidence_interval.level 必须为 0.95`);
  }
  const level = 0.95;
  return { ...interval, lower, upper, level };
}

function validateProductionEffect(value) {
  if (value !== "none") throw contractError("production_effect 必须明确为 none");
}

function sameSymbol(left, right) {
  const parse = (value) => {
    const match = String(value || "").trim().toUpperCase().match(/^(\d{6})(?:\.(SH|SZ|BJ))?$/);
    return match ? { code: match[1], market: match[2] || null } : null;
  };
  const actual = parse(left);
  const expected = parse(right);
  if (!actual || !expected || !actual.market || actual.code !== expected.code) return false;
  return !expected.market || actual.market === expected.market;
}

function canonicalAshareSymbol(value, label) {
  const text = nonEmptyString(value, label).toUpperCase();
  const match = text.match(/^(\d{6})\.(SH|SZ|BJ)$/);
  if (!match) throw contractError(`${label} 必须是带交易所的规范 A 股代码`);
  const [, code, market] = match;
  const valid = market === "SH"
    ? code.startsWith("6")
    : market === "SZ"
      ? code.startsWith("0") || code.startsWith("3")
      : ["43", "83", "87", "88", "92"].some((prefix) => code.startsWith(prefix));
  if (!valid) throw contractError(`${label} 的代码与交易所不一致`);
  return text;
}

function objectValue(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw contractError(`${label} 必须是对象`);
  }
  return value;
}

function arrayValue(value, label) {
  if (!Array.isArray(value)) throw contractError(`${label} 必须是数组`);
  return value;
}

function assertExactFields(value, expected, label) {
  const fields = Object.keys(value);
  const unexpected = fields.filter((field) => !expected.has(field));
  const missing = [...expected].filter((field) => !Object.hasOwn(value, field));
  if (unexpected.length || missing.length) {
    throw contractError(`${label} 字段无效：多余 ${unexpected.join(",") || "无"}；缺少 ${missing.join(",") || "无"}`);
  }
}

function stringArray(value, label) {
  return arrayValue(value, label).map((item, index) => nonEmptyString(item, `${label}[${index}]`));
}

function nonEmptyString(value, label) {
  if (typeof value !== "string" || !value.trim()) throw contractError(`${label} 必须是非空字符串`);
  return value.trim();
}

function nullableString(value, label) {
  if (value === null || value === undefined) return null;
  return nonEmptyString(value, label);
}

function nullableIsoDate(value, label) {
  if (value === null || value === undefined) return null;
  const text = nonEmptyString(value, label);
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match || !isCalendarDate(match[1], match[2], match[3])) {
    throw contractError(`${label} 必须是 ISO 日期或 null`);
  }
  return text;
}

function requiredTimestamp(value, label) {
  const text = nonEmptyString(value, label);
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/);
  if (!match || !isCalendarDate(match[1], match[2], match[3])) {
    throw contractError(`${label} 必须是含时区的有效时间`);
  }
  const timestamp = Date.parse(text);
  if (!Number.isFinite(timestamp)) throw contractError(`${label} 必须是有效时间`);
  if (timestamp > Date.now() + 5 * 60 * 1000) throw contractError(`${label} 不能晚于当前时间`);
  return timestamp;
}

function isCalendarDate(year, month, day) {
  const timestamp = Date.UTC(Number(year), Number(month) - 1, Number(day));
  const parsed = new Date(timestamp);
  return parsed.getUTCFullYear() === Number(year)
    && parsed.getUTCMonth() + 1 === Number(month)
    && parsed.getUTCDate() === Number(day);
}

function integerInRange(value, minimum, maximum, label) {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw contractError(`${label} 必须是 ${minimum} 到 ${maximum} 的整数`);
  }
  return value;
}

function nonNegativeInteger(value, label) {
  if (!Number.isInteger(value) || value < 0) throw contractError(`${label} 必须是非负整数`);
  return value;
}

function positiveInteger(value, label) {
  if (!Number.isInteger(value) || value < 1) throw contractError(`${label} 必须是正整数`);
  return value;
}

function nullableFiniteNumber(value, label) {
  if (value !== null && (typeof value !== "number" || !Number.isFinite(value))) {
    throw contractError(`${label} 必须是有限数值或 null`);
  }
  return value;
}

function nullableNonNegativeInteger(value, label) {
  if (value === null || value === undefined) return null;
  return nonNegativeInteger(value, label);
}

function nullableProbability(value, label) {
  return value === null || value === undefined ? null : requiredProbability(value, label);
}

function requiredProbability(value, label) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    throw contractError(`${label} 必须是 0 到 1 的有限数值`);
  }
  return value;
}

function contractError(message) {
  const error = new Error(message);
  error.name = "IndividualProbabilityContractError";
  return error;
}

function isSha256(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function isRegisteredExchangeSession(value) {
  const [year, month, day] = value.split("-").map(Number);
  if (year !== REGISTERED_SIGNAL_YEAR || REGISTERED_WEEKDAY_CLOSURES.has(value)) return false;
  const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
  return weekday >= 1 && weekday <= 5;
}

export const INDIVIDUAL_PROBABILITY_HORIZONS = HORIZON_DAYS;
