from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import DataQuality, SignalItem


LOW_CONFIDENCE_LEVELS = {"积极", "观察"}
HARD_QUALITY_ANOMALIES = {"K线严重滞后", "K线兜底缓存", "演示K线", "演示行情", "报价严重滞后"}


@dataclass(frozen=True)
class QualityBlockRule:
    name: str
    kind: str
    build_items: Callable[[DataQuality, str], list[SignalItem]]


def gate_signal_items(items: list[SignalItem], quality: DataQuality, kind: str) -> list[SignalItem]:
    if not _quality_requires_gate(quality):
        return items
    reason = quality_reason(quality)
    if quality_blocks_active_signals(quality):
        return _blocked_signal_items(kind, quality, reason)
    return [_low_confidence_signal_item(item, quality, reason) for item in items]


def quality_blocks_active_signals(quality: DataQuality) -> bool:
    return quality.score < 50 or any(item in HARD_QUALITY_ANOMALIES for item in quality.anomalies)


def quality_reason(quality: DataQuality) -> str:
    if quality.anomalies:
        return "、".join(quality.anomalies[:3])
    if quality.notes:
        return "、".join(quality.notes[:2])
    return "数据可靠性不足"


def _quality_requires_gate(quality: DataQuality) -> bool:
    return quality.score < 70


def _blocked_signal_items(kind: str, quality: DataQuality, reason: str) -> list[SignalItem]:
    for rule in QUALITY_BLOCK_RULES:
        if rule.kind == kind:
            return rule.build_items(quality, reason)
    return _blocked_sell_items(quality, reason)


def _low_confidence_signal_item(item: SignalItem, quality: DataQuality, reason: str) -> SignalItem:
    level = "谨慎" if item.level in LOW_CONFIDENCE_LEVELS else item.level
    return SignalItem(
        title=item.title,
        level=level,
        reason=f"数据质量为{quality.level}（{reason}），该信号仅作低置信观察；{item.reason}",
    )


def _blocked_buy_items(quality: DataQuality, reason: str) -> list[SignalItem]:
    return [
        SignalItem(
            title="暂停新增买点",
            level="风险",
            reason=f"当前数据质量为{quality.level}，存在{reason}；新增买点先暂停，等报价和K线重新确认后再判断。",
        )
    ]


def _blocked_t_items(quality: DataQuality, reason: str) -> list[SignalItem]:
    return [
        SignalItem(
            title="暂停做T",
            level="风险",
            reason=f"当前数据质量为{quality.level}，存在{reason}；分时价格和关键价位可信度不足，不建议做T。",
        ),
        SignalItem(
            title="已有底仓才做T",
            level="观察",
            reason="A股普通股票通常是T+1，今日买入部分不能当日卖出；恢复做T前也只能使用已有可卖底仓。",
        ),
    ]


def _blocked_sell_items(quality: DataQuality, reason: str) -> list[SignalItem]:
    return [
        SignalItem(
            title="先收紧风控",
            level="风险",
            reason=f"当前数据质量为{quality.level}，存在{reason}；不要按单个实时价机械卖出，优先等待支撑位或20日线收盘确认。",
        )
    ]


QUALITY_BLOCK_RULES = (
    QualityBlockRule("pause_buy_points", "buy", _blocked_buy_items),
    QualityBlockRule("pause_t_plan", "t", _blocked_t_items),
)


__all__ = ["QUALITY_BLOCK_RULES", "gate_signal_items", "quality_blocks_active_signals", "quality_reason"]
