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
  const calibrated = artifact.status === CALIBRATED_STATUS;
  setAttribute(elements.probabilityResearch, "aria-busy", "false");
  setData(elements.probabilityResearch, "marketScanRunId", normalized.run_id ?? "");
  setData(elements.probabilityResearch, "evidenceStatus", artifact.status);
  setText(elements.probabilityStatus, calibrated ? "样本外已校准 · Shadow" : "证据不足");
  elements.probabilityStatus.className = calibrated ? "calibrated" : "insufficient";
  setText(elements.probabilityTarget, targetLabel(artifact.target_definition));
  setText(elements.probabilityBaseRate, percentageText(artifact.base_rate));
  setText(elements.probabilityEvidence, evidenceText(artifact));
  setText(elements.probabilityVersion, versionText(artifact));
  setText(elements.probabilityCutoff, artifact.training_cutoff || "--");
  setText(elements.probabilityLimitations, limitationsText(artifact));
  syncMarketScanProbabilityFilter(elements, artifact);
}

export function resetMarketScanProbabilityResearch(elements, runId = null) {
  setAttribute(elements.probabilityResearch, "aria-busy", runId ? "true" : "false");
  setData(elements.probabilityResearch, "marketScanRunId", runId ?? "");
  setData(elements.probabilityResearch, "evidenceStatus", "not_generated");
  setText(elements.probabilityStatus, runId ? "正在读取证据" : "证据不足");
  elements.probabilityStatus.className = "insufficient";
  setText(elements.probabilityTarget, "未来所选周期净超额收益为正");
  [elements.probabilityBaseRate, elements.probabilityEvidence, elements.probabilityVersion, elements.probabilityCutoff]
    .forEach((element) => setText(element, "--"));
  setText(elements.probabilityLimitations, runId ? "正在读取该批次的冻结概率证据。" : "尚无可验证的上涨概率证据。");
  elements.probabilityMin.value = "";
  elements.probabilityMin.disabled = true;
  setText(elements.probabilityFilterHelp, "只有样本外已校准的 Shadow 概率才可筛选。当前已禁用。");
}

export function marketScanProbabilityCell(value, horizon = MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON) {
  const item = objectValue(value);
  const record = objectValue(objectValue(item.upside_probabilities)[String(horizon)]);
  if (record.status !== CALIBRATED_STATUS || finiteProbability(record.probability) === null) {
    return `<div class="market-scan-probability insufficient"><strong>${escapeHtml(horizon)}日 · 证据不足</strong><span>不上屏概率数值</span></div>`;
  }
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
    const calibrated = record.status === CALIBRATED_STATUS && finiteProbability(record.probability) !== null;
    const interval = calibrated ? normalizedInterval(record.confidence_interval, record.probability, false) : null;
    const probability = calibrated ? percentageText(record.probability) : "证据不足";
    const intervalText = interval ? `${percentageText(interval.lower)}–${percentageText(interval.upper)}` : "证据不足";
    return `<tr><th scope="row">${horizon} 日</th><td>${escapeHtml(probability)}</td><td>${escapeHtml(intervalText)}</td><td>${escapeHtml(percentageText(record.base_rate ?? artifact.base_rate))}</td><td>${escapeHtml(statusLabel(record.status || artifact.status))}</td></tr>`;
  }).join("");
  const primary = probabilityArtifact(research, MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON);
  const evidence = { ...primary, ...objectValue(probabilities[String(MARKET_SCAN_DEFAULT_PROBABILITY_HORIZON)]) };
  return `<section class="market-scan-probability-snapshot"><h4>上涨概率研究 · 冻结 Shadow 证据</h4><p class="market-scan-snapshot-rule">趋势强度是序数状态分；以下概率来自该批次持久化的样本外证据，不参与生产排序。</p><div class="market-scan-probability-table-wrap"><table><thead><tr><th>周期</th><th>净超额为正</th><th>95% CI</th><th>基础胜率</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table></div><dl class="market-scan-probability-evidence"><div><dt>模型 / 特征</dt><dd>${escapeHtml(`${evidence.model_version || "--"} / ${evidence.feature_version || "--"}`)}</dd></div><div><dt>标签 / 成本</dt><dd>${escapeHtml(`${evidence.label_version || "--"} / ${evidence.cost_model_version || "--"}`)}</dd></div><div><dt>训练截止</dt><dd>${escapeHtml(evidence.training_cutoff || "--")}</dd></div><div><dt>样本证据</dt><dd>${escapeHtml(evidenceText(evidence))}</dd></div><div><dt>校准指标</dt><dd>${escapeHtml(calibrationMetricsText(evidence))}</dd></div><div><dt>局限</dt><dd>${escapeHtml(limitationsText(evidence))}</dd></div></dl></section>`;
}

function normalizeArtifact(value, horizon) {
  if (value === null || value === undefined) return emptyArtifact(horizon);
  const raw = requireObject(value, `probability_research.horizons.${horizon}`);
  const status = normalizeStatus(raw.status);
  const versions = objectValue(raw.versions);
  if (raw.horizon !== null && raw.horizon !== undefined && Number(raw.horizon) !== horizon) {
    throw probabilityContractError(`probability_research.horizons.${horizon}.horizon 不匹配`);
  }
  return {
    ...raw,
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

function syncMarketScanProbabilityFilter(elements, artifact) {
  const enabled = artifact.status === CALIBRATED_STATUS;
  elements.probabilityMin.disabled = !enabled;
  if (!enabled) elements.probabilityMin.value = "";
  setData(elements.probabilityMin, "evidenceStatus", artifact.status);
  setText(
    elements.probabilityFilterHelp,
    enabled
      ? "按当前周期样本外校准概率筛选；只缩小结果集，不改变生产名次。"
      : "只有样本外已校准的 Shadow 概率才可筛选。当前已禁用。",
  );
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
  const support = objectValue(artifact.sample_support);
  const counts = objectValue(artifact.counts);
  const training = countValue(artifact.training_independent_date_count ?? support.training_independent_dates ?? counts.training_session_count);
  const calibration = countValue(artifact.calibration_independent_date_count ?? support.calibration_independent_dates ?? counts.calibration_session_count);
  const test = countValue(artifact.test_independent_date_count ?? support.test_independent_dates ?? counts.test_session_count);
  const observations = countValue(artifact.observation_count ?? support.observation_count ?? counts.observation_count);
  return `训练 ${training} 日 · 校准 ${calibration} 日 · 测试 ${test} 日 · ${observations} 条`;
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
  return values.length ? values.join("；") : artifact.status === CALIBRATED_STATUS ? "仅用于 Shadow 研究，不参与生产排序。" : "证据不足，暂不输出概率。";
}

function statusLabel(status) {
  return status === CALIBRATED_STATUS ? "样本外已校准 · Shadow" : "证据不足";
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
