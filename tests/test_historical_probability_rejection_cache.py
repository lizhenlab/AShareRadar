"""Exercise rejection caching through real compact-file and source-byte checks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.artifacts.io import ArtifactReadError, canonical_json_bytes
from app.services import market_scan_probability_historical_context as context
from tests import test_market_scan_probability_historical_context as support


def _published_pair(tmp_path, monkeypatch):
    source = support._source_path(tmp_path)
    source.write_bytes(b"{}")
    # Only the offline builder's expensive fit is synthesized. Runtime integrity,
    # payload contracts, path identity, source size and SHA checks remain real.
    with monkeypatch.context() as builder_patch:
        builder_patch.setattr(context, "verify_historical_replay_artifact", lambda _: support._verified_replay())
        target = context.publish_historical_probability_context(source, tmp_path)
    return target, source


def _supersede(target):
    artifact = json.loads(target.read_bytes())
    artifact["payload"]["source_artifact"]["schema_version"] = context.HISTORICAL_REPLAY_SUPERSEDED_ARTIFACT_SCHEMA_VERSION
    digest = hashlib.sha256(canonical_json_bytes({key: value for key, value in artifact.items() if key != "integrity"})).hexdigest()
    artifact["integrity"]["integrity_digest"] = digest
    replacement = target.with_name(f"{context.HISTORICAL_CONTEXT_PREFIX}-{digest}.json")
    replacement.write_bytes(canonical_json_bytes(artifact))
    target.unlink()
    return replacement


def _watch_loader(monkeypatch):
    loader = Mock(wraps=context._load_newest_projection)
    monkeypatch.setattr(context, "_load_newest_projection", loader)
    return loader


def _assert_unavailable(store):
    projection = store.research_projection()
    assert projection["status"] == "unavailable"
    assert projection["availability"] == "historical_context_integrity_unavailable"
    assert projection["sample"] is None and projection["source_artifact"] is None
    assert projection["generated_at"] is None and projection["cohort"] is None
    assert projection["horizons"] == {}
    assert projection["selection_qualified"] is False and projection["filter_qualified"] is False
    assert projection["production_ranking_effect"] == "none"
    assert not store.refresh_pending()


def test_superseded_contract_is_cached_only_as_an_unavailable_verdict(tmp_path, monkeypatch):
    target, source = _published_pair(tmp_path, monkeypatch)
    _supersede(target)
    loader = _watch_loader(monkeypatch)
    store = context.MarketScanHistoricalProbabilityContextStore(tmp_path)

    with pytest.raises(context.HistoricalProbabilityContextRejectedError, match="superseded-fit-contract") as rejected:
        store.preload()
    assert isinstance(rejected.value.__cause__, context.HistoricalProbabilityContextError)
    _assert_unavailable(store)
    assert store.preload() == 0
    assert store.preload() == 0
    assert loader.call_count == 1
    assert source.read_bytes() == b"{}"


@pytest.mark.parametrize("changed_file", ["compact", "bound_source"])
def test_rejected_snapshot_revalidates_even_when_size_and_mtime_are_preserved(tmp_path, monkeypatch, changed_file):
    target, source = _published_pair(tmp_path, monkeypatch)
    target = _supersede(target)
    loader = _watch_loader(monkeypatch)
    store = context.MarketScanHistoricalProbabilityContextStore(tmp_path)
    with pytest.raises(context.HistoricalProbabilityContextError):
        store.preload()
    _assert_unavailable(store)

    changed = target if changed_file == "compact" else source
    facts = changed.stat()
    original = changed.read_bytes()
    mutated = original.replace(b"2026-08-11", b"2026-08-12") if changed_file == "compact" else b"[]"
    assert mutated != original and len(mutated) == len(original)
    changed.write_bytes(mutated)
    os.utime(changed, ns=(facts.st_atime_ns, facts.st_mtime_ns))

    assert store.research_projection()["availability"] == "source_index_verification_pending"
    with pytest.raises(context.HistoricalProbabilityContextError):
        store.preload()
    assert loader.call_count == 2
    _assert_unavailable(store)
    assert store.preload() == 0 and loader.call_count == 2


def test_compact_digest_rejection_can_recover_after_the_actual_bytes_are_repaired(tmp_path, monkeypatch):
    target, _ = _published_pair(tmp_path, monkeypatch)
    valid = target.read_bytes()
    artifact = json.loads(valid)
    artifact["payload"]["selection_qualified"] = True
    target.write_bytes(canonical_json_bytes(artifact))
    loader = _watch_loader(monkeypatch)
    store = context.MarketScanHistoricalProbabilityContextStore(tmp_path)
    with pytest.raises(context.HistoricalProbabilityContextError, match="摘要不一致"):
        store.preload()
    _assert_unavailable(store)
    assert store.preload() == 0 and loader.call_count == 1

    target.write_bytes(valid)
    assert store.research_projection()["availability"] == "source_index_verification_pending"
    assert store.preload() == 1 and loader.call_count == 2
    assert store.research_projection()["status"] == "ready"
    assert all(value["probability"] is None for value in store.research_projection()["horizons"].values())


def test_bound_source_hash_change_hides_cached_projection_and_repair_revalidates(tmp_path, monkeypatch):
    _, source = _published_pair(tmp_path, monkeypatch)
    loader = _watch_loader(monkeypatch)
    store = context.MarketScanHistoricalProbabilityContextStore(tmp_path)
    assert store.preload() == 1
    source.write_bytes(b"[]")
    assert store.research_projection()["availability"] == "source_index_verification_pending"
    with pytest.raises(context.HistoricalProbabilityContextError, match="源文件摘要不一致"):
        store.preload()
    _assert_unavailable(store)
    assert store.preload() == 0 and loader.call_count == 2

    source.write_bytes(b"{}")
    assert store.research_projection()["availability"] == "source_index_verification_pending"
    assert store.preload() == 1 and loader.call_count == 3
    assert store.research_projection()["status"] == "ready"


def test_missing_source_never_commits_a_negative_snapshot_and_can_recover(tmp_path, monkeypatch):
    _, source = _published_pair(tmp_path, monkeypatch)
    source.unlink()
    loader = _watch_loader(monkeypatch)
    store = context.MarketScanHistoricalProbabilityContextStore(tmp_path)
    for attempt in (1, 2):
        with pytest.raises(context.HistoricalProbabilityContextError, match="源不可用") as retryable:
            store.preload()
        assert not isinstance(retryable.value, context.HistoricalProbabilityContextRejectedError)
        assert loader.call_count == attempt
        assert store.research_projection()["availability"] == "source_index_verification_pending"
        assert not store.refresh_pending()
    source.write_bytes(b"{}")
    assert store.preload() == 1 and loader.call_count == 3


@pytest.mark.parametrize("failed_file", ["compact", "bound_source"])
def test_temporary_read_error_retries_unchanged_files_and_never_caches_unavailable(tmp_path, monkeypatch, failed_file):
    target, source = _published_pair(tmp_path, monkeypatch)
    broken = target if failed_file == "compact" else source
    loader = _watch_loader(monkeypatch)
    store = context.MarketScanHistoricalProbabilityContextStore(tmp_path)
    original_read = context.read_regular_file

    def unavailable(path, **kwargs):
        if path == broken:
            raise ArtifactReadError(path) from PermissionError("temporary test denial")
        return original_read(path, **kwargs)

    with monkeypatch.context() as read_patch:
        read_patch.setattr(context, "read_regular_file", unavailable)
        for attempt in (1, 2):
            with pytest.raises(context.HistoricalProbabilityContextError) as retryable:
                store.preload()
            assert not isinstance(retryable.value, context.HistoricalProbabilityContextRejectedError)
            assert loader.call_count == attempt
            assert store.research_projection()["availability"] == "source_index_verification_pending"
    assert store.preload() == 1 and loader.call_count == 3


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_unsafe_file_alias_is_not_hidden_by_a_cached_rejection(tmp_path, monkeypatch, link_kind):
    target, source = _published_pair(tmp_path, monkeypatch)
    _supersede(target)
    store = context.MarketScanHistoricalProbabilityContextStore(tmp_path)
    with pytest.raises(context.HistoricalProbabilityContextError):
        store.preload()
    _assert_unavailable(store)
    alias_target = tmp_path / "untracked-replay-copy.json"
    source.rename(alias_target)
    if link_kind == "symlink":
        source.symlink_to(alias_target)
    else:
        os.link(alias_target, source)
    for operation in (store.research_projection, store.preload):
        with pytest.raises(context.HistoricalProbabilityContextError, match="单链接普通文件"):
            operation()
    source.unlink()
    alias_target.rename(source)
    with pytest.raises(context.HistoricalProbabilityContextError, match="superseded-fit-contract"):
        store.preload()
    _assert_unavailable(store)


def test_directory_alias_cannot_reuse_cached_rejection(tmp_path, monkeypatch):
    directory = tmp_path / "research"
    directory.mkdir()
    target, _ = _published_pair(directory, monkeypatch)
    _supersede(target)
    store = context.MarketScanHistoricalProbabilityContextStore(directory)
    with pytest.raises(context.HistoricalProbabilityContextError):
        store.preload()
    _assert_unavailable(store)
    moved = tmp_path / "relocated-research"
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    for operation in (store.research_projection, store.preload):
        with pytest.raises(context.HistoricalProbabilityContextError, match="路径别名"):
            operation()


@pytest.mark.parametrize("rejected_context", [False, True])
def test_files_changed_during_verification_commit_neither_success_nor_rejection(tmp_path, monkeypatch, rejected_context):
    target, source = _published_pair(tmp_path, monkeypatch)
    if rejected_context:
        target = _supersede(target)
    loader = _watch_loader(monkeypatch)
    store = context.MarketScanHistoricalProbabilityContextStore(tmp_path)
    original_read = context.read_regular_file

    def change_after_read(path: Path, **kwargs):
        result = original_read(path, **kwargs)
        if path == target:
            # Preserve length; both content rejection and apparent success must
            # lose their verdict if the complete input snapshot changed.
            source.write_bytes(b"{}")
        return result

    with monkeypatch.context() as read_patch:
        read_patch.setattr(context, "read_regular_file", change_after_read)
        with pytest.raises(context.HistoricalProbabilityContextError, match="读取期间发生变化") as retryable:
            store.preload()
        assert not isinstance(retryable.value, context.HistoricalProbabilityContextRejectedError)
    assert store.research_projection()["availability"] == "source_index_verification_pending"
    if rejected_context:
        with pytest.raises(context.HistoricalProbabilityContextError, match="superseded-fit-contract"):
            store.preload()
        _assert_unavailable(store)
    else:
        assert store.preload() == 1
        assert store.research_projection()["status"] == "ready"
    assert loader.call_count == 2
