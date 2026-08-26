from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, time

import pytest
from pydantic import ValidationError

import app.models.market_scan_execution_quote as execution_quote_model
import app.models.market_scan_execution_session as execution_session_model
from app.models.market import Quote
from app.models.market_scan import MarketScanResultItem
from app.services.market_scan_execution_quote import (
    MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY,
    build_market_scan_execution_quote_evidence,
    market_scan_execution_quote_evidence_digest,
    verify_market_scan_execution_quote_evidence,
)
from app.services.market_scan_execution_session import (
    build_market_scan_execution_session_evidence,
    market_scan_execution_session_content_digest,
    verify_market_scan_execution_session_evidence,
)
from app.services.market_scan_scoring import score_market_scan_item
from tests.test_market_scan_scoring import DATA_DATE, _rows


def _item() -> MarketScanResultItem:
    return MarketScanResultItem(
        run_id=1,
        symbol="600519.SH",
        code="600519",
        market="SH",
        name="贵州茅台",
        industry="白酒",
        list_date="2001-08-27",
        is_st=False,
        metadata_source="provider-full-pool",
        status="pending",
        updated_at="2026-08-21T07:00:00Z",
    )


def _quote(*, fallback_used: bool = False) -> Quote:
    return Quote(
        code="600519",
        name="贵州茅台",
        market="SH",
        price=10.3,
        prev_close=10.0,
        open=10.0,
        high=10.4,
        low=9.9,
        volume=1_000_000.0,
        amount=20_000_000.0,
        change=0.3,
        change_pct=3.0,
        turnover_rate=1.2,
        timestamp="2026-08-21T15:00:00+08:00",
        source="腾讯行情",
        fallback_used=fallback_used,
    )


def test_execution_quote_is_unadjusted_digest_bound_but_does_not_claim_exchange_authority() -> None:
    evidence = build_market_scan_execution_quote_evidence(
        _item(),
        _quote(),
        mode="official",
        quote_date="2026-08-21",
        captured_at="2026-08-21T15:00:02+08:00",
    )

    assert evidence["source_authority"] == "vendor_normalized_quote"
    assert evidence["bar"]["adjustment_mode"] == "none"
    assert evidence["bar"]["session_status"] == "trading"
    assert evidence["bar"]["amount"] == 20_000_000.0
    assert evidence["bar"]["corporate_action_status"] == "unknown"
    assert evidence["evidence_digest"] == market_scan_execution_quote_evidence_digest(evidence)
    assert verify_market_scan_execution_quote_evidence(evidence) == evidence


@pytest.mark.parametrize(
    "path",
    ["bar", "instrument", "normalized_quote_digest", "evidence_digest"],
)
def test_execution_quote_rejects_every_tampered_binding_layer(path: str) -> None:
    evidence = build_market_scan_execution_quote_evidence(
        _item(),
        _quote(),
        mode="official",
        quote_date="2026-08-21",
        captured_at="2026-08-21T15:00:02+08:00",
    )
    tampered = deepcopy(evidence)
    if path == "bar":
        tampered["bar"]["amount"] = 1.0
    elif path == "instrument":
        tampered["instrument"]["is_st"] = True
    else:
        tampered[path] = "a" * 64

    with pytest.raises(ValidationError):
        verify_market_scan_execution_quote_evidence(tampered)


def test_scored_result_persists_execution_quote_inside_snapshot_bound_score_details() -> None:
    rows = _rows(DATA_DATE, 80)
    quote = _quote().model_copy(
        update={
            "price": 10.5,
            "high": 10.8,
            "change": 0.5,
            "change_pct": 5.0,
            "timestamp": f"{DATA_DATE.isoformat()}T15:00:00+08:00",
        }
    )
    item = _item().model_copy(update={"updated_at": f"{DATA_DATE.isoformat()}T16:00:00+08:00"})
    result = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=datetime.combine(
            DATA_DATE,
            time(16, 30),
        ),
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        expected_quote_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        mode="official",
        quote_observed_at=f"{DATA_DATE.isoformat()}T15:00:02+08:00",
    )

    evidence = result.score_details[MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY]
    assert verify_market_scan_execution_quote_evidence(evidence)["symbol"] == item.symbol


def test_capture_rejects_event_observed_after_claimed_capture_time() -> None:
    with pytest.raises(ValidationError, match="before provider event"):
        build_market_scan_execution_quote_evidence(
            _item(),
            _quote(),
            mode="official",
            quote_date="2026-08-21",
            captured_at="2026-08-21T14:59:59+08:00",
        )


