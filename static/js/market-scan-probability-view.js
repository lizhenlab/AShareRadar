import { escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";

export const MARKET_SCAN_PROBABILITY_HORIZONS = Object.freeze([1, 5, 20]);
export const MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON = 5;
const CALIBRATED_STATUS = "calibrated_shadow";
const ALLOWED_STATUSES = new Set([
  "calibrated_shadow",
  "insufficient_data",
  "insufficient_evidence",
  "not_generated",
]);
const LIMITATION_LABELS = Object.freeze({
  no_observations: "尚无成熟标签观察",
  minimum_label_coverage: "标签覆盖率不足",
  minimum_independent_sessions: "独立交易日不足",
  shadow_only_no_production_ranking_effect: "仅用于 Shadow 研究，不影响生产排序",
  run_has_no_out_of_sample_calibrated_prediction: "当前批次没有样本外校准预测",
  live_point_in_time_source_archived: "已归档真实点时样本",
  waiting_fixed_horizon_labels: "等待固定周期标签成熟",
  shadow_research_only_no_production_ranking_effect: "仅用于 Shadow 研究，不影响生产排序",
  daily_bar_cannot_reconstruct_intraday_queue_or_execution_order: "日K无法还原盘中排队或成交顺序",
  signal_date_instrument_status_held_constant_through_horizon: "持有期证券状态暂按信号日冻结",
  missing_fixed_session_bar_is_unavailable_and_never_shifted: "固定交易日缺K线即标记不可用，不顺延",
  integrity_digest_is_not_an_authenticity_signature: "摘要用于完整性校验，不代表数字签名",
  individual_probability_projection_not_published: "尚未发布逐股概率投影",
  selection_filter_fail_closed: "选股筛选保持关闭",
  bounded_sample_benchmark_not_full_market_contract_selection_forbidden: "有界样本不满足全市场基准与 Top100 契约，禁止用于选股筛选",
});
const SELECTION_GATE_LABELS = Object.freeze({
  complete_label_contract_bound: "标签/成本契约",
  positive_oos_brier_skill: "总体 Brier Skill",
  effective_probability_stratification: "概率分层",
  multiple_complete_oos_folds: "完整 OOS 折数",
  stable_positive_skill_across_complete_oos_folds: "跨折稳定性",
  full_market_benchmark_contract: "全市场基准契约",
  full_market_top100_contract: "Top100 契约",
  deterministic_sample_replay: "有界样本重放",
});

export function marketScanProbabilityElements(root, requireElement) {
  const get = (id) => requireElement(root, id);
  const horizon1d = get("marketScanProbabilityHorizon1d");
  const horizon5d = get("marketScanProbabilityHorizon5d");
  const horizon20d = get("marketScanProbabilityHorizon20d");
  return {
    probabilityResearch: get("marketScanProbabilityResearch"),
    probabilityHorizonControl: get("marketScanProbabilityHorizonControl"),
    probabilityHorizon1d: horizon1d,
    probabilityHorizon5d: horizon5d,
    probabilityHorizon20d: horizon20d,
    probabilityHorizonInputs: [horizon1d, horizon5d, horizon20d],
    probabilityStatus: get("marketScanProbabilityStatus"),
    probabilityTarget: get("marketScanProbabilityTarget"),
    probabilityBaseRate: get("marketScanProbabilityBaseRate"),
    probabilityEvidence: get("marketScanProbabilityEvidence"),
    probabilityEffectiveness: get("marketScanProbabilityEffectiveness"),
    probabilityVersion: get("marketScanProbabilityVersion"),
    probabilityCutoff: get("marketScanProbabilityCutoff"),
    probabilityLimitations: get("marketScanProbabilityLimitations"),
    probabilityMin: get("marketScanProbabilityMin"),
    probabilityFilterHelp: get("marketScanProbabilityFilterHelp"),
  };
}

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
  const horizons = Object.fromEntries(MARKET_SCAN_PROBABILITY_HORIZONS.map((horizon) => [
    String(horizon),
    normalizeArtifact(primaryTarget(rawHorizons[String(horizon)]), horizon),
  ]));
  return {
    ...payload,
    schema_version: String(payload.schema_version || "market-scan-probability-not-generated-v1"),
    run_id: expectedRunId,
    default_horizon: MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON,
    primary_target: "net_excess_positive",
    horizons,
  };
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

export function selectedMarketScanProbabilityHorizon(elements) {
  const selected = elements.probabilityHorizonInputs?.find((input) => input.checked);
  return validHorizon(selected?.value) || MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON;
}

export function bindMarketScanProbabilityHorizon(elements, onChange) {
  elements.probabilityHorizonInputs.forEach((input) => input.addEventListener("change", () => {
    if (input.checked) onChange?.(Number(input.value));
  }));
}

export function renderMarketScanProbabilityResearch(elements, research) {
  const normalized = research || emptyProbabilityResearch(null);
  const horizon = selectedMarketScanProbabilityHorizon(elements);
  const artifact = probabilityArtifact(normalized, horizon);
  setAttribute(elements.probabilityResearch, "aria-busy", "false");
  setData(elements.probabilityResearch, "marketScanRunId", normalized.run_id ?? "");
  setData(elements.probabilityResearch, "evidenceStatus", artifact.status);
  setText(elements.probabilityStatus, statusLabel(artifact.status));
  elements.probabilityStatus.className = artifact.status === CALIBRATED_STATUS ? "calibrated" : "insufficient";
  setText(elements.probabilityTarget, targetLabel(artifact.target_definition));
  setText(elements.probabilityBaseRate, percentageText(artifact.base_rate));
  setText(elements.probabilityEvidence, evidenceText(artifact));
  setText(elements.probabilityEffectiveness, effectivenessText(artifact));
  elements.probabilityEffectiveness.className = selectionQualificationPassed(artifact)
    ? "qualified"
    : "insufficient";
  setText(elements.probabilityVersion, versionText(artifact));
  setText(elements.probabilityCutoff, artifact.training_cutoff || "--");
  setText(elements.probabilityLimitations, limitationsText(artifact));
  syncMarketScanProbabilityFilter(elements, artifact);
}

export function resetMarketScanProbabilityResearch(elements, runId = null) {
  setAttribute(elements.probabilityResearch, "aria-busy", runId ? "true" : "false");
  setData(elements.probabilityResearch, "marketScanRunId", runId ?? "");
  setData(elements.probabilityResearch, "evidenceStatus", "not_generated");
  setText(elements.probabilityStatus, runId ? "正在读取证据" : "尚未生成研究证据");
  elements.probabilityStatus.className = "insufficient";
  setText(elements.probabilityTarget, "未来所选周期净超额收益为正");
  [elements.probabilityBaseRate, elements.probabilityEvidence, elements.probabilityEffectiveness, elements.probabilityVersion, elements.probabilityCutoff]
    .forEach((element) => setText(element, "--"));
  setText(elements.probabilityLimitations, runId ? "正在读取该批次的冻结概率证据。" : "尚无可验证的上涨概率证据。");
  elements.probabilityMin.value = "";
  elements.probabilityMin.disabled = true;
  setText(elements.probabilityFilterHelp, "只有样本外已校准的 Shadow 概率才可筛选。当前已禁用。");
}

export function marketScanProbabilityCell(value, horizon = MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON, research = null) {
  const item = objectValue(value);
  const record = objectValue(objectValue(item.upside_probabilities)[String(horizon)]);
  const artifact = probabilityArtifact(research, horizon);
  const calibrated = artifact.status === CALIBRATED_STATUS && record.status === CALIBRATED_STATUS && finiteProbability(record.probability) !== null;
  if (!calibrated) return unavailableProbabilityCell(horizon, artifact);
  const interval = normalizedInterval(record.confidence_interval, record.probability, false);
  const intervalText = interval
    ? `95% CI ${percentageText(interval.lower)}–${percentageText(interval.upper)}`
    : "置信区间不可用";
  return `<div class="market-scan-probability calibrated"><strong>${escapeHtml(horizon)}日 ${escapeHtml(percentageText(record.probability))}</strong><span>${escapeHtml(intervalText)} · 基础 ${escapeHtml(percentageText(record.base_rate))}</span><em>样本外已校准 · Shadow</em></div>`;
}

export function marketScanProbabilitySnapshot(value, research) {
  const item = objectValue(value);
  const probabilities = objectValue(item.upside_probabilities);
  const rows = MARKET_SCAN_PROBABILITY_HORIZONS.map((horizon) => {
    const record = objectValue(probabilities[String(horizon)]);
    const artifact = probabilityArtifact(research, horizon);
    const calibrated = artifact.status === CALIBRATED_STATUS && record.status === CALIBRATED_STATUS && finiteProbability(record.probability) !== null;
    const interval = calibrated ? normalizedInterval(record.confidence_interval, record.probability, false) : null;
    const probability = calibrated ? percentageText(record.probability) : "—";
    const intervalText = interval ? `${percentageText(interval.lower)}–${percentageText(interval.upper)}` : "—";
    const status = calibrated ? CALIBRATED_STATUS : artifact.status;
    const label = artifact.status === CALIBRATED_STATUS && !calibrated ? "个股预测不可用" : statusLabel(status);
    return `<tr><th scope="row">${horizon} 日</th><td>${escapeHtml(probability)}</td><td>${escapeHtml(intervalText)}</td><td>${escapeHtml(percentageText(record.base_rate ?? artifact.base_rate))}</td><td>${escapeHtml(label)}</td></tr>`;
  }).join("");
  const primary = probabilityArtifact(research, MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON);
  const primaryRecord = objectValue(probabilities[String(MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON)]);
  const evidence = primary.status === CALIBRATED_STATUS && primaryRecord.status === CALIBRATED_STATUS
    ? { ...primary, ...primaryRecord }
    : primary;
  const copy = probabilitySnapshotCopy(primary);
  return `<section class="market-scan-probability-snapshot"><h4>${escapeHtml(copy.title)}</h4><p class="market-scan-snapshot-rule">${escapeHtml(copy.description)}</p><div class="market-scan-probability-table-wrap"><table><thead><tr><th>周期</th><th>净超额为正</th><th>95% CI</th><th>基础胜率</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table></div><dl class="market-scan-probability-evidence"><div><dt>模型 / 特征</dt><dd>${escapeHtml(`${evidence.model_version || "--"} / ${evidence.feature_version || "--"}`)}</dd></div><div><dt>标签 / 成本</dt><dd>${escapeHtml(`${evidence.label_version || "--"} / ${evidence.cost_model_version || "--"}`)}</dd></div><div><dt>训练截止</dt><dd>${escapeHtml(evidence.training_cutoff || "--")}</dd></div><div><dt>样本证据</dt><dd>${escapeHtml(evidenceText(evidence))}</dd></div><div><dt>预测效力</dt><dd>${escapeHtml(effectivenessText(evidence))}</dd></div><div><dt>校准指标</dt><dd>${escapeHtml(calibrationMetricsText(evidence))}</dd></div><div><dt>局限</dt><dd>${escapeHtml(limitationsText(evidence))}</dd></div></dl></section>`;
}

function normalizeArtifact(value, horizon) {
  if (value === null || value === undefined) return emptyArtifact(horizon);
  const raw = requireObject(value, `probability_research.horizons.${horizon}`);
  const status = normalizeStatus(raw.status);
  const selection = normalizeSelectionQualification(raw, horizon, status);
  const versions = objectValue(raw.versions);
  if (raw.horizon !== null && raw.horizon !== undefined && Number(raw.horizon) !== horizon) {
    throw probabilityContractError(`probability_research.horizons.${horizon}.horizon 不匹配`);
  }
  return {
    ...raw,
    ...selection,
    status,
    horizon,
    target_definition: String(raw.target || raw.target_definition || "net_excess_positive"),
    base_rate: optionalProbability(raw.base_rate, `probability_research.horizons.${horizon}.base_rate`),
    model_version: raw.model_version || versions.model || null,
    feature_version: raw.feature_version || versions.feature || null,
    label_version: raw.label_version || versions.label || null,
    cost_model_version: raw.cost_model_version || versions.cost_model || null,
    limitations: stringList(raw.limitations, `probability_research.horizons.${horizon}.limitations`),
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
  if (qualified === true && (status !== CALIBRATED_STATUS || qualification?.passed !== true)) {
    throw probabilityContractError(`probability_research.horizons.${horizon} 的选股效力资格与证据状态不一致`);
  }
  return { selection_qualified: qualified === true, selection_qualification: qualification };
}

function normalizePrediction(value, artifact, horizon) {
  if (value === null || value === undefined) return emptyPrediction(artifact, horizon);
  const raw = requireObject(value, `upside_probabilities.${horizon}`);
  const status = normalizeStatus(raw.status || artifact.status);
  const probability = optionalProbability(raw.probability, `upside_probabilities.${horizon}.probability`);
  if (status === CALIBRATED_STATUS && artifact.status !== CALIBRATED_STATUS) {
    throw probabilityContractError(`upside_probabilities.${horizon} 不能超越批次研究证据状态`);
  }
  if (status === CALIBRATED_STATUS && probability === null) {
    throw probabilityContractError(`upside_probabilities.${horizon}.probability 校准后不能为空`);
  }
  if (status !== CALIBRATED_STATUS && probability !== null) {
    throw probabilityContractError(`upside_probabilities.${horizon}.probability 证据不足时必须为空`);
  }
  const interval = normalizedInterval(raw.confidence_interval, probability, status === CALIBRATED_STATUS);
  const baseRate = optionalProbability(raw.base_rate ?? artifact.base_rate, `upside_probabilities.${horizon}.base_rate`);
  return {
    ...artifact,
    ...raw,
    status,
    horizon,
    probability,
    confidence_interval: interval,
    base_rate: baseRate,
    limitations: stringList(raw.limitations ?? artifact.limitations, `upside_probabilities.${horizon}.limitations`),
  };
}

function normalizedInterval(value, probability, required) {
  if (value === null || value === undefined) {
    if (required) throw probabilityContractError("calibrated_shadow 概率缺少 confidence_interval");
    return null;
  }
  const raw = Array.isArray(value) ? { lower: value[0], upper: value[1], level: 0.95 } : requireObject(value, "confidence_interval");
  const lower = optionalProbability(raw.lower, "confidence_interval.lower");
  const upper = optionalProbability(raw.upper, "confidence_interval.upper");
  const level = optionalProbability(raw.level ?? 0.95, "confidence_interval.level");
  if (lower === null || upper === null || level === null || level <= 0 || lower > upper || probability === null || probability < lower || probability > upper) {
    throw probabilityContractError("confidence_interval 必须覆盖概率且位于 0–1");
  }
  return { ...raw, level, lower, upper };
}

function primaryTarget(value) {
  if (value === null || value === undefined) return value;
  const source = objectValue(value);
  return source.net_excess_positive ?? source;
}

function probabilityArtifact(research, horizon) {
  const artifact = objectValue(objectValue(research?.horizons)[String(horizon)]);
  return Object.keys(artifact).length ? artifact : emptyArtifact(horizon);
}

function unavailableProbabilityCell(horizon, artifact) {
  if (artifact.status === CALIBRATED_STATUS) {
    return `<div class="market-scan-probability insufficient"><strong>${escapeHtml(horizon)}日 —</strong><span>个股预测不可用</span></div>`;
  }
  return '<div class="market-scan-probability insufficient" title="概率不可用；详情见上方"><strong aria-label="概率不可用">—</strong></div>';
}

function probabilitySnapshotCopy(artifact) {
  if (artifact.status === CALIBRATED_STATUS) {
    return {
      title: "上涨概率研究 · 冻结 Shadow 证据",
      description: "趋势强度是序数状态分；以下概率来自该批次持久化的样本外校准证据，不参与生产排序。",
    };
  }
  if (artifact.status === "not_generated") {
    return {
      title: "上涨概率研究 · 尚未生成",
      description: "当前批次尚未生成上涨概率研究证据，不展示概率或置信区间，也不影响生产排序。",
    };
  }
  if (artifact.fit_status === "sampled_oos_assessment" || artifact.pipeline_stage === "sampled_fit_assessed") {
    return {
      title: "上涨概率研究 · 有界样本评估完成",
      description: "已完成可重放的有界样本评估，但它不满足全市场基准与 Top100 契约；逐股概率、置信区间和选股筛选继续保持为空或关闭。",
    };
  }
  const counts = objectValue(artifact.counts);
  const archivedSessions = countNumber(counts.archived_independent_session_count);
  const sourceOnly = (archivedSessions !== null && archivedSessions > 0)
    || artifact.limitations?.includes?.("live_point_in_time_source_archived");
  return sourceOnly
    ? {
        title: "上涨概率研究 · 点时样本积累中",
        description: "当前仅归档了真实点时源样本，尚未形成成熟标签和样本外校准概率；概率与置信区间保持为空。",
      }
    : {
        title: "上涨概率研究 · 样本不足",
        description: "研究证据已生成，但尚未通过独立日期、标签覆盖或校准门槛；概率与置信区间保持为空。",
      };
}

function syncMarketScanProbabilityFilter(elements, artifact) {
  const fitted = artifact.status === CALIBRATED_STATUS;
  const enabled = fitted && selectionQualificationPassed(artifact);
  elements.probabilityMin.disabled = !enabled;
  if (!enabled) elements.probabilityMin.value = "";
  setData(elements.probabilityMin, "evidenceStatus", artifact.status);
  setText(
    elements.probabilityFilterHelp,
    enabled
      ? "按当前周期样本外校准概率筛选；只缩小结果集，不改变生产名次。"
      : fitted
        ? "概率已完成样本外校准，但预测效力门禁未通过，暂不允许用于选股筛选。"
        : probabilityUnavailableHelp(artifact.status),
  );
}

function selectionQualificationPassed(artifact) {
  const qualification = objectValue(artifact.selection_qualification);
  return artifact.selection_qualified === true && qualification.passed === true;
}

function effectivenessText(artifact) {
  if (selectionQualificationPassed(artifact)) {
    return artifact.status === CALIBRATED_STATUS ? "通过选股门禁" : "效力通过 · 个股投影待生成";
  }
  if (artifact.status === CALIBRATED_STATUS) {
    const failed = failedSelectionGateLabels(artifact);
    return failed.length ? `未通过：${failed.join("、")}` : "已拟合 · 效力未通过";
  }
  if (artifact.fit_status === "fitted_oos") {
    const failed = failedSelectionGateLabels(artifact);
    return failed.length ? `未通过：${failed.join("、")}` : "已拟合 · 效力未通过";
  }
  if (artifact.fit_status === "sampled_oos_assessment" || artifact.pipeline_stage === "sampled_fit_assessed") {
    return "有界样本评估完成 · 不具备选股资格";
  }
  if (artifact.status === "not_generated") return "--";
  return {
    source_archived: "等待首次标签维护",
    waiting_labels: "等待固定交易日标签成熟",
    fit_insufficient: "成熟样本未达拟合门槛",
    labels_matured: "标签已成熟 · 等待验证拟合",
    sampled_fit_assessed: "有界样本评估完成 · 个股投影待生成",
    projection_pending: "有界样本评估完成 · 个股投影待生成",
  }[artifact.pipeline_stage] || "等待成熟标签与样本外评估";
}

function failedSelectionGateLabels(artifact) {
  const gates = objectValue(objectValue(artifact.selection_qualification).gates);
  return Object.entries(SELECTION_GATE_LABELS)
    .filter(([name]) => gates[name] === false)
    .map(([, label]) => label);
}

function probabilityUnavailableHelp(status) {
  return status === "not_generated"
    ? "当前批次尚未生成 Shadow 研究，概率筛选不可用。"
    : "当前研究样本不足，概率筛选不可用。";
}

function emptyProbabilityResearch(runId) {
  return {
    schema_version: "market-scan-probability-not-generated-v1",
    run_id: runId,
    default_horizon: MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON,
    primary_target: "net_excess_positive",
    horizons: Object.fromEntries(MARKET_SCAN_PROBABILITY_HORIZONS.map((horizon) => [String(horizon), emptyArtifact(horizon)])),
  };
}

function emptyArtifact(horizon) {
  return {
    status: "not_generated",
    horizon,
    target_definition: "net_excess_positive",
    base_rate: null,
    limitations: ["旧批次或当前批次未持久化上涨概率证据"],
  };
}

function emptyPrediction(artifact, horizon) {
  return { ...artifact, status: "not_generated", horizon, probability: null, confidence_interval: null };
}

function normalizeStatus(value) {
  const status = String(value || "not_generated").trim();
  if (!ALLOWED_STATUSES.has(status)) throw probabilityContractError(`未知上涨概率状态：${status}`);
  return status;
}

function targetLabel(value) {
  const target = String(value || "");
  if (target.includes("net_excess") || target === "net_excess_positive") return "未来所选周期净超额收益为正";
  if (target.includes("net_return") || target === "net_return_positive") return "未来所选周期绝对净收益为正";
  return target || "未来所选周期净超额收益为正";
}

function evidenceText(artifact) {
  if (artifact.status === "not_generated") return "--";
  const counts = objectValue(artifact.counts);
  const progress = evidenceProgressText(artifact, counts);
  if (progress) return progress;
  return partitionEvidenceText(artifact, objectValue(artifact.sample_support), counts);
}

function evidenceProgressText(artifact, counts) {
  const available = countNumber(counts.available_independent_session_count);
  const archived = countNumber(counts.archived_independent_session_count);
  const mature = countNumber(counts.mature_label_session_count);
  const required = requiredIndependentSessions(artifact);
  const coverage = finiteProbability(counts.label_coverage);
  const requiredCoverage = minimumLabelCoverage(artifact);
  const observations = countNumber(counts.observation_count);
  if (archived !== null || mature !== null) {
    const archivedText = `已归档 ${archived ?? "--"} 日`;
    const matureText = available === null
      ? `成熟标签 ${mature ?? "--"}${required === null ? "" : `/${required}`}`
      : `成熟 / 可用 ${mature ?? "--"} / ${available}${required === null ? "" : ` / ${required}`}`;
    const coverageText = coverage === null ? null : `标签覆盖 ${percentageText(coverage)}${requiredCoverage === null ? "" : `/${percentageText(requiredCoverage)}`}`;
    const nextMaturity = String(counts.next_maturity_date ?? artifact.next_maturity_date ?? "").trim();
    return [archivedText, matureText, coverageText, nextMaturity ? `下次到期 ${nextMaturity}` : null, observations === null ? null : `${observations} 条点时样本`]
      .filter(Boolean).join(" · ");
  }
  if (available === null && required === null && coverage === null) return "";
  const dates = `独立日期 ${available ?? "--"}${required === null ? "" : `/${required}`}`;
  const coverageText = coverage === null ? null : `标签覆盖 ${percentageText(coverage)}${requiredCoverage === null ? "" : `/${percentageText(requiredCoverage)}`}`;
  return [dates, coverageText, observations === null ? null : `${observations} 条`].filter(Boolean).join(" · ");
}

function partitionEvidenceText(artifact, support, counts) {
  const training = countValue(artifact.training_independent_date_count ?? support.training_independent_dates ?? counts.training_session_count);
  const calibration = countValue(artifact.calibration_independent_date_count ?? support.calibration_independent_dates ?? counts.calibration_session_count);
  const test = countValue(artifact.test_independent_date_count ?? support.test_independent_dates ?? counts.test_session_count);
  const observations = countValue(artifact.observation_count ?? support.observation_count ?? counts.observation_count);
  return `训练 ${training} 日 · 校准 ${calibration} 日 · 测试 ${test} 日 · ${observations} 条`;
}

function requiredIndependentSessions(artifact) {
  const split = objectValue(objectValue(artifact.contract).split);
  const values = [
    countNumber(split.minimum_train_sessions),
    countNumber(split.minimum_calibration_sessions),
    countNumber(split.minimum_test_sessions),
    countNumber(split.gap_sessions),
  ];
  return values.some((value) => value === null) ? null : values[0] + values[1] + values[2] + (2 * values[3]);
}

function minimumLabelCoverage(artifact) {
  const evaluation = objectValue(objectValue(artifact.contract).evaluation);
  return finiteProbability(evaluation.minimum_label_coverage);
}

function calibrationMetricsText(artifact) {
  const calibrated = objectValue(objectValue(artifact.calibration_metrics).calibrated);
  if (!Object.keys(calibrated).length) return "--";
  const monotonic = typeof calibrated.bin_monotonic === "boolean" ? (calibrated.bin_monotonic ? "是" : "否") : "--";
  return `Brier ${metricText(calibrated.brier_score, 4)} · BSS ${metricText(calibrated.brier_skill_score, 3)} · ECE ${metricText(calibrated.ece, 3)} · AUC ${metricText(calibrated.auc, 3)} · 分箱单调 ${monotonic}`;
}

function metricText(value, digits) {
  return finiteMetric(value) === null ? "--" : formatNumber(value, digits);
}

function versionText(artifact) {
  const values = [artifact.model_version, artifact.feature_version, artifact.label_version, artifact.cost_model_version]
    .map((value) => String(value || "").trim()).filter(Boolean);
  return values.length ? values.join(" · ") : "--";
}

function limitationsText(artifact) {
  const values = Array.isArray(artifact.limitations) ? artifact.limitations.filter(Boolean) : [];
  if (values.length) return values.map(limitationLabel).join("；");
  if (artifact.status === CALIBRATED_STATUS) return "仅用于 Shadow 研究，不参与生产排序。";
  return artifact.status === "not_generated" ? "当前批次尚未生成上涨概率研究。" : "研究已生成，但样本不足，暂不输出概率。";
}

function limitationLabel(value) {
  return LIMITATION_LABELS[String(value)] || String(value);
}

function statusLabel(status) {
  if (status === CALIBRATED_STATUS) return "样本外已校准";
  return status === "not_generated" ? "尚未生成研究证据" : "研究已生成·样本不足";
}

function percentageText(value) {
  const number = finiteProbability(value);
  return number === null ? "--" : `${formatNumber(number * 100, 1)}%`;
}

function optionalProbability(value, path) {
  if (value === null || value === undefined) return null;
  const number = finiteProbability(value);
  if (number === null) throw probabilityContractError(`${path} 必须是 0–1 的有限数值`);
  return number;
}

function finiteProbability(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 && number <= 1 ? number : null;
}

function finiteMetric(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function stringList(value, path) {
  if (value === null || value === undefined) return [];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw probabilityContractError(`${path} 必须是字符串数组`);
  }
  return value;
}

function validHorizon(value) {
  const horizon = Number(value);
  return MARKET_SCAN_PROBABILITY_HORIZONS.includes(horizon) ? horizon : null;
}

function countValue(value) {
  if (value === null || value === undefined || value === "") return "--";
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 ? String(number) : "--";
}

function countNumber(value) {
  const number = Number(value);
  return value === null || value === undefined || value === "" || !Number.isInteger(number) || number < 0 ? null : number;
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

function setText(element, value) {
  element.textContent = String(value ?? "--");
}

function setAttribute(element, name, value) {
  if (typeof element?.setAttribute === "function") element.setAttribute(name, String(value));
  else if (element) element[name] = String(value);
}

function setData(element, name, value) {
  if (element?.dataset) element.dataset[name] = String(value);
  else setAttribute(element, `data-${name.replace(/[A-Z]/g, (match) => `-${match.toLowerCase()}`)}`, value);
}
