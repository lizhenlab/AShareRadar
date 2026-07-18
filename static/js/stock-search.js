import {
  DEFAULT_REQUEST_TIMEOUT_MS,
  createRequestScope,
  fetchJson,
  isAbortError,
} from "./api.js";

export const DEFAULT_STOCK_SEARCH_DEBOUNCE_MS = 250;
export const DEFAULT_STOCK_SEARCH_CACHE_SIZE = 40;
export const STOCK_SEARCH_LIMIT = 8;

const REQUIRED_STOCK_FIELDS = ["symbol", "code", "market", "name"];
const STOCK_SYMBOL_PATTERN = /^(\d{6})\.(SH|SZ)$/;
const UNAVAILABLE_MESSAGE = "Stock search unavailable";

export function createStockSearchController(options = {}) {
  const settings = normalizeOptions(options);
  const cache = new Map();
  let destroyed = false;
  let sequence = 0;
  let debounceTimer = null;
  let requestScope = null;
  let state = createState("idle", "", [], -1, "");

  function input(value) {
    if (destroyed) return false;
    const query = String(value ?? "").trim();
    const requestSequence = invalidatePendingWork();

    if (!query) {
      publish(createState("idle", "", [], -1, ""));
      return true;
    }

    const cacheKey = query.toLowerCase();
    const cachedItems = readCache(cache, cacheKey);
    if (cachedItems !== null) {
      publish(resultState(query, cachedItems));
      return true;
    }

    publish(createState("loading", query, [], -1, ""));
    if (!isSequenceCurrent(requestSequence)) return false;
    debounceTimer = setTimeout(() => {
      debounceTimer = null;
      void search(query, cacheKey, requestSequence);
    }, settings.debounceMs);
    return true;
  }

  function move(delta) {
    if (destroyed || state.phase !== "ready" || state.items.length === 0) return -1;
    if (!Number.isInteger(delta) || delta === 0) return state.activeIndex;

    let nextIndex;
    if (state.activeIndex < 0) {
      nextIndex = delta > 0 ? 0 : state.items.length - 1;
    } else {
      nextIndex = clamp(state.activeIndex + delta, 0, state.items.length - 1);
    }
    if (nextIndex !== state.activeIndex) {
      publish(createState("ready", state.query, state.items, nextIndex, ""));
    }
    return nextIndex;
  }

  function selectActive() {
    return selectIndex(state.activeIndex);
  }

  function selectIndex(index) {
    if (
      destroyed ||
      state.phase !== "ready" ||
      !Number.isInteger(index) ||
      index < 0 ||
      index >= state.items.length
    ) {
      return null;
    }

    const selected = cloneStock(state.items[index]);
    close();
    if (!destroyed) settings.onSelect(selected.symbol, cloneStock(selected));
    return selected;
  }

  function close() {
    if (destroyed) return false;
    invalidatePendingWork();
    publish(createState("closed", state.query, [], -1, ""));
    return true;
  }

  function destroy() {
    if (destroyed) return false;
    destroyed = true;
    sequence += 1;
    clearDebounceTimer();
    abortRequest();
    cache.clear();
    state = createState("closed", state.query, [], -1, "");
    return true;
  }

  async function search(query, cacheKey, requestSequence) {
    if (!isSequenceCurrent(requestSequence)) return;
    const scope = createRequestScope(requestScope);
    requestScope = scope;
    try {
      const payload = await fetchJson(
        `/api/stocks?keyword=${encodeURIComponent(query)}&limit=${STOCK_SEARCH_LIMIT}`,
        { signal: scope.signal, timeoutMs: settings.timeoutMs }
      );
      if (!ownsRequest(scope, requestSequence)) return;
      const items = validateStockItems(payload);
      writeCache(cache, cacheKey, items, settings.cacheSize);
      publish(resultState(query, items));
    } catch (error) {
      if (isAbortError(error) || !ownsRequest(scope, requestSequence)) return;
      publish(createState("unavailable", query, [], -1, unavailableMessage(error)));
    } finally {
      if (requestScope === scope) requestScope = null;
      scope.dispose();
    }
  }

  function invalidatePendingWork() {
    sequence += 1;
    clearDebounceTimer();
    abortRequest();
    return sequence;
  }

  function clearDebounceTimer() {
    if (debounceTimer === null) return;
    clearTimeout(debounceTimer);
    debounceTimer = null;
  }

  function abortRequest() {
    if (!requestScope) return;
    const scope = requestScope;
    requestScope = null;
    scope.abort();
  }

  function isSequenceCurrent(requestSequence) {
    return !destroyed && sequence === requestSequence;
  }

  function ownsRequest(scope, requestSequence) {
    return isSequenceCurrent(requestSequence) && requestScope === scope && !scope.signal.aborted;
  }

  function publish(nextState) {
    if (destroyed) return;
    state = nextState;
    settings.onState(stateSnapshot(state));
  }

  return { input, move, selectActive, selectIndex, close, destroy };
}

