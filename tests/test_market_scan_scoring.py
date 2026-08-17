from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from app.artifacts.io import canonical_json_text, sha256_hex
from app.models.market_scan import MarketScanResultItem, MarketScanResultWrite, MarketScanRun
from app.models.schemas import Kline, StockInfo
from app.services.market_scan_scoring import (
    FULL_MARKET_SCORE_TIE_BREAK,
    MarketScanDataMissing,
    MarketScanReplayError,
    MarketScanSkipped,
    completed_market_scan_klines,
    market_scan_score_spec,
    market_scan_score_spec_v4,
    rank_score_details,
    replay_score_details,
    score_market_scan_item,
    stable_score_spec_hash,
    verify_persisted_market_scan_result,
)
from app.services.market_scan_score_dimensions import (
    MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
    MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION,
    MARKET_SCAN_EVIDENCE_LEGACY_V3_CONTRACT_VERSION,
    verify_market_scan_point_in_time_evidence,
    verify_market_scan_point_in_time_evidence_context,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE, build_market_scan_universe
from tests.factories import make_kline, make_quote, make_stock_info


AS_OF = datetime(2026, 7, 17, 16, 30)
DATA_DATE = date(2026, 7, 17)
PREOPEN_AS_OF = datetime(2026, 7, 20, 8, 0)


def test_market_scan_61_bar_contract_skips_60_and_scores_61() -> None:
    kwargs = {
        "as_of": AS_OF,
        "completed_cutoff": DATA_DATE,
        "expected_data_date": DATA_DATE,
        "min_history_rows": 60,
        "min_data_quality_score": 0,
    }

    available = completed_market_scan_klines(_rows(DATA_DATE, 90), DATA_DATE)
    rows_60 = available[-60:]
    rows_61 = available[-61:]

    with pytest.raises(MarketScanDataMissing, match="需要 61 根，当前 60 根"):
        score_market_scan_item(_item(), _quote(), rows_60, **kwargs)

    result = score_market_scan_item(_item(), _quote(), rows_61, **kwargs)

    assert result.status == "success"
    assert result.error is None
    evidence = result.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    assert len(evidence["payload"]["bar_contract_61"]) == 61


def test_market_scan_score_is_deterministic_and_keeps_metadata_tags() -> None:
    item = _item(is_st=True, is_new=True, list_date=None)
    quote = _quote(fallback_used=True)
    rows = _rows(DATA_DATE, 80)
    rows[-1] = rows[-1].model_copy(update={"fallback_used": True})

    first = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    second = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )

    assert first == second
    assert first.status == "success"
    assert all(0 <= value <= 100 for value in (first.score, first.trend_score, first.leader_score, first.data_quality_score))  # type: ignore[operator]
    assert {"ST", "新股", "兜底行情", "兜底K线", "上市日期未知"}.issubset(first.tags)
    assert {"close", "ma5", "ma20", "ma60", "high20", "low20", "volume_ratio"} == set(first.metrics)
    assert first.data_date == DATA_DATE.isoformat()
    assert first.adjustment_mode == "qfq"
    assert first.quote_fallback_used is True
    assert first.kline_fallback_used is True
    assert first.metadata_degraded is True
    assert first.degradation_reasons == (
        "quote_fallback",
        "kline_fallback",
        "list_date_missing",
    )
    assert "趋势强度" in (first.reason or "")
    assert "非上涨概率" in (first.reason or "")
    assert first.score_details["semantics"] == {
        "kind": "ordinal-cross-sectional-ranking",
        "expected_return": False,
        "probability": False,
        "benchmark": "none-in-production-score",
        "transaction_cost_model": "none-in-production-score",
        "execution_model": "none-in-production-score",
        "actionable": False,
    }


def test_preopen_score_matches_the_same_completed_official_snapshot() -> None:
    item = _item()
    quote = _quote()
    rows = _rows(DATA_DATE, 80)

    official = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        expected_quote_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        mode="official",
    )
    preopen = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=PREOPEN_AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        expected_quote_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        mode="preopen",
    )

    assert preopen.score == official.score
    assert preopen.raw_score == official.raw_score
    assert preopen.data_date == DATA_DATE.isoformat()
    assert preopen.quote_timestamp == quote.timestamp


def test_preopen_score_keeps_quote_and_kline_date_gates_strict() -> None:
    kwargs = {
        "as_of": PREOPEN_AS_OF,
        "completed_cutoff": DATA_DATE,
        "expected_data_date": DATA_DATE,
        "expected_quote_date": DATA_DATE,
        "min_history_rows": 60,
        "min_data_quality_score": 0,
        "mode": "preopen",
    }

    with pytest.raises(MarketScanDataMissing, match="报价日期.*不一致"):
        score_market_scan_item(
            _item(),
            _quote(timestamp="2026-07-16 15:00:00"),
            _rows(DATA_DATE, 80),
            **kwargs,
        )

    with pytest.raises(MarketScanDataMissing, match="早于应有交易日"):
        score_market_scan_item(
            _item(),
            _quote(),
            _rows(date(2026, 7, 16), 80),
            **kwargs,
        )


