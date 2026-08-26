from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

import app.services.market_scan_official_execution as official_module
import app.services.market_scan_official_execution_store as official_store_module
import tools.ingest_market_scan_official_execution as official_intake_cli
from app.services.market_scan_official_execution import (
    BoundOfficialExecutionDecisionSession,
    OfficialExecutionIntakeError,
    OfficialExecutionInstrumentRules,
    OfficialExecutionSessionArtifact,
    VerifiedOfficialExecutionSession,
    VerifiedOfficialExecutionSourceRegistry,
    bind_official_execution_session_to_decisions,
    load_verified_official_execution_session,
    load_verified_official_execution_source_registry,
    seal_official_execution_session_artifact,
    seal_official_execution_instrument_rules,
    seal_official_execution_source_registry,
)
from app.services.market_scan_official_execution_store import (
    MarketScanOfficialExecutionStore,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _registry() -> dict[str, object]:
    return seal_official_execution_source_registry(
        [
            {
                "source_id": "licensed-exchange-feed",
                "dataset_id": "daily-ohlcv-state-v1",
                "provider_legal_name": "Licensed Exchange Data Provider",
                "markets": ["BJ", "SH", "SZ"],
                "authority_basis": "exchange_direct_subscription",
                "delivery_channel": "https",
                "source_base_uri": "https://licensed.example.test/daily",
                "license_reference": "LIC-RESEARCH-001",
                "license_document_sha256": "a" * 64,
                "valid_from": "2026-01-01",
                "valid_through": "2026-12-31",
                "research_use_authorized": True,
            }
        ],
        registered_at="2026-08-01T09:00:00+08:00",
    )


def _receipt(raw: bytes, relative_path: str = "2026/08/25/sh.raw") -> dict[str, object]:
    return {
        "source_id": "licensed-exchange-feed",
        "dataset_id": "daily-ohlcv-state-v1",
        "market": "SH",
        "session_date": "2026-08-25",
        "relative_path": relative_path,
        "byte_size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source_uri": "https://licensed.example.test/daily/2026/08/25/sh.raw",
        "available_at": "2026-08-25T15:05:00+08:00",
        "acquired_at": "2026-08-25T15:06:00+08:00",
        "parser_version": "official-daily-normalizer-v1",
        "license_reference": "LIC-RESEARCH-001",
    }


def _row(receipt_digest: str) -> dict[str, object]:
    return {
        "symbol": "600519.SH",
        "code": "600519",
        "market": "SH",
        "session_date": "2026-08-25",
        "observed_at": "2026-08-25T15:05:30+08:00",
        "source_id": "licensed-exchange-feed",
        "dataset_id": "daily-ohlcv-state-v1",
        "receipt_digest": receipt_digest,
        "source_record_id": "SH:20260825:600519",
        "exchange_session_state": "trading",
        "entry_execution_state": "executable",
        "entry_reason_code": "official_open_executable",
        "exit_execution_state": "executable",
        "exit_reason_code": "official_close_executable",
        "instrument_rules": {
            "effective_date": "2026-08-25",
            "board": "main",
            "is_st": False,
            "listing_status": "listed",
            "board_rule_id": "sse-main-20260825",
            "st_rule_id": "sse-st-20260825",
            "delisting_rule_id": "sse-listing-20260825",
            "minimum_buy_quantity": 100,
            "buy_quantity_step": 100,
            "sell_quantity_step": 1,
            "price_limit_pct": 0.10,
            "ruleset_digest": "b" * 64,
        },
        "corporate_action": {
            "status": "none",
            "event_id": None,
            "previous_close": 1500.0,
            "reference_price": 1500.0,
            "reference_price_rule_id": "sse-reference-price-20260825",
        },
        "bar": {
            "adjustment_mode": "none",
            "open": 1501.0,
            "high": 1520.0,
            "low": 1490.0,
            "close": 1510.0,
            "volume": 1_000_000.0,
            "amount": 1_500_000_000.0,
        },
    }


def _artifacts(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    raw = b"licensed exchange delivery bytes\n"
    raw_root = tmp_path / "raw"
    raw_path = raw_root / "2026" / "08" / "25" / "sh.raw"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(raw)
    registry = _registry()
    registry_path = tmp_path / "registry.json"
    _write_json(registry_path, registry)
    receipt = _receipt(raw)
    from app.services.market_scan_official_execution import (
        seal_official_execution_raw_file_receipt,
    )

    sealed_receipt = seal_official_execution_raw_file_receipt(receipt)
    artifact = seal_official_execution_session_artifact(
        session_date="2026-08-25",
        generated_at="2026-08-25T15:07:00+08:00",
        source_registry_digest=str(registry["registry_digest"]),
        receipts=[receipt],
        rows=[_row(str(sealed_receipt["receipt_digest"]))],
    )
    artifact_path = tmp_path / "session.json"
    _write_json(artifact_path, artifact)
    return registry_path, artifact_path, raw_root, registry


def test_official_execution_intake_requires_pin_raw_bytes_and_exact_decision_coverage(
    tmp_path: Path,
) -> None:
    registry_path, artifact_path, raw_root, registry_value = _artifacts(tmp_path)
    registry = load_verified_official_execution_source_registry(
        registry_path,
        expected_registry_digest=str(registry_value["registry_digest"]),
    )
    session = load_verified_official_execution_session(
        artifact_path,
        registry=registry,
        raw_file_root=raw_root,
    )
    bound = bind_official_execution_session_to_decisions(
        session,
        expected_symbols=["600519.SH"],
        source_snapshot_digest="c" * 64,
    )

    assert isinstance(registry, VerifiedOfficialExecutionSourceRegistry)
    assert isinstance(session, VerifiedOfficialExecutionSession)
    assert isinstance(bound, BoundOfficialExecutionDecisionSession)
    assert session.session_date == "2026-08-25"
    assert session.raw_file_set_digest != session.artifact_digest
    assert bound.source_snapshot_digest == "c" * 64
    bound_row = bound.row_by_symbol()["600519.SH"]
    bound_bar = cast(dict[str, object], bound_row["bar"])
    bound_action = cast(dict[str, object], bound_row["corporate_action"])
    assert bound_bar["adjustment_mode"] == "none"
    assert bound_action["status"] == "none"


def test_official_instrument_rules_require_exact_content_digest() -> None:
    rules = cast(dict[str, object], _row("a" * 64)["instrument_rules"])
    sealed = seal_official_execution_instrument_rules(rules)

    assert sealed["ruleset_digest"] != "b" * 64
    assert OfficialExecutionInstrumentRules.model_validate(sealed).price_limit_pct == 0.10

    tampered = deepcopy(sealed)
    tampered["price_limit_pct"] = 0.20
    with pytest.raises(ValidationError, match="ruleset digest mismatch"):
        OfficialExecutionInstrumentRules.model_validate(tampered)


def test_official_execution_intake_cli_verifies_then_installs_atomically(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry_path, artifact_path, raw_root, registry = _artifacts(tmp_path)
    session_directory = tmp_path / "managed" / "sessions"
    common = [
        "--registry-path",
        str(registry_path),
        "--registry-digest",
        str(registry["registry_digest"]),
        "--raw-root",
        str(raw_root),
        "--session-directory",
        str(session_directory),
    ]

    assert official_intake_cli.main([*common, "verify", "--candidate", str(artifact_path)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "ready"
    assert verified["session_date"] == "2026-08-25"
    assert verified["store_status"]["status"] == "waiting_sessions"
    assert not session_directory.exists()

    assert official_intake_cli.main([*common, "ingest", "--candidate", str(artifact_path)]) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["status"] == "ready"
    assert installed["store_status"]["verified_session_count"] == 1
    assert Path(installed["managed_target"]).is_file()


def test_official_execution_intake_cli_exposes_non_authorizing_contract_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert official_intake_cli.main(["contract"]) == 0

    emitted = capsys.readouterr()
    payload = json.loads(emitted.out)
    registry_schema = cast(dict[str, object], payload["registry_json_schema"])
    session_schema = cast(dict[str, object], payload["session_json_schema"])

    assert emitted.err == ""
    assert payload["schema_version"] == "market-scan-official-execution-contract-schema-v1"
    assert payload["authorizing"] is False
    assert payload["required_markets"] == ["SH", "SZ", "BJ"]
    assert payload["required_session_path"] == [
        "D",
        "D+1",
        "D+2",
        "D+3",
        "D+4",
        "D+5",
        "D+6",
    ]
    assert registry_schema["title"] == "OfficialExecutionSourceRegistry"
    assert session_schema["title"] == "OfficialExecutionSessionArtifact"
    assert "independently pinned" in str(payload["integrity_notice"])


def test_official_execution_intake_cli_requires_store_arguments_for_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert official_intake_cli.main(["status"]) == 2

    emitted = capsys.readouterr()
    failure = json.loads(emitted.err)
    assert emitted.out == ""
    assert failure["operation"] == "status"
    assert failure["status"] == "failed"
    assert "--registry-path" in failure["error"]
    assert "--registry-digest" in failure["error"]
    assert "--raw-root" in failure["error"]
    assert "--session-directory" in failure["error"]


def test_official_execution_intake_cli_fails_before_installing_tampered_raw_bytes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry_path, artifact_path, raw_root, registry = _artifacts(tmp_path)
    session_directory = tmp_path / "managed" / "sessions"
    (raw_root / "2026" / "08" / "25" / "sh.raw").write_bytes(b"tampered")

    result = official_intake_cli.main(
        [
            "--registry-path",
            str(registry_path),
            "--registry-digest",
            str(registry["registry_digest"]),
            "--raw-root",
            str(raw_root),
            "--session-directory",
            str(session_directory),
            "ingest",
            "--candidate",
            str(artifact_path),
        ]
    )

    assert result == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure["status"] == "failed"
    assert "bytes" in failure["error"]
    assert not session_directory.exists()


def test_official_registry_receipt_action_and_bar_validators_fail_closed() -> None:
    registry = _registry()
    registration = cast(list[dict[str, object]], registry["registrations"])[0]
    for message, values in (
        ("validity is inverted", {"valid_through": "2025-12-31"}),
        ("sorted and unique", {"markets": ["SZ", "SH", "BJ"]}),
        ("credential-free", {"source_base_uri": "https://user:pass@licensed.example.test/daily"}),
        ("digest mismatch", {"registration_digest": "f" * 64}),
    ):
        with pytest.raises(ValidationError, match=message):
            official_module.OfficialExecutionSourceRegistration.model_validate(
                {**registration, **values}
            )
    duplicate_registry = {
        **registry,
        "registrations": [registration, registration],
    }
    duplicate_registry["registry_digest"] = official_module.official_execution_content_digest(
        duplicate_registry,
        "registry_digest",
    )
    with pytest.raises(ValidationError, match="canonical and unique"):
        official_module.OfficialExecutionSourceRegistry.model_validate(duplicate_registry)

    raw = b"official\n"
    receipt = official_module.seal_official_execution_raw_file_receipt(_receipt(raw))
    for message, values in (
        ("acquired before", {"acquired_at": "2026-08-25T15:04:00+08:00"}),
        ("after its session close", {"available_at": "2026-08-25T15:00:00+08:00"}),
        ("safe relative", {"relative_path": "../outside.raw"}),
        ("must not persist credentials", {"source_uri": "https://user:pass@licensed.example.test/a"}),
        ("receipt digest mismatch", {"receipt_digest": "f" * 64}),
    ):
        candidate = {**receipt, **values}
        if "digest" not in message:
            candidate["receipt_digest"] = official_module.official_execution_content_digest(
                candidate,
                "receipt_digest",
            )
        with pytest.raises((ValidationError, ValueError), match=message):
            official_module.OfficialExecutionRawFileReceipt.model_validate(candidate)

    action = official_module.seal_official_execution_corporate_action_reference(
        {
            "status": "none",
            "event_id": None,
            "previous_close": 10.0,
            "reference_price": 10.0,
            "reference_price_rule_id": "rule-v1",
        }
    )
    with pytest.raises(ValidationError, match="event identity"):
        official_module.OfficialExecutionCorporateActionReference.model_validate(
            {
                **action,
                "event_id": "unexpected",
                "evidence_digest": official_module.official_execution_content_digest(
                    {**action, "event_id": "unexpected"},
                    "evidence_digest",
                ),
            }
        )
    with pytest.raises(ValidationError, match="reference digest"):
        official_module.OfficialExecutionCorporateActionReference.model_validate(
            {**action, "evidence_digest": "f" * 64}
        )
    assert official_module.OfficialExecutionDailyBar.model_validate({}).open is None
    with pytest.raises(ValidationError, match="wholly present"):
        official_module.OfficialExecutionDailyBar.model_validate({"open": 10.0})
    with pytest.raises(ValidationError, match="OHLC bounds"):
        official_module.OfficialExecutionDailyBar.model_validate(
            {
                "open": 10.0,
                "high": 9.0,
                "low": 11.0,
                "close": 10.0,
                "volume": 1.0,
                "amount": 1.0,
            }
        )


def test_official_row_and_artifact_private_invariants_cover_all_states(tmp_path: Path) -> None:
    _registry_path, artifact_path, _raw_root, _registry_value = _artifacts(tmp_path)
    artifact_value = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact = official_module.OfficialExecutionSessionArtifact.model_validate(artifact_value)
    row = artifact.rows[0]
    for message, changed in (
        ("symbol/code/market", {"code": "000001"}),
        (
                "rules are not effective",
                {
                    "instrument_rules": official_module.seal_official_execution_instrument_rules(
                        {
                            **row.instrument_rules.model_dump(mode="json"),
                            "effective_date": "2026-08-24",
                        }
                    )
                },
        ),
        ("observed after close", {"observed_at": "2026-08-25T15:00:00+08:00"}),
    ):
        with pytest.raises(ValueError, match=message):
            official_module.OfficialExecutionSessionRow.model_validate(
                {**row.model_dump(mode="json"), **changed}
            )
    with pytest.raises(ValidationError, match="trading-state digest"):
        official_module.OfficialExecutionSessionRow.model_validate(
            {**row.model_dump(mode="json"), "trading_state_digest": "f" * 64}
        )
    with pytest.raises(ValidationError, match="row digest"):
        official_module.OfficialExecutionSessionRow.model_validate(
            {**row.model_dump(mode="json"), "row_digest": "f" * 64}
        )

    official_module._validate_trading_execution_state(row)
    with pytest.raises(ValueError, match="positive volume"):
        official_module._validate_trading_execution_state(
            row.model_copy(update={"bar": row.bar.model_copy(update={"volume": 0.0})})
        )
    with pytest.raises(ValueError, match="incompatible execution state"):
        official_module._validate_trading_execution_state(
            row.model_copy(update={"entry_execution_state": "suspended"})
        )
    blank_bar = official_module.OfficialExecutionDailyBar.model_validate({})
    with pytest.raises(ValueError, match="unadjusted bar"):
        official_module._validate_trading_execution_state(
            row.model_copy(update={"bar": blank_bar})
        )
    suspended = row.model_copy(
        update={
            "exchange_session_state": "suspended",
            "entry_execution_state": "suspended",
            "exit_execution_state": "suspended",
            "bar": blank_bar,
        }
    )
    official_module._validate_nontrading_execution_state(suspended)
    with pytest.raises(ValueError, match="must not synthesize"):
        official_module._validate_nontrading_execution_state(
            suspended.model_copy(update={"bar": row.bar})
        )
    with pytest.raises(ValueError, match="both execution sides"):
        official_module._validate_nontrading_execution_state(
            suspended.model_copy(update={"entry_execution_state": "executable"})
        )
    with pytest.raises(ValueError, match="rule-ineligible"):
        official_module._validate_nontrading_execution_state(
            suspended.model_copy(
                update={
                    "exchange_session_state": "not_listed",
                    "entry_execution_state": "suspended",
                }
            )
        )

    for message, changed in (
        ("expected market counts are incomplete", {"expected_market_counts": {"SH": 1}}),
        ("do not conserve", {"expected_row_count": 2}),
        ("unique canonical symbols", {"rows": [row, row], "expected_row_count": 2, "expected_market_counts": {"SH": 2, "SZ": 0, "BJ": 0}}),
        ("row count is incomplete", {"rows": []}),
        ("per-market row counts", {"expected_market_counts": {"SH": 0, "SZ": 1, "BJ": 0}}),
    ):
        candidate = artifact.model_copy(update=changed)
        with pytest.raises(ValueError, match=message):
            official_module._validate_artifact_counts(candidate)
    duplicate_receipts = artifact.model_copy(update={"receipts": [artifact.receipts[0], artifact.receipts[0]]})
    with pytest.raises(ValueError, match="canonical and unique"):
        official_module._validate_artifact_receipts(duplicate_receipts)
    with pytest.raises(ValueError, match="raw receipt"):
        official_module._validate_artifact_row_bindings(
            artifact,
            {},
            datetime.fromisoformat(artifact.generated_at),
        )
    with pytest.raises(ValueError, match="predates a row"):
        official_module._validate_artifact_row_bindings(
            artifact,
            {artifact.receipts[0].receipt_digest: artifact.receipts[0]},
            datetime.fromisoformat("2026-08-25T15:05:00+08:00"),
        )


def test_official_helpers_reject_unsafe_uri_path_time_and_binding_inputs(tmp_path: Path) -> None:
    for value in ("../a.raw", "/a.raw", "a\\b.raw"):
        with pytest.raises(ValueError, match="path"):
            official_module._relative_path(value)
    for value in ("http://example.test/a", "https://user:pass@example.test/a"):
        with pytest.raises(ValueError, match="source URI"):
            official_module._source_uri(value, "https", "source")
    with pytest.raises(ValueError, match="supported source URI"):
        official_module._any_source_uri("file:///tmp/a", "source")
    with pytest.raises(ValueError, match="persist credentials"):
        official_module._any_source_uri("sftp://user:pass@example.test/a", "source")
    with pytest.raises(ValueError, match="ISO date"):
        official_module._iso_date("bad", "date")
    with pytest.raises(ValueError, match="ISO timestamp"):
        official_module._aware_timestamp("bad", "time")
    with pytest.raises(ValueError, match="timezone"):
        official_module._aware_timestamp("2026-08-25T15:00:00", "time")
    with pytest.raises(TypeError, match="model or mapping"):
        official_module.official_execution_content_digest(object(), "digest")
    assert not official_module._valid_symbol(123)
    assert not official_module._is_sha256(True)

    registry = official_module.OfficialExecutionSourceRegistration.model_validate(
        cast(list[dict[str, object]], _registry()["registrations"])[0]
    )
    receipt = official_module.OfficialExecutionRawFileReceipt.model_validate(
        official_module.seal_official_execution_raw_file_receipt(
            _receipt(b"x")
        )
    )
    assert official_module._uri_within_registration(
        receipt.source_uri,
        registry.source_base_uri,
    )
    assert not official_module._uri_within_registration(
        "https://other.example.test/a",
        registry.source_base_uri,
    )
    with pytest.raises(official_module.OfficialExecutionIntakeError, match="conflicts"):
        official_module._verify_receipt_registration(
            receipt.model_copy(update={"license_reference": "other-license"}),
            registry,
        )
    with pytest.raises(official_module.OfficialExecutionIntakeError, match="unavailable"):
        official_module._trusted_raw_path(tmp_path / "missing", "a.raw")


def test_registry_cannot_self_assert_authority_or_construct_opaque_tokens(tmp_path: Path) -> None:
    registry_path, _artifact_path, _raw_root, registry = _artifacts(tmp_path)
    with pytest.raises(OfficialExecutionIntakeError, match="pin"):
        load_verified_official_execution_source_registry(
            registry_path,
            expected_registry_digest="",
        )
    with pytest.raises(OfficialExecutionIntakeError, match="operator pin"):
        load_verified_official_execution_source_registry(
            registry_path,
            expected_registry_digest="f" * 64,
        )
    with pytest.raises(TypeError, match="strict loader"):
        VerifiedOfficialExecutionSourceRegistry("{}", str(registry["registry_digest"]))
    with pytest.raises(TypeError, match="strict loader"):
        VerifiedOfficialExecutionSession(
            "{}",
            artifact_digest="a" * 64,
            session_date="2026-08-25",
            raw_file_set_digest="b" * 64,
        )


def test_raw_file_tamper_and_symlink_fail_closed(tmp_path: Path) -> None:
    registry_path, artifact_path, raw_root, registry_value = _artifacts(tmp_path)
    registry = load_verified_official_execution_source_registry(
        registry_path,
        expected_registry_digest=str(registry_value["registry_digest"]),
    )
    raw_path = raw_root / "2026" / "08" / "25" / "sh.raw"
    raw_path.write_bytes(b"changed")
    with pytest.raises(OfficialExecutionIntakeError, match="bytes"):
        load_verified_official_execution_session(
            artifact_path,
            registry=registry,
            raw_file_root=raw_root,
        )

    registry_path, artifact_path, raw_root, registry_value = _artifacts(tmp_path / "linked")
    registry = load_verified_official_execution_source_registry(
        registry_path,
        expected_registry_digest=str(registry_value["registry_digest"]),
    )
    raw_path = raw_root / "2026" / "08" / "25" / "sh.raw"
    target = tmp_path / "outside.raw"
    target.write_bytes(raw_path.read_bytes())
    raw_path.unlink()
    raw_path.symlink_to(target)
    with pytest.raises(OfficialExecutionIntakeError, match="symlink"):
        load_verified_official_execution_session(
            artifact_path,
            registry=registry,
            raw_file_root=raw_root,
        )


def test_session_and_decision_bindings_reject_semantic_gaps(tmp_path: Path) -> None:
    registry_path, artifact_path, raw_root, registry_value = _artifacts(tmp_path)
    registry = load_verified_official_execution_source_registry(
        registry_path,
        expected_registry_digest=str(registry_value["registry_digest"]),
    )
    session = load_verified_official_execution_session(
        artifact_path,
        registry=registry,
        raw_file_root=raw_root,
    )
    with pytest.raises(OfficialExecutionIntakeError, match="does not cover"):
        bind_official_execution_session_to_decisions(
            session,
            expected_symbols=["000001.SZ", "600519.SH"],
            source_snapshot_digest="c" * 64,
        )

    artifact = deepcopy(session.artifact)
    rows = cast(list[dict[str, object]], artifact["rows"])
    cast(dict[str, object], rows[0]["bar"])["adjustment_mode"] = "qfq"
    with pytest.raises(ValidationError):
        OfficialExecutionSessionArtifact.model_validate(artifact)

    artifact = deepcopy(session.artifact)
    rows = cast(list[dict[str, object]], artifact["rows"])
    cast(dict[str, object], rows[0]["corporate_action"])["status"] = "unknown"
    with pytest.raises(ValidationError):
        OfficialExecutionSessionArtifact.model_validate(artifact)


def test_receipt_must_match_registered_license_and_source_uri(tmp_path: Path) -> None:
    registry_path, artifact_path, raw_root, registry_value = _artifacts(tmp_path)
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["receipts"][0]["source_uri"] = "https://attacker.example.test/sh.raw"
    from app.services.market_scan_official_execution import official_execution_content_digest

    receipt = artifact["receipts"][0]
    receipt["receipt_digest"] = official_execution_content_digest(receipt, "receipt_digest")
    artifact["rows"][0]["receipt_digest"] = receipt["receipt_digest"]
    # Re-seal the normalized envelope. It is mechanically valid but still not
    # authoritative because the URI conflicts with the pinned registration.
    row = artifact["rows"][0]
    row.pop("trading_state_digest")
    row.pop("row_digest")
    rebuilt = seal_official_execution_session_artifact(
        session_date=artifact["session_date"],
        generated_at=artifact["generated_at"],
        source_registry_digest=artifact["source_registry_digest"],
        receipts=[receipt],
        rows=[row],
    )
    _write_json(artifact_path, rebuilt)
    registry = load_verified_official_execution_source_registry(
        registry_path,
        expected_registry_digest=str(registry_value["registry_digest"]),
    )
    with pytest.raises(OfficialExecutionIntakeError, match="registration"):
        load_verified_official_execution_session(
            artifact_path,
            registry=registry,
            raw_file_root=raw_root,
        )


def test_official_execution_store_is_unconfigured_without_out_of_band_digest(
    tmp_path: Path,
) -> None:
    store = MarketScanOfficialExecutionStore(
        registry_path=tmp_path / "registry.json",
        registry_digest=None,
        raw_file_root=tmp_path / "raw",
        session_directory=tmp_path / "sessions",
    )

    status = store.status().payload()

    assert status["configured"] is False
    assert status["status"] == "unconfigured_pinned_registry"
    assert status["formal_evidence_available"] is False
    assert status["public_vendor_auto_upgrade_forbidden"] is True


def test_official_execution_store_ingests_only_after_full_replay(tmp_path: Path) -> None:
    registry_path, artifact_path, raw_root, registry_value = _artifacts(tmp_path)
    session_directory = tmp_path / "managed" / "sessions"
    store = MarketScanOfficialExecutionStore(
        registry_path=registry_path,
        registry_digest=str(registry_value["registry_digest"]),
        raw_file_root=raw_root,
        session_directory=session_directory,
    )

    target = store.ingest(artifact_path)
    repeated = store.ingest(artifact_path)
    status = store.status()

    assert target == repeated
    assert target.parent == session_directory
    assert status.status == "ready"
    assert status.formal_evidence_available is True
    assert status.verified_session_count == 1
    assert store.session("2026-08-25") is not None


def test_official_execution_store_surfaces_verification_failure(tmp_path: Path) -> None:
    registry_path, artifact_path, raw_root, registry_value = _artifacts(tmp_path)
    store = MarketScanOfficialExecutionStore(
        registry_path=registry_path,
        registry_digest=str(registry_value["registry_digest"]),
        raw_file_root=raw_root,
        session_directory=tmp_path / "managed" / "sessions",
    )
    target = store.ingest(artifact_path)
    target.write_text("{}", encoding="utf-8")

    status = store.status()

    assert status.status == "verification_failed"
    assert status.formal_evidence_available is False
    assert status.failures


def test_official_execution_store_caches_only_unchanged_registry_session_and_raw_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path, artifact_path, raw_root, registry_value = _artifacts(tmp_path)
    store = MarketScanOfficialExecutionStore(
        registry_path=registry_path,
        registry_digest=str(registry_value["registry_digest"]),
        raw_file_root=raw_root,
        session_directory=tmp_path / "managed" / "sessions",
    )
    store.ingest(artifact_path)
    original = official_store_module.load_verified_official_execution_session
    calls = 0

    def counted(*args: object, **kwargs: object) -> VerifiedOfficialExecutionSession:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        official_store_module,
        "load_verified_official_execution_session",
        counted,
    )

    assert store.status().status == "ready"
    assert store.status().status == "ready"
    assert store.session("2026-08-25") is not None
    assert calls == 1

    raw_path = raw_root / "2026" / "08" / "25" / "sh.raw"
    raw_path.write_bytes(b"x" * raw_path.stat().st_size)
    assert store.status().status == "verification_failed"
    assert calls == 2


def test_official_store_filename_and_fingerprint_helpers_fail_closed(
    tmp_path: Path,
) -> None:
    store = MarketScanOfficialExecutionStore(
        registry_path=tmp_path / "registry.json",
        registry_digest=None,
        raw_file_root=tmp_path / "raw",
        session_directory=tmp_path / "sessions",
    )
    status = store.status()
    assert status.configured is False
    assert status.formal_evidence_available is False
    assert status.payload()["public_vendor_auto_upgrade_forbidden"] is True
    with pytest.raises(OfficialExecutionIntakeError, match="not configured"):
        store.registry()

    assert official_store_module.official_execution_session_filename(
        {"session_date": "2026-08-25", "artifact_digest": "a" * 64}
    ).endswith(f"-{'a' * 64}.json")
    for invalid in (
        {"session_date": "bad", "artifact_digest": "a" * 64},
        {"session_date": "2026-08-25", "artifact_digest": "bad"},
    ):
        with pytest.raises(OfficialExecutionIntakeError, match="filename identity"):
            official_store_module.official_execution_session_filename(invalid)

    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    directory = tmp_path / "directory"
    directory.mkdir()
    assert official_store_module._path_fingerprint(  # noqa: SLF001
        registry_path,
        directory=False,
    )[0] == str(registry_path)
    assert official_store_module._path_fingerprint(  # noqa: SLF001
        directory,
        directory=True,
    )[0] == str(directory)
    with pytest.raises(OfficialExecutionIntakeError, match="unsafe path"):
        official_store_module._path_fingerprint(  # noqa: SLF001
            registry_path,
            directory=True,
        )
    assert official_store_module._mapping_sequence([{"a": 1}], "rows") == [  # noqa: SLF001
        {"a": 1}
    ]
    with pytest.raises(OfficialExecutionIntakeError, match="invalid"):
        official_store_module._mapping_sequence([1], "rows")  # noqa: SLF001
    assert official_store_module._catalog_fingerprint(  # noqa: SLF001
        registry_path,
        tmp_path / "missing-sessions",
    )[1:] == (None, ())
    fingerprint = official_store_module._store_fingerprint(  # noqa: SLF001
        registry_path,
        tmp_path / "missing-raw",
        tmp_path / "missing-sessions",
        (),
    )
    assert fingerprint[1] is None
    assert official_store_module._short_error(  # noqa: SLF001
        ValueError(" spaced   failure ")
    ) == "ValueError: spaced failure"