function normalizeOptions(options) {
  if (!options || typeof options !== "object" || Array.isArray(options)) {
    throw new TypeError("Stock search options must be an object");
  }
  if (options.onState !== undefined && typeof options.onState !== "function") {
    throw new TypeError("onState must be a function");
  }
  if (options.onSelect !== undefined && typeof options.onSelect !== "function") {
    throw new TypeError("onSelect must be a function");
  }
  return {
    onState: options.onState || (() => {}),
    onSelect: options.onSelect || (() => {}),
    debounceMs: nonNegativeNumber(options.debounceMs, DEFAULT_STOCK_SEARCH_DEBOUNCE_MS),
    cacheSize: nonNegativeInteger(options.cacheSize, DEFAULT_STOCK_SEARCH_CACHE_SIZE),
    timeoutMs: nonNegativeNumber(options.timeoutMs, DEFAULT_REQUEST_TIMEOUT_MS),
  };
}

function validateStockItems(payload) {
  if (!Array.isArray(payload) || payload.length > STOCK_SEARCH_LIMIT) {
    throw new TypeError("Invalid stock search response");
  }
  return payload.map(validateStockItem);
}

function validateStockItem(item) {
  if (!isPlainObject(item) || REQUIRED_STOCK_FIELDS.some((field) => !hasOwn(item, field))) {
    throw new TypeError("Invalid stock search item");
  }
  const { symbol, code, market, name } = item;
  const match = typeof symbol === "string" ? STOCK_SYMBOL_PATTERN.exec(symbol) : null;
  const normalizedName = boundedText(name, 80);
  if (
    !match ||
    match[1] === "000000" ||
    typeof code !== "string" ||
    typeof market !== "string" ||
    !normalizedName ||
    code !== match[1] ||
    market !== match[2]
  ) {
    throw new TypeError("Invalid stock search item");
  }
  return {
    symbol,
    code,
    market,
    name: normalizedName,
    industry: optionalBoundedText(item.industry, 80),
    source: optionalBoundedText(item.source, 120),
    updated_at: optionalBoundedText(item.updated_at, 40),
  };
}

function boundedText(value, maxLength) {
  if (typeof value !== "string") return "";
  const text = value.trim();
  return text && text.length <= maxLength ? text : "";
}

function optionalBoundedText(value, maxLength) {
  if (value === null || value === undefined) return null;
  const text = boundedText(value, maxLength);
  if (!text) throw new TypeError("Invalid stock search item");
  return text;
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasOwn(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function resultState(query, items) {
  const phase = items.length > 0 ? "ready" : "empty";
  return createState(phase, query, items, -1, "");
}

function createState(phase, query, items, activeIndex, message) {
  return { phase, query, items: cloneItems(items), activeIndex, message };
}

function stateSnapshot(state) {
  return createState(state.phase, state.query, state.items, state.activeIndex, state.message);
}

function cloneItems(items) {
  return items.map(cloneStock);
}

function cloneStock(item) {
  return { ...item };
}

function readCache(cache, key) {
  if (!cache.has(key)) return null;
  const items = cache.get(key);
  cache.delete(key);
  cache.set(key, items);
  return cloneItems(items);
}

function writeCache(cache, key, items, cacheSize) {
  if (cacheSize === 0) return;
  cache.delete(key);
  cache.set(key, cloneItems(items));
  while (cache.size > cacheSize) {
    cache.delete(cache.keys().next().value);
  }
}

function unavailableMessage(error) {
  const detail = error && typeof error.message === "string" ? error.message.trim() : "";
  return detail ? `${UNAVAILABLE_MESSAGE}: ${detail}` : UNAVAILABLE_MESSAGE;
}

function nonNegativeNumber(value, fallback) {
  if (value === undefined) return fallback;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : fallback;
}

function nonNegativeInteger(value, fallback) {
  const number = nonNegativeNumber(value, fallback);
  return Math.floor(number);
}

function clamp(value, minimum, maximum) {
  return Math.min(Math.max(value, minimum), maximum);
}
