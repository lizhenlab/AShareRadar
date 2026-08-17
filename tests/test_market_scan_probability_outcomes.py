from __future__ import annotations

from copy import deepcopy
import gzip
from pathlib import Path
from typing import cast

import pytest

from app.models.market import Kline
from app.services.market_scan_probability import stable_probability_hash
import app.services.market_scan_probability_outcomes as outcomes_module
from app.services.market_scan_probability_outcomes import (
    ProbabilityOutcomeError,
    ProbabilityOutcomeSemanticDriftError,
    build_probability_outcome_artifact,
    list_probability_outcome_artifacts,
    load_probability_outcome_artifact,
    load_probability_outcome_artifact_for_run,
    probability_outcome_corpus_progress,
    probability_outcome_payload_digest,
    probability_outcome_required_dates,
    probability_research_rows_from_outcome_artifacts,
    probability_samples_from_outcome_artifacts,
    publish_built_probability_outcome_artifact,
    publish_probability_outcome_artifact,
    verify_probability_outcome_artifact,
)


GENERATED_AT = "2026-08-13T16:30:00+08:00"


def test_outcome_artifact_is_compact_content_addressed_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)
    rows = {"600001.SH": _complete_h1_rows()}

    first = publish_probability_outcome_artifact(
        tmp_path,
        source,
        rows,
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )
    repeated = publish_probability_outcome_artifact(
        tmp_path,
        source,
        rows,
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )

    assert first == repeated
    assert list_probability_outcome_artifacts(tmp_path) == [first]
    loaded = load_probability_outcome_artifact(cast(str, first["path"]))
    assert load_probability_outcome_artifact_for_run(tmp_path, 71) == loaded
    record = cast(list[dict[str, object]], cast(dict[str, object], loaded["payload"])["records"])[0]
    assert "features" not in record and "dimensions" not in record
    assert record["feature_vector_digest"] == stable_probability_hash(_features())
    h1 = cast(dict[str, object], cast(dict[str, object], record["horizons"])["1"])
    assert h1["maturity"] == "mature"
    assert cast(dict[str, object], h1["outcome"])["status"] == "modelled"

    built = build_probability_outcome_artifact(
        source,
        rows,
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )
    assert publish_built_probability_outcome_artifact(tmp_path, built) == first


def test_outcome_required_dates_are_exact_and_horizon_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)

    assert probability_outcome_required_dates(source, as_of_date="2026-08-12") == ()
    assert probability_outcome_required_dates(source, as_of_date="2026-08-13") == (
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
    )
    assert probability_outcome_required_dates(source, as_of_date="2026-08-19") == (
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
        "2026-08-18",
        "2026-08-19",
    )


def test_outcome_uses_fixed_sessions_and_never_shifts_a_missing_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)
    rows = [*_complete_h1_rows()[:-1], _bar("2026-08-14", 12.0, 12.2)]

    artifact = build_probability_outcome_artifact(
        source,
        {"600001.SH": rows},
        generated_at="2026-08-14T16:30:00+08:00",
        as_of_date="2026-08-14",
    )

    payload = cast(dict[str, object], artifact["payload"])
    record = cast(list[dict[str, object]], payload["records"])[0]
    evidence = cast(dict[str, object], record["bar_evidence"])
    assert evidence["requested_dates"] == ["2026-08-11", "2026-08-12", "2026-08-13"]
    assert evidence["missing_dates"] == ["2026-08-13"]
    assert "2026-08-14" not in cast(list[str], evidence["observed_dates"])
    h1 = cast(dict[str, object], cast(dict[str, object], record["horizons"])["1"])
    outcome = cast(dict[str, object], h1["outcome"])
    assert h1["target_session_date"] == "2026-08-13"
    assert outcome["status"] == "data_unavailable"
    assert outcome["reason"] == "fixed_exit_session_bar_missing"
    assert outcome["exit_date"] == "2026-08-13"
    quality = cast(dict[str, object], cast(dict[str, object], payload["quality"])["horizons"])
    assert cast(dict[str, object], quality["1"])["label_coverage"] == 0.0


