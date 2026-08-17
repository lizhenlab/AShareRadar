import { escapeHtml } from "./dom.js";
import { evidenceStatusLabel } from "./individual-probability-contracts.js";

const EMPTY_HORIZONS = Object.freeze([2, 3, 4].map((displayDay) => Object.freeze({
  display_day: displayDay,
  holding_sessions: displayDay - 1,
  status: "not_generated",
  probability: null,
  confidence_interval: null,
  base_rate: null,
  counts: {},
  gate_reasons: ["尚未读取该周期的独立研究证据"],
})));
const LIMITATION_LABELS = Object.freeze({
  historical_replay_is_not_official_point_in_time_evidence: "认证历史回放不是正式逐日点时证据",
  survivorship_bias_from_fixed_current_sample: "固定当前样本可能含幸存者偏差",
  historical_listing_st_and_delisting_membership_unavailable: "历史上市、ST 与退市成员状态尚不可用",
  qfq_provider_vintage_is_one_attested_snapshot_not_daily_vintages: "前复权行情仅有一次认证快照，非逐日版本且不等于历史现金成交价",
  amount_and_turnover_unavailable_capacity_not_modelled: "缺少成交额和换手历史，容量尚未建模",
  D_plus_1_open_is_daily_bar_proxy_not_proven_executable_fill: "D+1 开盘价只是日K价格代理，不能证明实际成交",
  daily_price_limit_fill_and_exit_tradeability_not_modelled: "涨跌停排队、实际买入及退出可交易性尚未建模",
  shadow_research_only_no_production_ranking_or_advice_effect: "仅用于 Shadow 研究，不影响生产排名或操作建议",
  legacy_official_pit_sources_audit_only_not_current_evidence: "旧版正式 PIT 来源仅供历史审计，不计入当前证据",
  compact_horizon_metrics_not_independently_replayable: "周期摘要指标尚不能独立重放验证",
  official_pit_source_artifacts_not_runtime_replayed: "正式 PIT 来源尚未在运行时逐源定位并重放，当前按 0 日计",
});
const GATE_REASON_LABELS = Object.freeze({
  historical_replay_not_official_point_in_time: "历史回放不是正式逐日点时证据",
  official_pit_sessions_below_registered_minimum: "正式点时交易日尚未达到注册门槛",
  current_stock_replayable_predictor_not_persisted: "尚未发布可重放的当前个股预测器",
  minimum_probability_bin_sessions: "概率分箱的独立交易日不足",
  shadow_only_no_production_ranking_effect: "仅作 Shadow 研究，不影响生产排名",
});
const SELECTION_GATE_LABELS = Object.freeze({
  complete_label_contract_bound: "标签与成本合同不完整",
  effective_probability_stratification: "有效概率分层未通过",
  multiple_complete_oos_folds: "完整样本外折数不足",
  positive_oos_brier_skill: "样本外 Brier Skill 未为正",
  stable_positive_skill_across_complete_oos_folds: "各完整样本外折未保持正 Brier Skill",
});

export function createIndividualProbabilityView(root = globalThis.document) {
  const elements = probabilityElements(root);
  const available = () => probabilitySurfaceAvailable(elements);
  return {
    available,
    elements,
    bindRetry: (handler) => elements.retry?.addEventListener?.("click", handler),
    renderLoading: (symbol) => renderLoading(elements, available, symbol),
    renderReport: (report) => renderReport(elements, available, report),
    renderUnavailable: (reason) => renderUnavailable(elements, available, reason),
  };
}

function probabilityElements(root) {
  const get = (id) => root?.getElementById?.(id) || null;
  return {
    shell: get("individualProbabilityResearch"), cards: get("individualProbabilityCards"),
    target: get("individualProbabilityTarget"), evidence: get("individualProbabilityEvidence"),
    limitations: get("individualProbabilityLimitations"), status: get("individualProbabilityAnnouncement"),
    retry: get("individualProbabilityRetry"),
  };
}

function probabilitySurfaceAvailable(elements) {
  return Boolean(
    elements.shell
    && elements.shell.dataset?.individualProbabilitySurface === "true"
    && elements.cards
    && elements.target
    && elements.evidence
    && elements.limitations
    && elements.status
    && elements.retry
  );
}

