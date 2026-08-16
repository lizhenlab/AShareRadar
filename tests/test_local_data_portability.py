from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import app.repositories.advice_reviews as advice_review_repository_module
import app.services.user_data_portability as portability_module
from app.config import Settings
from app.db.advice_review_schema import _backfill_current_plan_revisions
from app.db.paper_trading_schema import paper_run_output_digest
from app.db.schema_migrations import AUDIT_TIMESTAMP_UTC_MIGRATION
from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline
from app.models.local_data import USER_DATA_TABLE_ALLOWLIST, UserDataBundle
from app.models.paper_trading import PaperStrategyCreate
from app.models.reviews import AdviceReviewPlanUpdate
from app.models.strategy_lab import (
    StrategyHardFilter,
    StrategySpecCreate,
    StrategySpecInput,
    StrategySpecUpdate,
)
from app.services.cache import SQLiteCache
from app.services.paper_trading import simulate_paper_portfolio
from app.services.research_replay import evaluate_advice_forward_window
from app.services.user_data_portability import (
    export_user_data,
    import_user_data,
    user_data_state_digest,
)


def test_export_contains_only_exact_user_data_allowlist(tmp_path: Path) -> None:
    path = tmp_path / "source.sqlite3"
    SQLiteCache(path)
    _insert_watchlist(path, "600519.SH", note="source")

    bundle = export_user_data(path)

    assert set(bundle.tables) == USER_DATA_TABLE_ALLOWLIST
    assert bundle.row_counts["watchlist"] == 1
    assert "quote_snapshot" not in bundle.tables
    assert "provider_status" not in bundle.tables
    assert "schema_migration" not in bundle.tables
    assert bundle.tables["watchlist"].column_types is not None
    assert bundle.tables["watchlist"].column_types["symbol"] == "TEXT"
    assert bundle.audit_timestamps is not None
    assert bundle.audit_timestamps.semantics == "utc-fixed"
    assert bundle.audit_timestamps.legacy_timezone is None
    assert bundle.tables["watchlist"].rows[0]["created_at"].endswith(".000000Z")


def test_merge_dry_run_reports_changes_without_writing_and_commit_is_source_wins(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="source")
    _insert_watchlist(target, "600519.SH", note="target")
    bundle = export_user_data(source)

    preview = import_user_data(target, bundle, mode="merge", dry_run=True)

    assert preview.committed is False
    assert preview.tables["watchlist"].updated == 1
    assert _watchlist_note(target, "600519.SH") == "target"

    result = import_user_data(target, bundle, mode="merge", dry_run=False)

    assert result.committed is True
    assert result.conflict_strategy == "remap_surrogate_ids_source_wins_on_stable_keys"
    assert result.tables["watchlist"].remapped == 0
    assert _watchlist_note(target, "600519.SH") == "source"


def test_strategy_specs_and_immutable_versions_port_across_id_collisions_idempotently(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    source_cache = SQLiteCache(source)
    target_cache = SQLiteCache(target)
    created = source_cache.strategy_lab_service.create(
        StrategySpecCreate(spec=StrategySpecInput(name="可携带策略"), confirmed=True)
    )
    source_cache.strategy_lab_service.update(
        created.strategy_id,
        StrategySpecUpdate(
            spec=created.spec.model_copy(
                update={
                    "hard_filters": [
                        StrategyHardFilter(
                            field="amount",
                            operator="gte",
                            value=100_000_000.0,
                        )
                    ]
                }
            ),
            expected_revision=1,
            confirmed=True,
        ),
    )
    target_cache.strategy_lab_service.create(
        StrategySpecCreate(spec=StrategySpecInput(name="目标已有策略"), confirmed=True)
    )
    bundle = export_user_data(source)

    first = import_user_data(target, bundle, mode="merge", dry_run=False)
    second = import_user_data(target, bundle, mode="merge", dry_run=False)

    assert first.tables["strategy_spec"].remapped == 1
    assert first.tables["strategy_spec_version"].remapped == 1
    assert second.tables["strategy_spec"].inserted == 0
    assert second.tables["strategy_spec_version"].inserted == 0
    with sqlite3.connect(target) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM strategy_spec_version WHERE revision = 1"
            ).fetchall()
        }
        versions = conn.execute(
            """
            SELECT COUNT(*)
            FROM strategy_spec_version AS version
            JOIN strategy_spec AS strategy ON strategy.id = version.strategy_id
            WHERE version.name = '可携带策略' AND strategy.current_revision = 2
            """
        ).fetchone()[0]
    assert names == {"可携带策略", "目标已有策略"}
    assert versions == 2


def test_v1_bundle_without_metadata_keeps_aware_audit_timestamps_compatible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="legacy-aware")
    payload = export_user_data(source).model_dump(mode="json")
    payload.pop("audit_timestamps")

    import_user_data(
        target,
        UserDataBundle.model_validate(payload),
        mode="merge",
        dry_run=False,
    )

    assert _watchlist_note(target, "600519.SH") == "legacy-aware"


def test_replace_requires_complete_snapshot_and_removes_rows_absent_from_source(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="kept")
    _insert_watchlist(target, "000001.SZ", note="removed")
    bundle = export_user_data(source)
    incomplete_payload = bundle.model_dump(mode="json")
    incomplete_payload["tables"].pop("stock_note")
    incomplete_payload["row_counts"].pop("stock_note")
    incomplete = UserDataBundle.model_validate(incomplete_payload)

    with pytest.raises(ValueError, match="replace 模式必须包含全部用户数据表"):
        import_user_data(target, incomplete, mode="replace", dry_run=False)

    import_user_data(target, bundle, mode="replace", dry_run=False)

    assert _watchlist_symbols(target) == ["600519.SH"]


