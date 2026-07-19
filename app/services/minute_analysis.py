from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean
from typing import Callable
from zoneinfo import ZoneInfo

from app.models.schemas import MinuteAnalysisReport, MinuteKline, MinuteSupportResistance, MinuteTPlan
from app.services.indicators import pct_change
from app.utils.market_data import filter_valid_minute_klines, finite_float
from app.utils.time import now_text

MIN_MINUTE_SAMPLE_COUNT = 8
PLAN_FALLBACK_WINDOW = 8
MIN_PLAN_WIDTH_PCT = 0.75
MIN_PLAN_RANGE_PCT = 1.0
BASE_T_PLAN_CONFIDENCE = 40
MAX_SAMPLE_CONFIDENCE_BONUS = 24
LEVEL_CONFIDENCE_BONUS = 12
DEFENSIVE_CONFIDENCE_PENALTY = 8
UNCONFIRMED_ZONE_CONFIDENCE_PENALTY = 15
DEGRADED_PROVENANCE_CONFIDENCE_PENALTY = 12
VOLUME_SURGE_RATIO = 1.8
VOLUME_SHRINK_RATIO = 0.55
MIN_LEVEL_STRENGTH = 20
MAX_LEVEL_STRENGTH = 92
BASE_LEVEL_STRENGTH = 38
LEVEL_TOUCH_BONUS = 12
LEVEL_TOUCH_BAND_PCT = 0.25
LEVEL_DEDUPE_BAND_PCT = 0.1
ASHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")
MINUTE_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d{1,6})?)?(?:[zZ]|[+-]\d{2}:?\d{2})?$"
)
COMPACT_MINUTE_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<date>\d{8})[ T]?(?P<clock>\d{4}(?:\d{2})?)(?P<fraction>\.\d{1,6})?$"
)


@dataclass(frozen=True)
class UnavailableReasonRule:
    label: str
    markers: tuple[str, ...]

    def matches(self, text: str, lower_text: str) -> bool:
        return any(marker in text or marker.lower() in lower_text for marker in self.markers)


@dataclass(frozen=True)
class MinuteTrendSnapshot:
    latest_price: float
    short_ma: float
    long_ma: float
    prev_short_ma: float

    @property
    def short_ma_rising(self) -> bool:
        return self.short_ma >= self.prev_short_ma

    @property
    def short_ma_falling(self) -> bool:
        return self.short_ma < self.prev_short_ma


@dataclass(frozen=True)
class MinuteTrendRule:
    label: str
    matches: Callable[[MinuteTrendSnapshot], bool]


@dataclass(frozen=True)
class VolumePulseSnapshot:
    ratio: float
    price_change_pct: float


@dataclass(frozen=True)
class VolumePulseRule:
    label: str
    matches: Callable[[VolumePulseSnapshot], bool]


@dataclass(frozen=True)
class MomentumRule:
    label: str
    matches: Callable[[float], bool]


@dataclass(frozen=True)
class MinuteLevelSpec:
    level_type: str
    labels: tuple[str, str]
    ratios: tuple[float, float]
    price_selector: Callable[[MinuteKline], float]
    candidate_matches: Callable[[float, float], bool]


UNAVAILABLE_REASON_RULES = (
    UnavailableReasonRule("AKShare 依赖环境异常", ("AKShare 依赖不可用", "numpy.core.multiarray", "numpy ABI mismatch")),
    UnavailableReasonRule("数据源短暂冷却中", ("最近失败，短暂冷却中", "cooldown")),
    UnavailableReasonRule("网络代理连接失败", ("ProxyError", "Unable to connect to proxy")),
    UnavailableReasonRule("行情接口连接失败", ("Max retries exceeded", "HTTPSConnectionPool", "ConnectionError")),
    UnavailableReasonRule("行情接口超时", ("timeout", "timed out", "ReadTimeout")),
    UnavailableReasonRule("行情接口返回为空", ("返回为空", "empty response", "no data")),
    UnavailableReasonRule("行情接口远端断开", ("RemoteDisconnected", "Connection reset", "remote end closed")),
    UnavailableReasonRule("行情接口域名解析失败", ("NameResolutionError", "Temporary failure in name resolution")),
    UnavailableReasonRule("行情接口 SSL 校验失败", ("SSLError", "CERTIFICATE_VERIFY_FAILED")),
)

