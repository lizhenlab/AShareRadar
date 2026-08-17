from __future__ import annotations

from pathlib import Path

import pytest

from app.artifacts.io import ArtifactIOError
from app.services import individual_probability as service_module
from app.services.individual_probability import IndividualProbabilityStore
from app.services.individual_probability_artifact import (
    INDIVIDUAL_PROBABILITY_ASSESSMENT_PREFIX,
    IndividualProbabilityArtifactError,
    individual_probability_target_contract,
)
from app.models.individual_probability import IndividualProbabilityTargetContract


def test_store_rejects_directory_that_never_stabilizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = IndividualProbabilityStore(tmp_path)
    call_count = 0

    def changing_snapshot():
        nonlocal call_count
        call_count += 1
        marker = (f"candidate-{call_count}.json", 0, 0, 0, 0, 0, 0, "digest")
        return tmp_path, "primary", (marker,)

    monkeypatch.setattr(store, "_effective_snapshot", changing_snapshot)
    monkeypatch.setattr(store, "_load_latest", lambda _directory, _snapshot: {"stable": False})

    with pytest.raises(IndividualProbabilityArtifactError, match="读取期间持续变化"):
        store.latest()

    assert call_count == 6


def test_store_translates_safe_fingerprint_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / f"{INDIVIDUAL_PROBABILITY_ASSESSMENT_PREFIX}-{'a' * 64}.json"
    candidate.write_text("{}", encoding="utf-8")

    def fail_read(_path: Path, *, max_bytes: int) -> bytes:
        assert max_bytes > 0
        raise ArtifactIOError("simulated read race")

    monkeypatch.setattr(service_module, "read_regular_file", fail_read)

    with pytest.raises(IndividualProbabilityArtifactError, match="无法安全指纹化"):
        IndividualProbabilityStore(tmp_path).latest()


def test_store_rejects_candidate_replaced_by_directory_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / f"{INDIVIDUAL_PROBABILITY_ASSESSMENT_PREFIX}-{'b' * 64}.json"
    candidate.write_text("{}", encoding="utf-8")

    def replace_candidate(path: Path, *, max_bytes: int) -> bytes:
        assert max_bytes > 0
        encoded = path.read_bytes()
        path.unlink()
        path.mkdir()
        return encoded

    monkeypatch.setattr(service_module, "read_regular_file", replace_candidate)

    with pytest.raises(IndividualProbabilityArtifactError, match="非普通候选文件"):
        IndividualProbabilityStore(tmp_path).latest()


def test_real_directory_guard_rejects_missing_path() -> None:
    with pytest.raises(IndividualProbabilityArtifactError, match="目录不可访问"):
        service_module._require_real_directory(Path("/definitely/missing/individual-probability"))  # noqa: SLF001


def test_store_rejects_directory_beneath_symlinked_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    target = real / "artifacts"
    target.mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real, target_is_directory=True)
    directory = linked_parent / "artifacts"

    with pytest.raises(IndividualProbabilityArtifactError, match="符号链接"):
        IndividualProbabilityStore(directory).latest()


def test_assessment_projection_rejects_missing_generation_time() -> None:
    contract = IndividualProbabilityTargetContract.model_validate(
        individual_probability_target_contract()
    )

    with pytest.raises(IndividualProbabilityArtifactError, match="generated_at 无效"):
        service_module._assessment_report(  # noqa: SLF001
            "600519.SH",
            "2026-08-13T15:15:00+08:00",
            contract,
            {"generated_at": ""},
        )


def test_bound_compact_official_evidence_declares_runtime_replay_limitation() -> None:
    limitations = service_module._report_limitations(  # noqa: SLF001
        {"limitations": ["shadow_only"]},
        {"session_count": 288},
        source_contract_bound=True,
        runtime_source_replayed=False,
    )

    assert "official_pit_source_artifacts_not_runtime_replayed" in limitations
