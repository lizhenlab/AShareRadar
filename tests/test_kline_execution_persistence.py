"""Daily cache and offline readers must preserve execution evidence, not invent it."""

from dataclasses import asdict
from pathlib import Path
import sqlite3

import pytest

from app.config import Settings
from app.db import schema_migrations as migrations
from app.db.market_mappers import row_to_kline
from app.repositories.market_klines import _daily_content_revision
from app.services import trading_calendar as calendar
from app.services.cache import SQLiteCache
from app.services.market_scan_probability_labels import ProbabilityLabelConfig, build_probability_label_outcomes
from app.services.research_factor_execution_contract import factor_calibration_evidence_issue
from tests.test_kline_contract import LEGACY_KLINE_TABLE_SQL
from tests.test_market_scan_probability_outcomes import _complete_h1_rows
from tools.maintain_market_scan_probability import _ReadOnlyKlineCache


_FIELDS = {"session_status", "open_execution_status", "corporate_action_status", "adjustment_factor",
           "point_in_time", "execution_metadata_version"}
_MIGRATION = "20260908_kline_daily_execution_evidence_v1"
_OLD_COLUMNS = ("symbol", "adjustment_mode", "date", "open", "close", "high", "low", "volume", "as_of",
                "data_version", "contract_version", "fallback_used", "source", "fetched_at")
_OLD_SCHEMA = """
CREATE TABLE kline_daily (
    symbol TEXT NOT NULL, adjustment_mode TEXT NOT NULL DEFAULT 'unknown', date TEXT NOT NULL,
    open REAL NOT NULL, close REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, volume REAL NOT NULL,
    as_of TEXT, data_version TEXT NOT NULL DEFAULT 'legacy', contract_version TEXT NOT NULL DEFAULT 'legacy',
    fallback_used INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY(symbol, adjustment_mode, date)
)
"""


@pytest.fixture(autouse=True)
def bundled_calendar_only(monkeypatch):
    baseline, warning = calendar._load_calendar_file(
        calendar.BUNDLED_CALENDAR_PATH, calendar.TradeCalendarSource.BUNDLED_BASELINE,
    )
    assert baseline is not None, warning
    monkeypatch.setattr(calendar, "_trade_days", lambda: baseline)
    monkeypatch.setattr(calendar, "_should_auto_refresh", lambda *args: False)


def _rows():
    return [row.model_copy(update={
        "as_of": f"{row.date} 15:15:00", "fetched_at": "2026-08-14T00:00:00.000000Z",
        "session_status": "trading", "open_execution_status": "tradable", "corporate_action_status": "none",
        "adjustment_factor": 1.0, "point_in_time": True, "execution_metadata_version": "factor-execution-evidence.v1",
    }) for row in _complete_h1_rows()]


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
    (2, {"open_execution_status": "locked_limit_down"}),
    (1, {"open_execution_status": "unavailable"}),
    (2, {"open_execution_status": "unavailable"}),
    (2, {"corporate_action_status": "effective_event", "adjustment_factor": 0.5}),
    (1, {"point_in_time": False, "execution_metadata_version": None}),
])
def test_all_cache_read_paths_preserve_execution_metadata_and_labels(tmp_path: Path, position, update) -> None:
    path = tmp_path / "execution.sqlite3"
    cache = SQLiteCache(path)
    cold = _rows()
    cold[position] = cold[position].model_copy(update=update)
    expected_label = asdict(_label(cold))
    cache.save_klines("600001.SH", cold, "test")
    paths = [
        cache.get_klines("600001.SH", 3, 10**9),
        cache.get_klines_many(("600001.SH",), 3, 10**9)["600001.SH"],
        cache.get_klines_by_dates_many(("600001.SH",), [row.date for row in cold])["600001.SH"],
        _ReadOnlyKlineCache(path).get_klines_by_dates_many(("600001.SH",), [row.date for row in cold])["600001.SH"],
    ]
    for warm in paths:
        assert asdict(_label(warm)) == expected_label
        assert factor_calibration_evidence_issue(warm) == factor_calibration_evidence_issue(cold)
        assert [row.model_dump(include=_FIELDS) for row in warm] == [row.model_dump(include=_FIELDS) for row in cold]
        assert all(row.from_cache for row in warm)


