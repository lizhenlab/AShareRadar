"""Strict integrity contract for one frozen production full-market snapshot."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
import hashlib
import json

from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MarketScanResultItem,
    MarketScanRun,
)


class MarketScanSnapshotIntegrityError(ValueError):
    """Raised when persisted market-scan evidence changes across a read boundary."""


class FrozenFullMarketSnapshotIntegrityError(MarketScanSnapshotIntegrityError):
    """Raised when a frozen production snapshot is incomplete or self-inconsistent."""


@dataclass(frozen=True)
class FrozenFullMarketSnapshotIntegrity:
    run_id: int
    result_count: int
    success_count: int
    missing_count: int
    skipped_count: int
    production_score_rule_version: str
    production_score_spec_hash: str


def validate_frozen_full_market_snapshot(
    run: MarketScanRun,
    items: Sequence[MarketScanResultItem],
) -> FrozenFullMarketSnapshotIntegrity:
    """Rebuild and seal all invariants required by frozen strategy consumers."""

    _validate_run_header(run)
    counts: Counter[str] = Counter(str(item.status) for item in items)
    _validate_result_population(run, items, counts)
    score_rule, score_hash = _validate_success_score_contract(run, items)
    return FrozenFullMarketSnapshotIntegrity(
        run_id=run.id,
        result_count=len(items),
        success_count=counts["success"],
        missing_count=counts["missing"],
        skipped_count=counts["skipped"],
        production_score_rule_version=score_rule,
        production_score_spec_hash=score_hash,
    )


def validate_market_scan_run_binding(
    expected: MarketScanRun,
    observed: MarketScanRun,
) -> None:
    """Require a terminal read to remain bound to one complete cohort header."""

    validate_market_scan_cohort_binding(expected, observed)
    fields = (
        "status",
        "total_count",
        "excluded_count",
        "processed_count",
        "success_count",
        "missing_count",
        "skipped_count",
        "finished_at",
        "duration_ms",
        "quote_capture_started_at",
        "quote_capture_finished_at",
        "quote_capture_duration_ms",
        "quote_capture_count",
        "snapshot_digest",
        "snapshot_seal_origin",
        "snapshot_sealed_at",
    )
    mismatches = [name for name in fields if getattr(expected, name) != getattr(observed, name)]
    if mismatches:
        raise MarketScanSnapshotIntegrityError(
            f"全市场榜单读取期间冻结批次绑定发生变化：{','.join(mismatches)}"
        )


def validate_market_scan_cohort_binding(
    expected: MarketScanRun,
    observed: MarketScanRun,
) -> None:
    """Require immutable cohort identity to remain stable, including for active runs."""

    fields = (
        "id",
        "retry_of_run_id",
        "trigger",
        "mode",
        "scope",
        "stock_pool_source",
        "rule_version",
        "as_of",
        "data_date",
        "quote_date",
    )
    mismatches = [name for name in fields if getattr(expected, name) != getattr(observed, name)]
    if mismatches:
        raise MarketScanSnapshotIntegrityError(
            f"全市场榜单读取期间冻结批次绑定发生变化：{','.join(mismatches)}"
        )


def _validate_run_header(run: MarketScanRun) -> None:
    if run.status not in {"success", "degraded"}:
        raise FrozenFullMarketSnapshotIntegrityError("冻结全市场快照只接受已发布批次")
    if run.mode not in {"official", "intraday"}:
        raise FrozenFullMarketSnapshotIntegrityError("冻结全市场快照只接受盘后正式或盘中临时批次")
    if run.scope != MARKET_SCAN_FULL_MARKET_SCOPE:
        raise FrozenFullMarketSnapshotIntegrityError("冻结全市场快照只接受完整全市场批次")
    try:
        quote_date = date.fromisoformat(run.quote_date)
        data_date = date.fromisoformat(run.data_date)
    except ValueError as exc:
        raise FrozenFullMarketSnapshotIntegrityError("冻结全市场快照日期格式无效") from exc
    if quote_date.isoformat() != run.quote_date or data_date.isoformat() != run.data_date:
        raise FrozenFullMarketSnapshotIntegrityError("冻结全市场快照日期不是规范 ISO 日期")
    if run.mode == "official" and quote_date != data_date:
        raise FrozenFullMarketSnapshotIntegrityError("冻结全市场快照要求行情日与数据日一致")
    if run.mode == "intraday" and quote_date < data_date:
        raise FrozenFullMarketSnapshotIntegrityError("盘中冻结快照的行情日不能早于完整日K截止日")


def _validate_result_population(
    run: MarketScanRun,
    items: Sequence[MarketScanResultItem],
    counts: Counter[str],
) -> None:
    if counts["pending"]:
        raise FrozenFullMarketSnapshotIntegrityError("已发布冻结批次仍包含待处理结果")
    expected_counts = {
        "success": run.success_count,
        "missing": run.missing_count,
        "skipped": run.skipped_count,
    }
    actual_counts = {name: counts[name] for name in expected_counts}
    if actual_counts != expected_counts:
        raise FrozenFullMarketSnapshotIntegrityError("冻结批次头部状态计数与完整结果集不一致")
    if run.total_count != len(items) or run.processed_count != len(items):
        raise FrozenFullMarketSnapshotIntegrityError("冻结批次头部总数与完整结果集不一致")
    if sum(expected_counts.values()) != len(items):
        raise FrozenFullMarketSnapshotIntegrityError("冻结批次头部终态计数无法覆盖完整结果集")
    _validate_result_identities(run, items)
    _validate_success_ranks(run, items)


def _validate_result_identities(
    run: MarketScanRun,
    items: Sequence[MarketScanResultItem],
) -> None:
    if any(item.run_id != run.id for item in items):
        raise FrozenFullMarketSnapshotIntegrityError("冻结结果包含其他批次的数据")
    symbols = [item.symbol for item in items]
    if any(not symbol or symbol != symbol.strip() for symbol in symbols):
        raise FrozenFullMarketSnapshotIntegrityError("冻结结果包含无效股票代码")
    if len(set(symbols)) != len(symbols):
        raise FrozenFullMarketSnapshotIntegrityError("冻结结果股票代码不唯一")


def _validate_success_ranks(
    run: MarketScanRun,
    items: Sequence[MarketScanResultItem],
) -> None:
    successful = [item for item in items if item.status == "success"]
    ranks: list[int] = []
    for item in successful:
        if item.rank is None:
            raise FrozenFullMarketSnapshotIntegrityError("成功结果缺少生产排名")
        ranks.append(item.rank)
    if sorted(ranks) != list(range(1, len(successful) + 1)):
        raise FrozenFullMarketSnapshotIntegrityError("成功结果生产排名必须唯一且从 1 连续")
    if any(item.rank is not None for item in items if item.status != "success"):
        raise FrozenFullMarketSnapshotIntegrityError("非成功结果不得携带生产排名")
    if any(item.data_date != run.data_date for item in successful):
        raise FrozenFullMarketSnapshotIntegrityError("成功结果数据日与冻结批次不一致")


def _validate_score_values(items: Sequence[MarketScanResultItem]) -> None:
    score_fields = ("score", "raw_score", "trend_score", "leader_score", "data_quality_score")
    successful = [item for item in items if item.status == "success"]
    if any(
        getattr(item, field) is None
        for item in successful
        for field in score_fields
    ):
        raise FrozenFullMarketSnapshotIntegrityError("成功结果缺少完整生产评分字段")
    if any(
        getattr(item, field) is not None
        for item in items
        if item.status != "success"
        for field in score_fields
    ):
        raise FrozenFullMarketSnapshotIntegrityError("非成功结果不得携带生产评分字段")


def _validate_success_score_contract(
    run: MarketScanRun,
    items: Sequence[MarketScanResultItem],
) -> tuple[str, str]:
    contracts: set[tuple[str, str]] = set()
    successful = [item for item in items if item.status == "success"]
    if not successful:
        raise FrozenFullMarketSnapshotIntegrityError("冻结全市场快照缺少成功评分结果")
    _validate_score_values(items)
    for item in successful:
        score_spec = item.score_details.get("score_spec")
        score_hash = item.score_details.get("score_spec_hash")
        run_rule = item.score_details.get("run_rule_version")
        if not isinstance(score_spec, dict) or not isinstance(score_hash, str):
            raise FrozenFullMarketSnapshotIntegrityError("成功结果缺少生产评分合同或摘要")
        score_rule = score_spec.get("rule_version")
        if not isinstance(score_rule, str) or not score_rule or score_rule != score_rule.strip():
            raise FrozenFullMarketSnapshotIntegrityError("成功结果缺少生产评分规则版本")
        if run_rule != run.rule_version:
            raise FrozenFullMarketSnapshotIntegrityError("成功结果扫描规则版本与冻结批次不一致")
        if score_hash != _stable_score_spec_hash(score_spec):
            raise FrozenFullMarketSnapshotIntegrityError(
                "冻结批次包含不一致的生产评分合同：评分规范摘要校验失败"
            )
        contracts.add((score_rule, score_hash))
    if len(contracts) != 1:
        raise FrozenFullMarketSnapshotIntegrityError("冻结批次包含不一致的生产评分合同")
    return next(iter(contracts))


def _stable_score_spec_hash(spec: dict[str, object]) -> str:
    try:
        canonical = json.dumps(
            spec,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FrozenFullMarketSnapshotIntegrityError("生产评分合同不是可规范化的有限 JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "FrozenFullMarketSnapshotIntegrity",
    "FrozenFullMarketSnapshotIntegrityError",
    "MarketScanSnapshotIntegrityError",
    "validate_frozen_full_market_snapshot",
    "validate_market_scan_cohort_binding",
    "validate_market_scan_run_binding",
]
