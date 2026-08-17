from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import cast

import pytest

from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline
from app.services.runtime_backup import create_runtime_backup
import app.services.market_scan_probability_history as history_module
from app.services.market_scan_probability_history import (
    PROBABILITY_HISTORY_BARS,
    PROBABILITY_HISTORY_LEGACY_MANIFEST_SCHEMA_VERSION,
    PROBABILITY_HISTORY_MANIFEST_MAX_BYTES,
    PROBABILITY_HISTORY_MANIFEST_SCHEMA_VERSION,
    ProbabilityHistoryConfig,
    ProbabilityHistoryError,
    backfill_market_scan_probability_history,
    canonical_probability_history_json,
    history_manifest_filename,
    load_legacy_market_scan_probability_history_manifest,
    load_market_scan_probability_history_manifest,
    probability_history_manifest_assurance,
    trusted_probability_history_dates,
    upgrade_legacy_attested_probability_history_manifest,
    verify_market_scan_probability_history_manifest,
)
from app.services.market_scan_probability_replay import (
    HistoricalReplayConfig,
    evaluate_market_scan_probability_replay,
)
from tools import backfill_market_scan_probability_history as history_cli


ANCHOR_DATE = "2026-08-11"
V1_SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "market_scan_probability_history_v1_source.json"


class _FatalHistoryBoundary(BaseException):
    pass


class FakeHistoryProvider:
    source_name = "fake-tencent-qfq"

    def __init__(
        self,
        dates: tuple[str, ...],
        *,
        transient: frozenset[str] = frozenset(),
        failures: frozenset[str] = frozenset(),
        short: frozenset[str] = frozenset(),
    ) -> None:
        self.dates = dates
        self.transient = transient
        self.failures = failures
        self.short = short
        self.attempts: Counter[str] = Counter()
        self.active = 0
        self.maximum_active = 0

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        assert limit == PROBABILITY_HISTORY_BARS
        self.attempts[symbol] += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.001)
            if symbol in self.failures:
                raise RuntimeError("secret_token=must-not-leak")
            if symbol in self.transient and self.attempts[symbol] == 1:
                raise RuntimeError("temporary secret_token=must-not-leak")
            dates = self.dates[:-1] if symbol in self.short else self.dates
            return [_bar(value, index) for index, value in enumerate(dates)]
        finally:
            self.active -= 1


def test_history_backfill_is_balanced_bounded_replay_compatible_and_restart_verifiable(
    tmp_path: Path,
) -> None:
    symbols = (
        "600001.SH", "600002.SH", "600003.SH",
        "000001.SZ", "000002.SZ", "000003.SZ",
        "830001.BJ", "830002.BJ", "830003.BJ",
    )
    source = _source_database(tmp_path / "live.sqlite3", symbols)
    source_before = _sha256_file(source)
    dates = trusted_probability_history_dates(ANCHOR_DATE)
    provider = FakeHistoryProvider(
        dates,
        transient=frozenset({"000001.SZ"}),
        short=frozenset({"600002.SH"}),
    )
    target = tmp_path / "research" / "history.sqlite3"
    output = tmp_path / "manifests"
    config = ProbabilityHistoryConfig(
        symbol_limit=9,
        concurrency=2,
        max_retries=2,
        retry_delay_seconds=0,
        minimum_symbols_per_market=2,
        minimum_symbols_total=6,
    )

    result = asyncio.run(
        backfill_market_scan_probability_history(
            source, target, output, config=config, provider=provider,
            generated_at="2026-08-11T09:00:00+00:00",
        ),
    )

    assert _sha256_file(source) == source_before
    assert provider.maximum_active == 2
    assert provider.attempts["000001.SZ"] == 2
    assert max(provider.attempts.values()) <= 3
    assert result.database_path == target.resolve()
    assert result.manifest_path.name.endswith(".manifest.json")
    loaded = load_market_scan_probability_history_manifest(result.manifest_path)
    assert loaded["schema_version"] == PROBABILITY_HISTORY_MANIFEST_SCHEMA_VERSION
    assert probability_history_manifest_assurance(loaded) == "attested_v2"
    assert load_market_scan_probability_history_manifest(
        result.manifest_path,
        require_attested=True,
    ) == loaded
    payload = cast(dict[str, object], loaded["payload"])
    quality = cast(dict[str, object], payload["quality"])
    database = cast(dict[str, object], payload["database"])
    replay_input = cast(dict[str, object], payload["replay_input"])
    source_evidence = cast(dict[str, object], payload["source"])
    runtime_backup = cast(dict[str, object], source_evidence["runtime_backup"])
    assert quality["accepted_symbol_count"] == 8
    assert quality["accepted_market_counts"] == {"SH": 2, "SZ": 3, "BJ": 3}
    assert quality["maximum_observed_concurrency"] == 2
    assert quality["excluded_reason_counts"] == {"short_history_or_new_listing": 1}
    assert database["row_count"] == 8 * PROBABILITY_HISTORY_BARS
    assert database["bars_per_symbol"] == PROBABILITY_HISTORY_BARS
    assert database["bar_start"] == dates[0]
    assert database["bar_end"] == dates[-1]
    assert runtime_backup["verified_before_and_after_fetch"] is True
    assert runtime_backup["manifest_file"] == "manifest.json"
    assert runtime_backup["verified_sha256"] == cast(dict[str, object], runtime_backup["manifest"])["sha256"]
    assert runtime_backup["sqlite_sidecar_policy"] == "reject_db_wal_shm_journal_v1"
    assert payload["cohort"] == {
        "mode": "historical_replay_v1",
        "scope": "tencent_qfq_360_fixed_session_deterministic_sample",
        "rule_version": "historical-replay-history-source-v1",
        "official": False,
        "live_cohort_compatible": False,
        "production_ranking_effect": "none",
    }

    replay = evaluate_market_scan_probability_replay(
        target,
        config=HistoricalReplayConfig(
            start_date=str(replay_input["start_date"]),
            end_date=str(replay_input["end_date"]),
            symbol_limit=8,
        ),
        generated_at="2026-08-11T09:01:00+00:00",
    )
    replay_quality = cast(dict[str, object], replay["quality"])
    replay_cohort = cast(dict[str, object], replay["cohort"])
    assert replay_cohort["mode"] == "historical_replay_v1"
    assert replay_quality["selected_symbol_count"] == 8
    assert int(cast(int, replay_quality["record_independent_session_count"])) >= 260