def test_execution_session_binds_complete_result_set_and_preserves_vendor_limitations() -> None:
    rows = _rows(DATA_DATE, 80)
    quote = _quote().model_copy(
        update={
            "price": 10.5,
            "high": 10.8,
            "change": 0.5,
            "change_pct": 5.0,
            "timestamp": f"{DATA_DATE.isoformat()}T15:00:00+08:00",
        }
    )
    item = _item().model_copy(
        update={"updated_at": f"{DATA_DATE.isoformat()}T16:00:00+08:00"}
    )
    scored = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=datetime.combine(DATA_DATE, time(16, 30)),
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        expected_quote_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        mode="official",
        quote_observed_at=f"{DATA_DATE.isoformat()}T15:00:02+08:00",
    )
    success = {
        **item.model_dump(mode="json"),
        **asdict(scored),
        "status": "success",
        "quote_observed_at": f"{DATA_DATE.isoformat()}T15:00:02+08:00",
        "updated_at": f"{DATA_DATE.isoformat()}T16:30:01+08:00",
    }
    skipped = {
        **_item().model_copy(
            update={
                "symbol": "000001.SZ",
                "code": "000001",
                "market": "SZ",
                "status": "skipped",
            }
        ).model_dump(mode="json"),
        "score_details": {},
        "updated_at": f"{DATA_DATE.isoformat()}T16:30:01+08:00",
    }
    run = {
        "id": 1,
        "mode": "official",
        "quote_date": DATA_DATE.isoformat(),
        "as_of": f"{DATA_DATE.isoformat()}T16:30:00+08:00",
        "total_count": 2,
        "success_count": 1,
        "missing_count": 0,
        "skipped_count": 1,
        "snapshot_digest": "a" * 64,
    }

    session = build_market_scan_execution_session_evidence(
        run,
        [skipped, success],
        canonical_published=True,
    )

    assert session["complete_result_set"] is True
    assert session["source_snapshot_binding"] == "verified_digest"
    assert session["quote_evidence_count"] == 1
    assert session["quote_evidence_coverage"] == 0.5
    assert session["formal_equivalent_pit"] is False
    assert session["limitations"] == [
        "vendor_quote_not_exchange_authoritative",
        "corporate_action_evidence_unavailable",
        "quote_evidence_incomplete",
    ]
    assert [row["symbol"] for row in session["rows"]] == ["000001.SZ", "600519.SH"]
    assert session["evidence_digest"] == market_scan_execution_session_content_digest(
        session
    )
    assert verify_market_scan_execution_session_evidence(session) == session


def test_execution_session_rejects_resealed_outer_quote_conflict() -> None:
    evidence = build_market_scan_execution_quote_evidence(
        _item(),
        _quote(),
        mode="official",
        quote_date="2026-08-21",
        captured_at="2026-08-21T15:00:02+08:00",
    )
    result = {
        **_item().model_copy(update={"status": "success"}).model_dump(mode="json"),
        "price": 10.3,
        "amount": 20_000_000.0,
        "quote_timestamp": "2026-08-21T15:00:00+08:00",
        "quote_observed_at": "2026-08-21T15:00:02+08:00",
        "quote_source": "腾讯行情",
        "score_details": {MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY: evidence},
        "updated_at": "2026-08-21T16:00:01+08:00",
    }
    session = build_market_scan_execution_session_evidence(
        {
            "id": 1,
            "mode": "official",
            "quote_date": "2026-08-21",
            "as_of": "2026-08-21T16:00:00+08:00",
            "total_count": 1,
            "success_count": 1,
            "missing_count": 0,
            "skipped_count": 0,
            "snapshot_digest": "b" * 64,
        },
        [result],
        canonical_published=True,
    )
    tampered = deepcopy(session)
    tampered["rows"][0]["outer_quote_price"] = 9.9
    tampered["rows"][0]["row_digest"] = market_scan_execution_session_content_digest(
        tampered["rows"][0]
    )
    tampered["row_set_digest"] = probability_row_set_digest(tampered["rows"])
    tampered["evidence_digest"] = market_scan_execution_session_content_digest(tampered)

    with pytest.raises(ValidationError, match="outer price"):
        verify_market_scan_execution_session_evidence(tampered)


def probability_row_set_digest(rows: list[dict[str, object]]) -> str:
    from app.artifacts.io import canonical_json_bytes, sha256_hex

    return sha256_hex(canonical_json_bytes(rows))