MINUTE_TREND_RULES = (
    MinuteTrendRule(
        "盘中偏强",
        lambda snapshot: snapshot.latest_price >= snapshot.short_ma >= snapshot.long_ma and snapshot.short_ma_rising,
    ),
    MinuteTrendRule(
        "盘中转弱",
        lambda snapshot: snapshot.latest_price <= snapshot.short_ma <= snapshot.long_ma and snapshot.short_ma_falling,
    ),
    MinuteTrendRule(
        "震荡偏强",
        lambda snapshot: snapshot.latest_price >= snapshot.long_ma and snapshot.short_ma >= snapshot.long_ma,
    ),
    MinuteTrendRule(
        "震荡偏弱",
        lambda snapshot: snapshot.latest_price <= snapshot.long_ma and snapshot.short_ma <= snapshot.long_ma,
    ),
)

VOLUME_PULSE_RULES = (
    VolumePulseRule("放量上攻", lambda snapshot: snapshot.ratio >= VOLUME_SURGE_RATIO and snapshot.price_change_pct > 0),
    VolumePulseRule("放量回落", lambda snapshot: snapshot.ratio >= VOLUME_SURGE_RATIO and snapshot.price_change_pct < 0),
    VolumePulseRule("明显缩量", lambda snapshot: snapshot.ratio <= VOLUME_SHRINK_RATIO),
)
MOMENTUM_RULES = (
    MomentumRule("短线加速", lambda recent: recent >= 0.8),
    MomentumRule("短线走弱", lambda recent: recent <= -0.8),
    MomentumRule("温和转强", lambda recent: recent >= 0.25),
    MomentumRule("温和转弱", lambda recent: recent <= -0.25),
)

MINUTE_LEVEL_SPECS = {
    "support": MinuteLevelSpec(
        level_type="support",
        labels=("近端支撑", "防守支撑"),
        ratios=(0.65, 0.35),
        price_selector=lambda row: row.low,
        candidate_matches=lambda price, latest_price: price <= latest_price,
    ),
    "resistance": MinuteLevelSpec(
        level_type="resistance",
        labels=("近端压力", "强压力"),
        ratios=(0.35, 0.65),
        price_selector=lambda row: row.high,
        candidate_matches=lambda price, latest_price: price >= latest_price,
    ),
}


@dataclass(frozen=True)
class MinuteTPlanZones:
    support: float
    resistance: float
    low_zone: str
    high_zone: str
    width_pct: float


@dataclass(frozen=True)
class MinuteTDecision:
    suitability: str
    style: str


@dataclass(frozen=True)
class MinuteTDecisionRule:
    decision: Callable[[str], MinuteTDecision]
    matches: Callable[[str, str, float, float], bool]


@dataclass(frozen=True)
class MinuteAnalysisContext:
    symbol: str
    interval: str
    rows: list[MinuteKline]
    latest_price: float
    updated_at: str
    source: str
    intraday_change_pct: float
    intraday_range_pct: float
    volume_pulse: str
    trend_label: str
    momentum_label: str
    supports: list[MinuteSupportResistance]
    resistances: list[MinuteSupportResistance]
    t_plan: MinuteTPlan
    warnings: list[str]
    availability: str
    availability_reason: str
    reason_code: str
    missing_data: list[str]


@dataclass(frozen=True)
class MinuteAvailability:
    status: str
    reason: str
    reason_code: str
    missing_data: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class MinuteWarningContext:
    latest: MinuteKline
    t_plan: MinuteTPlan
    intraday_range_pct: float
    volume_pulse: str


@dataclass(frozen=True)
class MinuteWarningRule:
    message: str
    matches: Callable[[MinuteWarningContext], bool]