def test_import_rejects_column_type_and_primary_key_drift_before_writing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="source")
    payload = export_user_data(source).model_dump(mode="json")
    payload["tables"]["watchlist"]["rows"] = []
    payload["row_counts"]["watchlist"] = 0
    column_drift = deepcopy(payload)
    note_index = column_drift["tables"]["watchlist"]["columns"].index("note")
    column_drift["tables"]["watchlist"]["columns"][note_index] = "other_note"
    column_drift["tables"]["watchlist"]["column_types"]["other_note"] = column_drift["tables"]["watchlist"]["column_types"].pop("note")
    type_drift = deepcopy(payload)
    type_drift["tables"]["watchlist"]["column_types"]["note"] = "INTEGER"
    primary_key_drift = deepcopy(payload)
    primary_key_drift["tables"]["watchlist"]["primary_key"] = ["code"]

    for drifted_payload, message in (
        (column_drift, "列结构"),
        (type_drift, "列类型"),
        (primary_key_drift, "主键结构"),
    ):
        bundle = UserDataBundle.model_validate(drifted_payload)
        with pytest.raises(ValueError, match=message):
            import_user_data(target, bundle, mode="merge", dry_run=False)

    assert _watchlist_symbols(target) == []


def test_migrated_export_imports_into_fresh_database_with_different_column_order(
    tmp_path: Path,
) -> None:
    source = tmp_path / "migrated.sqlite3"
    target = tmp_path / "fresh.sqlite3"
    _create_legacy_watchlist_database(source)
    SQLiteCache(
        source,
        settings=Settings(
            cache_path=source,
            scheduler_enabled=False,
            legacy_audit_timezone="Asia/Shanghai",
        ),
    )
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="migrated")

    source_columns = _table_columns(source, "watchlist")
    target_columns = _table_columns(target, "watchlist")
    assert source_columns != target_columns
    assert set(source_columns) == set(target_columns)

    bundle = export_user_data(source)
    preview = import_user_data(target, bundle, mode="merge", dry_run=True)

    assert preview.tables["watchlist"].inserted == 1
    assert _watchlist_symbols(target) == []

    import_user_data(target, bundle, mode="merge", dry_run=False)

    assert _watchlist_note(target, "600519.SH") == "migrated"