def test_history_backfill_provider_failure_is_retried_sanitized_and_fail_closed(
    tmp_path: Path,
) -> None:
    symbols = ("600001.SH", "000001.SZ", "830001.BJ")
    source = _source_database(tmp_path / "live.sqlite3", symbols)
    source_before = _sha256_file(source)
    provider = FakeHistoryProvider(
        trusted_probability_history_dates(ANCHOR_DATE),
        failures=frozenset({"830001.BJ"}),
    )
    target = tmp_path / "research.sqlite3"
    output = tmp_path / "manifests"
    config = ProbabilityHistoryConfig(
        symbol_limit=3,
        symbols=symbols,
        retry_delay_seconds=0,
        minimum_symbols_per_market=1,
        minimum_symbols_total=3,
    )

    with pytest.raises(ProbabilityHistoryError) as caught:
        asyncio.run(
            backfill_market_scan_probability_history(
                source, target, output, config=config, provider=provider,
            ),
        )

    assert "secret_token" not in str(caught.value)
    assert provider.attempts["830001.BJ"] == 3
    assert not target.exists()
    assert not list(output.glob("*.manifest.json"))
    assert _sha256_file(source) == source_before


def test_history_validation_and_mechanical_error_boundaries_are_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_configs = (
        {"symbol_limit": True},
        {"history_bars": 359},
        {"concurrency": 0},
        {"max_retries": 3},
        {"retry_delay_seconds": float("nan")},
        {"minimum_symbols_per_market": 0},
        {"symbol_limit": 6, "minimum_symbols_per_market": 2, "minimum_symbols_total": 3},
        {"symbol_limit": 3, "minimum_symbols_per_market": 1, "minimum_symbols_total": 4},
        {
            "symbol_limit": 3,
            "symbols": ("600001.SH", "000001.SZ", "830001.BJ", "600002.SH"),
            "minimum_symbols_per_market": 1,
            "minimum_symbols_total": 3,
        },
    )
    for values in invalid_configs:
        with pytest.raises(ValueError):
            ProbabilityHistoryConfig(**values)  # type: ignore[arg-type]
    for anchor, count in (("bad-date", 1), (ANCHOR_DATE, 0), (ANCHOR_DATE, True)):
        with pytest.raises(ValueError):
            trusted_probability_history_dates(anchor, count)  # type: ignore[arg-type]

    categorized = (
        (history_module.ProviderCoverageMiss("missing"), "provider_coverage_miss"),
        (history_module.ProviderTransportError("offline"), "provider_transport_error"),
        (history_module.ProviderInstrumentDataError("bad row"), "provider_instrument_data_error"),
        (history_module.ProviderProtocolError("bad payload"), "provider_protocol_error"),
        (TimeoutError(), "provider_timeout"),
        (RuntimeError(), "provider_error"),
    )
    assert [history_module._provider_error_category(error) for error, _expected in categorized] == [
        expected for _error, expected in categorized
    ]

    first = _bar(ANCHOR_DATE, 0)
    second = _bar("2026-08-08", 1)
    rejection_cases = (
        ((first, first), (ANCHOR_DATE,), "unexpected_bar_count"),
        ((second,), (ANCHOR_DATE,), "fixed_session_coverage_mismatch_or_suspension"),
        (
            (first, second.model_copy(update={"source": "other"})),
            (ANCHOR_DATE, "2026-08-08"),
            "series_contract_conflict",
        ),
        ((first.model_copy(update={"adjustment_mode": "hfq"}),), (ANCHOR_DATE,), "invalid_qfq_series_contract"),
        ((first.model_copy(update={"high": 0.1}),), (ANCHOR_DATE,), "invalid_ohlcv"),
        ((first,), (ANCHOR_DATE,), None),
    )
    for rows, expected_dates, expected in rejection_cases:
        assert history_module._download_rejection_reason(rows, expected_dates) == expected

    config = ProbabilityHistoryConfig(
        symbol_limit=3,
        minimum_symbols_per_market=1,
        minimum_symbols_total=3,
    )
    all_markets = ("600001.SH", "000001.SZ", "830001.BJ")
    with pytest.raises(ProbabilityHistoryError, match="总量门槛"):
        history_module._require_candidate_minimums(all_markets[:2], all_markets, config)
    with pytest.raises(ProbabilityHistoryError, match="BJ"):
        history_module._require_candidate_minimums(
            ("600001.SH", "600002.SH", "000001.SZ"),
            all_markets,
            config,
        )
    with pytest.raises(ProbabilityHistoryError, match="总量门槛"):
        history_module._require_coverage({symbol: (first,) for symbol in all_markets[:2]}, config)
    with pytest.raises(ProbabilityHistoryError, match="市场门槛"):
        history_module._require_coverage(
            {symbol: (first,) for symbol in ("600001.SH", "600002.SH", "000001.SZ")},
            config,
        )
    assert history_module._balanced_round_robin({"SH": (), "SZ": (), "BJ": ()}, 3) == ()
    assert history_module._equity_market("bad") is None
    assert history_module._equity_market("123456.BJ") is None
    with pytest.raises(ProbabilityHistoryError, match="replay 日期范围"):
        history_module._replay_input(tmp_path / "history.sqlite3", (ANCHOR_DATE,))
    with pytest.raises(ProbabilityHistoryError, match="必须是 object"):
        history_module._mapping([], "value")
    with pytest.raises(ProbabilityHistoryError, match="不可序列化"):
        history_module._json_copy({"bad": {1, 2}})

    target = tmp_path / "target.sqlite3"
    stage = tmp_path / "stage.sqlite3"
    stage.write_bytes(b"stage")
    with monkeypatch.context() as scoped:
        scoped.setattr(
            history_module.os,
            "link",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(FileExistsError()),
        )
        with pytest.raises(ProbabilityHistoryError, match="并发发布冲突"):
            history_module._publish_database_exclusive(stage, target)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            history_module.os,
            "link",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()),
        )
        with pytest.raises(ProbabilityHistoryError, match="原子发布失败"):
            history_module._publish_database_exclusive(stage, target)
    for error, message in (
        (history_module.ArtifactContentConflictError(target), "内容地址"),
        (history_module.ArtifactPublishConflictError(target), "并发发布冲突"),
    ):
        with monkeypatch.context() as scoped:
            scoped.setattr(
                history_module,
                "exclusive_atomic_publish",
                lambda *_args, _error=error, **_kwargs: (_ for _ in ()).throw(_error),
            )
            with pytest.raises(ProbabilityHistoryError, match=message):
                history_module._publish_bytes_exclusive(target, b"value")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            history_module.tempfile,
            "mkstemp",
            lambda **_kwargs: (_ for _ in ()).throw(PermissionError()),
        )
        with pytest.raises(ProbabilityHistoryError, match="staging SQLite 无法创建"):
            history_module._temporary_database_path(target)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            history_module.sqlite3,
            "connect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError()),
        )
        with pytest.raises(ProbabilityHistoryError, match="staging SQLite 写入失败"):
            history_module._write_staging_database(target, {}, "2026-08-11T00:00:00Z")


