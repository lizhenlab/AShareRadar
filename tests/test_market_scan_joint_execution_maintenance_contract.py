from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import gzip
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import cast

import pytest

import app.services.market_scan_joint_execution_maintenance as maintenance
import app.services.market_scan_joint_execution_probability as joint_probability


class _OfficialStatus:
    def __init__(self, *, ready: bool = True) -> None:
        self.formal_evidence_available = ready
        self._ready = ready

    def payload(self) -> dict[str, object]:
        return {
            "configured": self._ready,
            "status": "ready" if self._ready else "unconfigured_pinned_registry",
            "formal_evidence_available": self._ready,
            "failures": [] if self._ready else ["registry_not_configured"],
        }


class _OfficialStore:
    def __init__(self, *, ready: bool = True, sessions: tuple[object, ...] = ()) -> None:
        self._status = _OfficialStatus(ready=ready)
        self._sessions = sessions

    def status(self) -> _OfficialStatus:
        return self._status

    def sessions(self) -> tuple[object, ...]:
        return self._sessions


class _RankingStore:
    def __init__(self) -> None:
        self.published: list[tuple[object, Path | None]] = []
        self.rollbacks: list[object] = []

    def publish(self, publication: object, *, artifact_path: Path | None = None) -> None:
        self.published.append((publication, artifact_path))

    def record_rollback(self, control: object) -> None:
        self.rollbacks.append(control)

    def is_rolled_back(self, _publication: object) -> bool:
        return False

    def verify_mirror(self, _publication: object) -> bool:
        return True


def _service(
    tmp_path: Path,
    *,
    ready: bool = True,
    authorization_digest: str | None = None,
    ranking_control_digest: str | None = None,
) -> maintenance.MarketScanJointExecutionMaintenanceService:
    cache = SimpleNamespace(path=tmp_path / "runtime.sqlite3")
    return maintenance.MarketScanJointExecutionMaintenanceService(
        cache,
        cast(maintenance.MarketScanOfficialExecutionStore, _OfficialStore(ready=ready)),
        authorization_digest=authorization_digest,
        ranking_control_digest=ranking_control_digest,
        source_directory=tmp_path / "sources",
        research_directory=tmp_path / "research",
        ranking_store=cast(maintenance.MarketScanProbabilityRankingStore, _RankingStore()),
    )


def _source(run_id: int, session: str, *, frozen_at: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        signal_session=session,
        decision_frozen_at=frozen_at or f"{session}T15:01:00+08:00",
        artifact_digest=maintenance.canonical_json_bytes([run_id, "artifact"]).hex()[:64].ljust(64, "0"),
        source_snapshot_digest="b" * 64,
        decision_identity_digest="c" * 64,
        decision_membership_digest="d" * 64,
        feature_schema_digest="e" * 64,
        feature_schema={"version": "features-v1", "names": ["x"]},
        records=[],
        __len__=lambda self: 0,
    )


def _pair(run_id: int, session: str) -> maintenance._MaturePair:
    return maintenance._MaturePair(
        source=cast(maintenance.VerifiedJointExecutionSourceCorpus, _source(run_id, session)),
        outcome=cast(maintenance.VerifiedJointExecutionOutcomeCorpus, SimpleNamespace(run_id=run_id)),
    )


def _state(
    *,
    authorization: bool = False,
    ranking_control: bool = False,
) -> maintenance._MaintenanceRunState:
    return maintenance._MaintenanceRunState(
        generated_at="2026-08-23T16:00:00+08:00",
        official_status=_OfficialStatus().payload(),
        authorization_configured=authorization,
        ranking_control_configured=ranking_control,
        source_inputs=(),
        source_tokens=[],
        mature_pairs=[],
        blockers=[],
        failures=[],
    )


def test_maintenance_summary_projects_ready_rollback_and_degraded_states() -> None:
    base = dict(
        source_archive_count=3,
        current_source_count=3,
        verified_source_count=3,
        mature_h5_session_count=2,
        selection_minimum_session_count=292,
        selection_qualified=True,
        authorization_configured=True,
        authorization_verified=True,
        deployment_verified=True,
        current_prediction_run_id=9,
        current_prediction_count=4,
        official_execution={"status": "ready"},
        generated_at="2026-08-23T16:00:00+08:00",
    )
    ready = maintenance.JointExecutionMaintenanceSummary(
        status="current_prediction_ready",
        probability_ranking_status="production_ranking_ready",
        **base,
    )
    rollback = maintenance.JointExecutionMaintenanceSummary(
        status="current_prediction_ready",
        probability_ranking_status="rollback_active",
        failures=("mirror mismatch",),
        **base,
    )
    waiting = maintenance.JointExecutionMaintenanceSummary(
        status="selection_evidence_accumulating",
        **base,
    )

    assert ready.filter_ready and not ready.degraded
    assert ready.payload()["production_ranking_effect"] == "v6_active_for_exact_published_run"
    assert rollback.degraded
    assert rollback.payload()["production_ranking_effect"] == "rollback_active_v5_only"
    assert not waiting.filter_ready
    assert waiting.payload()["production_ranking_effect"] == "none_without_v6_manual_promotion"
    assert "成熟会话 2/292" in ready.message()


def test_service_fail_closed_status_and_timezone_contract(tmp_path: Path) -> None:
    service = _service(tmp_path, ready=False)
    before = service.status_projection()
    assert before["status"] == "maintenance_not_run"
    with pytest.raises(ValueError, match="timezone-aware"):
        service.run(now=datetime(2026, 8, 23, 16, 0))

    summary = service.run(now=datetime.fromisoformat("2026-08-23T16:00:00+08:00"))
    assert summary.status == "official_execution_unavailable"
    assert summary.blockers == ("registry_not_configured",)
    assert service.run_projection(1)[1] == {}
    assert not service.has_current_projection(1)
    assert not service.filter_qualified(1)
    assert not service.has_production_ranking(1)
    assert service.production_ranking_projection(1)[0]["status"] == "inactive"


