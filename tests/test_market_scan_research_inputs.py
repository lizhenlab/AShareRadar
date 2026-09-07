from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from app.db.market_scan_integrity import seal_market_scan_snapshot
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE
from app.services.market_scan_research_inputs import (
    ResearchInputConfig, audit_research_inputs, read_research_session, readonly_research_connection,
)
from tests.test_market_scan_evaluation import _disable_market_scan_immutability, _initialize, _seed_run as _seed_legacy_run


def _seed_run(path: Path, **kwargs: object) -> int:
    run_id = _seed_legacy_run(path, **kwargs)  # type: ignore[arg-type]
    with sqlite3.connect(path) as conn:
        _disable_market_scan_immutability(conn)
        conn.execute("UPDATE market_scan_run SET scope = ?, snapshot_digest = NULL, snapshot_seal_origin = NULL, snapshot_sealed_at = NULL WHERE id = ?", (MARKET_SCAN_FULL_MARKET_SCOPE, run_id))
        seal_market_scan_snapshot(conn, run_id)
    return run_id


def test_missing_database_is_blocked_and_never_created(tmp_path: Path) -> None:
    path = tmp_path / "absent.sqlite3"
    config = ResearchInputConfig(as_of_date="2026-09-04")
    report = audit_research_inputs(path, config)
    assert report["status"] == "blocked"
    assert report["reason"] == "database_read_unavailable"
    assert report["returns_inspected"] is False
    assert report == audit_research_inputs(path, config)
    assert not path.exists()


@pytest.mark.parametrize("kwargs", [
    {"as_of_date": "bad"}, {"as_of_date": "2026-09-04", "horizon": 0},
    {"as_of_date": "2026-09-04", "max_runs": 0},
    {"as_of_date": "2026-09-04", "run_ids": (1, 1)},
    {"as_of_date": "2026-09-04", "run_ids": (0,)},
])
def test_selection_config_rejects_ambiguous_input(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ResearchInputConfig(**kwargs)  # type: ignore[arg-type]


def test_readonly_session_preserves_frozen_order_and_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "research.sqlite3"
    _initialize(path)
    run_id = _seed_run(path, mode="official", rule_version="v5", quote_date="2026-08-03", ranks=("600002.SH", "600001.SH"))
    before = path.read_bytes()
    with readonly_research_connection(path) as conn:
        package = read_research_session(conn, run_id)
        assert [item["symbol"] for item in package["items"]] == ["600002.SH", "600001.SH"]
        assert set(package) == {"run", "items"}
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM market_scan_result")
    assert path.read_bytes() == before


def test_reader_requires_query_only_connection(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "new.sqlite3") as conn:
        with pytest.raises(ValueError, match="query_only"):
            read_research_session(conn, 1)


def test_audit_uses_fixed_calendar_and_does_not_replace_failed_pit_batch(tmp_path: Path) -> None:
    path = tmp_path / "research.sqlite3"
    _initialize(path)
    _seed_run(path, mode="official", rule_version="v5", quote_date="2026-08-03", ranks=("600001.SH",))
    selected = _seed_run(path, mode="official", rule_version="v5", quote_date="2026-08-04", ranks=("600001.SH",), point_in_time=False)
    _seed_run(path, mode="official", rule_version="v5", quote_date="2026-08-12", ranks=("600001.SH",))
    report = audit_research_inputs(path, ResearchInputConfig(as_of_date="2026-08-12", max_runs=1))
    batch = report["batches"][0]
    assert batch["run_id"] == selected
    assert batch["target_trading_dates"][-1] == "2026-08-12"
    assert batch["snapshot_prepared"] is False
    assert batch["reasons"] == ["point_in_time_evidence_incomplete"]
    assert batch["target_price_coverage"]["status"] == "not_inspected"


def test_valid_pit_batch_has_digest_but_no_execution_claim(tmp_path: Path) -> None:
    path = tmp_path / "research.sqlite3"
    _initialize(path)
    run_id = _seed_run(path, mode="official", rule_version="v5", quote_date="2026-08-03", ranks=("600001.SH",))
    report = audit_research_inputs(path, ResearchInputConfig(as_of_date="2026-09-04", run_ids=(run_id,)))
    batch = report["batches"][0]
    assert batch["snapshot_prepared"] is True
    assert batch["pit_status_counts"] == {"verified": 1}
    assert len(batch["member_ordering_digest"]) == 64
    assert report["execution_evidence"]["qfq_cache_is_execution_evidence"] is False
    assert report["v6_activation_status"].startswith("unknown")


def test_requested_missing_and_immature_batches_remain_explicit(tmp_path: Path) -> None:
    path = tmp_path / "research.sqlite3"
    _initialize(path)
    run_id = _seed_run(path, mode="official", rule_version="v5", quote_date="2026-08-03", ranks=("600001.SH",))
    report = audit_research_inputs(path, ResearchInputConfig(as_of_date="2026-08-04", run_ids=(run_id, 999)))
    assert report["missing_requested_run_ids"] == [999]
    assert report["status"] == "blocked"
    assert "target_dates_not_mature" in report["batches"][0]["reasons"]


def test_missing_member_is_preserved_and_blocks_snapshot_preparation(tmp_path: Path) -> None:
    path = tmp_path / "research.sqlite3"
    _initialize(path)
    run_id = _seed_run(path, mode="official", rule_version="v5", quote_date="2026-08-03", ranks=("600001.SH",))
    with sqlite3.connect(path) as conn:
        _disable_market_scan_immutability(conn)
        conn.execute("UPDATE market_scan_run SET status = 'degraded', total_count = 2, processed_count = 2, missing_count = 1, snapshot_digest = NULL, snapshot_seal_origin = NULL, snapshot_sealed_at = NULL WHERE id = ?", (run_id,))
        conn.execute("INSERT INTO market_scan_result(run_id,symbol,code,market,name,status,updated_at) VALUES(?,'600002.SH','600002','SH','missing','missing','2026-08-03T08:00:00Z')", (run_id,))
        seal_market_scan_snapshot(conn, run_id)
    report = audit_research_inputs(path, ResearchInputConfig(as_of_date="2026-09-04", run_ids=(run_id,)))
    assert report["batches"][0]["reasons"] == ["missing_frozen_members_block_shared_account_research"]
    assert report["batches"][0]["snapshot_prepared"] is False
    assert len(report["batches"][0]["member_ordering"]) == 2


def test_audit_cli_retains_blocked_report_and_cannot_overwrite_database(tmp_path, monkeypatch, capsys):
    import json
    from tools import audit_market_scan_research as cli
    missing = tmp_path / "missing.sqlite3"
    args = ["audit", "--database", str(missing), "--as-of-date", "2026-09-04"]
    monkeypatch.setattr("sys.argv", args)
    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    output = tmp_path / "audit.json"
    monkeypatch.setattr("sys.argv", [*args, "--output", str(output)])
    assert cli.main() == 2
    assert json.loads(output.read_text())["reason"] == "database_read_unavailable"
    before = output.read_bytes()
    assert cli.main() == 2  # Identical immutable output is idempotent.
    assert output.read_bytes() == before
    monkeypatch.setattr("sys.argv", [*args, "--output", str(missing)])
    with pytest.raises(SystemExit):
        cli.main()
    assert not missing.exists()
