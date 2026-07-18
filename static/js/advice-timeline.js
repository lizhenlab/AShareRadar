import { $, escapeHtml } from "./dom.js";

const COMPARISON_STATUSES = new Set(["comparable", "no_previous", "legacy", "version_changed"]);
const CATEGORY_LABELS = Object.freeze({
  action: "动作",
  advice: "建议",
  trend: "趋势",
  risk: "风险",
  score: "评分",
  scores: "评分",
  price: "支撑 / 压力",
  price_level: "支撑 / 压力",
  price_levels: "支撑 / 压力",
  support_resistance: "支撑 / 压力",
  data_quality: "数据质量",
  quality: "数据质量",
});
const FIELD_LABELS = Object.freeze({
  action: "建议动作",
  confidence: "建议强度",
  trend_score: "趋势评分",
  trend_label: "趋势状态",
  risk_level: "风险等级",
  support: "支撑位",
  resistance: "压力位",
  data_quality_score: "质量评分",
  data_quality_level: "质量等级",
  data_quality_source: "质量来源",
});
const SCORE_FIELDS = new Set(["confidence", "trend_score", "data_quality_score"]);

export function renderAdviceTimeline(items, target = $("adviceTimeline")) {
  const view = buildTimelineView(items);
  if (target) {
    target.innerHTML = view.html;
    target.setAttribute?.("aria-busy", "false");
  }
  return view.complete;
}

export function renderAdviceTimelineLoading(symbol, target = $("adviceTimeline")) {
  const stock = cleanScalarText(symbol, "当前股票", 40);
  if (target) {
    target.innerHTML = stateHtml("正在读取核心分析建议变化", `${stock} 的保留快照比较正在加载。`, "is-loading");
    target.setAttribute?.("aria-busy", "true");
  }
}

export function renderAdviceTimelineUnavailable(error, target = $("adviceTimeline")) {
  const detail = cleanScalarText(error && error.message ? error.message : error, "请稍后重试。", 180);
  if (target) {
    target.innerHTML = unavailableHtml(detail);
    target.setAttribute?.("aria-busy", "false");
  }
  return false;
}

export function formatAdviceTime(item) {
  if (!isPlainObject(item)) return "--";
  const createdAt = cleanScalarText(item.created_at, "", 80);
  const updatedAt = cleanScalarText(item.updated_at, "", 80);
  if (!createdAt && !updatedAt) return "--";
  if (!createdAt || !updatedAt || createdAt === updatedAt) return createdAt || updatedAt;
  return `${createdAt} 至 ${updatedAt}`;
}

function buildTimelineView(items) {
  if (!Array.isArray(items)) {
    return {
      complete: false,
      html: unavailableHtml("返回的保留快照格式无法识别。"),
    };
  }
  if (!items.length) {
    return {
      complete: true,
      html: stateHtml("暂无核心分析建议变化", "保留新的个股分析快照后，这里会显示与上一条留痕的比较。"),
    };
  }

  const normalized = items.map(normalizeTimelineItem).filter(Boolean);
  const skippedCount = items.length - normalized.length;
  if (!normalized.length) {
    return {
      complete: false,
      html: unavailableHtml("保留快照内容无法安全展示。"),
    };
  }

  const skippedHtml = skippedCount
    ? stateHtml("部分留痕不可用", `已跳过 ${skippedCount} 条格式异常的保留快照。`, "timeline-data-note")
    : "";
  return {
    complete: skippedCount === 0,
    html: normalized.map(renderTimelineItem).join("") + skippedHtml,
  };
}

function normalizeTimelineItem(item) {
  if (!isPlainObject(item) || !hasTimelineFields(item)) return null;
  const rawStatus = typeof item.comparison_status === "string" ? item.comparison_status.trim() : "";
  return {
    item,
    comparisonStatus: COMPARISON_STATUSES.has(rawStatus) ? rawStatus : "legacy",
    changes: normalizeChanges(item.changes),
  };
}

function hasTimelineFields(item) {
  return [
    "id",
    "action",
    "confidence",
    "trend_score",
    "risk_level",
    "created_at",
    "market_time",
    "comparison_status",
    "changes",
  ].some((field) => Object.prototype.hasOwnProperty.call(item, field));
}

function normalizeChanges(changes) {
  if (!Array.isArray(changes)) return [];
  return changes.filter((change) => {
    if (!isPlainObject(change)) return false;
    return ["category", "field", "before", "after"].some((field) =>
      Object.prototype.hasOwnProperty.call(change, field)
    );
  });
}

function renderTimelineItem(entry) {
  const { item } = entry;
  const action = cleanScalarText(item.action, "建议待确认", 80);
  const strength = scoreText(item.confidence);
  const marketTime = cleanScalarText(item.market_time, "--", 80);
  const recordTime = formatAdviceTime(item);
  const repeatCount = positiveInteger(item.repeat_count);
  const repeatText = repeatCount > 1 ? ` · 归并 ${repeatCount} 次记录` : "";
  const trend = metricText(item.trend_label, item.trend_score);
  const risk = cleanScalarText(item.risk_level, "--", 80);
  const quality = metricText(item.data_quality_level, item.data_quality_score);
  const qualitySource = cleanScalarText(item.data_quality_source, "", 120);
  const basis = firstText([item.reason, item.summary], 500);
  const provenance = provenanceText(item);

  return `
    <article class="timeline-item advice-timeline-item" role="listitem">
      <div class="advice-timeline-heading">
        <strong>${escapeHtml(action)} · 建议强度 ${escapeHtml(strength)}/100</strong>
        <p class="advice-time-line">市场时间 ${escapeHtml(marketTime)} · 记录时间 ${escapeHtml(recordTime)}${repeatText}</p>
      </div>
      <dl class="advice-current-facts" aria-label="本条建议摘要">
        <div><dt>趋势</dt><dd>${escapeHtml(trend)}</dd></div>
        <div><dt>风险</dt><dd>${escapeHtml(risk)}</dd></div>
        <div><dt>质量</dt><dd>${escapeHtml(quality)}${qualitySource ? ` · 来源 ${escapeHtml(qualitySource)}` : ""}</dd></div>
      </dl>
      ${basis ? `<p class="advice-basis"><b>结论依据</b>${escapeHtml(basis)}</p>` : ""}
      ${renderComparison(entry)}
      ${provenance ? `<p class="advice-provenance">${escapeHtml(provenance)}</p>` : ""}
    </article>`;
}