def _seed_old_maintenance_authority(
    service: maintenance.MarketScanJointExecutionMaintenanceService,
) -> None:
    service._current_authority = cast(maintenance._CurrentAuthority, object())
    service._active_ranking_control = cast(
        maintenance.VerifiedProbabilityRankingManualControl, object()
    )
    service._ranking_publications[29] = cast(
        maintenance.VerifiedProbabilityRankingPublication, object()
    )
    service._last_summary = service._summary(
        status="current_prediction_ready",
        generated_at="2026-08-23T16:00:00+08:00",
        official_status={"status": "ready", "formal_evidence_available": True},
        selection_qualified=True,
        authorization_verified=True,
        deployment_verified=True,
        current_prediction_run_id=29,
        current_prediction_count=10,
        probability_ranking_status="production_ranking_ready",
        probability_ranking_count=10,
    )


def _assert_closed_maintenance_reads(
    service: maintenance.MarketScanJointExecutionMaintenanceService,
    *,
    status: str,
) -> None:
    projected = service.status_projection()
    assert projected["status"] == status
    for key in ("filter_ready", "selection_qualified", "authorization_verified", "deployment_verified"):
        assert projected[key] is False
    assert projected["current_prediction_count"] == 0
    assert projected["probability_ranking_count"] == 0
    assert projected["probability_ranking_control_verified"] is False
    assert projected["official_execution"]["formal_evidence_available"] is False
    assert projected["official_execution"]["verified_session_count"] == 0
    assert not service.has_current_projection(29)
    assert not service.filter_qualified(29)
    research = service.research_projection(29)
    assert research["availability"] == status
    assert research["pipeline_stage"] == status
    assert research["status"] == "not_generated"
    assert research["filter_qualified"] is False
    assert research["run_binding"] is None
    research_with_records, records = service.run_projection(29)
    assert research_with_records["availability"] == status and records == {}
    assert not service.has_production_ranking(29)
    ranking, ranked_records = service.production_ranking_projection(29)
    assert ranking["status"] == "inactive" and ranked_records == {}
    assert ranking["reason"] == status


def _forbid_authority_store_reads(
    service: maintenance.MarketScanJointExecutionMaintenanceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pending/failed maintenance must not inspect authority stores")

    monkeypatch.setattr(service.official_store, "status", forbidden)
    monkeypatch.setattr(service.ranking_store, "is_rolled_back", forbidden)
    monkeypatch.setattr(service.ranking_store, "verify_mirror", forbidden)


def test_all_projection_reads_return_pending_before_background_maintenance_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, authorization_digest="a" * 64, ranking_control_digest="b" * 64)
    _seed_old_maintenance_authority(service)
    _forbid_authority_store_reads(service, monkeypatch)
    entered, release, read_done = Event(), Event(), Event()
    errors: list[BaseException] = []

    def maintain(_now: datetime) -> maintenance.JointExecutionMaintenanceSummary:
        entered.set()
        assert release.wait(5)
        return service._maintenance_summary("maintenance_failed")

    def read() -> None:
        try:
            _assert_closed_maintenance_reads(service, status="maintenance_pending")
        except BaseException as exc:
            errors.append(exc)
        finally:
            read_done.set()

    monkeypatch.setattr(service, "_run_locked", maintain)
    worker = Thread(target=service.run, daemon=True)
    reader = Thread(target=read, daemon=True)
    worker.start()
    try:
        assert entered.wait(1)
        reader.start()
        assert read_done.wait(1), "projection read waited for the maintenance writer"
        assert not errors
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(timeout=2)
        if reader.ident is not None:
            reader.join(timeout=2)
    assert not worker.is_alive() and not reader.is_alive()


def test_same_thread_rlock_reentry_cannot_observe_partial_maintenance_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    _forbid_authority_store_reads(service, monkeypatch)

    def maintain(_now: datetime) -> maintenance.JointExecutionMaintenanceSummary:
        _seed_old_maintenance_authority(service)
        _assert_closed_maintenance_reads(service, status="maintenance_pending")
        with pytest.raises(RuntimeError, match="already running"):
            service.run()
        assert service._maintenance_running
        service._revoke_current_authority()
        return service._maintenance_summary("maintenance_failed")

    monkeypatch.setattr(service, "_run_locked", maintain)
    service.run()
    assert not service._maintenance_running
    _assert_closed_maintenance_reads(service, status="maintenance_failed")


@pytest.mark.parametrize("failure", [RuntimeError("fit failed"), KeyboardInterrupt()])
def test_failed_maintenance_revokes_partial_authority_and_releases_read_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    service = _service(tmp_path)
    _seed_old_maintenance_authority(service)
    _forbid_authority_store_reads(service, monkeypatch)

    def maintain(_now: datetime) -> maintenance.JointExecutionMaintenanceSummary:
        assert service._current_authority is None
        assert service._active_ranking_control is None
        assert service._ranking_publications == {}
        _seed_old_maintenance_authority(service)
        raise failure

    monkeypatch.setattr(service, "_run_locked", maintain)
    with pytest.raises(type(failure)):
        service.run()
    assert service._current_authority is None
    assert service._active_ranking_control is None
    assert service._ranking_publications == {}
    assert not service._maintenance_running
    _assert_closed_maintenance_reads(service, status="maintenance_failed")
    assert type(failure).__name__ in service.status_projection()["failures"][0]
    monkeypatch.setattr(service, "_run_locked", lambda _now: service._maintenance_summary("maintenance_pending"))
    assert service.run().status == "maintenance_pending"


@pytest.mark.parametrize("rolled_back,mirror_valid", [(True, True), (False, False), (False, True)])
def test_nonbusy_ranking_reads_still_require_rollback_and_mirror_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rolled_back: bool, mirror_valid: bool
) -> None:
    service = _service(tmp_path)
    _seed_old_maintenance_authority(service)
    calls: list[str] = []
    monkeypatch.setattr(service.ranking_store, "is_rolled_back", lambda _value: calls.append("rollback") or rolled_back)
    monkeypatch.setattr(service.ranking_store, "verify_mirror", lambda _value: calls.append("mirror") or mirror_valid)
    assert service.has_production_ranking(29) is (not rolled_back and mirror_valid)
    assert calls == (["rollback"] if rolled_back else ["rollback", "mirror"])