def test_merge_remaps_colliding_surrogate_ids_and_dependent_relationships(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_alert_chain(source, "600519.SH", marker="source")
    _insert_alert_chain(target, "000001.SZ", marker="target")
    _insert_stock_note(source, "600519.SH", marker="source")
    _insert_stock_note(target, "000001.SZ", marker="target")
    source_advice = _insert_advice(source, "600519.SH", marker="source")
    target_advice = _insert_advice(target, "000001.SZ", marker="target")
    source_plan = _insert_review_plan(source, source_advice, "600519.SH", marker="source")
    target_plan = _insert_review_plan(target, target_advice, "000001.SZ", marker="target")
    _insert_review_result(source, source_plan, source_advice, "600519.SH", marker="source")
    _insert_review_result(target, target_plan, target_advice, "000001.SZ", marker="target")
    bundle = export_user_data(source)

    preview = import_user_data(target, bundle, mode="merge", dry_run=True)

    remapped_tables = (
        "alert_rule",
        "alert_event",
        "stock_note",
        "advice_history",
        "advice_review_plan",
        "advice_review_result",
    )
    for table in remapped_tables:
        assert preview.tables[table].inserted == 1
        assert preview.tables[table].updated == 0
        assert preview.tables[table].remapped == 1
    assert preview.totals.remapped == len(remapped_tables)
    assert _table_count(target, "advice_history") == 1
    assert _table_count(target, "advice_review_result") == 1

    result = import_user_data(target, bundle, mode="merge", dry_run=False)

    assert result.tables == preview.tables
    assert result.totals == preview.totals
    assert _joined_alert_markers(target) == {("source", "source"), ("target", "target")}
    assert _joined_review_markers(target) == {
        ("source", "source", "source"),
        ("target", "target", "target"),
    }
    assert _stock_note_markers(target) == {"source", "target"}


def test_repeated_merge_is_idempotent_for_surrogate_rows(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="source")
    _insert_alert_chain(source, "600519.SH", marker="source")
    _insert_stock_note(source, "600519.SH", marker="source")
    advice_id = _insert_advice(source, "600519.SH", marker="source")
    plan_id = _insert_review_plan(source, advice_id, "600519.SH", marker="source")
    _insert_review_result(source, plan_id, advice_id, "600519.SH", marker="source")
    bundle = export_user_data(source)

    import_user_data(target, bundle, mode="merge", dry_run=False)
    preview = import_user_data(target, bundle, mode="merge", dry_run=True)

    for table in USER_DATA_TABLE_ALLOWLIST:
        assert preview.tables[table].inserted == 0
        assert preview.tables[table].updated == 0
        assert preview.tables[table].remapped == 0
        assert preview.tables[table].unchanged == bundle.row_counts[table]
        assert _table_count(target, table) == bundle.row_counts[table]


def test_populated_paper_run_round_trips_with_all_relationships(tmp_path: Path) -> None:
    source = tmp_path / "paper-source.sqlite3"
    target = tmp_path / "paper-target.sqlite3"
    source_cache = SQLiteCache(source)
    target_cache = SQLiteCache(target)
    advice_id = _insert_advice(source, "600519.SH", marker="paper")
    plan_id = _insert_review_plan(source, advice_id, "600519.SH", marker="paper")
    plan = source_cache.advice_review_plan(plan_id)
    assert plan is not None
    source_cache.create_paper_strategy(
        plan,
        PaperStrategyCreate(
            plan_id=plan.id,
            expected_plan_revision=plan.revision,
            expected_plan_payload_digest=plan.plan_payload_digest,
            allocation_pct=10,
        ),
        activation_market_time="2026-07-16 10:00:00",
    )
    strategy = source_cache.paper_strategies()[0].model_copy(
        update={
            "snapshot_adjustment_mode": "qfq",
            "snapshot_anchor_date": "2026-07-16",
            "snapshot_anchor_close": 100,
            "snapshot_data_version": "portability-paper-v1",
            "snapshot_contract_version": DAILY_KLINE_CONTRACT_VERSION,
        }
    )
    draft = simulate_paper_portfolio(
        source_cache.paper_trading_account(),
        [strategy],
        {
            strategy.symbol: [
                _paper_bar("2026-07-16", 100, 101, 99, 100),
                _paper_bar("2026-07-17", 100, 105, 99, 104),
                _paper_bar("2026-07-20", 104, 112, 103, 111),
            ]
        },
        as_of=datetime(2026, 7, 20, 16),
    )
    source_dashboard = source_cache.save_paper_simulation(draft)
    run_id = source_dashboard.selected_run_id
    assert run_id is not None
    bundle = export_user_data(source)

    for table in (
        "paper_trading_account",
        "paper_strategy",
        "paper_trading_run",
        "paper_strategy_result",
        "paper_trade",
        "paper_equity_snapshot",
        "paper_trading_event",
    ):
        assert bundle.row_counts[table] > 0

    import_user_data(target, bundle, mode="merge", dry_run=False)
    restored = target_cache.paper_trading_dashboard(run_id=run_id)
    repeated = import_user_data(target, bundle, mode="merge", dry_run=True)

    assert restored.selected_run_id == run_id
    assert restored.runs[0].input_fingerprint == draft.input_fingerprint
    assert [item.side for item in reversed(restored.trades)] == ["buy", "sell"]
    assert restored.strategies[0].status == "closed"
    assert restored.events
    assert restored.equity_curve
    for table in (
        "paper_strategy",
        "paper_trading_run",
        "paper_strategy_result",
        "paper_trade",
        "paper_equity_snapshot",
        "paper_trading_event",
    ):
        assert repeated.tables[table].unchanged == bundle.row_counts[table]
        assert repeated.tables[table].inserted == 0
        assert repeated.tables[table].remapped == 0


def test_paper_output_digest_round_trips_and_rejects_tampered_bundle_atomically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper-digest-source.sqlite3"
    target = tmp_path / "paper-digest-target.sqlite3"
    source_cache = SQLiteCache(source)
    SQLiteCache(target)
    _save_portable_paper_run(source_cache, source, marker="digest")
    bundle = export_user_data(source)
    run_row = bundle.tables["paper_trading_run"].rows[0]
    source_digest = str(run_row["output_digest"])
    assert len(source_digest) == 64

    import_user_data(target, bundle, mode="merge", dry_run=False)

    with sqlite3.connect(target) as conn:
        imported = conn.execute(
            "SELECT id, output_digest FROM paper_trading_run"
        ).fetchone()
        assert imported is not None
        assert imported[1] == source_digest
        assert imported[1] == paper_run_output_digest(conn, int(imported[0]))

    tampered = bundle.model_dump(mode="json")
    tampered["tables"]["paper_strategy_result"]["rows"][0]["last_price"] = 999.0
    before = user_data_state_digest(target)
    with pytest.raises(ValueError, match="输出摘要"):
        import_user_data(
            target,
            UserDataBundle.model_validate(tampered),
            mode="merge",
            dry_run=False,
        )
    assert user_data_state_digest(target) == before


def test_legacy_paper_bundle_without_output_digest_is_rehashed_on_import(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-paper-source.sqlite3"
    target = tmp_path / "legacy-paper-target.sqlite3"
    source_cache = SQLiteCache(source)
    SQLiteCache(target)
    _save_portable_paper_run(source_cache, source, marker="legacy-digest")
    payload = export_user_data(source).model_dump(mode="json")
    _remove_bundle_columns(payload, "paper_trading_run", {"output_digest"})

    import_user_data(
        target,
        UserDataBundle.model_validate(payload),
        mode="merge",
        dry_run=False,
    )

    with sqlite3.connect(target) as conn:
        run_id, digest = conn.execute(
            "SELECT id, output_digest FROM paper_trading_run"
        ).fetchone()
        assert len(str(digest)) == 64
        assert digest == paper_run_output_digest(conn, int(run_id))


def test_nonempty_paper_run_requires_complete_output_ledger_before_writing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "partial-paper-source.sqlite3"
    target = tmp_path / "partial-paper-target.sqlite3"
    source_cache = SQLiteCache(source)
    SQLiteCache(target)
    _save_portable_paper_run(source_cache, source, marker="partial-digest")
    payload = export_user_data(source).model_dump(mode="json")
    payload["tables"].pop("paper_trading_event")
    payload["row_counts"].pop("paper_trading_event")

    with pytest.raises(ValueError, match="完整输出账本"):
        import_user_data(
            target,
            UserDataBundle.model_validate(payload),
            mode="merge",
            dry_run=False,
        )

    assert _table_count(target, "advice_history") == 0
    assert _table_count(target, "paper_trading_run") == 0


def test_paper_output_digest_is_recomputed_after_plan_digest_remap(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper-remap-source.sqlite3"
    target = tmp_path / "paper-remap-target.sqlite3"
    source_cache = SQLiteCache(source)
    target_cache = SQLiteCache(target)
    _save_portable_paper_run(source_cache, source, marker="source")
    _save_portable_paper_run(
        target_cache,
        target,
        marker="target",
        symbol="000001.SZ",
    )
    source_bundle = export_user_data(source)
    source_digest = str(source_bundle.tables["paper_trading_run"].rows[0]["output_digest"])

    result = import_user_data(target, source_bundle, mode="merge", dry_run=False)

    assert result.tables["advice_history"].remapped == 1
    assert result.tables["advice_review_plan"].remapped == 1
    assert result.tables["paper_strategy"].remapped == 1
    assert result.tables["paper_trading_run"].remapped == 1
    with sqlite3.connect(target) as conn:
        rows = conn.execute(
            """
            SELECT run.id, run.output_digest
            FROM paper_trading_run AS run
            JOIN paper_strategy_result AS result ON result.run_id = run.id
            JOIN paper_strategy AS strategy ON strategy.id = result.strategy_id
            WHERE strategy.symbol = '600519.SH'
            """
        ).fetchall()
        assert len(rows) == 1
        run_id, imported_digest = int(rows[0][0]), str(rows[0][1])
        assert imported_digest == paper_run_output_digest(conn, run_id)
    assert imported_digest != source_digest


def test_strategy_only_merge_cannot_rewrite_a_strategy_bound_to_an_immutable_run(
    tmp_path: Path,
) -> None:
    source = tmp_path / "strategy-only-source.sqlite3"
    target = tmp_path / "strategy-only-target.sqlite3"
    source_cache = SQLiteCache(source)
    target_cache = SQLiteCache(target)
    target_run_id = _save_portable_paper_run(
        target_cache,
        target,
        marker="shared-plan",
    )
    source_advice = _insert_advice(source, "600519.SH", marker="shared-plan")
    source_plan_id = _insert_review_plan(
        source,
        source_advice,
        "600519.SH",
        marker="shared-plan",
    )
    source_plan = source_cache.advice_review_plan(source_plan_id)
    assert source_plan is not None
    source_cache.create_paper_strategy(
        source_plan,
        PaperStrategyCreate(
            plan_id=source_plan.id,
            expected_plan_revision=source_plan.revision,
            expected_plan_payload_digest=source_plan.plan_payload_digest,
            allocation_pct=25,
        ),
        activation_market_time="2026-07-16 10:00:00",
    )
    source_bundle = export_user_data(source)
    assert source_bundle.row_counts["paper_trading_run"] == 0
    before_state = user_data_state_digest(target)
    with sqlite3.connect(target) as conn:
        before = conn.execute(
            """
            SELECT strategy.allocation_pct, run.output_digest
            FROM paper_strategy AS strategy
            JOIN paper_strategy_result AS result ON result.strategy_id = strategy.id
            JOIN paper_trading_run AS run ON run.id = result.run_id
            WHERE run.id = ?
            """,
            (target_run_id,),
        ).fetchone()

    with pytest.raises(ValueError, match="不可变运行"):
        import_user_data(target, source_bundle, mode="merge", dry_run=False)

    assert user_data_state_digest(target) == before_state
    with sqlite3.connect(target) as conn:
        after = conn.execute(
            """
            SELECT strategy.allocation_pct, run.output_digest
            FROM paper_strategy AS strategy
            JOIN paper_strategy_result AS result ON result.strategy_id = strategy.id
            JOIN paper_trading_run AS run ON run.id = result.run_id
            WHERE run.id = ?
            """,
            (target_run_id,),
        ).fetchone()
        assert after[1] == paper_run_output_digest(conn, target_run_id)
    assert after == before
    assert target_cache.paper_trading_dashboard(run_id=target_run_id).selected_run_id == (
        target_run_id
    )


def test_account_only_merge_cannot_change_initial_cash_after_paper_history_exists(
    tmp_path: Path,
) -> None:
    target = tmp_path / "paper-account-history-target.sqlite3"
    target_cache = SQLiteCache(target)
    _save_portable_paper_run(target_cache, target, marker="account-history")
    payload = export_user_data(target).model_dump(mode="json")
    account = payload["tables"]["paper_trading_account"]
    account["rows"][0]["initial_cash"] = 2_000_000
    payload["tables"] = {"paper_trading_account": account}
    payload["row_counts"] = {"paper_trading_account": 1}
    bundle = UserDataBundle.model_validate(payload)
    before = user_data_state_digest(target)

    with pytest.raises(ValueError, match="模拟账户"):
        import_user_data(target, bundle, mode="merge", dry_run=False)

    assert user_data_state_digest(target) == before
    assert target_cache.paper_trading_account().initial_cash == 1_000_000


def test_import_normalizes_legacy_audit_fields_after_schema_migration_and_orders_by_epoch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    target_cache = SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="older")
    _insert_watchlist(source, "000001.SZ", note="newer")
    payload = export_user_data(source).model_dump(mode="json")
    rows = {
        row["symbol"]: row
        for row in payload["tables"]["watchlist"]["rows"]
    }
    rows["600519.SH"]["created_at"] = "2026-07-24T00:30:00Z"
    rows["600519.SH"]["updated_at"] = "2026-07-24T00:30:00Z"
    rows["000001.SZ"]["created_at"] = "2026-07-24 09:30:00.500000"
    rows["000001.SZ"]["updated_at"] = "2026-07-24 09:30:00.500000"
    payload.pop("audit_timestamps")
    bundle = UserDataBundle.model_validate(payload)
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migration WHERE name = ?",
            (AUDIT_TIMESTAMP_UTC_MIGRATION,),
        ).fetchone()[0] == 1

    import_user_data(
        target,
        bundle,
        mode="merge",
        dry_run=False,
        legacy_audit_timezone="Asia/Shanghai",
    )

    with sqlite3.connect(target) as conn:
        stored = dict(conn.execute("SELECT symbol, updated_at FROM watchlist"))
    assert stored == {
        "000001.SZ": "2026-07-24T01:30:00.500000Z",
        "600519.SH": "2026-07-24T00:30:00.000000Z",
    }
    with sqlite3.connect(target) as conn:
        conn.execute(
            "UPDATE watchlist SET updated_at = '2026-07-24T01:30:00.000000Z' "
            "WHERE symbol = '600519.SH'"
        )
    assert target_cache.watchlist_repo.symbol_selection().active_symbols == (
        "000001.SZ",
        "600519.SH",
    )


def test_import_uses_configured_timezone_and_rejects_invalid_audit_text(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="source")
    payload = export_user_data(source).model_dump(mode="json")
    row = payload["tables"]["watchlist"]["rows"][0]
    row["created_at"] = "2026-07-24 09:30:00"
    row["updated_at"] = "2026-07-24 09:30:00"
    payload.pop("audit_timestamps")
    bundle = UserDataBundle.model_validate(payload)

    with pytest.raises(ValueError, match="bundle 语义一致"):
        import_user_data(target, bundle, mode="merge", dry_run=True)

    import_user_data(
        target,
        bundle,
        mode="merge",
        dry_run=False,
        legacy_audit_timezone="America/Los_Angeles",
    )

    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT updated_at FROM watchlist").fetchone()[0] == "2026-07-24T16:30:00.000000Z"
    row["updated_at"] = "not-a-timestamp"
    invalid_bundle = UserDataBundle.model_validate(payload)
    with pytest.raises(ValueError, match="watchlist.updated_at 不是与 bundle 语义一致"):
        import_user_data(
            target,
            invalid_bundle,
            mode="merge",
            dry_run=True,
            legacy_audit_timezone="America/Los_Angeles",
        )


def test_legacy_metadata_preserves_source_timezone_across_target_timezones(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="source")
    payload = export_user_data(source).model_dump(mode="json")
    payload["audit_timestamps"] = {
        "semantics": "legacy-naive",
        "legacy_timezone": "America/Los_Angeles",
    }
    row = payload["tables"]["watchlist"]["rows"][0]
    row["created_at"] = "2026-07-24 09:30:00"
    row["updated_at"] = "2026-07-24 09:30:00"

    import_user_data(
        target,
        UserDataBundle.model_validate(payload),
        mode="merge",
        dry_run=False,
        legacy_audit_timezone="Asia/Shanghai",
    )

    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT updated_at FROM watchlist").fetchone()[0] == (
            "2026-07-24T16:30:00.000000Z"
        )