@pytest.mark.parametrize("field,value", [
    ("session_status", "suspended"), ("open_execution_status", "locked_limit_up"),
    ("corporate_action_status", "effective_event"), ("adjustment_factor", 0.5),
    ("point_in_time", False), ("execution_metadata_version", "corrected-v2"),
])
def test_execution_only_correction_changes_daily_content_revision(field, value) -> None:
    original = _rows()
    corrected = [*original[:2], original[-1].model_copy(update={field: value})]
    assert _daily_content_revision(original) != _daily_content_revision(corrected)


def _create_old(path: Path, *, kind: str = "current") -> None:
    row = _rows()[0]
    values = {**row.model_dump(), "symbol": "600001.SH", "fallback_used": 0}
    with sqlite3.connect(path) as connection:
        connection.execute(LEGACY_KLINE_TABLE_SQL if kind == "legacy" else _OLD_SCHEMA)
        columns = [str(item[1]) for item in connection.execute("PRAGMA table_info(kline_daily)")]
        connection.execute(
            f"INSERT INTO kline_daily ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )
        if kind == "partial":
            connection.execute("ALTER TABLE kline_daily ADD COLUMN session_status TEXT NOT NULL DEFAULT 'unknown'")
            connection.execute("ALTER TABLE kline_daily ADD COLUMN open_execution_status TEXT NOT NULL DEFAULT 'unknown'")
            connection.execute("ALTER TABLE kline_daily ADD COLUMN point_in_time INTEGER NOT NULL DEFAULT 0")
            connection.execute("UPDATE kline_daily SET session_status='trading', open_execution_status='locked_limit_up', point_in_time=1")


@pytest.mark.parametrize("kind", ["legacy", "current", "partial"])
def test_old_cache_migration_is_conservative_idempotent_and_preserves_indexes(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "old.sqlite3"
    _create_old(path, kind=kind)
    cache = SQLiteCache(settings=Settings(cache_path=path))
    mode = "unknown" if kind == "legacy" else "qfq"
    row = cache.get_klines("600001.SH", 3, 10**9, adjustment_mode=mode)[0]
    assert row.close == _rows()[0].close
    assert row.session_status == ("trading" if kind == "partial" else "unknown")
    assert row.open_execution_status == ("locked_limit_up" if kind == "partial" else "unknown")
    assert row.point_in_time is (kind == "partial")
    assert row.corporate_action_status == "unknown" and row.adjustment_factor is None
    assert row.execution_metadata_version is None
    SQLiteCache(path)
    with sqlite3.connect(path) as connection:
        info = connection.execute("PRAGMA table_info(kline_daily)").fetchall()
        assert _FIELDS <= {item[1] for item in info}
        assert tuple(item[1] for item in sorted(info, key=lambda item: item[5]) if item[5]) == ("symbol", "adjustment_mode", "date")
        assert connection.execute("SELECT COUNT(*) FROM schema_migration WHERE name=?", (_MIGRATION,)).fetchone()[0] == 1
        indexes = {item[1] for item in connection.execute("PRAGMA index_list(kline_daily)")}
        assert {"idx_kline_symbol_date", "idx_kline_daily_adjustment_fetch"} <= indexes
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_readonly_old_history_keeps_unknown_evidence_without_migrating(tmp_path: Path) -> None:
    path = tmp_path / "old-readonly.sqlite3"
    _create_old(path)
    before = path.read_bytes()
    cached = _ReadOnlyKlineCache(path).get_klines_by_dates_many(("600001.SH",), ("2026-08-11",))["600001.SH"][0]
    assert cached.session_status == "unknown" and cached.open_execution_status == "unknown"
    assert cached.corporate_action_status == "unknown" and cached.adjustment_factor is None
    assert cached.point_in_time is False and cached.execution_metadata_version is None
    assert path.read_bytes() == before


def test_existing_explicit_old_column_projection_stays_readable(tmp_path: Path) -> None:
    path = tmp_path / "projection.sqlite3"
    cache = SQLiteCache(path)
    cache.save_klines("600001.SH", _rows(), "test")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        raw = connection.execute(f"SELECT {','.join(_OLD_COLUMNS)} FROM kline_daily ORDER BY date LIMIT 1").fetchone()
    row = row_to_kline(raw)
    assert row.close == _rows()[0].close and row.session_status == "unknown" and row.point_in_time is False


def test_failed_migration_rolls_back_rebuild_rows_and_migration_history(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rollback.sqlite3"
    _create_old(path)
    with sqlite3.connect(path) as connection:
        before = list(connection.iterdump())

        def fail_indexes(_connection):
            raise RuntimeError("injected index failure")

        monkeypatch.setattr(migrations, "ensure_compat_indexes", fail_indexes)
        with pytest.raises(RuntimeError, match="injected index failure"):
            migrations.apply_compat_schema(connection)
        assert list(connection.iterdump()) == before
        assert not connection.in_transaction


def test_invalid_execution_metadata_rolls_back_full_replacement_batch(tmp_path: Path) -> None:
    path = tmp_path / "failed-write.sqlite3"
    cache = SQLiteCache(path)
    cache.save_klines("600001.SH", _rows(), "test")
    original = cache.get_klines("600001.SH", 3, 10**9)
    replacement = [row.model_copy(update={"source": "replacement", "fetched_at": "2026-08-15T00:00:00.000000Z"}) for row in _rows()]
    replacement[1] = replacement[1].model_copy(update={"session_status": "invalid-state"})
    with pytest.raises(ValueError, match="执行状态"):
        cache.save_klines("600001.SH", replacement, "replacement")
    assert cache.get_klines("600001.SH", 3, 10**9) == original


@pytest.mark.parametrize("update", [
    {"point_in_time": "false"}, {"point_in_time": "0"}, {"point_in_time": 1},
    {"adjustment_factor": float("inf")}, {"adjustment_factor": float("nan")},
    {"adjustment_factor": True}, {"adjustment_factor": "1.5"}, {"execution_metadata_version": 1},
])
def test_invalid_raw_metadata_cannot_be_coerced_into_execution_authority(tmp_path: Path, update) -> None:
    cache = SQLiteCache(tmp_path / "bad-evidence.sqlite3")
    cache.save_klines("600001.SH", _rows(), "test")
    before = cache.get_klines("600001.SH", 3, 10**9)
    bad = [*_rows()[:2], _rows()[-1].model_copy(update=update)]
    with pytest.raises(ValueError):
        cache.save_klines("600001.SH", bad, "test")
    assert cache.get_klines("600001.SH", 3, 10**9) == before


@pytest.mark.parametrize("value", ["false", 2])
def test_bad_legacy_boolean_is_rejected_by_readonly_mapper(tmp_path: Path, value) -> None:
    path = tmp_path / "bad-legacy.sqlite3"
    _create_old(path, kind="partial")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE kline_daily SET point_in_time=?", (value,))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="point_in_time"):
        _ReadOnlyKlineCache(path).get_klines_by_dates_many(("600001.SH",), ("2026-08-11",))
    assert path.read_bytes() == before


def test_metadata_only_newer_revision_is_persisted_and_changes_label(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "correction.sqlite3")
    rows = _rows()
    cache.save_klines("600001.SH", rows, "test")
    corrected = [row.model_copy(update={"fetched_at": "2026-08-15T00:00:00.000000Z"}) for row in rows]
    corrected[1] = corrected[1].model_copy(update={"open_execution_status": "locked_limit_up"})
    cache.save_klines("600001.SH", corrected, "test")
    warm = cache.get_klines("600001.SH", 3, 10**9)
    assert warm[1].open_execution_status == "locked_limit_up"
    assert _label(warm) == _label(corrected) and _label(warm) != _label(rows)


def test_invalid_partial_schema_metadata_aborts_migration_without_erasure(tmp_path: Path) -> None:
    path = tmp_path / "invalid-migration.sqlite3"
    _create_old(path, kind="partial")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE kline_daily SET session_status='invalid-state'")
        connection.commit()
        before = list(connection.iterdump())
        with pytest.raises(sqlite3.IntegrityError):
            migrations.apply_compat_schema(connection)
        assert list(connection.iterdump()) == before


def test_migration_cannot_upgrade_text_boolean_through_sqlite_affinity(tmp_path: Path) -> None:
    path = tmp_path / "text-boolean.sqlite3"
    _create_old(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE kline_daily ADD COLUMN point_in_time TEXT")
        connection.execute("UPDATE kline_daily SET point_in_time='1'")
        connection.commit()
        before = list(connection.iterdump())
        with pytest.raises(sqlite3.IntegrityError, match="原始类型"):
            migrations.apply_compat_schema(connection)
        assert list(connection.iterdump()) == before
        assert connection.execute("SELECT typeof(point_in_time),point_in_time FROM kline_daily").fetchone() == ("text", "1")


def test_complete_schema_and_migration_marker_do_not_hide_bad_metadata(tmp_path: Path) -> None:
    path = tmp_path / "already-migrated.sqlite3"
    cache = SQLiteCache(path)
    cache.save_klines("600001.SH", _rows(), "test")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE kline_daily SET point_in_time=2")
        connection.commit()
        before = list(connection.iterdump())
        assert connection.execute("SELECT 1 FROM schema_migration WHERE name=?", (_MIGRATION,)).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="原始类型"):
            migrations.apply_compat_schema(connection)
        assert list(connection.iterdump()) == before


def test_late_insert_error_rolls_back_deleted_window_and_partial_inserts(tmp_path: Path) -> None:
    path = tmp_path / "late-failure.sqlite3"
    cache = SQLiteCache(path)
    cache.save_klines("600001.SH", _rows(), "test")
    before = cache.get_klines("600001.SH", 3, 10**9)
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TRIGGER injected_insert_error BEFORE INSERT ON kline_daily
            WHEN NEW.date='2026-08-12' BEGIN SELECT RAISE(ABORT,'injected insert failure'); END""")
    replacement = [row.model_copy(update={"source": "replacement", "fetched_at": "2026-08-15T00:00:00.000000Z"}) for row in _rows()]
    with pytest.raises(sqlite3.IntegrityError, match="injected insert failure"):
        cache.save_klines("600001.SH", replacement, "replacement")
    assert cache.get_klines("600001.SH", 3, 10**9) == before


@pytest.mark.parametrize("column,sql_type,value", [
    ("adjustment_factor", "TEXT", "0.5"),
    ("execution_metadata_version", "BLOB", b"factor-execution-evidence.v1"),
    ("session_status", "BLOB", b"trading"),
])
def test_readonly_raw_metadata_cannot_gain_authority_through_pydantic_coercion(tmp_path: Path, column, sql_type, value) -> None:
    path = tmp_path / "coercion.sqlite3"
    _create_old(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"ALTER TABLE kline_daily ADD COLUMN {column} {sql_type}")
        connection.execute(f"UPDATE kline_daily SET {column}=?", (value,))
    before = path.read_bytes()
    with pytest.raises(ValueError, match=column):
        _ReadOnlyKlineCache(path).get_klines_by_dates_many(("600001.SH",), ("2026-08-11",))
    assert path.read_bytes() == before


def test_integer_factor_has_same_content_revision_after_sqlite_roundtrip(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "factor-canonical.sqlite3")
    cold = [row.model_copy(update={"adjustment_factor": 1}) for row in _rows()]
    cache.save_klines("600001.SH", cold, "test")
    warm = cache.get_klines("600001.SH", 3, 10**9)
    assert _daily_content_revision(warm) == _daily_content_revision(cold)
