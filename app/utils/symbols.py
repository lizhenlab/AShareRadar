from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


SUPPORTED_MARKETS = {"sh", "sz"}
DEFAULT_SH_PREFIXES = ("5", "6", "9")
SYMBOL_ERROR_MESSAGE = "股票代码应为6位数字且不能全为0，例如 600519 或 000001"


@dataclass(frozen=True)
class SymbolListResult:
    symbols: list[str]
    skipped_count: int = 0


@dataclass
class _SymbolListBuilder:
    skip_invalid: bool
    max_items: int | None
    truncate: bool
    count_duplicates_as_skipped: bool
    symbols: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    skipped_count: int = 0
    stopped: bool = False

    def add(self, raw_symbol: object) -> None:
        symbol = _standard_symbol_from_raw(raw_symbol, skip_invalid=self.skip_invalid)
        if symbol is None:
            self.skipped_count += 1
            return
        if symbol in self.seen:
            self._skip_duplicate()
            return
        self._append(symbol)

    def result(self) -> SymbolListResult:
        if self.max_items is not None and len(self.symbols) > self.max_items:
            raise ValueError(f"一次最多查询 {self.max_items} 个股票代码")
        return SymbolListResult(symbols=self.symbols, skipped_count=self.skipped_count)

    def _append(self, symbol: str) -> None:
        self.seen.add(symbol)
        self.symbols.append(symbol)
        if self.truncate and self.max_items is not None and len(self.symbols) == self.max_items:
            self.stopped = True

    def _skip_duplicate(self) -> None:
        if self.count_duplicates_as_skipped:
            self.skipped_count += 1


def normalize_symbol(symbol: str) -> tuple[str, str]:
    cleaned = _clean_symbol(symbol)
    suffix_market, without_suffix = _split_suffix_market(cleaned)
    prefix_market, code_text = _split_prefix_market(without_suffix)
    if "." in code_text:
        raise ValueError(SYMBOL_ERROR_MESSAGE)
    if prefix_market and suffix_market and prefix_market != suffix_market:
        raise ValueError(SYMBOL_ERROR_MESSAGE)
    code = _validated_symbol_code(code_text)
    return code, prefix_market or suffix_market or _infer_market(code)


def _clean_symbol(symbol: str) -> str:
    return symbol.strip().lower().replace("-", "")


def _split_suffix_market(value: str) -> tuple[str, str]:
    for suffix in SUPPORTED_MARKETS:
        token = f".{suffix}"
        if value.endswith(token):
            return suffix, value[: -len(token)]
    return "", value


def _split_prefix_market(value: str) -> tuple[str, str]:
    for prefix in SUPPORTED_MARKETS:
        dotted = f"{prefix}."
        if value.startswith(dotted):
            return prefix, value[len(dotted) :]
        if value.startswith(prefix):
            return prefix, value[len(prefix) :]
    return "", value


def _validated_symbol_code(value: str) -> str:
    if not value.isdigit() or len(value) != 6 or value == "000000":
        raise ValueError(SYMBOL_ERROR_MESSAGE)
    return value


def _infer_market(code: str) -> str:
    return "sh" if code.startswith(DEFAULT_SH_PREFIXES) else "sz"


def standard_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{code}.{market.upper()}"


def standard_symbol_list(
    symbols: Iterable[object],
    *,
    skip_invalid: bool = False,
    max_items: int | None = None,
    truncate: bool = False,
    count_duplicates_as_skipped: bool = False,
) -> SymbolListResult:
    builder = _SymbolListBuilder(
        skip_invalid=skip_invalid,
        max_items=max_items,
        truncate=truncate,
        count_duplicates_as_skipped=count_duplicates_as_skipped,
    )
    for raw_symbol in symbols:
        builder.add(raw_symbol)
        if builder.stopped:
            break
    return builder.result()


def _standard_symbol_from_raw(raw_symbol: object, *, skip_invalid: bool) -> str | None:
    try:
        if raw_symbol is None:
            raise ValueError(SYMBOL_ERROR_MESSAGE)
        text = str(raw_symbol).strip()
        if not text:
            raise ValueError(SYMBOL_ERROR_MESSAGE)
        return standard_symbol(text)
    except (AttributeError, TypeError, ValueError):
        if skip_invalid:
            return None
        raise


def tencent_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{market}{code}"