def test_collect_sources_isolates_missing_waiting_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    inputs = tuple(
        maintenance._SourceInput({}, index, f"2026-08-{index:02d}", "x", "x", tmp_path / str(index))
        for index in range(10, 14)
    )
    state = _state()
    state.source_inputs = inputs
    token = cast(maintenance.VerifiedJointExecutionSourceCorpus, _source(11, "2026-08-11"))
    pair = _pair(13, "2026-08-13")

    def source_token(item: maintenance._SourceInput, *_args: object, **_kwargs: object) -> object:
        if item.run_id == 10:
            return None
        if item.run_id == 12:
            raise RuntimeError("broken run")
        return token if item.run_id == 11 else pair.source

    monkeypatch.setattr(service, "_source_token", source_token)
    monkeypatch.setattr(
        service,
        "_mature_pair",
        lambda value, *_args, **_kwargs: None if value.run_id == 11 else pair,
    )

    service._collect_mature_sources(state, {})

    assert "official_signal_session_missing:2026-08-10" in state.blockers
    assert "official_h5_path_waiting:2026-08-11" in state.blockers
    assert len(state.source_tokens) == 2
    assert state.mature_pairs == [pair]
    assert state.failures and state.failures[0].startswith("run 12: RuntimeError")


def test_run_locked_covers_selection_and_authorization_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    source_input = maintenance._SourceInput(
        {}, 1, "2026-08-10", "2026-08-10T16:00:00+08:00", "2026-08-10T16:00:00+08:00", tmp_path / "one"
    )
    pair = _pair(1, "2026-08-10")
    study = SimpleNamespace(payload={"selection_qualified": True})
    monkeypatch.setattr(maintenance, "_canonical_source_inputs", lambda _directory: (source_input,))
    monkeypatch.setattr(
        service,
        "_collect_mature_sources",
        lambda state, _sessions: (state.source_tokens.append(pair.source), state.mature_pairs.append(pair)),
    )
    monkeypatch.setattr(maintenance, "_learning_corpus", lambda _pairs: object())
    monkeypatch.setattr(service, "_study_for_corpus", lambda *_args, **_kwargs: study)
    monkeypatch.setattr(service, "_replay_historical_rankings", lambda *_args: [])

    waiting = service._run_locked(datetime.fromisoformat("2026-08-23T16:00:00+08:00"))
    assert waiting.status == "selection_passed_waiting_authorization"
    assert "exact_probability_filter_authorization_not_pinned" in waiting.blockers

    service.authorization_digest = "a" * 64
    monkeypatch.setattr(service, "_authorized_maintenance", lambda _state: (_ for _ in ()).throw(ValueError("bad pin")))
    blocked = service._run_locked(datetime.fromisoformat("2026-08-23T16:00:00+08:00"))
    assert blocked.status == "authorization_or_deployment_blocked"
    assert "bad pin" in blocked.failures[0]


def test_ranking_maintenance_requires_shadow_and_explicit_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    state = _state()
    authorized = cast(
        maintenance._AuthorizedMaintenance,
        SimpleNamespace(oos_v3=object(), selected_pairs=(), study=object()),
    )
    failed_shadow = SimpleNamespace(qualified=False, integrity_digest="a" * 64)
    monkeypatch.setattr(service, "_ranking_shadow_for_oos", lambda *_args, **_kwargs: failed_shadow)
    result = service._ranking_maintenance(state, authorized)
    assert result.status == "shadow_gates_failed"

    state = _state()
    qualified_shadow = SimpleNamespace(qualified=True, integrity_digest="b" * 64)
    monkeypatch.setattr(service, "_ranking_shadow_for_oos", lambda *_args, **_kwargs: qualified_shadow)
    result = service._ranking_maintenance(state, authorized)
    assert result.status == "waiting_explicit_human_promotion"

    service.ranking_control_digest = "c" * 64
    state = _state(ranking_control=True)
    monkeypatch.setattr(maintenance, "_load_pinned_authorization", lambda *_args: {"payload": {}, "integrity": {"integrity_digest": "c" * 64}})
    control = SimpleNamespace(action="rollback", integrity_digest="c" * 64)
    monkeypatch.setattr(maintenance, "verify_probability_ranking_manual_control_artifact", lambda *_args, **_kwargs: control)
    monkeypatch.setattr(service, "_publish_envelope", lambda *_args, **_kwargs: tmp_path / "control.json.gz")
    result = service._ranking_maintenance(state, authorized)
    assert result.status == "rollback_active"
    assert cast(_RankingStore, service.ranking_store).rollbacks == [control]

    control.action = "promote"
    result = service._ranking_maintenance(_state(ranking_control=True), authorized)
    assert result.status == "promotion_verified_waiting_new_official_batch"

    monkeypatch.setattr(service, "_ranking_shadow_for_oos", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("tamper")))
    state = _state()
    result = service._ranking_maintenance(state, authorized)
    assert result.status == "shadow_or_control_verification_failed"
    assert "tamper" in state.failures[0]


def test_publish_current_ranking_obeys_manual_effective_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    state = _state()
    authorized = cast(maintenance._AuthorizedMaintenance, SimpleNamespace(study=object(), deployment=object()))
    source = cast(maintenance.VerifiedJointExecutionSourceCorpus, _source(9, "2026-08-23"))
    predictions = cast(maintenance.VerifiedJointExecutionCurrentPredictionCorpus, SimpleNamespace())
    assert service._publish_current_ranking(
        state,
        authorized,
        maintenance._RankingMaintenance(None, None, "waiting"),
        source,
        predictions,
    ) == (None, "waiting")

    control = SimpleNamespace(action="promote", effective_after_run_id=9)
    result = service._publish_current_ranking(
        state,
        authorized,
        maintenance._RankingMaintenance(None, cast(maintenance.VerifiedProbabilityRankingManualControl, control), "promoted"),
        source,
        predictions,
    )
    assert result[0] is None
    assert "requires_post_promotion_run" in state.blockers[-1]

    source.run_id = 10
    publication = SimpleNamespace(run_id=10, artifact_digest="f" * 64)
    monkeypatch.setattr(service, "_ranking_publication_for_tokens", lambda *_args, **_kwargs: (publication, tmp_path / "rank.json.gz"))
    result = service._publish_current_ranking(
        state,
        authorized,
        maintenance._RankingMaintenance(None, cast(maintenance.VerifiedProbabilityRankingManualControl, control), "promoted"),
        source,
        predictions,
    )
    assert result == (publication, "production_ranking_ready")
    assert service._ranking_publications[10] is publication

    monkeypatch.setattr(service, "_ranking_publication_for_tokens", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("write failed")))
    assert service._publish_current_ranking(
        _state(),
        authorized,
        maintenance._RankingMaintenance(None, cast(maintenance.VerifiedProbabilityRankingManualControl, control), "promoted"),
        source,
        predictions,
    )[1] == "production_ranking_publication_failed"


