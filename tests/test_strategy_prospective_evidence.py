"""Research capture retains frozen requirements and never invents official facts."""

from dataclasses import replace
from datetime import datetime
import json
import os

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services import strategy_prospective_archive as archive_module
from app.services import strategy_prospective_evidence as evidence
from app.services.strategy_template_tracking_metrics import (
    STRATEGY_TEMPLATE_TRACKING_IDS, StrategyTrackingPosition, StrategyTrackingSelection, StrategyTrackingSession,
)
from app.services.trading_calendar import ASHARE_TIMEZONE, TradingCalendarCoverageError
from tests import test_fuyao_dumps_sync as dump_fixtures
from tests.test_fuyao_dumps_validation import action_row, daily_row


AS_OF = datetime(2026, 9, 7, 16, tzinfo=ASHARE_TIMEZONE)
OBSERVED = datetime(2026, 9, 4, 16, tzinfo=ASHARE_TIMEZONE)
bundle = dump_fixtures.bundle


def _session(*, symbols=("600519.SH",), status="ready", day="2026-09-01", run_id=1):
    positions = tuple(StrategyTrackingPosition(symbol, "fixture", None, .1, 10.0) for symbol in symbols)
    selections = tuple(StrategyTrackingSelection(
        template_id, status, 10, len(positions), positions, len(positions) * .1, 900_000.0, 100.0,
        "c" * 64, "d" * 64, "missing" if positions else "no_selection", None,
    ) for template_id in STRATEGY_TEMPLATE_TRACKING_IDS)
    return StrategyTrackingSession(run_id, day, None, "fixture-rule", "a" * 64, "b" * 64,
                                   day + "T16:00:00+08:00", selections)


