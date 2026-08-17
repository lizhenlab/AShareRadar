import { escapeHtml } from "./dom.js";
import { validateUiSymbol } from "./symbols.js";

export const STOCK_SEARCH_HISTORY_STORAGE_KEY = "ashare-radar.stock-search-history";
export const STOCK_SEARCH_HISTORY_VERSION = 1;
export const DEFAULT_STOCK_SEARCH_HISTORY_LIMIT = 8;

export function loadStockSearchHistory(storage = browserStorage(), limit = DEFAULT_STOCK_SEARCH_HISTORY_LIMIT) {
  if (!storage || typeof storage.getItem !== "function") return [];
  try {
    const payload = JSON.parse(storage.getItem(STOCK_SEARCH_HISTORY_STORAGE_KEY));
    if (!isRecord(payload) || payload.version !== STOCK_SEARCH_HISTORY_VERSION || !Array.isArray(payload.items)) {
      return [];
    }
    return sanitizeHistory(payload.items, limit);
  } catch (error) {
    return [];
  }
}

export function saveStockSearchHistory(items, storage = browserStorage(), limit = DEFAULT_STOCK_SEARCH_HISTORY_LIMIT) {
  if (!storage || typeof storage.setItem !== "function") return false;
  try {
    storage.setItem(
      STOCK_SEARCH_HISTORY_STORAGE_KEY,
      JSON.stringify({
        version: STOCK_SEARCH_HISTORY_VERSION,
        items: sanitizeHistory(items, limit),
      })
    );
    return true;
  } catch (error) {
    return false;
  }
}

export function mergeStockSearchHistory(
  items,
  candidate,
  limit = DEFAULT_STOCK_SEARCH_HISTORY_LIMIT
) {
  const next = sanitizeHistoryItem(candidate);
  if (!next) return sanitizeHistory(items, limit);
  return [next, ...sanitizeHistory(items, limit).filter((item) => item.symbol !== next.symbol)]
    .slice(0, normalizedLimit(limit));
}

export function createStockSearchHistory(options = {}) {
  const root = options.root || globalThis.document;
  const storage = options.storage === undefined ? browserStorage() : options.storage;
  const limit = normalizedLimit(options.limit);
  const onSelect = typeof options.onSelect === "function" ? options.onSelect : () => {};
  const panel = root?.getElementById?.("stockSearchHistory");
  const list = root?.getElementById?.("stockSearchHistoryList");
  const count = root?.getElementById?.("stockSearchHistoryCount");
  const empty = root?.getElementById?.("stockSearchHistoryEmpty");
  const clear = root?.getElementById?.("stockSearchHistoryClear");
  let items = loadStockSearchHistory(storage, limit);

  const render = () => {
    if (count) count.textContent = String(items.length);
    if (clear) clear.disabled = items.length === 0;
    if (empty) empty.hidden = items.length > 0;
    if (list) {
      list.innerHTML = items.map(historyItemHtml).join("");
      list.hidden = items.length === 0;
    }
    return items;
  };

  const handleClear = () => {
    items = [];
    saveStockSearchHistory(items, storage, limit);
    render();
  };
  const handleListClick = (event) => {
    const button = event.target?.closest?.("button[data-stock-history-symbol]");
    if (!button) return;
    const item = items.find((entry) => entry.symbol === button.dataset.stockHistorySymbol);
    if (!item) return;
    onSelect(item.symbol, { ...item });
  };

  clear?.addEventListener?.("click", handleClear);
  list?.addEventListener?.("click", handleListClick);
  if (panel) panel.hidden = false;
  render();

  return {
    close() {
      if (panel) panel.hidden = false;
      return false;
    },
    items() {
      return items.map((item) => ({ ...item }));
    },
    record(candidate) {
      const nextItems = mergeStockSearchHistory(items, candidate, limit);
      if (sameHistory(items, nextItems)) return false;
      items = nextItems;
      saveStockSearchHistory(items, storage, limit);
      render();
      return true;
    },
    clear: handleClear,
    destroy() {
      clear?.removeEventListener?.("click", handleClear);
      list?.removeEventListener?.("click", handleListClick);
    },
  };
}

function historyItemHtml(item) {
  const [code, market] = item.symbol.split(".");
  return `
    <button type="button" class="stock-search-history-item" data-stock-history-symbol="${escapeHtml(item.symbol)}" title="重新加载 ${escapeHtml(item.name)} 的分析">
      <strong>${escapeHtml(item.name)}</strong>
      <span>${escapeHtml(code)}.${escapeHtml(market)}</span>
      <small>重新分析</small>
    </button>`;
}

function sanitizeHistory(items, limit) {
  if (!Array.isArray(items)) return [];
  const result = [];
  for (const candidate of items) {
    const item = sanitizeHistoryItem(candidate);
    if (!item || result.some((entry) => entry.symbol === item.symbol)) continue;
    result.push(item);
    if (result.length >= normalizedLimit(limit)) break;
  }
  return result;
}

function sanitizeHistoryItem(candidate) {
  if (!isRecord(candidate)) return null;
  try {
    const symbol = validateUiSymbol(candidate.symbol);
    const name = String(candidate.name || "").replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 40);
    if (!name) return null;
    return { symbol, name };
  } catch (error) {
    return null;
  }
}

function sameHistory(left, right) {
  return left.length === right.length
    && left.every((item, index) => item.symbol === right[index].symbol && item.name === right[index].name);
}

function normalizedLimit(limit) {
  const parsed = Number.parseInt(limit, 10);
  return Number.isFinite(parsed) ? Math.min(20, Math.max(1, parsed)) : DEFAULT_STOCK_SEARCH_HISTORY_LIMIT;
}

function browserStorage() {
  try {
    return globalThis.localStorage || null;
  } catch (error) {
    return null;
  }
}

function isRecord(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