def test_utc_semantics_reject_naive_or_non_fixed_audit_timestamps(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="source")
    payload = export_user_data(source).model_dump(mode="json")

    for invalid in ("2026-07-24 09:30:00", "2026-07-24T09:30:00+08:00"):
        candidate = deepcopy(payload)
        candidate["tables"]["watchlist"]["rows"][0]["updated_at"] = invalid
        with pytest.raises(ValueError, match="bundle 语义一致"):
            import_user_data(
                target,
                UserDataBundle.model_validate(candidate),
                mode="merge",
                dry_run=True,
                legacy_audit_timezone="Asia/Shanghai",
            )


def test_bundle_rejects_invalid_audit_timestamp_semantics(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    SQLiteCache(source)
    _insert_watchlist(source, "600519.SH", note="source")
    payload = export_user_data(source).model_dump(mode="json")
    payload["audit_timestamps"] = {"semantics": "local-time"}

    with pytest.raises(ValueError, match="audit_timestamps.semantics"):
        UserDataBundle.model_validate(payload)


def test_merge_rejects_child_rows_without_bundled_surrogate_parent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_alert_chain(source, "600519.SH", marker="source")
    _insert_alert_chain(target, "000001.SZ", marker="target")
    payload = export_user_data(source).model_dump(mode="json")
    payload["tables"].pop("alert_rule")
    payload["row_counts"].pop("alert_rule")
    child_only_bundle = UserDataBundle.model_validate(payload)

    with pytest.raises(ValueError, match="外键约束要求导入包同时包含 alert_rule"):
        import_user_data(target, child_only_bundle, mode="merge", dry_run=False)

    assert _joined_alert_markers(target) == {("target", "target")}


def test_v1_bundle_without_frozen_price_provenance_is_rejected_atomically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    advice_id = _insert_advice(source, "600519.SH", marker="legacy")
    plan_id = _insert_review_plan(source, advice_id, "600519.SH", marker="legacy")
    _insert_review_result(source, plan_id, advice_id, "600519.SH", marker="legacy")
    payload = export_user_data(source).model_dump(mode="json")
    _remove_bundle_columns(
        payload,
        "advice_history",
        {
            "kline_adjustment_mode",
            "kline_anchor_date",
            "kline_anchor_close",
            "kline_data_version",
            "kline_contract_version",
        },
    )
    _remove_bundle_columns(
        payload,
        "advice_review_plan",
        {
            "snapshot_adjustment_mode",
            "snapshot_anchor_date",
            "snapshot_anchor_close",
            "snapshot_data_version",
            "snapshot_contract_version",
            "trigger_basis",
            "invalidation_basis",
        },
    )
    _remove_bundle_columns(
        payload,
        "advice_review_result",
        {
            "snapshot_adjustment_mode",
            "snapshot_anchor_date",
            "snapshot_anchor_close",
            "snapshot_data_version",
            "snapshot_contract_version",
            "evaluation_adjustment_mode",
            "evaluation_data_version",
            "evaluation_contract_version",
            "anchor_evaluation_close",
            "price_scale_factor",
            "normalized_entry_price",
            "normalized_target_price",
            "normalized_stop_price",
            "trigger_basis",
            "invalidation_basis",
        },
    )

    with pytest.raises(ValueError, match="不可变版本账本"):
        import_user_data(
            target,
            UserDataBundle.model_validate(payload),
            mode="merge",
            dry_run=False,
        )

    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM advice_history").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM advice_review_plan").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM advice_review_result").fetchone()[0] == 0


