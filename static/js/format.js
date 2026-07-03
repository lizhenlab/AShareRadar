export function formatNumber(value, digits = 2) {
  if (value === null || value === undefined) return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toFixed(normalizeDigits(digits));
}

export function formatAmount(value) {
  if (value === null || value === undefined) return "--";
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "--";
  if (number >= 100000000) return `${formatNumber(number / 100000000, 1)}亿`;
  if (number >= 10000) return `${formatNumber(number / 10000, 1)}万`;
  return formatNumber(number, 0);
}

export function changeClass(value) {
  if (value === null || value === undefined) return "neutral";
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "neutral";
  return number > 0 ? "up" : "down";
}

export function toneByScore(score, goodAt = 70, riskAt = 45) {
  const value = Number(score);
  if (!Number.isFinite(value)) return "";
  if (value >= goodAt) return "good";
  if (value <= riskAt) return "risk";
  return "warn";
}

export function toneByText(text) {
  const value = String(text || "");
  if (/(积极|较好|偏强|顺风|共振|可控|正常|良好|优秀)/.test(value)) return "good";
  if (/(风险|不足|偏弱|压制|破位|不可用|失败|缺失|异常|暂停)/.test(value)) return "risk";
  if (/(等待|观察|确认|一般|待)/.test(value)) return "warn";
  return "";
}

function normalizeDigits(digits) {
  const value = Number(digits);
  if (!Number.isInteger(value)) return 2;
  return Math.max(0, Math.min(20, value));
}
