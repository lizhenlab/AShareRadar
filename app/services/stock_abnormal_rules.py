from __future__ import annotations

from collections.abc import Callable

from app.models.schemas import AbnormalEventItem
from app.services.indicators import pct_change
from app.services.stock_abnormal_context import AbnormalEventContext

Detector = Callable[[AbnormalEventContext], AbnormalEventItem | None]


def detect_abnormal_events(context: AbnormalEventContext) -> list[AbnormalEventItem]:
    events: list[AbnormalEventItem] = []
    for detector in _DETECTORS:
        item = detector(context)
        if item:
            events.append(item)
    return events


def _event(
    context: AbnormalEventContext,
    *,
    title: str,
    level: str,
    direction: str,
    description: str,
    evidence: list[str],
    watch_points: list[str],
) -> AbnormalEventItem:
    return AbnormalEventItem(
        date=context.latest_date,
        title=title,
        level=level,
        direction=direction,
        description=description,
        evidence=evidence,
        watch_points=watch_points,
    )


def _volume_up(context: AbnormalEventContext) -> AbnormalEventItem | None:
    if not context.volume_ratio or context.volume_ratio < 1.8 or context.change_pct <= 1:
        return None
    return _event(
        context,
        title="放量上涨",
        level="积极" if context.change_pct < 7 else "观察",
        direction="向上",
        description=f"成交量约为近5日均量 {context.volume_ratio:.2f} 倍，价格上涨 {context.change_pct:.2f}%。",
        evidence=[f"量比估算 {context.volume_ratio:.2f}", f"涨跌幅 {context.change_pct:.2f}%"],
        watch_points=["次日若缩量跌回突破位，信号需要降级。"],
    )


def _volume_down(context: AbnormalEventContext) -> AbnormalEventItem | None:
    if not context.volume_ratio or context.volume_ratio < 1.8 or context.change_pct >= -1:
        return None
    return _event(
        context,
        title="放量下跌",
        level="风险",
        direction="向下",
        description=f"成交量约为近5日均量 {context.volume_ratio:.2f} 倍，同时价格下跌 {abs(context.change_pct):.2f}%。",
        evidence=[f"量比估算 {context.volume_ratio:.2f}", f"跌幅 {abs(context.change_pct):.2f}%"],
        watch_points=["先观察是否止跌放缓，避免把放量下跌误判成洗盘。"],
    )


def _gap_up(context: AbnormalEventContext) -> AbnormalEventItem | None:
    quote = context.quote
    if not context.prev_close or quote.open < context.prev_close * 1.015:
        return None
    return _event(
        context,
        title="向上跳空",
        level="积极" if quote.price >= quote.open else "观察",
        direction="向上",
        description=f"开盘价较昨收高开 {pct_change(quote.open, context.prev_close):.2f}%。",
        evidence=[f"开盘 {quote.open:.2f}", f"昨收 {context.prev_close:.2f}"],
        watch_points=["观察缺口是否被快速回补。"],
    )


def _gap_down(context: AbnormalEventContext) -> AbnormalEventItem | None:
    quote = context.quote
    if not context.prev_close or quote.open > context.prev_close * 0.985:
        return None
    return _event(
        context,
        title="向下跳空",
        level="风险",
        direction="向下",
        description=f"开盘价较昨收低开 {abs(pct_change(quote.open, context.prev_close)):.2f}%。",
        evidence=[f"开盘 {quote.open:.2f}", f"昨收 {context.prev_close:.2f}"],
        watch_points=["向下缺口未回补前，短线反弹质量要打折。"],
    )


def _upper_shadow(context: AbnormalEventContext) -> AbnormalEventItem | None:
    quote = context.quote
    if context.upper_shadow_pct < 2.5 or context.upper_shadow_pct <= context.lower_shadow_pct * 1.4:
        return None
    return _event(
        context,
        title="长上影压力",
        level="风险" if context.change_pct <= 0 else "观察",
        direction="压力",
        description=f"上影线约 {context.upper_shadow_pct:.2f}%，盘中冲高后承压。",
        evidence=[f"最高 {quote.high:.2f}", f"现价 {quote.price:.2f}"],
        watch_points=["若后续不能重新站回上影线中部，压力仍在。"],
    )


def _lower_shadow(context: AbnormalEventContext) -> AbnormalEventItem | None:
    quote = context.quote
    if context.lower_shadow_pct < 2.5 or context.lower_shadow_pct <= context.upper_shadow_pct * 1.4:
        return None
    return _event(
        context,
        title="长下影承接",
        level="积极" if context.change_pct >= 0 else "观察",
        direction="承接",
        description=f"下影线约 {context.lower_shadow_pct:.2f}%，低位出现承接迹象。",
        evidence=[f"最低 {quote.low:.2f}", f"现价 {quote.price:.2f}"],
        watch_points=["承接需要后续放量站稳短期均线确认。"],
    )


def _near_limit_up(context: AbnormalEventContext) -> AbnormalEventItem | None:
    if context.change_pct < 9:
        return None
    return _event(
        context,
        title="接近涨停",
        level="积极",
        direction="向上",
        description=f"涨幅 {context.change_pct:.2f}%，短线情绪很强。",
        evidence=[f"涨跌幅 {context.change_pct:.2f}%"],
        watch_points=["高情绪日后要关注开板、放量滞涨和次日承接。"],
    )


def _near_limit_down(context: AbnormalEventContext) -> AbnormalEventItem | None:
    if context.change_pct > -9:
        return None
    return _event(
        context,
        title="接近跌停",
        level="风险",
        direction="向下",
        description=f"跌幅 {abs(context.change_pct):.2f}%，短线风险释放剧烈。",
        evidence=[f"涨跌幅 {context.change_pct:.2f}%"],
        watch_points=["先等待流动性和承接恢复，不急于判断反转。"],
    )


def _wide_amplitude(context: AbnormalEventContext) -> AbnormalEventItem | None:
    quote = context.quote
    if context.amplitude_pct < 6:
        return None
    return _event(
        context,
        title="日内大振幅",
        level="观察",
        direction="波动",
        description=f"日内振幅约 {context.amplitude_pct:.2f}%，多空分歧较大。",
        evidence=[f"最高 {quote.high:.2f}", f"最低 {quote.low:.2f}"],
        watch_points=["振幅放大时，策略参考价要留出更宽容错。"],
    )


_DETECTORS: tuple[Detector, ...] = (
    _volume_up,
    _volume_down,
    _gap_up,
    _gap_down,
    _upper_shadow,
    _lower_shadow,
    _near_limit_up,
    _near_limit_down,
    _wide_amplitude,
)


__all__ = ["detect_abnormal_events"]