@pytest.fixture
def source(tmp_path, request, monkeypatch):
    monkeypatch.setattr("app.services.fuyao_dumps.market_now", lambda: OBSERVED)
    rows = [daily_row(day) for day in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04")]
    manifest, _client = request.getfixturevalue("bundle")(rows)
    return tmp_path / "history", manifest


def _requirements(session=None, horizon=1):
    return evidence.build_strategy_execution_requirements([session or _session()], horizon=horizon)


def _collect(source, requirements=None, as_of=AS_OF):
    return evidence.collect_strategy_execution_research(requirements or _requirements(), source[0], as_of=as_of)


def test_requirements_freeze_all_three_templates_union_and_full_d_through_h_plus_one():
    requirements = _requirements(_session(symbols=("600519.SH", "000001.SZ")))
    assert requirements["status"] == "ready"
    assert (requirements["signal_count"], requirements["symbol_count"], requirements["pair_count"]) == (1, 2, 6)
    assert len(requirements["signals"][0]["selections"]) == 3
    assert {row["session_date"] for row in requirements["symbol_sessions"]} == {"2026-09-01", "2026-09-02", "2026-09-03"}
    frozen = _session()
    changed = replace(frozen, selections=tuple(replace(item, weighted_gross_return=999, outcome_status="available") for item in frozen.selections))
    assert _requirements(frozen) == _requirements(changed)  # Future labels cannot select evidence requests.


def test_requirements_keep_different_source_contracts_and_deduplicate_shared_pairs():
    one = _session()
    two = replace(one, run_id=2, rule_version="other-rule", score_spec_hash="e" * 64)
    requirements = evidence.build_strategy_execution_requirements([two, one], horizon=1)
    assert requirements["signal_count"] == 2 and requirements["pair_count"] == 3
    assert [row["run_id"] for row in requirements["signals"]] == [1, 2]
    with pytest.raises(ValueError, match="duplicate"):
        evidence.build_strategy_execution_requirements([one, one], horizon=1)


def test_no_candidates_have_structured_needs_source_with_zero_symbols(tmp_path):
    requirements = _requirements(_session(symbols=(), status="no_trade"))
    assert requirements["status"] == "needs_source" and requirements["symbol_count"] == requirements["pair_count"] == 0
    root = tmp_path / "not-created"
    report = evidence.collect_strategy_execution_research(requirements, root, as_of=AS_OF)
    assert report["status"] == "needs_source" and report["symbol_count"] == 0
    assert report["data_rows"] == [] and not root.exists()


def test_calendar_unavailable_blocks_without_weekday_fallback(monkeypatch):
    def unavailable(*args, **kwargs):
        assert kwargs["allow_auto_refresh"] is False
        raise TradingCalendarCoverageError("fixture")
    monkeypatch.setattr(evidence, "next_trade_dates", unavailable)
    requirements = _requirements()
    assert requirements["status"] == "blocked"
    assert requirements["symbols"] == ["600519.SH"] and requirements["pair_count"] == 0
    assert requirements["blocked_reasons"] == ["calendar_unavailable"]
    report = evidence.collect_strategy_execution_research(requirements, None, as_of=AS_OF)
    assert report["status"] == "blocked"


def test_readonly_capture_binds_verified_source_and_retains_unknown_official_fields(source):
    before = {path.relative_to(source[0]): path.read_bytes() for path in source[0].rglob("*") if path.is_file()}
    report = _collect(source)
    assert report["status"] == "research_only" and report["collected_pair_count"] == 3
    assert report["source"] == "public_vendor_research"
    assert report["official_execution_admitted"] is report["point_in_time_verified"] is False
    assert report["provider_calls"] == report["runtime_writes"] == 0
    assert report["archive_observed_at"] == OBSERVED.isoformat()
    assert report["collected_at"] != report["archive_observed_at"]
    assert report["source_manifest"]["version"] == source[1].version
    assert len(report["source_manifest"]["source_sha256"]) == 2
    assert report["corporate_action_coverage"] == "unknown"
    assert "entry_execution_state" in report["official_field_gaps"]
    assert all(row["values"]["adjusted"] == "none" and row["source_record_digest"] for row in report["data_rows"])
    after = {path.relative_to(source[0]): path.read_bytes() for path in source[0].rglob("*") if path.is_file()}
    assert before == after
    assert report["artifact_digest"] == sha256_hex(canonical_json_bytes({k: v for k, v in report.items() if k != "artifact_digest"}))


def test_missing_stock_is_not_removed_replaced_or_inferred_suspended(source):
    requirements = _requirements(_session(symbols=("600519.SH", "000001.SZ")))
    report = _collect(source, requirements)
    assert report["status"] == "partial_research_only"
    assert report["required_pair_count"] == 6 and report["missing_pair_count"] == report["collected_pair_count"] == 3
    assert {row["symbol"] for row in report["missing_pairs"]} == {"000001.SZ"}
    assert requirements["symbols"] == ["000001.SZ", "600519.SH"]
    assert "absent_bars_are_not_proof_of_suspension" in report["limitations"]


def test_future_pairs_remain_pending_and_archive_after_cutoff_is_unavailable(source):
    report = _collect(source, _requirements(horizon=5))
    assert report["pending_pair_count"] == 2 and report["missing_pair_count"] == 1
    earlier = _collect(source, as_of=datetime(2026, 9, 2, 16, tzinfo=ASHARE_TIMEZONE))
    assert earlier["source_unavailable_reason"] == "archive_not_available_at_as_of"
    assert earlier["collected_pair_count"] == 0 and earlier["pending_pair_count"] == 1


def test_same_day_action_rows_remain_separate_without_no_event_claim(tmp_path, request, monkeypatch):
    monkeypatch.setattr("app.services.fuyao_dumps.market_now", lambda: OBSERVED)
    actions = [action_row(), action_row(dividend_per_share=0, per_share_bonus=-.5)]
    manifest, _client = request.getfixturevalue("bundle")([daily_row(day) for day in ("2026-09-01", "2026-09-02", "2026-09-03")], actions=actions)
    report = _collect((tmp_path / "history", manifest))
    assert report["corporate_action_record_count"] == 2
    assert {row["values"]["per_share_bonus"] for row in report["corporate_actions"]} == {0, -.5}
    assert report["corporate_action_coverage"] == "unknown"
    assert len({row["source_record_digest"] for row in report["corporate_actions"]}) == 2


def test_full_hash_verification_is_cached_but_same_size_timestamp_restored_tampering_invalidates(source, monkeypatch):
    from app.services import fuyao_dumps_storage
    original = fuyao_dumps_storage.verify_dump_files
    calls = []
    def verify(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(fuyao_dumps_storage, "verify_dump_files", verify)
    assert _collect(source)["collected_pair_count"] == 3
    assert _collect(source)["collected_pair_count"] == 3
    assert len(calls) == 1
    path = source[0] / "versions" / source[1].version / "source-daily.parquet"
    before = path.stat()
    raw = path.read_bytes()
    path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    report = _collect(source)
    assert len(calls) == 2 and report["source_unavailable_reason"] == "history_archive_verification_failed"
    assert report["collected_pair_count"] == 0 and report["missing_pair_count"] == 3


def test_source_mutation_during_extract_discards_all_rows(source, monkeypatch):
    original = archive_module._extract_rows
    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        path = source[0] / "versions" / source[1].version / "source-actions.parquet"
        path.write_bytes(path.read_bytes() + b"changed")
        return result
    monkeypatch.setattr(archive_module, "_extract_rows", mutate)
    report = _collect(source)
    assert report["source_unavailable_reason"] == "history_archive_verification_failed"
    assert report["data_rows"] == report["corporate_actions"] == []


def test_requirement_membership_is_rebuilt_even_if_caller_rehashes_tampered_projection(source):
    requirements = _requirements()
    requirements["symbol_sessions"] = requirements["symbol_sessions"][:1]
    requirements["pair_count"] = 1
    requirements["requirements_digest"] = sha256_hex(canonical_json_bytes({k: v for k, v in requirements.items() if k != "requirements_digest"}))
    with pytest.raises(ValueError, match="membership mismatch"):
        _collect(source, requirements)


@pytest.mark.parametrize("horizon", [True, 0, 2, 21, 1.0])
def test_invalid_horizons_are_rejected(horizon):
    with pytest.raises(ValueError):
        _requirements(horizon=horizon)


@pytest.mark.parametrize("weight", [True, 0, float("nan"), float("inf"), 10 ** 400])
def test_invalid_frozen_numeric_identity_is_rejected(weight):
    source = _session()
    selection = source.selections[0]
    changed = replace(selection, positions=(replace(selection.positions[0], target_weight=weight),))
    with pytest.raises(ValueError):
        _requirements(replace(source, selections=(changed, *source.selections[1:])))


def test_invalid_asof_and_symlink_sources_fail_without_creation(source, tmp_path):
    with pytest.raises(ValueError):
        _collect(source, as_of=AS_OF.replace(tzinfo=None))
    with pytest.raises(ValueError):
        _collect(source, as_of=datetime(2100, 1, 1, tzinfo=ASHARE_TIMEZONE))
    alias = tmp_path / "alias"
    alias.symlink_to(source[0], target_is_directory=True)
    assert _collect((alias, source[1]))["source_unavailable_reason"] == "history_archive_verification_failed"
    missing = tmp_path / "missing"
    assert _collect((missing, None))["source_unavailable_reason"] == "history_archive_unavailable"
    assert not missing.exists()


def test_complete_requirement_digest_survives_json_roundtrip(source):
    requirements = json.loads(json.dumps(_requirements()))
    assert _collect(source, requirements)["collected_pair_count"] == 3


def test_pair_limit_rejects_instead_of_truncating(monkeypatch):
    monkeypatch.setattr(evidence, "_MAX_PAIRS", 2)
    with pytest.raises(ValueError, match="200000"):
        _requirements()


def test_predicate_cross_product_does_not_add_unrequested_symbol_dates(tmp_path, request, monkeypatch):
    monkeypatch.setattr("app.services.fuyao_dumps.market_now", lambda: OBSERVED)
    symbols = ("600519.SH", "000001.SZ")
    days = ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04")
    manifest, _client = request.getfixturevalue("bundle")([daily_row(day, thscode=symbol) for day in days for symbol in symbols])
    requirements = evidence.build_strategy_execution_requirements([
        _session(symbols=(symbols[0],)), _session(symbols=(symbols[1],), day="2026-09-02", run_id=2),
    ], horizon=1)
    report = _collect((tmp_path / "history", manifest), requirements)
    actual = {(row["symbol"], row["session_date"]) for row in report["data_rows"]}
    expected = {(row["symbol"], row["session_date"]) for row in requirements["symbol_sessions"]}
    assert actual == expected and len(actual) == 6
    assert (symbols[0], days[-1]) not in actual and (symbols[1], days[0]) not in actual


@pytest.mark.parametrize("change", [
    {"signals": [None]}, {"signals": [{"run_id": []}]},
    {"signals": [{"run_id": 1, "signal_date": None, "published_at": None}]},
])
def test_malformed_requirement_objects_are_rejected_as_validation_errors(change):
    requirements = {**_requirements(), **change}
    with pytest.raises(ValueError):
        evidence.collect_strategy_execution_research(requirements, None, as_of=AS_OF)