def test_foreign_key_failure_rolls_back_every_table(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="must rollback")
    advice_id = _insert_advice(source, "600519.SH", marker="broken")
    _insert_review_plan(source, advice_id, "600519.SH", marker="broken")
    payload = export_user_data(source).model_dump(mode="json")
    payload["tables"].pop("advice_history")
    payload["row_counts"].pop("advice_history")
    broken_bundle = UserDataBundle.model_validate(payload)

    with pytest.raises(ValueError, match="外键约束"):
        import_user_data(target, broken_bundle, mode="merge", dry_run=False)

    assert _watchlist_symbols(target) == []
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM advice_review_plan").fetchone()[0] == 0


@pytest.mark.parametrize("missing_kind", ["table", "historical_revision"])
def test_import_rejects_plan_without_complete_revision_ledger_before_writing(
    tmp_path: Path,
    missing_kind: str,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    source_cache = SQLiteCache(source)
    SQLiteCache(target)
    plan_id = _insert_review_plan(
        source,
        _insert_advice(source, "600519.SH", marker="source"),
        "600519.SH",
        marker="source-v1",
    )
    plan = source_cache.advice_review_plan(plan_id)
    assert plan is not None
    source_cache.update_advice_review_plan(
        plan.id,
        AdviceReviewPlanUpdate(expected_revision=1, hypothesis="source-v2"),
    )
    payload = export_user_data(source).model_dump(mode="json")
    if missing_kind == "table":
        payload["tables"].pop("advice_review_plan_revision")
        payload["row_counts"].pop("advice_review_plan_revision")
        message = "不可变版本账本"
    else:
        ledger_rows = payload["tables"]["advice_review_plan_revision"]["rows"]
        payload["tables"]["advice_review_plan_revision"]["rows"] = [
            row for row in ledger_rows if row["revision"] != 1
        ]
        payload["row_counts"]["advice_review_plan_revision"] = 1
        message = "完整、连续"

    with pytest.raises(ValueError, match=message):
        import_user_data(
            target,
            UserDataBundle.model_validate(payload),
            mode="merge",
            dry_run=False,
        )

    assert _table_count(target, "advice_history") == 0
    assert _table_count(target, "advice_review_plan") == 0
    assert _table_count(target, "advice_review_plan_revision") == 0


def test_merge_rejects_stale_plan_revision_and_rolls_back_every_table(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    source_cache = SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="stale-source")
    plan_id = _insert_review_plan(
        source,
        _insert_advice(source, "600519.SH", marker="source"),
        "600519.SH",
        marker="source-v1",
    )
    stale_bundle = export_user_data(source)
    source_cache.update_advice_review_plan(
        plan_id,
        AdviceReviewPlanUpdate(expected_revision=1, hypothesis="source-v2"),
    )
    import_user_data(target, export_user_data(source), mode="merge", dry_run=False)
    before = user_data_state_digest(target)

    with pytest.raises(ValueError, match="不能用旧修订回退"):
        import_user_data(target, stale_bundle, mode="merge", dry_run=False)

    assert user_data_state_digest(target) == before
    with sqlite3.connect(target) as conn:
        plan = conn.execute(
            "SELECT revision, hypothesis FROM advice_review_plan"
        ).fetchone()
    assert plan == (2, "source-v2")
    assert _watchlist_note(target, "600519.SH") == "stale-source"


def test_collision_remap_rewrites_canonical_plan_result_and_paper_bindings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    source_cache = SQLiteCache(source)
    target_cache = SQLiteCache(target)
    source_advice = _insert_advice(source, "600519.SH", marker="source")
    target_advice = _insert_advice(target, "000001.SZ", marker="target")
    source_plan_id = _insert_review_plan(
        source,
        source_advice,
        "600519.SH",
        marker="source",
    )
    target_plan_id = _insert_review_plan(
        target,
        target_advice,
        "000001.SZ",
        marker="target",
    )
    source_plan = source_cache.advice_review_plan(source_plan_id)
    target_plan = target_cache.advice_review_plan(target_plan_id)
    assert source_plan is not None and target_plan is not None
    bars = [
        _paper_bar("2026-07-16", 100, 102, 99, 101),
        _paper_bar("2026-07-17", 101, 104, 100, 103),
        _paper_bar("2026-07-20", 103, 106, 102, 105),
        _paper_bar("2026-07-21", 105, 108, 104, 107),
        _paper_bar("2026-07-22", 107, 109, 106, 108),
    ]
    source_evaluation = None
    for cache, plan in ((source_cache, source_plan), (target_cache, target_plan)):
        evaluation = evaluate_advice_forward_window(
            plan,
            bars,
            as_of=datetime(2026, 7, 22, 16),
            evaluated_at="2026-07-22T08:01:00.000000Z",
        )
        saved_evaluation = cache.save_advice_review_evaluation(evaluation)
        if cache is source_cache:
            source_evaluation = saved_evaluation
        cache.create_paper_strategy(
            plan,
            PaperStrategyCreate(
                plan_id=plan.id,
                expected_plan_revision=plan.revision,
                expected_plan_payload_digest=plan.plan_payload_digest,
                allocation_pct=10,
            ),
            activation_market_time="2026-07-16 10:00:00",
        )

    result = import_user_data(
        target,
        export_user_data(source),
        mode="merge",
        dry_run=False,
    )

    assert result.tables["advice_history"].remapped == 1
    assert result.tables["advice_review_plan"].remapped == 1
    assert result.tables["advice_review_result"].remapped == 1
    assert result.tables["paper_strategy"].remapped == 1
    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT plan.id AS plan_id, plan.advice_id, plan.plan_payload_digest,
                   ledger.payload_json, ledger.payload_digest,
                   result.plan_id AS result_plan_id,
                   result.advice_id AS result_advice_id,
                   result.plan_payload_digest AS result_plan_digest,
                   strategy.plan_id AS strategy_plan_id,
                   strategy.advice_id AS strategy_advice_id,
                   strategy.plan_payload_digest AS strategy_plan_digest
            FROM advice_history AS advice
            JOIN advice_review_plan AS plan ON plan.advice_id = advice.id
            JOIN advice_review_plan_revision AS ledger
              ON ledger.plan_id = plan.id AND ledger.revision = plan.revision
            JOIN advice_review_result AS result
              ON result.plan_id = plan.id AND result.plan_revision = plan.revision
            JOIN paper_strategy AS strategy
              ON strategy.plan_id = plan.id AND strategy.plan_revision = plan.revision
            WHERE advice.summary = 'source'
            """
        ).fetchone()
        assert row is not None
        result_row = conn.execute(
            "SELECT * FROM advice_review_result WHERE plan_id = ?",
            (row["plan_id"],),
        ).fetchone()
    assert result_row is not None
    payload = json.loads(str(row["payload_json"]))
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert row["plan_id"] != source_plan_id
    assert row["advice_id"] != source_advice
    assert payload["advice_id"] == row["advice_id"]
    assert digest == row["payload_digest"] == row["plan_payload_digest"]
    assert (row["result_plan_id"], row["result_advice_id"], row["result_plan_digest"]) == (
        row["plan_id"],
        row["advice_id"],
        digest,
    )
    assert (
        row["strategy_plan_id"],
        row["strategy_advice_id"],
        row["strategy_plan_digest"],
    ) == (row["plan_id"], row["advice_id"], digest)
    imported_detail = target_cache.advice_review_detail(int(row["plan_id"]))
    assert imported_detail is not None and imported_detail.latest_evaluation is not None
    assert source_evaluation is not None
    result_digest_fields = {"plan_payload_digest", "input_digest", "result_digest"}
    result_value_fields = {
        "status",
        "conclusion",
        "return_pct",
        "max_favorable_excursion_pct",
        "max_adverse_excursion_pct",
        "target_hit",
        "target_hit_date",
        "stop_hit",
        "stop_hit_date",
    }
    input_payload = {
        field: result_row[field]
        for field in advice_review_repository_module._RESULT_INSERT_FIELDS
        if field not in result_digest_fields | result_value_fields
    }
    assert result_row["input_digest"] == advice_review_repository_module._payload_digest(
        input_payload
    )
    assert result_row["input_digest"] != source_evaluation.input_digest
    assert result_row["evidence_contract_version"] == "advice-review-evidence.v2"
    imported_strategy = next(
        item for item in target_cache.paper_strategies() if item.symbol == "600519.SH"
    )
    assert imported_strategy.plan_payload_digest == digest


@pytest.mark.parametrize("table", ["advice_review_result", "paper_strategy"])
def test_import_rejects_result_or_paper_row_bound_to_different_plan_payload(
    tmp_path: Path,
    table: str,
) -> None:
    source = tmp_path / f"source-{table}.sqlite3"
    target = tmp_path / f"target-{table}.sqlite3"
    source_cache = SQLiteCache(source)
    SQLiteCache(target)
    advice_id = _insert_advice(source, "600519.SH", marker="source")
    plan_id = _insert_review_plan(source, advice_id, "600519.SH", marker="source")
    _insert_review_result(source, plan_id, advice_id, "600519.SH", marker="source")
    plan = source_cache.advice_review_plan(plan_id)
    assert plan is not None
    source_cache.create_paper_strategy(
        plan,
        PaperStrategyCreate(
            plan_id=plan.id,
            expected_plan_revision=plan.revision,
            expected_plan_payload_digest=plan.plan_payload_digest,
            allocation_pct=10,
        ),
        activation_market_time="2026-07-16 10:00:00",
    )
    payload = export_user_data(source).model_dump(mode="json")
    payload["tables"][table]["rows"][0]["target_price"] = 111.0

    with pytest.raises(ValueError, match=table):
        import_user_data(
            target,
            UserDataBundle.model_validate(payload),
            mode="merge",
            dry_run=False,
        )

    assert _table_count(target, "advice_history") == 0
    assert _table_count(target, "advice_review_plan") == 0
    assert _table_count(target, "advice_review_result") == 0
    assert _table_count(target, "paper_strategy") == 0


def test_v1_review_evidence_digest_imports_as_fail_closed_audit_history(
    tmp_path: Path,
) -> None:
    source = tmp_path / "review-v1-source.sqlite3"
    target = tmp_path / "review-v1-target.sqlite3"
    source_cache = SQLiteCache(source)
    target_cache = SQLiteCache(target)
    advice_id = _insert_advice(source, "600519.SH", marker="review-v1")
    plan_id = _insert_review_plan(source, advice_id, "600519.SH", marker="review-v1")
    plan = source_cache.advice_review_plan(plan_id)
    assert plan is not None
    source_cache.save_advice_review_evaluation(
        evaluate_advice_forward_window(
            plan,
            [
                _paper_bar("2026-07-16", 100, 102, 99, 101),
                _paper_bar("2026-07-17", 101, 104, 100, 103),
            ],
            as_of=datetime(2026, 7, 17, 16),
            evaluated_at="2026-07-17T08:01:00.000000Z",
        )
    )
    payload = export_user_data(source).model_dump(mode="json")
    row = payload["tables"]["advice_review_result"]["rows"][0]
    row["evidence_contract_version"] = "advice-review-evidence.v1"
    row["input_digest"] = portability_module._review_result_input_digest(row)

    import_user_data(
        target,
        UserDataBundle.model_validate(payload),
        mode="merge",
        dry_run=False,
    )

    with sqlite3.connect(target) as conn:
        stored = conn.execute(
            """
            SELECT evidence_contract_version, input_digest
            FROM advice_review_result
            """
        ).fetchone()
    assert stored == ("advice-review-evidence.v1", row["input_digest"])
    detail = target_cache.advice_review_detail(plan_id)
    assert detail is not None and detail.latest_evaluation is not None
    assert detail.latest_evaluation.status == "insufficient"
    assert detail.latest_evaluation.conclusion == "insufficient_data"


def _insert_watchlist(path: Path, symbol: str, *, note: str) -> None:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (
                symbol, code, market, name, note, group_name, pinned,
                research_status, priority, unread_change_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, '默认', 0, 'watching', 'medium', 0, ?, ?)
            """,
            (
                symbol,
                code,
                market,
                f"测试{code}",
                note,
                "2026-07-16T02:00:00.000000Z",
                "2026-07-16T02:00:00.000000Z",
            ),
        )