def test_pair_selection_current_source_and_projection_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = (_pair(1, "2026-08-20"), _pair(2, "2026-08-21"), _pair(3, "2026-08-22"))
    selected = maintenance._deployment_pairs((pairs[0],), pairs)
    assert [item.source.run_id for item in selected] == [1, 2, 3]
    monkeypatch.setattr(maintenance, "_learning_corpus", lambda items: SimpleNamespace(bindings=[{"run_id": item.source.run_id} for item in items]))
    exact = maintenance._pairs_for_exact_bindings(
        [{"run_id": 1}, {"run_id": 3}],
        pairs,
        label="bindings",
    )
    assert [item.source.run_id for item in exact] == [1, 3]
    for invalid in (None, [], [{"run_id": True}], [{"run_id": 1}, {"run_id": 1}], [{"run_id": 99}]):
        with pytest.raises(maintenance.JointExecutionProbabilityError):
            maintenance._pairs_for_exact_bindings(invalid, pairs, label="bindings")

    current = maintenance._current_prediction_source(
        [
            cast(maintenance.VerifiedJointExecutionSourceCorpus, _source(4, "2026-08-23")),
            cast(maintenance.VerifiedJointExecutionSourceCorpus, _source(5, "2026-08-23")),
        ],
        latest_training_session="2026-08-22",
        generated_at="2026-08-23T16:00:00+08:00",
    )
    assert current is not None and current.run_id == 5
    assert maintenance._current_prediction_source(
        [cast(maintenance.VerifiedJointExecutionSourceCorpus, _source(6, "2026-08-22"))],
        latest_training_session="2026-08-22",
        generated_at="2026-08-23T16:00:00+08:00",
    ) is None
    with pytest.raises(maintenance.ProbabilityRankingError):
        maintenance._historical_ranking_source({}, {}, 1)
    assert maintenance._historical_ranking_source(
        {"joint_source_artifact_digest": pairs[0].source.artifact_digest},
        {1: pairs[0].source},
        1,
    ) is pairs[0].source

    assert maintenance._interval_projection([0.1, 0.2], semantics="test")["lower"] == 0.1
    with pytest.raises(maintenance.JointExecutionProbabilityError):
        maintenance._interval_projection([0.1], semantics="test")
    unavailable = maintenance._not_generated_joint_projection(
        7,
        {"status": "waiting", "blockers": ["x"]},
    )
    assert unavailable["run_id"] == 7 and unavailable["limitations"] == ["x"]
    with pytest.raises(maintenance.JointExecutionProbabilityError):
        maintenance._mapping([], "value")
    with pytest.raises(ValueError, match="timezone"):
        maintenance._timestamp("2026-08-23T16:00:00")
    assert "ValueError: spaced message" == maintenance._short_error(
        ValueError(" spaced   message ")
    )


def test_managed_artifact_io_is_canonical_immutable_and_unambiguous(tmp_path: Path) -> None:
    service = _service(tmp_path)
    encoded = maintenance.canonical_json_bytes({"a": 1})
    path = service._publish_encoded(tmp_path / "managed", "artifact-a.json.gz", encoded)
    assert maintenance._read_gzip_json(path) == {"a": 1}
    assert maintenance._single_managed_artifact(tmp_path / "managed", "*.json.gz") == {"a": 1}
    with pytest.raises(ValueError, match="unsafe"):
        service._publish_encoded(tmp_path, "../bad.json.gz", encoded)
    service._publish_encoded(tmp_path / "managed", "artifact-b.json.gz", encoded)
    with pytest.raises(maintenance.JointExecutionProbabilityError, match="ambiguous"):
        maintenance._single_managed_artifact(tmp_path / "managed", "*.json.gz")
    assert maintenance._single_managed_artifact(tmp_path / "missing", "*.json.gz") is None
    path.write_bytes(b"not-gzip")
    with pytest.raises(maintenance.JointExecutionProbabilityError, match="cannot be read"):
        maintenance._read_gzip_json(path)


