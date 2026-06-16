from __future__ import annotations


def normalize_symbol(symbol: str) -> tuple[str, str]:
    normalized = symbol.strip().lower().replace("-", "")
    suffix_market = ""
    if normalized.endswith((".sh", ".sz")):
        suffix_market = normalized[-2:]
        normalized = normalized[:-3]
    normalized = normalized.replace(".", "")
    prefix_market = normalized[:2] if normalized[:2] in {"sh", "sz"} else ""
    raw = normalized.removeprefix("sh").removeprefix("sz")
    if not raw.isdigit() or len(raw) != 6:
        raise ValueError("股票代码应为6位数字，例如 600519 或 000001")
    market = prefix_market or suffix_market or ("sh" if raw.startswith(("5", "6", "9")) else "sz")
    return raw, market


def standard_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{code}.{market.upper()}"


def tencent_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{market}{code}"
