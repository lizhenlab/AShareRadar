import { escapeHtml } from "./dom.js";

const SOURCE_ORDER = Object.freeze(["advice", "alert", "note"]);
const SOURCE_FIELDS = Object.freeze({
  advice: "adviceItems",
  alert: "alertEvents",
  note: "notes",
});
const SOURCE_LABELS = Object.freeze({
  advice: "\u5efa\u8bae",
  alert: "\u63d0\u9192",
  note: "\u7b14\u8bb0",
});
const VALID_PHASES = new Set(["ready", "loading", "unavailable"]);
const VALID_TONES = new Set(["neutral", "good", "warn", "risk"]);
const MAX_LENGTH = Object.freeze({
  id: 80,
  occurredAt: 40,
  title: 120,
  summary: 500,
  meta: 240,
  shortText: 80,
  sourceMessage: 180,
});

export const ACTIVITY_FILTERS = Object.freeze([
  Object.freeze({ value: "all", label: "\u5168\u90e8" }),
  Object.freeze({ value: "advice", label: SOURCE_LABELS.advice }),
  Object.freeze({ value: "alert", label: SOURCE_LABELS.alert }),
  Object.freeze({ value: "note", label: SOURCE_LABELS.note }),
]);

export function mergeResearchActivity(options = {}) {
  const payload = isPlainObject(options) ? options : {};
  const limit = normalizeLimit(payload.limit === undefined ? 30 : payload.limit);
  const skippedBySource = { advice: 0, alert: 0, note: 0 };
  const merged = [];
  let inputOrder = 0;

  for (const kind of SOURCE_ORDER) {
    const records = payload[SOURCE_FIELDS[kind]];
    if (!Array.isArray(records)) {
      skippedBySource[kind] += 1;
      continue;
    }

    for (let index = 0; index < records.length; index += 1) {
      let item = null;
      try {
        if (Object.prototype.hasOwnProperty.call(records, index)) {
          item = normalizeSourceItem(kind, records[index]);
        }
      } catch {
        item = null;
      }
      if (item) merged.push({ item, inputOrder });
      else skippedBySource[kind] += 1;
      inputOrder += 1;
    }
  }

  merged.sort((left, right) => right.item.timestamp - left.item.timestamp || left.inputOrder - right.inputOrder);
  return {
    items: merged.slice(0, limit).map((entry) => entry.item),
    skippedBySource,
  };
}

export function renderResearchActivity(options = {}, target) {
  const payload = isPlainObject(options) ? options : {};
  const activeKind = filterValue(payload.activeKind);
  const sourceStates = normalizeSourceStates(payload.sourceStates);
  const skippedBySource = normalizeSkippedBySource(payload.skippedBySource);
  const rawItems = Array.isArray(payload.items) ? payload.items : [];
  const normalizedItems = [];
  let renderSkippedCount = Array.isArray(payload.items) ? 0 : 1;

  for (let index = 0; index < rawItems.length; index += 1) {
    let item = null;
    try {
      if (Object.prototype.hasOwnProperty.call(rawItems, index)) {
        item = normalizeRenderedItem(rawItems[index]);
      }
    } catch {
      item = null;
    }
    if (item) normalizedItems.push(item);
    else renderSkippedCount += 1;
  }

  const visibleItems = activeKind === "all"
    ? normalizedItems
    : normalizedItems.filter((item) => item.kind === activeKind);
  const html = buildActivityHtml({
    activeKind,
    normalizedItems,
    visibleItems,
    sourceStates,
    skippedBySource,
    renderSkippedCount,
  });

  if (target && typeof target === "object") {
    target.innerHTML = html;
    target.setAttribute?.("aria-busy", SOURCE_ORDER.some((kind) => sourceStates[kind].phase === "loading") ? "true" : "false");
  }
  return {
    html,
    activeKind,
    visibleCount: visibleItems.length,
    totalCount: normalizedItems.length,
  };
}

function normalizeSourceItem(kind, record) {
  if (!isPlainObject(record)) return null;
  if (kind === "advice") return normalizeAdvice(record);
  if (kind === "alert") return normalizeAlert(record);
  return normalizeNote(record);
}

