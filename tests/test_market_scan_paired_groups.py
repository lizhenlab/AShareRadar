from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from app.services import market_scan_evaluation as evaluation


CONFIG = evaluation.EvaluationConfig(horizons=(5,), minimum_sample_size=1, minimum_session_count=1)


def _rows(run_id, *, available, count=100, value=0.1):
    return tuple(SimpleNamespace(
        run_id=run_id, rank=rank, quote_date=f"2026-08-{run_id + 10:02d}",
        returns={5: value} if available(rank) else {},
    ) for rank in range(1, count + 1))


def _monotonicity(rows):
    return evaluation._monotonicity_record("official", "all", "v1", rows, 5, CONFIG)


def _deciles(rows):
    return evaluation._decile_record(
        "official", "all", "v1", rows, Counter(item.run_id for item in rows), 5, CONFIG,
    )


@pytest.mark.parametrize("build", [_monotonicity, _deciles])
def test_disjoint_outcome_dates_cannot_create_monotonicity(build):
    # Both halves separately look strongly ordered, but there is no date on
    # which all frozen groups can be compared.
    rows = _rows(1, available=lambda rank: rank <= 50, value=0.2)
    rows += _rows(2, available=lambda rank: rank > 50, value=-0.2)
    report = build(rows)
    assert report["status"] == "insufficient_data"
    assert report["monotonic"] is None
    assert report["excluded_session_count"] == 2
    assert all(band["average_return"] is None for band in report["bands"])


@pytest.mark.parametrize("build", [_monotonicity, _deciles])
def test_only_common_dates_enter_every_band_average(build):
    rows = _rows(1, available=lambda rank: rank <= 50, value=1)
    rows += _rows(2, available=lambda rank: rank > 50, value=-1)
    rows += _rows(3, available=lambda rank: True, value=-0.01)
    report = build(rows)
    assert report["status"] == "ok"
    assert report["excluded_session_count"] == 2
    assert all(band["average_return"] == pytest.approx(-0.01) for band in report["bands"])
    assert all(band["independent_session_count"] == 1 for band in report["bands"])


def test_coverage_uses_original_membership_not_only_available_labels():
    rows = _rows(1, available=lambda rank: rank not in (1, 2), count=100)
    report = _monotonicity(rows)
    assert report["status"] == "insufficient_data"  # 18/20 < 95% in high group.
    assert report["excluded_session_count"] == 1
    rows = _rows(1, available=lambda rank: rank != 1, count=100)
    assert _monotonicity(rows)["status"] == "ok"  # Exact 19/20 boundary.


def test_small_universe_does_not_invent_missing_rank_groups():
    report = _monotonicity(_rows(1, available=lambda rank: True, count=30))
    assert report["status"] == "insufficient_data"
    assert report["excluded_session_count"] == 1


def test_decile_membership_is_not_recomputed_after_missing_labels():
    rows = _rows(1, available=lambda rank: rank != 1, count=10)
    report = _deciles(rows)
    assert report["status"] == "insufficient_data"
    assert report["excluded_session_count"] == 1