T_PLAN_DECISION_RULES = (
    MinuteTDecisionRule(
        decision=lambda trend_label: MinuteTDecision("不适合主动做T", "防守型"),
        matches=lambda trend_label, volume_pulse, width_pct, intraday_range_pct: volume_pulse == "放量回落" or trend_label == "盘中转弱",
    ),
    MinuteTDecisionRule(
        decision=lambda trend_label: MinuteTDecision("仅底仓可做T", _range_t_style(trend_label)),
        matches=lambda trend_label, volume_pulse, width_pct, intraday_range_pct: width_pct >= MIN_PLAN_WIDTH_PCT and intraday_range_pct >= MIN_PLAN_RANGE_PCT,
    ),
)
DEFAULT_T_PLAN_DECISION = MinuteTDecision("等待更大区间", "窄幅等待型")
MINUTE_WARNING_RULES = (
    MinuteWarningRule(
        "当前分钟K线来自缓存或兜底结果，做T区间需要降权。",
        lambda context: context.latest.from_cache or context.latest.fallback_used,
    ),
    MinuteWarningRule(
        "当前不适合主动做T，避免为了交易而交易。",
        lambda context: context.t_plan.suitability == "不适合主动做T",
    ),
    MinuteWarningRule(
        "分钟量能显示放量回落，先防守再考虑高抛低吸。",
        lambda context: context.volume_pulse == "放量回落",
    ),
    MinuteWarningRule(
        "盘中振幅偏窄，做T空间可能不足。",
        lambda context: context.intraday_range_pct < 0.8,
    ),
)


def build_minute_analysis_report(symbol: str, rows: list[MinuteKline], interval: str = "5m") -> MinuteAnalysisReport:
    normalized_interval = _normalize_interval(interval)
    clean_rows = _valid_minute_rows(rows)
    if len(clean_rows) < MIN_MINUTE_SAMPLE_COUNT:
        reason, reason_code = _insufficient_sample_reason(rows, clean_rows)
        return _empty_report(symbol, clean_rows, normalized_interval, reason, reason_code=reason_code)

    return _minute_report(_minute_analysis_context(symbol, clean_rows, normalized_interval))


def build_unavailable_minute_analysis_report(symbol: str, interval: str = "5m", reason: str | None = None) -> MinuteAnalysisReport:
    user_reason = "分钟K线数据源暂不可用，已暂停盘中做T判断。"
    if reason:
        user_reason = f"{user_reason}原因：{_compact_unavailable_reason(reason)}。"
    return _empty_report(symbol, [], _normalize_interval(interval), user_reason, reason_code="provider_failure")


def _normalize_interval(interval: str) -> str:
    normalized = str(interval or "5m").lower().strip()
    return normalized or "5m"


def _minute_analysis_context(symbol: str, rows: list[MinuteKline], interval: str) -> MinuteAnalysisContext:
    latest = rows[-1]
    latest_price = latest.close
    intraday_range_pct = _intraday_range_pct(rows, latest_price)
    trend_label = _trend_label_from_rows(rows)
    volume_pulse = _volume_pulse(rows)
    availability = _minute_availability(rows, volume_pulse)
    supports = _minute_levels(rows, latest_price, "support")
    resistances = _minute_levels(rows, latest_price, "resistance")
    t_plan = _minute_t_plan(rows, latest_price, supports, resistances, trend_label, volume_pulse, intraday_range_pct)
    return MinuteAnalysisContext(
        symbol=symbol,
        interval=interval,
        rows=rows,
        latest_price=latest_price,
        updated_at=latest.timestamp or now_text(),
        source=_minute_source(rows),
        intraday_change_pct=_intraday_change_pct(rows),
        intraday_range_pct=intraday_range_pct,
        volume_pulse=volume_pulse,
        trend_label=trend_label,
        momentum_label=_momentum_label(rows),
        supports=supports,
        resistances=resistances,
        t_plan=t_plan,
        warnings=_dedupe_text(_minute_warnings(rows, t_plan, intraday_range_pct, volume_pulse) + availability.warnings),
        availability=availability.status,
        availability_reason=availability.reason,
        reason_code=availability.reason_code,
        missing_data=availability.missing_data,
    )


def _minute_report(context: MinuteAnalysisContext) -> MinuteAnalysisReport:
    return MinuteAnalysisReport(
        symbol=context.symbol,
        updated_at=context.updated_at,
        interval=context.interval,
        source=context.source,
        sample_count=len(context.rows),
        klines=context.rows,
        availability=context.availability,
        availability_reason=context.availability_reason,
        reason_code=context.reason_code,
        latest_price=context.latest_price,
        intraday_change_pct=context.intraday_change_pct,
        intraday_range_pct=context.intraday_range_pct,
        volume_pulse=context.volume_pulse,
        trend_label=context.trend_label,
        momentum_label=context.momentum_label,
        summary=_minute_summary(context),
        supports=context.supports,
        resistances=context.resistances,
        t_plan=context.t_plan,
        warnings=context.warnings,
        missing_data=context.missing_data,
    )


