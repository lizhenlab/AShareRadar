from __future__ import annotations


SUPPORTED_MARKETS = {"sh", "sz"}
DEFAULT_SH_PREFIXES = ("5", "6", "9")
SYMBOL_ERROR_MESSAGE = "股票代码应为6位数字且不能全为0，例如 600519 或 000001"


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


def tencent_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{market}{code}"
