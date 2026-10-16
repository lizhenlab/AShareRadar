import { clearAppliedScreenContext } from "./market-scan-screen-context.js";

const records = new WeakMap();
const STALE_MESSAGE = "自选成员已变化，请点击刷新自选范围；当前保留的是刷新前结果。";

function record(elements) {
  const key = elements.tableWrap || elements.table || elements;
  if (!records.has(key)) records.set(key, { scope: null, stale: false, generation: 0, reload: null, exportQuery: null });
  return records.get(key);
}

export function selectionRequest(elements) {
  if (elements.universe?.value !== "watchlist") return {};
  const state = record(elements);
  if (state.stale) throw new Error(STALE_MESSAGE);
  return { universe: "watchlist", ...(state.scope ? { watchlist_digest: state.scope.watchlist_digest } : {}) };
}

export function selectionGeneration(elements) { return record(elements).generation; }
export function selectionIsStale(elements) { return elements.universe?.value === "watchlist" && (record(elements).stale || !record(elements).scope); }

export function applySelectionParams(elements, params) {
  params.delete("universe"); params.delete("watchlist_digest");
  for (const [key, value] of Object.entries(selectionRequest(elements))) params.set(key, value);
  return params;
}

export function applySelectionQuery(elements, query) {
  if (!query) return query;
  const [path, search = ""] = query.split("?");
  return `${path}?${applySelectionParams(elements, new URLSearchParams(search))}`;
}

export function querySelection(query) {
  const params = new URLSearchParams(String(query || "").split("?")[1] || "");
  return { universe: params.get("universe") || "all", watchlist_digest: params.get("watchlist_digest") };
}

export function validateSelectionScope(value) {
  if (value == null) return null;
  if (value.universe !== "watchlist" || !/^[0-9a-f]{64}$/.test(value.watchlist_digest)
    || !Number.isSafeInteger(value.member_count) || value.member_count < 0
    || Object.keys(value).some(key => !["universe", "watchlist_digest", "member_count"].includes(key))) {
    throw new Error("榜单自选范围身份无效");
  }
  return { universe: "watchlist", watchlist_digest: value.watchlist_digest, member_count: value.member_count };
}

export function requireSelectionBinding(payload, expected = {}) {
  const scope = validateSelectionScope(payload.selection_scope);
  if ((expected.universe || "all") === "all") {
    if (scope) throw new Error("榜单范围与请求的全市场范围不一致");
  } else if (!scope || (expected.watchlist_digest && scope.watchlist_digest !== expected.watchlist_digest)) {
    throw new Error("榜单自选成员摘要与请求不一致");
  }
  if (scope && expected.member_count != null && scope.member_count !== expected.member_count) throw new Error("自选成员计数与已应用范围不一致");
  if (scope && (payload.total ?? payload.matched_count) > scope.member_count) throw new Error("榜单命中总数超过自选成员数");
  return scope;
}

export function acceptSelection(elements, payload, expected, exportQuery = null) {
  const scope = requireSelectionBinding(payload, expected);
  const state = record(elements);
  state.scope = scope; state.stale = false;
  state.exportQuery = exportQuery ? { query: bindSelectionQuery(exportQuery, scope), runId: payload.run?.id ?? payload.run_id } : null;
  renderSelection(elements);
  return scope;
}

export function bindSelectionQuery(query, scope) {
  if (!scope) return query;
  const [path, search = ""] = query.split("?");
  const params = new URLSearchParams(search);
  params.set("universe", "watchlist"); params.set("watchlist_digest", scope.watchlist_digest);
  return `${path}?${params}`;
}

export function selectionExportUrl(elements, runId, fallback) {
  const state = record(elements);
  const selected = elements.universe?.value === "watchlist";
  if (!selected && (state.scope || state.exportQuery?.runId !== runId
    || new URLSearchParams(state.exportQuery.query.split("?")[1]).get("ranking_basis") !== "frozen")) return fallback();
  selectionRequest(elements);
  if (selected && (!state.scope || state.exportQuery?.runId !== runId)) throw new Error("请先应用并确认当前自选范围，再导出");
  const [path, search] = state.exportQuery.query.split("?");
  const params = new URLSearchParams(search); params.delete("page"); params.delete("page_size");
  return `${path.replace(/\/results$/, "/export.xlsx")}?${params}`;
}

export function markSelectionConflict(elements, error) {
  if (elements.universe?.value !== "watchlist" || error?.status !== 409 || !String(error.message).includes("自选成员")) return false;
  markSelectionStale(elements);
  return true;
}

export function markSelectionStale(elements) {
  const state = record(elements);
  if (elements.universe?.value !== "watchlist") return;
  state.stale = true; state.generation += 1;
  clearAppliedScreenContext(elements.tableWrap);
  renderSelection(elements);
}

export function notifySelectionMembers(elements, payload = {}) {
  const key = items => [...new Set((items || []).map(item => item.symbol))].sort().join("|");
  if (key(payload.items) !== key(payload.previousItems)) markSelectionStale(elements);
}

export function setSelectionReload(elements, reload) { record(elements).reload = reload; }

export function resetSelection(elements) {
  const state = record(elements);
  state.scope = null; state.stale = false; state.exportQuery = null; state.generation += 1;
  clearAppliedScreenContext(elements.tableWrap);
  renderSelection(elements);
}

export function bindSelectionControls(elements, reload) {
  const refresh = () => {
    const action = record(elements).reload || reload;
    resetSelection(elements);
    void action();
  };
  elements.universe?.addEventListener?.("change", refresh);
  elements.scopeRefresh?.addEventListener?.("click", refresh);
  renderSelection(elements);
}

export function renderSelection(elements) {
  const state = record(elements), selected = elements.universe?.value === "watchlist";
  if (elements.scopeRefresh) elements.scopeRefresh.hidden = !selected;
  if (elements.scopeStatus) elements.scopeStatus.textContent = !selected ? "查看整个冻结批次。"
    : state.stale ? STALE_MESSAGE : state.scope
      ? `当前自选 ${state.scope.member_count} 只 · 成员摘要 ${state.scope.watchlist_digest.slice(0, 12)}；范围只作用本次查看，不保存进方案。增删自选后需刷新。`
      : "当前自选范围待确认；空自选显示 0 条。范围只作用本次查看，不保存进方案。";
  if (selected && (state.stale || !state.scope)) {
    for (const control of [elements.prev, elements.next]) if (control) control.disabled = true;
  }
  renderSelectionExport(elements, state, selected);
}

function renderSelectionExport(elements, state, selected) {
  const button = elements.scopeExportButton || elements.exportButton;
  if (!button) return;
  const blocked = selected && (state.stale || !state.scope || !state.exportQuery);
  if (blocked) {
    if (!state.exportBlocked) state.exportWasDisabled = Boolean(button.disabled);
    state.exportBlocked = true; button.disabled = true;
  } else if (state.exportBlocked) {
    button.disabled = Boolean(state.exportWasDisabled); state.exportBlocked = false;
  }
}