function normalizeAdvice(record) {
  const id = requiredId(record.id);
  const time = requiredTime(record.created_at);
  const action = optionalText(record, "action", MAX_LENGTH.shortText) || "\u5efa\u8bae\u5f85\u786e\u8ba4";
  validateOptionalNumbers(record, [
    "confidence",
    "trend_score",
    "price",
    "change_pct",
    "support",
    "resistance",
    "data_quality_score",
  ]);

  const reason = optionalText(record, "reason", MAX_LENGTH.summary);
  const summary = optionalText(record, "summary", MAX_LENGTH.summary);
  const risk = optionalText(record, "risk_level", MAX_LENGTH.shortText);
  const trendLabel = optionalText(record, "trend_label", MAX_LENGTH.shortText);
  optionalText(record, "comparison_status", MAX_LENGTH.shortText);

  let hasChanges = false;
  if (hasOwn(record, "has_changes")) {
    if (typeof record.has_changes !== "boolean") throw new TypeError("invalid has_changes");
    hasChanges = record.has_changes;
  }
  if (hasOwn(record, "changes")) {
    if (!Array.isArray(record.changes)) throw new TypeError("invalid changes");
    hasChanges = hasChanges || record.changes.length > 0;
  }

  const repeatCount = optionalPositiveInteger(record, "repeat_count", 1);
  const trend = trendLabel || (finiteOwnNumber(record, "trend_score") === null
    ? ""
    : `${formatFiniteNumber(record.trend_score)}/100`);
  const meta = [];
  if (risk) meta.push(`\u98ce\u9669 ${risk}`);
  if (trend) meta.push(`\u8d8b\u52bf ${trend}`);
  if (repeatCount > 1) meta.push(`\u5f52\u5e76 ${repeatCount} \u6b21`);

  return activityItem({
    id: `advice:${id}`,
    kind: "advice",
    time,
    title: `${action} \u00b7 ${hasChanges ? "\u7ed3\u8bba\u53d8\u5316" : "\u7ed3\u8bba\u5ef6\u7eed"}`,
    summary: reason || summary || "\u672a\u8bb0\u5f55\u5efa\u8bae\u6458\u8981",
    meta: meta.join(" \u00b7 ") || "\u5efa\u8bae\u8bb0\u5f55",
    tone: adviceTone(action, risk),
  });
}

function normalizeAlert(record) {
  const id = requiredId(record.id);
  const time = requiredTime(record.created_at);
  const name = requiredText(record.name, MAX_LENGTH.shortText);
  const eventType = requiredText(record.event_type, MAX_LENGTH.shortText);
  const message = requiredText(record.message, MAX_LENGTH.summary);
  const price = requiredFiniteNumber(record.price);
  const threshold = requiredFiniteNumber(record.threshold);
  if (hasOwn(record, "rule_id") && record.rule_id !== null) requiredId(record.rule_id);
  validateOptionalNumbers(record, ["change_pct"]);

  return activityItem({
    id: `alert:${id}`,
    kind: "alert",
    time,
    title: `${name} \u00b7 ${eventType}`,
    summary: message,
    meta: `\u4ef7\u683c ${formatFiniteNumber(price)} \u00b7 \u9608\u503c ${formatFiniteNumber(threshold)}`,
    tone: alertTone(eventType),
  });
}

function normalizeNote(record) {
  const id = requiredId(record.id);
  const noteType = requiredText(record.note_type, MAX_LENGTH.shortText);
  const content = requiredText(record.content, MAX_LENGTH.summary);
  const updatedAt = optionalTime(record, "updated_at");
  const time = updatedAt || requiredTime(record.created_at);
  const price = finiteOwnNumber(record, "price");
  const tradeDate = optionalText(record, "trade_date", MAX_LENGTH.shortText);
  const meta = [updatedAt ? "\u65f6\u95f4\u53e3\u5f84 \u66f4\u65b0\u65f6\u95f4" : "\u65f6\u95f4\u53e3\u5f84 \u521b\u5efa\u65f6\u95f4"];
  if (price !== null) meta.push(`\u4ef7\u683c ${formatFiniteNumber(price)}`);
  if (tradeDate) meta.push(`\u4ea4\u6613\u65e5 ${tradeDate}`);

  return activityItem({
    id: `note:${id}`,
    kind: "note",
    time,
    title: noteType,
    summary: content,
    meta: meta.join(" \u00b7 "),
    tone: "neutral",
  });
}

