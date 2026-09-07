from __future__ import annotations

from copy import deepcopy

import pytest

from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE
from app.services.market_scan_research_availability import (
    AvailabilityPlan, summarize_availability_snapshots,
)


DATES = ("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07")


def plan(**overrides):
    return AvailabilityPlan(**{
        "trading_dates": DATES, "selection_policy": "latest-published-by-as-of-and-id",
        "selection_cutoff": "2026-08-08T00:00:00+08:00", **overrides,
    })


def snapshot(day=DATES[0], run_id=1, statuses=("success", "missing", "skipped")):
    stamp = day + "T16:00:00+08:00"
    items = [{
        "run_id": run_id, "symbol": f"60000{index}.SH", "code": f"60000{index}", "market": "SH",
        "name": "fixture", "industry": "industry-a", "status": status, "updated_at": stamp,
        "rank": index if status == "success" else None,
    } for index, status in enumerate(statuses, 1)]
    return {"run": {
        "id": run_id, "status": "degraded", "trigger": "manual", "mode": "official",
        "rule_version": "fixture-v5", "as_of": stamp, "data_date": day, "quote_date": day,
        "scope": MARKET_SCAN_FULL_MARKET_SCOPE, "total_count": len(items), "excluded_count": 0,
        "processed_count": len(items) - statuses.count("pending"), "success_count": statuses.count("success"),
        "missing_count": statuses.count("missing"), "skipped_count": statuses.count("skipped"), "retry_count": 0,
        "progress_pct": 100, "coverage_pct": round(100 * statuses.count("success") / max(1, len(items) - statuses.count("skipped")), 2),
        "created_at": stamp, "updated_at": stamp,
        "finished_at": stamp, "snapshot_digest": "a" * 64, "snapshot_seal_origin": "publication", "snapshot_sealed_at": stamp,
    }, "items": items}


def test_calendar_placeholders_denominators_and_synthetic_provenance():
    report = summarize_availability_snapshots(plan(), [snapshot()])
    assert [day["data_date"] for day in report["days"]] == list(DATES)
    first = report["days"][0]
    assert first["member_count"] == 3
    assert first["status_counts"] == {"success": 1, "missing": 1, "pending": 0, "skipped": 1}
    assert first["observed_member_fraction"] == pytest.approx(1 / 3)
    assert first["source_status"] == "synthetic_unverified"
    assert first["predecision_differences"]["status"] == "insufficient_evidence"
    assert report["days"][1]["member_count"] is None
    assert report["days"][1]["status_counts"] is None
    assert report["summary"]["calendar_session_count"] == 5
    assert report["returns_inspected"] is False and report["promotion_eligible"] is False


def test_contiguous_missing_does_not_bridge_absent_batch_or_membership():
    packages = [snapshot(DATES[index], index + 1) for index in (0, 1, 3, 4)]
    report = summarize_availability_snapshots(plan(), packages)
    streaks = [item for item in report["missing_streaks"] if item["symbol"] == "600002.SH"]
    assert [(item["start_date"], item["end_date"], item["session_count"]) for item in streaks] == [
        (DATES[0], DATES[1], 2), (DATES[3], DATES[4], 2),
    ]
    assert report["days"][2]["reason"] == "no_selected_batch"


def test_groups_preserve_missing_sources_and_error_category_is_fixed():
    package = snapshot()
    package["items"][0].update(metadata_source="pool", quote_source="quote", kline_source="bars")
    package["items"][1]["error"] = "provider timeout accessing https://private.example/?token=SECRET"
    report = summarize_availability_snapshots(plan(), [package])
    groups = report["days"][0]["groups"]
    assert sum(row["member_count"] for row in groups["quote_source"]) == 3
    unknown = next(row for row in groups["quote_source"] if row["value"] == "UNKNOWN")
    assert unknown["status_counts"]["missing"] == 1
    assert "timeout" in [row["value"] for row in groups["error_category"]]
    assert "SECRET" not in str(report)


def test_stale_missing_and_skipped_members_keep_their_frozen_denominator():
    package = snapshot(DATES[1])
    for item in package["items"]:
        item["data_date"] = DATES[1] if item["status"] == "success" else DATES[0]
    day = summarize_availability_snapshots(plan(), [package])["days"][1]
    assert day["member_count"] == 3
    assert day["status_counts"] == {"success": 1, "missing": 1, "pending": 0, "skipped": 1}
    assert day["strict_member_admission"] == "blocked"


@pytest.mark.parametrize("member_index", [1, 2])
def test_unobserved_member_future_date_is_still_rejected(member_index):
    package = snapshot()
    package["items"][member_index]["data_date"] = DATES[1]
    with pytest.raises(ValueError, match="data_date"):
        summarize_availability_snapshots(plan(), [package])