def _minimal_execution_session() -> dict[str, object]:
    skipped = {
        **_item().model_copy(update={"status": "skipped"}).model_dump(mode="json"),
        "score_details": {},
        "updated_at": "2026-08-21T16:00:01+08:00",
    }
    return build_market_scan_execution_session_evidence(
        {
            "id": 1,
            "mode": "official",
            "quote_date": "2026-08-21",
            "as_of": "2026-08-21T16:00:00+08:00",
            "total_count": 1,
            "success_count": 0,
            "missing_count": 0,
            "skipped_count": 1,
            "snapshot_digest": "a" * 64,
        },
        [skipped],
        canonical_published=True,
    )


def test_execution_quote_bar_and_instrument_validators_cover_all_market_states() -> None:
    evidence = build_market_scan_execution_quote_evidence(
        _item(),
        _quote(),
        mode="official",
        quote_date="2026-08-21",
        captured_at="2026-08-21T15:00:02+08:00",
    )
    bar = evidence["bar"]
    for message, values in (
        ("positive OHLC", {"amount": 0.0}),
        ("high conflicts", {"high": 9.0}),
        ("low conflicts", {"low": 11.0, "high": 12.0}),
    ):
        with pytest.raises(ValidationError, match=message):
            execution_quote_model.MarketScanExecutionQuoteBar.model_validate(
                {**bar, **values}
            )
    suspended = {
        **bar,
        "session_status": "suspended",
        "volume": 0.0,
        "amount": 0.0,
        "buy_open_state": "unavailable",
        "sell_open_state": "unavailable",
    }
    assert (
        execution_quote_model.MarketScanExecutionQuoteBar.model_validate(suspended)
        .session_status
        == "suspended"
    )
    with pytest.raises(ValidationError, match="zero volume"):
        execution_quote_model.MarketScanExecutionQuoteBar.model_validate(
            {**suspended, "volume": 1.0}
        )
    with pytest.raises(ValidationError, match="both sides unavailable"):
        execution_quote_model.MarketScanExecutionQuoteBar.model_validate(
            {**suspended, "buy_open_state": "executable"}
        )
    instrument = evidence["instrument"]
    with pytest.raises(ValidationError, match="future list date"):
        execution_quote_model.MarketScanExecutionInstrumentState.model_validate(
            {**instrument, "list_date": "2026-08-22"}
        )


def test_execution_quote_evidence_identity_dates_and_helpers_fail_closed() -> None:
    evidence = build_market_scan_execution_quote_evidence(
        _item(),
        _quote(),
        mode="official",
        quote_date="2026-08-21",
        captured_at="2026-08-21T15:00:02+08:00",
    )
    mutations = (
        ("identity mismatch", {**evidence, "symbol": "000001.SZ"}),
        (
            "event date mismatch",
            {**evidence, "provider_event_at": "2026-08-20T15:00:00+08:00"},
        ),
        (
            "metadata is not effective",
            {
                **evidence,
                "instrument": {
                    **evidence["instrument"],
                    "metadata_effective_date": "2026-08-20",
                },
            },
        ),
    )
    for message, candidate in mutations:
        with pytest.raises(ValidationError, match=message):
            execution_quote_model.MarketScanExecutionQuoteEvidence.model_validate(
                candidate
            )
    with pytest.raises(ValueError, match="must be an object"):
        execution_quote_model.verify_market_scan_execution_quote_evidence([])
    with pytest.raises(TypeError, match="model or mapping"):
        execution_quote_model.market_scan_execution_quote_evidence_digest(object())
    with pytest.raises(ValueError, match="ISO date"):
        execution_quote_model._date("bad", "date")
    with pytest.raises(ValueError, match="ISO timestamp"):
        execution_quote_model._timestamp("bad", "time")
    with pytest.raises(ValueError, match="timezone"):
        execution_quote_model._timestamp("2026-08-21T15:00:00", "time")


