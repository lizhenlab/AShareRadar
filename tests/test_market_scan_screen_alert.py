from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from app.db.market_scan_action_source import MarketScanActionSourceError
from app.market_scan_screening import screen_spec_digest, screen_spec_from_discovery
from app.models.discovery import (
    DiscoveryCriteria,
    DiscoveryPresetCreate,
    DiscoveryScoreRange,
    DiscoverySort,
)
from app.models.market_scan import (
    MarketScanMode,
    MarketScanResultWrite,
    MarketScanRun,
    MarketScanSeed,
)
from app.repositories.market_scan_screen_alert import (
    MarketScanScreenAlertPresetRevisionError,
    MarketScanScreenAlertRepository,
)
from app.services.cache import SQLiteCache
from app.services.market_scan_screen_alert import MarketScanScreenAlertService
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from tests.market_scan_test_support import (
    action_pass_publication_diagnostics,
    distribution_degraded_publication_diagnostics,
)


def test_saved_screen_change_event_is_compiler_driven_and_idempotent(tmp_path: Path) -> None:
    cache, service = _runtime(tmp_path)
    preset = _preset(cache)
    seeds = [_seed(index) for index in range(1, 5)]
    previous = _published(
        cache,
        seeds,
        [_write(1, 90), _write(2, 85), _write(3, 70), _write(4, 88)],
        data_date="2026-08-10",
    )
    current = _published(
        cache,
        seeds,
        [_write(1, 70), _write(2, 86), _write(3, 92), _missing(4)],
        data_date="2026-08-11",
    )

    first = service.record(
        preset_id=preset.id,
        current_run_id=current.id,
        expected_preset_revision=preset.revision,
    )
    second = service.record(
        preset_id=preset.id,
        current_run_id=current.id,
        expected_preset_revision=preset.revision,
    )

    expected_spec = screen_spec_from_discovery(preset.criteria, preset.sort)
    assert first.preset.spec_digest == screen_spec_digest(expected_spec)
    assert first.previous is not None and first.previous.run_id == previous.id
    assert first.entered_symbols == ("600003.SH",)
    assert first.exited_symbols == ("600001.SH",)
    assert first.suppressed_unrankable_symbols == ("600004.SH",)
    assert first.created is True
    assert second.created is False
    assert second.event_digest == first.event_digest
    assert _event_count(cache.path) == 1


def test_distribution_degraded_scan_cannot_persist_screen_alert_event(
    tmp_path: Path,
) -> None:
    cache, service = _runtime(tmp_path)
    preset = _preset(cache)
    seeds = [_seed(1)]
    _published(cache, seeds, [_write(1, 70)], data_date="2026-08-10")
    current = _published(
        cache,
        seeds,
        [_write(1, 90)],
        data_date="2026-08-11",
        action_eligible=False,
    )

    with pytest.raises(MarketScanActionSourceError, match="评分分布门禁"):
        service.record(preset_id=preset.id, current_run_id=current.id)

    assert _event_count(cache.path) == 0


def test_no_previous_same_cohort_is_typed_unavailable_and_writes_nothing(tmp_path: Path) -> None:
    cache, service = _runtime(tmp_path)
    preset = _preset(cache)
    current = _published(
        cache,
        [_seed(1)],
        [_write(1, 90)],
        data_date="2026-08-11",
    )

    result = service.record(preset_id=preset.id, current_run_id=current.id)

    assert result.status == "unavailable"
    assert result.unavailable_reason == "previous_same_cohort_not_found"
    assert result.previous is None
    assert result.created is False
    assert _event_count(cache.path) == 0


def test_alert_selects_immediate_older_same_mode_scope_and_rule(tmp_path: Path) -> None:
    cache, service = _runtime(tmp_path)
    preset = _preset(cache)
    seeds = [_seed(1)]
    matching = _published(cache, seeds, [_write(1, 90)], data_date="2026-08-07")
    _published(
        cache,
        seeds,
        [_write(1, 90)],
        data_date="2026-08-08",
        rule_version="other-rule",
    )
    _published(
        cache,
        seeds,
        [_write(1, 90)],
        data_date="2026-08-09",
        mode="preopen",
    )
    _published(
        cache,
        seeds,
        [_write(1, 90)],
        data_date="2026-08-10",
        scope="TOP100快速更新评分",
    )
    current = _published(cache, seeds, [_write(1, 90)], data_date="2026-08-11")

    result = service.record(preset_id=preset.id, current_run_id=current.id)

    assert result.status == "ready"
    assert result.previous is not None and result.previous.run_id == matching.id