def test_not_mature_horizons_do_not_request_bars_or_fabricate_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)

    artifact = build_probability_outcome_artifact(
        source,
        {},
        generated_at="2026-08-12T11:30:00+08:00",
        as_of_date="2026-08-12",
    )

    payload = cast(dict[str, object], artifact["payload"])
    record = cast(list[dict[str, object]], payload["records"])[0]
    assert cast(dict[str, object], record["bar_evidence"])["requested_dates"] == []
    for state in cast(dict[str, dict[str, object]], record["horizons"]).values():
        assert state["maturity"] == "not_mature"
        assert state["outcome"] is None
    progress = probability_outcome_corpus_progress([artifact])
    assert all(cast(dict[str, object], value)["mature_label_session_count"] == 0 for value in progress.values())


def test_outcome_verification_replays_labels_and_join_restores_source_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)
    artifact = build_probability_outcome_artifact(
        source,
        {"600001.SH": _complete_h1_rows()},
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )

    research_rows = probability_research_rows_from_outcome_artifacts([source], [artifact])
    assert len(research_rows) == 1
    assert research_rows[0].features == _features()
    assert research_rows[0].mature_horizons == frozenset({1})
    samples = probability_samples_from_outcome_artifacts(
        [source],
        [artifact],
        horizon=1,
        target="absolute_net_positive",
    )
    assert len(samples) == 1 and samples[0].executable is True and samples[0].target == 1

    tampered = deepcopy(artifact)
    payload = cast(dict[str, object], tampered["payload"])
    record = cast(list[dict[str, object]], payload["records"])[0]
    outcome = cast(dict[str, object], cast(dict[str, object], cast(dict[str, object], record["horizons"])["1"])["outcome"])
    outcome["net_return"] = -0.25
    cast(dict[str, object], tampered["integrity"])["integrity_digest"] = probability_outcome_payload_digest(payload)
    with pytest.raises(ProbabilityOutcomeError, match="不能由固定会话K线重放"):
        verify_probability_outcome_artifact(tampered)