def test_preopen_score_requires_quote_close_to_match_completed_daily_close() -> None:
    quote = _quote().model_copy(
        update={"price": 10.7, "change": 0.7, "change_pct": 7.0},
    )

    with pytest.raises(MarketScanDataMissing, match="盘前复盘报价收盘价与上一完成交易日日K"):
        score_market_scan_item(
            _item(),
            quote,
            _rows(DATA_DATE, 80),
            as_of=PREOPEN_AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            expected_quote_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
            mode="preopen",
        )


def test_market_scan_persists_separate_score_dimensions_and_verifiable_point_in_time_evidence() -> None:
    result = score_market_scan_item(
        _item(),
        _quote(),
        _rows(DATA_DATE, 80),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )

    dimensions = result.score_details["components"]["score_dimensions"]
    scores = dimensions["scores"]
    evidence = dimensions["point_in_time_evidence"]

    assert dimensions["semantics"]["alpha"] == "ordinal-research-score-not-return-probability"
    assert all(0 <= scores[key] <= 100 for key in ("alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"))
    assert set(scores["decision_utility"]) == {"conservative", "balanced", "aggressive"}
    assert verify_market_scan_point_in_time_evidence(evidence) is True
    assert evidence["contract_version"] == MARKET_SCAN_EVIDENCE_CONTRACT_VERSION
    assert evidence["action_eligible"] is True
    assert evidence["payload"]["derived_scores"] == {
        key: scores[key]
        for key in ("alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability")
    }
    assert len(evidence["payload"]["dimension_spec_hash"]) == 64
    assert len(evidence["payload"]["bar_contract_61"]) == 61
    assert all(len(bar) == 10 for bar in evidence["payload"]["bar_contract_61"])
    assert {bar[9] for bar in evidence["payload"]["bar_contract_61"]} == {
        DATA_DATE.isoformat()
    }
    assert dimensions["algorithm"] == "full-market-dimensions-v4-session-coverage"
    assert dimensions["volume_context"]["snapshot_bar_position"] == "snapshot-session"
    coverage = evidence["payload"]["session_coverage"]
    assert coverage["status"] == "verified"
    assert coverage["observed_session_count"] == 61
    assert coverage["missing_session_count"] == 0
    assert coverage["max_gap_sessions"] == 0
    assert set(coverage["recent_windows"]) == {"5", "20", "60"}
    closes = [bar[2] for bar in evidence["payload"]["bar_contract_61"]]
    current = evidence["payload"]["quote_price"]
    features = dimensions["raw_features"]
    for horizon, reference in ((1, closes[-2]), (5, closes[-6]), (20, closes[-21]), (60, closes[-61])):
        assert features[f"return_{horizon}d_pct"] == pytest.approx(
            (current / reference - 1) * 100,
            abs=1e-4,
        )
    assert result.score_details["inputs"]["continuous_trend_return_5d_pct"] == pytest.approx(
        features["return_5d_pct"],
        abs=1e-4,
    )
    assert result.score_details["inputs"]["continuous_trend_return_20d_pct"] == pytest.approx(
        features["return_20d_pct"],
        abs=1e-4,
    )

    corrupted = deepcopy(evidence)
    corrupted["payload"]["quote_price"] += 1
    assert verify_market_scan_point_in_time_evidence(corrupted) is False

    persisted = _persisted_item(result)
    assert verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=persisted,
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=AS_OF.isoformat(),
    ) is True
    swapped_identity = persisted.model_copy(update={"symbol": "000001.SZ", "code": "000001", "market": "SZ"})
    assert verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=swapped_identity,
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=AS_OF.isoformat(),
    ) is False
    changed_details = deepcopy(persisted.score_details)
    changed_details["components"]["score_dimensions"]["scores"]["risk"] += 1
    changed_outer_score = persisted.model_copy(update={"score_details": changed_details})
    assert verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=changed_outer_score,
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=AS_OF.isoformat(),
    ) is False
    resealed = deepcopy(evidence)
    resealed["payload"]["derived_scores"]["risk"] += 1
    resealed["payload_digest"] = sha256_hex(canonical_json_text(resealed["payload"]))
    assert verify_market_scan_point_in_time_evidence(resealed) is True
    assert verify_market_scan_point_in_time_evidence_context(
        resealed,
        item=changed_outer_score,
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=AS_OF.isoformat(),
    ) is False