function renderLoading(elements, available, symbol) {
  if (!available()) return false;
  setBusy(elements, true, "loading");
  elements.retry.hidden = true;
  elements.target.textContent = targetDescription();
  elements.cards.innerHTML = [2, 3, 4].map((day) => renderCard({
    ...EMPTY_HORIZONS[day - 2], status: "not_generated", gate_reasons: [`正在读取 ${symbol || "当前个股"} 的独立证据`],
  }, { loading: true })).join("");
  elements.evidence.innerHTML = renderEvidence({ signal_date: null, generated_at: null, evidence: {} });
  elements.limitations.innerHTML = "<li>正在读取样本外校准证据；主分析可继续使用。</li>";
  announce(elements, "正在读取 D+2、D+3、D+4 上涨概率研究");
  return true;
}

function renderReport(elements, available, report) {
  if (!available()) return false;
  setBusy(elements, false, report.status);
  elements.retry.hidden = true;
  elements.target.textContent = targetDescription(report.target_contract);
  elements.cards.innerHTML = report.horizons.map((horizon) => renderCard(horizon)).join("");
  elements.evidence.innerHTML = renderEvidence(report);
  elements.limitations.innerHTML = collectLimitations(report).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const calibrated = report.horizons.filter((horizon) => horizon.status === "calibrated_shadow").length;
  announce(elements, calibrated
    ? `${report.symbol} 已读取 ${calibrated} 个样本外校准周期；其余周期保持证据状态`
    : `${report.symbol} 尚无可展示的样本外校准概率`);
  return true;
}

function renderUnavailable(elements, available, reason) {
  if (!available()) return false;
  const safeReason = String(reason || "个股上涨概率研究暂不可用").trim();
  setBusy(elements, false, "unavailable");
  elements.retry.hidden = false;
  elements.cards.innerHTML = [2, 3, 4].map((day) => renderCard({
    ...EMPTY_HORIZONS[day - 2], status: "unavailable", gate_reasons: [safeReason],
  })).join("");
  elements.evidence.innerHTML = renderEvidence({ signal_date: null, generated_at: null, evidence: {} });
  elements.limitations.innerHTML = `<li>${escapeHtml(safeReason)}；主分析、趋势评分与操作建议不受影响。</li>`;
  announce(elements, `上涨概率研究暂不可用：${safeReason}`);
  return true;
}

function setBusy(elements, busy, status) {
  elements.shell.setAttribute?.("aria-busy", String(busy));
  elements.shell.dataset.status = status;
}

function announce(elements, message) {
  if (elements.status) elements.status.textContent = message;
}

function renderCard(horizon, options = {}) {
  const calibrated = horizon.status === "calibrated_shadow" && !options.loading;
  const probability = calibrated ? formatPercent(horizon.probability) : "—";
  const interval = calibrated
    ? `${formatPercent(horizon.confidence_interval.lower)}–${formatPercent(horizon.confidence_interval.upper)}`
    : "—";
  const baseRate = horizon.base_rate === null || horizon.base_rate === undefined ? "—" : formatPercent(horizon.base_rate);
  const counts = horizon.counts || {};
  const dates = countValue(
    counts.independent_session_count ?? counts.independent_dates ?? counts.independent_date_count ?? counts.test_session_count
  );
  const observations = countValue(counts.observation_count ?? counts.observations ?? counts.test_observation_count);
  const reason = firstReason(horizon, options.loading);
  const status = options.loading ? "读取中" : evidenceStatusLabel(horizon.status);
  const statusClass = calibrated ? "calibrated" : horizon.status;
  return `
    <article class="individual-probability-card ${escapeHtml(statusClass)}" data-display-day="${horizon.display_day}" data-status="${escapeHtml(horizon.status)}">
      <div class="individual-probability-card-head">
        <div><span>目标交易日</span><strong>D+${horizon.display_day}</strong></div>
        <em>${escapeHtml(status)}</em>
      </div>
      <div class="individual-probability-value"><strong>${escapeHtml(probability)}</strong><span>上涨概率</span></div>
      <dl>
        <div><dt>95% 概率估计区间</dt><dd>${escapeHtml(interval)}</dd></div>
        <div><dt>持有</dt><dd>${horizon.holding_sessions} 个交易日</dd></div>
        <div><dt>非官方历史基准率</dt><dd>${escapeHtml(baseRate)}</dd></div>
        <div><dt>非官方历史日期 / 观察</dt><dd>${escapeHtml(dates)} / ${escapeHtml(observations)}</dd></div>
        <div><dt>证据截止</dt><dd>${escapeHtml(horizon.training_cutoff || "—")}</dd></div>
        <div><dt>模型 / 特征</dt><dd>${escapeHtml(versionPair(horizon.model_version, horizon.feature_version))}</dd></div>
      </dl>
      ${renderHistoricalDiagnostics(horizon.calibration_metrics)}
      <p>${escapeHtml(reason)}</p>
    </article>`;
}

