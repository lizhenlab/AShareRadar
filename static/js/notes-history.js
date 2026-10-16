import { $ } from "./dom.js";
import { normalizeUiSymbol, validateUiSymbol } from "./symbols.js";

export const NOTE_PAGE_SIZE = 50;
const digestPattern = /^[0-9a-f]{64}$/;
const formatError = "笔记历史身份或分页数据不匹配，请刷新重试";

export function noteHistoryState(state, symbol = state.symbol) {
  const normalized = normalizeUiSymbol(symbol);
  if (state.notesHistory?.symbol !== normalized) {
    state.notesReadRequest?.abort();
    state.notesHistory = { symbol: normalized, active: false, page: 1, total: null, pageCount: 0,
      snapshot: null, pages: new Map(), busy: false, stale: false, message: "", recentCount: 0 };
  }
  return state.notesHistory;
}

export function resetNoteHistory(state, symbol = state.symbol) {
  renderNoteHistoryControls(noteHistoryState(state, symbol));
}

export function isNoteHistoryActive(state, symbol = state.symbol) {
  return noteHistoryState(state, symbol).active;
}

export function planNoteHistoryRead(state, action, symbol = state.symbol) {
  const history = noteHistoryState(state, symbol);
  const normalized = validateUiSymbol(symbol);
  if (!["open", "refresh", "previous", "next"].includes(action)) throw new Error("未知笔记历史操作");
  const turning = action === "previous" || action === "next";
  if (turning && (!history.active || history.stale || !history.snapshot || history.busy)) return null;
  const page = action === "open" ? 1 : history.page + (action === "previous" ? -1 : action === "next" ? 1 : 0);
  if (!Number.isSafeInteger(page) || page < 1 || turning && page > history.pageCount) return null;
  history.active = true;
  return { history, symbol: normalized, page, snapshot: turning ? history.snapshot : null };
}

export function noteHistoryUrl(plan) {
  if (!Number.isSafeInteger(plan.page) || plan.page < 1 || validateUiSymbol(plan.symbol) !== plan.symbol
      || plan.snapshot !== null && (typeof plan.snapshot !== "string" || !digestPattern.test(plan.snapshot))) throw new Error(formatError);
  const query = new URLSearchParams({ symbol: plan.symbol, page: String(plan.page), page_size: String(NOTE_PAGE_SIZE) });
  if (plan.snapshot) query.set("expected_snapshot", plan.snapshot);
  return `/api/stock/notes/page?${query}`;
}

export function validateNoteHistoryPage(payload, plan) {
  if (!validHistoryEnvelope(payload, plan)) throw new TypeError(formatError);
  const expected = Math.min(NOTE_PAGE_SIZE, Math.max(0, payload.total - (plan.page - 1) * NOTE_PAGE_SIZE));
  if (payload.items.length !== expected) throw new TypeError(formatError);
  const ids = new Set();
  for (const item of payload.items) {
    if (!validHistoryNote(item, plan.symbol) || ids.has(item.id)) throw new TypeError(formatError);
    ids.add(item.id);
  }
  const history = plan.history;
  if (history.snapshot === payload.snapshot) {
    if (history.total !== payload.total || history.pageCount !== payload.page_count) throw new TypeError(formatError);
    for (const [page, seen] of history.pages) {
      if (page !== plan.page && seen.some(id => ids.has(id))) throw new TypeError(formatError);
    }
  }
  return payload;
}

function validHistoryEnvelope(payload, plan) {
  return payload && payload.symbol === plan.symbol && payload.page === plan.page
    && payload.page_size === NOTE_PAGE_SIZE && Number.isSafeInteger(payload.total) && payload.total >= 0
    && payload.page_count === Math.ceil(payload.total / NOTE_PAGE_SIZE)
    && typeof payload.snapshot === "string" && digestPattern.test(payload.snapshot)
    && (!plan.snapshot || payload.snapshot === plan.snapshot) && Array.isArray(payload.items);
}

function validHistoryNote(item, symbol) {
  return item && Number.isSafeInteger(item.id) && item.id > 0 && item.symbol === symbol
    && item.code === symbol.slice(0, 6) && item.market === symbol.slice(7)
    && typeof item.revision === "string" && digestPattern.test(item.revision)
    && ["name", "content", "note_type", "created_at", "updated_at"].every(key => typeof item[key] === "string")
    && (item.price === null || typeof item.price === "number" && Number.isFinite(item.price))
    && ["trade_date", "color"].every(key => item[key] === null || typeof item[key] === "string")
    && typeof item.visible === "boolean";
}

export function acceptNoteHistoryPage(history, payload) {
  if (history.snapshot !== payload.snapshot) history.pages.clear();
  history.pages.set(payload.page, payload.items.map(item => item.id));
  Object.assign(history, { page: payload.page, total: payload.total, pageCount: payload.page_count,
    snapshot: payload.snapshot, stale: false, message: "" });
}

export function failNoteHistoryRead(history, error) {
  history.stale = history.stale || error.status === 409 || error instanceof TypeError;
  history.message = error.status === 409
    ? "历史已变化，当前保留旧页；请点击刷新后继续翻页"
    : `历史读取失败，已保留当前笔记：${error.message}${history.stale ? "；请刷新后继续翻页" : ""}`;
}

export function renderNoteHistoryControls(history) {
  const controls = $("noteHistoryControls");
  if (controls) controls.setAttribute("aria-busy", String(history.busy));
  setDisabled("noteHistoryOpen", history.busy || history.active);
  setDisabled("noteHistoryRefresh", history.busy);
  setDisabled("noteHistoryPrevious", history.busy || history.stale || !history.snapshot || history.page <= 1);
  setDisabled("noteHistoryNext", history.busy || history.stale || !history.snapshot || history.page >= history.pageCount);
  const status = $("noteHistoryStatus");
  if (!status) return;
  const summary = history.total === null
    ? `当前显示最近 ${history.recentCount} 条；全部历史可查看总数`
    : history.total === 0 ? "共 0 条笔记" : `共 ${history.total} 条 · 第 ${history.page} / ${history.pageCount} 页 · 每页 ${NOTE_PAGE_SIZE} 条`;
  status.textContent = `${summary}${history.busy ? " · 读取中…" : history.message ? ` · ${history.message}` : ""}`;
}

function setDisabled(id, disabled) {
  const control = $(id);
  if (control) control.disabled = disabled;
}