@pytest.mark.parametrize("value", ["20260803", "2026-00-03"])
def test_unobserved_member_date_must_remain_a_canonical_calendar_date(value):
    package = snapshot(DATES[1])
    package["items"][1]["data_date"] = value
    with pytest.raises(ValueError):
        summarize_availability_snapshots(plan(), [package])


@pytest.mark.parametrize("mutation", ["duplicate", "foreign_run", "symbol_conflict", "date_conflict", "rank_conflict", "count", "status_count"])
def test_rejects_conflicting_complete_membership(mutation):
    package = snapshot()
    if mutation == "duplicate":
        package["items"][1] = deepcopy(package["items"][0])
    elif mutation == "foreign_run":
        package["items"][1]["run_id"] = 99
    elif mutation == "symbol_conflict":
        package["items"][1]["market"] = "SZ"
    elif mutation == "date_conflict":
        package["items"][0]["data_date"] = DATES[1]
    elif mutation == "rank_conflict":
        package["items"][0]["rank"] = 2
    elif mutation == "count":
        package["items"].pop()
    else:
        package["run"]["missing_count"] = 0
    with pytest.raises(ValueError):
        summarize_availability_snapshots(plan(), [package])


@pytest.mark.parametrize("overrides", [
    {"trading_dates": (DATES[0], DATES[2])}, {"trading_dates": (DATES[1], DATES[0])},
    {"trading_dates": ("2026-08-08",)}, {"trading_dates": ()},
    {"selection_cutoff": "2026-08-08"}, {"selection_cutoff": "2026-08-02T00:00:00Z"},
    {"selection_policy": "latest-published-by-as-of-and-id; DROP TABLE market_scan_run"},
    {"run_ids": (1,)}, {"selection_policy": "explicit-run-ids", "run_ids": ()},
    {"selection_policy": "explicit-run-ids", "run_ids": (1, 1)},
    {"selection_policy": "explicit-run-ids", "run_ids": (True,)},
])
def test_plan_rejects_incomplete_or_ambiguous_selection(overrides):
    with pytest.raises(ValueError):
        plan(**overrides)


def test_same_day_selection_is_not_replaced_after_reading_member_failures():
    first, second = snapshot(run_id=1), snapshot(run_id=2, statuses=("missing",))
    report = summarize_availability_snapshots(plan(), [second, first])
    assert report["days"][0]["run_id"] == 2
    assert report["days"][0]["status_counts"]["missing"] == 1
    explicit = plan(selection_policy="explicit-run-ids", run_ids=(1, 2))
    with pytest.raises(ValueError, match="one.*date"):
        summarize_availability_snapshots(explicit, [first, second])


def test_order_does_not_change_report_and_unselected_members_are_not_inspected():
    first, second = snapshot(run_id=1), snapshot(run_id=2)
    first["items"] = [{"unselected": "must not inspect"}]
    report = summarize_availability_snapshots(plan(), [first, second])
    assert report == summarize_availability_snapshots(plan(), [second, first])


def test_explicit_missing_run_remains_reported_without_substitution():
    declared = plan(selection_policy="explicit-run-ids", run_ids=(99,))
    report = summarize_availability_snapshots(declared, [snapshot()])
    assert report["missing_requested_run_ids"] == [99]
    assert all(day["member_count"] is None for day in report["days"])


def test_unavailable_explicit_id_blocks_complete_calendar_from_claiming_complete_audit():
    declared = plan(trading_dates=(DATES[0],), selection_policy="explicit-run-ids", run_ids=(1, 99))
    report = summarize_availability_snapshots(declared, [snapshot()])
    assert report["summary"]["member_observed_session_count"] == 1
    assert report["missing_requested_run_ids"] == [99]
    assert report["status"] == "incomplete"


def test_cutoff_and_tie_break_use_actual_instants_instead_of_timestamp_text():
    first, second = snapshot(run_id=1), snapshot(run_id=2)
    second["run"]["as_of"] = DATES[0] + "T08:00:00Z"
    report = summarize_availability_snapshots(plan(), [second, first])
    assert report["days"][0]["run_id"] == 2
    second["run"]["snapshot_sealed_at"] = "2026-08-08T00:00:00.000001+08:00"
    report = summarize_availability_snapshots(plan(), [second, first])
    assert report["days"][0]["run_id"] == 1


