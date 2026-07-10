export async function fetchJson(url, options = {}) {
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
  } catch {
    throw new Error("网络连接失败，请检查本地服务是否可用");
  }
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