def test_source_and_outcome_artifacts_build_once_then_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    service.cache.verified_market_scan_read = lambda _run_id: nullcontext(
        SimpleNamespace(execution_session_evidence=lambda: {"execution": "sealed"})
    )
    source_input = maintenance._SourceInput(
        {"payload": {}},
        7,
        "2026-08-10",
        "2026-08-10T16:00:00+08:00",
        "2026-08-10T16:00:00+08:00",
        tmp_path / "source.json.gz",
    )
    official = cast(maintenance.VerifiedOfficialExecutionSession, SimpleNamespace())
    token = cast(
        maintenance.VerifiedJointExecutionSourceCorpus,
        _source(7, "2026-08-10"),
    )
    token.artifact_digest = "a" * 64
    built: list[object] = []
    monkeypatch.setattr(
        maintenance,
        "build_joint_execution_source_artifact",
        lambda *args, **kwargs: {"source": "artifact"},
    )
    monkeypatch.setattr(
        maintenance,
        "replay_and_verify_joint_execution_source_artifact",
        lambda artifact, *_args: built.append(artifact) or token,
    )
    monkeypatch.setattr(
        service,
        "_publish_encoded",
        lambda directory, name, encoded: directory / name,
    )

    assert service._source_token(
        source_input,
        {"2026-08-10": official},
        generated_at="2026-08-23T16:00:00+08:00",
    ) is token
    assert built == [{"source": "artifact"}]
    assert service._source_token(
        source_input,
        {},
        generated_at="2026-08-23T16:00:00+08:00",
    ) is None

    monkeypatch.setattr(maintenance, "_single_managed_artifact", lambda *_args: {"stored": True})
    assert service._source_token(
        source_input,
        {"2026-08-10": official},
        generated_at="2026-08-23T16:00:00+08:00",
    ) is token
    assert built[-1] == {"stored": True}

    required_dates = tuple(f"2026-08-{day:02d}" for day in range(11, 17))
    monkeypatch.setattr(
        maintenance,
        "next_trade_dates",
        lambda *_args: tuple(datetime.fromisoformat(item).date() for item in required_dates),
    )
    monkeypatch.setattr(maintenance, "_single_managed_artifact", lambda *_args: None)
    assert service._mature_pair(
        token,
        {},
        generated_at="2026-08-23T16:00:00+08:00",
    ) is None
    outcome = cast(
        maintenance.VerifiedJointExecutionOutcomeCorpus,
        SimpleNamespace(artifact_digest="b" * 64),
    )
    monkeypatch.setattr(
        maintenance,
        "build_joint_execution_outcome_artifact",
        lambda *_args, **_kwargs: {"outcome": "artifact"},
    )
    replayed: list[object] = []
    monkeypatch.setattr(
        maintenance,
        "replay_and_verify_joint_execution_outcome_artifact",
        lambda artifact, *_args: replayed.append(artifact) or outcome,
    )
    sessions = {item: official for item in required_dates}
    pair = service._mature_pair(
        token,
        sessions,
        generated_at="2026-08-23T16:00:00+08:00",
    )
    assert pair is not None and pair.outcome is outcome
    monkeypatch.setattr(maintenance, "_single_managed_artifact", lambda *_args: {"stored": "outcome"})
    assert service._mature_pair(
        token,
        sessions,
        generated_at="2026-08-23T16:00:00+08:00",
    ).outcome is outcome
    assert replayed[-1] == {"stored": "outcome"}


def test_selected_and_cached_studies_require_exact_digest_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    pairs = (_pair(1, "2026-08-10"), _pair(2, "2026-08-11"))
    digest = "a" * 64
    artifact = {
        "generated_at": "2026-08-20T16:00:00+08:00",
        "input_digest": "corpus",
        "evidence_digest": digest,
        "source_bindings": [{"run_id": 1}],
    }
    corpus = SimpleNamespace(corpus_digest="corpus")
    study = SimpleNamespace(evidence_digest=digest, payload={"selection_qualified": True})
    monkeypatch.setattr(maintenance, "_read_gzip_json", lambda _path: artifact)
    monkeypatch.setattr(maintenance, "_learning_corpus", lambda _pairs: corpus)
    monkeypatch.setattr(
        maintenance,
        "replay_and_verify_joint_execution_probability_evidence",
        lambda *_args: study,
    )

    selected = service._selected_study(
        {"payload": {"evidence_binding": {"evidence_digest": digest}}},
        pairs,
    )
    assert selected[0] is study and selected[2] == (pairs[0],)
    with pytest.raises(maintenance.JointExecutionProbabilityError, match="digest is invalid"):
        service._selected_study(
            {"payload": {"evidence_binding": {"evidence_digest": "bad"}}},
            pairs,
        )
    artifact["source_bindings"] = [{"run_id": 99}]
    with pytest.raises(maintenance.JointExecutionProbabilityError, match="no longer available"):
        service._selected_study(
            {"payload": {"evidence_binding": {"evidence_digest": digest}}},
            pairs,
        )
    artifact["source_bindings"] = [{"run_id": 1}]
    study.evidence_digest = "b" * 64
    with pytest.raises(maintenance.JointExecutionProbabilityError, match="does not replay"):
        service._selected_study(
            {"payload": {"evidence_binding": {"evidence_digest": digest}}},
            pairs,
        )

    study.evidence_digest = digest
    path = tmp_path / f"joint-execution-study-{digest}.json.gz"
    monkeypatch.setattr(maintenance, "_managed_artifact_paths", lambda *_args: (path,))
    assert service._study_for_corpus(
        cast(maintenance.VerifiedJointExecutionLearningCorpus, corpus),
        generated_at="2026-08-23T16:00:00+08:00",
    ) is study
    artifact["input_digest"] = "other"
    built = {"evidence_digest": "c" * 64}
    built_study = SimpleNamespace(evidence_digest="c" * 64)
    monkeypatch.setattr(maintenance, "fit_joint_execution_probability", lambda *_args, **_kwargs: built)
    monkeypatch.setattr(
        maintenance,
        "replay_and_verify_joint_execution_probability_evidence",
        lambda value, _corpus: built_study if value is built else study,
    )
    published: list[object] = []
    monkeypatch.setattr(service, "_publish_study", lambda value: published.append(value))
    assert service._study_for_corpus(
        cast(maintenance.VerifiedJointExecutionLearningCorpus, corpus),
        generated_at="2026-08-23T16:00:00+08:00",
    ) is built_study
    assert published == [built]


def test_shadow_evaluation_reuses_exact_corpus_or_builds_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    oos = SimpleNamespace(integrity_digest="a" * 64)
    study = SimpleNamespace(evidence_digest="b" * 64)
    token = SimpleNamespace(integrity_digest="c" * 64, qualified=True)
    artifact = {
        "generated_at": "2026-08-23T15:00:00+08:00",
        "payload": {
            "oos_corpus_digest": oos.integrity_digest,
            "study_evidence_digest": study.evidence_digest,
        },
    }
    path = tmp_path / f"probability-ranking-shadow-{token.integrity_digest}.json.gz"
    monkeypatch.setattr(maintenance, "_managed_artifact_paths", lambda *_args: (path,))
    monkeypatch.setattr(maintenance, "_read_gzip_json", lambda _path: artifact)
    monkeypatch.setattr(
        maintenance,
        "verify_probability_ranking_shadow_artifact",
        lambda *_args, **_kwargs: token,
    )
    assert service._ranking_shadow_for_oos(
        cast(maintenance.VerifiedJointExecutionProbabilityCorpusV3, oos),
        [],
        cast(maintenance.VerifiedJointExecutionProbabilityStudy, study),
        generated_at="2026-08-23T16:00:00+08:00",
    ) is token

    artifact["payload"]["oos_corpus_digest"] = "other"
    built = {"integrity": {"integrity_digest": "d" * 64}}
    built_token = SimpleNamespace(integrity_digest="d" * 64, qualified=False)
    monkeypatch.setattr(maintenance, "build_probability_ranking_shadow_artifact", lambda *_args, **_kwargs: built)
    monkeypatch.setattr(
        maintenance,
        "verify_probability_ranking_shadow_artifact",
        lambda value, **_kwargs: built_token if value is built else token,
    )
    monkeypatch.setattr(service, "_publish_envelope", lambda *_args, **_kwargs: tmp_path / "built.json.gz")
    assert service._ranking_shadow_for_oos(
        cast(maintenance.VerifiedJointExecutionProbabilityCorpusV3, oos),
        [],
        cast(maintenance.VerifiedJointExecutionProbabilityStudy, study),
        generated_at="2026-08-23T16:00:00+08:00",
    ) is built_token