def test_intact_legacy_rule_profile_drift_is_typed_but_digest_tamper_is_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = deepcopy(_source())
    source_record = cast(list[dict[str, object]], cast(dict[str, object], source["payload"])["records"])[0]
    cast(dict[str, object], source_record["instrument"])["list_date"] = "1990-12-19"
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)
    artifact = build_probability_outcome_artifact(
        source,
        {"600001.SH": _complete_h1_rows()},
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )
    ordinary_mismatch = deepcopy(artifact)
    payload = cast(dict[str, object], artifact["payload"])
    record = cast(list[dict[str, object]], payload["records"])[0]
    h1 = cast(dict[str, object], cast(dict[str, object], record["horizons"])["1"])
    h1["outcome"] = _legacy_degraded_entry_outcome()
    payload["quality"] = outcomes_module._quality(  # noqa: SLF001
        cast(list[dict[str, object]], payload["records"]),
        cast(dict[str, object], payload["source"]),
        (1, 5, 20),
    )
    digest = probability_outcome_payload_digest(payload)
    cast(dict[str, object], artifact["integrity"])["integrity_digest"] = digest
    filename = f"market-scan-probability-outcomes-run-71-through-2026-08-13-{digest}.json.gz"
    path = tmp_path / filename
    path.write_bytes(gzip.compress(outcomes_module._canonical_json(artifact).encode(), compresslevel=9, mtime=0))

    with pytest.raises(ProbabilityOutcomeSemanticDriftError, match="旧规则画像语义") as drift:
        load_probability_outcome_artifact(path)
    assert (
        drift.value.run_id,
        drift.value.as_of_date,
        drift.value.generated_at,
        drift.value.integrity_digest,
        drift.value.source_digest,
    ) == (71, "2026-08-13", GENERATED_AT, digest, "a" * 64)

    for field, value in (
        ("label", 0),
        ("gross_return", 0.0),
        ("net_return", 0.0),
        ("cost_drag", 0.0),
        ("entry_date", "2026-08-13"),
        ("exit_date", "2026-08-13"),
        ("entry_price", 10.0),
        ("exit_price", 11.0),
        ("model_limited", True),
        ("daily_bar_model_limited", True),
    ):
        resealed = deepcopy(artifact)
        resealed_payload = cast(dict[str, object], resealed["payload"])
        resealed_record = cast(list[dict[str, object]], resealed_payload["records"])[0]
        resealed_h1 = cast(dict[str, object], cast(dict[str, object], resealed_record["horizons"])["1"])
        cast(dict[str, object], resealed_h1["outcome"])[field] = value
        resealed_digest = probability_outcome_payload_digest(resealed_payload)
        cast(dict[str, object], resealed["integrity"])["integrity_digest"] = resealed_digest
        resealed_path = tmp_path / (
            f"market-scan-probability-outcomes-run-71-through-2026-08-13-{resealed_digest}.json.gz"
        )
        resealed_path.write_bytes(
            gzip.compress(
                outcomes_module._canonical_json(resealed).encode(),  # noqa: SLF001
                compresslevel=9,
                mtime=0,
            )
        )
        with pytest.raises(ProbabilityOutcomeError, match="不能由固定会话K线重放") as resealed_error:
            load_probability_outcome_artifact(resealed_path)
        assert not isinstance(resealed_error.value, ProbabilityOutcomeSemanticDriftError)

    tampered = deepcopy(artifact)
    tampered_payload = cast(dict[str, object], tampered["payload"])
    tampered_record = cast(list[dict[str, object]], tampered_payload["records"])[0]
    tampered_record["feature_vector_digest"] = "0" * 64
    tampered_dir = tmp_path / "tampered"
    tampered_dir.mkdir()
    tampered_path = tampered_dir / filename
    tampered_path.write_bytes(
        gzip.compress(outcomes_module._canonical_json(tampered).encode(), compresslevel=9, mtime=0)
    )
    with pytest.raises(ProbabilityOutcomeError, match="payload digest 不一致") as error:
        load_probability_outcome_artifact(tampered_path)
    assert not isinstance(error.value, ProbabilityOutcomeSemanticDriftError)

    mismatch_payload = cast(dict[str, object], ordinary_mismatch["payload"])
    mismatch_record = cast(list[dict[str, object]], mismatch_payload["records"])[0]
    mismatch_h1 = cast(dict[str, object], cast(dict[str, object], mismatch_record["horizons"])["1"])
    mismatch_outcome = cast(dict[str, object], mismatch_h1["outcome"])
    mismatch_outcome["net_return"] = -0.25
    mismatch_digest = probability_outcome_payload_digest(mismatch_payload)
    cast(dict[str, object], ordinary_mismatch["integrity"])["integrity_digest"] = mismatch_digest
    mismatch_path = tmp_path / (
        f"market-scan-probability-outcomes-run-71-through-2026-08-13-{mismatch_digest}.json.gz"
    )
    mismatch_path.write_bytes(
        gzip.compress(
            outcomes_module._canonical_json(ordinary_mismatch).encode(),  # noqa: SLF001
            compresslevel=9,
            mtime=0,
        )
    )
    with pytest.raises(ProbabilityOutcomeError, match="不能由固定会话K线重放") as mismatch:
        load_probability_outcome_artifact(mismatch_path)
    assert not isinstance(mismatch.value, ProbabilityOutcomeSemanticDriftError)


@pytest.mark.parametrize("reverse_records", (False, True))
def test_legacy_drift_never_hides_invalid_sibling_record(
    monkeypatch: pytest.MonkeyPatch,
    reverse_records: bool,
) -> None:
    source = deepcopy(_source())
    payload = cast(dict[str, object], source["payload"])
    first = cast(list[dict[str, object]], payload["records"])[0]
    second = deepcopy(first)
    second["symbol"] = "600002.SH"
    second["source_evidence_digest"] = "c" * 64
    payload["records"] = [first, second]
    cast(dict[str, object], first["instrument"])["list_date"] = "1990-12-19"
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)
    artifact = build_probability_outcome_artifact(
        source,
        {
            "600001.SH": _complete_h1_rows(),
            "600002.SH": _complete_h1_rows(),
        },
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )
    outcome_payload = cast(dict[str, object], artifact["payload"])
    records = cast(list[dict[str, object]], outcome_payload["records"])
    drift_record = next(item for item in records if item["symbol"] == "600001.SH")
    bad_record = next(item for item in records if item["symbol"] == "600002.SH")
    drift_h1 = cast(dict[str, object], cast(dict[str, object], drift_record["horizons"])["1"])
    drift_h1["outcome"] = _legacy_degraded_entry_outcome()
    bad_h1 = cast(dict[str, object], cast(dict[str, object], bad_record["horizons"])["1"])
    cast(dict[str, object], bad_h1["outcome"])["net_return"] = -0.25
    if reverse_records:
        records.reverse()
    cast(dict[str, object], artifact["integrity"])["integrity_digest"] = (
        probability_outcome_payload_digest(outcome_payload)
    )

    with pytest.raises(ProbabilityOutcomeError) as error:
        verify_probability_outcome_artifact(artifact)
    assert not isinstance(error.value, ProbabilityOutcomeSemanticDriftError)