function activityItem({ id, kind, time, title, summary, meta, tone }) {
  return {
    id: clipText(id, MAX_LENGTH.id),
    kind,
    occurredAt: time.occurredAt,
    timestamp: time.timestamp,
    title: clipText(title, MAX_LENGTH.title),
    summary: clipText(summary, MAX_LENGTH.summary),
    meta: clipText(meta, MAX_LENGTH.meta),
    tone,
  };
}

function normalizeRenderedItem(record) {
  if (!isPlainObject(record) || !SOURCE_ORDER.includes(record.kind)) return null;
  const id = scalarText(record.id, MAX_LENGTH.id);
  const occurredAt = requiredText(record.occurredAt, MAX_LENGTH.occurredAt);
  const timestamp = requiredFiniteNumber(record.timestamp);
  const title = requiredText(record.title, MAX_LENGTH.title);
  const summary = requiredText(record.summary, MAX_LENGTH.summary);
  const meta = requiredText(record.meta, MAX_LENGTH.meta);
  if (!id || !VALID_TONES.has(record.tone)) return null;
  requiredTime(occurredAt);
  return { id, kind: record.kind, occurredAt, timestamp, title, summary, meta, tone: record.tone };
}

function buildActivityHtml(context) {
  const unavailable = SOURCE_ORDER.filter((kind) => context.sourceStates[kind].phase === "unavailable");
  const loading = SOURCE_ORDER.filter((kind) => context.sourceStates[kind].phase === "loading");
  const notices = [];

  if (unavailable.length) {
    const allUnavailable = unavailable.length === SOURCE_ORDER.length;
    notices.push(stateHtml(
      allUnavailable ? "\u5168\u90e8\u672c\u5730\u8bb0\u5f55\u6682\u4e0d\u53ef\u7528" : "\u90e8\u5206\u672c\u5730\u8bb0\u5f55\u6682\u4e0d\u53ef\u7528",
      sourceStateDetail(unavailable, context.sourceStates),
      "is-unavailable"
    ));
  }
  if (loading.length) {
    const allLoading = loading.length === SOURCE_ORDER.length;
    notices.push(stateHtml(
      allLoading ? "\u672c\u5730\u7814\u7a76\u8bb0\u5f55\u52a0\u8f7d\u4e2d" : "\u90e8\u5206\u672c\u5730\u8bb0\u5f55\u6b63\u5728\u52a0\u8f7d",
      sourceStateDetail(loading, context.sourceStates),
      "is-loading"
    ));
  }

  const skippedDetails = skippedDetail(context.skippedBySource, context.renderSkippedCount);
  if (skippedDetails) {
    notices.push(stateHtml(
      context.normalizedItems.length ? "\u90e8\u5206\u672c\u5730\u8bb0\u5f55\u683c\u5f0f\u5f02\u5e38" : "\u672c\u5730\u7814\u7a76\u8bb0\u5f55\u65e0\u6cd5\u5b89\u5168\u5c55\u793a",
      skippedDetails,
      "is-unavailable"
    ));
  }

  let body = "";
  if (context.visibleItems.length) {
    body = `<div class="research-activity-list" role="list" aria-label="\u672c\u5730\u7814\u7a76\u6d3b\u52a8">${context.visibleItems.map(renderActivityItem).join("")}</div>`;
  } else if (!unavailable.length && !loading.length && !skippedDetails) {
    body = context.normalizedItems.length
      ? stateHtml("\u5f53\u524d\u7c7b\u522b\u6682\u65e0\u672c\u5730\u7814\u7a76\u6d3b\u52a8", "\u5176\u4ed6\u7c7b\u522b\u4ecd\u6709\u672c\u5730\u8bb0\u5f55\u3002", "is-empty")
      : stateHtml("\u6682\u65e0\u672c\u5730\u7814\u7a76\u6d3b\u52a8", "\u5c1a\u65e0\u672c\u5730\u5efa\u8bae\u3001\u63d0\u9192\u6216\u7b14\u8bb0\u8bb0\u5f55\u3002", "is-empty");
  }

  return `<div class="research-activity-view" data-active-kind="${context.activeKind}">${notices.join("")}${body}</div>`;
}

