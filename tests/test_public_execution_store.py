"""Raw replay, collection-time cutoffs, crash budgets and no execution promotion."""

from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, sha256_hex
from app.services import public_execution_store as store
from tests.test_public_execution_baostock import DAY, _envelope


NOW = datetime.fromisoformat("2026-09-20T02:00:00+00:00")


@pytest.fixture
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(store, "utc_now", lambda: NOW)
    def fetch(provider, day):
        calls.append((provider, day))
        return deepcopy(_envelope())
    monkeypatch.setattr(store, "_fetch", fetch)
    return calls


def test_raw_round_trip_and_repeated_collection_never_refetch(tmp_path, client):
    result = store.collect_public_execution_source(tmp_path, "baostock", DAY)
    assert result["status"] == "success" and result["row_count"] == 1
    raw = (tmp_path / "raw" / f'{result["digest"]}.json').read_bytes()
    assert sha256_hex(raw) == result["digest"]
    assert json.loads(raw)["envelope"] == _envelope()
    report = store.read_public_execution_source(tmp_path, "baostock", DAY, as_of=NOW)
    assert report["rows"][0]["reference_price"] == 9.5
    assert report["official_execution_admitted"] is False
    assert store.collect_public_execution_source(tmp_path, "baostock", DAY)["status"] == "cached"
    assert client == [("baostock", DAY)]


def test_retroactive_observation_cannot_appear_before_local_collection(tmp_path, client):
    store.collect_public_execution_source(tmp_path, "baostock", DAY)
    result = store.read_public_execution_source(tmp_path, "baostock", DAY, as_of=NOW - timedelta(seconds=1))
    assert result["status"] == "pending" and result["rows"] == []


@pytest.mark.parametrize("mutation", ["empty", "wrong_date", "wrong_provider", "bad_interval", "bad_fields", "error"])
def test_invalid_raw_is_retained_but_never_qualified_or_retried_today(tmp_path, client, monkeypatch, mutation):
    envelope = _envelope()
    if mutation == "empty":
        envelope["response"]["rows"] = []
    elif mutation == "wrong_date":
        envelope["session_date"] = "2026-09-17"
    elif mutation == "wrong_provider":
        envelope["provider"] = "sse_public_notice"
    elif mutation == "bad_interval":
        envelope["observed_at"] = "2026-09-21T00:00:00+00:00"
    elif mutation == "bad_fields":
        envelope["response"]["fields"] = ["bad"]
    else:
        envelope.update(status="timeout", response=None, error_type="timeout")
    monkeypatch.setattr(store, "_fetch", lambda *_: envelope)
    result = store.collect_public_execution_source(tmp_path, "baostock", DAY)
    assert result["status"] == "invalid_response"
    assert (tmp_path / "raw" / f'{result["digest"]}.json').is_file()
    assert store.read_public_execution_source(tmp_path, "baostock", DAY, as_of=NOW)["status"] == "missing"
    assert store.collect_public_execution_source(tmp_path, "baostock", DAY)["status"] == "attempted_today"


def test_crash_reservation_consumes_budget_before_request(tmp_path, client, monkeypatch):
    def crash(*_):
        raise RuntimeError("provider failed")
    monkeypatch.setattr(store, "_fetch", crash)
    with pytest.raises(RuntimeError):
        store.collect_public_execution_source(tmp_path, "baostock", DAY)
    assert store.collect_public_execution_source(tmp_path, "baostock", DAY)["status"] == "attempted_today"


def test_daily_budget_includes_failed_and_abandoned_attempts(tmp_path, client):
    directory = tmp_path / "attempts" / "2026-09-20"
    directory.mkdir(parents=True)
    for i in range(12):
        (directory / f"reserved-{i}.json").write_text("{}")
    assert store.collect_public_execution_source(tmp_path, "baostock", DAY)["status"] == "daily_budget_exhausted"
    assert not client


@pytest.mark.parametrize("current", ["2026-09-18T07:00:00+00:00", "2026-09-18T09:44:59+00:00", "2026-09-17T12:00:00+00:00"])
def test_collection_waits_for_vendor_publication_margin(tmp_path, client, monkeypatch, current):
    monkeypatch.setattr(store, "utc_now", lambda: datetime.fromisoformat(current))
    assert store.collect_public_execution_source(tmp_path, "baostock", DAY)["status"] == "pending_update"
    assert not client and not list(tmp_path.iterdir())


def test_nontrading_day_and_duplicate_providers_rejected(tmp_path, client):
    with pytest.raises(ValueError):
        store.collect_public_execution_source(tmp_path, "baostock", "2026-09-19")
    with pytest.raises(ValueError):
        store.collect_public_execution_day(tmp_path, DAY, providers=["baostock", "baostock"])
    with pytest.raises(ValueError):
        store.collect_public_execution_day(tmp_path, DAY, providers=[])
    with pytest.raises(ValueError):
        store.collect_public_execution_day(tmp_path, DAY, providers=["baostock", "unapproved"])
    assert not client