@pytest.mark.parametrize(
    ("item_updates", "expected_as_of"),
    (
        ({"quote_observed_at": "2026-07-17T08:31:00Z"}, AS_OF.isoformat()),
        ({"quote_observed_at": "2026-07-17T08:29:00+00:00"}, "2026-07-17T16:28:00+08:00"),
    ),
)
def test_point_in_time_evidence_rejects_observation_after_decision_absolute_time(
    item_updates: dict[str, object],
    expected_as_of: str,
) -> None:
    result = score_market_scan_item(
        _item(), _quote(), _rows(DATA_DATE, 80),
        as_of=AS_OF, completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE, min_history_rows=60,
        min_data_quality_score=0,
    )
    evidence = result.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    item = _persisted_item(result).model_copy(update=item_updates)

    assert verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=item,
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=expected_as_of,
    ) is False


def test_point_in_time_evidence_rejects_resealed_future_bar_as_of() -> None:
    result = score_market_scan_item(
        _item(), _quote(), _rows(DATA_DATE, 80),
        as_of=AS_OF, completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE, min_history_rows=60,
        min_data_quality_score=0,
    )
    evidence = deepcopy(
        result.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    )
    evidence["payload"]["bar_contract_61"][-1][9] = "2026-07-17T16:31:00+08:00"
    evidence["payload_digest"] = sha256_hex(canonical_json_text(evidence["payload"]))

    assert verify_market_scan_point_in_time_evidence(evidence) is True
    assert verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=_persisted_item(result),
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=AS_OF.isoformat(),
    ) is False


def test_legacy_v2_point_in_time_evidence_is_audit_only_and_action_ineligible() -> None:
    result = score_market_scan_item(
        _item(),
        _quote(),
        _rows(DATA_DATE, 80),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    evidence = deepcopy(
        result.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    )
    evidence["contract_version"] = MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION

    assert verify_market_scan_point_in_time_evidence(evidence) is True
    assert verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=_persisted_item(result),
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=AS_OF.isoformat(),
    ) is False


def test_legacy_v3_without_bar_as_of_is_audit_only_and_action_ineligible() -> None:
    result = score_market_scan_item(
        _item(),
        _quote(),
        _rows(DATA_DATE, 80),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    evidence = deepcopy(
        result.score_details["components"]["score_dimensions"]["point_in_time_evidence"]
    )
    evidence["contract_version"] = MARKET_SCAN_EVIDENCE_LEGACY_V3_CONTRACT_VERSION
    evidence["payload"]["bar_contract_61"] = [
        bar[:9] for bar in evidence["payload"]["bar_contract_61"]
    ]
    evidence["payload_digest"] = sha256_hex(canonical_json_text(evidence["payload"]))

    assert verify_market_scan_point_in_time_evidence(evidence) is True
    assert verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=_persisted_item(result),
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=AS_OF.isoformat(),
    ) is False


def test_exchange_session_gap_reduces_confidence_and_blocks_action_without_filling_ohlc() -> None:
    complete_rows = completed_market_scan_klines(_rows(DATA_DATE, 90), DATA_DATE)
    missing_date = complete_rows[-10].date
    gapped_rows = [row for row in complete_rows if row.date != missing_date]
    with pytest.raises(MarketScanSkipped, match="缺失 1 个预期会话.*不会补造K线"):
        score_market_scan_item(
            _item(),
            _quote(),
            gapped_rows,
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )

    complete = score_market_scan_item(
        _item(),
        _quote(),
        complete_rows,
        as_of=PREOPEN_AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        mode="preopen",
    )
    gapped = score_market_scan_item(
        _item(),
        _quote(),
        gapped_rows,
        as_of=PREOPEN_AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        mode="preopen",
    )
    complete_dimensions = complete.score_details["components"]["score_dimensions"]
    gapped_dimensions = gapped.score_details["components"]["score_dimensions"]
    evidence = gapped_dimensions["point_in_time_evidence"]
    coverage = evidence["payload"]["session_coverage"]

    assert coverage["missing_session_count"] == 1
    assert coverage["max_gap_sessions"] == 1
    assert coverage["recent_windows"]["20"]["missing_session_count"] == 1
    assert missing_date not in {bar[0] for bar in evidence["payload"]["bar_contract_61"]}
    assert gapped_dimensions["scores"]["confidence"] < complete_dimensions["scores"]["confidence"]
    assert evidence["action_eligible"] is False
    assert evidence["eligible_for_promotion_evidence"] is False
    assert verify_market_scan_point_in_time_evidence(evidence) is True
    assert verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=_persisted_item(gapped),
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=PREOPEN_AS_OF.isoformat(),
        expected_mode="preopen",
        require_action_eligible=False,
    ) is True
    assert verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=_persisted_item(gapped),
        expected_data_date=DATA_DATE.isoformat(),
        expected_quote_date=DATA_DATE.isoformat(),
        expected_as_of=PREOPEN_AS_OF.isoformat(),
        expected_mode="preopen",
    ) is False