def _insert_alert_chain(path: Path, symbol: str, *, marker: str) -> tuple[int, int]:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        rule = conn.execute(
            """
            INSERT INTO alert_rule (
                symbol, code, market, stock_name, name, condition_type, threshold,
                note, enabled, last_state, trigger_count, cooldown_seconds, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'price_above', 101, ?, 1, '等待', 0, 300, ?, ?)
            """,
            (
                symbol,
                code,
                market,
                marker,
                marker,
                marker,
                "2026-07-16T02:00:00.000000Z",
                "2026-07-16T02:00:00.000000Z",
            ),
        )
        event = conn.execute(
            """
            INSERT INTO alert_event (
                rule_id, symbol, code, market, stock_name, name, condition_type,
                event_type, message, price, change_pct, threshold, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'price_above', '触发', ?, 102, 1, 101, ?)
            """,
            (
                int(rule.lastrowid),
                symbol,
                code,
                market,
                marker,
                marker,
                marker,
                "2026-07-16T02:01:00.000000Z",
            ),
        )
        return int(rule.lastrowid), int(event.lastrowid)


def _insert_stock_note(path: Path, symbol: str, *, marker: str) -> int:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO stock_note (
                symbol, code, market, name, note_type, content, visible, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'research', ?, 1, ?, ?)
            """,
            (
                symbol,
                code,
                market,
                marker,
                marker,
                "2026-07-16T02:00:00.000000Z",
                "2026-07-16T02:00:00.000000Z",
            ),
        )
        return int(cursor.lastrowid)


def _insert_advice(path: Path, symbol: str, *, marker: str) -> int:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO advice_history (
                symbol, code, market, name, action, confidence, trend_score,
                trend_label, risk_level, price, change_pct, support, resistance,
                data_quality_score, data_quality_level, reason, summary, created_at,
                market_time
            ) VALUES (?, ?, ?, ?, '等待信号', 60, 55, '中性观察', '可控风险',
                      100, 0, 95, 110, 90, '优秀', ?, ?, ?, ?)
            """,
            (
                symbol,
                code,
                market,
                marker,
                marker,
                marker,
                "2026-07-16T02:00:00.000000Z",
                "2026-07-16 09:59:00",
            ),
        )
        conn.execute(
            """
            UPDATE advice_history
            SET kline_adjustment_mode = 'qfq',
                kline_anchor_date = '2026-07-16',
                kline_anchor_close = 100,
                kline_data_version = 'portability-qfq-v1',
                kline_contract_version = ?
            WHERE id = ?
            """,
            (DAILY_KLINE_CONTRACT_VERSION, cursor.lastrowid),
        )
        return int(cursor.lastrowid)