def test_explicit_synthetic_pending_members_remain_counted_and_unverified():
    package = snapshot(statuses=("success", "pending", "skipped"))
    package["run"].update(status="running", progress_pct=66.67, snapshot_digest=None,
                          snapshot_seal_origin=None, snapshot_sealed_at=None, finished_at=None)
    report = summarize_availability_snapshots(plan(selection_policy="explicit-run-ids", run_ids=(1,)), [package])
    day = report["days"][0]
    assert day["status_counts"] == {"success": 1, "pending": 1, "missing": 0, "skipped": 1}
    assert day["source_status"] == "synthetic_unverified"
    assert day["strict_member_admission"] == "blocked"


def test_declared_calendar_cannot_be_overridden_with_extra_returns_or_plan_fields():
    from dataclasses import asdict
    from app.services.market_scan_research_availability_contract import AVAILABILITY_PLAN_VERSION, availability_plan_from_payload
    payload = {"schema_version": AVAILABILITY_PLAN_VERSION, **asdict(plan()), "future_returns": [1.0]}
    with pytest.raises(ValueError, match="exact declared schema"):
        availability_plan_from_payload(payload)


def test_predecision_differences_require_complete_evidence_in_both_status_populations():
    from dataclasses import replace
    from app.services.market_scan_research_availability_contract import AvailabilityMember
    from app.services.market_scan_research_availability_statistics import availability_predecision_differences
    observed = AvailabilityMember("600001.SH", "success", "SH", "A", "source", "source", "source", "none", 300.0)
    unknown = replace(observed, symbol="600002.SH", status="missing", predecision_amount=None)
    incomplete = availability_predecision_differences([observed, unknown])
    assert incomplete["means"] == {"observed": None, "unobserved": None}
    assert incomplete["evidence_counts"] == {"observed": 1, "unobserved": 0}
    complete = availability_predecision_differences([observed, replace(unknown, predecision_amount=100.0)])
    assert complete["observed_minus_unobserved"] == 200.0
    assert complete["status"] == "descriptive_only"


@pytest.mark.parametrize("change", ["none", "payload", "late", "identity", "unobserved"])
def test_predecision_features_require_actual_context_bound_and_timely_evidence(change):
    from app.services.market_scan_scoring import score_market_scan_item
    from tests.test_market_scan_scoring import AS_OF, DATA_DATE, _item, _persisted_item, _quote, _rows
    result = score_market_scan_item(_item(), _quote(), _rows(DATA_DATE, 80), as_of=AS_OF,
                                   completed_cutoff=DATA_DATE, expected_data_date=DATA_DATE, min_history_rows=60, min_data_quality_score=0)
    item = _persisted_item(result).model_dump(mode="json")
    package = snapshot(DATA_DATE.isoformat(), statuses=("success",))
    for key in ("as_of", "created_at", "updated_at", "finished_at", "snapshot_sealed_at"):
        package["run"][key] = AS_OF.isoformat() + "+08:00"
    if change == "payload":
        item["score_details"]["components"]["score_dimensions"]["point_in_time_evidence"]["payload"]["quote_amount"] += 1
    elif change == "late":
        item["quote_observed_at"] = "2026-07-17T17:00:00+08:00"
    elif change == "identity":
        item.update(symbol="600518.SH", code="600518")
    elif change == "unobserved":
        item["quote_observed_at"] = None
    package["items"] = [item]
    day = summarize_availability_snapshots(plan(trading_dates=(DATA_DATE.isoformat(),)), [package])["days"][0]
    assert day["predecision_differences"]["evidence_counts"]["observed"] == int(change == "none")
    assert day["predecision_differences"]["observed_minus_unobserved"] is None


@pytest.mark.parametrize("error, expected", [("429 quota", "rate_limit"), ("OHLC invalid", "invalid_data"),
                                              ("data missing", "unavailable"), ("other detail", "unclassified"), (None, "not_recorded")])
def test_error_categories_are_stable_and_do_not_publish_raw_error(error, expected):
    package = snapshot()
    package["items"][1]["error"] = error
    day = summarize_availability_snapshots(plan(), [package])["days"][0]
    selected = next(group for group in day["groups"]["error_category"] if group["status_counts"]["missing"])
    assert selected["value"] == expected


@pytest.mark.parametrize("change", ["mode", "date", "scope"])
def test_unselected_run_identity_never_changes_calendar_membership(change):
    package = snapshot()
    package["run"][{"mode": "mode", "date": "data_date", "scope": "scope"}[change]] = {
        "mode": "intraday", "date": "2026-08-10", "scope": "test subset",
    }[change]
    report = summarize_availability_snapshots(plan(), [package])
    assert report["summary"]["selected_session_count"] == 0
    assert report["summary"]["known_member_observation_count"] == 0
