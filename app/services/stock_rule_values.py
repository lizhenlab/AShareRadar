from __future__ import annotations

from app.services.stock_rule_contracts import RULE_CONFIDENCE, STATUS_CLOSE, STATUS_MATCHED, STATUS_MISSED
from app.utils.market_data import finite_float


def _status_from_flags(hit: bool, close: bool) -> str:
    if hit:
        return STATUS_MATCHED
    if close:
        return STATUS_CLOSE
    return STATUS_MISSED


def _positive_price_level(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _positive_metric(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _non_negative_score(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _positive_score(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _price_level_evidence(label: str, value: object) -> str:
    price_level = _positive_price_level(value)
    return f"{label} {price_level:.2f}" if price_level is not None else f"{label} 缺失"


def _score_evidence(label: str, value: object, *, positive: bool = False) -> str:
    score = _positive_score(value) if positive else _non_negative_score(value)
    return f"{label} {score:g}" if score is not None else f"{label} 缺失"


def _missing_data_items(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _rule_confidence(rule_id: str, status: str) -> int:
    return int(RULE_CONFIDENCE[rule_id][status])