function renderEvidence(report) {
  const evidence = report.evidence || {};
  const cutoffs = report.horizons?.map((item) => item.training_cutoff).filter(Boolean) || [];
  const cutoff = cutoffs.length ? [...new Set(cutoffs)].join(" / ") : null;
  const modelVersion = versionSet(report.horizons, "model_version");
  const evidenceVersion = evidence.assessment_digest || report.schema_version;
  return [
    ["最新正式 PIT 日", evidence.official_pit_session_count > 0 ? report.signal_date || "—" : "—"],
    ["正式 PIT 日期", countPair(evidence.official_pit_session_count, evidence.required_official_pit_session_count)],
    ["非官方历史回放", `${countValue(evidence.historical_replay_session_count)} 日（${evidence.historical_replay_official === true ? "正式" : "非正式"}）`],
    ["证据截止", cutoff || "—"],
    ["模型版本", modelVersion || "—"],
    ["证据版本", evidenceVersion || "—"],
  ].map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}

function targetDescription() {
  return "D+1 官方日K开盘价代理（不保证成交）→ 固定 D+2 / D+3 / D+4 收盘；持有 1 / 2 / 3 个交易日；目标：扣除声明成本后的日K代理净收益 > 0";
}

function collectLimitations(report) {
  const collected = [
    "Shadow 独立研究：趋势分是序数状态分，不是概率；本面板不改变生产评分、排名或操作建议。",
    "95% 概率估计区间描述模型估计的不确定性，不是该股票单次涨跌结果区间或收益区间。",
    ...report.limitations.map(limitationLabel),
  ];
  return [...new Set(collected.filter((item) => typeof item === "string" && item.trim()).map((item) => item.trim()))];
}

function limitationLabel(value) {
  return LIMITATION_LABELS[String(value)] || String(value);
}

function firstReason(horizon, loading) {
  if (loading) return gateReasonLabel(horizon.gate_reasons[0]) || "正在读取证据";
  if (horizon.status === "calibrated_shadow") return "仅作独立 Shadow 研究，不构成操作建议。";
  return gateReasonLabel(horizon.gate_reasons[0]) || {
    insufficient_data: "独立日期或观察样本未通过门禁。",
    not_generated: "该周期尚未生成研究证据。",
    unavailable: "该周期证据暂不可用。",
  }[horizon.status] || "证据暂不可用。";
}

function gateReasonLabel(value) {
  const reason = String(value || "");
  if (GATE_REASON_LABELS[reason]) return GATE_REASON_LABELS[reason];
  const prefix = "selection_gate_failed:";
  if (reason.startsWith(prefix)) {
    const gate = reason.slice(prefix.length);
    return SELECTION_GATE_LABELS[gate] || `选择门禁未通过：${gate}`;
  }
  return reason;
}

function countValue(value) {
  return Number.isInteger(value) && value >= 0 ? String(value) : "—";
}

function countPair(value, required) {
  return `${countValue(value)} / ${countValue(required)}`;
}

function versionPair(modelVersion, featureVersion) {
  const values = [modelVersion, featureVersion].filter(Boolean);
  return values.length ? values.join(" / ") : "—";
}

function versionSet(horizons, field) {
  const values = (Array.isArray(horizons) ? horizons : []).map((item) => item[field]).filter(Boolean);
  return values.length ? [...new Set(values)].join(" / ") : "—";
}

function renderHistoricalDiagnostics(metrics) {
  if (!metrics || typeof metrics !== "object") return "";
  return `
    <details class="individual-probability-diagnostics">
      <summary>非官方历史 OOS 诊断（不是当前个股概率）</summary>
      <dl>
        <div><dt>Brier skill</dt><dd>${escapeHtml(metricNumber(metrics.brier_skill_score, 3))}</dd></div>
        <div><dt>AUC</dt><dd>${escapeHtml(metricNumber(metrics.auc, 3))}</dd></div>
        <div><dt>ECE</dt><dd>${escapeHtml(metricNumber(metrics.ece, 3))}</dd></div>
        <div><dt>分箱单调</dt><dd>${escapeHtml(booleanLabel(metrics.bin_monotonic))}</dd></div>
      </dl>
    </details>`;
}

function metricNumber(value, digits) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

function booleanLabel(value) {
  return value === true ? "是" : value === false ? "否" : "—";
}

function formatPercent(value) {
  return `${(Number(value) * 100).toFixed(1)}%`;
}