def test_mature_source_requests_only_fixed_sessions_and_maps_loader_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "load_probability_source_snapshot", lambda _path: source)
    published: dict[str, object] = {}

    def publish(directory, snapshot, rows, **kwargs):
        published.update(
            directory=directory,
            snapshot=snapshot,
            rows=rows,
            kwargs=kwargs,
        )
        return {"path": "published"}

    monkeypatch.setattr(outcomes_module, "publish_probability_outcome_artifact", publish)
    requests: list[tuple[str, tuple[str, ...]]] = []

    def load(symbol: str, dates: tuple[str, ...]):
        requests.append((symbol, dates))
        return _complete_h1_rows()

    result = outcomes_module.mature_probability_source_snapshot(
        tmp_path / "source.json.gz",
        tmp_path / "outcomes",
        load,
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )

    assert result == {"path": "published"}
    assert requests == [("600001.SH", ("2026-08-11", "2026-08-12", "2026-08-13"))]
    assert published["rows"] == {"600001.SH": tuple(_complete_h1_rows())}

    def fail(_symbol: str, _dates: tuple[str, ...]):
        raise OSError("provider unavailable")

    with pytest.raises(ProbabilityOutcomeError, match="600001.SH outcome K线加载失败"):
        outcomes_module.mature_probability_source_snapshot(
            tmp_path / "source.json.gz",
            tmp_path / "outcomes",
            fail,
            generated_at=GENERATED_AT,
            as_of_date="2026-08-13",
        )


def test_outcome_public_artifact_boundary_translates_storage_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)
    artifact = build_probability_outcome_artifact(
        source,
        {"600001.SH": _complete_h1_rows()},
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )
    target = tmp_path / outcomes_module.probability_outcome_artifact_filename(artifact)
    publisher_errors = (
        (outcomes_module.ArtifactContentConflictError(target), "已存在且内容不同"),
        (outcomes_module.ArtifactPublishConflictError(target), "并发发布冲突"),
        (outcomes_module.ArtifactNotDirectoryError(tmp_path), "输出目录必须是真实目录"),
        (outcomes_module.ArtifactNotRegularError(target), "target 不是普通文件"),
        (outcomes_module.ArtifactTooLargeError(target, max_bytes=1), "超过压缩大小上限"),
        (outcomes_module.ArtifactIOError("write failed"), "写入失败"),
    )
    for error, message in publisher_errors:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                outcomes_module,
                "exclusive_atomic_publish",
                lambda *_args, _error=error, **_kwargs: (_ for _ in ()).throw(_error),
            )
            with pytest.raises(ProbabilityOutcomeError, match=message):
                publish_built_probability_outcome_artifact(tmp_path, artifact)

    read_errors = (
        (outcomes_module.ArtifactNotRegularError(target), "必须是普通文件"),
        (outcomes_module.ArtifactTooLargeError(target, max_bytes=1), "超过压缩大小上限"),
        (outcomes_module.ArtifactChangedError(target, stage="read"), "读取期间发生变化"),
        (outcomes_module.ArtifactIOError("read failed"), "无法读取"),
    )
    for error, message in read_errors:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                outcomes_module,
                "read_regular_file",
                lambda *_args, _error=error, **_kwargs: (_ for _ in ()).throw(_error),
            )
            with pytest.raises(ProbabilityOutcomeError, match=message):
                load_probability_outcome_artifact(target)

    target.write_bytes(b"not-gzip")
    with pytest.raises(ProbabilityOutcomeError, match="gzip 损坏"):
        load_probability_outcome_artifact(target)

    target.write_bytes(gzip.compress(b"[]", mtime=0))
    with pytest.raises(ProbabilityOutcomeError, match="顶层必须是 object"):
        load_probability_outcome_artifact(target)

    target.write_bytes(gzip.compress(b'{"value": 1, "value": 2}', mtime=0))
    with pytest.raises(ProbabilityOutcomeError, match="重复 JSON key"):
        load_probability_outcome_artifact(target)


