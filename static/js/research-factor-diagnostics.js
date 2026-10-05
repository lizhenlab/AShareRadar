import { $, escapeHtml } from "./dom.js";
import {
  factorDirectionClass,
  fallbackText,
  firstText,
  formatNumber,
  joinedMetric,
  validationStatusClass,
} from "./research-formatters.js";
import {
  asArray,
  asObject,
  renderInlineItems,
  renderLimitedItems,
  renderMetricPairs,
  renderMissingData,
  signedText,
} from "./research-render-utils.js";

export function renderFeatureSnapshot(feature) {
  const el = $("featureSnapshot");
  if (!el || !feature) {
    if (el) el.innerHTML = "";
    return;
  }
  const chips = [
    ["趋势", joinedMetric(feature.trend_score, feature.trend_label)],
    ["量价热度", proxyScoreText(feature.fund_flow_score, feature.fund_flow_data_nature)],
    ["龙头", joinedMetric(feature.leader_score, feature.leader_level)],
    ["量能", feature.volume_ratio_available === true ? `${formatNumber(feature.volume_ratio)}倍` : "—（证据不可用）"],
    ["估值", feature.valuation_score_available === true ? fallbackText(feature.valuation_score) : "—（证据不可用）"],
    ["质量", joinedMetric(feature.data_quality_level, feature.data_quality_score, " ")],
  ];
  el.innerHTML = `
    ${chips
      .map(
        ([label, value]) => `
        <div>
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>`
      )
      .join("")}
    <div class="feature-tags">${renderInlineItems(feature.tags, "i")}</div>
  `;
}

