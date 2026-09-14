"""Score explicit TTM/MRQ observations with frozen time and no provider access."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import re
from typing import Any

from app.models.fuyao_research import FuyaoObservation
from app.models.fuyao_scoring import FuyaoScoreContribution, FuyaoValuationScore
from app.services.fuyao_observations import batch_timestamp, canonical_stock, finite_or_none
from app.services.valuation_thresholds import (
    FUNDAMENTAL_BASE_SCORE, HIGH_PB_THRESHOLD, HIGH_PE_THRESHOLD, LOW_PB_THRESHOLD, LOW_PE_THRESHOLD,
    PB_SCORE_ADJUSTMENT, PE_SCORE_ADJUSTMENT,
)
from app.utils.audit_time import audit_datetime_to_text, parse_audit_time


MAX_OBSERVATION_AGE = timedelta(days=7)
VALUATION_SCORE_WARNINGS = [
    "沿用项目固定估值阈值；分数越高仅表示该规则下估值压力较低，不是财务健康、上涨概率或预期收益。",
    "七日限制约束本地获取时间；供应商批次时间不证明每项指标同时更新。",
    "当前观察仅用于个股研究；不与行情源历史PE/PB或同行样本混算，不回填全市场历史排名。",
    "未按行业校准；亏损、负净资产及缺失指标须结合原值解释。",
]


class FuyaoScoreInputError(ValueError):
    """A bounded, user-facing reason for excluding an observation from scoring."""


def build_fuyao_valuation_score(
    symbol: str, observation: FuyaoObservation | None, evaluated_at: str,
) -> FuyaoValuationScore:
    symbol = canonical_stock(symbol)
    cutoff = _score_time(evaluated_at)
    result = FuyaoValuationScore(symbol=symbol, evaluated_at=audit_datetime_to_text(cutoff),
                                 unavailable_reason="当前股票没有扶摇估值缓存", warnings=list(VALUATION_SCORE_WARNINGS))
    if observation is None:
        return result
    metadata = {"fetched_at": observation.fetched_at, "source": observation.source,
                "observation_id": observation.id, "observation_digest": observation.digest}
    try:
        values = _admitted_values(symbol, observation, cutoff)
        components = [_score_component("pe_ttm", values.get("pe_ttm")), _score_component("pb_mrq", values.get("pb_mrq"))]
    except FuyaoScoreInputError as exc:
        return result.model_copy(update={**metadata, "unavailable_reason": str(exc)})
    except (ValueError, TypeError, OverflowError):
        return result.model_copy(update={**metadata, "unavailable_reason": "扶摇估值缓存未通过身份、摘要、时间或数值校验"})
    available = any(item.value is not None and item.value != 0 for item in components)
    missing = [f"{item.label}：{item.reason}" for item in components if item.value is None or item.value == 0]
    return FuyaoValuationScore(
        **{**result.model_dump(), **metadata, "components": components,
           "pe_ttm": components[0].value, "pb_mrq": components[1].value,
           "score": FUNDAMENTAL_BASE_SCORE + sum(item.points for item in components) if available else None,
           "score_available": available, "missing_data": missing,
           "unavailable_reason": None if available else "PE TTM/PB MRQ 均缺失或为无意义的零值",
           "evidence": _score_evidence(components, observation)}
    )


def _score_evidence(components: list[FuyaoScoreContribution], observation: FuyaoObservation) -> list[str]:
    details = [f"{item.label} {item.value:g}：{item.reason}；贡献 {item.points:+d} 分。"
               for item in components if item.value is not None and item.value != 0]
    return [*details,
            f"来源：{observation.source}；本地获取：{observation.fetched_at}；规则基准 {FUNDAMENTAL_BASE_SCORE} 分。"]


def _score_time(value: str) -> datetime:
    if not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", value):
        raise ValueError("估值观察必须包含完整时间")
    return parse_audit_time(value)


def _admitted_values(symbol: str, observation: FuyaoObservation, cutoff: datetime) -> dict[str, Any]:
    if observation.capability != "valuations" or canonical_stock(observation.symbol) != symbol or observation.id <= 0:
        raise FuyaoScoreInputError("估值观察身份不匹配")
    payload = observation.payload
    if canonical_stock(payload.get("symbol")) != symbol:
        raise FuyaoScoreInputError("估值内容身份不匹配")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if hashlib.sha256(encoded.encode()).hexdigest() != observation.digest:
        raise FuyaoScoreInputError("估值观察摘要不匹配")
    fetched = _score_time(observation.fetched_at)
    if fetched > cutoff:
        raise FuyaoScoreInputError("估值观察晚于本次研究时刻，未纳入评分")
    if cutoff - fetched > MAX_OBSERVATION_AGE:
        raise FuyaoScoreInputError("扶摇估值缓存超过七日观察窗口，请显式刷新后重试")
    timestamp = batch_timestamp(payload.get("batch_timestamp"))
    if timestamp is not None and timestamp / 1000 > fetched.timestamp():
        raise FuyaoScoreInputError("供应商估值批次时间晚于本地观察，未纳入评分")
    if timestamp is not None and cutoff.timestamp() - timestamp / 1000 > MAX_OBSERVATION_AGE.total_seconds():
        raise FuyaoScoreInputError("供应商估值批次时间已超过七日，重新获取不能刷新旧数据时效")
    values = payload.get("values")
    if not isinstance(values, dict):
        raise ValueError("估值字段结构异常")
    return values


def _score_component(key: str, raw: object) -> FuyaoScoreContribution:
    value = finite_or_none(raw)
    pe = key == "pe_ttm"
    label = "PE TTM" if pe else "PB MRQ"
    low, high = (LOW_PE_THRESHOLD, HIGH_PE_THRESHOLD) if pe else (LOW_PB_THRESHOLD, HIGH_PB_THRESHOLD)
    adjustment = PE_SCORE_ADJUSTMENT if pe else PB_SCORE_ADJUSTMENT
    points, reason = _valuation_points(value, low, high, adjustment, pe)
    return FuyaoScoreContribution(key="pe_ttm" if pe else "pb_mrq", label=label, value=value, points=points, reason=reason)


def _valuation_points(value: float | None, low: float, high: float, adjustment: int, pe: bool) -> tuple[int, str]:
    if value is None:
        return 0, "未返回；不填中性值、不重新分配权重"
    if value == 0:
        return 0, "零值不能形成有意义的估值倍数，不计分"
    if value < 0:
        return -adjustment, "负PE提示亏损口径，不当作便宜" if pe else "负PB提示负净资产口径，不当作便宜"
    if value < low:
        return adjustment, f"低于固定观察阈值 {low:g}"
    if value > high:
        return -adjustment, f"高于固定观察阈值 {high:g}"
    return 0, f"位于固定观察区间 {low:g}–{high:g}"
