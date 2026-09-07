from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from app.models.market import Quote
from app.models.market_scan import MarketScanMode, MarketScanResultItem, MarketScanResultWrite, MarketScanSeed
from app.services.market_scan_score_dimensions import (
    verify_market_scan_point_in_time_evidence,
    verify_market_scan_point_in_time_evidence_context,
)
from app.services.market_scan_scoring import (
    replay_score_details,
    score_market_scan_item,
    verify_persisted_market_scan_result,
)
from app.utils.clock import ASHARE_TIMEZONE
from tests.test_market_scan_scoring import _quote, _rows
from tests.test_market_scan_skip_contract import _mode_context, _running_repository


def _quote_at(timestamp: str, *, mode: MarketScanMode) -> Quote:
    quote = _quote(timestamp=timestamp)
    if mode == "intraday":
        return quote.model_copy(
            update={"prev_close": 10.5, "price": 10.6, "change": 0.1, "change_pct": 0.1 / 10.5 * 100},
        )
    return quote


def _score(
    item: MarketScanResultItem,
    timestamp: str,
    *,
    mode: MarketScanMode,
    rule_version: str,
) -> MarketScanResultWrite:
    as_of, data_date, quote_date, observed, _event = _mode_context(mode)
    return replace(
        score_market_scan_item(
            item, _quote_at(timestamp, mode=mode), _rows(data_date, 80),
            as_of=as_of, completed_cutoff=data_date, expected_data_date=data_date,
            expected_quote_date=quote_date, min_history_rows=61, min_data_quality_score=0,
            mode=mode, rule_version=rule_version, quote_observed_at=observed,
        ),
        quote_observed_at=observed,
    )


def _evidence(result: MarketScanResultWrite) -> dict[str, object]:
    return result.score_details["components"]["score_dimensions"]["point_in_time_evidence"]


def _persisted(item: MarketScanResultItem, result: MarketScanResultWrite) -> MarketScanResultItem:
    return item.model_copy(update=asdict(result))


@pytest.mark.parametrize("mode", ["official", "preopen", "intraday"])
@pytest.mark.parametrize("offset_hours", [8, 0, -12])
def test_equivalent_quote_instants_use_market_date_through_persistence_and_replay(
    tmp_path: Path,
    mode: MarketScanMode,
    offset_hours: int,
) -> None:
    repo, run, item, _settings, rule_version = _running_repository(tmp_path, mode=mode)
    repo.refresh_pending_metadata(
        run.id,
        [MarketScanSeed(item.symbol, item.code, item.market, item.name,
                        industry="白酒", list_date="2001-08-27", metadata_source=item.metadata_source)],
    )
    item = repo.pending_items(run.id)[0]
    as_of, data_date, quote_date, _observed, local_timestamp = _mode_context(mode)
    event = datetime.fromisoformat(local_timestamp)
    timestamp = event.astimezone(timezone(timedelta(hours=offset_hours))).isoformat()
    baseline = _score(item, local_timestamp, mode=mode, rule_version=rule_version)
    result = _score(item, timestamp, mode=mode, rule_version=rule_version)

    evidence = _evidence(result)
    assert evidence["payload"]["quote_date"] == quote_date.isoformat()
    assert evidence["payload"]["quote_timestamp"] == timestamp
    assert result.quote_timestamp == timestamp
    assert verify_market_scan_point_in_time_evidence(evidence)
    assert verify_market_scan_point_in_time_evidence_context(
        evidence, item=_persisted(item, result), expected_data_date=data_date.isoformat(),
        expected_quote_date=quote_date.isoformat(), expected_as_of=as_of.isoformat(), expected_mode=mode,
    )
    assert (result.score, result.raw_score, result.trend_score, result.data_quality_score) == (
        baseline.score, baseline.raw_score, baseline.trend_score, baseline.data_quality_score,
    )
    assert replay_score_details(result.score_details) == replay_score_details(baseline.score_details)
    updated = repo.save_result_batch(run.id, [result])
    assert updated.success_count == 1
    verify_persisted_market_scan_result(_persisted(item, result), updated)


@pytest.mark.parametrize("mode", ["official", "preopen", "intraday"])
def test_historical_cross_date_evidence_is_read_without_rewriting_its_identity(
    tmp_path: Path,
    mode: MarketScanMode,
) -> None:
    _repo, _run, item, _settings, rule_version = _running_repository(tmp_path, mode=mode)
    as_of, data_date, quote_date, _observed, local_timestamp = _mode_context(mode)
    timestamp = datetime.fromisoformat(local_timestamp).astimezone(timezone(timedelta(hours=-12))).isoformat()
    result = _score(item, timestamp, mode=mode, rule_version=rule_version)
    archived = deepcopy(result)
    evidence = _evidence(archived)
    # Preserve the legacy generator's date-prefix bytes, including its old context failure.
    evidence["payload"]["quote_date"] = timestamp[:10]
    encoded_payload = json.dumps(evidence["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    evidence["payload_digest"] = hashlib.sha256(encoded_payload.encode("utf-8")).hexdigest()
    frozen = json.dumps(archived.score_details, ensure_ascii=False, sort_keys=True, allow_nan=False)

    assert verify_market_scan_point_in_time_evidence(evidence)
    assert not verify_market_scan_point_in_time_evidence_context(
        evidence, item=_persisted(item, archived), expected_data_date=data_date.isoformat(),
        expected_quote_date=quote_date.isoformat(), expected_as_of=as_of.isoformat(), expected_mode=mode,
    )
    assert replay_score_details(archived.score_details) == replay_score_details(result.score_details)
    assert json.dumps(archived.score_details, ensure_ascii=False, sort_keys=True, allow_nan=False) == frozen


def test_timezone_conversion_does_not_change_market_day_for_naive_local_timestamp(tmp_path: Path) -> None:
    _repo, _run, item, _settings, rule_version = _running_repository(tmp_path)
    local = datetime(2026, 7, 17, 15, 0)
    naive = _score(item, local.isoformat(sep=" "), mode="official", rule_version=rule_version)
    aware = _score(item, local.replace(tzinfo=ASHARE_TIMEZONE).isoformat(), mode="official", rule_version=rule_version)

    assert _evidence(naive)["payload"]["quote_date"] == date(2026, 7, 17).isoformat()
    assert replay_score_details(naive.score_details) == replay_score_details(aware.score_details)
