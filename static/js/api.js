export async function fetchJson(url, options = {}) {
  const response = await fetchResponse(url, options);
  if (!response.ok) {
    const error = await errorPayload(response);
    throw new Error(errorMessage(error, response.status));
  }
  try {
    return await response.json();
  } catch {
    throw new Error("响应数据格式异常");
  }
}

async function fetchResponse(url, options) {
  try {
    return await fetch(url, options);
  } catch {
    throw new Error("网络连接失败，请检查本地服务是否可用");
  }
}

async function errorPayload(response) {
  if (response && typeof response.json === "function") {
    const error = await response.json().catch(() => null);
    if (error !== null && error !== undefined) return error;
  }
  return {};
}

function errorMessage(error, status) {
  if (error && typeof error === "object" && "detail" in error) {
    const message = detailMessage(error.detail);
    if (message) return message;
  }
  return defaultErrorMessage(status);
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
