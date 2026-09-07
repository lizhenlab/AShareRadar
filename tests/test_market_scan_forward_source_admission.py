from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any, cast

import pytest

from app.services import market_scan_future_range as future_range
from app.services.market_scan_evaluation_price_basis import valid_forward_price_bar
from app.services import market_scan_probability_outcomes as outcomes
from tests.test_market_scan_evaluation_price_basis import _inputs, _observe
from tests.test_market_scan_future_range import FULL_MARKET_SCOPE, TARGET_DATES, _initialize, _seed_run
from tests.test_market_scan_probability_labels import _labels, _rows
from tests.test_market_scan_probability_outcomes import GENERATED_AT, _complete_h1_rows, _source


DEMO_SOURCES = ("demo", "cached-DEMO-kline", "演示行情")


@pytest.mark.parametrize("source", DEMO_SOURCES)
@pytest.mark.parametrize("bad_index", [0, 1, 2])
def test_evaluation_rejects_demo_overlap_entry_or_exit(source: str, bad_index: int) -> None:
    run, result, bars = _inputs()
    bars[bad_index]["source"] = source
    observation = _observe(run, result, bars)

    assert observation.rank == 1
    assert observation.execution[1].status == "data_unavailable"
    assert observation.execution[1].net_return is None
    assert observation.probability_labels[1].status == "data_unavailable"
    assert observation.probability_labels[1].label is None
    if bad_index != 2:
        assert observation.returns == {}
    else:
        # Gross H1 ends at D+1; only executable H1 requires D+2.
        assert observation.returns[1] == pytest.approx(0.02)


@pytest.mark.parametrize("source", DEMO_SOURCES)
@pytest.mark.parametrize("bad_index", [0, 1, 2])
def test_probability_labels_reject_demo_before_numerical_modelling(source: str, bad_index: int) -> None:
    rows = _rows(
        ("2026-01-05", 100, 100, 101, 99, 1_000),
        ("2026-01-06", 100, 101, 102, 99, 1_000),
        ("2026-01-07", 101, 102, 103, 100, 1_000),
    )
    rows[bad_index] = rows[bad_index].model_copy(update={"source": source})
    with pytest.raises(ValueError, match="demo probability label bar"):
        _labels(rows, horizons=(1,))


@pytest.mark.parametrize("demo_first", [True, False])
def test_identical_prices_do_not_hide_demo_probability_input(demo_first: bool) -> None:
    rows = _rows(
        ("2026-01-05", 100, 100, 101, 99, 1_000),
        ("2026-01-06", 100, 101, 102, 99, 1_000),
        ("2026-01-07", 101, 102, 103, 100, 1_000),
    )
    demo = rows[1].model_copy(update={"source": "demo"})
    rows.insert(1 if demo_first else 2, demo)
    with pytest.raises(ValueError, match="demo probability label bar"):
        _labels(rows, horizons=(1,))


@pytest.mark.parametrize("source", DEMO_SOURCES)
@pytest.mark.parametrize("allow_zero_volume", [True, False])
def test_future_range_rejects_demo_before_relabelling_source(source: str, allow_zero_volume: bool) -> None:
    _, _, bars = _inputs()
    row = bars[1]
    row["source"] = source
    parsed, error = future_range._verified_target_bar(
        cast(sqlite3.Row, row), expected_date=row["date"], allow_zero_volume=allow_zero_volume,
    )
    assert parsed is None
    assert error == "target_bar_demo_source"


@pytest.mark.parametrize("source", DEMO_SOURCES)
def test_probability_artifact_builder_rejects_demo_evidence(
    source: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_artifact = _source()
    monkeypatch.setattr(outcomes, "_source_artifact", lambda _value: source_artifact)
    rows = _complete_h1_rows()
    rows[-1] = rows[-1].model_copy(update={"source": source})
    with pytest.raises(outcomes.ProbabilityOutcomeError, match="演示"):
        outcomes.build_probability_outcome_artifact(
            source_artifact, {"600001.SH": rows}, generated_at=GENERATED_AT, as_of_date="2026-08-13",
        )


def test_future_range_keeps_demo_targets_unavailable_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "demo-targets.sqlite3"
    _initialize(path)
    run_id = _seed_run(path, scope=FULL_MARKET_SCOPE, with_targets=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE kline_daily SET source = 'demo' WHERE date >= ?", (TARGET_DATES[0].isoformat(),))
    monkeypatch.setattr(future_range, "next_trade_dates", lambda _value, _count: TARGET_DATES)
    before = path.read_bytes()
    report = future_range.evaluate_market_scan_future_range(
        path, run_ids=[run_id], generated_at="2026-01-08T08:00:00+00:00",
    )
    assert path.read_bytes() == before
    payload = cast(list[dict[str, Any]], report["reports"])[0]
    assert payload["records"]
    for record in payload["records"]:
        for offset in record["offsets"]:
            assert offset["fixed_session_status"] == "unavailable"
            assert offset["reason"] == "target_bar_demo_source"
            assert offset["execution"]["status"] == "data_unavailable"
            assert offset["execution"]["net_return"] is None


def test_real_fallback_remains_usable_without_demoting_every_fallback() -> None:
    run, result, bars = _inputs()
    for row in bars:
        row.update(source="tencent", fallback_used=True)
    observation = _observe(run, result, bars)
    assert observation.returns[1] == pytest.approx(0.02)
    assert observation.execution[1].status == "modelled"
    assert observation.probability_labels[1].status == "modelled"


@pytest.mark.parametrize("source", [b"demo", b"tencent", b"", 0])
def test_invalid_sqlite_source_is_unavailable_without_aborting_the_report(source: object) -> None:
    _, _, bars = _inputs()
    fields = dict(bars[1], source=source)
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT " + ", ".join(f"? AS {name}" for name in fields)
        row = conn.execute(query, tuple(fields.values())).fetchone()
    assert not valid_forward_price_bar(row)
    parsed, error = future_range._verified_target_bar(row, expected_date=fields["date"])
    assert parsed is None
    assert error == "target_bar_source_invalid"