function renderActivityItem(item) {
  return `
    <article class="research-activity-item tone-${item.tone}" role="listitem" data-kind="${item.kind}" data-activity-id="${escapeHtml(item.id)}">
      <div class="research-activity-heading">
        <span class="research-activity-kind">${SOURCE_LABELS[item.kind]}</span>
        <time datetime="${escapeHtml(item.occurredAt)}">${escapeHtml(item.occurredAt)}</time>
      </div>
      <h3 class="research-activity-title">${escapeHtml(item.title)}</h3>
      <p class="research-activity-summary">${escapeHtml(item.summary)}</p>
      <p class="research-activity-meta"><span>\u8865\u5145</span>${escapeHtml(item.meta)}</p>
    </article>`;
}

function stateHtml(title, detail, className) {
  return `
    <div class="research-activity-state ${className}" role="status">
      <strong>${escapeHtml(title)}</strong>
      <p>${escapeHtml(detail)}</p>
    </div>`;
}

function sourceStateDetail(kinds, sourceStates) {
  return kinds.map((kind) => {
    const message = sourceStates[kind].message;
    return `${SOURCE_LABELS[kind]}${message ? `\uff1a${message}` : ""}`;
  }).join("\uff1b");
}

function skippedDetail(skippedBySource, renderSkippedCount) {
  const parts = SOURCE_ORDER
    .filter((kind) => skippedBySource[kind] > 0)
    .map((kind) => `${SOURCE_LABELS[kind]} ${skippedBySource[kind]} \u6761`);
  if (renderSkippedCount > 0) parts.push(`\u6d3b\u52a8\u9879 ${renderSkippedCount} \u6761`);
  return parts.length ? `\u5df2\u8df3\u8fc7\u683c\u5f0f\u5f02\u5e38\u8bb0\u5f55\uff1a${parts.join("\u3001")}` : "";
}

function normalizeSourceStates(value) {
  const states = {};
  const invalidContainer = value !== undefined && !isPlainObject(value);
  for (const kind of SOURCE_ORDER) {
    if (invalidContainer) {
      states[kind] = { phase: "unavailable", message: "\u6765\u6e90\u72b6\u6001\u683c\u5f0f\u5f02\u5e38" };
      continue;
    }
    const raw = value && hasOwn(value, kind) ? value[kind] : undefined;
    if (raw === undefined) {
      states[kind] = { phase: "ready", message: "" };
      continue;
    }
    if (!isPlainObject(raw) || !VALID_PHASES.has(raw.phase)) {
      states[kind] = { phase: "unavailable", message: "\u6765\u6e90\u72b6\u6001\u683c\u5f0f\u5f02\u5e38" };
      continue;
    }
    const message = typeof raw.message === "string"
      ? clipText(raw.message, MAX_LENGTH.sourceMessage)
      : "";
    states[kind] = { phase: raw.phase, message };
  }
  return states;
}

function normalizeSkippedBySource(value) {
  const skipped = { advice: 0, alert: 0, note: 0 };
  if (!isPlainObject(value)) return skipped;
  for (const kind of SOURCE_ORDER) {
    const count = value[kind];
    if (typeof count === "number" && Number.isSafeInteger(count) && count >= 0) skipped[kind] = count;
  }
  return skipped;
}

function filterValue(value) {
  return value === "all" || SOURCE_ORDER.includes(value) ? value : "all";
}

function normalizeLimit(value) {
  if (typeof value !== "number" || !Number.isInteger(value)) throw new TypeError("limit must be an integer");
  if (value < 1 || value > 100) throw new RangeError("limit must be between 1 and 100");
  return value;
}

function requiredId(value) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) throw new TypeError("invalid id");
  return String(value);
}

function requiredFiniteNumber(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new TypeError("invalid number");
  return value;
}

function validateOptionalNumbers(record, fields) {
  for (const field of fields) finiteOwnNumber(record, field);
}

function finiteOwnNumber(record, field) {
  if (!hasOwn(record, field) || record[field] === null || record[field] === undefined) return null;
  return requiredFiniteNumber(record[field]);
}

function optionalPositiveInteger(record, field, fallback) {
  if (!hasOwn(record, field) || record[field] === null || record[field] === undefined) return fallback;
  const value = record[field];
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1 || value > 1000000) {
    throw new TypeError(`invalid ${field}`);
  }
  return value;
}

function requiredText(value, maxLength) {
  if (typeof value !== "string") throw new TypeError("invalid text");
  const text = clipText(value, maxLength);
  if (!text) throw new TypeError("empty text");
  return text;
}

function optionalText(record, field, maxLength) {
  if (!hasOwn(record, field) || record[field] === null || record[field] === undefined) return "";
  if (typeof record[field] !== "string") throw new TypeError(`invalid ${field}`);
  return clipText(record[field], maxLength);
}

