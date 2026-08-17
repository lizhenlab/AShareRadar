from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, cast

import pytest

from app.db.schema import initialize_schema
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs
import app.services.market_scan_probability_replay as replay_module
from app.services.market_scan_probability_replay import (
    HISTORICAL_REPLAY_COHORT_MODE,
    HistoricalReplayConfig,
    HistoricalReplayError,
    build_historical_replay_artifact,
    evaluate_market_scan_probability_replay,
    forecast_historical_replay_shadow,
    historical_replay_artifact_filename,
    historical_replay_feature_values,
    load_historical_replay_artifact,
    replay_rows_to_probability_samples,
    verify_historical_replay_artifact,
    write_historical_replay_artifact,
)
from tests.factories import make_kline


def test_historical_replay_uses_fixed_sessions_costs_and_pit_common_features(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _daily_catalog(95)
    database = tmp_path / "replay.sqlite3"
    _initialize(database)
    symbols = ("600001.SH", "000002.SZ")
    _seed_bars(database, symbols, catalog)
    signal_dates = (catalog[61], catalog[62])
    missing_h5 = catalog[61 + 5 + 1]
    conn = sqlite3.connect(database)
    try:
        with conn:
            conn.execute(
                "DELETE FROM kline_daily WHERE symbol = ? AND adjustment_mode = 'qfq' AND date = ?",
                (symbols[1], missing_h5.isoformat()),
            )
            _insert_bar(conn, symbols[1], missing_h5, 900.0, adjustment_mode="hfq")
    finally:
        conn.close()
    _patch_calendar(monkeypatch, catalog)
    before = database.read_bytes()

    report = evaluate_market_scan_probability_replay(
        database,
        config=HistoricalReplayConfig(
            start_date=signal_dates[0].isoformat(),
            end_date=signal_dates[1].isoformat(),
            minimum_history_bars=61,
            cost_profile="stress",
            execution_notional=12_345.0,
            symbol_limit=2,
            symbols=symbols,
        ),
        generated_at="2026-08-11T12:00:00Z",
    )

    assert database.read_bytes() == before
    assert report["cohort"] == {
        "mode": HISTORICAL_REPLAY_COHORT_MODE,
        "scope": "qfq_kline_daily_deterministic_market_sample",
        "rule_version": "historical-replay-common-ohlcv-v1",
        "official": False,
        "live_cohort_compatible": False,
    }
    records = cast(list[dict[str, Any]], report["records"])
    first = next(item for item in records if item["symbol"] == symbols[0] and item["signal_date"] == signal_dates[0].isoformat())
    assert first["feature_source"]["history_end"] == signal_dates[0].isoformat()
    assert first["entry"]["session_date"] == catalog[62].isoformat()
    assert first["outcomes"][0]["target_session_date"] == catalog[63].isoformat()
    assert first["outcomes"][1]["target_session_date"] == catalog[67].isoformat()
    assert first["outcomes"][2]["target_session_date"] == catalog[82].isoformat()
    h5 = first["outcomes"][1]
    expected_quantity = (int(12_345.0 / first["entry"]["open"]) // 100) * 100
    assert h5["quantity"] == expected_quantity
    assert h5["net_return"] < h5["gross_return"]
    missing = next(item for item in records if item["symbol"] == symbols[1] and item["signal_date"] == signal_dates[0].isoformat())
    assert missing["outcomes"][1]["status"] == "data_unavailable"
    assert missing["outcomes"][1]["reason"] == "fixed_exit_bar_missing_no_shift"
    assert report["metadata_contract"]["historical_st_status"] is None  # type: ignore[index]
    assert report["metadata_contract"]["historical_amount"] is None  # type: ignore[index]
    quality = cast(dict[str, Any], report["quality"])
    assert quality["horizons"]["5"]["modelled_independent_session_count"] == 2
    assert quality["horizons"]["5"]["minimum_required_independent_session_count"] == 232
    fit = cast(dict[str, Any], report["probability_fit"])["horizons"]["5"]
    assert fit["status"] == "insufficient_data"
    assert fit["probability"] is None

    rows = [make_kline(date=value.isoformat(), close=100 + index) for index, value in enumerate(catalog[:70])]
    changed = list(rows)
    changed[-1] = make_kline(date=catalog[69].isoformat(), close=10_000)
    left = historical_replay_feature_values(rows, signal_date=signal_dates[0].isoformat())
    right = historical_replay_feature_values(changed, signal_date=signal_dates[0].isoformat())
    assert left == right


def test_historical_replay_artifact_roundtrip_samples_forecast_and_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _daily_catalog(90)
    database = tmp_path / "artifact.sqlite3"
    _initialize(database)
    _seed_bars(database, ("600001.SH",), catalog)
    _patch_calendar(monkeypatch, catalog)
    signal = catalog[61]
    report = evaluate_market_scan_probability_replay(
        database,
        config=HistoricalReplayConfig(
            signal.isoformat(), signal.isoformat(), symbol_limit=1, symbols=("600001.SH",),
        ),
        generated_at="2026-08-11T12:00:00Z",
    )
    artifact = build_historical_replay_artifact(report)
    target = tmp_path / historical_replay_artifact_filename(artifact)

    written = write_historical_replay_artifact(target, artifact, database_path=database)

    assert written == target.resolve()
    assert load_historical_replay_artifact(target) == artifact
    assert write_historical_replay_artifact(target, artifact, database_path=database) == target.resolve()
    samples = replay_rows_to_probability_samples(artifact, horizon=5)
    assert len(samples) == 1
    assert samples[0].session_date == signal.isoformat()
    assert samples[0].target in {0, 1}
    current = [make_kline(date=value.isoformat(), close=100 + index) for index, value in enumerate(catalog[:62])]
    forecast = forecast_historical_replay_shadow(
        artifact, current, signal_date=signal.isoformat(), horizon=5,
    )
    assert forecast["status"] == "insufficient_data"
    assert forecast["probability"] is None
    assert forecast["signal_date"] == signal.isoformat()
    assert len(cast(list[float], forecast["feature_values"])) == 11

    changed = deepcopy(report)
    record = cast(list[dict[str, Any]], changed["records"])[0]
    record["outcomes"][1]["quantity"] += 100
    unsigned = {key: value for key, value in record.items() if key != "record_digest"}
    record["record_digest"] = replay_module._sha256_json(unsigned)
    with pytest.raises(HistoricalReplayError, match="execution_notional"):
        build_historical_replay_artifact(changed)

    resealed = deepcopy(artifact)
    payload = cast(dict[str, Any], resealed["payload"])
    resealed_record = cast(list[dict[str, Any]], payload["records"])[0]
    resealed_record["feature_values"][0] += 0.01
    resealed_record["feature_vector_digest"] = replay_module._sha256_json(
        {"names": replay_module.HISTORICAL_REPLAY_FEATURE_NAMES, "values": tuple(resealed_record["feature_values"])}
    )
    record_unsigned = {key: value for key, value in resealed_record.items() if key != "record_digest"}
    resealed_record["record_digest"] = replay_module._sha256_json(record_unsigned)
    artifact_unsigned = {key: value for key, value in resealed.items() if key != "integrity"}
    resealed_integrity = cast(dict[str, Any], resealed["integrity"])
    resealed_integrity["integrity_digest"] = replay_module._sha256_json(artifact_unsigned)
    with pytest.raises(HistoricalReplayError, match="records确定性重建"):
        verify_historical_replay_artifact(resealed)

    label_resealed = deepcopy(artifact)
    label_payload = cast(dict[str, Any], label_resealed["payload"])
    label_record = cast(list[dict[str, Any]], label_payload["records"])[0]
    outcome = label_record["outcomes"][1]
    entry_price, quantity = label_record["entry"]["open"], outcome["quantity"]
    outcome["exit_close"] = entry_price * 0.5
    profile = resolve_cost_profile("base")
    buy_amount, sell_amount = entry_price * quantity, outcome["exit_close"] * quantity
    outcome["buy_cost"] = trade_costs(profile, side="buy", gross_amount=buy_amount).total
    outcome["sell_cost"] = trade_costs(profile, side="sell", gross_amount=sell_amount).total
    outcome["gross_return"] = outcome["exit_close"] / entry_price - 1
    outcome["net_return"] = (
        sell_amount - outcome["sell_cost"] - buy_amount - outcome["buy_cost"]
    ) / (buy_amount + outcome["buy_cost"])
    outcome["cost_drag"] = outcome["gross_return"] - outcome["net_return"]
    outcome["gross_positive"] = outcome["gross_return"] > 0
    outcome["net_positive"] = outcome["net_return"] > 0
    label_unsigned = {key: value for key, value in label_record.items() if key != "record_digest"}
    label_record["record_digest"] = replay_module._sha256_json(label_unsigned)
    label_artifact_unsigned = {key: value for key, value in label_resealed.items() if key != "integrity"}
    label_integrity = cast(dict[str, Any], label_resealed["integrity"])
    label_integrity["integrity_digest"] = replay_module._sha256_json(label_artifact_unsigned)
    with pytest.raises(HistoricalReplayError, match="records确定性重建"):
        verify_historical_replay_artifact(label_resealed)


def test_historical_replay_artifact_io_is_bounded_nofollow_and_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _daily_catalog(90)
    database = tmp_path / "artifact-io.sqlite3"
    _initialize(database)
    _seed_bars(database, ("600001.SH",), catalog)
    _patch_calendar(monkeypatch, catalog)
    signal = catalog[61]
    report = evaluate_market_scan_probability_replay(
        database,
        config=HistoricalReplayConfig(
            signal.isoformat(),
            signal.isoformat(),
            symbol_limit=1,
            symbols=("600001.SH",),
        ),
        generated_at="2026-08-11T12:00:00Z",
    )
    artifact = build_historical_replay_artifact(report)
    encoded = replay_module.canonical_historical_replay_json(artifact).encode("utf-8")

    exact_target = tmp_path / "exact.json"
    exact_target.write_bytes(encoded)
    assert (
        write_historical_replay_artifact(
            exact_target,
            artifact,
            database_path=database,
        )
        == exact_target.absolute()
    )

    conflicting_target = tmp_path / "conflict.json"
    conflicting_target.write_bytes(b"{}")
    with pytest.raises(HistoricalReplayError, match="已存在且内容不同"):
        write_historical_replay_artifact(
            conflicting_target,
            artifact,
            database_path=database,
        )
    assert conflicting_target.read_bytes() == b"{}"

    loader_symlink = tmp_path / "loader-symlink.json"
    loader_symlink.symlink_to(exact_target)
    with pytest.raises(HistoricalReplayError, match="artifact 读取失败"):
        load_historical_replay_artifact(loader_symlink)

    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as stream:
        stream.truncate(replay_module._MAXIMUM_ARTIFACT_BYTES + 1)
    with pytest.raises(HistoricalReplayError, match="artifact 读取失败"):
        load_historical_replay_artifact(oversized)

    deeply_nested = tmp_path / "deeply-nested.json"
    deeply_nested.write_bytes(b"[" * 2_000 + b"0" + b"]" * 2_000)
    with pytest.raises(HistoricalReplayError, match="artifact 读取失败"):
        load_historical_replay_artifact(deeply_nested)

    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside-must-not-change")
    hostile_target = tmp_path / "writer-symlink.json"
    hostile_target.symlink_to(outside)
    with pytest.raises(HistoricalReplayError, match="artifact 写入失败"):
        write_historical_replay_artifact(
            hostile_target,
            artifact,
            database_path=database,
        )
    assert hostile_target.is_symlink()
    assert outside.read_bytes() == b"outside-must-not-change"


def test_historical_replay_default_sampling_is_bounded_balanced_and_not_official(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _daily_catalog(90)
    symbols = (
        "600001.SH", "600002.SH", "000001.SZ", "000002.SZ", "830001.BJ", "830002.BJ",
    )
    database = tmp_path / "sampling.sqlite3"
    _initialize(database)
    _seed_bars(database, symbols, catalog)
    _patch_calendar(monkeypatch, catalog)
    signal = catalog[61]

    report = evaluate_market_scan_probability_replay(
        database,
        config=HistoricalReplayConfig(signal.isoformat(), signal.isoformat(), symbol_limit=3),
    )

    quality = cast(dict[str, Any], report["quality"])
    assert quality["universe_symbol_count"] == 6
    assert quality["selected_symbol_count"] == 3
    assert quality["selected_market_counts"] == {"SH": 1, "SZ": 1, "BJ": 1, "OTHER": 0}
    assert quality["sampling_strategy"] == "deterministic_sha256_balanced_round_robin_SH_SZ_BJ_v1"
    records = cast(list[dict[str, str]], report["records"])
    assert {record["symbol"].split(".")[-1] for record in records} == {"SH", "SZ", "BJ"}
    assert report["cohort"]["official"] is False  # type: ignore[index]
    with pytest.raises(ValueError, match="symbol_limit"):
        HistoricalReplayConfig(signal.isoformat(), signal.isoformat(), symbol_limit=501)


def test_historical_replay_resealed_summary_and_contract_tampering_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _daily_catalog(90)
    database = tmp_path / "semantic.sqlite3"
    _initialize(database)
    _seed_bars(database, ("600001.SH",), catalog)
    _patch_calendar(monkeypatch, catalog)
    signal = catalog[61]
    report = evaluate_market_scan_probability_replay(
        database,
        config=HistoricalReplayConfig(
            signal.isoformat(), signal.isoformat(), symbol_limit=1, symbols=("600001.SH",),
        ),
        generated_at="2026-08-11T12:00:00Z",
    )
    artifact = build_historical_replay_artifact(report)
    mutations: tuple[tuple[tuple[object, ...], object], ...] = (
        (("quality", "horizons", "5", "label_coverage"), -1.0),
        (("quality", "requested_signal_session_count"), 99),
        (("quality", "registered_probability_split_defaults", "5", "gap_sessions"), 99),
        (("quality", "excluded_candidate_count"), 99),
        (("metadata_contract", "capacity_modelled"), True),
        (("limitations", 0), "forged_limitation"),
        (("source", "adjustment_mode"), "hfq"),
    )

    for path, forged_value in mutations:
        resealed = deepcopy(artifact)
        payload = cast(dict[str, Any], resealed["payload"])
        _set_nested_value(payload, path, forged_value)
        unsigned = {key: value for key, value in resealed.items() if key != "integrity"}
        cast(dict[str, Any], resealed["integrity"])["integrity_digest"] = replay_module._sha256_json(unsigned)
        with pytest.raises(HistoricalReplayError):
            verify_historical_replay_artifact(resealed)


def test_historical_replay_cli_is_read_only_and_reports_exact_h5_dates(tmp_path: Path) -> None:
    trade_dates = _bundled_trade_dates()
    signal = next(value for value in trade_dates if value >= date(2026, 6, 1))
    signal_index = trade_dates.index(signal)
    catalog = tuple(trade_dates[signal_index - 61:signal_index + 22])
    database = tmp_path / "cli.sqlite3"
    _initialize(database)
    _seed_bars(database, ("600001.SH",), catalog)
    before = database.read_bytes()
    assert not _sqlite_sidecars(database)
    output = tmp_path / "output"

    command = [
        sys.executable,
        "tools/backfill_market_scan_probability_replay.py",
        "--database", str(database),
        "--output-dir", str(output),
        "--start-date", signal.isoformat(),
        "--end-date", signal.isoformat(),
        "--symbol", "600001.SH",
        "--symbol-limit", "1",
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(completed.stdout)

    assert database.read_bytes() == before
    assert not _sqlite_sidecars(database)
    assert summary["database_read_only"] is True
    assert summary["database_immutable"] is True
    assert summary["database_sidecar_policy"] == "reject_db_wal_shm_journal_v1"
    assert summary["official"] is False
    assert summary["h5_coverage"]["modelled_independent_session_count"] == 1
    assert summary["h5_coverage"]["minimum_required_independent_session_count"] == 232
    loaded = load_historical_replay_artifact(summary["artifact"])
    assert loaded["payload"]["probability_fit"]["horizons"]["5"]["probability"] is None  # type: ignore[index]

    outside = tmp_path / "replay-cli-outside"
    outside.mkdir()
    alias = tmp_path / "replay-cli-alias"
    alias.symlink_to(outside, target_is_directory=True)
    output_index = command.index("--output-dir") + 1
    for hostile_output in (alias, alias / "not-created"):
        hostile_command = list(command)
        hostile_command[output_index] = str(hostile_output)
        failed = subprocess.run(
            hostile_command,
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
    assert list(outside.iterdir()) == []
    assert not (outside / "not-created").exists()


def test_historical_replay_rejects_uncheckpointed_wal_without_touching_main_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _daily_catalog(90)
    database = tmp_path / "active-wal.sqlite3"
    _initialize(database)
    _seed_bars(database, ("600001.SH",), catalog)
    _patch_calendar(monkeypatch, catalog)
    signal = catalog[61]
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        before = database.read_bytes()
        writer.execute(
            "UPDATE kline_daily SET close = close + 0.01 WHERE symbol = ? AND date = ?",
            ("600001.SH", catalog[10].isoformat()),
        )
        writer.commit()
        assert database.read_bytes() == before
        assert Path(f"{database}-wal").stat().st_size > 0
        assert Path(f"{database}-shm").is_file()

        with pytest.raises(HistoricalReplayError, match="sidecar"):
            evaluate_market_scan_probability_replay(
                database,
                config=HistoricalReplayConfig(
                    signal.isoformat(),
                    signal.isoformat(),
                    symbol_limit=1,
                    symbols=("600001.SH",),
                ),
            )
        assert database.read_bytes() == before
    finally:
        writer.close()


def test_readonly_connection_preserves_cancellation_closes_and_does_not_touch_source(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cancel.sqlite3"
    _initialize(database)
    before = database.read_bytes()
    cancellation = asyncio.CancelledError("cancel immutable replay")
    connection: sqlite3.Connection | None = None

    with pytest.raises(asyncio.CancelledError) as caught:
        with replay_module._readonly_connection(database) as opened:
            connection = opened
            raise cancellation

    assert caught.value is cancellation
    assert connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    assert database.read_bytes() == before
    assert not _sqlite_sidecars(database)


def test_readonly_connection_keeps_body_failure_when_close_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "close-failure.sqlite3"
    _initialize(database)
    body_failure = KeyboardInterrupt("stop replay")

    class CloseFailingConnection:
        row_factory: object = None
        close_attempted = False

        def execute(self, _statement: str) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            self.close_attempted = True
            raise RuntimeError("close failed")

    connection = CloseFailingConnection()
    monkeypatch.setattr(replay_module.sqlite3, "connect", lambda *_args, **_kwargs: connection)

    with pytest.raises(KeyboardInterrupt) as caught:
        with replay_module._readonly_connection(database):
            raise body_failure

    assert caught.value is body_failure
    assert connection.close_attempted is True
    assert isinstance(caught.value.__cause__, RuntimeError)


def _daily_catalog(count: int) -> tuple[date, ...]:
    start = date(2026, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def _patch_calendar(monkeypatch: pytest.MonkeyPatch, catalog: tuple[date, ...]) -> None:
    catalog_set = frozenset(catalog)

    def fixed(value: date, count: int) -> tuple[date, ...]:
        values = tuple(item for item in catalog if item > value)
        if len(values) < count:
            raise RuntimeError("test calendar exhausted")
        return values[:count]

    monkeypatch.setattr(replay_module, "is_trading_day", lambda value: value in catalog_set)
    monkeypatch.setattr(replay_module, "next_trade_dates", fixed)


def _initialize(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        with conn:
            initialize_schema(conn)
    finally:
        conn.close()


def _seed_bars(path: Path, symbols: tuple[str, ...], catalog: tuple[date, ...]) -> None:
    conn = sqlite3.connect(path)
    try:
        with conn:
            for symbol_index, symbol in enumerate(symbols):
                for index, value in enumerate(catalog):
                    _insert_bar(conn, symbol, value, 20.0 + symbol_index + index * 0.1)
    finally:
        conn.close()


def _insert_bar(
    conn: sqlite3.Connection,
    symbol: str,
    value: date,
    close: float,
    *,
    adjustment_mode: str = "qfq",
) -> None:
    conn.execute(
        """
        INSERT INTO kline_daily (
            symbol, adjustment_mode, date, open, close, high, low, volume, as_of,
            data_version, contract_version, fallback_used, source, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'test', ?)
        """,
        (
            symbol, adjustment_mode, value.isoformat(), close - 0.05, close,
            close + 0.2, close - 0.2, 10_000 + value.toordinal(), value.isoformat(),
            "qfq-test-v1" if adjustment_mode == "qfq" else "hfq-test-v1",
            "daily-kline.v1", value.isoformat(),
        ),
    )


def _bundled_trade_dates() -> list[date]:
    payload = json.loads(
        (Path(__file__).resolve().parents[1] / "app/resources/trading_calendar.json").read_text(encoding="utf-8")
    )
    return [date.fromisoformat(value) for value in payload["trade_dates"]]


def _set_nested_value(root: object, path: tuple[object, ...], value: object) -> None:
    current = root
    for key in path[:-1]:
        current = current[key]  # type: ignore[index]
    current[path[-1]] = value  # type: ignore[index]


def _sqlite_sidecars(database: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for suffix in ("-wal", "-shm", "-journal")
        if (candidate := Path(f"{database}{suffix}")).exists()
    )
