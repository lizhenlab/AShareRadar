"""Small strict parsers for externally supplied financial records."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import math
import re
from typing import cast

from app.models.fuyao import FinancialFact
from app.services.fuyao_financials_fields import INDICATOR_ABILITIES, INDICATOR_LABELS
from app.utils.clock import ASHARE_TIMEZONE
from app.utils.symbols import standard_a_share_stock_symbol


def financial_object(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"invalid financial {label}")
    return dict(raw)


def financial_time(raw: object) -> datetime:
    if not isinstance(raw, str) or not re.match(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", raw):
        raise ValueError("financial fetched_at requires a full ISO timestamp")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("invalid financial fetched_at") from exc
    value = value.replace(tzinfo=ASHARE_TIMEZONE) if value.tzinfo is None else value.astimezone(ASHARE_TIMEZONE)
    if value.year < 1900:
        raise ValueError("invalid financial timestamp year")
    return value


def financial_ms(raw: object, fetched: datetime, label: str) -> datetime:
    if type(raw) is not int or raw <= 0:
        raise ValueError(f"invalid financial {label} milliseconds")
    try:
        value = datetime.fromtimestamp(raw / 1000, ASHARE_TIMEZONE)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"invalid financial {label} milliseconds") from exc
    if value.year < 1900 or value > fetched:
        raise ValueError(f"financial {label} is outside the observed time range")
    return value


def financial_identity(row: Mapping[str, object], symbol: str) -> None:
    raw = row.get("thscode")
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", raw):
        raise ValueError("financial response requires an explicit thscode")
    if standard_a_share_stock_symbol(raw) != symbol:
        raise ValueError("financial response belongs to a different stock")
    if "ticker" in row and row["ticker"] != symbol.split(".", 1)[0]:
        raise ValueError("financial ticker and thscode disagree")


def financial_number(raw: object) -> float | None:
    if raw is None:
        return None
    if type(raw) not in (int, float):
        raise ValueError("financial statement number must be numeric or null")
    try:
        value = float(cast(float, raw))
    except OverflowError as exc:
        raise ValueError("non-finite financial number") from exc
    if not math.isfinite(value):
        raise ValueError("non-finite financial number")
    return value


def financial_payload(raw: object) -> dict[str, object]:
    envelope = financial_object(raw, "envelope")
    if type(envelope.get("code")) is not int or envelope["code"] != 0:
        raise ValueError("financial API business result is not successful")
    return financial_object(envelope.get("data"), "data")


def financial_indicator(raw: object, ability: str) -> FinancialFact:
    item = financial_object(raw, "indicator")
    key, raw_value = item.get("index_id"), item.get("value")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,160}", key):
        raise ValueError("invalid financial indicator identity")
    if raw_value is not None and (not isinstance(raw_value, str) or len(raw_value) > 256):
        raise ValueError("financial indicator value must be bounded text or null")
    value, unit = _indicator_value(raw_value)
    return FinancialFact(key=key, label=INDICATOR_LABELS.get(key, key), value=value, raw_value=raw_value,
                         unit=unit, source_kind="indicators", ability=ability)


def _indicator_value(raw: str | None) -> tuple[float | None, str | None]:
    if raw is None:
        return None, None
    text = raw.strip()
    if text.rstrip("%％").lower() in {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "true", "false"}:
        raise ValueError("invalid financial indicator number")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[%％]?", text):
        return None, None
    percentage = text.endswith(("%", "％"))
    value = float(text[:-1] if percentage else text)
    if not math.isfinite(value):
        raise ValueError("non-finite financial indicator number")
    return value, "%" if percentage else None


def financial_indicators(data: Mapping[str, object]) -> list[FinancialFact]:
    abilities = data.get("abilities")
    if not isinstance(abilities, list) or len(abilities) > 5:
        raise ValueError("financial abilities must be a bounded array")
    output: list[FinancialFact] = []
    seen: set[str] = set()
    for raw in abilities:
        block = financial_object(raw, "ability")
        name, items = block.get("ability"), block.get("indicators")
        if not isinstance(name, str) or name not in INDICATOR_ABILITIES or name in seen:
            raise ValueError("invalid or repeated financial ability")
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("financial indicators must be a bounded array")
        seen.add(name)
        output.extend(financial_indicator(item, name) for item in items)
    if len({item.key for item in output}) != len(output):
        raise ValueError("duplicate financial indicator identity")
    return output