def test_authorized_maintenance_binds_oos_authorization_and_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path, authorization_digest="a" * 64)
    state = _state(authorization=True)
    state.mature_pairs = [_pair(1, "2026-08-10"), _pair(2, "2026-08-11")]
    authorization_artifact = {"payload": {}, "integrity": {"integrity_digest": "a" * 64}}
    study = SimpleNamespace(payload={"selection_qualified": True})
    corpus = SimpleNamespace(corpus_digest="selected")
    oos = SimpleNamespace(integrity_digest="b" * 64)
    authorization = SimpleNamespace(integrity_digest="a" * 64)
    deployment = SimpleNamespace(integrity_digest="c" * 64)
    monkeypatch.setattr(maintenance, "_load_pinned_authorization", lambda *_args: authorization_artifact)
    monkeypatch.setattr(
        service,
        "_selected_study",
        lambda *_args: (study, corpus, tuple(state.mature_pairs[:1])),
    )
    monkeypatch.setattr(maintenance, "build_joint_execution_probability_oos_corpus_v3", lambda *_args: oos)
    monkeypatch.setattr(maintenance, "encode_joint_execution_probability_corpus_v3", lambda _value: b"oos")
    monkeypatch.setattr(
        maintenance,
        "verify_probability_filter_authorization_artifact",
        lambda *_args: authorization,
    )
    monkeypatch.setattr(maintenance, "_deployment_pairs", lambda *_args: tuple(state.mature_pairs))
    monkeypatch.setattr(maintenance, "_learning_corpus", lambda _pairs: SimpleNamespace(corpus_digest="deployment"))
    monkeypatch.setattr(service, "_deployment_for_corpus", lambda *_args, **_kwargs: deployment)
    published: list[str] = []
    monkeypatch.setattr(
        service,
        "_publish_encoded",
        lambda directory, name, encoded: published.append(name) or directory / name,
    )
    monkeypatch.setattr(
        service,
        "_publish_envelope",
        lambda _directory, prefix, _artifact: published.append(prefix) or tmp_path / prefix,
    )

    result = service._authorized_maintenance(state)

    assert result.study is study
    assert result.oos_v3 is oos
    assert result.authorization is authorization
    assert result.deployment is deployment
    assert any(name.startswith("joint-execution-oos-v3-") for name in published)
    assert "probability-filter-authorization" in published


