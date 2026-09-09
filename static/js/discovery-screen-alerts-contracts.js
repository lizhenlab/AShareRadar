import { auditTimestampEpoch } from "./audit-time.js";
import { validateMarketScanScreenAlert } from "./market-scan-screening-contracts.js";
import { validateUiSymbol } from "./symbols.js";

export const SCREEN_ALERT_HISTORY_SIZE = 20;
export const SCREEN_ALERT_DETAIL_SIZE = 50;
export const SCREEN_ALERT_KINDS = Object.freeze(["all", "entered", "exited", "unrankable"]);
const COUNTS = Object.freeze({ entered: "entered_count", exited: "exited_count", unrankable: "suppressed_unrankable_count" });
const SUMMARY_FIELDS = Object.freeze([
  "id", "preset_id", "preset_revision", "current_run_id", "previous_run_id", "event_digest", "created_at",
  ...Object.values(COUNTS),
]);

export function validateScreenAlertSummary(value, presetId) {
  exactFields(value, SUMMARY_FIELDS, "变化记录");
  for (const key of ["id", "preset_id", "preset_revision", "current_run_id", "previous_run_id"]) positive(value[key], key);
  if (value.preset_id !== presetId || value.current_run_id === value.previous_run_id) invalid("变化记录来源不一致");
  for (const key of Object.values(COUNTS)) count(value[key], key);
  digest(value.event_digest);
  if (typeof value.created_at !== "string" || !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(value.created_at)
    || auditTimestampEpoch(value.created_at) === null) invalid("变化记录时间无效");
  return { ...value };
}

export function validateScreenAlertHistory(value, presetId, page) {
  exactFields(value, ["preset_id", "items", "total", "page", "page_size", "page_count"], "变化历史页");
  if (value.preset_id !== presetId) invalid("变化历史与所选方案不一致");
  validatePage(value, page, SCREEN_ALERT_HISTORY_SIZE);
  const items = value.items.map(item => validateScreenAlertSummary(item, presetId));
  if (items.some((item, index) => index > 0 && item.id >= items[index - 1].id)) invalid("变化记录顺序或身份重复");
  return { ...value, items };
}

export function validateScreenAlertDetail(value, expected, page, kind) {
  exactFields(value, ["event", "items", "total", "page", "page_size", "page_count", "kind"], "变化明细页");
  const event = validateScreenAlertSummary(value.event, expected.preset_id);
  if (SUMMARY_FIELDS.some(field => event[field] !== expected[field])) invalid("变化明细与所选记录身份不一致");
  if (!SCREEN_ALERT_KINDS.includes(kind) || value.kind !== kind) invalid("变化分类与请求不一致");
  validatePage(value, page, SCREEN_ALERT_DETAIL_SIZE);
  if (value.total !== kindCount(event, kind)) invalid("变化明细数量与记录不一致");
  const items = validateDetailItems(value.items, event, page, kind);
  return { ...value, event, items, source: "history" };
}

export function screenAlertAcknowledgement(value, preset) {
  positive(value?.current?.run_id, "当前批次");
  validateMarketScanScreenAlert(value, preset.id, value.current.run_id);
  if (value.preset.preset_id !== preset.id || value.preset.preset_revision !== preset.revision) invalid("记录回执与所选方案修订不一致");
  if (value.status !== "ready") return null;
  positive(value.previous?.run_id, "前批次");
  if (value.previous.run_id === value.current.run_id) invalid("记录回执前后批次不能相同");
  const items = acknowledgementItems(value);
  const event = {
    id: null, preset_id: preset.id, preset_revision: preset.revision,
    current_run_id: value.current.run_id, previous_run_id: value.previous.run_id,
    event_digest: value.event_digest, created_at: null,
    entered_count: value.entered_symbols.length, exited_count: value.exited_symbols.length,
    suppressed_unrankable_count: value.suppressed_unrankable_symbols.length,
  };
  return { event, items };
}

export function acknowledgedDetailPage(ack, page = 1, kind = "all") {
  if (!SCREEN_ALERT_KINDS.includes(kind)) invalid("变化分类无效");
  positive(page, "明细页码");
  const items = ack.items.filter(item => kind === "all" || item.change === kind);
  const size = SCREEN_ALERT_DETAIL_SIZE;
  return { event: ack.event, items: items.slice((page - 1) * size, page * size), total: items.length,
    page, page_size: size, page_count: Math.ceil(items.length / size), kind, source: "ack" };
}

function acknowledgementItems(value) {
  const fields = { entered: "entered_symbols", exited: "exited_symbols", unrankable: "suppressed_unrankable_symbols" };
  const items = Object.entries(fields).flatMap(([change, field]) => (
    value[field].map(symbol => ({ symbol: canonicalSymbol(symbol), change })).sort((a, b) => a.symbol.localeCompare(b.symbol))
  ));
  if (new Set(items.map(item => item.symbol)).size !== items.length) invalid("记录回执包含重复股票");
  return items;
}

function validateDetailItems(values, event, page, kind) {
  const offset = (page - 1) * SCREEN_ALERT_DETAIL_SIZE;
  const items = values.map((item, index) => {
    exactFields(item, ["symbol", "change"], "变化股票");
    const change = kind === "all" ? changeAt(event, offset + index) : kind;
    if (item.change !== change) invalid("变化股票分类或顺序不一致");
    return { symbol: canonicalSymbol(item.symbol), change };
  });
  if (new Set(items.map(item => item.symbol)).size !== items.length) invalid("变化明细包含重复股票");
  if (items.some((item, index) => index && item.change === items[index - 1].change
    && item.symbol <= items[index - 1].symbol)) invalid("变化股票顺序无效");
  return items;
}

function changeAt(event, position) {
  if (position < event.entered_count) return "entered";
  if (position < event.entered_count + event.exited_count) return "exited";
  return "unrankable";
}

function kindCount(event, kind) {
  return kind === "all" ? Object.values(COUNTS).reduce((sum, key) => sum + event[key], 0) : event[COUNTS[kind]];
}

function validatePage(value, page, size) {
  positive(page, "请求页码");
  count(value.total, "总数");
  if (!Array.isArray(value.items) || value.page !== page || value.page_size !== size
    || value.page_count !== Math.ceil(value.total / size)
    || value.items.length !== Math.max(0, Math.min(size, value.total - (page - 1) * size))) invalid("变化分页数据不完整或页码不一致");
}

function canonicalSymbol(value) {
  if (typeof value !== "string" || !/^\d{6}\.(SH|SZ|BJ)$/.test(value) || validateUiSymbol(value) !== value) invalid("变化股票代码无效");
  return value;
}

function exactFields(value, fields, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).length !== fields.length || fields.some(field => !Object.hasOwn(value, field))) invalid(`${label}格式无效`);
}

function positive(value, label) {
  if (!Number.isSafeInteger(value) || value < 1) invalid(`${label}必须是正整数`);
}

function count(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) invalid(`${label}必须是非负整数`);
}

function digest(value) {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) invalid("变化记录摘要无效");
}

function invalid(message) {
  throw new TypeError(message);
}