@pytest.mark.parametrize(
    ("run_kind", "reason"),
    [
        ("running", "current_not_published"),
        ("partial", "current_not_full_market"),
    ],
)
def test_ineligible_current_run_is_typed_unavailable(
    tmp_path: Path,
    run_kind: str,
    reason: str,
) -> None:
    cache, service = _runtime(tmp_path)
    preset = _preset(cache)
    if run_kind == "running":
        current = _running(cache, data_date="2026-08-11")
    else:
        current = _published(
            cache,
            [_seed(1)],
            [_write(1, 90)],
            data_date="2026-08-11",
            scope="TOP100快速更新评分",
        )

    result = service.record(preset_id=preset.id, current_run_id=current.id)

    assert result.status == "unavailable"
    assert result.unavailable_reason == reason
    assert result.created is False
    assert _event_count(cache.path) == 0


def test_expected_preset_revision_fails_closed(tmp_path: Path) -> None:
    cache, service = _runtime(tmp_path)
    preset = _preset(cache)
    current = _running(cache, data_date="2026-08-11")

    with pytest.raises(MarketScanScreenAlertPresetRevisionError, match="修订冲突"):
        service.record(
            preset_id=preset.id,
            current_run_id=current.id,
            expected_preset_revision=preset.revision + 1,
        )


def _runtime(tmp_path: Path) -> tuple[SQLiteCache, MarketScanScreenAlertService]:
    cache = SQLiteCache(tmp_path / "screen-alert.sqlite3")
    repository = MarketScanScreenAlertRepository(cache.path, cache._lock)
    return cache, MarketScanScreenAlertService(repository, now=lambda: "2026-08-12 08:00:00")


def _preset(cache: SQLiteCache):
    return cache.discovery_repo.create_preset(
        DiscoveryPresetCreate(
            name="高分提醒",
            criteria=DiscoveryCriteria(score=DiscoveryScoreRange(min=80)),
            sort=[DiscoverySort(field="rank", order="asc")],
        ),
        timestamp="2026-08-01 08:00:00",
    )


def _published(
    cache: SQLiteCache,
    seeds: list[MarketScanSeed],
    writes: list[MarketScanResultWrite],
    *,
    data_date: str,
    mode: MarketScanMode = "official",
    rule_version: str = "screen-alert-rule-v1",
    scope: str = FULL_MARKET_SCOPE,
    action_eligible: bool = True,
) -> MarketScanRun:
    run = _running(
        cache,
        data_date=data_date,
        mode=mode,
        rule_version=rule_version,
        scope=scope,
    )
    cache.market_scan_repo.seed_results(run.id, seeds, excluded_count=0)
    dated = [replace(item, data_date=data_date) for item in writes]
    cache.market_scan_repo.save_result_batch(run.id, dated)
    status = "degraded" if any(item.status != "success" for item in dated) else "success"
    return cache.market_scan_repo.finish_run(
        run.id,
        status,
        message="published",
        publication_diagnostics=(
            action_pass_publication_diagnostics()
            if action_eligible
            else distribution_degraded_publication_diagnostics()
        ),
    )


def _running(
    cache: SQLiteCache,
    *,
    data_date: str,
    mode: MarketScanMode = "official",
    rule_version: str = "screen-alert-rule-v1",
    scope: str = FULL_MARKET_SCOPE,
) -> MarketScanRun:
    run = cache.market_scan_repo.create_run(
        trigger="manual",
        mode=mode,
        rule_version=rule_version,
        as_of=f"{data_date} 15:30:00",
        data_date=data_date,
        quote_date=data_date,
        scope=scope,
    )
    return cache.market_scan_repo.start_run(run.id)


def _seed(index: int) -> MarketScanSeed:
    code = f"600{index:03d}"
    return MarketScanSeed(
        symbol=f"{code}.SH",
        code=code,
        market="SH",
        name=f"股票{index}",
        industry="银行",
        metadata_source="metadata-a",
    )


def _write(index: int, score: int) -> MarketScanResultWrite:
    symbol = _seed(index).symbol
    return MarketScanResultWrite(
        symbol=symbol,
        status="success",
        score=score,
        raw_score=float(score),
        trend_score=score,
        leader_score=score,
        data_quality_score=95,
        price=10.0,
        change_pct=1.0,
        turnover_rate=2.0,
        volume_ratio=1.2,
        amount=100_000_000.0,
        metrics={"ma20": 9.5},
        reason="test score",
        quote_timestamp="2026-08-11 15:00:00",
        quote_source="quote-a",
        kline_source="kline-a",
        adjustment_mode="qfq",
        data_date="2026-08-11",
    )


def _missing(index: int) -> MarketScanResultWrite:
    return MarketScanResultWrite(symbol=_seed(index).symbol, status="missing", error="quote unavailable")


def _event_count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM discovery_screen_alert_event").fetchone()[0])