def test_market_scan_score_is_neutral_to_short_cache_but_not_fallback() -> None:
    rows = _rows(DATA_DATE, 80)
    direct = score_market_scan_item(
        _item(),
        _quote(),
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    cached = score_market_scan_item(
        _item(),
        _quote().model_copy(update={"source": "腾讯行情·短时缓存", "from_cache": True}),
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    fallback = score_market_scan_item(
        _item(),
        _quote(fallback_used=True).model_copy(update={"source": "腾讯行情·兜底缓存", "from_cache": True}),
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )

    assert cached.data_quality_score == direct.data_quality_score
    assert cached.raw_score == direct.raw_score
    assert cached.score == direct.score
    assert fallback.data_quality_score < direct.data_quality_score  # type: ignore[operator]
    assert fallback.raw_score < direct.raw_score  # type: ignore[operator]


def test_market_scan_metadata_degradation_keeps_industry_and_list_date_reasons_distinct() -> None:
    result = score_market_scan_item(
        _item(industry=None, list_date=None),
        _quote(),
        _rows(DATA_DATE, 80),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )

    assert result.metadata_degraded is True
    assert {"行业未知", "上市日期未知"}.issubset(result.tags)
    assert result.degradation_reasons == ("industry_missing", "list_date_missing")


def test_market_scan_v5_spec_promotes_continuous_trend_into_material_base_score() -> None:
    spec = market_scan_score_spec(min_data_quality_score=50)

    assert spec["schema_version"] == 5
    assert spec["rule_version"] == "full-market-score-v5"
    assert spec["leader_profile"] == {
        "algorithm": "leader-score-additive-v1",
        "profile_id": "full-market-trend-only-v1",
        "base": 50,
        "trend_weight": 1.0,
        "rules": [],
    }
    assert spec["data_quality_policy"]["cached_quote"] == "neutral"
    assert spec["volume_ratio"] == {"recent_window": 5, "base_window": 20, "min_count": 6, "precision": 2}
    assert spec["final_score"]["quality_policy"] == "penalty-only"
    assert spec["final_score"]["quality_penalty_per_missing_point"] == 0.15
    assert spec["research_dimensions"]["ranking_effect"] == "none"
    assert spec["research_dimensions"]["actionable"] is False
    assert spec["research_dimensions"]["probability"] is False
    continuous = spec["ranking"]["continuous_trend"]
    assert continuous["algorithm"] == "bounded-medium-term-continuous-trend-v2"
    assert continuous["score_role"] == "material-base-score-component"
    assert continuous["adjustment_range"] == [-4.0, 4.0]
    assert spec["ranking"]["raw_score_formula"] == "base_score"
    assert spec["rounding"]["raw_score_decimals"] == 6
    assert spec["eligibility"]["single_price_session_excluded"] is True
    assert spec["eligibility"]["valid_quote_fields_required"] is True
    assert spec["eligibility"]["max_change_pct_gap"] == 0.3
    assert spec["eligibility"]["official_contiguous_session_coverage_required"] is True
    assert tuple(tuple(entry) for entry in spec["ranking"]["tie_break"]) == FULL_MARKET_SCORE_TIE_BREAK
    assert FULL_MARKET_SCORE_TIE_BREAK == (("raw_score", "desc"), ("symbol", "asc"))


def test_market_scan_v4_spec_builder_reproduces_frozen_run_82_hash() -> None:
    spec = market_scan_score_spec_v4(min_data_quality_score=50)

    assert spec["schema_version"] == 4
    assert spec["rule_version"] == "full-market-score-v4"
    assert spec["ranking"]["refinement"]["algorithm"] == "bounded-medium-term-refinement-v1"
    assert spec["ranking"]["refinement"]["max_rank_discount"] < spec["ranking"]["base_score_minimum_step"]
    assert "official_contiguous_session_coverage_required" not in spec["eligibility"]
    assert stable_score_spec_hash(spec) == "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"


def test_market_scan_replay_validates_v5_and_compatibly_replays_v2_v3_v4_specs() -> None:
    result = score_market_scan_item(
        _item(),
        _quote(),
        _rows(DATA_DATE, 80),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    replay = replay_score_details(result.score_details)

    assert replay.score_spec_schema_version == 5
    assert replay.raw_score == result.raw_score
    assert replay.tie_break == (("raw_score", "desc"), ("symbol", "asc"))

    corrupted_raw = deepcopy(result.score_details)
    corrupted_raw["ranking"]["tie_break_values"]["raw_score"] += 0.1
    with pytest.raises(MarketScanReplayError, match="raw_score"):
        replay_score_details(corrupted_raw)

    v4 = _as_v4_score_details(result.score_details)
    assert replay_score_details(v4).score_spec_schema_version == 4

    v3 = _as_v3_score_details(v4)
    assert replay_score_details(v3).score_spec_schema_version == 3

    legacy = deepcopy(v3)
    legacy_spec = legacy["score_spec"]
    legacy_spec["schema_version"] = 2
    legacy_spec["algorithms"]["volume_ratio"] = "recent-volume-ratio-v1"
    legacy_spec["algorithms"]["data_quality"] = "data-quality-v1"
    legacy_spec["algorithms"]["final_score"] = "weighted-leader-quality-v1"
    legacy_spec["leader_profile"].pop("profile_id")
    legacy_spec.pop("volume_ratio")
    legacy_spec.pop("data_quality_policy")
    legacy_spec["rounding"].pop("raw_score_decimals")
    legacy_spec["ranking"]["tie_break"] = [entry for entry in legacy_spec["ranking"]["tie_break"] if entry[0] != "raw_score"]
    legacy["ranking"]["tie_break"] = [entry for entry in legacy["ranking"]["tie_break"] if entry[0] != "raw_score"]
    legacy["ranking"]["tie_break_values"].pop("raw_score")
    legacy["score_spec_hash"] = stable_score_spec_hash(legacy_spec)

    assert replay_score_details(legacy).score_spec_schema_version == 2


def test_current_v6_registry_rejects_replayable_v4_while_historical_v6_reads_it() -> None:
    run_rule_version = f"full-market-scan-v6:{'a' * 64}"
    current = score_market_scan_item(
        _item(),
        _quote(),
        _rows(DATA_DATE, 80),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=50,
        rule_version=run_rule_version,
        quote_observed_at="2026-07-17T07:00:00Z",
    )
    v4_details = _as_v4_score_details(current.score_details)
    final_score = v4_details["components"]["final_score"]
    v4_result = replace(
        current,
        score=final_score["score"],
        raw_score=final_score["raw"],
        score_details=v4_details,
    )
    persisted_v4 = _persisted_item(v4_result)
    run = MarketScanRun(
        id=1,
        status="running",
        trigger="manual",
        mode="official",
        rule_version=run_rule_version,
        as_of="2026-07-17 16:30:00",
        data_date=DATA_DATE.isoformat(),
        quote_date=DATA_DATE.isoformat(),
        scope=FULL_MARKET_SCOPE,
        total_count=1,
        excluded_count=0,
        processed_count=1,
        success_count=1,
        missing_count=0,
        skipped_count=0,
        retry_count=0,
        progress_pct=100,
        coverage_pct=100,
        created_at="2026-07-17 16:29:00",
        updated_at="2026-07-17 16:31:00",
        quote_capture_started_at="2026-07-17T08:29:00Z",
        quote_capture_finished_at="2026-07-17T08:31:00Z",
        quote_capture_duration_ms=120_000,
        quote_capture_count=1,
    )

    # A historical v6 row without the later run-contract registry remains strictly replayable.
    verify_persisted_market_scan_result(persisted_v4, run)
    assert replay_score_details(v4_details).score_spec_hash == (
        "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
    )

    with pytest.raises(MarketScanReplayError, match="封存的 v5"):
        verify_persisted_market_scan_result(
            persisted_v4,
            run,
            expected_score_rule_version="full-market-score-v5",
            expected_score_spec_hash=current.score_details["score_spec_hash"],
        )


def test_market_scan_replay_uses_continuous_medium_term_structure_before_symbol() -> None:
    slow = score_market_scan_item(
        _item(),
        _quote(),
        _trend_rows(DATA_DATE, 80, first_close=6.1, last_close=10.5),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    fast = score_market_scan_item(
        _item(),
        _quote(),
        _trend_rows(DATA_DATE, 80, first_close=6.0, last_close=10.5),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    higher = _score_details_with_symbol(fast.score_details, "600002.SH")
    lower = _score_details_with_symbol(slow.score_details, "600001.SH")

    assert fast.score == slow.score
    assert fast.trend_score == slow.trend_score
    assert higher["components"]["final_score"]["raw"] > lower["components"]["final_score"]["raw"]
    assert rank_score_details([("600001.SH", lower), ("600002.SH", higher)]) == {
        "600002.SH": 1,
        "600001.SH": 2,
    }


def test_completed_market_scan_klines_excludes_future_invalid_and_identical_duplicates() -> None:
    earlier = make_kline(date="2026-07-16", close=10)
    replacement = earlier.model_copy(update={"source": "另一个来源"})
    current = make_kline(date="2026-07-17", close=12)
    future = make_kline(date="2026-07-18", close=13)
    invalid_date = make_kline(date="2026/07/17", close=14)

    rows = completed_market_scan_klines(
        [earlier, future, invalid_date, replacement, current],
        DATA_DATE,
    )

    assert [(row.date, row.close) for row in rows] == [
        ("2026-07-16", 10),
        ("2026-07-17", 12),
    ]


def test_completed_market_scan_klines_rejects_conflicting_same_day_bars_regardless_of_order() -> None:
    first = make_kline(date="2026-07-16", close=10)
    conflicting = make_kline(date="2026-07-16", close=11)

    for rows in ([first, conflicting], [conflicting, first]):
        with pytest.raises(MarketScanDataMissing, match="存在冲突日K"):
            completed_market_scan_klines(rows, DATA_DATE)


def test_market_scan_rejects_provider_bar_after_expected_trading_date() -> None:
    rows = [*_rows(DATA_DATE, 79), make_kline(date="2026-07-20", close=13)]

    with pytest.raises(MarketScanDataMissing, match="晚于应有交易日"):
        score_market_scan_item(
            _item(),
            _quote(),
            rows,
            as_of=datetime(2026, 7, 20, 16, 30),
            completed_cutoff=date(2026, 7, 20),
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


@pytest.mark.parametrize(
    ("item_factory", "quote_factory", "rows_factory", "expected_exception", "message"),
    [
        (
            lambda: _item(),
            lambda: _quote(code="000001", market="SZ"),
            lambda: _rows(DATA_DATE, 80),
            MarketScanDataMissing,
            "代码不匹配",
        ),
        (lambda: _item(), lambda: _quote(), lambda: _rows(DATA_DATE, 59), MarketScanDataMissing, "日K不足"),
        (
            lambda: _item(),
            lambda: _quote().model_copy(update={"volume": 0.0, "amount": 0.0}),
            lambda: _rows(date(2026, 7, 16), 80),
            MarketScanDataMissing,
            "可能停牌",
        ),
        (
            lambda: _item(),
            lambda: _quote(timestamp="2026-07-16 15:00:00"),
            lambda: _rows(DATA_DATE, 80),
            MarketScanDataMissing,
            "与完整交易日",
        ),
        (
            lambda: _item(),
            lambda: _quote(),
            lambda: [row.model_copy(update={"adjustment_mode": "none"}) for row in _rows(DATA_DATE, 80)],
            MarketScanDataMissing,
            "不是一致的前复权",
        ),
    ],
)
def test_market_scan_score_rejects_non_comparable_data(
    item_factory,
    quote_factory,
    rows_factory,
    expected_exception: type[Exception],
    message: str,
) -> None:
    with pytest.raises(expected_exception, match=message):
        score_market_scan_item(
            item_factory(),
            quote_factory(),
            rows_factory(),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_current_trading_quote_does_not_turn_stale_kline_into_suspension() -> None:
    with pytest.raises(MarketScanDataMissing, match="当日报价存在有效成交"):
        score_market_scan_item(
            _item(),
            _quote(),
            _rows(date(2026, 7, 16), 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_current_zero_liquidity_quote_and_bar_are_classified_as_possible_suspension() -> None:
    rows = _rows(DATA_DATE, 80)
    rows[-1] = rows[-1].model_copy(update={"volume": 0.0})

    with pytest.raises(MarketScanDataMissing, match="可能停牌"):
        score_market_scan_item(
            _item(),
            _quote().model_copy(update={"volume": 0.0, "amount": 0.0}),
            rows,
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_rejects_quote_after_the_batch_as_of_boundary() -> None:
    with pytest.raises(MarketScanDataMissing, match="晚于批次截止时点"):
        score_market_scan_item(
            _item(),
            _quote(timestamp="2026-07-17 17:00:00"),
            _rows(DATA_DATE, 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


@pytest.mark.parametrize(
    ("row_update", "message"),
    [
        ({"as_of": "2026-07-18"}, "日K快照时点.*晚于批次截止时点"),
        ({"data_version": "unknown"}, "缺少可审计数据版本"),
        ({"contract_version": "daily-kline.v999"}, "日K合同版本未知"),
    ],
)
def test_market_scan_rejects_untrusted_or_future_kline_snapshot_contract(
    row_update: dict[str, object],
    message: str,
) -> None:
    rows = [row.model_copy(update=row_update) for row in _rows(DATA_DATE, 80)]

    with pytest.raises(MarketScanDataMissing, match=message):
        score_market_scan_item(
            _item(),
            _quote(),
            rows,
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


@pytest.mark.parametrize(
    ("quote_update", "message"),
    [
        ({"volume": 0}, "成交量或成交额"),
        ({"amount": 0}, "成交量或成交额"),
        ({"turnover_rate": None}, "缺少换手率"),
    ],
)
def test_market_scan_score_rejects_missing_rankable_quote_liquidity(
    quote_update: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(MarketScanDataMissing, match=message):
        score_market_scan_item(
            _item(),
            _quote().model_copy(update=quote_update),
            _rows(DATA_DATE, 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_marks_single_price_session_missing_with_uncertain_execution() -> None:
    locked = _quote().model_copy(update={"open": 10.5, "high": 10.5, "low": 10.5, "price": 10.5})

    with pytest.raises(MarketScanDataMissing, match="全天单一价格"):
        score_market_scan_item(
            _item(),
            locked,
            _rows(DATA_DATE, 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_score_rejects_missing_recent_kline_volume() -> None:
    rows = _rows(DATA_DATE, 80)
    rows[-5] = rows[-5].model_copy(update={"volume": 0})

    with pytest.raises(MarketScanDataMissing, match="连续有效成交量"):
        score_market_scan_item(
            _item(),
            _quote(),
            rows,
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


@pytest.mark.parametrize(
    ("quote_update", "message"),
    [
        ({"open": 10.9}, "OHLC"),
        ({"turnover_rate": -0.1}, "OHLC"),
        ({"change_pct": -5.0}, "涨跌幅"),
    ],
)
def test_market_scan_score_rejects_malformed_or_internally_inconsistent_quotes(
    quote_update: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(MarketScanDataMissing, match=message):
        score_market_scan_item(
            _item(),
            _quote().model_copy(update=quote_update),
            _rows(DATA_DATE, 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_score_rejects_quote_and_same_day_kline_close_mismatch() -> None:
    rows = _rows(DATA_DATE, 80)
    rows[-1] = rows[-1].model_copy(update={"close": 10.0, "high": 10.2})

    with pytest.raises(MarketScanDataMissing, match="收盘价偏差"):
        score_market_scan_item(
            _item(),
            _quote(),
            rows,
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_universe_deduplicates_and_marks_st_new_and_delisted() -> None:
    primary = make_stock_info("600519", "SH").model_copy(
        update={"name": "*ST茅台", "industry": "白酒", "list_date": "2026-07-01"}
    )
    rows = [
        primary,
        primary.model_copy(),
        make_stock_info("000001", "SZ").model_copy(update={"name": "退市样本"}),
        make_stock_info("920066", "BJ").model_copy(update={"name": "北交样本", "list_date": "20240703"}),
        make_stock_info("600000", "SH").model_copy(update={"symbol": "000001.SZ", "name": "字段冲突"}),
        make_stock_info("600001", "SH").model_copy(
            update={"symbol": "600001.SZ", "market": "SZ", "name": "交易所错配"}
        ),
        make_stock_info("900901", "SH").model_copy(update={"name": "沪市B股"}),
        make_stock_info("200002", "SZ").model_copy(update={"name": "深市B股"}),
        StockInfo(
            symbol="123456.HK",
            code="123456",
            market="HK",
            name="非A股",
            source="test",
            updated_at="2026-07-17 16:00:00",
        ),
    ]

    universe = build_market_scan_universe(rows, data_date=DATA_DATE, new_stock_days=120)

    assert [seed.symbol for seed in universe.seeds] == ["920066.BJ", "600519.SH"]
    assert universe.excluded_count == 7
    by_symbol = {seed.symbol: seed for seed in universe.seeds}
    assert by_symbol["600519.SH"].is_st is True
    assert by_symbol["600519.SH"].is_new is True
    assert by_symbol["600519.SH"].list_date == "2026-07-01"
    assert by_symbol["600519.SH"].metadata_source == rows[0].source
    assert by_symbol["920066.BJ"].is_new is False
    assert by_symbol["920066.BJ"].list_date == "2024-07-03"


def _score_details_with_symbol(details: dict[str, object], symbol: str) -> dict[str, object]:
    updated = deepcopy(details)
    updated["ranking"]["tie_break_values"]["symbol"] = symbol
    return updated


def _as_v4_score_details(details: dict[str, object]) -> dict[str, object]:
    updated = deepcopy(details)
    inputs = updated["inputs"]
    components = updated["components"]
    continuous = components.pop("continuous_trend")
    for key in tuple(inputs):
        if key.startswith("continuous_trend_"):
            inputs[f"rank_{key.removeprefix('continuous_trend_')}"] = inputs.pop(key)
    components["rank_refinement"] = continuous
    quality_penalty = components["final_score"]["quality_penalty"]
    leader_score = components["leader_score"]["score"]
    base_score = round(max(0.0, min(100.0, leader_score - quality_penalty)), 4)
    rank_discount = round((1 - continuous["score"]) * 0.0499, 8)
    raw_score = round(max(0.0, base_score - rank_discount), 6)
    rounded_score = round(base_score)
    components["final_score"] = {
        "quality_penalty": quality_penalty,
        "base": base_score,
        "rank_discount": rank_discount,
        "raw": raw_score,
        "rounded": rounded_score,
        "score": max(0, min(100, rounded_score)),
    }
    minimum = updated["score_spec"]["eligibility"]["min_data_quality_score"]
    spec = market_scan_score_spec_v4(min_data_quality_score=minimum)
    updated["score_spec"] = spec
    updated["score_spec_hash"] = stable_score_spec_hash(spec)
    updated["ranking"]["tie_break_values"]["raw_score"] = raw_score
    return updated


def _as_v3_score_details(details: dict[str, object]) -> dict[str, object]:
    updated = deepcopy(details)
    spec = updated["score_spec"]
    inputs = updated["inputs"]
    components = updated["components"]
    ranking = updated["ranking"]
    leader_score = components["leader_score"]["score"]
    quality_score = inputs["data_quality_score"]
    raw_score = round(leader_score * 0.85 + quality_score * 0.15, 4)
    rounded_score = round(raw_score)
    v3_tie_break = [
        ["score", "desc"],
        ["raw_score", "desc"],
        ["trend_score", "desc"],
        ["change_pct", "desc"],
        ["amount", "desc"],
        ["symbol", "asc"],
    ]

    spec["schema_version"] = 3
    spec["rule_version"] = "full-market-score-v3"
    spec["algorithms"].pop("rank_refinement")
    spec["algorithms"]["trend_score"] = "trend-score-v1"
    spec["algorithms"]["final_score"] = "weighted-trend-quality-v2"
    spec["eligibility"].pop("quote_kline_close_consistency")
    spec["eligibility"].pop("quote_timestamp_not_after_as_of")
    spec["eligibility"].pop("single_price_session_excluded")
    spec["eligibility"].pop("valid_quote_fields_required")
    spec["eligibility"].pop("max_change_pct_gap")
    spec["final_score"] = {
        "formula": "leader_score * leader_weight + data_quality_score * quality_weight",
        "weights": {"leader_score": 0.85, "data_quality_score": 0.15},
        "clamp": [0, 100],
    }
    spec["rounding"]["component_stage"] = "after-trend-weight-and-final-weighted-sum"
    spec["rounding"]["raw_score_decimals"] = 4
    spec["ranking"] = {"tie_break": v3_tie_break}
    for key in tuple(inputs):
        if key.startswith("rank_"):
            inputs.pop(key)
    components.pop("rank_refinement")
    components["final_score"] = {
        "weighted_terms": {
            "leader_score": leader_score * 0.85,
            "data_quality_score": quality_score * 0.15,
        },
        "raw": raw_score,
        "rounded": rounded_score,
        "score": rounded_score,
    }
    ranking["tie_break"] = v3_tie_break
    ranking["tie_break_values"] = {
        "score": rounded_score,
        "raw_score": raw_score,
        "trend_score": inputs["trend_score"],
        "change_pct": inputs["change_pct"],
        "amount": inputs["amount"],
        "symbol": ranking["tie_break_values"]["symbol"],
    }
    updated["score_spec_hash"] = stable_score_spec_hash(spec)
    return updated


def _item(
    *,
    is_st: bool = False,
    is_new: bool = False,
    industry: str | None = "白酒",
    list_date: str | None = "2001-08-27",
) -> MarketScanResultItem:
    return MarketScanResultItem(
        run_id=1,
        symbol="600519.SH",
        code="600519",
        market="SH",
        name="贵州茅台",
        industry=industry,
        list_date=list_date,
        is_st=is_st,
        is_new=is_new,
        status="pending",
        updated_at="2026-07-17 16:30:00",
    )


def _persisted_item(result: MarketScanResultWrite) -> MarketScanResultItem:
    seed = _item()
    return seed.model_copy(
        update={
            "status": result.status,
            "rank": 1,
            "score": result.score,
            "raw_score": result.raw_score,
            "trend_score": result.trend_score,
            "leader_score": result.leader_score,
            "data_quality_score": result.data_quality_score,
            "price": result.price,
            "change_pct": result.change_pct,
            "turnover_rate": result.turnover_rate,
            "volume_ratio": result.volume_ratio,
            "amount": result.amount,
            "score_details": result.score_details,
            "data_date": result.data_date,
            "quote_timestamp": result.quote_timestamp,
            "quote_observed_at": "2026-07-17T07:00:00Z",
            "quote_source": result.quote_source,
            "kline_source": result.kline_source,
            "adjustment_mode": result.adjustment_mode,
            "quote_fallback_used": result.quote_fallback_used,
            "kline_fallback_used": result.kline_fallback_used,
            "metadata_degraded": result.metadata_degraded,
            "degradation_reasons": list(result.degradation_reasons),
        }
    )


def _quote(
    *,
    code: str = "600519",
    market: str = "SH",
    timestamp: str = "2026-07-17 15:00:00",
    fallback_used: bool = False,
):
    return make_quote(
        price=10.5,
        prev_close=10.0,
        high=10.8,
        low=9.9,
        change_pct=5.0,
        turnover_rate=4.5,
        timestamp=timestamp,
    ).model_copy(
        update={
            "code": code,
            "market": market,
            "name": "贵州茅台",
            "open": 10.1,
            "amount": 900_000_000,
            "change": 0.5,
            "fallback_used": fallback_used,
        }
    )


def _rows(latest: date, count: int) -> list[Kline]:
    return _trend_rows(latest, count, first_close=10.5 - (count - 1) * 0.04, last_close=10.5)


def _trend_rows(latest: date, count: int, *, first_close: float, last_close: float) -> list[Kline]:
    days: list[date] = []
    cursor = latest
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    step = (last_close - first_close) / max(1, count - 1)
    return [
        make_kline(
            date=day.isoformat(),
            close=first_close + index * step,
            volume=1_000_000 + index * 20_000,
            source="test-qfq",
            as_of=latest.isoformat(),
            data_version=f"test|qfq|{latest.isoformat()}",
        )
        for index, day in enumerate(days)
    ]