def _minute_summary(context: MinuteAnalysisContext) -> str:
    return (
        f"{context.interval} 分钟分析：盘中趋势「{context.trend_label}」，"
        f"动量「{context.momentum_label}」，量能「{context.volume_pulse}」。做T结论：{context.t_plan.suitability}。"
    )


def _intraday_change_pct(rows: list[MinuteKline]) -> float:
    return round(pct_change(rows[-1].close, rows[0].open), 2)


def _intraday_range_pct(rows: list[MinuteKline], latest_price: float) -> float:
    if latest_price <= 0:
        return 0
    low = min(item.low for item in rows)
    high = max(item.high for item in rows)
    return round((high - low) / latest_price * 100, 2)


def _trend_label_from_rows(rows: list[MinuteKline]) -> str:
    short_ma = _ma(rows, 6)
    long_ma = _ma(rows, 18)
    prev_short_ma = _ma(rows[:-3], 6) if len(rows) >= 12 else short_ma
    return _minute_trend_label(rows[-1].close, short_ma, long_ma, prev_short_ma)


def _minute_source(rows: list[MinuteKline]) -> str:
    latest = rows[-1]
    return latest.source or rows[0].source or "分钟源待确认"


def _empty_report(
    symbol: str,
    rows: list[MinuteKline],
    interval: str,
    reason: str,
    *,
    reason_code: str,
) -> MinuteAnalysisReport:
    return MinuteAnalysisReport(
        symbol=symbol,
        updated_at=_empty_report_updated_at(rows),
        interval=interval,
        source=_empty_report_source(rows),
        sample_count=len(rows),
        klines=rows,
        availability="unavailable",
        availability_reason=reason,
        reason_code=reason_code,
        latest_price=_empty_report_latest_price(rows),
        summary=reason,
        t_plan=_empty_t_plan(reason),
        warnings=[reason],
        missing_data=["分钟K线"],
    )


def _empty_report_updated_at(rows: list[MinuteKline]) -> str:
    return rows[-1].timestamp if rows else now_text()


def _empty_report_source(rows: list[MinuteKline]) -> str:
    source = rows[-1].source if rows else None
    return source or "分钟源待确认"


def _empty_report_latest_price(rows: list[MinuteKline]) -> float | None:
    if not rows:
        return None
    return _positive_finite_float(rows[-1].close)


def _empty_t_plan(reason: str) -> MinuteTPlan:
    return MinuteTPlan(
        low_zone="不可用",
        high_zone="不可用",
        suitability="暂停做T判断",
        style="数据不可用",
        confidence=0,
        summary=reason,
        execution_steps=["等待有效分钟K线样本补齐、价格区间确认后再判断盘中区间。"],
        stop_conditions=["分钟K线缺失、价格无效或样本不足时，不按盘中区间做T。"],
    )


def _valid_minute_rows(rows: list[MinuteKline]) -> list[MinuteKline]:
    # 同一实际时刻可能被数据源后续修订，保留输入序列中最后一条有效记录。
    deduped: dict[datetime, MinuteKline] = {}
    for row in filter_valid_minute_klines(rows):
        timestamp_key = _minute_timestamp_key(row.timestamp)
        if timestamp_key is not None:
            deduped[timestamp_key] = row
    if not deduped:
        return []

    latest_local_date = max(deduped).astimezone(ASHARE_TIMEZONE).date()
    return [
        deduped[timestamp_key]
        for timestamp_key in sorted(deduped)
        if timestamp_key.astimezone(ASHARE_TIMEZONE).date() == latest_local_date
    ]