export function renderDiagnosis(diagnosis) {
  const el = $("diagnosisPanel");
  if (!el || !diagnosis) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="diagnosis-head">
      <div>
        <span>个股诊断</span>
        <strong>${escapeHtml(diagnosis.headline)}</strong>
      </div>
      <i>研究诊断（不写建议历史）：${escapeHtml(diagnosis.action)} · 诊断证据充分度 ${escapeHtml(diagnosis.confidence)}/100</i>
    </div>
    <p>${escapeHtml(diagnosis.beginner_summary)}</p>
    <small>${escapeHtml(diagnosis.professional_summary)}</small>
    <div class="diagnosis-grid">
      <div>
        <strong>确认信号</strong>
        ${renderInlineItems(diagnosis.confirmation_signals, "span", 4)}
      </div>
      <div>
        <strong>硬风险</strong>
        ${renderInlineItems(diagnosis.hard_risks, "span", 4, "risk")}
      </div>
    </div>
  `;
}

export function renderAlphaEvidence(report) {
  const el = $("alphaEvidence");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const view = alphaEvidenceView(report);
  el.innerHTML = `
    <div class="alpha-head">
      <strong>Alpha证据链</strong>
      <span>${escapeHtml(view.verdict)} · Alpha证据充分度 ${escapeHtml(view.confidence)}/100</span>
    </div>
    <p>${escapeHtml(view.summary)}</p>
    <div class="alpha-grid">
      ${renderAlphaColumn("支持证据", view.positives, "good", "等待更多积极证据。")}
      ${renderAlphaColumn("风险证据", view.negatives, "risk", "当前未识别核心风险证据。")}
    </div>
    ${renderAlphaMissingData(view.missingData)}
  `;
}

function alphaEvidenceView(report) {
  report = asObject(report);
  return {
    verdict: report.verdict,
    confidence: report.confidence,
    summary: report.summary,
    positives: asArray(report.positives).slice(0, 4),
    negatives: asArray(report.negatives).slice(0, 4),
    missingData: asArray(report.missing_data).slice(0, 6),
  };
}

function renderAlphaColumn(title, items, className, emptyText) {
  return `
      <div>
        <strong>${escapeHtml(title)}</strong>
        ${items.length ? items.map((item) => renderAlphaItem(item, className)).join("") : renderAlphaEmpty(emptyText)}
      </div>`;
}

function renderAlphaItem(item, className) {
  item = asObject(item);
  return `<span class="${className}"><b>${escapeHtml(item.title)} ${escapeHtml(signedText(item.impact))}</b><small>${escapeHtml(item.reason)}</small></span>`;
}

function renderAlphaEmpty(text) {
  return `<span><b>暂无</b><small>${escapeHtml(text)}</small></span>`;
}

function renderAlphaMissingData(items) {
  return renderMissingData(items, { tagName: "em", prefix: "待补数据：" });
}

export function renderSignalValidation(report) {
  const el = $("signalValidation");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const items = asArray(report.items);
  el.innerHTML = `
    <div class="validation-head">
      <div>
        <span>信号验证闭环</span>
        <strong>${escapeHtml(report.overall_status)}</strong>
      </div>
      <i>${escapeHtml(items.length)}项</i>
    </div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="validation-grid">
      ${renderLimitedItems(items, 4, renderValidationItem)}
    </div>
    ${renderInlineItems(report.notes, "small", 1)}
  `;
}

function renderValidationItem(item) {
  item = asObject(item);
  return `
    <div class="validation-item ${validationStatusClass(item.status)}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.status)} · 验证强度 ${escapeHtml(item.confidence)}/100</span>
      </div>
      <small>触发：${escapeHtml(item.trigger_condition)}</small>
      <small>确认：${escapeHtml(item.confirmation_condition)}</small>
      <small>失效：${escapeHtml(item.invalidation_condition)}</small>
      <em>${escapeHtml(item.historical_reference)}</em>
    </div>
  `;
}

export function renderFactorLab(report) {
  const el = $("factorLab");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const aggregation = consistentFactorScoreReport(report) ? report.score_aggregation : null;
  el.innerHTML = `
    ${renderFactorLabHead(report)}
    <div class="factor-lab-metrics">${renderFactorLabMetrics(report)}</div>
    ${renderFactorScoreAggregation(aggregation, report)}
    ${renderFactorEvidenceSupport(report)}
    <p>${escapeHtml(report.summary)}</p>
    <div class="factor-lab-grid">${renderFactorLabItems(report.factors, aggregation)}</div>
    ${renderInlineItems(report.weight_policy, "em", 2)}
    ${renderFactorParticipationNote(report.factors, aggregation)}
    ${renderInlineItems(report.notes, "small", 2)}
  `;
}

function renderFactorLabHead(report) {
  const sufficiency = report.evidence_sufficiency ?? report.calibrated_confidence ?? "--";
  const reliability = report.composite_reliability_level || compositeReliabilityLevel(sufficiency);
  return `
    <div class="factor-lab-head">
      <div>
        <span>因子实验室</span>
        <strong>${escapeHtml(report.total_score)}分 · 证据充分度 ${escapeHtml(sufficiency)}/100 · 综合可信等级 ${escapeHtml(reliability)}</strong>
      </div>
      <i>${escapeHtml(firstText(report.top_positive, "等待确认"))}</i>
    </div>`;
}

function proxyScoreText(score, nature) {
  return nature === "unavailable" || !nature ? "不可用" : fallbackText(score);
}

function compositeReliabilityLevel(value) {
  const score = Number(value);
  if (!Number.isFinite(score)) return "待确认";
  if (score >= 75) return "较高";
  if (score >= 55) return "中等";
  if (score >= 35) return "较低";
  return "不足";
}

function renderFactorLabMetrics(report) {
  return renderMetricPairs([
    ["个股画像", report.profile_label || "常规个股"],
    ["历史样本", report.calibration_sample_count || 0],
    ["正向因子", report.positive_factor_count || 0],
    ["拖累因子", report.negative_factor_count || 0],
  ]);
}

function renderFactorEvidenceSupport(report) {
  const support = asObject(report.evidence_support);
  if (report.evidence_sufficiency_version !== "factor-evidence-sufficiency.v2" || !validFactorEvidenceSupport(support)) return "";
  const note = typeof report.evidence_sufficiency_note === "string" ? report.evidence_sufficiency_note.trim() : "";
  return `
    <details data-factor-evidence-support>
      <summary>充分度依据（不是上涨概率）</summary>
      <div class="factor-lab-metrics">${renderMetricPairs([
        ["数据质量", `${formatNumber(support.data_quality_score, 1)}分`],
        ["历史校准覆盖", `${formatNumber(support.calibration_coverage_pct, 1)}%`],
        ["样本支持", `${formatNumber(support.sample_support_pct, 1)}%`],
      ])}</div>
      <p>充分度受三项中最低值限制；不是统计置信度或上涨概率。</p>
      <small>历史校准因子 ${escapeHtml(support.calibrated_factor_count)}/${escapeHtml(support.required_factor_count)}；最低相似样本 ${escapeHtml(support.minimum_similar_samples)}，充分支持阈值 ${escapeHtml(support.full_support_sample_threshold)}。</small>
      ${note ? `<p>${escapeHtml(note)}</p>` : ""}
    </details>`;
}

function validFactorEvidenceSupport(support) {
  const percentages = [support.data_quality_score, support.calibration_coverage_pct, support.sample_support_pct];
  const counts = [support.required_factor_count, support.calibrated_factor_count, support.minimum_similar_samples, support.full_support_sample_threshold];
  return percentages.every(value => Number.isFinite(value) && value >= 0 && value <= 100)
    && counts.every(value => Number.isSafeInteger(value) && value >= 0)
    && support.full_support_sample_threshold > 0
    && support.calibrated_factor_count <= support.required_factor_count;
}

function renderFactorScoreAggregation(aggregation, report) {
  if (!aggregation) return asObject(report.score_aggregation).version === "factor-aggregation.v3"
    ? "<p data-factor-score-error>评分分解不一致，等待刷新。</p>" : "";
  const chain = `方向分 ${formatNumber(aggregation.directional_score, 1)} → 风险扣分 ${formatNumber(aggregation.risk_penalty, 1)} → 总分 ${aggregation.total_score}`;
  return `<details data-factor-score-aggregation>
    <summary>综合分依据</summary>
    <p>${escapeHtml(chain)}</p>
    <p>方向覆盖 ${escapeHtml(formatNumber(aggregation.coverage_pct, 1))}%；缺失份额不转移。</p>
    <div class="factor-lab-metrics">${aggregation.groups.map(group => `<span>${escapeHtml(group.name)}
      <br>固定预算 <b>${escapeHtml(formatNumber(group.budget_pct, 1))}%</b>
      <br>贡献 ${escapeHtml(signedText(formatNumber(group.contribution, 2)))} 分
      <br>组内覆盖 ${escapeHtml(formatNumber(group.coverage_pct, 1))}%</span>`).join("")}</div>
    <p>以50分为中性，使用三组固定预算；画像不改变方向预算。风险只扣分，不加看多。</p>
    <small>覆盖度衡量可用方向证据；预算是工程参数，综合分和覆盖度都不是上涨概率。</small>
  </details>`;
}

function validFactorScoreAggregation(value) {
  const item = asObject(value);
  return item.version === "factor-aggregation.v3"
    && [item.directional_score, item.total_score, item.coverage_pct].every(factorPercentage)
    && Number.isInteger(item.total_score) && factorBoundedNumber(item.risk_penalty, 0, 12.5)
    && Array.isArray(item.groups) && item.groups.length === 3 && item.groups.every(validFactorScoreGroup)
    && Math.abs(item.groups.reduce((sum, group) => sum + group.budget_pct, 0) - 100) < 0.001
    && item.factor_shares === asObject(item.factor_shares) && Object.values(item.factor_shares).every(factorPercentage)
    && Array.isArray(item.excluded_ids) && item.excluded_ids.every(id => typeof id === "string");
}

function consistentFactorScoreReport(report) {
  const aggregation = report.score_aggregation;
  if (!validFactorScoreAggregation(aggregation) || report.total_score !== aggregation.total_score) return false;
  if (!Array.isArray(report.factors)) return false;
  const ids = new Set();
  return report.factors.every(factor => {
    const item = asObject(factor);
    if (ids.has(item.id) || !consistentFactorScoreUsage(item, aggregation.factor_shares)) return false;
    ids.add(item.id);
    return true;
  });
}

function consistentFactorScoreUsage(item, shares) {
  if (typeof item.id !== "string" || !item.id.trim() || !factorPercentage(item.score_share_pct)) return false;
  const role = item.aggregation_role === undefined ? "independent" : item.aggregation_role;
  if (!["independent", "composite"].includes(role)) return false;
  const expectedShare = Object.hasOwn(shares, item.id) ? shares[item.id] : 0;
  if (Math.abs(item.score_share_pct - expectedShare) > 1e-8) return false;
  if (role === "composite") return item.score_usage === "observation";
  if (item.id === "risk_pressure") return expectedShare === 0 && item.score_usage === "risk_constraint";
  return item.score_usage === (expectedShare > 0 ? "direction" : "excluded");
}

function validFactorScoreGroup(value) {
  const group = asObject(value);
  return typeof group.name === "string" && group.name.trim()
    && factorPercentage(group.budget_pct) && factorPercentage(group.coverage_pct)
    && Math.abs(group.budget_pct - 100 / 3) < 0.001
    && factorBoundedNumber(group.contribution, -group.budget_pct / 2, group.budget_pct / 2);
}

function factorPercentage(value) {
  return factorBoundedNumber(value, 0, 100);
}

function factorBoundedNumber(value, minimum, maximum) {
  return Number.isFinite(value) && value >= minimum && value <= maximum;
}

function renderFactorLabItems(items, aggregation) {
  return asArray(items).map(item => renderStandardFactor(item, aggregation)).join("");
}

function renderFactorParticipationNote(items, aggregation) {
  const factors = asArray(items).map(asObject).filter(item => !aggregation || item.score_usage !== "excluded");
  const currentExcludedNames = uniqueFactorNames(
    factors.filter((item) => item.participates_in_current_score === false && item.aggregation_role !== "composite"),
  );
  const compositeNames = uniqueFactorNames(factors.filter((item) => item.aggregation_role === "composite"));
  const historicalExcludedNames = uniqueFactorNames(
    factors.filter((item) => item.participates_in_current_score !== false
      && item.aggregation_role !== "composite"
      && asObject(item.calibration).participates_in_historical_aggregate === false),
  );
  return [
    compositeNames.length
      ? `<small>${escapeHtml(`当前评分口径：${compositeNames.join("、")}为复合观察，不重复计分。`)}</small>`
      : "",
    currentExcludedNames.length
      ? `<small>${escapeHtml(`当前评分口径：${currentExcludedNames.join("、")}当前不计分/不可用。`)}</small>`
      : "",
    historicalExcludedNames.length
      ? `<small>${escapeHtml(`历史聚合口径：${historicalExcludedNames.join("、")}参与当前评分，但不提供历史支持、不参与正负证据与历史样本聚合。`)}</small>`
      : "",
  ].join("");
}

function uniqueFactorNames(items) {
  return [
    ...new Set(
      items
        .map((item) => item.name)
        .filter((name) => typeof name === "string" && name.trim())
        .map((name) => name.trim()),
    ),
  ];
}

function renderStandardFactor(item, aggregation) {
  item = asObject(item);
  if (aggregation && item.score_usage !== "direction") return renderFactorUsageObservation(item);
  if (item.aggregation_role === "composite") return renderCompositeFactor(item);
  const calibration = asObject(item.calibration);
  const bucket = asArray(item.calibration_buckets)[0];
  const participates = item.participates_in_current_score !== false
    && (!aggregation || (item.data_nature !== "unavailable" && factorPercentage(item.score)));
  return `
    <div class="standard-factor ${participates ? factorDirectionClass(item) : "unavailable"}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${factorScoreLabel(item, participates, aggregation)}</span>
      </div>
      ${aggregation ? factorScoreShareLine(item) : ""}
      <div class="score-bar"><i style="width:${participates ? Math.max(0, Math.min(100, Number(item.score) || 0)) : 0}%"></i></div>
      <p>${participates ? escapeHtml(item.value) : "当前观测值不可用，未形成评分证据。"}</p>
      <small>${participates ? factorCalibrationSampleText(calibration) : "当前观测证据不可用，不纳入当前评分。"}</small>
      ${participates ? factorCalibrationReturnLine(calibration) : ""}
      ${participates ? factorPercentileLine(item) : ""}
      ${participates ? factorBucketLine(bucket) : ""}
      ${participates ? renderInlineItems(item.evidence, "small", 1) : ""}
    </div>
  `;
}

function factorScoreLabel(item, participates, aggregation) {
  if (aggregation) return participates ? `${escapeHtml(item.score)} 分` : "当前不可用";
  return participates ? `${escapeHtml(item.score)} · 权重 ${formatNumber(item.weight, 2)}` : "当前不计分/不可用 · 权重不生效";
}

function factorScoreShareLine(item) {
  return factorPercentage(item.score_share_pct)
    ? `<small>固定份额 ${escapeHtml(formatNumber(item.score_share_pct, 1))}%${item.participates_in_current_score === false ? "；缺失保留份额" : ""}</small>`
    : "<small>固定份额信息不可用</small>";
}

function renderFactorUsageObservation(item) {
  if (item.score_usage === "observation") return renderCompositeFactor(item);
  const risk = item.score_usage === "risk_constraint";
  const observed = item.data_nature !== "unavailable" && factorPercentage(item.score);
  const reason = risk ? "仅作风险约束，风险只扣分，不加看多。"
    : item.score_usage === "excluded" ? "未注册，不计入综合分。" : "计分用途未确认，不展示方向份额。";
  return `<div class="standard-factor ${risk ? "risk-constraint" : "observation"}">
    <div><strong>${escapeHtml(item.name)}</strong><span>${risk ? "风险约束" : "不计分"}</span></div>
    <p>${observed ? escapeHtml(item.value) : "当前观测证据不可用。"}</p>
    <small>${escapeHtml(reason)}</small>
    ${observed ? renderInlineItems(item.evidence, "small", 1) : ""}
  </div>`;
}

function renderCompositeFactor(item) {
  const observed = item.data_nature !== "unavailable" && typeof item.score === "number" && Number.isFinite(item.score);
  return `<div class="standard-factor composite">
    <div><strong>${escapeHtml(item.name)}</strong>
      <span>${observed ? `${escapeHtml(item.score)} · ` : ""}复合观察，不重复计分</span></div>
    <p>${observed ? escapeHtml(item.value) : "当前观测证据不可用。"}</p>
    <small>包含其他因子信息，不再叠加到评分，也不参与历史概率。</small>
    ${observed ? renderInlineItems(item.evidence, "small", 1) : ""}
  </div>`;
}

function factorCalibrationSampleText(calibration) {
  calibration = asObject(calibration);
  if (!calibration.sample_count) {
    return escapeHtml(calibration.confidence_level || "待补数据");
  }
  return `样本 ${escapeHtml(calibration.sample_count)} · ${escapeHtml(calibration.confidence_level || "观察")} / ${escapeHtml(calibration.expected_level || "观察")}`;
}

function factorCalibrationReturnLine(calibration) {
  calibration = asObject(calibration);
  if (!calibration.sample_count) return "";
  const text = `胜率 ${formatNumber(calibration.win_rate, 1)}% · 5日 ${formatNumber(calibration.avg_forward_5d_return)}% · 最大不利 ${formatNumber(calibration.max_adverse_return)}%`;
  return `<small>${escapeHtml(text)}</small>`;
}

function factorPercentileLine(item) {
  if (item.percentile === null || item.percentile === undefined) return "";
  return `<em>${escapeHtml(`历史分位 ${formatNumber(item.percentile, 1)}%`)}</em>`;
}

function factorBucketLine(bucket) {
  bucket = asObject(bucket);
  if (!Object.keys(bucket).length) return "";
  return `<em>${escapeHtml(bucket.name)}：${escapeHtml(bucket.sample_count)}样本 / 5日 ${formatNumber(bucket.avg_forward_5d_return)}%</em>`;
}

export function renderChipAnalysis(chip) {
  const el = $("chipPanel");
  if (!el || !chip) {
    if (el) el.innerHTML = "";
    return;
  }
  if (chip.distribution_available !== true) {
    const validSessionCount = Number.isInteger(chip.valid_session_count) && chip.valid_session_count >= 0
      ? chip.valid_session_count
      : 0;
    el.innerHTML = `
      <div class="chip-head">
        <strong>筹码证据不可用</strong>
        <span>有效样本 ${escapeHtml(validSessionCount)}</span>
      </div>
      <p>${escapeHtml(chip.summary || "有效日K样本不足，暂不能形成筹码分布估算。")}</p>
      ${renderInlineItems(chip.notes, "small", 2)}
    `;
    return;
  }
  el.innerHTML = `
    <div class="chip-head">
      <strong>${escapeHtml(chip.distribution_label)} · ${escapeHtml(chip.concentration)}</strong>
      <span>成本中枢 ${formatNumber(chip.center_price)}</span>
    </div>
    <p>${escapeHtml(chip.summary)}</p>
    <div class="band-grid">
      <div>
        <strong>支撑区</strong>
        ${renderChipBands(chip.support_bands)}
      </div>
      <div>
        <strong>压力区</strong>
        ${renderChipBands(chip.pressure_bands)}
      </div>
    </div>
    ${renderInlineItems(chip.notes, "small", 2)}
  `;
}

function renderChipBands(items) {
  return asArray(items).length
    ? renderLimitedItems(
        items,
        3,
        (item) => `<span><b>${formatNumber(item.low)} - ${formatNumber(item.high)}</b><small>${formatNumber(item.share, 1)}% · ${escapeHtml(item.note)}</small></span>`
      )
    : `<span><b>暂无</b><small>当前价格附近缺少明显成交密集区。</small></span>`;
}

export function renderLeadership(report) {
  const el = $("leadershipPanel");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="leader-head">
      <strong>${escapeHtml(report.score)} · ${escapeHtml(report.level)}</strong>
      <span>${escapeHtml(report.summary)}</span>
    </div>
    <div class="feature-tags">${renderInlineItems(report.tags, "i")}</div>
    ${renderInlineItems(report.evidence, "p", 4)}
    ${renderMissingData(report.missing_data)}
  `;
}
