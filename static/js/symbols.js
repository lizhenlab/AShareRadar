export const UI_SYMBOL_ERROR_MESSAGE = "股票代码应为6位数字且不能全为0，例如 600519 或 000001";

export function normalizeUiSymbol(symbol) {
  const raw = String(symbol || "").trim().toUpperCase();
  const digits = raw.replace(/^SH|^SZ/, "").replace(/\.(SH|SZ)$/, "");
  if (!/^\d{6}$/.test(digits)) return raw;
  if (digits === "000000") return raw;
  const market = raw.endsWith(".SZ") || raw.startsWith("SZ") || (!digits.startsWith("5") && !digits.startsWith("6") && !digits.startsWith("9")) ? "SZ" : "SH";
  return `${digits}.${market}`;
}

export function validateUiSymbol(symbol) {
  const raw = String(symbol || "").trim().toUpperCase();
  const digits = raw.replace(/^SH|^SZ/, "").replace(/\.(SH|SZ)$/, "");
  if (!/^\d{6}$/.test(digits) || digits === "000000") {
    throw new Error(UI_SYMBOL_ERROR_MESSAGE);
  }
  return normalizeUiSymbol(raw);
}