function renderComparison(entry) {
  const { item, comparisonStatus, changes } = entry;
  if (comparisonStatus === "no_previous") {
    return comparisonNote("无更早可比留痕");
  }
  if (comparisonStatus === "legacy") {
    return comparisonNote("快照版本未知，无法与上一条保留快照直接横比。");
  }
  if (comparisonStatus === "version_changed") {
    const details = changes.length ? renderChangeDetails(changes, `查看 ${changes.length} 项记录差异`, true) : "";
    return `${comparisonNote("分析版本已变化，记录差异仅作中性展示，不作方向判断。", "is-version-changed")}${details}`;
  }

  const declaresChanges = item.has_changes === true || (item.has_changes !== false && changes.length > 0);
  if (!declaresChanges) return comparisonNote("结论延续", "is-continuation");
  if (!changes.length) return comparisonNote("变化明细暂不可用", "is-unavailable");
  return renderChangeDetails(changes, `自上次保留快照以来 ${changes.length} 项变化`, false);
}

function renderChangeDetails(changes, summary, neutral) {
  return `
    <details class="advice-change-details">
      <summary>${escapeHtml(summary)}</summary>
      <ul class="advice-change-list">
        ${changes.map((change) => renderChange(change, neutral)).join("")}
      </ul>
    </details>`;
}

function renderChange(change, neutral) {
  const rawCategory = cleanScalarText(change.category, "其他", 80);
  const rawField = cleanScalarText(change.field, "字段", 80);
  const category = CATEGORY_LABELS[rawCategory.toLowerCase()] || rawCategory;
  const field = FIELD_LABELS[rawField.toLowerCase()] || rawField;
  const label = category === field ? category : `${category} · ${field}`;
  const before = formatChangeValue(change.before, rawField);
  const after = formatChangeValue(change.after, rawField);
  const caveat = !neutral && change.comparable === false ? `<small>口径不同，仅展示</small>` : "";
  return `
    <li class="advice-change-row">
      <span class="advice-change-name">${escapeHtml(label)}${caveat}</span>
      <span class="advice-change-values">
        <span><b>前</b>${escapeHtml(before)}</span>
        <i aria-hidden="true">&rarr;</i>
        <span><b>后</b>${escapeHtml(after)}</span>
      </span>
    </li>`;
}

function comparisonNote(text, extraClass = "") {
  return `<p class="advice-comparison ${extraClass}">${escapeHtml(text)}</p>`;
}

function stateHtml(title, detail, extraClass = "") {
  return `
    <div class="timeline-item timeline-state ${extraClass}" role="listitem">
      <strong>${escapeHtml(title)}</strong>
      <p>${escapeHtml(detail)}</p>
    </div>`;
}

function unavailableHtml(detail) {
  return stateHtml("核心分析建议变化暂不可用", detail, "is-unavailable");
}

function provenanceText(item) {
  const fields = [
    ["快照口径", item.snapshot_contract_version],
    ["结论口径", item.conclusion_basis],
    ["规则版本", item.rule_version],
    ["模型版本", item.model_version],
  ];
  return fields
    .map(([label, value]) => {
      const text = cleanScalarText(value, "", 120);
      return text ? `${label} ${text}` : "";
    })
    .filter(Boolean)
    .join(" · ");
}

function metricText(labelValue, scoreValue) {
  const label = cleanScalarText(labelValue, "", 80);
  const score = scoreText(scoreValue);
  if (label && score !== "--") return `${label} · ${score}/100`;
  if (label) return label;
  return score === "--" ? "--" : `${score}/100`;
}

function scoreText(value) {
  const number = finiteNumber(value);
  if (number === null || number < 0 || number > 100) return "--";
  return formatFiniteNumber(number);
}

function formatChangeValue(value, field) {
  if (value === null || value === undefined) return "未记录";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return "不可用";
    if (SCORE_FIELDS.has(String(field).toLowerCase())) {
      return value >= 0 && value <= 100 ? `${formatFiniteNumber(value)}/100` : "不可用";
    }
    return formatFiniteNumber(value);
  }
  return cleanScalarText(value, "不可用", 240);
}

function firstText(values, maxLength) {
  for (const value of values) {
    const text = cleanScalarText(value, "", maxLength);
    if (text) return text;
  }
  return "";
}

function cleanScalarText(value, fallback, maxLength) {
  if (typeof value === "string") {
    const text = value.trim();
    return text ? text.slice(0, maxLength) : fallback;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return formatFiniteNumber(value).slice(0, maxLength);
  }
  return fallback;
}

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function positiveInteger(value) {
  const number = finiteNumber(value);
  return number !== null && Number.isInteger(number) && number > 0 && number <= 100000 ? number : 1;
}

function formatFiniteNumber(value) {
  if (!Number.isFinite(value)) return "--";
  if (Object.is(value, -0)) return "0";
  if (Number.isInteger(value)) return String(value);
  const rounded = Math.round(value * 100) / 100;
  return Number.isFinite(rounded) ? String(rounded) : String(value);
}

function isPlainObject(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