@pytest.mark.parametrize("mutation", ["raw", "pointer", "identity", "symlink", "nonobject"])
def test_corrupt_cached_artifacts_fail_closed(tmp_path, client, mutation):
    result = store.collect_public_execution_source(tmp_path, "baostock", DAY)
    pointer = tmp_path / "sources" / "baostock" / f"{DAY}.json"
    target = tmp_path / "raw" / f'{result["digest"]}.json'
    if mutation == "raw":
        target.write_bytes(target.read_bytes() + b" ")
    elif mutation == "pointer":
        pointer.write_text('{"digest":"../elsewhere"}')
    elif mutation == "symlink":
        saved = tmp_path / "copy.json"
        saved.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(saved)
    else:
        payload = json.loads(target.read_bytes()) if mutation == "identity" else []
        if mutation == "identity":
            payload["session_date"] = "2026-09-17"
        raw = canonical_json_bytes(payload)
        digest = sha256_hex(raw)
        (tmp_path / "raw" / f"{digest}.json").write_bytes(raw)
        pointer.write_text(json.dumps({"digest": digest}))
    with pytest.raises((ValueError, ArtifactIOError)):
        store.collect_public_execution_source(tmp_path, "baostock", DAY)
    assert len(client) == 1


def test_unsafe_roots_and_naive_asof_rejected(tmp_path):
    alias = tmp_path / "alias"
    actual = tmp_path / "actual"
    actual.mkdir()
    alias.symlink_to(actual, target_is_directory=True)
    for root in (alias, tmp_path / ".git" / "data"):
        with pytest.raises(ValueError):
            store.read_public_execution_source(root, "baostock", DAY, as_of=NOW)
    with pytest.raises(ValueError):
        store.read_public_execution_source(actual, "baostock", DAY, as_of=NOW.replace(tzinfo=None))


def test_missing_dates_retain_all_sources_without_network(tmp_path, client):
    sources = store.read_public_execution_sources(tmp_path, [DAY, DAY], as_of=NOW)
    assert len(sources) == 3 and all(row["status"] == "missing" for row in sources.values())
    assert not client
    with pytest.raises(ValueError):
        store.read_public_execution_sources(tmp_path, [str(value) for value in range(251)], as_of=NOW)


def test_default_provider_routes_are_fixed(monkeypatch):
    calls = []
    monkeypatch.setattr(store, "fetch_baostock_execution_day", lambda day: calls.append(("bao", day)) or {})
    monkeypatch.setattr(store, "fetch_exchange_execution_notices", lambda market, day: calls.append((market, day)) or {})
    for provider in store.PUBLIC_EXECUTION_PROVIDERS:
        store._fetch(provider, DAY)
    assert calls == [("bao", DAY), ("SH", DAY), ("SZ", DAY)]


def test_rehashed_premarket_artifact_cannot_bypass_publication_cutoff(tmp_path, client):
    result = store.collect_public_execution_source(tmp_path, "baostock", DAY)
    original = tmp_path / "raw" / f'{result["digest"]}.json'
    payload = json.loads(original.read_bytes())
    payload["envelope"].update(requested_at="2026-09-18T01:00:00+00:00", observed_at="2026-09-18T01:01:00+00:00")
    payload["collected_at"] = "2026-09-18T01:02:00+00:00"
    raw = canonical_json_bytes(payload)
    digest = sha256_hex(raw)
    (tmp_path / "raw" / f"{digest}.json").write_bytes(raw)
    (tmp_path / "sources" / "baostock" / f"{DAY}.json").write_text(json.dumps({"digest": digest}))
    with pytest.raises(ValueError, match="publication cutoff"):
        store.collect_public_execution_source(tmp_path, "baostock", DAY)
    assert len(client) == 1


def test_fixed_pair_filter_reduces_retained_rows_after_full_raw_validation(tmp_path, client):
    store.collect_public_execution_source(tmp_path, "baostock", DAY)
    sources = store.read_public_execution_sources(tmp_path, [DAY], as_of=NOW, wanted_pairs=[("000001.SZ", DAY)])
    assert sources[("baostock", DAY)]["rows"] == []
    assert sources[("baostock", DAY)]["status"] == "success"
    full = store.read_public_execution_source(tmp_path, "baostock", DAY, as_of=NOW)
    assert len(full["rows"]) == 1
    with pytest.raises(ValueError, match="fixed symbol"):
        store.read_public_execution_sources(tmp_path, [DAY, "2026-09-17"], as_of=NOW)
    for pairs in ([("invalid", DAY)], [("600000.SH", "20260918")]):
        with pytest.raises(ValueError):
            store.read_public_execution_sources(tmp_path, [DAY], as_of=NOW, wanted_pairs=pairs)