function scalarText(value, maxLength) {
  if (typeof value === "string") return clipText(value, maxLength);
  if (typeof value === "number" && Number.isFinite(value)) return clipText(formatFiniteNumber(value), maxLength);
  return "";
}

function clipText(value, maxLength) {
  return String(value).replace(/\s+/g, " ").trim().slice(0, maxLength);
}

function requiredTime(value) {
  if (typeof value !== "string") throw new TypeError("invalid time");
  const occurredAt = value.trim();
  if (!occurredAt || occurredAt.length > MAX_LENGTH.occurredAt) throw new TypeError("invalid time");
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(?:(Z)|([+-])(\d{2}):?(\d{2}))?)?$/.exec(occurredAt);
  if (!match) throw new TypeError("invalid time");
  const parts = timeParts(match);
  validateTimeParts(parts);
  const { year, month, day, hour, minute, second, millisecond } = parts;
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(hour, minute, second, millisecond);
  if (!dateMatchesParts(date, parts)) throw new TypeError("invalid time");
  const timestamp = date.getTime() - timeOffsetMilliseconds(match, parts);
  if (!Number.isFinite(timestamp)) throw new TypeError("invalid time");
  return { occurredAt, timestamp };
}

function timeParts(match) {
  return {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: numericMatchGroup(match, 4),
    minute: numericMatchGroup(match, 5),
    second: numericMatchGroup(match, 6),
    millisecond: Number(String(match[7] ?? "0").padEnd(3, "0").slice(0, 3)),
    offsetHour: numericMatchGroup(match, 10),
    offsetMinute: numericMatchGroup(match, 11),
  };
}

function numericMatchGroup(match, index) {
  const value = match[index];
  return value === undefined || value === "" ? 0 : Number(value);
}

function validateTimeParts(parts) {
  const { year, month, day, hour, minute, second, offsetHour, offsetMinute } = parts;
  if (year < 1000 || month < 1 || month > 12 || day < 1) throw new TypeError("invalid time");
  if (hour > 23 || minute > 59 || second > 59) throw new TypeError("invalid time");
  if (offsetHour > 23 || offsetMinute > 59) throw new TypeError("invalid time");
}

function dateMatchesParts(date, parts) {
  return date.getUTCFullYear() === parts.year
    && date.getUTCMonth() === parts.month - 1
    && date.getUTCDate() === parts.day
    && date.getUTCHours() === parts.hour
    && date.getUTCMinutes() === parts.minute
    && date.getUTCSeconds() === parts.second;
}

function timeOffsetMilliseconds(match, parts) {
  if (match[8]) return 0;
  const direction = match[9] === "-" ? -1 : 1;
  return direction * (parts.offsetHour * 60 + parts.offsetMinute) * 60000;
}

function optionalTime(record, field) {
  if (!hasOwn(record, field) || record[field] === null || record[field] === undefined) return null;
  if (typeof record[field] !== "string") throw new TypeError(`invalid ${field}`);
  return record[field].trim() ? requiredTime(record[field]) : null;
}

function formatFiniteNumber(value) {
  if (!Number.isFinite(value)) return "--";
  if (Object.is(value, -0)) return "0";
  if (Number.isInteger(value) || Math.abs(value) >= 1e21 || (Math.abs(value) > 0 && Math.abs(value) < 1e-6)) {
    return String(value);
  }
  return String(Number(value.toFixed(4)));
}

function adviceTone(action, risk) {
  if (["\u5356\u51fa", "\u51cf\u4ed3", "\u56de\u907f"].some((word) => action.includes(word))) return "risk";
  if (["\u9ad8\u98ce\u9669", "\u98ce\u9669\u8f83\u9ad8", "\u504f\u9ad8", "\u6781\u9ad8"].some((word) => risk.includes(word))) return "risk";
  if (["\u4e70\u5165", "\u589e\u6301", "\u79ef\u6781", "\u5173\u6ce8"].some((word) => action.includes(word))) return "good";
  return "neutral";
}

function alertTone(eventType) {
  return ["\u6062\u590d", "\u89e3\u9664", "\u56de\u843d"].some((word) => eventType.includes(word)) ? "good" : "warn";
}

function hasOwn(value, field) {
  return Object.prototype.hasOwnProperty.call(value, field);
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}
