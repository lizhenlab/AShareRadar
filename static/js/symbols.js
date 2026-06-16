export function normalizeUiSymbol(symbol) {
  const raw = String(symbol || "").trim().toUpperCase();
  const digits = raw.replace(/^SH|^SZ/, "").replace(/\.(SH|SZ)$/, "");
  if (!/^\d{6}$/.test(digits)) return raw;
  const market = raw.endsWith(".SZ") || raw.startsWith("SZ") || (!digits.startsWith("5") && !digits.startsWith("6") && !digits.startsWith("9")) ? "SZ" : "SH";
  return `${digits}.${market}`;
}
