const ERROR_MESSAGE_RULES = [
  {
    matches: (text) => text.includes("ProxyError") || text.includes("Unable to connect to proxy"),
    message: "网络代理连接失败，相关模块已按保守口径暂停。",
  },
  {
    matches: (text) => text.includes("HTTPSConnectionPool") || text.includes("Max retries exceeded"),
    message: "行情接口连接失败，稍后刷新或切换数据源。",
  },
  {
    matches: (text) => text.includes("BaoStock登录失败"),
    message: "BaoStock 登录失败，通常是源站网络波动或连接受限。",
  },
  {
    matches: (text) => text.includes("Futu OpenAPI 未启用"),
    message: "Futu OpenAPI 未启用，当前不会参与分析。",
  },
];

export function compactErrorMessage(message) {
  const text = String(message ?? "");
  for (const rule of ERROR_MESSAGE_RULES) {
    if (rule.matches(text)) return rule.message;
  }
  if (text.length > 120) return `${text.slice(0, 120)}...`;
  return text ? text : "请求失败";
}
