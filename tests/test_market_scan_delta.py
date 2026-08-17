from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.artifacts.io import canonical_json_text, sha256_hex
from app.models.market_scan import (
    MarketScanMode,
    MarketScanResultWrite,
    MarketScanRun,
    MarketScanSeed,
)
from app.models.market_scan_delta import MarketScanDeltaResponse
from app.repositories.market_scan import MarketScanRepository
from app.services.cache import SQLiteCache
from app.services.market_scan_delta import MarketScanDeltaService
from app.services.market_scan_universe import FULL_MARKET_SCOPE


def test_delta_is_same_cohort_deterministic_and_separates_unrankable(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "delta.sqlite3")
    repo = cache.market_scan_repo
    service = MarketScanDeltaService(cache.repositories.market_scan_delta)
    symbols = tuple(f"{index:06d}.SH" for index in range(1, 122))
    seeds = [_seed(symbol, industry="银行" if index <= 60 else "软件") for index, symbol in enumerate(symbols, 1)]

    previous = _published(repo, seeds, _writes(symbols, quote_source="source-a"), data_date="2026-08-10")

    current_writes = _writes(symbols, quote_source="source-a")
    # Previous #1 remains present but unrankable; it must not be reported as a Top-N exit.
    current_writes[0] = replace(
        current_writes[0],
        status="missing",
        score=None,
        raw_score=None,
        trend_score=None,
        leader_score=None,
        data_quality_score=None,
        error="quote unavailable",
    )
    # Move old #30 into Top20, and expose a source change within the Top100 union.
    current_writes[29] = replace(current_writes[29], raw_score=100.0, score=100)
    current_writes[1] = replace(
        current_writes[1],
        quote_source="source-b",
        quote_fallback_used=True,
        degradation_reasons=("quote_fallback",),
    )
    current = _published(repo, seeds, current_writes, data_date="2026-08-11")

    first = service.compare(current.id)
    second = service.compare(current.id)

    assert first == second
    assert first.canonical_digest == second.canonical_digest
    assert first.status == "ready"
    assert first.previous is not None and first.previous.run_id == previous.id
    top20 = first.top_buckets[0]
    assert "000001.SH" not in {item.symbol for item in top20.exits}
    unrankable = {item.symbol: item for item in top20.present_but_unrankable}
    assert unrankable["000001.SH"].reason_codes == (
        "present_but_unrankable",
        "current_status_missing",
    )
    assert "000030.SH" in {item.symbol for item in top20.entrants}
    changes = {item.symbol: item.reason_codes for item in first.evidence_changes}
    assert changes["000002.SH"] == (
        "quote_source_changed",
        "quote_fallback_changed",
        "degradation_reasons_changed",
    )


def test_delta_ignores_newer_different_cohorts_and_selects_immediate_older_match(
    tmp_path: Path,
) -> None:
    cache = SQLiteCache(tmp_path / "cohort.sqlite3")
    repo = cache.market_scan_repo
    service = MarketScanDeltaService(cache.repositories.market_scan_delta)
    seeds = [_seed("600001.SH")]
    writes = [_write("600001.SH", raw_score=80.0)]
    older = _published(repo, seeds, writes, data_date="2026-08-08")
    _published(repo, seeds, writes, data_date="2026-08-09", rule_version="other-rule")
    _published(repo, seeds, writes, data_date="2026-08-10", mode="preopen")
    current = _published(repo, seeds, writes, data_date="2026-08-11")

    result = service.compare(current.id)

    assert result.status == "ready"
    assert result.previous is not None and result.previous.run_id == older.id


def test_delta_returns_explicit_unavailable_for_missing_previous_and_unpublished(
    tmp_path: Path,
) -> None:
    cache = SQLiteCache(tmp_path / "unavailable.sqlite3")
    repo = cache.market_scan_repo
    service = MarketScanDeltaService(cache.repositories.market_scan_delta)
    seeds = [_seed("600001.SH")]
    published = _published(repo, seeds, [_write("600001.SH")], data_date="2026-08-10")

    no_previous = service.compare(published.id)
    assert no_previous.status == "unavailable"
    assert no_previous.unavailable_reason == "previous_same_cohort_not_found"
    assert no_previous.previous is None

    running = repo.create_run(
        trigger="manual",
        mode="official",
        rule_version="delta-rule-v1",
        as_of="2026-08-11 15:30:00",
        data_date="2026-08-11",
        quote_date="2026-08-11",
        scope=FULL_MARKET_SCOPE,
    )
    repo.start_run(running.id)
    unavailable = service.compare(running.id)
    assert unavailable.status == "unavailable"
    assert unavailable.unavailable_reason == "current_not_published"