def test_outcome_verifier_fails_closed_at_each_public_contract_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(outcomes_module, "_source_artifact", lambda _value: source)
    artifact = build_probability_outcome_artifact(
        source,
        {"600001.SH": _complete_h1_rows()},
        generated_at=GENERATED_AT,
        as_of_date="2026-08-13",
    )

    def payload(value: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], value["payload"])

    def record(value: dict[str, object]) -> dict[str, object]:
        return cast(list[dict[str, object]], payload(value)["records"])[0]

    def label_contract(value: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], payload(value)["label_contract"])

    def reseal_label(value: dict[str, object]) -> None:
        payload(value)["label_contract_digest"] = stable_probability_hash(label_contract(value))

    def calendar(value: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], payload(value)["calendar_contract"])

    def bars(value: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], record(value)["bar_evidence"])

    def mutate_label(value: dict[str, object], name: str, replacement: object) -> None:
        label_contract(value)[name] = replacement
        reseal_label(value)

    cases = (
        ("schema_version", lambda value: value.__setitem__("schema_version", "future"), "schema_version"),
        (
            "payload contract",
            lambda value: payload(value).__setitem__("contract_version", "future"),
            "contract_version",
        ),
        (
            "generated identity",
            lambda value: payload(value).__setitem__("generated_at", "2026-08-13T16:31:00+08:00"),
            "generated_at 冲突",
        ),
        (
            "label digest",
            lambda value: payload(value).__setitem__("label_contract_digest", "0" * 64),
            "label_contract_digest",
        ),
        (
            "source date identity",
            lambda value: cast(dict[str, object], payload(value)["source"]).__setitem__(
                "data_date", "2026-08-08"
            ),
            "source 日期身份",
        ),
        (
            "official cohort",
            lambda value: cast(dict[str, object], payload(value)["cohort"]).__setitem__("mode", "intraday"),
            "official source cohort",
        ),
        (
            "label horizons",
            lambda value: mutate_label(value, "horizons", [1, 20, 5]),
            "label horizons",
        ),
        (
            "cost profile",
            lambda value: mutate_label(value, "cost_profile_id", "unknown-v1"),
            "cost_profile_id",
        ),
        (
            "label exact contract",
            lambda value: mutate_label(value, "unexpected", True),
            "可执行标签契约",
        ),
        (
            "calendar version",
            lambda value: calendar(value).__setitem__("version", "future"),
            "calendar contract version",
        ),
        (
            "calendar entry",
            lambda value: calendar(value).__setitem__("entry_session_date", "2026-08-13"),
            "quote/entry date",
        ),
        (
            "missing policy",
            lambda value: calendar(value).__setitem__("missing_bar_policy", "shift"),
            "missing bar policy",
        ),
        (
            "future grid shape",
            lambda value: calendar(value).__setitem__(
                "future_sessions", cast(list[object], calendar(value)["future_sessions"])[:-1]
            ),
            "future session grid",
        ),
        (
            "exit grid",
            lambda value: cast(dict[str, object], calendar(value)["horizon_exit_sessions"]).__setitem__(
                "1", "2026-08-14"
            ),
            "horizon exit sessions",
        ),
        (
            "session grid digest",
            lambda value: calendar(value).__setitem__("session_grid_digest", "0" * 64),
            "session_grid_digest",
        ),
        (
            "instrument identity",
            lambda value: cast(dict[str, object], record(value)["instrument"]).__setitem__("market", "SZ"),
            "instrument market/qfq",
        ),
        (
            "bar evidence version",
            lambda value: bars(value).__setitem__("version", "future"),
            "bar evidence version",
        ),
        (
            "bar requested dates",
            lambda value: bars(value).__setitem__(
                "requested_dates", cast(list[object], bars(value)["requested_dates"])[:-1]
            ),
            "requested_dates",
        ),
        (
            "bar observed identity",
            lambda value: bars(value).__setitem__("observed_dates", []),
            "observed/missing dates",
        ),
        (
            "bar digest",
            lambda value: bars(value).__setitem__("bar_set_digest", "0" * 64),
            "bar_set_digest",
        ),
        (
            "quality replay",
            lambda value: cast(dict[str, object], payload(value)["quality"]).__setitem__("record_count", 0),
            "quality 不能由 records 重放",
        ),
        (
            "limitations",
            lambda value: cast(list[str], payload(value)["limitations"]).append("unsafe"),
            "limitations contract",
        ),
    )

    for _name, mutate, message in cases:
        candidate = deepcopy(artifact)
        mutate(candidate)
        candidate_payload = payload(candidate)
        cast(dict[str, object], candidate["integrity"])["integrity_digest"] = (
            probability_outcome_payload_digest(candidate_payload)
        )
        with pytest.raises(ProbabilityOutcomeError, match=message):
            verify_probability_outcome_artifact(candidate)

    bad_integrity = deepcopy(artifact)
    cast(dict[str, object], bad_integrity["integrity"])["notice"] = "signature"
    with pytest.raises(ProbabilityOutcomeError, match="integrity contract"):
        verify_probability_outcome_artifact(bad_integrity)

    bad_digest = deepcopy(artifact)
    cast(dict[str, object], bad_digest["integrity"])["integrity_digest"] = "0" * 64
    with pytest.raises(ProbabilityOutcomeError, match="payload digest"):
        verify_probability_outcome_artifact(bad_digest)


