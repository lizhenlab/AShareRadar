import {
  DEFAULT_REQUEST_TIMEOUT_MS,
  GLOBAL_DATA_TTL_MS,
  cancelCachedJsonRequest,
  createRequestScope,
  fetchCachedJson,
  fetchJson,
  getCachedJsonSnapshot,
  invalidateCachedJson,
  isAbortError,
} from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { changeClass, formatNumber } from "./format.js";

export const WATCHLIST_ENDPOINT = "/api/watchlist";

const RESEARCH_STATUS_LABELS = Object.freeze({
  to_research: "待研究",
  watching: "持续观察",
  holding_research: "持仓研究",
  excluded: "已排除",
});
const PRIORITY_LABELS = Object.freeze({ high: "高", medium: "中", low: "低" });
const EDITABLE_FIELDS = new Set([
  "research_status",
  "priority",
  "next_review_date",
  "group_name",
  "note",
  "pinned",
]);

export async function loadWatchlist(state, options = {}) {
  const requestId = nextWatchlistRequestId(state);
  const request = createRequestScope(state.watchlistRequest, options.signal);
  state.watchlistRequest = request;
  const isCurrent = () =>
    state.watchlistSeq === requestId &&
    state.watchlistRequest === request &&
    !request.signal.aborted &&
    (!options.isCurrent || options.isCurrent());
  try {
    const items = await fetchCachedJson(WATCHLIST_ENDPOINT, {
      force: Boolean(options.force),
      signal: request.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      ttlMs: options.ttlMs ?? GLOBAL_DATA_TTL_MS,
      validate: watchlistItems,
    });
    if (!isCurrent()) return false;
    applyWatchlistItems(state, items, options);
    return true;
  } catch (error) {
    if (isAbortError(error)) return false;
    if (!isCurrent()) return false;
    const cached = getCachedJsonSnapshot(WATCHLIST_ENDPOINT);
    let preservedCachedValue = false;
    if (cached.found && options.useCachedOnError !== false) {
      applyWatchlistItems(state, watchlistItems(cached.value), options);
      preservedCachedValue = true;
    }
    if (preservedCachedValue) {
      appendWatchlistLoadWarning(error);
    } else if (!options.preserveOnError) {
      const list = $("watchList");
      if (list) {
        list.innerHTML = `<div class="watch-row watch-row-message watch-row-error"><strong>自选股读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
      }
    }
    throw error;
  } finally {
    finishWatchlistReadRequest(state, request);
  }
}

function nextWatchlistRequestId(state) {
  state.watchlistSeq = Number(state.watchlistSeq || 0) + 1;
  return state.watchlistSeq;
}

function watchlistItems(payload) {
  if (Array.isArray(payload)) return payload;
  throw new Error("自选股数据格式异常");
}

function applyWatchlistItems(state, items, options = {}) {
  const previousItems = Array.isArray(state.watchlist) ? state.watchlist : [];
  const changed = watchlistSubscriptionKey(previousItems) !== watchlistSubscriptionKey(items);
  state.watchlist = items;
  renderWatchlist(items);
  if (changed && typeof options.onItemsChanged === "function") {
    options.onItemsChanged({ items, previousItems });
  }
}

export function watchlistSubscriptionKey(items) {
  return (Array.isArray(items) ? items : [])
    .filter((item) => !isExcludedWatchlistItem(item))
    .map((item) => normalizeMutationSymbol(item && item.symbol))
    .filter(Boolean)
    .filter((item, index, rows) => rows.indexOf(item) === index)
    .sort()
    .join(",");
}

export function isExcludedWatchlistItem(item) {
  return String((item && item.research_status) || "")
    .trim()
    .toLowerCase() === "excluded";
}

export function renderWatchlist(items, options = {}) {
  const list = $("watchList");
  if (!list) return;
  const rows = Array.isArray(items) ? items : [];
  const today = normalizedDate(options.today) || localToday(options.now);
  list.innerHTML = rows.length
    ? rows.map((item, index) => renderWatchlistRow(item || {}, index, today)).join("")
    : `<div class="watch-row watch-row-message"><strong>暂无自选</strong><span>输入代码后加入研究队列。</span></div>`;
}

function renderWatchlistRow(item, index, today) {
  const symbol = normalizeMutationSymbol(item.symbol);
  const code = String(item.code || symbol.slice(0, 6) || "--").trim();
  const name = String(item.name || code || symbol || "未知股票").trim();
  const status = normalizedStatus(item.research_status);
  const priority = normalizedPriority(item.priority);
  const excluded = status === "excluded";
  const pinned = Boolean(item.pinned);
  const unread = normalizedUnreadCount(item.unread_change_count);
  const groupName = String(item.group_name || "默认").trim() || "默认";
  const note = String(item.note || "").trim();
  const review = reviewDateMeta(item.next_review_date, today);
  const price = formatNumber(item.latest_price);
  const change = formatNumber(item.latest_change_pct);
  const changeText = change === "--" ? "--" : `${change}%`;
  const editorId = `watch-editor-${index}`;
  return `
    <article class="watch-row watch-queue-row${excluded ? " is-excluded" : ""}" data-symbol="${escapeHtml(symbol)}">
      <div class="watch-row-summary">
        <button type="button" class="watch-main" data-action="open" data-symbol="${escapeHtml(symbol)}">
          <span class="watch-stock-title">
            <strong>${escapeHtml(name)}</strong>
            <span class="watch-code">${escapeHtml(code)}</span>
          </span>
          <span class="watch-badges">
            ${pinned ? `<span class="watch-badge watch-pin">置顶</span>` : ""}
            <span class="watch-badge watch-status status-${escapeHtml(status)}">${escapeHtml(RESEARCH_STATUS_LABELS[status])}</span>
            <span class="watch-badge watch-priority priority-${escapeHtml(priority)}">${escapeHtml(PRIORITY_LABELS[priority])}优先级</span>
            <span class="watch-badge watch-review ${escapeHtml(review.className)}">${escapeHtml(review.label)}</span>
            ${unread ? `<span class="watch-badge watch-unread">${escapeHtml(unreadLabel(unread))}</span>` : ""}
          </span>
          <span class="watch-context">
            <span class="watch-group">分组 · ${escapeHtml(groupName)}</span>
            <small>关注原因 · ${escapeHtml(note || "暂无")}</small>
          </span>
        </button>
        <div class="watch-side">
          <span class="watch-quote"><strong>${escapeHtml(price)}</strong><span class="${changeClass(item.latest_change_pct)}">${escapeHtml(changeText)}</span></span>
          <span class="watch-row-actions">
            <button type="button" class="watch-action-button" title="编辑研究队列" aria-label="编辑 ${escapeHtml(name)}" aria-expanded="false" aria-controls="${editorId}" data-action="edit" data-symbol="${escapeHtml(symbol)}">编辑</button>
            <button type="button" class="watch-action-button watch-remove" title="移出自选" aria-label="移出自选" data-action="remove" data-symbol="${escapeHtml(symbol)}">移除</button>
          </span>
        </div>
      </div>
      ${renderWatchlistEditor({ editorId, symbol, status, priority, reviewDate: review.value, groupName, note, pinned })}
    </article>`;
}

function renderWatchlistEditor({ editorId, symbol, status, priority, reviewDate, groupName, note, pinned }) {
  return `
    <form class="watch-edit-form" id="${editorId}" data-watch-edit data-symbol="${escapeHtml(symbol)}" hidden>
      <div class="watch-edit-grid">
        <label><span>研究状态</span><select name="research_status">${choiceOptions(RESEARCH_STATUS_LABELS, status)}</select></label>
        <label><span>优先级</span><select name="priority">${choiceOptions(PRIORITY_LABELS, priority)}</select></label>
        <label><span>复核日期</span><input type="date" name="next_review_date" value="${escapeHtml(reviewDate)}" /></label>
        <label><span>分组</span><input name="group_name" value="${escapeHtml(groupName)}" maxlength="20" autocomplete="off" /></label>
        <label class="watch-edit-note"><span>关注原因</span><input name="note" value="${escapeHtml(note)}" maxlength="80" autocomplete="off" placeholder="可留空" /></label>
      </div>
      <div class="watch-edit-actions">
        <label class="watch-pin-toggle"><input type="checkbox" name="pinned"${pinned ? " checked" : ""} /><span>置顶</span></label>
        <span class="watch-edit-commands">
          <button type="button" class="watch-action-button" data-action="cancel-edit" data-symbol="${escapeHtml(symbol)}">取消</button>
          <button type="submit" class="watch-save-button">保存</button>
        </span>
      </div>
      <p class="watch-edit-feedback" role="alert" hidden></p>
    </form>`;
}

function choiceOptions(labels, selectedValue) {
  return Object.entries(labels)
    .map(
      ([value, label]) =>
        `<option value="${escapeHtml(value)}"${value === selectedValue ? " selected" : ""}>${escapeHtml(label)}</option>`
    )
    .join("");
}

export async function addWatchlistItem(state, options = {}) {
  const symbolInput = $("watchSymbolInput");
  const noteInput = $("watchNoteInput");
  const groupInput = $("watchGroupInput");
  const statusInput = $("watchStatusInput");
  const priorityInput = $("watchPriorityInput");
  const reviewInput = $("watchReviewDateInput");
  const symbol = String((symbolInput && symbolInput.value) || "").trim() || options.symbol || state.symbol;
  const note = String((noteInput && noteInput.value) || "").trim();
  const groupName = String((groupInput && groupInput.value) || "").trim();
  const researchStatus = normalizedStatus((statusInput && statusInput.value) || "to_research", "to_research");
  const priority = normalizedPriority((priorityInput && priorityInput.value) || "medium");
  const nextReviewDate = normalizedDate((reviewInput && reviewInput.value) || "");
  const form = $("watchForm");
  const button = form && typeof form.querySelector === "function" ? form.querySelector('button[type="submit"]') || form.querySelector("button") : null;
  if (button && button.disabled) return false;
  const request = beginWatchlistMutation(state, options, "add", symbol);
  if (!request) return false;
  const previousText = button ? button.textContent : "";
  try {
    if (form && typeof form.setAttribute === "function") form.setAttribute("aria-busy", "true");
    if (button) {
      button.disabled = true;
      button.textContent = "加入中";
    }
    await fetchJson(WATCHLIST_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol,
        note: note || null,
        group_name: groupName || null,
        research_status: researchStatus,
        priority,
        next_review_date: nextReviewDate || null,
      }),
      signal: request.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (!request.isActive()) return false;
    invalidateWatchlistCache();
    if (request.isCurrent()) {
      resetAddFormIfUnchanged({ noteInput, note, groupInput, groupName, statusInput, researchStatus, priorityInput, priority, reviewInput, nextReviewDate });
      notifyMutationSuccess(options, { action: "add", symbol });
    }
    return await refreshWatchlistAfterMutation(state, request, options, "加入");
  } catch (error) {
    if (isAbortError(error)) return false;
    if (!request.isCurrent()) return false;
    throw error;
  } finally {
    finishWatchlistMutationRequest(state, request);
    if (form && typeof form.setAttribute === "function") form.setAttribute("aria-busy", "false");
    if (button) {
      button.disabled = false;
      button.textContent = previousText || "加入队列";
    }
  }
}

function resetAddFormIfUnchanged(values) {
  if (values.noteInput && values.noteInput.value.trim() === values.note) values.noteInput.value = "";
  if (values.groupInput && values.groupInput.value.trim() === values.groupName) values.groupInput.value = "";
  if (values.reviewInput && normalizedDate(values.reviewInput.value) === values.nextReviewDate) values.reviewInput.value = "";
  if (values.statusInput && values.statusInput.value === values.researchStatus) values.statusInput.value = "to_research";
  if (values.priorityInput && values.priorityInput.value === values.priority) values.priorityInput.value = "medium";
}

export async function updateWatchlistItem(state, symbol, updates, options = {}) {
  const payload = editableWatchlistPayload(updates);
  if (!Object.keys(payload).length) return false;
  const request = beginWatchlistMutation(state, options, "update", symbol);
  if (!request) return false;
  try {
    const item = await fetchJson(`${WATCHLIST_ENDPOINT}/${encodeURIComponent(symbol)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: request.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (!request.isActive()) return false;
    invalidateWatchlistCache();
    confirmWatchlistUpdate(state, symbol, item, payload, options);
    if (request.isCurrent()) notifyMutationSuccess(options, { action: "update", symbol, fields: Object.keys(payload) });
    return await refreshWatchlistAfterMutation(state, request, options, "更新");
  } catch (error) {
    if (isAbortError(error)) return false;
    if (!request.isCurrent()) return false;
    throw error;
  } finally {
    finishWatchlistMutationRequest(state, request);
  }
}

export async function markWatchlistItemViewed(state, symbol, options = {}) {
  const viewedThroughAdviceId = options.viewedThroughAdviceId;
  if (!Number.isInteger(viewedThroughAdviceId) || viewedThroughAdviceId <= 0) {
    throw new Error("建议变化尚未完整加载，未读状态保持");
  }
  const request = beginWatchlistMutation(state, options, "mark-viewed", symbol);
  if (!request) return false;
  try {
    const item = await fetchJson(`${WATCHLIST_ENDPOINT}/${encodeURIComponent(symbol)}/mark-viewed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clear_unread: true,
        viewed_through_advice_id: viewedThroughAdviceId,
      }),
      signal: request.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (!request.isActive()) return false;
    invalidateWatchlistCache();
    confirmWatchlistUpdate(state, symbol, item, {}, options);
    if (request.isCurrent()) notifyMutationSuccess(options, { action: "mark-viewed", symbol });
    return await refreshWatchlistAfterMutation(state, request, options, "标记已读");
  } catch (error) {
    if (isAbortError(error)) return false;
    if (!request.isCurrent()) return false;
    throw error;
  } finally {
    finishWatchlistMutationRequest(state, request);
  }
}

export async function removeWatchlistItem(state, symbol, options = {}) {
  const request = beginWatchlistMutation(state, options, "remove", symbol);
  if (!request) return false;
  try {
    await fetchJson(`${WATCHLIST_ENDPOINT}/${encodeURIComponent(symbol)}`, {
      method: "DELETE",
      signal: request.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (!request.isActive()) return false;
    invalidateWatchlistCache();
    confirmWatchlistRemoval(state, symbol, options);
    if (request.isCurrent()) notifyMutationSuccess(options, { action: "remove", symbol });
    return await refreshWatchlistAfterMutation(state, request, options, "删除");
  } catch (error) {
    if (isAbortError(error)) return false;
    if (!request.isCurrent()) return false;
    throw error;
  } finally {
    finishWatchlistMutationRequest(state, request);
  }
}

export function watchlistUpdatesFromForm(form) {
  const status = formControl(form, "research_status");
  const priority = formControl(form, "priority");
  const reviewDate = formControl(form, "next_review_date");
  const groupName = formControl(form, "group_name");
  const note = formControl(form, "note");
  const pinned = formControl(form, "pinned");
  const dateValue = normalizedDate(reviewDate && reviewDate.value);
  return {
    research_status: normalizedStatus(status && status.value),
    priority: normalizedPriority(priority && priority.value),
    next_review_date: dateValue || null,
    group_name: String((groupName && groupName.value) || "").trim(),
    note: String((note && note.value) || "").trim() || null,
    pinned: Boolean(pinned && pinned.checked),
  };
}

function formControl(form, name) {
  if (!form) return null;
  if (form.elements && typeof form.elements.namedItem === "function") return form.elements.namedItem(name);
  return typeof form.querySelector === "function" ? form.querySelector(`[name="${name}"]`) : null;
}

export function toggleWatchlistEditor(button, forceOpen) {
  const row = button && typeof button.closest === "function" ? button.closest(".watch-row") : null;
  const form = row && typeof row.querySelector === "function" ? row.querySelector(".watch-edit-form") : null;
  if (!form) return false;
  const shouldOpen = typeof forceOpen === "boolean" ? forceOpen : Boolean(form.hidden);
  if (shouldOpen) closeOtherWatchlistEditors(form);
  form.hidden = !shouldOpen;
  if (!shouldOpen && typeof form.reset === "function") form.reset();
  if (typeof button.setAttribute === "function") button.setAttribute("aria-expanded", String(shouldOpen));
  if (shouldOpen) {
    const first = typeof form.querySelector === "function" ? form.querySelector("select, input") : null;
    if (first && typeof first.focus === "function") first.focus({ preventScroll: true });
  }
  return true;
}

function closeOtherWatchlistEditors(currentForm) {
  if (!document || typeof document.querySelectorAll !== "function") return;
  document.querySelectorAll(".watch-edit-form:not([hidden])").forEach((form) => {
    if (form === currentForm) return;
    form.hidden = true;
    if (typeof form.reset === "function") form.reset();
    const row = typeof form.closest === "function" ? form.closest(".watch-row") : null;
    const button = row && typeof row.querySelector === "function" ? row.querySelector('[data-action="edit"]') : null;
    if (button && typeof button.setAttribute === "function") button.setAttribute("aria-expanded", "false");
  });
}

function editableWatchlistPayload(updates) {
  if (!updates || typeof updates !== "object" || Array.isArray(updates)) return {};
  return Object.entries(updates).reduce((payload, [field, value]) => {
    if (EDITABLE_FIELDS.has(field)) payload[field] = value;
    return payload;
  }, {});
}

async function refreshWatchlistAfterMutation(state, mutation, options, actionLabel) {
  const isRefreshCurrent = mutation.isActive;
  const loadOptions = {
    force: true,
    isCurrent: isRefreshCurrent,
    onItemsChanged: options.onItemsChanged,
    preserveOnError: true,
    useCachedOnError: false,
  };
  try {
    await loadWatchlist(state, loadOptions);
  } catch (error) {
    if (isAbortError(error) || !isRefreshCurrent()) return mutation.isCurrent();
    renderWatchlist(Array.isArray(state.watchlist) ? state.watchlist : []);
    appendWatchlistReadbackWarning(actionLabel, error);
    if (mutation.isCurrent()) {
      notifyReadbackError(options, error, {
        action: mutation.action,
        symbol: mutation.symbol,
      });
    }
  }
  return mutation.isCurrent();
}

function confirmWatchlistUpdate(state, symbol, responseItem, fallbackPatch, options) {
  const target = normalizeMutationSymbol(symbol);
  const items = Array.isArray(state.watchlist) ? state.watchlist : [];
  let found = false;
  const serverPatch = responseItem && typeof responseItem === "object" && !Array.isArray(responseItem) ? responseItem : {};
  const updated = items.map((item) => {
    if (normalizeMutationSymbol(item && item.symbol) !== target) return item;
    found = true;
    return { ...item, ...fallbackPatch, ...serverPatch, symbol: item.symbol };
  });
  if (found) applyWatchlistItems(state, updated, options);
}

function confirmWatchlistRemoval(state, symbol, options) {
  const target = normalizeMutationSymbol(symbol);
  const items = Array.isArray(state.watchlist) ? state.watchlist : [];
  const remaining = items.filter((item) => normalizeMutationSymbol(item && item.symbol) !== target);
  applyWatchlistItems(state, remaining, options);
}

function appendWatchlistReadbackWarning(actionLabel, error) {
  appendWatchlistMessage(`已${actionLabel}，列表同步降级`, error && error.message);
}

function appendWatchlistLoadWarning(error) {
  appendWatchlistMessage("自选股同步降级，显示上次结果", error && error.message);
}

export function appendWatchlistMessage(title, detail = "") {
  const list = $("watchList");
  if (!list) return;
  list.innerHTML += `<div class="watch-row watch-row-message watch-row-error"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
}

function beginWatchlistMutation(state, options, action, symbol) {
  const operationKey = `${action}:${normalizeMutationSymbol(symbol)}`;
  const operations = mutationOperations(state);
  if (operations.has(operationKey)) return null;
  const requestId = Number(state.watchlistMutationSeq || 0) + 1;
  // Watchlist writes are global; caller context only gates stale UI effects.
  const scope = createRequestScope();
  const requests = mutationRequests(state);
  state.watchlistMutationSeq = requestId;
  requests.set(requestId, scope);
  operations.set(operationKey, scope);
  const isActive = () => requests.get(requestId) === scope && !scope.signal.aborted;
  return {
    action,
    id: requestId,
    operationKey,
    scope,
    signal: scope.signal,
    symbol,
    isActive,
    isCurrent: () =>
      isActive() &&
      (!options.signal || !options.signal.aborted) &&
      (!options.isCurrent || options.isCurrent()),
  };
}

function mutationRequests(state) {
  if (!(state.watchlistMutationRequests instanceof Map)) state.watchlistMutationRequests = new Map();
  return state.watchlistMutationRequests;
}

function mutationOperations(state) {
  if (!(state.watchlistMutationOperations instanceof Map)) state.watchlistMutationOperations = new Map();
  return state.watchlistMutationOperations;
}

function normalizeMutationSymbol(symbol) {
  return String(symbol || "").trim().toUpperCase();
}

function notifyMutationSuccess(options, detail) {
  if (typeof options.onMutationSuccess === "function") options.onMutationSuccess(detail);
}

function notifyReadbackError(options, error, detail) {
  if (typeof options.onReadbackError === "function") options.onReadbackError(error, detail);
}

export function invalidateWatchlistCache() {
  invalidateCachedJson(WATCHLIST_ENDPOINT);
}

export function cancelWatchlistRefresh(state) {
  if (state.watchlistRequest) state.watchlistRequest.abort();
  cancelCachedJsonRequest(WATCHLIST_ENDPOINT);
}

function finishWatchlistReadRequest(state, request) {
  if (state.watchlistRequest === request) state.watchlistRequest = null;
  request.dispose();
}

function finishWatchlistMutationRequest(state, request) {
  const requests = mutationRequests(state);
  const operations = mutationOperations(state);
  if (requests.get(request.id) === request.scope) requests.delete(request.id);
  if (operations.get(request.operationKey) === request.scope) operations.delete(request.operationKey);
  request.scope.dispose();
}

function normalizedStatus(value, fallback = "watching") {
  const normalized = String(value || "").trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(RESEARCH_STATUS_LABELS, normalized) ? normalized : fallback;
}

function normalizedPriority(value) {
  const normalized = String(value || "").trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(PRIORITY_LABELS, normalized) ? normalized : "medium";
}

function normalizedUnreadCount(value) {
  const count = Number(value);
  return Number.isInteger(count) && count > 0 ? count : 0;
}

function unreadLabel(count) {
  return `${count > 99 ? "99+" : count} 条新变化`;
}

function reviewDateMeta(value, today) {
  const date = normalizedDate(value);
  if (!date) return { className: "review-unset", label: "未设复核", value: "" };
  const difference = dateDifference(date, today);
  if (difference < 0) return { className: "review-overdue", label: `逾期复核 · ${date}`, value: date };
  if (difference === 0) return { className: "review-due", label: `今日复核 · ${date}`, value: date };
  return { className: "review-upcoming", label: `复核 · ${date}`, value: date };
}

function normalizedDate(value) {
  const text = String(value || "").trim().slice(0, 10);
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!match) return "";
  const timestamp = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const parsed = new Date(timestamp);
  const canonical = `${parsed.getUTCFullYear().toString().padStart(4, "0")}-${String(parsed.getUTCMonth() + 1).padStart(2, "0")}-${String(parsed.getUTCDate()).padStart(2, "0")}`;
  return canonical === text ? text : "";
}

function localToday(now) {
  const date = now instanceof Date && Number.isFinite(now.getTime()) ? now : new Date();
  return `${date.getFullYear().toString().padStart(4, "0")}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function dateDifference(left, right) {
  return (dateTimestamp(left) - dateTimestamp(right)) / 86400000;
}

function dateTimestamp(value) {
  const [year, month, day] = value.split("-").map(Number);
  return Date.UTC(year, month - 1, day);
}
