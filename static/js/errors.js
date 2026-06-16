export function compactErrorMessage(message) {
  const text = String(message || "");
  if (text.includes("ProxyError") || text.includes("Unable to connect to proxy")) return "网络代理连接失败，相关模块已按保守口径暂停。";
  if (text.includes("HTTPSConnectionPool") || text.includes("Max retries exceeded")) return "行情接口连接失败，稍后刷新或切换数据源。";
  if (text.includes("BaoStock登录失败")) return "BaoStock 登录失败，通常是源站网络波动或连接受限。";
  if (text.includes("Futu OpenAPI 未启用")) return "Futu OpenAPI 未启用，当前不会参与分析。";
  if (text.length > 120) return `${text.slice(0, 120)}...`;
  return text || "请求失败";
}