def _insert_review_plan(
    path: Path,
    advice_id: int,
    symbol: str,
    *,
    marker: str,
) -> int:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            INSERT INTO advice_review_plan (
                advice_id, symbol, snapshot_market_time, snapshot_price,
                snapshot_adjustment_mode, snapshot_anchor_date, snapshot_anchor_close,
                snapshot_data_version, snapshot_contract_version,
                hypothesis, trigger_condition, invalidation_condition,
                target_price, stop_price, horizon_days, evidence_refs_json,
                revision, plan_payload_digest, created_at, updated_at
            ) VALUES (?, ?, '2026-07-16 09:59:00', 100,
                      'qfq', '2026-07-15', 100, 'portability-qfq-v1', ?,
                      ?, ?, ?, 110, 95, 5,
                      '[]', 1, ?, '2026-07-16T02:00:00.000000Z',
                      '2026-07-16T02:00:00.000000Z')
            """,
            (advice_id, symbol, DAILY_KLINE_CONTRACT_VERSION, marker, marker, marker, "b" * 64),
        )
        plan_id = int(cursor.lastrowid)
        _backfill_current_plan_revisions(conn)
        return plan_id


def _insert_review_result(
    path: Path,
    plan_id: int,
    advice_id: int,
    symbol: str,
    *,
    marker: str,
) -> int:
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO advice_review_result (
                plan_id, plan_revision, advice_id, symbol, snapshot_market_time,
                as_of, evaluated_at, status, conclusion, rule_version,
                snapshot_adjustment_mode, snapshot_anchor_date, snapshot_anchor_close,
                snapshot_data_version, snapshot_contract_version,
                entry_price, target_price, stop_price, horizon_days,
                visible_bar_count, available_forward_days, target_hit, stop_hit
            ) VALUES (
                ?, 1, ?, ?, '2026-07-16 09:59:00', '2026-07-17',
                '2026-07-17T08:00:00.000000Z', 'evaluated', 'horizon_gain', ?,
                'qfq', '2026-07-15', 100, 'portability-qfq-v1', ?,
                100, 110, 95, 5, 1, 1, 0, 0
            )
            """,
            (plan_id, advice_id, symbol, marker, DAILY_KLINE_CONTRACT_VERSION),
        )
        return int(cursor.lastrowid)


