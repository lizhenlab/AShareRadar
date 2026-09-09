const STATUS_VALUES = new Set(["all", "to_research", "watching", "holding_research", "excluded"]);
const bindings = new WeakMap();
const knownLists = new WeakSet();
const FILTER_EVENTS = Object.freeze([
  ["watchQueueSearch", "input"],
  ["watchQueueStatus", "change"],
  ["watchQueueDue", "change"],
  ["watchQueueUnread", "change"],
]);

export function selectWatchlistQueue(items, filters = {}, options = {}) {
  const rows = Array.isArray(items) ? items : [];
  const today = watchlistReviewDate(options.today) || watchlistMarketDate(options.now);
  const terms = normalizedSearch(filters.query).split(/\s+/).filter(Boolean);
  const status = STATUS_VALUES.has(filters.status) ? filters.status : "all";
  return rows.filter((item) => {
    if (!item || typeof item !== "object") return false;
    const actualStatus = String(item.research_status || "watching").trim().toLowerCase();
    if (status !== "all" && actualStatus !== status) return false;
    const reviewDate = watchlistReviewDate(item.next_review_date);
    if (filters.due && (!reviewDate || reviewDate > today)) return false;
    const unread = typeof item.unread_change_count === "boolean" ? 0 : Number(item.unread_change_count);
    if (filters.unread && (!Number.isInteger(unread) || unread <= 0)) return false;
    const searchText = normalizedSearch([item.symbol, item.code, item.name, item.group_name, item.note].join(" "));
    return terms.every((term) => searchText.includes(term));
  });
}

export function applyWatchlistQueueView(items, options = {}) {
  const root = options.root || globalThis.document;
  const rows = Array.isArray(items) ? items : [];
  const filters = readFilters(root);
  const reset = root?.getElementById?.("watchQueueReset");
  if (reset) reset.disabled = !filters.query.trim() && filters.status === "all" && !filters.due && !filters.unread;
  const list = root?.getElementById?.("watchList");
  if (list && options.sourceReady === true) knownLists.add(list);
  if (list && options.sourceReady === false) knownLists.delete(list);
  if (list && !knownLists.has(list)) {
    const count = root?.getElementById?.("watchQueueCount");
    if (count) count.textContent = "队列尚未读取成功，暂无法筛选";
    const empty = root?.getElementById?.("watchQueueNoMatch");
    if (empty) empty.hidden = true;
    return [];
  }
  const today = watchlistReviewDate(options.today) || watchlistMarketDate(options.now);
  const matches = selectWatchlistQueue(rows, filters, { today });
  const symbols = new Set(matches.map((item) => String(item.symbol || "").trim().toUpperCase()));
  const records = new Map(rows.map((item) => [String(item?.symbol || "").trim().toUpperCase(), item]));
  for (const row of list?.querySelectorAll?.(".watch-queue-row") || []) {
    const symbol = String(row.dataset.symbol || "").trim().toUpperCase();
    row.hidden = !symbols.has(symbol);
    refreshReviewBadge(row, records.get(symbol), today);
  }
  const count = root?.getElementById?.("watchQueueCount");
  if (count) count.textContent = `显示 ${matches.length} / 共 ${rows.length} 条`;
  const empty = root?.getElementById?.("watchQueueNoMatch");
  if (empty) empty.hidden = !rows.length || matches.length > 0;
  return matches;
}

export function bindWatchlistQueueFilters(getItems, options = {}) {
  const root = options.root || globalThis.document;
  if (!root) return () => {};
  bindings.get(root)?.();
  const listeners = [];
  const apply = () => applyWatchlistQueueView(getItems(), options);
  const listen = (target, event, handler) => {
    if (!target?.addEventListener) return;
    target.addEventListener(event, handler);
    listeners.push(() => target.removeEventListener?.(event, handler));
  };
  for (const [id, event] of FILTER_EVENTS) listen(root.getElementById?.(id), event, apply);
  listen(root.getElementById?.("watchQueueReset"), "click", () => {
    resetFilters(root);
    apply();
  });
  listen(root, "visibilitychange", () => {
    if (!root.hidden) apply();
  });
  const dispose = () => {
    for (const remove of listeners) remove();
    if (bindings.get(root) === dispose) bindings.delete(root);
  };
  bindings.set(root, dispose);
  return dispose;
}

export function watchlistReviewMeta(value, today) {
  const date = watchlistReviewDate(value);
  if (!date) return { className: "review-unset", label: "未设复核", value: "" };
  if (date < today) return { className: "review-overdue", label: `逾期复核 · ${date}`, value: date };
  if (date === today) return { className: "review-due", label: `今日复核 · ${date}`, value: date };
  return { className: "review-upcoming", label: `复核 · ${date}`, value: date };
}

function refreshReviewBadge(row, item, today) {
  const badge = row.querySelector?.(".watch-review");
  if (!badge || !item) return;
  const review = watchlistReviewMeta(item.next_review_date, today);
  badge.textContent = review.label;
  badge.className = `watch-badge watch-review ${review.className}`;
}

export function watchlistReviewDate(value) {
  const text = String(value || "").trim();
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!match) return "";
  const parsed = new Date(`${text}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === text ? text : "";
}

export function watchlistMarketDate(now) {
  const date = now instanceof Date && Number.isFinite(now.getTime()) ? now : new Date();
  return new Date(date.getTime() + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
}

function normalizedSearch(value) {
  return String(value || "").normalize("NFKC").trim().toLocaleLowerCase("zh-CN");
}

function readFilters(root) {
  return {
    query: String(root?.getElementById?.("watchQueueSearch")?.value || "").slice(0, 120),
    status: root?.getElementById?.("watchQueueStatus")?.value || "all",
    due: Boolean(root?.getElementById?.("watchQueueDue")?.checked),
    unread: Boolean(root?.getElementById?.("watchQueueUnread")?.checked),
  };
}

function resetFilters(root) {
  for (const [id, value] of [["watchQueueSearch", ""], ["watchQueueStatus", "all"]]) {
    const input = root?.getElementById?.(id);
    if (input) input.value = value;
  }
  for (const id of ["watchQueueDue", "watchQueueUnread"]) {
    const input = root?.getElementById?.(id);
    if (input) input.checked = false;
  }
}