def test_delta_rejects_non_full_market_even_when_published(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "scope.sqlite3")
    repo = cache.market_scan_repo
    service = MarketScanDeltaService(cache.repositories.market_scan_delta)
    seeds = [_seed("600001.SH")]
    partial = _published(
        repo,
        seeds,
        [_write("600001.SH")],
        data_date="2026-08-10",
        scope="TOP100快速更新评分",
    )

    result = service.compare(partial.id)

    assert result.status == "unavailable"
    assert result.unavailable_reason == "current_not_full_market"


def test_delta_contract_rejects_tampered_or_resealed_inconsistent_history(
    tmp_path: Path,
) -> None:
    cache = SQLiteCache(tmp_path / "tamper.sqlite3")
    repo = cache.market_scan_repo
    symbols = tuple(f"{index:06d}.SH" for index in range(1, 122))
    seeds = [_seed(symbol) for symbol in symbols]
    _published(repo, seeds, _writes(symbols, quote_source="a"), data_date="2026-08-10")
    current = _published(repo, seeds, _writes(symbols, quote_source="a"), data_date="2026-08-11")
    baseline = MarketScanDeltaService(cache.repositories.market_scan_delta).compare(current.id)

    unsealed = baseline.model_dump(mode="json")
    unsealed["summary"]["current_present_count"] += 1
    with pytest.raises(ValueError, match="canonical_digest"):
        MarketScanDeltaResponse.model_validate(unsealed)

    bad_bucket = baseline.model_dump(mode="json")
    bad_bucket["top_buckets"][0]["retained_count"] -= 1
    _reseal_delta(bad_bucket)
    with pytest.raises(ValueError, match="Top-N.*不守恒"):
        MarketScanDeltaResponse.model_validate(bad_bucket)

    bad_time = baseline.model_dump(mode="json")
    bad_time["previous"]["finished_at"] = bad_time["current"]["finished_at"]
    bad_time["previous"]["snapshot_sealed_at"] = bad_time["current"]["snapshot_sealed_at"]
    _reseal_delta(bad_time)
    with pytest.raises(ValueError, match="完成时间必须早于"):
        MarketScanDeltaResponse.model_validate(bad_time)


def _reseal_delta(payload: dict[str, object]) -> None:
    unsigned = {key: value for key, value in payload.items() if key != "canonical_digest"}
    payload["canonical_digest"] = sha256_hex(canonical_json_text(unsigned))


def _published(
    repo: MarketScanRepository,
    seeds: list[MarketScanSeed],
    writes: list[MarketScanResultWrite],
    *,
    data_date: str,
    rule_version: str = "delta-rule-v1",
    mode: MarketScanMode = "official",
    scope: str = FULL_MARKET_SCOPE,
) -> MarketScanRun:
    run = repo.create_run(
        trigger="manual",
        mode=mode,
        rule_version=rule_version,
        as_of=f"{data_date} 15:30:00",
        data_date=data_date,
        quote_date=data_date,
        scope=scope,
    )
    repo.start_run(run.id)
    repo.seed_results(run.id, seeds, excluded_count=0)
    repo.save_result_batch(run.id, writes)
    status = (
        "degraded"
        if any(item.status != "success" or item.degradation_reasons for item in writes)
        else "success"
    )
    return repo.finish_run(run.id, status, message="published")


def _seed(symbol: str, *, industry: str = "银行") -> MarketScanSeed:
    code, market = symbol.split(".")
    return MarketScanSeed(
        symbol=symbol,
        code=code,
        market=market,
        name=f"股票{code}",
        industry=industry,
        metadata_source="metadata-a",
    )


def _writes(symbols: tuple[str, ...], *, quote_source: str) -> list[MarketScanResultWrite]:
    return [
        _write(symbol, raw_score=100.0 - index * 0.5, quote_source=quote_source)
        for index, symbol in enumerate(symbols, 1)
    ]


def _write(
    symbol: str,
    *,
    raw_score: float = 80.0,
    quote_source: str = "source-a",
) -> MarketScanResultWrite:
    return MarketScanResultWrite(
        symbol=symbol,
        status="success",
        score=min(100, round(raw_score)),
        raw_score=min(100.0, raw_score),
        trend_score=80,
        leader_score=75,
        data_quality_score=95,
        price=10.0,
        change_pct=1.0,
        turnover_rate=2.0,
        volume_ratio=1.2,
        amount=100_000_000.0,
        metrics={"ma20": 9.5},
        reason="test score",
        quote_source=quote_source,
        quote_timestamp="2026-08-11 15:00:00",
        kline_source="kline-a",
        adjustment_mode="qfq",
        data_date="2026-08-11",
    )