def test_history_publish_preserves_fatal_failure_and_removes_partial_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ("600001.SH", "000001.SZ", "830001.BJ")
    source = _source_database(tmp_path / "live.sqlite3", symbols)
    target = tmp_path / "research" / "history.sqlite3"
    output = tmp_path / "manifests"
    failure = _FatalHistoryBoundary("cancel history publication")

    def fail_after_database_publish(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise failure

    monkeypatch.setattr(
        history_module,
        "verify_market_scan_probability_history_manifest",
        fail_after_database_publish,
    )
    with pytest.raises(_FatalHistoryBoundary) as caught:
        asyncio.run(
            backfill_market_scan_probability_history(
                source,
                target,
                output,
                config=ProbabilityHistoryConfig(
                    symbol_limit=3,
                    symbols=symbols,
                    retry_delay_seconds=0,
                    minimum_symbols_per_market=1,
                    minimum_symbols_total=3,
                ),
                provider=FakeHistoryProvider(trusted_probability_history_dates(ANCHOR_DATE)),
            ),
        )

    assert caught.value is failure
    assert not target.exists()
    assert not list(target.parent.glob(f".{target.name}.*.staging"))
    assert not list(output.glob("*.manifest.json"))


def test_history_manifest_v1_v2_compatibility_matrix_and_tamper_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ("600001.SH", "000001.SZ", "830001.BJ")
    source = _source_database(tmp_path / "live.sqlite3", symbols)
    result = asyncio.run(
        backfill_market_scan_probability_history(
            source,
            tmp_path / "history.sqlite3",
            tmp_path / "v2",
            config=ProbabilityHistoryConfig(
                symbol_limit=3,
                symbols=symbols,
                retry_delay_seconds=0,
                minimum_symbols_per_market=1,
                minimum_symbols_total=3,
            ),
            provider=FakeHistoryProvider(trusted_probability_history_dates(ANCHOR_DATE)),
            generated_at="2026-08-11T09:00:00+00:00",
        ),
    )
    assert result.manifest["schema_version"] == PROBABILITY_HISTORY_MANIFEST_SCHEMA_VERSION
    assert probability_history_manifest_assurance(result.manifest) == "attested_v2"
    verify_market_scan_probability_history_manifest(
        result.manifest,
        database_path=result.database_path,
        require_attested=True,
    )

    transitional_v1 = deepcopy(result.manifest)
    transitional_v1["schema_version"] = PROBABILITY_HISTORY_LEGACY_MANIFEST_SCHEMA_VERSION
    _reseal_manifest(transitional_v1)
    assert probability_history_manifest_assurance(transitional_v1) == "legacy_attested_v1"
    verify_market_scan_probability_history_manifest(
        transitional_v1,
        database_path=result.database_path,
        require_attested=False,
    )
    with pytest.raises(ProbabilityHistoryError, match="legacy_attested_v1"):
        verify_market_scan_probability_history_manifest(
            transitional_v1,
            database_path=result.database_path,
            require_attested=True,
        )
    transitional_path = tmp_path / "v1-attested" / history_manifest_filename(transitional_v1)
    transitional_path.parent.mkdir()
    transitional_path.write_text(
        canonical_probability_history_json(transitional_v1) + "\n",
        encoding="utf-8",
    )
    upgraded_path = upgrade_legacy_attested_probability_history_manifest(
        transitional_path,
        tmp_path / "upgraded-v2",
        database_path=result.database_path,
    )
    upgraded = load_market_scan_probability_history_manifest(
        upgraded_path,
        database_path=result.database_path,
    )
    assert probability_history_manifest_assurance(upgraded) == "attested_v2"

    rejected_mutations: tuple[tuple[tuple[str, ...], object, str], ...] = (
        (("schema_version",), "unsupported", "schema_version"),
        (("payload", "schema_version"), "unsupported", "payload schema"),
        (("payload", "status"), "failed", "cohort/status"),
        (("payload", "config", "history_bars"), 359, "config"),
        (("payload", "replay_input", "official"), True, "replay_input"),
        (("payload", "source", "unexpected"), True, "source 字段"),
        (("payload", "source", "runtime_backup", "unexpected"), True, "备份证明字段"),
        (
            ("payload", "source", "runtime_backup", "verified_before_and_after_fetch"),
            False,
            "备份证明状态",
        ),
        (("payload", "source", "runtime_backup", "manifest"), {}, "manifest schema"),
        (("payload", "source", "runtime_backup", "verified_sha256"), "bad", "manifest 绑定"),
        (("payload", "quality", "status"), "failed", "quality 状态"),
        (("payload", "quality", "accepted_symbol_count"), 999, "accepted symbol"),
        (("payload", "quality", "accepted_market_counts"), {}, "accepted market"),
        (("payload", "config", "minimum_symbols_total"), 4, "总量门槛"),
        (("payload", "config", "minimum_symbols_per_market"), 2, "分市场门槛"),
        (("payload", "database", "sha256"), "0" * 64, "SQLite 内容"),
    )
    for mutation_path, value, message in rejected_mutations:
        mutated = deepcopy(result.manifest)
        _set_nested_mapping_value(mutated, mutation_path, value)
        _reseal_manifest(mutated)
        with pytest.raises(ProbabilityHistoryError, match=message):
            verify_market_scan_probability_history_manifest(
                mutated,
                database_path=result.database_path,
            )

    invalid_integrity = deepcopy(result.manifest)
    cast(dict[str, object], invalid_integrity["integrity"])["unexpected"] = True
    with pytest.raises(ProbabilityHistoryError, match="integrity 字段"):
        verify_market_scan_probability_history_manifest(invalid_integrity)

    non_object_manifest = tmp_path / "non-object-history.json"
    non_object_manifest.write_text("[]", encoding="utf-8")
    with pytest.raises(ProbabilityHistoryError, match="顶层必须是 object"):
        load_market_scan_probability_history_manifest(non_object_manifest)

    wrong_name = tmp_path / "wrong-history-name.json"
    wrong_name.write_text(canonical_probability_history_json(upgraded), encoding="utf-8")
    with pytest.raises(ProbabilityHistoryError, match="文件名不是内容地址"):
        load_market_scan_probability_history_manifest(
            wrong_name,
            database_path=result.database_path,
        )
    with pytest.raises(ProbabilityHistoryError, match="当前 attested v2"):
        load_legacy_market_scan_probability_history_manifest(
            upgraded_path,
            database_path=result.database_path,
        )

    manifest_alias = tmp_path / "history-manifest-alias.json"
    manifest_alias.symlink_to(upgraded_path)
    with pytest.raises(ProbabilityHistoryError, match="无法读取"):
        load_market_scan_probability_history_manifest(
            manifest_alias,
            database_path=result.database_path,
        )

    oversized = tmp_path / "oversized-history-manifest.json"
    oversized.write_bytes(b" " * (PROBABILITY_HISTORY_MANIFEST_MAX_BYTES + 1))
    with pytest.raises(ProbabilityHistoryError, match="无法读取"):
        load_market_scan_probability_history_manifest(
            oversized,
            database_path=result.database_path,
        )

    hostile_output = tmp_path / "hostile-upgrade-output"
    hostile_output.mkdir()
    outside_target = tmp_path / "outside-manifest.json"
    outside_target.write_text("preserve", encoding="utf-8")
    (hostile_output / history_manifest_filename(upgraded)).symlink_to(outside_target)
    with pytest.raises(ProbabilityHistoryError, match="原子发布失败"):
        upgrade_legacy_attested_probability_history_manifest(
            transitional_path,
            hostile_output,
            database_path=result.database_path,
        )
    assert outside_target.read_text(encoding="utf-8") == "preserve"

    upgrade_outside = tmp_path / "upgrade-outside"
    upgrade_outside.mkdir()
    upgrade_alias = tmp_path / "upgrade-alias"
    upgrade_alias.symlink_to(upgrade_outside, target_is_directory=True)
    with pytest.raises(ProbabilityHistoryError, match="upgrade manifest output.*不接受符号链接"):
        upgrade_legacy_attested_probability_history_manifest(
            transitional_path,
            upgrade_alias / "not-created",
            database_path=result.database_path,
        )
    assert not (upgrade_outside / "not-created").exists()

    failed_upgrade_output = tmp_path / "failed-upgrade-output"
    real_fsync = history_module.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if history_module.stat.S_ISDIR(history_module.os.fstat(descriptor).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(descriptor)

    with monkeypatch.context() as scoped:
        scoped.setattr(history_module.os, "fsync", fail_directory_fsync)
        with pytest.raises(ProbabilityHistoryError, match="manifest 原子发布失败"):
            upgrade_legacy_attested_probability_history_manifest(
                transitional_path,
                failed_upgrade_output,
                database_path=result.database_path,
            )
    assert list(failed_upgrade_output.iterdir()) == []

    legacy_v1 = deepcopy(result.manifest)
    legacy_v1["schema_version"] = PROBABILITY_HISTORY_LEGACY_MANIFEST_SCHEMA_VERSION
    payload = cast(dict[str, object], legacy_v1["payload"])
    payload["source"] = json.loads(V1_SOURCE_FIXTURE.read_text(encoding="utf-8"))
    _reseal_manifest(legacy_v1)
    legacy_path = tmp_path / "v1" / history_manifest_filename(legacy_v1)
    legacy_path.parent.mkdir()
    legacy_path.write_text(canonical_probability_history_json(legacy_v1) + "\n", encoding="utf-8")
    with pytest.raises(ProbabilityHistoryError, match="legacy_unattested"):
        load_market_scan_probability_history_manifest(
            legacy_path,
            database_path=result.database_path,
        )
    loaded_legacy = load_legacy_market_scan_probability_history_manifest(
        legacy_path,
        database_path=result.database_path,
    )
    assert loaded_legacy.assurance == "legacy_unattested"
    assert loaded_legacy.trusted_for_attested_research is False
    assert probability_history_manifest_assurance(loaded_legacy.manifest) == "legacy_unattested"

    for mutation_path, value, message in (
        (("payload", "source", "unexpected"), True, "legacy v1 source 字段"),
        (("payload", "source", "database_fingerprint", "size_bytes"), -1, "fingerprint"),
    ):
        mutated_legacy = deepcopy(legacy_v1)
        _set_nested_mapping_value(mutated_legacy, mutation_path, value)
        _reseal_manifest(mutated_legacy)
        with pytest.raises(ProbabilityHistoryError, match=message):
            verify_market_scan_probability_history_manifest(
                mutated_legacy,
                database_path=result.database_path,
                require_attested=False,
            )
    with pytest.raises(ProbabilityHistoryError, match="legacy_unattested"):
        upgrade_legacy_attested_probability_history_manifest(
            legacy_path,
            tmp_path / "forbidden-upgrade",
            database_path=result.database_path,
        )

    tampered = deepcopy(legacy_v1)
    cast(dict[str, object], cast(dict[str, object], tampered["payload"])["source"])[
        "database_read_only"
    ] = False
    _reseal_manifest(tampered)
    with pytest.raises(ProbabilityHistoryError, match="只读/provider"):
        verify_market_scan_probability_history_manifest(
            tampered,
            database_path=result.database_path,
            require_attested=False,
        )


def test_history_backfill_requires_matching_runtime_backup_manifest(tmp_path: Path) -> None:
    symbols = ("600001.SH", "000001.SZ", "830001.BJ")
    dates = trusted_probability_history_dates(ANCHOR_DATE)
    config = ProbabilityHistoryConfig(
        symbol_limit=3,
        symbols=symbols,
        retry_delay_seconds=0,
        minimum_symbols_per_market=1,
        minimum_symbols_total=3,
    )
    unverified = _raw_source_database(tmp_path / "unverified" / "runtime.sqlite3", symbols)
    with pytest.raises(ProbabilityHistoryError, match="manifest 不存在"):
        asyncio.run(
            backfill_market_scan_probability_history(
                unverified,
                tmp_path / "missing-manifest.sqlite3",
                tmp_path / "missing-manifest-output",
                config=config,
                provider=FakeHistoryProvider(dates),
            ),
        )

    source = _source_database(tmp_path / "verified.sqlite3", symbols)
    with source.open("ab") as stream:
        stream.write(b"manifest-mismatch")
    with pytest.raises(ProbabilityHistoryError, match="SHA-256"):
        asyncio.run(
            backfill_market_scan_probability_history(
                source,
                tmp_path / "mismatch.sqlite3",
                tmp_path / "mismatch-output",
                config=config,
                provider=FakeHistoryProvider(dates),
            ),
        )


def test_history_backfill_rejects_wal_change_hidden_from_main_file_fingerprint(
    tmp_path: Path,
) -> None:
    symbols = ("600001.SH", "000001.SZ", "830001.BJ")
    source = _source_database(tmp_path / "source.sqlite3", symbols)
    dates = trusted_probability_history_dates(ANCHOR_DATE)
    config = ProbabilityHistoryConfig(
        symbol_limit=3,
        symbols=symbols,
        retry_delay_seconds=0,
        minimum_symbols_per_market=1,
        minimum_symbols_total=3,
    )
    with sqlite3.connect(source) as conn:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    _rewrite_backup_manifest_for_current_main_file(source)
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        before = (source.stat().st_size, source.stat().st_mtime_ns)
        writer.execute(
            "UPDATE kline_daily SET date = ? WHERE symbol = ?",
            ("2026-08-08", symbols[0]),
        )
        writer.commit()
        after = (source.stat().st_size, source.stat().st_mtime_ns)
        assert after == before
        assert Path(f"{source}-wal").is_file()

        with pytest.raises(ProbabilityHistoryError, match="SQLite sidecar"):
            asyncio.run(
                backfill_market_scan_probability_history(
                    source,
                    tmp_path / "wal.sqlite3",
                    tmp_path / "wal-output",
                    config=config,
                    provider=FakeHistoryProvider(dates),
                ),
            )
    finally:
        writer.close()


def test_history_backfill_rejects_output_retarget_during_provider_fetch_without_outside_writes(
    tmp_path: Path,
) -> None:
    symbols = ("600001.SH", "000001.SZ", "830001.BJ")
    source = _source_database(tmp_path / "live.sqlite3", symbols)
    dates = trusted_probability_history_dates(ANCHOR_DATE)
    target_parent = tmp_path / "target-parent"
    output = tmp_path / "manifest-output"
    moved_target_parent = tmp_path / "moved-target-parent"
    moved_output = tmp_path / "moved-manifest-output"
    outside_target = tmp_path / "outside-target"
    outside_output = tmp_path / "outside-output"
    outside_target.mkdir()
    outside_output.mkdir()

    class RetargetingProvider(FakeHistoryProvider):
        retargeted = False

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            if not self.retargeted:
                self.retargeted = True
                target_parent.rename(moved_target_parent)
                target_parent.symlink_to(outside_target, target_is_directory=True)
                output.rename(moved_output)
                output.symlink_to(outside_output, target_is_directory=True)
            return await super().kline(symbol, limit)

    config = ProbabilityHistoryConfig(
        symbol_limit=3,
        symbols=symbols,
        retry_delay_seconds=0,
        minimum_symbols_per_market=1,
        minimum_symbols_total=3,
    )
    with pytest.raises(ProbabilityHistoryError, match="target parent.*发生变化"):
        asyncio.run(
            backfill_market_scan_probability_history(
                source,
                target_parent / "history.sqlite3",
                output,
                config=config,
                provider=RetargetingProvider(dates),
            ),
        )

    assert list(outside_target.iterdir()) == []
    assert list(outside_output.iterdir()) == []
    assert list(moved_target_parent.iterdir()) == []
    assert list(moved_output.iterdir()) == []


def test_history_backfill_maps_shared_manifest_io_error_and_cleans_published_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ("600001.SH", "000001.SZ", "830001.BJ")
    source = _source_database(tmp_path / "live.sqlite3", symbols)
    dates = trusted_probability_history_dates(ANCHOR_DATE)
    target = tmp_path / "target" / "history.sqlite3"
    output = tmp_path / "output"
    config = ProbabilityHistoryConfig(
        symbol_limit=3,
        symbols=symbols,
        retry_delay_seconds=0,
        minimum_symbols_per_market=1,
        minimum_symbols_total=3,
    )
    real_fsync = history_module.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if history_module.stat.S_ISDIR(history_module.os.fstat(descriptor).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(history_module.os, "fsync", fail_directory_fsync)

    with pytest.raises(ProbabilityHistoryError, match="manifest 原子发布失败"):
        asyncio.run(
            backfill_market_scan_probability_history(
                source,
                target,
                output,
                config=config,
                provider=FakeHistoryProvider(dates),
            ),
        )

    assert not target.exists()
    assert list(output.iterdir()) == []
    assert list(target.parent.glob(".history.sqlite3.*.staging")) == []


def test_history_alias_guards_immutability_manifest_tamper_and_cli_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    symbols = ("600001.SH", "000001.SZ", "830001.BJ")
    source = _source_database(tmp_path / "live.sqlite3", symbols)
    dates = trusted_probability_history_dates(ANCHOR_DATE)
    config = ProbabilityHistoryConfig(
        symbol_limit=3,
        symbols=symbols,
        retry_delay_seconds=0,
        minimum_symbols_per_market=1,
        minimum_symbols_total=3,
    )
    with pytest.raises(ProbabilityHistoryError, match="彼此隔离"):
        asyncio.run(
            backfill_market_scan_probability_history(
                source, source, tmp_path / "output", config=config,
                provider=FakeHistoryProvider(dates),
            ),
        )

    real_target_parent = tmp_path / "real-target-parent"
    real_target_parent.mkdir()
    target_parent_alias = tmp_path / "target-parent-alias"
    target_parent_alias.symlink_to(real_target_parent, target_is_directory=True)
    with pytest.raises(ProbabilityHistoryError, match="target parent.*不接受符号链接"):
        asyncio.run(
            backfill_market_scan_probability_history(
                source,
                target_parent_alias / "history.sqlite3",
                tmp_path / "alias-output",
                config=config,
                provider=FakeHistoryProvider(dates),
            ),
        )
    assert list(real_target_parent.iterdir()) == []

    with pytest.raises(ProbabilityHistoryError, match="target parent.*不接受符号链接"):
        asyncio.run(
            backfill_market_scan_probability_history(
                source,
                target_parent_alias / "not-created" / "history.sqlite3",
                tmp_path / "nested-alias-output",
                config=config,
                provider=FakeHistoryProvider(dates),
            ),
        )
    assert not (real_target_parent / "not-created").exists()

    target_parent_loop = tmp_path / "target-parent-loop"
    target_parent_loop.symlink_to(target_parent_loop, target_is_directory=True)
    with pytest.raises(ProbabilityHistoryError, match="source/target/output 路径无法解析"):
        asyncio.run(
            backfill_market_scan_probability_history(
                source,
                target_parent_loop / "not-created" / "history.sqlite3",
                tmp_path / "loop-output",
                config=config,
                provider=FakeHistoryProvider(dates),
            ),
        )
    assert not (tmp_path / "loop-output").exists()

    result = asyncio.run(
        backfill_market_scan_probability_history(
            source, tmp_path / "history.sqlite3", tmp_path / "output",
            config=config, provider=FakeHistoryProvider(dates),
            generated_at="2026-08-11T09:00:00+00:00",
        ),
    )
    with pytest.raises(ProbabilityHistoryError, match="拒绝覆盖"):
        asyncio.run(
            backfill_market_scan_probability_history(
                source, result.database_path, tmp_path / "second-output",
                config=config, provider=FakeHistoryProvider(dates),
            ),
        )
    tampered = deepcopy(result.manifest)
    cast(dict[str, object], cast(dict[str, object], tampered["payload"])["cohort"])["official"] = True
    with pytest.raises(ProbabilityHistoryError, match="integrity digest"):
        verify_market_scan_probability_history_manifest(tampered)

    async def fake_backfill(*_args: object, **_kwargs: object):
        return result

    monkeypatch.setattr(history_cli, "backfill_market_scan_probability_history", fake_backfill)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "history-cli", "--source-database", str(source), "--target-database",
            str(tmp_path / "unused.sqlite3"), "--output-dir", str(tmp_path / "unused-output"),
        ],
    )
    assert history_cli.main() == 0
    summary = json.loads(capsys.readouterr().out)
    summary_payload = cast(dict[str, object], result.manifest["payload"])
    summary_source = cast(dict[str, object], summary_payload["source"])
    summary_runtime_backup = cast(dict[str, object], summary_source["runtime_backup"])
    assert summary["database"] == str(result.database_path)
    assert summary["selected_market_counts"] == {"SH": 1, "SZ": 1, "BJ": 1}
    assert summary["bars_per_symbol"] == 360
    assert summary["bar_coverage"] == 1.0
    assert summary["source_backup_manifest"] == "manifest.json"
    assert summary["source_backup_sha256"] == summary_runtime_backup["verified_sha256"]
    assert summary["source_backup_verified_before_and_after_fetch"] is True
    assert summary["replay_input"]["official"] is False


def _source_database(path: Path, symbols: tuple[str, ...]) -> Path:
    raw = _raw_source_database(path, symbols)
    backup = create_runtime_backup(raw, path.with_name(f"{path.stem}-runtime-backup"))
    return Path(backup.database_path)


def _raw_source_database(path: Path, symbols: tuple[str, ...]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE kline_daily (symbol TEXT NOT NULL, adjustment_mode TEXT NOT NULL, date TEXT NOT NULL)",
        )
        conn.executemany(
            "INSERT INTO kline_daily VALUES (?, 'qfq', ?)",
            [(symbol, ANCHOR_DATE) for symbol in symbols],
        )
        conn.commit()
    return path


def _rewrite_backup_manifest_for_current_main_file(source: Path) -> None:
    manifest_path = source.parent / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["sha256"] = _sha256_file(source)
    payload["database_size_bytes"] = source.stat().st_size
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _reseal_manifest(manifest: dict[str, object]) -> None:
    manifest.pop("integrity", None)
    manifest["integrity"] = {
        "algorithm": "sha256",
        "integrity_digest": hashlib.sha256(canonical_probability_history_json(manifest).encode()).hexdigest(),
        "notice": "SHA-256 detects accidental mutation; it is not an authenticity signature.",
    }


def _set_nested_mapping_value(
    value: dict[str, object],
    path: tuple[str, ...],
    replacement: object,
) -> None:
    current = value
    for key in path[:-1]:
        current = cast(dict[str, object], current[key])
    current[path[-1]] = replacement


def _bar(value: str, index: int) -> Kline:
    close = 10.0 + index / 1000
    return Kline(
        date=value,
        open=close - 0.02,
        close=close,
        high=close + 0.05,
        low=close - 0.05,
        volume=100_000 + index,
        adjustment_mode="qfq",
        as_of=ANCHOR_DATE,
        data_version=f"{DAILY_KLINE_CONTRACT_VERSION}|qfq|fake-tencent-qfq|{ANCHOR_DATE}",
        contract_version=DAILY_KLINE_CONTRACT_VERSION,
        source="fake-tencent-qfq",
        fetched_at="2026-08-11T09:00:00+00:00",
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
