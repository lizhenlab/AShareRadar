from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import AnalysisResult, ChipAnalysis, ChipBand, FeatureSnapshot, Kline
from app.services.indicators import pct_change
from app.services.scoring import clamp_score
from app.utils.market_data import finite_float


CHIP_LOOKBACK_DAYS = 80
MIN_CHIP_KLINES = 10
CHIP_BUCKET_COUNT = 12
MAX_CHIP_BANDS = 3
MIN_CHIP_PRICE_SPAN = 0.01

ChipBin = tuple[float, float, float]


@dataclass(frozen=True)
class ChipBucketContext:
    price_low: float
    bucket_size: float
    bucket_count: int


@dataclass(frozen=True)
class ChipDistribution:
    bins: list[ChipBin]
    total_volume: float
    center_price: float
    concentration: int


def build_chip_analysis(analysis: AnalysisResult, feature: FeatureSnapshot) -> ChipAnalysis:
    rows = _valid_chip_rows(analysis.klines[-CHIP_LOOKBACK_DAYS:])
    current_price = _effective_chip_price(feature.price, rows)
    if len(rows) < MIN_CHIP_KLINES:
        return _insufficient_chip_analysis(feature, current_price)
    distribution = _chip_distribution(rows)
    if distribution is None:
        return _insufficient_chip_analysis(feature, current_price)
    support_bands = _support_bands(distribution.bins, distribution.total_volume, current_price)
    pressure_bands = _pressure_bands(distribution.bins, distribution.total_volume, current_price)
    label = _chip_distribution_label(distribution.concentration)
    summary = (
        f"近{len(rows)}根有效日K估算成本中枢约 {distribution.center_price:.2f}，{label}。"
        f"现价相对中枢 {pct_change(current_price, distribution.center_price):.2f}%。"
    )
    return ChipAnalysis(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        center_price=round(distribution.center_price, 2),
        concentration=distribution.concentration,
        distribution_label=label,
        summary=summary,
        support_bands=support_bands,
        pressure_bands=pressure_bands,
        notes=[
            "筹码分布用日K成交量和均价近似估算，适合判断压力/支撑区域，不代表真实股东成本。",
            "价格或成交量异常的K线会被剔除，避免单条坏数据污染成本中枢。",
            "若接入逐笔成交或区间成交分布，可替换为更精确的筹码模型。",
        ],
    )


def _insufficient_chip_analysis(feature: FeatureSnapshot, current_price: float) -> ChipAnalysis:
    return ChipAnalysis(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        center_price=round(current_price, 2),
        concentration=35,
        distribution_label="筹码样本不足",
        summary="有效K线样本不足，暂不能形成有效筹码分布估算。",
        notes=["筹码为日K成交量按价格区间近似分布，不等同于交易所真实持仓成本。"],
    )


def _valid_chip_rows(rows: list[Kline]) -> list[Kline]:
    return [item for item in rows if _valid_chip_row(item)]


def _valid_chip_row(row: Kline) -> bool:
    volume = _positive_number(row.volume)
    low = _positive_number(row.low)
    close = _positive_number(row.close)
    high = _positive_number(row.high)
    return (
        volume is not None
        and low is not None
        and close is not None
        and high is not None
        and low <= close <= high
    )


def _effective_chip_price(feature_price: float, rows: list[Kline]) -> float:
    price = _positive_number(feature_price)
    if price is not None:
        return price
    if not rows:
        return 0
    return _positive_number(rows[-1].close) or 0