def test_deployment_current_and_ranking_artifact_caches_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    corpus = SimpleNamespace(corpus_digest="corpus")
    study = SimpleNamespace(evidence_digest="a" * 64)
    authorization = SimpleNamespace(integrity_digest="b" * 64)
    deployment = SimpleNamespace(integrity_digest="c" * 64)
    deployment_artifact = {
        "generated_at": "2026-08-23T15:00:00+08:00",
        "payload": {
            "corpus_digest": "corpus",
            "study_evidence_digest": "a" * 64,
            "authorization_digest": "b" * 64,
        },
    }
    deployment_path = tmp_path / f"joint-execution-deployment-{deployment.integrity_digest}.json.gz"
    monkeypatch.setattr(maintenance, "_managed_artifact_paths", lambda _directory, pattern: (deployment_path,) if pattern.startswith("joint-execution-deployment") else ())
    monkeypatch.setattr(maintenance, "_read_gzip_json", lambda _path: deployment_artifact)
    monkeypatch.setattr(maintenance, "verify_joint_execution_deployment_artifact", lambda *_args, **_kwargs: deployment)
    monkeypatch.setattr(maintenance, "joint_execution_deployment_is_fresh", lambda *_args, **_kwargs: True)
    assert service._deployment_for_corpus(
        cast(maintenance.VerifiedJointExecutionLearningCorpus, corpus),
        cast(maintenance.VerifiedJointExecutionProbabilityStudy, study),
        cast(maintenance.VerifiedProbabilityFilterAuthorization, authorization),
        generated_at="2026-08-23T16:00:00+08:00",
    ) is deployment

    deployment_artifact["payload"]["corpus_digest"] = "other"
    built_deployment_artifact = {"integrity": {"integrity_digest": "d" * 64}}
    built_deployment = SimpleNamespace(integrity_digest="d" * 64)
    monkeypatch.setattr(maintenance, "fit_joint_execution_deployment_estimator", lambda *_args, **_kwargs: built_deployment_artifact)
    monkeypatch.setattr(
        maintenance,
        "verify_joint_execution_deployment_artifact",
        lambda value, **_kwargs: built_deployment if value is built_deployment_artifact else deployment,
    )
    monkeypatch.setattr(service, "_publish_envelope", lambda *_args, **_kwargs: tmp_path / "deployment.json.gz")
    assert service._deployment_for_corpus(
        cast(maintenance.VerifiedJointExecutionLearningCorpus, corpus),
        cast(maintenance.VerifiedJointExecutionProbabilityStudy, study),
        cast(maintenance.VerifiedProbabilityFilterAuthorization, authorization),
        generated_at="2026-08-23T16:00:00+08:00",
    ) is built_deployment

    source = cast(maintenance.VerifiedJointExecutionSourceCorpus, _source(7, "2026-08-23"))
    source.artifact_digest = "e" * 64
    predictions = SimpleNamespace(
        artifact_digest="f" * 64,
        generated_at="2026-08-23T15:30:00+08:00",
    )
    current_artifact = {
        "payload": {
            "run": {"source_artifact_digest": source.artifact_digest},
            "study_evidence_digest": study.evidence_digest,
            "deployment_artifact_digest": deployment.integrity_digest,
        }
    }
    current_path = tmp_path / f"joint-execution-current-run-7-{predictions.artifact_digest}.json.gz"
    monkeypatch.setattr(maintenance, "_managed_artifact_paths", lambda _directory, pattern: (current_path,) if pattern.startswith("joint-execution-current") else ())
    monkeypatch.setattr(maintenance, "_read_gzip_json", lambda _path: current_artifact)
    monkeypatch.setattr(maintenance, "verify_joint_execution_current_prediction_artifact", lambda *_args, **_kwargs: predictions)
    assert service._current_prediction_for_source(
        source,
        cast(maintenance.VerifiedJointExecutionProbabilityStudy, study),
        cast(maintenance.VerifiedJointExecutionDeploymentEstimator, deployment),
        generated_at="2026-08-23T16:00:00+08:00",
    ) is predictions

    current_artifact["payload"]["study_evidence_digest"] = "other"
    built_current_artifact = {"integrity": {"integrity_digest": "1" * 64}}
    built_predictions = SimpleNamespace(artifact_digest="1" * 64)
    monkeypatch.setattr(maintenance, "build_joint_execution_current_prediction_artifact", lambda *_args, **_kwargs: built_current_artifact)
    monkeypatch.setattr(
        maintenance,
        "verify_joint_execution_current_prediction_artifact",
        lambda value, **_kwargs: built_predictions if value is built_current_artifact else predictions,
    )
    assert service._current_prediction_for_source(
        source,
        cast(maintenance.VerifiedJointExecutionProbabilityStudy, study),
        cast(maintenance.VerifiedJointExecutionDeploymentEstimator, deployment),
        generated_at="2026-08-23T16:00:00+08:00",
    ) is built_predictions

    promotion = SimpleNamespace(integrity_digest="2" * 64)
    publication = SimpleNamespace(
        artifact_digest="3" * 64,
        generated_at="2026-08-23T15:45:00+08:00",
    )
    ranking_artifact = {
        "payload": {
            "joint_source_artifact_digest": source.artifact_digest,
            "current_prediction_artifact_digest": predictions.artifact_digest,
            "study_evidence_digest": study.evidence_digest,
            "deployment_artifact_digest": deployment.integrity_digest,
            "promotion_digest": promotion.integrity_digest,
        }
    }
    ranking_path = tmp_path / f"probability-ranking-v6-run-7-{publication.artifact_digest}.json.gz"
    monkeypatch.setattr(maintenance, "_managed_artifact_paths", lambda _directory, pattern: (ranking_path,) if pattern.startswith("probability-ranking-v6") else ())
    monkeypatch.setattr(maintenance, "_read_gzip_json", lambda _path: ranking_artifact)
    monkeypatch.setattr(maintenance, "verify_probability_ranking_publication_artifact", lambda *_args, **_kwargs: publication)
    cached, cached_path = service._ranking_publication_for_tokens(
        source,
        cast(maintenance.VerifiedJointExecutionCurrentPredictionCorpus, predictions),
        cast(maintenance.VerifiedJointExecutionProbabilityStudy, study),
        cast(maintenance.VerifiedJointExecutionDeploymentEstimator, deployment),
        cast(maintenance.VerifiedProbabilityRankingManualControl, promotion),
        generated_at="2026-08-23T16:00:00+08:00",
    )
    assert cached is publication and cached_path == ranking_path

    ranking_artifact["payload"]["promotion_digest"] = "other"
    built_ranking_artifact = {"integrity": {"integrity_digest": "4" * 64}}
    built_publication = SimpleNamespace(artifact_digest="4" * 64)
    monkeypatch.setattr(maintenance, "build_probability_ranking_publication_artifact", lambda *_args, **_kwargs: built_ranking_artifact)
    monkeypatch.setattr(
        maintenance,
        "verify_probability_ranking_publication_artifact",
        lambda value, **_kwargs: built_publication if value is built_ranking_artifact else publication,
    )
    built, built_path = service._ranking_publication_for_tokens(
        source,
        cast(maintenance.VerifiedJointExecutionCurrentPredictionCorpus, predictions),
        cast(maintenance.VerifiedJointExecutionProbabilityStudy, study),
        cast(maintenance.VerifiedJointExecutionDeploymentEstimator, deployment),
        cast(maintenance.VerifiedProbabilityRankingManualControl, promotion),
        generated_at="2026-08-23T16:00:00+08:00",
    )
    assert built is built_publication and built_path == tmp_path / "deployment.json.gz"


def test_current_summary_waits_for_new_source_then_publishes_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    state = _state(authorization=True)
    current_source = cast(maintenance.VerifiedJointExecutionSourceCorpus, _source(9, "2026-08-23"))
    prior = _pair(1, "2026-08-20")
    state.source_tokens = [current_source]
    state.source_inputs = (
        maintenance._SourceInput({}, 9, "2026-08-23", "x", "x", tmp_path / "source"),
    )
    predictions = SimpleNamespace(run_id=9, __len__=lambda self: 2)
    authorized = cast(
        maintenance._AuthorizedMaintenance,
        SimpleNamespace(
            study=SimpleNamespace(),
            authorization=SimpleNamespace(),
            deployment=SimpleNamespace(),
            deployment_pairs=(prior,),
        ),
    )
    ranking = maintenance._RankingMaintenance(None, None, "waiting")
    monkeypatch.setattr(maintenance, "_current_prediction_source", lambda *_args, **_kwargs: None)
    waiting = service._current_maintenance_summary(state, authorized, ranking)
    assert waiting.status == "deployment_ready_waiting_new_official_batch"
    assert "new_same_session_official_source_not_available" in waiting.blockers

    monkeypatch.setattr(maintenance, "_current_prediction_source", lambda *_args, **_kwargs: current_source)
    monkeypatch.setattr(service, "_current_prediction_for_source", lambda *_args, **_kwargs: predictions)
    monkeypatch.setattr(service, "_publish_current_ranking", lambda *_args, **_kwargs: (None, "waiting"))
    monkeypatch.setattr(
        service,
        "_ready_summary",
        lambda *_args, **_kwargs: service._summary(
            status="current_prediction_ready",
            generated_at=state.generated_at,
            official_status=state.official_status,
            current_prediction_run_id=9,
            current_prediction_count=2,
            selection_qualified=True,
            authorization_configured=True,
            authorization_verified=True,
            deployment_verified=True,
        ),
    )
    ready = service._current_maintenance_summary(state, authorized, ranking)
    assert ready.status == "current_prediction_ready"
    assert service._current_authority is not None


