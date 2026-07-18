export const DEFAULT_REQUEST_TIMEOUT_MS = 12000;
export const GLOBAL_DATA_TTL_MS = 15000;

const cachedJsonRequests = new Map();

export async function fetchJson(url, options = {}) {
  const request = timedRequest(options);
  try {
    if (request.signal && request.signal.aborted) throw abortError();
    return await abortable(fetchJsonResponse(url, request.options), request.signal);
  } catch (error) {
    if (request.didTimeout()) throw new Error("请求超时，请稍后重试");
    throw error;
  } finally {
    request.dispose();
  }
}

export function fetchCachedJson(url, options = {}) {
  const {
    cacheKey = String(url),
    force = false,
    signal,
    ttlMs = GLOBAL_DATA_TTL_MS,
    validate,
    ...fetchOptions
  } = options || {};
  const record = cachedJsonRecord(cacheKey);
  if (!force && cachedJsonIsFresh(record, ttlMs)) {
    return abortable(Promise.resolve(record.value), signal);
  }
  if (record.inflight && !record.inflight.scope.signal.aborted) {
    return abortable(record.inflight.promise, signal);
  }

  const generation = record.generation;
  const scope = createRequestScope();
  const inflight = { generation, scope, promise: null };
  const promise = fetchJson(url, { ...fetchOptions, signal: scope.signal })
    .then((value) => {
      const validatedValue = typeof validate === "function" ? validate(value) : value;
      if (record.generation === generation) {
        record.hasValue = true;
        record.value = validatedValue;
        record.updatedAt = Date.now();
      }
      return validatedValue;
    })
    .finally(() => {
      if (record.inflight === inflight) record.inflight = null;
      scope.dispose();
    });
  inflight.promise = promise;
  record.inflight = inflight;
  return abortable(promise, signal);
}

export function invalidateCachedJson(url, options = {}) {
  const record = cachedJsonRequests.get(String(url));
  if (!record) return false;
  record.generation += 1;
  record.updatedAt = 0;
  if (options.abortInflight !== false) abortCachedJsonInflight(record);
  return true;
}

export function cancelCachedJsonRequest(url) {
  const record = cachedJsonRequests.get(String(url));
  if (!record || !record.inflight) return false;
  record.generation += 1;
  abortCachedJsonInflight(record);
  return true;
}

export function getCachedJsonSnapshot(url) {
  const record = cachedJsonRequests.get(String(url));
  if (!record || !record.hasValue) return { found: false, value: undefined, updatedAt: 0 };
  return { found: true, value: record.value, updatedAt: record.updatedAt };
}

export function clearCachedJsonRequests() {
  cachedJsonRequests.forEach((record) => abortCachedJsonInflight(record));
  cachedJsonRequests.clear();
}

function cachedJsonRecord(key) {
  if (!cachedJsonRequests.has(key)) {
    cachedJsonRequests.set(key, {
      generation: 0,
      hasValue: false,
      inflight: null,
      updatedAt: 0,
      value: undefined,
    });
  }
  return cachedJsonRequests.get(key);
}

function cachedJsonIsFresh(record, ttlMs) {
  const ttl = Number(ttlMs);
  return record.hasValue && Number.isFinite(ttl) && ttl > 0 && Date.now() - record.updatedAt < ttl;
}

function abortCachedJsonInflight(record) {
  const inflight = record.inflight;
  if (!inflight) return;
  record.inflight = null;
  inflight.scope.abort();
}

export function isAbortError(error) {
  return Boolean(error && (error.name === "AbortError" || error.code === "ABORT_ERR"));
}

export function createRequestScope(previousScope = null, parentSignal = null) {
  if (previousScope && typeof previousScope.abort === "function") previousScope.abort();
  const controller = new AbortController();
  let disposed = false;
  const abortFromParent = () => controller.abort();
  if (parentSignal) {
    if (parentSignal.aborted) {
      controller.abort();
    } else {
      parentSignal.addEventListener("abort", abortFromParent, { once: true });
    }
  }
  return {
    signal: controller.signal,
    abort() {
      if (!controller.signal.aborted) controller.abort();
      this.dispose();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      if (parentSignal) parentSignal.removeEventListener("abort", abortFromParent);
    },
  };
}