def _positive_number(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _chip_distribution(rows: list[Kline], bucket_count: int = CHIP_BUCKET_COUNT) -> ChipDistribution | None:
    bins = _volume_price_bins(rows, bucket_count=bucket_count)
    total_volume = sum(item[2] for item in bins)
    if total_volume <= 0:
        return None
    center_price = sum(((low + high) / 2) * volume for low, high, volume in bins) / total_volume
    return ChipDistribution(
        bins=bins,
        total_volume=total_volume,
        center_price=center_price,
        concentration=_chip_concentration(bins, center_price, total_volume),
    )


def _volume_price_bins(rows: list[Kline], bucket_count: int) -> list[ChipBin]:
    valid_rows = _valid_chip_rows(rows)
    context = _chip_bucket_context(valid_rows, bucket_count)
    if context is None:
        return []
    return _non_empty_chip_bins(context, _chip_bucket_volumes(valid_rows, context))


def _chip_bucket_context(rows: list[Kline], bucket_count: int) -> ChipBucketContext | None:
    if not rows or bucket_count <= 0:
        return None
    price_low = min(item.low for item in rows)
    price_high = max(item.high for item in rows)
    span = max(MIN_CHIP_PRICE_SPAN, price_high - price_low)
    return ChipBucketContext(price_low=price_low, bucket_size=span / bucket_count, bucket_count=bucket_count)


def _chip_bucket_volumes(rows: list[Kline], context: ChipBucketContext) -> list[float]:
    buckets = [0.0 for _ in range(context.bucket_count)]
    for item in rows:
        index = _chip_bucket_index(context, _typical_chip_price(item))
        buckets[index] += item.volume
    return buckets


def _typical_chip_price(row: Kline) -> float:
    return (row.high + row.low + row.close) / 3


def _chip_bucket_index(context: ChipBucketContext, price: float) -> int:
    raw_index = int((price - context.price_low) / context.bucket_size)
    return min(context.bucket_count - 1, max(0, raw_index))


def _non_empty_chip_bins(context: ChipBucketContext, buckets: list[float]) -> list[ChipBin]:
    return [
        _chip_bin(context, index, volume)
        for index, volume in enumerate(buckets)
        if volume > 0
    ]


def _chip_bin(context: ChipBucketContext, index: int, volume: float) -> ChipBin:
    low = context.price_low + index * context.bucket_size
    high = context.price_low + (index + 1) * context.bucket_size
    return round(low, 2), round(high, 2), volume


def _support_bands(bins: list[ChipBin], total_volume: float, current_price: float) -> list[ChipBand]:
    candidates = sorted(
        (item for item in bins if item[0] <= current_price),
        key=lambda item: (_support_band_distance(item, current_price), -item[2]),
    )
    return [_chip_band("支撑筹码区", item, total_volume, _support_band_note(item, current_price)) for item in candidates[:MAX_CHIP_BANDS]]


def _pressure_bands(bins: list[ChipBin], total_volume: float, current_price: float) -> list[ChipBand]:
    candidates = sorted(
        (item for item in bins if item[1] >= current_price),
        key=lambda item: (_pressure_band_distance(item, current_price), -item[2]),
    )
    return [_chip_band("压力筹码区", item, total_volume, _pressure_band_note(item, current_price)) for item in candidates[:MAX_CHIP_BANDS]]


def _support_band_distance(item: ChipBin, current_price: float) -> float:
    low, high, _ = item
    return 0 if low <= current_price <= high else current_price - high


def _pressure_band_distance(item: ChipBin, current_price: float) -> float:
    low, high, _ = item
    return 0 if low <= current_price <= high else low - current_price


def _support_band_note(item: ChipBin, current_price: float) -> str:
    low, high, _ = item
    if low <= current_price <= high:
        return "现价所在成交密集区，跌破后支撑意义下降。"
    return "现价下方成交区，跌破后支撑意义下降。"


def _pressure_band_note(item: ChipBin, current_price: float) -> str:
    low, high, _ = item
    if low <= current_price <= high:
        return "现价所在成交密集区，放量站稳上沿后压力才会转化。"
    return "现价上方成交区，放量站稳后压力才会转化。"


def _chip_band(label: str, item: ChipBin, total_volume: float, note: str) -> ChipBand:
    low, high, volume = item
    share = volume / total_volume * 100 if total_volume > 0 else 0
    return ChipBand(label=label, low=low, high=high, share=round(share, 1), note=note)


def _chip_distribution_label(concentration: int) -> str:
    if concentration >= 68:
        return "筹码相对集中"
    if concentration >= 48:
        return "筹码分布适中"
    return "筹码较分散"


def _chip_concentration(bins: list[ChipBin], center: float, total_volume: float) -> int:
    if not bins or center <= 0 or total_volume <= 0:
        return 0
    near_volume = sum(volume for low, high, volume in bins if low <= center <= high or abs(((low + high) / 2 - center) / center) <= 0.05)
    return clamp_score(35 + near_volume / total_volume * 65, round_value=True)