def _paper_bar(
    day: str,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> Kline:
    return Kline(
        date=day,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1_000,
        adjustment_mode="qfq",
        as_of=f"{day} 15:15:00",
        data_version="portability-paper-v1",
        contract_version=DAILY_KLINE_CONTRACT_VERSION,
        point_in_time=True,
        session_status="trading",
        open_execution_status="tradable",
        corporate_action_status="none",
        execution_metadata_version="factor-execution-evidence.v1",
    )


def _save_portable_paper_run(
    cache: SQLiteCache,
    path: Path,
    *,
    marker: str,
    symbol: str = "600519.SH",
) -> int:
    advice_id = _insert_advice(path, symbol, marker=marker)
    plan_id = _insert_review_plan(path, advice_id, symbol, marker=marker)
    plan = cache.advice_review_plan(plan_id)
    assert plan is not None
    cache.create_paper_strategy(
        plan,
        PaperStrategyCreate(
            plan_id=plan.id,
            expected_plan_revision=plan.revision,
            expected_plan_payload_digest=plan.plan_payload_digest,
            allocation_pct=10,
        ),
        activation_market_time="2026-07-16 10:00:00",
    )
    strategy = next(item for item in cache.paper_strategies() if item.plan_id == plan_id)
    draft = simulate_paper_portfolio(
        cache.paper_trading_account(),
        [strategy],
        {
            symbol: [
                _paper_bar("2026-07-16", 100, 101, 99, 100),
                _paper_bar("2026-07-17", 100, 105, 99, 104),
                _paper_bar("2026-07-20", 104, 112, 103, 111),
            ]
        },
        as_of=datetime(2026, 7, 20, 16),
    )
    dashboard = cache.save_paper_simulation(draft)
    assert dashboard.selected_run_id is not None
    return dashboard.selected_run_id


def _create_legacy_watchlist_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE watchlist (
                symbol TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT NOT NULL,
                note TEXT,
                group_name TEXT NOT NULL DEFAULT '默认',
                pinned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def _remove_bundle_columns(payload: dict, table: str, columns: set[str]) -> None:
    table_payload = payload["tables"][table]
    table_payload["columns"] = [column for column in table_payload["columns"] if column not in columns]
    for column in columns:
        table_payload["column_types"].pop(column)
    for row in table_payload["rows"]:
        for column in columns:
            row.pop(column)


def _watchlist_note(path: Path, symbol: str) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT note FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
    return row[0] if row else None


def _watchlist_symbols(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT symbol FROM watchlist ORDER BY symbol")]


def _table_columns(path: Path, table: str) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _table_count(path: Path, table: str) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _joined_alert_markers(path: Path) -> set[tuple[str, str]]:
    with sqlite3.connect(path) as conn:
        return {
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                """
                SELECT alert_rule.name, alert_event.message
                FROM alert_event
                JOIN alert_rule ON alert_rule.id = alert_event.rule_id
                """
            )
        }


def _joined_review_markers(path: Path) -> set[tuple[str, str, str]]:
    with sqlite3.connect(path) as conn:
        return {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute(
                """
                SELECT advice_history.summary, advice_review_plan.hypothesis,
                       advice_review_result.rule_version
                FROM advice_review_result
                JOIN advice_review_plan ON advice_review_plan.id = advice_review_result.plan_id
                JOIN advice_history
                  ON advice_history.id = advice_review_plan.advice_id
                 AND advice_history.id = advice_review_result.advice_id
                """
            )
        }


def _stock_note_markers(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {str(row[0]) for row in conn.execute("SELECT content FROM stock_note")}
