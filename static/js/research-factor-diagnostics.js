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
    ["量能", `${formatNumber(feature.volume_ratio)}倍`],
    ["估值", fallbackText(feature.valuation_score)],
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
      <i>${escapeHtml(diagnosis.action)} · 诊断证据充分度 ${escapeHtml(diagnosis.confidence)}/100</i>
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
  el.innerHTML = `
    ${renderFactorLabHead(report)}
    <div class="factor-lab-metrics">${renderFactorLabMetrics(report)}</div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="factor-lab-grid">${renderFactorLabItems(report.factors)}</div>
    ${renderInlineItems(report.weight_policy, "em", 2)}
    ${renderFactorParticipationNote(report.factors)}
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

function renderFactorLabItems(items) {
  return asArray(items).map(renderStandardFactor).join("");
}

function renderFactorParticipationNote(items) {
  const excludedNames = [
    ...new Set(
      asArray(items)
        .filter((item) => asObject(asObject(item).calibration).participates_in_historical_aggregate === false)
        .map((item) => asObject(item).name)
        .filter((name) => typeof name === "string" && name.trim())
        .map((name) => name.trim()),
    ),
  ];
  if (!excludedNames.length) return "";
  return `<small>${escapeHtml(`历史聚合口径：${excludedNames.join("、")}仍参与当前评分，但不参与综合证据充分度、正负证据与历史样本聚合。`)}</small>`;
}

function renderStandardFactor(item) {
  item = asObject(item);
  const calibration = asObject(item.calibration);
  const bucket = asArray(item.calibration_buckets)[0];
  return `
    <div class="standard-factor ${factorDirectionClass(item)}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.score)} · 权重 ${formatNumber(item.weight, 2)}</span>
      </div>
      <div class="score-bar"><i style="width:${Math.max(0, Math.min(100, Number(item.score) || 0))}%"></i></div>
      <p>${escapeHtml(item.value)}</p>
      <small>${factorCalibrationSampleText(calibration)}</small>
      ${factorCalibrationReturnLine(calibration)}
      ${factorPercentileLine(item)}
      ${factorBucketLine(bucket)}
      ${renderInlineItems(item.evidence, "small", 1)}
    </div>
  `;
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
