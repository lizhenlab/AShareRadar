import { compactErrorMessage } from "./errors.js";
import { validateUiSymbol } from "./symbols.js";

const DEFAULT_OBSERVED_SYMBOLS = ["600519", "000001", "300750", "002594", "600036"];

export function quoteStreamSymbols(symbol, items, isExcluded) {
  const watchlist = Array.isArray(items) ? items : [];
  const excluded = new Set(watchlist.filter(isExcluded).map((item) => canonicalSymbol(item?.symbol)).filter(Boolean));
  const watched = watchlist.filter((item) => !isExcluded(item)).map((item) => item?.symbol);
  const active = canonicalSymbol(symbol);
  const observed = [...watched, ...DEFAULT_OBSERVED_SYMBOLS]
    .map(canonicalSymbol)
    .filter((item) => item && !excluded.has(item) && item !== active);
  return [...new Set([active, ...observed].filter(Boolean))].slice(0, 8);
}

export function parseQuoteStreamFrame(event) {
  let rows;
  try {
    rows = JSON.parse(event.data);
  } catch (error) {
    return { rows: null, error: "观察报价流数据异常，等待下一次刷新" };
  }
  if (!Array.isArray(rows)) return { rows: null, error: "观察报价流数据格式异常，等待下一次刷新" };
  if (!rows.every(isValidQuoteRow)) return { rows: null, error: "观察报价流帧含无效数据，已保留上一帧" };
  return { rows, error: "" };
}

export function quoteStreamErrorMessage(event) {
  if (event && typeof event.data === "string") {
    try {
      const payload = JSON.parse(event.data);
      return `观察报价流暂不可用：${compactErrorMessage(payload.message || "暂不可用")}`;
    } catch (error) {
      return "观察报价流暂不可用";
    }
  }
  return "观察报价流暂不可用";
}

function canonicalSymbol(symbol) {
  try {
    return validateUiSymbol(symbol);
  } catch (error) {
    return "";
  }
}

function isValidQuoteRow(item) {
  if (!item || typeof item !== "object" || Array.isArray(item)) return false;
  return typeof item.name === "string" && Boolean(item.name.trim())
    && validQuoteSymbol(item.code, item.market)
    && [item.price, item.change_pct, item.amount].every((value) => typeof value === "number" && Number.isFinite(value));
}

function validQuoteSymbol(code, market) {
  if (typeof code !== "string" || typeof market !== "string") return false;
  const symbol = `${code}.${market}`;
  return canonicalSymbol(symbol) === symbol;
}