async function fetchJsonResponse(url, options) {
  const response = await fetchResponse(url, options);
  if (!response.ok) {
    const error = await errorPayload(response);
    throw new Error(errorMessage(error, response.status));
  }
  return successPayload(response);
}

async function fetchResponse(url, options) {
  try {
    return await fetch(url, options);
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new Error("网络连接失败，请检查本地服务是否可用");
  }
}

function timedRequest(options) {
  const { timeoutMs = 0, ...fetchOptions } = options || {};
  const duration = Number(timeoutMs);
  if (!Number.isFinite(duration) || duration <= 0) {
    return passiveRequest(fetchOptions);
  }
  const sourceSignal = fetchOptions.signal;
  const controller = new AbortController();
  let timedOut = false;
  const abortFromSource = () => controller.abort();
  if (sourceSignal) {
    if (sourceSignal.aborted) controller.abort();
    else sourceSignal.addEventListener("abort", abortFromSource, { once: true });
  }
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, duration);
  return {
    options: { ...fetchOptions, signal: controller.signal },
    signal: controller.signal,
    didTimeout: () => timedOut,
    dispose() {
      clearTimeout(timer);
      if (sourceSignal) sourceSignal.removeEventListener("abort", abortFromSource);
    },
  };
}

function passiveRequest(options) {
  return {
    options,
    signal: options.signal,
    didTimeout: () => false,
    dispose() {},
  };
}

function abortable(promise, signal) {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(abortError());
    signal.addEventListener("abort", onAbort, { once: true });
    Promise.resolve(promise).then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      }
    );
  });
}

function abortError() {
  const error = new Error("请求已取消");
  error.name = "AbortError";
  return error;
}

async function errorPayload(response) {
  const text = await responseText(response);
  if (text !== null) return parseErrorText(text);
  if (response && typeof response.json === "function") {
    const error = await response.json().catch(() => null);
    if (error !== null && error !== undefined) return error;
  }
  return {};
}

function errorMessage(error, status) {
  if (typeof error === "string" && error.trim()) return error.trim();
  if (error && typeof error === "object" && "detail" in error) {
    const message = detailMessage(error.detail);
    if (message) return message;
  }
  if (error && typeof error === "object") {
    for (const field of ["message", "error"]) {
      const message = detailMessage(error[field]);
      if (message) return message;
    }
  }
  return defaultErrorMessage(status);
}

async function successPayload(response) {
  if (emptyResponse(response)) return null;
  const text = await responseText(response);
  if (text !== null) return parseJsonText(text);
  try {
    return await response.json();
  } catch {
    throw new Error("响应数据格式异常");
  }
}

function parseJsonText(text) {
  if (!text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch {
    throw new Error("响应数据格式异常");
  }
}

function parseErrorText(text) {
  if (!text.trim()) return {};
  try {
    return JSON.parse(text);
  } catch {
    return readableTextPayload(text) ? text.trim() : {};
  }
}

async function responseText(response) {
  if (!response || typeof response.text !== "function") return null;
  try {
    return await response.text();
  } catch {
    return null;
  }
}

function emptyResponse(response) {
  return response && (response.status === 204 || response.status === 205 || contentLength(response) === "0");
}

function contentLength(response) {
  const headers = response && response.headers;
  return headers && typeof headers.get === "function" ? headers.get("content-length") : null;
}

function readableTextPayload(text) {
  const trimmed = typeof text === "string" ? text.trim() : "";
  return Boolean(trimmed) && !trimmed.startsWith("<");
}

function detailMessage(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(detailMessage).filter(Boolean).join("；");
  if (detail && typeof detail === "object") {
    if ("msg" in detail) {
      const location = Array.isArray(detail.loc) ? detail.loc.join(".") : detail.loc;
      return [location, detail.msg].filter(Boolean).join(": ");
    }
    return stringifyDetail(detail);
  }
  return "";
}

function stringifyDetail(detail) {
  try {
    return JSON.stringify(detail);
  } catch {
    return String(detail);
  }
}

function defaultErrorMessage(status) {
  return status ? `请求失败（HTTP ${status}）` : "请求失败";
}