def test_execution_session_row_counts_summaries_and_boundaries_fail_closed() -> None:
    value = _minimal_execution_session()
    session = execution_session_model.MarketScanExecutionSessionEvidence.model_validate(
        value
    )
    row = session.rows[0]
    with pytest.raises(ValueError, match="symbol/code/market"):
        execution_session_model.MarketScanExecutionSessionRow.model_validate(
            {
                **row.model_dump(mode="json"),
                "code": "000001",
            }
        )
    with pytest.raises(ValueError, match="lacks persisted quote"):
        execution_session_model.MarketScanExecutionSessionRow.model_validate(
            {
                **row.model_dump(mode="json"),
                "result_status": "success",
            }
        )
    with pytest.raises(ValueError, match="row digest mismatch"):
        execution_session_model.MarketScanExecutionSessionRow.model_validate(
            {**row.model_dump(mode="json"), "row_digest": "f" * 64}
        )

    for message, changed in (
        ("expected status counts", {"expected_success_count": 1}),
        ("observed status counts", {"observed_result_count": 2}),
        ("unique canonical", {"rows": [row, row], "observed_result_count": 2, "observed_skipped_count": 2}),
        ("row statuses", {"observed_skipped_count": 0, "observed_missing_count": 1}),
        ("completeness flag", {"complete_result_set": False}),
    ):
        candidate = session.model_copy(update=changed)
        with pytest.raises(ValueError, match=message):
            execution_session_model._validate_session_counts(candidate)
    with pytest.raises(ValueError, match="quote evidence count"):
        execution_session_model._validate_session_evidence_summaries(
            session.model_copy(update={"quote_evidence_count": 1}),
            [],
        )
    with pytest.raises(ValueError, match="coverage mismatch"):
        execution_session_model._validate_session_evidence_summaries(
            session.model_copy(update={"quote_evidence_coverage": 1.0}),
            [],
        )

    quote = execution_quote_model.MarketScanExecutionQuoteEvidence.model_validate(
        build_market_scan_execution_quote_evidence(
            _item(),
            _quote(),
            mode="official",
            quote_date="2026-08-21",
            captured_at="2026-08-21T15:00:02+08:00",
        )
    )
    with pytest.raises(ValueError, match="summaries cannot be replayed"):
        execution_session_model._validate_session_evidence_summaries(
            session.model_copy(
                update={
                    "quote_evidence_count": 1,
                    "quote_evidence_coverage": 1.0,
                }
            ),
            [quote],
        )
    with pytest.raises(ValueError, match="published run boundary"):
        execution_session_model._validate_session_boundaries(
            session,
            [quote.model_copy(update={"mode": "intraday"})],
            datetime.fromisoformat("2026-08-21T00:00:00+08:00").date(),
            datetime.fromisoformat("2026-08-21T16:00:00+08:00"),
        )


def test_execution_session_build_and_scalar_helpers_reject_ambiguous_inputs() -> None:
    with pytest.raises(ValueError, match="canonical_published"):
        build_market_scan_execution_session_evidence({}, [], canonical_published=False)
    invalid_runs = (
        ({"id": 0}, "positive integer"),
        ({"id": 1, "mode": "bad"}, "mode is invalid"),
        ({"id": 1, "mode": "official", "quote_date": "bad"}, "ISO date"),
    )
    for raw, message in invalid_runs:
        with pytest.raises(ValueError, match=message):
            execution_session_model._session_run_facts(raw)
    with pytest.raises(ValueError, match="run_id mismatch"):
        execution_session_model._result_identity(
            {"run_id": 2, "symbol": "600519.SH", "status": "skipped"},
            1,
        )
    with pytest.raises(ValueError, match="status is invalid"):
        execution_session_model._result_identity(
            {"symbol": "600519.SH", "status": "pending"},
            1,
        )
    for value in (True, 0, "1"):
        with pytest.raises(ValueError, match="positive integer"):
            execution_session_model._positive_integer(value, "value")
    for value in (True, -1, "0"):
        with pytest.raises(ValueError, match="nonnegative integer"):
            execution_session_model._nonnegative_integer(value, "value")
    assert execution_session_model._optional_sha256(None) is None
    with pytest.raises(ValueError, match="sha256"):
        execution_session_model._optional_sha256("bad")
    with pytest.raises(ValueError, match="numeric outer field"):
        execution_session_model._optional_float(True)
    assert execution_session_model._optional_text("  ") is None
    assert execution_session_model._optional_aware_timestamp_text(None, "time") is None
    with pytest.raises(ValueError, match="ISO timestamp"):
        execution_session_model._aware_timestamp_text("bad", "time")
    with pytest.raises(ValueError, match="timezone"):
        execution_session_model._aware_timestamp("2026-08-21T15:00:00", "time")
    with pytest.raises(ValueError, match="mapping or Pydantic"):
        execution_session_model._object_mapping(object(), "value")
    with pytest.raises(ValueError, match="must be an object"):
        execution_session_model.verify_market_scan_execution_session_evidence([])
    with pytest.raises(TypeError, match="model or mapping"):
        execution_session_model.market_scan_execution_session_content_digest(object())
    assert execution_session_model._limitations(
        snapshot_bound=False,
        complete_result_set=False,
        quote_evidence_count=0,
        expected_result_count=1,
    ) == [
        "vendor_quote_not_exchange_authoritative",
        "corporate_action_evidence_unavailable",
        "source_snapshot_digest_unavailable",
        "incomplete_result_set",
        "quote_evidence_incomplete",
    ]