def _source() -> dict[str, object]:
    features = _features()
    record = {
        "symbol": "600001.SH",
        "features": features,
        "feature_vector_digest": stable_probability_hash(features),
        "dimensions": {
            "mode": "official",
            "scope": "test-full-market",
            "rule_version": "full-market-scan-v6:test",
            "market": "SH",
            "board": "SH_MAIN",
            "industry": "test",
            "liquidity": "medium",
            "regime": "neutral",
            "segment": "regular",
        },
        "source_evidence_digest": "b" * 64,
        "instrument": {
            "market": "SH",
            "list_date": "2020-01-02",
            "is_st": False,
            "quote_amount": 200_000_000.0,
            "adjustment_mode": "qfq",
        },
    }
    return {
        "schema_version": "market-scan-probability-source-artifact-v1",
        "captured_at": "2026-08-11T16:30:00+08:00",
        "payload": {
            "contract_version": "market-scan-probability-source-snapshot-v1",
            "run": {
                "run_id": 71,
                "quote_date": "2026-08-11",
                "data_date": "2026-08-11",
                "as_of": "2026-08-11T16:00:00+08:00",
            },
            "cohort": {
                "mode": "official",
                "scope": "test-full-market",
                "rule_version": "full-market-scan-v6:test",
            },
            "records": [record],
            "quality": {"record_count": 1},
        },
        "integrity": {"integrity_digest": "a" * 64},
    }


def _features() -> dict[str, float]:
    return {"trend_score": 80.0, "production_score": 82.0}


def _complete_h1_rows() -> list[Kline]:
    return [
        _bar("2026-08-11", 10.0, 10.0),
        _bar("2026-08-12", 10.0, 10.5),
        _bar("2026-08-13", 10.5, 11.0),
    ]


def _legacy_degraded_entry_outcome() -> dict[str, object]:
    return {
        "horizon": 1,
        "status": "data_unavailable",
        "reason": "entry_rule_profile_degraded",
        "label": None,
        "gross_return": None,
        "net_return": None,
        "cost_drag": None,
        "entry_date": "2026-08-12",
        "exit_date": None,
        "entry_price": None,
        "exit_price": None,
        "model_limited": False,
        "rule_profile_verified": False,
        "daily_bar_model_limited": False,
    }


def _bar(row_date: str, open_price: float, close_price: float) -> Kline:
    return Kline(
        date=row_date,
        open=open_price,
        close=close_price,
        high=max(open_price, close_price) + 0.2,
        low=min(open_price, close_price) - 0.1,
        volume=1_000,
        adjustment_mode="qfq",
        data_version="probability-outcome-test-v1",
        contract_version="daily-kline.v1",
        source="test",
    )
