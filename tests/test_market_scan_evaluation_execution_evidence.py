"""Offline evaluation must preserve daily execution evidence without migrating input."""

from dataclasses import asdict
from pathlib import Path
import sqlite3

import pytest

from app.db.schema_definitions import KLINE_DAILY_COLUMN_DEFINITIONS
from app.services import market_scan_evaluation as evaluation
from app.services.market_scan_probability_labels import ProbabilityLabelConfig, build_probability_label_outcomes
from app.services.research_factor_execution_contract import factor_calibration_evidence_issue
from tests.test_market_scan_probability_outcomes import _complete_h1_rows


_FIELDS = {"session_status", "open_execution_status", "corporate_action_status", "adjustment_factor",
           "point_in_time", "execution_metadata_version"}
_OLD_SCHEMA = """
CREATE TABLE kline_daily (
    symbol TEXT, adjustment_mode TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL, volume REAL,
    as_of TEXT, data_version TEXT, contract_version TEXT, fallback_used INTEGER, source TEXT, fetched_at TEXT
)
"""


def _rows():
    return [row.model_copy(update={
        "as_of": f"{row.date} 15:15:00", "session_status": "trading", "open_execution_status": "tradable",
        "corporate_action_status": "none", "point_in_time": True,
        "execution_metadata_version": "factor-execution-evidence.v1", "adjustment_factor": 1.0,
    }) for row in _complete_h1_rows()]


def _database(path: Path, rows, *, legacy=False):
    with sqlite3.connect(path) as connection:
        connection.execute(_OLD_SCHEMA if legacy else f"CREATE TABLE kline_daily ({KLINE_DAILY_COLUMN_DEFINITIONS})")
        connection.execute("CREATE TABLE market_scan_result (run_id INTEGER, symbol TEXT, status TEXT, adjustment_mode TEXT)")
        connection.execute("INSERT INTO market_scan_result VALUES (71, '600001.SH', 'success', 'qfq')")
        columns = [item[1] for item in connection.execute("PRAGMA table_info(kline_daily)")]
        for row in rows:
            values = {**row.model_dump(), "symbol": "600001.SH", "fallback_used": 0,
                      "fetched_at": "2026-08-14T00:00:00Z"}
            connection.execute(
                f"INSERT INTO kline_daily ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )


def _read(path: Path, kind: str):
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if kind == "forward":
            return evaluation._forward_bars(connection, 71, "2026-08-11")["600001.SH"]
        return evaluation._shadow_history_bars(connection, 71, "2026-08-13")["600001.SH"]


def _label(rows):
    return build_probability_label_outcomes(
        symbol="600001.SH", market="SH", list_date="2020-01-02", is_st=False,
        quote_date="2026-08-11", amount=200_000_000, rows=rows,
        eligible_dates=("2026-08-12", "2026-08-13"), config=ProbabilityLabelConfig(horizons=(1,)),
    )[1]


@pytest.mark.parametrize("position,update", [
    (1, {"session_status": "suspended", "open_execution_status": "unavailable", "volume": 0}),
    (2, {"session_status": "suspended", "open_execution_status": "unavailable", "volume": 0}),
    (1, {"open_execution_status": "locked_limit_up"}),
    (1, {"open_execution_status": "unavailable"}),
])
def test_forward_sql_and_evaluation_adapter_keep_restricted_labels(tmp_path, position, update) -> None:
    rows = _rows()
    rows[position] = rows[position].model_copy(update=update)
    path = tmp_path / "evaluation.sqlite3"
    _database(path, rows)
    before = path.read_bytes()
    raw = _read(path, "forward")
    observed = evaluation._probability_label_outcomes(
        result={"list_date": "2020-01-02"}, symbol="600001.SH", market="SH", is_st=False,
        quote_date="2026-08-11", amount=200_000_000, bars=raw,
        eligible_dates=("2026-08-12", "2026-08-13"), config=evaluation.EvaluationConfig(horizons=(1,)),
    )[1]
    assert asdict(observed) == asdict(_label(rows))
    assert observed.status == "unfilled"
    assert path.read_bytes() == before


def test_shadow_history_keeps_all_six_fields_and_strict_pit_eligibility(tmp_path) -> None:
    rows = _rows()
    rows[-1] = rows[-1].model_copy(update={"corporate_action_status": "effective_event", "adjustment_factor": .5})
    path = tmp_path / "shadow.sqlite3"
    _database(path, rows)
    before = path.read_bytes()
    observed = _read(path, "shadow")
    assert [row.model_dump(include=_FIELDS) for row in observed] == [row.model_dump(include=_FIELDS) for row in rows]
    assert factor_calibration_evidence_issue(list(observed)) == factor_calibration_evidence_issue(rows) is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["forward", "shadow"])
def test_readonly_old_schema_retains_unknown_metadata_without_migration(tmp_path, kind) -> None:
    path = tmp_path / "legacy.sqlite3"
    _database(path, _rows(), legacy=True)
    before = path.read_bytes()
    rows = _read(path, kind)
    if kind == "forward":
        rows = [evaluation._to_kline(row) for row in rows]
    for row in rows:
        assert row.session_status == row.open_execution_status == row.corporate_action_status == "unknown"
        assert row.adjustment_factor is None and row.execution_metadata_version is None and row.point_in_time is False
    assert factor_calibration_evidence_issue(list(rows)) is not None
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["forward", "shadow"])
@pytest.mark.parametrize("field,declaration,value", [
    ("point_in_time", "TEXT", "1"), ("adjustment_factor", "TEXT", "0.5"),
    ("execution_metadata_version", "BLOB", b"factor-execution-evidence.v1"),
    ("session_status", "TEXT", "invalid-state"),
])
def test_raw_invalid_execution_metadata_is_rejected_without_coercion_or_writes(tmp_path, kind, field, declaration, value) -> None:
    path = tmp_path / "invalid.sqlite3"
    _database(path, _rows(), legacy=True)
    with sqlite3.connect(path) as connection:
        connection.execute(f"ALTER TABLE kline_daily ADD COLUMN {field} {declaration}")
        connection.execute(f"UPDATE kline_daily SET {field}=?", (value,))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        rows = _read(path, kind)
        if kind == "forward":
            [evaluation._to_kline(row) for row in rows]
    assert path.read_bytes() == before


def test_evaluation_general_field_conversion_keeps_existing_legacy_semantics() -> None:
    raw = {**_complete_h1_rows()[0].model_dump(), "date": 20260811, "open": "10.0",
           "data_version": None, "contract_version": None, "source": None, "fallback_used": 0}
    for field in _FIELDS:
        raw.pop(field, None)
    result = evaluation._to_kline(raw)
    assert result.date == "20260811" and result.open == 10.0
    assert result.data_version == "unknown" and result.contract_version == "daily-kline.v1"
    assert result.source is None and result.from_cache is False and result.fallback_used is False