def _minute_timestamp_key(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parsed = _parse_minute_timestamp(text)
    if parsed is None:
        return None
    localized = parsed.replace(tzinfo=ASHARE_TIMEZONE) if parsed.tzinfo is None else parsed
    return localized.astimezone(timezone.utc)


def _parse_minute_timestamp(text: str) -> datetime | None:
    if MINUTE_TIMESTAMP_PATTERN.fullmatch(text):
        normalized = text.replace("/", "-")
        if normalized[-1:] in {"z", "Z"}:
            normalized = f"{normalized[:-1]}+00:00"
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    compact_match = COMPACT_MINUTE_TIMESTAMP_PATTERN.fullmatch(text)
    if compact_match is None:
        return None
    compact_value = f"{compact_match.group('date')}{compact_match.group('clock')}"
    timestamp_format = "%Y%m%d%H%M%S" if len(compact_value) == 14 else "%Y%m%d%H%M"
    try:
        parsed = datetime.strptime(compact_value, timestamp_format)
    except ValueError:
        return None
    fraction = compact_match.group("fraction")
    if fraction:
        parsed = parsed.replace(microsecond=int(fraction[1:].ljust(6, "0")))
    return parsed


def _insufficient_sample_reason(raw_rows: list[MinuteKline], clean_rows: list[MinuteKline]) -> tuple[str, str]:
    if not raw_rows:
        return "分钟K线返回为空，暂不能形成做T参考。", "empty_data"
    if clean_rows and _has_multiple_valid_minute_dates(raw_rows):
        return "最新交易日分钟K线样本不足，至少需要 8 条有效样本，暂不能形成做T参考。", "insufficient_samples"
    if len(raw_rows) < MIN_MINUTE_SAMPLE_COUNT and len(raw_rows) == len(clean_rows):
        return "分钟K线样本不足，至少需要 8 条有效样本，暂不能形成做T参考。", "insufficient_samples"
    return "过滤无效数据后，有效分钟K线不足 8 条，暂不能形成做T参考。", "insufficient_valid_samples"


def _has_multiple_valid_minute_dates(rows: list[MinuteKline]) -> bool:
    valid_dates = set()
    for row in filter_valid_minute_klines(rows):
        timestamp_key = _minute_timestamp_key(row.timestamp)
        if timestamp_key is None:
            continue
        valid_dates.add(timestamp_key.astimezone(ASHARE_TIMEZONE).date())
        if len(valid_dates) > 1:
            return True
    return False


def _minute_availability(rows: list[MinuteKline], volume_pulse: str) -> MinuteAvailability:
    cache_or_fallback = any(row.from_cache or row.fallback_used for row in rows)
    volume_missing = volume_pulse == "量能待确认"
    if cache_or_fallback and volume_missing:
        return MinuteAvailability(
            status="degraded",
            reason=(
                "分钟价格结构可分析，但数据来自缓存或兜底且关键成交量输入缺失；"
                "趋势、支撑压力和价格区间仍可参考，时效性、量能及量价结论不可用。"
            ),
            reason_code="cache_or_fallback_and_missing_volume",
            missing_data=["实时分钟K线（当前使用缓存或兜底数据）", "有效分钟成交量"],
            warnings=["量能输入不足，当前量能与量价配合结论不可用。"],
        )
    if cache_or_fallback:
        return MinuteAvailability(
            status="degraded",
            reason=(
                "分钟价格与成交量结构可分析，但数据来自缓存或兜底；"
                "趋势、支撑压力和价格区间仍可参考，时效性与量价确认需降权。"
            ),
            reason_code="cache_or_fallback",
            missing_data=["实时分钟K线（当前使用缓存或兜底数据）"],
            warnings=[],
        )
    if volume_missing:
        return MinuteAvailability(
            status="degraded",
            reason=(
                "分钟价格结构可分析，但关键成交量输入缺失；"
                "趋势、支撑压力和价格区间仍可用，量能及量价结论不可用。"
            ),
            reason_code="missing_volume",
            missing_data=["有效分钟成交量"],
            warnings=["量能输入不足，当前量能与量价配合结论不可用。"],
        )
    return MinuteAvailability(
        status="ok",
        reason="分钟价格、成交量和数据来源均满足分析要求，全部分钟分析结论可用。",
        reason_code="complete",
        missing_data=[],
        warnings=[],
    )


def _dedupe_text(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _compact_unavailable_reason(reason: str) -> str:
    text = " ".join(str(reason or "").split())
    if not text:
        return "数据源连接失败"
    lower_text = text.lower()
    for rule in UNAVAILABLE_REASON_RULES:
        if rule.matches(text, lower_text):
            return rule.label
    return text[:80]


def _positive_finite_float(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _ma(rows: list[MinuteKline], window: int) -> float:
    if not rows:
        return 0
    values = [item.close for item in rows[-window:]]
    return round(mean(values), 3)


def _minute_trend_label(latest_price: float, short_ma: float, long_ma: float, prev_short_ma: float) -> str:
    snapshot = MinuteTrendSnapshot(latest_price=latest_price, short_ma=short_ma, long_ma=long_ma, prev_short_ma=prev_short_ma)
    return next((rule.label for rule in MINUTE_TREND_RULES if rule.matches(snapshot)), "盘中震荡")


def _momentum_label(rows: list[MinuteKline]) -> str:
    if len(rows) < 4:
        return "待确认"
    recent = pct_change(rows[-1].close, rows[-4].close)
    return next((rule.label for rule in MOMENTUM_RULES if rule.matches(recent)), "动量平稳")


def _volume_pulse(rows: list[MinuteKline]) -> str:
    snapshot = _volume_pulse_snapshot(rows)
    if snapshot is None:
        return "量能待确认"
    return next((rule.label for rule in VOLUME_PULSE_RULES if rule.matches(snapshot)), "量能平稳")


def _volume_pulse_snapshot(rows: list[MinuteKline]) -> VolumePulseSnapshot | None:
    sample = _volume_pulse_sample(rows)
    if sample is None:
        return None
    return _volume_pulse_snapshot_from_sample(rows, sample)


def _volume_pulse_snapshot_from_sample(
    rows: list[MinuteKline],
    sample: tuple[list[float], list[float]],
) -> VolumePulseSnapshot:
    recent_volumes, base_volumes = sample
    return VolumePulseSnapshot(
        ratio=mean(recent_volumes) / mean(base_volumes),
        price_change_pct=_recent_price_change_pct(rows),
    )


def _volume_pulse_sample(rows: list[MinuteKline]) -> tuple[list[float], list[float]] | None:
    recent_volumes = _recent_volume_values(rows)
    base_volumes = _base_volume_values(rows)
    if not recent_volumes:
        return None
    if not _has_enough_base_volume(base_volumes):
        return None
    if mean(base_volumes) <= 0:
        return None
    return recent_volumes, base_volumes


def _recent_price_change_pct(rows: list[MinuteKline]) -> float:
    return pct_change(rows[-1].close, rows[-4].close) if len(rows) >= 4 else 0


def _recent_volume_values(rows: list[MinuteKline]) -> list[float]:
    if len(rows) < MIN_MINUTE_SAMPLE_COUNT:
        return []
    recent_rows = rows[-3:]
    recent_volumes = [_positive_finite_float(row.volume) for row in recent_rows]
    return [volume for volume in recent_volumes if volume is not None] if all(volume is not None for volume in recent_volumes) else []


def _base_volume_values(rows: list[MinuteKline]) -> list[float]:
    recent_window = [_positive_finite_float(row.volume) for row in rows[-18:-3]]
    fallback_window = [_positive_finite_float(row.volume) for row in rows[:-3]]
    return [volume for volume in recent_window if volume is not None] or [volume for volume in fallback_window if volume is not None]


def _has_enough_base_volume(volumes: list[float]) -> bool:
    return len(volumes) >= MIN_MINUTE_SAMPLE_COUNT - 3


def _minute_levels(rows: list[MinuteKline], latest_price: float, level_type: str) -> list[MinuteSupportResistance]:
    spec = MINUTE_LEVEL_SPECS.get(level_type)
    parsed_latest_price = _positive_finite_float(latest_price)
    if spec is None or not rows or parsed_latest_price is None:
        return []
    candidates = _level_candidates(rows, parsed_latest_price, spec)
    if not candidates:
        return []
    sorted_values = sorted(candidates)
    result = []
    for label, ratio in zip(spec.labels, spec.ratios):
        price = _quantile(sorted_values, ratio)
        result.append(_minute_level(label, price, candidates, parsed_latest_price, len(rows)))
    return _dedupe_levels(result, parsed_latest_price)


def _level_candidates(rows: list[MinuteKline], latest_price: float, spec: MinuteLevelSpec) -> list[float]:
    candidates: list[float] = []
    for row in rows:
        price = _positive_finite_float(spec.price_selector(row))
        if price is not None and spec.candidate_matches(price, latest_price):
            candidates.append(price)
    return candidates


def _minute_level(label: str, price: float, candidates: list[float], latest_price: float, sample_count: int) -> MinuteSupportResistance:
    touches = sum(1 for item in candidates if _pct_distance(item, price) <= LEVEL_TOUCH_BAND_PCT)
    strength = max(MIN_LEVEL_STRENGTH, min(MAX_LEVEL_STRENGTH, BASE_LEVEL_STRENGTH + touches * LEVEL_TOUCH_BONUS))
    return MinuteSupportResistance(
        label=label,
        price=round(price, 2),
        strength=strength,
        reason=f"{label}来自近 {sample_count} 根分钟K的价格密集区，触达次数约 {touches} 次。",
    )


def _dedupe_levels(levels: list[MinuteSupportResistance], latest_price: float) -> list[MinuteSupportResistance]:
    deduped: list[MinuteSupportResistance] = []
    for item in levels:
        if all(_pct_distance(item.price, old.price, base=latest_price) > LEVEL_DEDUPE_BAND_PCT for old in deduped):
            deduped.append(item)
    return deduped[:2]


def _pct_distance(left: float, right: float, *, base: float | None = None) -> float:
    denominator = base or right
    return abs(left - right) / denominator * 100 if denominator else 0


def _minute_t_plan(
    rows: list[MinuteKline],
    latest_price: float,
    supports: list[MinuteSupportResistance],
    resistances: list[MinuteSupportResistance],
    trend_label: str,
    volume_pulse: str,
    intraday_range_pct: float,
) -> MinuteTPlan:
    if not _has_t_plan_basis(rows, latest_price):
        return _empty_t_plan("分钟价格或样本不足，暂不能形成做T参考。")
    zones = _t_plan_zones(rows, latest_price, supports, resistances)
    decision = _t_plan_decision(trend_label, volume_pulse, zones.width_pct, intraday_range_pct)
    return MinuteTPlan(
        low_zone=zones.low_zone,
        high_zone=zones.high_zone,
        suitability=decision.suitability,
        style=decision.style,
        confidence=_t_plan_confidence(rows, supports, resistances, decision, zones),
        summary=_t_plan_summary(decision, zones),
        execution_steps=_t_plan_execution_steps(zones),
        stop_conditions=_t_plan_stop_conditions(zones),
    )


def _has_t_plan_basis(rows: list[MinuteKline], latest_price: float) -> bool:
    return len(rows) >= MIN_MINUTE_SAMPLE_COUNT and _positive_finite_float(latest_price) is not None


def _t_plan_zones(
    rows: list[MinuteKline],
    latest_price: float,
    supports: list[MinuteSupportResistance],
    resistances: list[MinuteSupportResistance],
) -> MinuteTPlanZones:
    parsed_latest_price = _positive_finite_float(latest_price)
    if parsed_latest_price is None or not rows:
        return _empty_t_plan_zones()
    support = _t_plan_support_price(rows, supports)
    resistance = _t_plan_resistance_price(rows, resistances)
    if not _valid_t_plan_range(support, resistance, parsed_latest_price):
        return _empty_t_plan_zones()
    return MinuteTPlanZones(
        support=support,
        resistance=resistance,
        low_zone=_zone_text(support, parsed_latest_price, lower=True),
        high_zone=_zone_text(resistance, parsed_latest_price, lower=False),
        width_pct=_t_plan_width_pct(support, resistance, parsed_latest_price),
    )


def _empty_t_plan_zones() -> MinuteTPlanZones:
    return MinuteTPlanZones(support=0, resistance=0, low_zone="待确认", high_zone="待确认", width_pct=0)


def _t_plan_support_price(rows: list[MinuteKline], supports: list[MinuteSupportResistance]) -> float:
    support = _first_valid_level_price(supports)
    if support is not None:
        return support
    lows = [_positive_finite_float(item.low) for item in rows[-PLAN_FALLBACK_WINDOW:]]
    valid_lows = [price for price in lows if price is not None]
    return min(valid_lows) if valid_lows else 0


def _t_plan_resistance_price(rows: list[MinuteKline], resistances: list[MinuteSupportResistance]) -> float:
    resistance = _first_valid_level_price(resistances)
    if resistance is not None:
        return resistance
    highs = [_positive_finite_float(item.high) for item in rows[-PLAN_FALLBACK_WINDOW:]]
    valid_highs = [price for price in highs if price is not None]
    return max(valid_highs) if valid_highs else 0


def _first_valid_level_price(levels: list[MinuteSupportResistance]) -> float | None:
    return next((price for level in levels if (price := _positive_finite_float(level.price)) is not None), None)


def _valid_t_plan_range(support: float, resistance: float, latest_price: float) -> bool:
    return (
        _positive_finite_float(support) is not None
        and _positive_finite_float(resistance) is not None
        and _positive_finite_float(latest_price) is not None
        and resistance > support
    )


def _t_plan_width_pct(support: float, resistance: float, latest_price: float) -> float:
    return (resistance - support) / latest_price * 100 if _valid_t_plan_range(support, resistance, latest_price) else 0


def _t_plan_decision(trend_label: str, volume_pulse: str, width_pct: float, intraday_range_pct: float) -> MinuteTDecision:
    return next(
        (
            rule.decision(trend_label)
            for rule in T_PLAN_DECISION_RULES
            if rule.matches(trend_label, volume_pulse, width_pct, intraday_range_pct)
        ),
        DEFAULT_T_PLAN_DECISION,
    )


def _range_t_style(trend_label: str) -> str:
    return "区间型" if "震荡" in trend_label else "趋势滚动型"


def _t_plan_confidence(
    rows: list[MinuteKline],
    supports: list[MinuteSupportResistance],
    resistances: list[MinuteSupportResistance],
    decision: MinuteTDecision,
    zones: MinuteTPlanZones | None = None,
) -> int:
    confidence = BASE_T_PLAN_CONFIDENCE + min(MAX_SAMPLE_CONFIDENCE_BONUS, len(rows) // 4)
    zones_confirmed = zones is None or _t_plan_zones_confirmed(zones)
    confidence += LEVEL_CONFIDENCE_BONUS if supports and resistances and zones_confirmed else 0
    confidence -= DEFENSIVE_CONFIDENCE_PENALTY if decision.suitability == "不适合主动做T" else 0
    confidence -= UNCONFIRMED_ZONE_CONFIDENCE_PENALTY if not zones_confirmed else 0
    if any(row.from_cache or row.fallback_used for row in rows):
        confidence -= DEGRADED_PROVENANCE_CONFIDENCE_PENALTY
    return max(25, min(88, confidence))


def _t_plan_summary(decision: MinuteTDecision, zones: MinuteTPlanZones) -> str:
    if not _t_plan_zones_confirmed(zones):
        return f"{decision.style}，{decision.suitability}。分钟价格或支撑压力区间待确认，暂不按盘中区间做T。"
    return f"{decision.style}，{decision.suitability}。参考低吸区 {zones.low_zone}，高抛区 {zones.high_zone}，区间宽度约 {zones.width_pct:.2f}%。"


def _t_plan_execution_steps(zones: MinuteTPlanZones) -> list[str]:
    if not _t_plan_zones_confirmed(zones):
        return [
            "只使用已有可卖底仓，今日新增买入部分不参与当日T。",
            "等待有效分钟K线样本补齐、支撑压力区间确认后再评估。",
            "区间未确认前不预设低吸或高抛价格。",
        ]
    return [
        "只使用已有可卖底仓，今日新增买入部分不参与当日T。",
        f"低吸只看 {zones.low_zone} 附近缩量止跌或快速收回，不接放量下跌。",
        f"高抛只看 {zones.high_zone} 附近冲高乏力、量能背离或接近压力。",
    ]


def _t_plan_stop_conditions(zones: MinuteTPlanZones) -> list[str]:
    if not _t_plan_zones_confirmed(zones):
        return [
            "分钟价格、支撑或压力缺失时，不按盘中区间做T。",
            "出现放量回落、盘中转弱或盘口卖压明显增强。",
            "区间未确认时，不为摊低成本强行加仓。",
        ]
    return [
        f"有效跌破 {zones.support:.2f} 后不能快速收回。",
        "出现放量回落、盘中转弱或盘口卖压明显增强。",
        "低吸区和高抛区间距不足以覆盖交易成本和滑点。",
    ]


def _minute_warnings(rows: list[MinuteKline], t_plan: MinuteTPlan, intraday_range_pct: float, volume_pulse: str) -> list[str]:
    context = MinuteWarningContext(
        latest=rows[-1],
        t_plan=t_plan,
        intraday_range_pct=intraday_range_pct,
        volume_pulse=volume_pulse,
    )
    return [rule.message for rule in MINUTE_WARNING_RULES if rule.matches(context)]


def _t_plan_zones_confirmed(zones: MinuteTPlanZones) -> bool:
    return zones.low_zone != "待确认" and zones.high_zone != "待确认" and zones.width_pct > 0


def _zone_text(price: float, latest_price: float, *, lower: bool) -> str:
    if _positive_finite_float(price) is None or _positive_finite_float(latest_price) is None:
        return "待确认"
    band = max(latest_price * 0.0015, 0.01)
    start = price - band if lower else price
    end = price if lower else price + band
    return f"{start:.2f}-{end:.2f}"


def _quantile(values: list[float], ratio: float) -> float:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight
