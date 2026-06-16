export async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "请求失败" }));
    throw new Error(error.detail || "请求失败");
  }
  return response.json();
}