def test_current_probability_projection_preserves_exact_authority_and_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        {
            "symbol": "600519.SH",
            "probability": 0.6,
            "calibration_bias_interval": [-0.02, 0.03],
            "calibration_adjusted_probability_interval": [0.58, 0.63],
            "record_digest": "a" * 64,
        },
        {
            "symbol": "300750.SZ",
            "probability": 0.55,
            "calibration_bias_interval": [-0.03, 0.04],
            "calibration_adjusted_probability_interval": [0.52, 0.59],
            "record_digest": "b" * 64,
        },
    ]
    predictions = joint_probability.VerifiedJointExecutionCurrentPredictionCorpus(
        maintenance.canonical_json_bytes(records).decode("utf-8"),
        artifact_digest="c" * 64,
        deployment_artifact_digest="d" * 64,
        decision_identity_digest="e" * 64,
        decision_membership_digest="f" * 64,
        generated_at="2026-08-23T16:00:00+08:00",
        run_id=9,
        signal_session="2026-08-23",
        source_artifact_digest="1" * 64,
        source_snapshot_digest="2" * 64,
        _seal=joint_probability._VERIFIED_CURRENT_PREDICTION_SEAL,  # noqa: SLF001
    )
    evidence = {
        name: None for name in maintenance._CURRENT_RESEARCH_SUMMARY_KEYS  # noqa: SLF001
    }
    evidence.update(
        {
            "schema_version": "joint-v1",
            "status": "calibrated_shadow",
            "fit_status": "fitted_oos_three_component",
            "selection_qualified": True,
            "generated_at": "2026-08-23T15:00:00+08:00",
            "evidence_digest": "3" * 64,
        }
    )
    source_artifact = {
        "payload": {
            "run": {
                "mode": "official",
                "scope": "all-a-shares",
                "rule_version": "full-market-scan-v6:abc123",
                "quote_date": "2026-08-23",
                "data_date": "2026-08-23",
                "production_score_rule_version": "full-market-score-v5",
                "production_score_spec_hash": "4" * 64,
            },
            "cohort": {"mode": "official"},
        },
        "integrity": {"integrity_digest": "5" * 64},
    }
    authority = cast(
        maintenance._CurrentAuthority,
        SimpleNamespace(
            source_input=maintenance._SourceInput(
                source_artifact,
                9,
                "2026-08-23",
                "2026-08-23T15:00:00+08:00",
                "2026-08-23T15:00:00+08:00",
                Path("source.json.gz"),
            ),
            source=SimpleNamespace(
                source_snapshot_digest="2" * 64,
                artifact_digest="1" * 64,
                decision_identity_digest="e" * 64,
                decision_membership_digest="f" * 64,
            ),
            study=SimpleNamespace(payload=evidence),
            authorization=SimpleNamespace(),
            deployment=SimpleNamespace(
                payload={
                    "generated_at": "2026-08-23T15:30:00+08:00",
                    "training_cutoff": "2026-08-20",
                    "calibration_cutoff": "2026-08-22",
                },
                integrity_digest="d" * 64,
            ),
            predictions=predictions,
        ),
    )
    monkeypatch.setattr(
        maintenance,
        "build_probability_filter_qualification",
        lambda *_args: {"qualified": True, "failed_gates": []},
    )
    monkeypatch.setattr(
        maintenance,
        "probability_filter_qualified",
        lambda *_args: True,
    )

    projection = maintenance._current_research_projection(authority)  # noqa: SLF001
    assert projection["filter_qualified"] is True
    assert projection["run_binding"]["run_id"] == 9  # type: ignore[index]
    summary = projection["horizons"]["5"]["net_excess_positive"]  # type: ignore[index]
    assert summary["current_prediction_count"] == 2
    all_rows = maintenance._current_record_projection(  # noqa: SLF001
        predictions,
        symbols=None,
    )
    assert set(all_rows) == {"600519.SH", "300750.SZ"}
    selected = maintenance._current_record_projection(  # noqa: SLF001
        predictions,
        symbols=["600519.SH"],
    )
    details = selected["600519.SH"]["5"]["net_excess_positive"]
    assert details["filter_qualified"] is True
    assert details["calibration_bias_interval"]["lower"] == -0.02  # type: ignore[index]


def test_pinned_and_managed_artifact_io_rejects_digest_and_canonicality_errors(
    tmp_path: Path,
) -> None:
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_bytes(
        maintenance.canonical_json_bytes(
            {"integrity": {"integrity_digest": "a" * 64}}
        )
    )
    assert maintenance._load_pinned_authorization(  # noqa: SLF001
        authorization_path,
        "a" * 64,
    )["integrity"]["integrity_digest"] == "a" * 64  # type: ignore[index]
    with pytest.raises(maintenance.JointExecutionProbabilityError, match="digest is invalid"):
        maintenance._load_pinned_authorization(authorization_path, "bad")  # noqa: SLF001
    with pytest.raises(maintenance.JointExecutionProbabilityError, match="pinned digest"):
        maintenance._load_pinned_authorization(authorization_path, "b" * 64)  # noqa: SLF001

    managed = tmp_path / "managed"
    managed.mkdir()
    noncanonical = managed / "noncanonical.json.gz"
    noncanonical.write_bytes(gzip.compress(b'{ "a": 1 }', mtime=0))
    with pytest.raises(maintenance.JointExecutionProbabilityError, match="canonical JSON"):
        maintenance._read_gzip_json(noncanonical)  # noqa: SLF001
    assert maintenance._managed_artifact_paths(  # noqa: SLF001
        tmp_path / "missing",
        "*.json.gz",
    ) == ()
