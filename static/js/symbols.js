export const UI_SYMBOL_ERROR_MESSAGE = "股票代码应为6位数字且不能全为0，例如 600519 或 000001";

const UI_MARKETS = Object.freeze(["SH", "SZ", "BJ"]);
const BJ_STOCK_PREFIXES = Object.freeze(["43", "83", "87", "88", "92"]);
const DEFAULT_SH_PREFIXES = Object.freeze(["5", "6", "9"]);

export function normalizeUiSymbol(symbol) {
  const raw = normalizedUiSymbolText(symbol);
  try {
    return parsedUiSymbol(raw);
  } catch (error) {
    return raw;
  }
}

export function validateUiSymbol(symbol) {
  return parsedUiSymbol(normalizedUiSymbolText(symbol));
}

function parsedUiSymbol(raw) {
  const cleaned = raw.replaceAll("-", "");
  const suffix = marketSuffix(cleaned);
  const withoutSuffix = suffix ? cleaned.slice(0, -(suffix.length + 1)) : cleaned;
  const prefix = marketPrefix(withoutSuffix);
  const code = prefix ? withoutSuffix.slice(prefix.length + (withoutSuffix[prefix.length] === "." ? 1 : 0)) : withoutSuffix;
  if (!/^\d{6}$/.test(code) || code === "000000") throw new Error(UI_SYMBOL_ERROR_MESSAGE);
  if (prefix && suffix && prefix !== suffix) throw new Error(`股票代码的市场前后缀冲突：${prefix} / ${suffix}`);
  const inferredMarket = inferredUiMarket(code);
  const explicitMarket = prefix || suffix;
  const explicitIsBeijing = explicitMarket === "BJ";
  const inferredIsBeijing = inferredMarket === "BJ";
  if (explicitMarket && explicitIsBeijing !== inferredIsBeijing) {
    throw new Error(`股票代码 ${code} 与市场标识 ${explicitMarket} 不一致`);
  }
  return `${code}.${explicitMarket || inferredMarket}`;
}

function marketPrefix(value) {
  return UI_MARKETS.find((market) => value.startsWith(market)) || "";
}

function marketSuffix(value) {
  return UI_MARKETS.find((market) => value.endsWith(`.${market}`)) || "";
}

function inferredUiMarket(code) {
  if (BJ_STOCK_PREFIXES.some((prefix) => code.startsWith(prefix))) return "BJ";
  return DEFAULT_SH_PREFIXES.some((prefix) => code.startsWith(prefix)) ? "SH" : "SZ";
}

function normalizedUiSymbolText(symbol) {
  return String(symbol || "").trim().toUpperCase();
}
