from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.artifacts.io import ArtifactContentConflictError
from app.services.choice_research import DAILY_FIELDS, make_plan, normalize, request, snapshot_dates
from app.services.choice_research_collect import ChoiceCollector
from app.services.choice_research_store import ChoiceBudget, ChoiceDataset, now_text
from app.services.choice_sdk import ChoiceError, ChoiceSDKClient, safe_directory
from tools.backfill_choice_research import _read_existing
from app.utils.clock import market_now


SYMBOLS = ["600519.SH", "000001.SZ", "920006.BJ"]
SESSIONS = ["2024-01-31", "2024-02-01", "2024-02-29"]


def quotas():
    today = market_now().date()
    first = today - timedelta(days=today.weekday())
    fields = ["FUNCENAME", "SECUTYPE", "PERIOD", "STARTDATE", "ENDDATE", "THRESHOLD",
              "USEDDATA", "AVAILABEDATA", "EFFECTIVEDATE"]
    return {"error_code": 0, "indicators": fields, "data": {
        str(i): [function, category, "W", first.isoformat(), (first + timedelta(days=6)).isoformat(),
                 str(remaining), "0", str(remaining), "2099-12-31 23:59:59"]
        for i, (function, category, remaining) in enumerate([
            ("EM_CSD", "全品种", 500000), ("EM_CSS", "全品种", 500000), ("EM_CTR", "分红送转", 10),
        ])
    }}


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, args):
        self.calls.append((method, args))
        if method == "datastatistics":
            return quotas()
        response = {"error_code": 0, "codes": [], "indicators": [], "dates": [], "data": {}}
        if method == "tradedates":
            response["dates"] = SESSIONS
        elif method == "sector":
            response.update({"codes": SYMBOLS, "indicators": ["SECUCODE", "SECURITYSHORTNAME"],
                             "data": [value for code in SYMBOLS for value in (code, "current name")], "dates": [args[1]]})
        elif method == "csd":
            response = daily_response(args[0].split(","))
        elif method == "css":
            fields = args[1].split(",")
            sample = {"NAME": "current name", "HISNAME": "historic name", "LISTDATE": "2000/1/1", "STATUS": "L",
                      "TRADESTATUS": "正常交易", "PRECLOSEEXCH": 10, "LIMITUPPRICE": 11, "LIMITDOWNPRICE": 9}
            response.update({"indicators": fields, "data": {symbol: [sample.get(field) for field in fields] for symbol in args[0].split(",")}})
            if "TradeDate=" in args[2]:
                response["dates"] = [args[2].split("TradeDate=", 1)[1].split(",", 1)[0]]
        elif method == "ctr":
            response["indicators"] = args[1].split(",")
            response["data"] = {"0": ["920006.BJ", "02/01/2024", "02/29/2024", 0.1, None, None]}
        else:
            raise AssertionError(method)
        return response


def daily_response(symbols=SYMBOLS):
    sample = dict(zip(DAILY_FIELDS, [10.0, 10.5, 9.5, 10.1, 1000, 10010, 10, 1.0, "正常交易", "否", "否", "否", "否", 1.2], strict=True))
    return {"error_code": 0, "codes": list(symbols), "indicators": DAILY_FIELDS, "dates": SESSIONS,
            "data": {symbol: [[sample[field]] * len(SESSIONS) for field in DAILY_FIELDS] for symbol in symbols}}


def daily_payload():
    return {"request": request("daily", "csd", ["unused"], symbols=SYMBOLS, fields=DAILY_FIELDS, sessions=SESSIONS),
            "result": daily_response()}


def plan():
    return make_plan(SESSIONS[0], SESSIONS[-1], 3, [], ["920006.BJ"])


def test_full_collection_is_replay_verified_and_resume_skips_paid_requests(tmp_path):
    client = FakeClient()
    output = tmp_path / "dataset"
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(output, plan()) as dataset:
        summary = ChoiceCollector(dataset, client, budget).run()
        assert summary["status"] == "complete_for_declared_research_scope"
        assert summary["record_counts"]["daily"] == 9
        assert summary["daily_market_counts"] == {"BJ": 1, "SH": 1, "SZ": 1}
        assert summary["raw_replay_verified"] is True
        assert summary["formal_equivalent_pit"] is False
        assert summary["production_ranking_effect"] == "none"
        assert dataset.db.execute("SELECT amount_yuan FROM daily_bars LIMIT 1").fetchone()[0] == 10010
        calls_before = len(client.calls)
        reservations_before = budget.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
        resumed = ChoiceCollector(dataset, client, budget).run()
        assert resumed["requests_this_run"] == 0
        assert all(method == "datastatistics" for method, _ in client.calls[calls_before:])
        assert budget.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == reservations_before
    assert _read_existing(output, verify=True)["record_counts"]["daily"] == 9


def test_request_limit_pause_resumes_from_checkpoint(tmp_path):
    client = FakeClient()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        with pytest.raises(ChoiceError, match="request-count pause"):
            ChoiceCollector(dataset, client, budget, max_requests=2).run()
        assert dataset.summary()["requests_completed"] == 2
        resumed = ChoiceCollector(dataset, client, budget).run()
        assert resumed["cached_requests"] == 2
        assert resumed["record_counts"]["daily"] == 9


@pytest.mark.parametrize("state,quality", [
    ("连续停牌", "suspended"), ("停牌一天", "suspended"), ("未上市", "not_trading"),
    ("终止上市", "not_trading"), (None, "unknown_or_intraday_restricted_state"),
    ("盘中停牌", "unknown_or_intraday_restricted_state"),
])
def test_non_executable_and_unknown_states_never_synthesized(state, quality):
    payload = daily_payload()
    for columns in payload["result"]["data"].values():
        columns[DAILY_FIELDS.index("TRADESTATUS")] = [state] * 3
        columns[DAILY_FIELDS.index("VOLUME")] = [None] * 3
        columns[DAILY_FIELDS.index("AMOUNT")] = [None] * 3
    records = normalize(payload)
    assert {row[3]["quality"] for row in records} == {quality}
    assert all(row[3]["execution_eligible"] is None and row[3]["AMOUNT"] is None for row in records)


@pytest.mark.parametrize("flag,quality", [("HIGHLIMIT", "single_price_limit_up"), ("LOWLIMIT", "single_price_limit_down")])
def test_single_price_limits_are_not_proof_of_fill(flag, quality):
    payload = daily_payload()
    for columns in payload["result"]["data"].values():
        for field in ["OPEN", "HIGH", "LOW", "CLOSE"]:
            columns[DAILY_FIELDS.index(field)] = [10] * 3
        columns[DAILY_FIELDS.index(flag)] = ["是"] * 3
    assert {record[3]["quality"] for record in normalize(payload)} == {quality}


@pytest.mark.parametrize("mutation", ["calendar", "field", "symbol", "short", "nan", "invalid_bool", "ohlc"])
def test_bad_daily_responses_rejected(mutation):
    payload = daily_payload()
    result = payload["result"]
    if mutation == "calendar":
        result["dates"] = SESSIONS[:-1]
    elif mutation == "field":
        result["indicators"] = list(reversed(DAILY_FIELDS))
    elif mutation == "symbol":
        del result["data"][SYMBOLS[0]]
    elif mutation == "short":
        result["data"][SYMBOLS[0]][0] = [10]
    elif mutation == "nan":
        result["data"][SYMBOLS[0]][0][0] = float("nan")
    elif mutation == "invalid_bool":
        result["data"][SYMBOLS[0]][DAILY_FIELDS.index("ISSTSTOCK")][0] = "maybe"
    else:
        result["data"][SYMBOLS[0]][DAILY_FIELDS.index("HIGH")][0] = 1
    with pytest.raises(ChoiceError):
        normalize(payload)


def test_raw_tamper_and_derived_tamper_fail_closed(tmp_path):
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        ChoiceCollector(dataset, FakeClient(), budget).run()
        with dataset.db:
            dataset.db.execute("UPDATE records SET payload_json='{}' WHERE kind='daily'")
        with pytest.raises(ChoiceError, match="normalized records"):
            dataset.verify(normalize)
        path = next((dataset.directory / "raw").glob("*.json"))
        body = json.loads(path.read_text())
        body["payload"]["captured_at"] = "1999-01-01"
        path.write_text(json.dumps(body))
        with pytest.raises(ChoiceError):
            dataset.verify(normalize)


def test_orphan_raw_response_recovers_without_network(tmp_path):
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        payload = daily_payload()
        dataset.archive(payload["request"], payload["result"])
        client = FakeClient()
        records = ChoiceCollector(dataset, client, budget).fetch(payload["request"], 126)
        assert len(records) == 9 and client.calls == []
        assert dataset.summary()["requests_completed"] == 1


def test_first_projection_uses_canonical_archive_order_like_resume(tmp_path):
    with ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        raw = daily_payload()
        assert list(raw["result"]["data"]) != sorted(raw["result"]["data"])
        payload = dataset.archive(raw["request"], raw["result"])
        dataset.project(payload, normalize(payload))
        assert dataset.verify(normalize)["record_counts"]["daily"] == 9
        assert payload == dataset.cached(raw["request"])


def test_request_timestamp_must_match_the_sealed_raw_receipt(tmp_path):
    with ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        raw = daily_payload()
        payload = dataset.archive(raw["request"], raw["result"])
        dataset.project(payload, normalize(payload))
        with dataset.db:
            dataset.db.execute("UPDATE requests SET captured_at='2000-01-01T00:00:00+08:00'")
        with pytest.raises(ChoiceError, match="timestamp differs"):
            dataset.verify(normalize)


def test_duplicate_descriptor_cannot_skip_another_requests_raw_replay(tmp_path):
    with ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        first, second = daily_payload(), daily_payload()
        second["request"]["args"] = ["different-request"]
        for raw in (first, second):
            payload = dataset.archive(raw["request"], raw["result"])
            dataset.project(payload, normalize(payload))
        key = dataset.key(first["request"])
        with dataset.db:
            dataset.db.execute("UPDATE requests SET descriptor_json=? WHERE request_key=?", (json.dumps(second["request"]), key))
        (dataset.directory / "raw" / f"{key}.json").write_bytes(b"corrupt first receipt")
        with pytest.raises(ChoiceError, match="request identity"):
            dataset.verify(normalize)


def test_budget_reservation_is_durable_and_permission_missing_fails(tmp_path):
    with ChoiceBudget(tmp_path / "control", csd_limit=100) as budget:
        with pytest.raises(ChoiceError, match="missing confirmed quota"):
            budget.reserve("csd", 1)
        budget.update(quotas())
        budget.reserve("csd", 80)
    with ChoiceBudget(tmp_path / "control", csd_limit=100) as budget:
        budget.update(quotas())
        with pytest.raises(ChoiceError, match="budget pause"):
            budget.reserve("csd", 21)


def test_shared_session_lock_prevents_concurrent_login(tmp_path):
    with ChoiceBudget(tmp_path / "control"):
        with pytest.raises(ChoiceError, match="another Choice collector"):
            with ChoiceBudget(tmp_path / "control"):
                pass


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_budget_start_interruption_propagates_and_releases_lease(tmp_path, monkeypatch, interruption):
    from app.services import choice_research_store

    directory = tmp_path / "control"
    original = choice_research_store._connect
    failure = interruption()

    def interrupted(*args, **kwargs):
        raise failure

    budget = ChoiceBudget(directory)
    monkeypatch.setattr(choice_research_store, "_connect", interrupted)
    with pytest.raises(interruption) as caught:
        budget.__enter__()
    assert caught.value is failure and budget.fd is None and budget.db is None
    monkeypatch.setattr(choice_research_store, "_connect", original)
    with ChoiceBudget(directory) as reopened:
        assert reopened.db is not None


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_sdk_start_interruption_propagates_and_reaps_partial_worker(monkeypatch, interruption):
    from app.services import choice_sdk

    failure = interruption()

    class Pipe:
        closed = False

        def send(self, message):
            assert message is None

        def close(self):
            self.closed = True

    class Process:
        pid = 123
        alive = True
        closed = False

        def start(self):
            raise failure

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            pass

        def terminate(self):
            self.alive = False

        def close(self):
            self.closed = True

    parent, child, process = Pipe(), Pipe(), Process()
    context = SimpleNamespace(Pipe=lambda: (parent, child), Process=lambda **kwargs: process)
    monkeypatch.setattr(choice_sdk.mp, "get_context", lambda method: context)
    client = ChoiceSDKClient()
    with pytest.raises(interruption) as caught:
        client.__enter__()
    assert caught.value is failure
    assert parent.closed and child.closed and process.closed and not process.alive
    assert client._pipe is None and client._process is None


def test_choice_quota_uses_shanghai_day_at_utc_week_boundary(tmp_path, monkeypatch):
    from app.utils import clock

    monkeypatch.setattr(clock, "utc_now", lambda: datetime(2026, 8, 23, 17, tzinfo=UTC))
    client = FakeClient()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        ChoiceCollector(dataset, client, budget).refresh_quota()
        assert client.calls[0][1][0] == ""
        assert "FUNCENAME" in client.calls[0][1][1] and "AVAILABEDATA" in client.calls[0][1][1]
        assert "StartDate=2026-07-26,EndDate=2026-08-24" in client.calls[0][1][2]
        budget.reserve("csd", 1)
        period, stamp = budget.db.execute("SELECT period,created_at FROM reservations").fetchone()
        assert period == "2026-08-24/2026-08-30"
        assert stamp == "2026-08-24T01:00:00+08:00"


def test_failed_response_is_raw_only_not_completed(tmp_path):
    client = FakeClient()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        payload = daily_payload()
        del payload["result"]["data"][SYMBOLS[0]]
        dataset.archive(payload["request"], payload["result"])
        with pytest.raises(ChoiceError):
            ChoiceCollector(dataset, client, budget).fetch(payload["request"], 126)
        assert dataset.summary()["requests_completed"] == 0 and client.calls == []


def test_plan_change_and_foreign_database_never_overwritten(tmp_path):
    with ChoiceDataset(tmp_path / "dataset", plan()):
        pass
    changed = deepcopy(plan())
    changed["symbol_limit"] = 6
    with pytest.raises(ArtifactContentConflictError):
        ChoiceDataset(tmp_path / "dataset", changed)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    path = foreign / "choice_research.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE user_data (name TEXT)")
        db.execute("INSERT INTO user_data VALUES ('preserve')")
    before = path.read_bytes()
    with pytest.raises(ChoiceError, match="non-Choice database"):
        ChoiceDataset(foreign, plan())
    assert path.read_bytes() == before


def test_symlink_and_write_sdk_methods_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ChoiceError):
        safe_directory(alias)
    with pytest.raises(ChoiceError, match="read-only"):
        ChoiceSDKClient().request("porder", ["not allowed"])


def test_snapshot_dates_and_explicit_scope():
    assert snapshot_dates(SESSIONS) == ["2024-01-31", "2024-02-29"]
    assert "monthly_universe_not_complete_daily_membership" in plan()["limitations"]
    with pytest.raises(ChoiceError):
        make_plan("2024-01-31", "2024-02-29", 4, [], [])


def test_stale_or_expired_quota_cannot_start_new_requests(tmp_path):
    with ChoiceBudget(tmp_path / "control") as budget:
        response = quotas()
        response["data"]["0"][3:5] = ["2000-01-03", "2000-01-09"]
        budget.update(response)
        with pytest.raises(ChoiceError, match="quota period is stale"):
            budget.reserve("csd", 1)
        response = quotas()
        response["data"]["0"][8] = "2000-01-01 00:00:00"
        budget.update(response)
        with pytest.raises(ChoiceError, match="expired"):
            budget.reserve("csd", 1)


def test_native_timeout_closes_pipe_and_terminates_process():
    class Pipe:
        closed = False

        def poll(self, timeout):
            return False

        def send(self, value):
            assert value is None

        def close(self):
            self.closed = True

    class Process:
        pid = 123
        alive = True
        terminated = False
        closed = False

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            pass

        def terminate(self):
            self.terminated = True
            self.alive = False

        def close(self):
            self.closed = True

    client = ChoiceSDKClient(timeout=1)
    pipe, process = Pipe(), Process()
    client._pipe, client._process = pipe, process
    with pytest.raises(ChoiceError, match="native worker terminated"):
        client._receive()
    assert pipe.closed and process.terminated and process.closed
    assert client._pipe is None and client._process is None


def test_wrong_historical_metadata_date_rejected():
    from app.services.choice_research import METADATA_FIELDS

    descriptor = request("metadata", "css", [",".join(SYMBOLS), ",".join(METADATA_FIELDS), "TradeDate=2024-02-29"],
                         symbols=SYMBOLS, fields=METADATA_FIELDS, as_of="2024-01-31")
    with pytest.raises(ChoiceError, match="metadata date"):
        normalize({"request": descriptor, "result": FakeClient().request("css", descriptor["args"])})


def test_checkpoint_record_count_tamper_rejected(tmp_path):
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        ChoiceCollector(dataset, FakeClient(), budget).run()
        with dataset.db:
            dataset.db.execute("UPDATE requests SET record_count=0")
        with pytest.raises(ChoiceError, match="normalized records"):
            dataset.verify(normalize)


class SupplementClient(FakeClient):
    def request(self, method, args):
        result = super().request(method, args)
        if method == "csd":
            for columns in result["data"].values():
                columns[DAILY_FIELDS.index("TAFACTOR")] = [1.2, 1.3, 1.4]
            result["data"]["600519.SH"][DAILY_FIELDS.index("TRADESTATUS")][0] = "连续停牌"
        elif method == "ctr":
            result["data"]["0"][0] = args[2].split("secucode=", 1)[1].split(",", 1)[0]
        return result


def test_supplement_reuses_base_and_resumes_without_recharging(tmp_path):
    from app.services.choice_research_supplement import ChoiceSupplementCollector, make_supplement_plan

    base, output, control = tmp_path / "base", tmp_path / "supplement", tmp_path / "control"
    with ChoiceBudget(control) as budget, ChoiceDataset(base, plan()) as dataset:
        ChoiceCollector(dataset, SupplementClient(), budget).run()
    before = {path.relative_to(base): path.read_bytes() for path in base.rglob("*") if path.is_file()}
    with ChoiceDataset.open_readonly(base) as source:
        selected = make_supplement_plan(source, recent_sessions=3, event_limit=3)
        assert selected["planned_requests"] == {"execution_reference": 1, "suspension_detail": 1, "universe": 1, "dividend_event": 2}
        assert selected["estimated_units"] == {"css": 13, "sector": 1, "ctr": 2}
        assert selected["reused_reference_rows"] == 6
    with ChoiceBudget(control) as budget, ChoiceDataset(output, selected) as dataset:
        client = SupplementClient()
        with pytest.raises(ChoiceError, match="request-count pause"):
            ChoiceSupplementCollector(dataset, client, budget, max_requests=1).run()
        summary = ChoiceSupplementCollector(dataset, client, budget).run()
        coverage = summary["coverage"]
        assert summary["cached_requests"] == 1 and summary["requests_this_run"] == 4
        assert coverage["reference_rows_covered"] == coverage["positive_reference_triplets"] == 9
        assert coverage["reference_rows_missing"] == coverage["restricted_sessions_missing_details"] == 0
        assert coverage["recent_universe_dates_missing"] == []
        assert set(coverage["dividend_history_symbols_queried"]) == set(SYMBOLS)
        assert summary["official"] is False and summary["production_ranking_effect"] == "none"
        calls = len(client.calls)
        reservations = budget.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
        assert ChoiceSupplementCollector(dataset, client, budget).run()["requests_this_run"] == 0
        assert all(method == "datastatistics" for method, _ in client.calls[calls:])
        assert budget.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == reservations
    assert _read_existing(output, verify=True)["coverage"]["reference_rows_missing"] == 0
    assert before == {path.relative_to(base): path.read_bytes() for path in base.rglob("*") if path.is_file()}


def test_supplement_changed_plan_or_source_stops_before_network(tmp_path):
    from app.services.choice_research_supplement import ChoiceSupplementCollector, make_supplement_plan

    base = tmp_path / "base"
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(base, plan()) as dataset:
        ChoiceCollector(dataset, FakeClient(), budget).run()
    with ChoiceDataset.open_readonly(base) as source:
        selected = make_supplement_plan(source)
    selected["source_receipts_sha256"] = "0" * 64
    client = FakeClient()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "supplement", selected) as dataset:
        with pytest.raises(ChoiceError, match="source/plan changed"):
            ChoiceSupplementCollector(dataset, client, budget).run()
    assert client.calls == []
    with sqlite3.connect(base / "choice_research.sqlite3") as db:
        db.execute("UPDATE records SET payload_json='{}' WHERE kind='daily'")
    with ChoiceDataset.open_readonly(base) as source:
        with pytest.raises(ChoiceError, match="normalized records"):
            make_supplement_plan(source)


@pytest.mark.parametrize("mutation", ["date", "symbol", "negative", "nan", "reversed", "field"])
def test_execution_reference_invalid_response_rejected(mutation):
    from app.services.choice_research import REFERENCE_FIELDS

    descriptor = request("execution_reference", "css", [",".join(SYMBOLS), ",".join(REFERENCE_FIELDS), "TradeDate=2024-01-31"],
                         as_of="2024-01-31", symbols=SYMBOLS, fields=REFERENCE_FIELDS)
    result = FakeClient().request("css", descriptor["args"])
    if mutation == "date":
        result["dates"] = ["2026-08-26"]
    elif mutation == "symbol":
        del result["data"][SYMBOLS[0]]
    elif mutation == "field":
        result["indicators"] = list(reversed(REFERENCE_FIELDS))
    else:
        result["data"][SYMBOLS[0]] = {"negative": [-1, 11, 9], "nan": [float("nan"), 11, 9], "reversed": [10, 9, 11]}[mutation]
    with pytest.raises(ChoiceError):
        normalize({"request": descriptor, "result": result})


def test_null_reference_and_retrospective_suspension_do_not_prove_execution():
    from app.services.choice_research import REFERENCE_FIELDS, SUSPENSION_FIELDS

    for kind, fields, values in [("execution_reference", REFERENCE_FIELDS, [None, 0, None]),
                                 ("suspension_detail", SUSPENSION_FIELDS, ["盘中停牌", None, "2024/1/31", "2024/2/1"])]:
        descriptor = request(kind, "css", [], as_of="2024-01-31", symbols=[SYMBOLS[0]], fields=fields)
        result = {"error_code": 0, "indicators": fields, "dates": ["2024-01-31"], "data": {SYMBOLS[0]: values}}
        record = normalize({"request": descriptor, "result": result})[0][3]
        assert record["execution_eligible"] is None
        assert record["original_vintage_available"] is False
        if kind == "suspension_detail":
            assert record["suspension_end_is_retrospective_not_known_at_start"] is True
            assert record["SUSPENDEDATE"] == "2024-02-01"
        else:
            assert record["null_or_zero_is_not_proof_of_no_limit"] is True


def test_delayed_server_quota_does_not_refund_local_reservations(tmp_path):
    with ChoiceBudget(tmp_path / "control", css_limit=200000) as budget:
        budget.update(quotas())
        budget.reserve("css", 90000)
        budget.update(quotas())  # unchanged/delayed statistics
        with pytest.raises(ChoiceError, match="budget pause"):
            budget.reserve("css", 110001)
        response = quotas()
        response["data"]["1"][6:8] = ["405000", "95000"]
        budget.update(response)
        with pytest.raises(ChoiceError, match="budget pause"):
            budget.reserve("css", 5001)


def test_quota_cli_does_not_require_output_but_other_operations_do():
    from tools.backfill_choice_research import parser

    args = parser().parse_args(["quota"])
    assert args.output_dir is None and args.operation == "quota"


def test_quota_refresh_is_throttled_but_expired_snapshot_is_queried(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.services.choice_research_collect.time.monotonic", lambda: clock[0])
    client = FakeClient()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        collector = ChoiceCollector(dataset, client, budget)
        collector.refresh_quota()
        collector.refresh_quota()
        clock[0] += 29
        collector.refresh_quota()
        assert len(client.calls) == 1
        budget.reserve("css", 10)
        clock[0] += 1
        collector.refresh_quota()
        assert len(client.calls) == 2
        assert budget.db.execute("SELECT SUM(units) FROM reservations").fetchone()[0] == 10


def _stale_week_quotas():
    response = quotas()
    for values in response["data"].values():
        values[3:5] = ["2026-08-24", "2026-08-30"]
    return response


def test_stale_week_blocks_before_any_uncached_market_or_discovery_request(tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.clock.utc_now", lambda: datetime(2026, 9, 1, 3, 0, tzinfo=UTC))

    class StaleWeek(FakeClient):
        def request(self, method, args):
            if method == "datastatistics":
                self.calls.append((method, args))
                return _stale_week_quotas()
            return super().request(method, args)

    client = StaleWeek()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        with pytest.raises(ChoiceError, match="current Choice quota period is unreported"):
            ChoiceCollector(dataset, client, budget).run()
        assert [method for method, _ in client.calls] == ["datastatistics"]
        assert budget.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


def test_complete_cached_replay_does_not_need_current_week_market_quota(tmp_path, monkeypatch):
    output, client = tmp_path / "dataset", FakeClient()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(output, plan()) as dataset:
        ChoiceCollector(dataset, client, budget).run()
    monkeypatch.setattr("app.utils.clock.utc_now", lambda: datetime(2026, 9, 1, 3, 0, tzinfo=UTC))

    class StaleWeek(FakeClient):
        def request(self, method, args):
            if method == "datastatistics":
                self.calls.append((method, args))
                return _stale_week_quotas()
            return super().request(method, args)

    stale = StaleWeek()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(output, plan()) as dataset:
        summary = ChoiceCollector(dataset, stale, budget).run()
        assert summary["requests_this_run"] == 0
        assert [method for method, _ in stale.calls] == ["datastatistics"]


def test_event_only_resume_ignores_exhausted_cached_csd_and_css(tmp_path):
    output, control = tmp_path / "dataset", tmp_path / "control"
    with ChoiceBudget(control) as budget, ChoiceDataset(output, plan()) as dataset:
        with pytest.raises(ChoiceError, match="request-count pause"):
            ChoiceCollector(dataset, FakeClient(), budget, max_requests=7).run()
        assert dataset.summary()["requests_completed"] == 7
        before = dict(budget.db.execute(
            "SELECT function,SUM(units) FROM reservations GROUP BY function ORDER BY function"
        ))
        assert "EM_CTR" not in before

        class EventOnlyCapacity(FakeClient):
            def request(self, method, args):
                if method != "datastatistics":
                    return super().request(method, args)
                self.calls.append((method, args))
                response = quotas()
                for index in ("0", "1"):
                    response["data"][index][6] = response["data"][index][5]
                    response["data"][index][7] = "0"
                return response

        client = EventOnlyCapacity()
        summary = ChoiceCollector(dataset, client, budget).run()
        after = dict(budget.db.execute(
            "SELECT function,SUM(units) FROM reservations GROUP BY function ORDER BY function"
        ))
        assert summary["requests_this_run"] == 1 and summary["cached_requests"] == 7
        assert [method for method, _ in client.calls] == ["datastatistics", "ctr"]
        assert {name: after[name] for name in ("EM_CSD", "EM_CSS")} == {
            name: before[name] for name in ("EM_CSD", "EM_CSS")
        }
        assert after["EM_CTR"] == 1


def test_collector_quota_archives_and_summary_drop_unrequested_provider_fields(tmp_path):
    class ExtraFields(FakeClient):
        def request(self, method, args):
            response = super().request(method, args)
            if method == "datastatistics":
                response["indicators"].append("SECRET_TOKEN")
                for values in response["data"].values():
                    values.append("must-not-leak")
            return response

    output, client = tmp_path / "dataset", ExtraFields()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(output, plan()) as dataset:
        summary = ChoiceCollector(dataset, client, budget).run()
    assert "must-not-leak" not in json.dumps(summary)
    assert set(summary["quota_snapshot"]["EM_CSD"]) == {
        "FUNCENAME", "SECUTYPE", "PERIOD", "STARTDATE", "ENDDATE", "THRESHOLD",
        "EFFECTIVEDATE", "USEDDATA", "AVAILABEDATA",
    }
    account_files = list((output / "account").glob("*.json"))
    assert account_files and all("must-not-leak" not in path.read_text() for path in account_files)
    saved_summary = json.loads((output / Path(summary["summary_path"]).name).read_text())
    assert "must-not-leak" not in json.dumps(saved_summary)


def test_quota_failure_does_not_start_next_data_request(tmp_path):
    class QuotaFailure(FakeClient):
        def request(self, method, args):
            if method == "datastatistics":
                raise ChoiceError("rate limited")
            return super().request(method, args)

    client = QuotaFailure()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "dataset", plan()) as dataset:
        with pytest.raises(ChoiceError, match="rate limited"):
            ChoiceCollector(dataset, client, budget).run()
        assert client.calls == []
        assert budget.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


def test_daily_universe_only_requests_missing_dates_and_reuses_multiple_bundles(tmp_path):
    from app.services.choice_research_universe import ChoiceUniverseCollector, make_universe_plan

    base, extra = tmp_path / "base", tmp_path / "universe"
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(base, plan()) as dataset:
        ChoiceCollector(dataset, FakeClient(), budget).run()
    before = (base / "choice_research.sqlite3").read_bytes()
    with ChoiceDataset.open_readonly(base) as source:
        selected = make_universe_plan(source, [])
        assert selected["missing_dates"] == ["2024-02-01"]
        assert selected["reused_date_count"] == 2
    client = FakeClient()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(extra, selected) as dataset:
        summary = ChoiceUniverseCollector(dataset, client, budget).run()
        assert summary["coverage"]["universe_dates_covered"] == 3
        assert summary["coverage"]["universe_dates_missing"] == []
        assert summary["coverage"]["membership_rows"] == 9
        assert summary["coverage"]["market_count_ranges"]["BJ"] == {"min": 1, "max": 1}
        assert summary["formal_equivalent_pit"] is False
        paid = [(method, args) for method, args in client.calls if method != "datastatistics"]
        assert paid == [("sector", ["001071", "2024-02-01", "RECVtimeout=15,Ispandas=0"])]
        assert ChoiceUniverseCollector(dataset, client, budget).run()["requests_this_run"] == 0
    assert _read_existing(extra, verify=True)["coverage"]["universe_dates_missing"] == []
    with ChoiceDataset.open_readonly(base) as source, ChoiceDataset.open_readonly(extra) as reused:
        all_covered = make_universe_plan(source, [reused])
        assert all_covered["missing_dates"] == [] and all_covered["reused_date_count"] == 3
        with pytest.raises(ChoiceError, match="distinct"):
            make_universe_plan(source, [source])
    assert (base / "choice_research.sqlite3").read_bytes() == before


def test_universe_continuity_checks_only_adjacent_sessions_and_each_market():
    from app.services.choice_research_universe import _check_continuity

    scope = {"sessions": SESSIONS, "minimum_adjacent_market_ratio": 0.95}
    dates = {SESSIONS[0]: {"market_counts": {"SH": 100, "SZ": 100, "BJ": 100}},
             SESSIONS[2]: {"market_counts": {"SH": 100, "SZ": 100, "BJ": 120}}}
    _check_continuity(dates, scope)  # not adjacent: do not reject legitimate long-term growth
    dates[SESSIONS[1]] = {"market_counts": {"SH": 100, "SZ": 100, "BJ": 90}}
    with pytest.raises(ChoiceError, match="continuity requires review"):
        _check_continuity(dates, scope)
    dates = {SESSIONS[0]: {"market_counts": {"SH": 100, "SZ": 100}}}
    with pytest.raises(ChoiceError, match="missing a market"):
        _check_continuity(dates, scope)


def test_universe_explicit_budget_does_not_discard_existing_reservations(tmp_path):
    with ChoiceBudget(tmp_path / "control", sector_limit=2) as budget:
        budget.reserve("sector", 2)
        with pytest.raises(ChoiceError, match="budget pause"):
            budget.reserve("sector", 1)
    with ChoiceBudget(tmp_path / "control", sector_limit=4) as budget:
        budget.reserve("sector", 2)
        with pytest.raises(ChoiceError, match="budget pause"):
            budget.reserve("sector", 1)
    with pytest.raises(ChoiceError, match="at most 600"):
        ChoiceBudget(tmp_path / "control", sector_limit=601)


@pytest.mark.parametrize("mutation", ["date", "sector", "missing_date"])
def test_wrong_universe_date_or_sector_is_rejected(mutation):
    descriptor = request("universe", "sector", ["001071", SESSIONS[0], "Ispandas=0"], as_of=SESSIONS[0])
    response = FakeClient().request("sector", descriptor["args"])
    if mutation == "date":
        response["dates"] = [SESSIONS[1]]
    elif mutation == "sector":
        descriptor["args"][0] = "001004"
    else:
        response["dates"] = []
    with pytest.raises(ChoiceError, match="universe date/sector"):
        normalize({"request": descriptor, "result": response})


def test_universe_conflicting_reuse_and_changed_source_fail_closed(tmp_path):
    from app.services.choice_research_universe import ChoiceUniverseCollector, make_universe_plan

    base = tmp_path / "base"
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(base, plan()) as dataset:
        ChoiceCollector(dataset, FakeClient(), budget).run()
    with ChoiceDataset.open_readonly(base) as source:
        selected = make_universe_plan(source, [])
        with ChoiceDataset(tmp_path / "conflict", plan()) as conflict:
            descriptor = request("universe", "sector", ["001071", SESSIONS[0], "Ispandas=0"], as_of=SESSIONS[0])
            response = FakeClient().request("sector", descriptor["args"])
            response["codes"] = [*SYMBOLS, "600000.SH"]
            response["data"] += ["600000.SH", "additional stock"]
            payload = conflict.archive(descriptor, response)
            conflict.project(payload, normalize(payload))
            with pytest.raises(ChoiceError, match="disagree"):
                make_universe_plan(source, [conflict])
    selected["sources"][0]["receipts_sha256"] = "0" * 64
    client = FakeClient()
    with ChoiceBudget(tmp_path / "control") as budget, ChoiceDataset(tmp_path / "output", selected) as dataset:
        with pytest.raises(ChoiceError, match="source/plan changed"):
            ChoiceUniverseCollector(dataset, client, budget).run()
        assert client.calls == []


def _choice_cli_environment(tmp_path, monkeypatch):
    from tools import backfill_choice_research as cli

    client = FakeClient()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "ChoiceSDKClient", lambda **kwargs: nullcontext(client))
    return cli, client


def _run_choice_cli(cli, monkeypatch, capsys, arguments):
    monkeypatch.setattr(cli.sys, "argv", ["choice", *map(str, arguments)])
    code = cli.main()
    return code, capsys.readouterr()


def test_choice_cli_plan_collect_pause_resume_and_readonly_status(tmp_path, monkeypatch, capsys):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    output = tmp_path / "dataset"
    scope = ["--output-dir", output, "--start-date", SESSIONS[0], "--end-date", SESSIONS[-1], "--symbol-limit", "3"]
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["plan", *scope])
    assert code == 0 and json.loads(captured.out)["symbol_limit"] == 3
    assert client.calls == [] and not output.exists()
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["collect", *scope, "--max-requests", "1"])
    assert code == 2
    assert json.loads(captured.out.splitlines()[-1])["status"] == "incomplete_resumable"
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["resume", "--output-dir", output])
    assert code == 0
    assert json.loads(captured.out.splitlines()[-1])["status"] == "complete_for_declared_research_scope"
    before = len(client.calls)
    for operation, status in [("status", "checkpoint_status"), ("verify", "existing_records_verified")]:
        code, captured = _run_choice_cli(cli, monkeypatch, capsys, [operation, "--output-dir", output])
        report = json.loads(captured.out)
        assert code == 0 and report["status"] == status
        assert report["raw_replay_verified"] is (operation == "verify")
    assert len(client.calls) == before


@pytest.mark.parametrize("operation", ["supplement", "universe"])
def test_choice_cli_derived_plans_collect_and_resume(tmp_path, monkeypatch, capsys, operation):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    base, output = tmp_path / "base", tmp_path / operation
    code, _ = _run_choice_cli(cli, monkeypatch, capsys, [
        "collect", "--output-dir", base, "--start-date", SESSIONS[0],
        "--end-date", SESSIONS[-1], "--symbol-limit", "3",
    ])
    assert code == 0
    scope = ["--source-dir", base, "--output-dir", output, "--event-limit", "0"]
    before = len(client.calls)
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, [f"{operation}-plan", *scope])
    assert code == 0 and json.loads(captured.out)["formal_equivalent_pit"] is False
    assert len(client.calls) == before and not output.exists()
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, [operation, *scope])
    result = json.loads(captured.out.splitlines()[-1])
    assert code == 0 and result["production_ranking_effect"] == "none"
    assert set(result["quota_snapshot"]["EM_CSD"]) == {
        "FUNCENAME", "SECUTYPE", "PERIOD", "STARTDATE", "ENDDATE", "THRESHOLD",
        "EFFECTIVEDATE", "USEDDATA", "AVAILABEDATA",
    }
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["resume", "--output-dir", output])
    assert code == 0 and json.loads(captured.out.splitlines()[-1])["requests_this_run"] == 0
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["verify", "--output-dir", output])
    assert code == 0 and json.loads(captured.out)["raw_replay_verified"] is True


@pytest.mark.parametrize("operation,extra,error", [
    ("plan", [], "explicit --start-date"),
    ("collect", ["--start-date", "2024-01-01", "--end-date", "2099-01-01"], "before today"),
    ("supplement-plan", [], "requires --source-dir"),
    ("universe-plan", [], "requires --source-dir"),
])
def test_choice_cli_invalid_scope_never_logs_in(tmp_path, monkeypatch, capsys, operation, extra, error):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    output = tmp_path / "output"
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, [operation, "--output-dir", output, *extra])
    report = json.loads(captured.err)
    assert code == 2 and error in report["error"]
    assert report["production_modified"] is False and client.calls == []
    assert not output.exists()


@pytest.mark.parametrize("operation", ["supplement-plan", "universe-plan"])
def test_choice_cli_refuses_source_overwrite(tmp_path, monkeypatch, capsys, operation):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, [
        operation, "--output-dir", tmp_path, "--source-dir", tmp_path,
    ])
    assert code == 2 and json.loads(captured.err)["production_modified"] is False
    assert client.calls == []


def test_choice_cli_missing_output_and_invalid_resume_fail_closed(tmp_path, monkeypatch, capsys):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["status"])
    assert code == 2 and "--output-dir" in json.loads(captured.err)["error"]
    (tmp_path / "plan.json").write_text("[]", encoding="utf-8")
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["resume", "--output-dir", tmp_path])
    assert code == 2 and "invalid saved Choice plan" in json.loads(captured.err)["error"]
    assert client.calls == []


def test_choice_cli_quota_reports_existing_reservations_without_market_requests(tmp_path, monkeypatch, capsys):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    control = tmp_path / "data" / "research" / "choice_ingestion_control"
    with ChoiceBudget(control) as budget:
        budget.update(quotas())
        budget.reserve("css", 7)
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["quota"])
    report = json.loads(captured.out)
    assert code == 0 and report["local_reservations"][0]["estimated_units"] == 7
    assert report["query_window"]["calendar_days"] == 30
    assert report["functions"]["EM_CSS"]["safe_units"] == 199993
    assert report["functions"]["EM_CFC"]["status"] == "unsupported_not_wired"
    assert report["action_plan"]["status"] == "arithmetic_candidates_require_gap_review"
    assert report["action_plan"]["execute_automatically"] is False
    assert report["market_data_requests"] == report["reservations_released"] == 0
    assert [method for method, _ in client.calls] == ["datastatistics"]
    today = market_now().date()
    assert client.calls[0][1][2] == (
        f"StartDate={(today - timedelta(days=29)).isoformat()},EndDate={today.isoformat()},Ispandas=0"
    )
    receipts = list((control / "account").glob("*.json"))
    assert len(receipts) == 1 and json.loads(receipts[0].read_text()) == report


def test_choice_cli_reports_unconfirmed_weekly_rollover_without_using_old_remaining(tmp_path, monkeypatch, capsys):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    monkeypatch.setattr("app.utils.clock.utc_now", lambda: datetime(2026, 9, 1, 3, 0, tzinfo=UTC))

    def request(method, args):
        client.calls.append((method, args))
        assert method == "datastatistics"
        return _stale_week_quotas()

    client.request = request
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["quota"])
    report = json.loads(captured.out)
    assert code == 0 and report["schema_version"] == "choice-quota-report-v3"
    for function in ("EM_CSD", "EM_CSS", "EM_CTR"):
        item = report["functions"][function]
        assert item["status"] == "paused_rollover_unconfirmed"
        assert item["reported_for_current_period"] is False and item["safe_units"] == 0
        assert item["expected_current_period"] == "2026-08-31/2026-09-06"
    assert report["action_plan"]["status"] == "blocked_current_period_unreported"
    assert report["action_plan"]["market_data_probe_permitted"] is False
    assert report["capacity_planning"]["status"] == "unavailable_without_current_period_quota"
    assert all(not item["feasible_under_current_safe_units"]
               for item in report["capacity_planning"]["complete_research_cohort_examples"])
    assert report["market_data_requests"] == report["reservations_released"] == 0
    assert "PERIOD" in client.calls[0][1][1] and "THRESHOLD" in client.calls[0][1][1]


@pytest.mark.parametrize("ctr_state,expected_status", [
    ("missing", "blocked_current_period_unreported"),
    ("expired", "blocked_function_unavailable"),
])
def test_choice_quota_keeps_paired_history_action_independent_from_optional_ctr(
    tmp_path, monkeypatch, capsys, ctr_state, expected_status,
):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    response = quotas()
    if ctr_state == "missing":
        response["data"].pop("2")
    else:
        response["data"]["2"][8] = "2020-01-01 00:00:00"
    client.request = lambda method, args: response

    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["quota"])
    report = json.loads(captured.out)
    paired = report["action_plan"]["workflows"]["paired_history"]
    events = report["action_plan"]["workflows"]["event_queries"]
    assert code == 0
    assert report["capacity_planning"]["status"] == "arithmetic_candidates_require_gap_review"
    assert report["action_plan"]["status"] == paired["status"] == "arithmetic_candidates_require_gap_review"
    assert paired["minimum_balanced_cohort_feasible"] is True
    assert events["status"] == expected_status and events["safe_requests"] == 0
    assert events["functions_unavailable"] == ["EM_CTR"]
    assert report["market_data_requests"] == report["reservations_released"] == 0


def test_choice_quota_requires_enough_units_for_one_balanced_complete_cohort(tmp_path, monkeypatch, capsys):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    response = quotas()
    for key in ("0", "1"):
        response["data"][key][5:8] = ["1", "0", "1"]
    client.request = lambda method, args: response

    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["quota"])
    report = json.loads(captured.out)
    minimum = report["capacity_planning"]["minimum_balanced_research_cohort"]
    paired = report["action_plan"]["workflows"]["paired_history"]
    assert code == 0 and minimum["symbols"] == 3
    assert minimum["feasible_under_current_safe_units"] is False
    assert report["capacity_planning"]["status"] == "insufficient_safe_units_for_minimum_balanced_cohort"
    assert report["action_plan"]["status"] == paired["status"] == "blocked_no_paired_capacity"
    assert paired["minimum_balanced_cohort_feasible"] is False
    assert report["market_data_requests"] == report["reservations_released"] == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_quota_update_selects_unique_current_week_from_thirty_day_rows(tmp_path, reverse):
    response = quotas()
    rows = list(response["data"].values())
    stale = deepcopy(rows[0])
    stale[3:5] = ["2026-08-24", "2026-08-30"]
    response["data"] = {"old": stale, "current": rows[0]}
    if reverse:
        response["data"] = dict(reversed(response["data"].items()))
    with ChoiceBudget(tmp_path / "control") as budget:
        budget.update(response, today=date(2026, 9, 1))
        assert budget.quotas["EM_CSD"]["STARTDATE"] == "2026-08-31"


def test_quota_update_rejects_ambiguous_current_rows_and_threshold_mismatch(tmp_path):
    duplicate = quotas()
    duplicate["data"]["duplicate"] = deepcopy(duplicate["data"]["0"])
    with ChoiceBudget(tmp_path / "control") as budget:
        with pytest.raises(ChoiceError, match="multiple current"):
            budget.update(duplicate, today=market_now().date())
    mismatch = quotas()
    mismatch["data"]["0"][5] = "500001"
    with ChoiceBudget(tmp_path / "control-2") as budget:
        with pytest.raises(ChoiceError, match="does not reconcile"):
            budget.update(mismatch)


def test_choice_cli_threshold_mismatch_fails_without_account_archive(tmp_path, monkeypatch, capsys):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    mismatch = quotas()
    mismatch["data"]["1"][5] = "500001"
    client.request = lambda method, args: mismatch
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["quota"])
    assert code == 2 and "does not reconcile" in json.loads(captured.err)["error"]
    account = tmp_path / "data" / "research" / "choice_ingestion_control" / "account"
    assert not account.exists() or not list(account.iterdir())


def test_choice_cli_quota_reports_realistic_safe_capacity_without_leaking_provider_fields(tmp_path, monkeypatch, capsys):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    today = market_now().date()
    first = today - timedelta(days=today.weekday())
    period = f"{first.isoformat()}/{(first + timedelta(days=6)).isoformat()}"
    fields = ["FUNCENAME", "SECUTYPE", "PERIOD", "STARTDATE", "ENDDATE", "THRESHOLD",
              "EFFECTIVEDATE", "USEDDATA", "AVAILABEDATA", "SECRET_TOKEN"]
    values = [
        ("EM_CSD", "全品种", 140918, 359082), ("EM_CSS", "全品种", 107217, 392783),
        ("EM_CTR", "分红送转", 6, 4), ("EM_CFC", "函数校验接口", 1, 19999),
    ]
    response = {"error_code": 0, "indicators": fields, "data": {
        str(index): [function, security, "W", first.isoformat(), (first + timedelta(days=6)).isoformat(),
                     used + remaining, "2099-12-31 23:59:59", used, remaining, "must-not-leak"]
        for index, (function, security, used, remaining) in enumerate(values)
    }}

    def request(method, args):
        client.calls.append((method, args))
        assert method == "datastatistics"
        return response

    client.request = request
    control = tmp_path / "data" / "research" / "choice_ingestion_control"
    with ChoiceBudget(control) as budget:
        assert budget.db is not None
        budget.db.executemany("INSERT INTO reservations VALUES (?,?,?,?)", [
            (period, "EM_CSD", 421680, now_text()), (period, "EM_CSS", 107216, now_text()),
            (period, "EM_CTR", 6, now_text()),
        ])
        budget.db.commit()
        before = budget.db.execute("SELECT * FROM reservations ORDER BY function").fetchall()
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["quota"])
    report = json.loads(captured.out)
    assert code == 0 and "must-not-leak" not in captured.out
    assert {name: report["functions"][name]["safe_units"] for name in ("EM_CSD", "EM_CSS", "EM_CTR")} == {
        "EM_CSD": 0, "EM_CSS": 92784, "EM_CTR": 0,
    }
    assert report["functions"]["EM_CFC"]["reported_remaining"] == 19999
    assert report["capacity_planning"]["paired_capacity"]["additional_symbols_for_assumed_sessions"] == 0
    assert report["capacity_planning"]["reference_only_css_capacity"] == {
        "additional_symbols_for_assumed_sessions": 61,
        "additional_sessions_for_assumed_symbols": 515,
        "not_usable_as_paired_history_without_csd": True,
    }
    assert all(not scenario["feasible_under_current_safe_units"] for scenario in report["capacity_planning"]["examples"])
    cohorts = report["capacity_planning"]["complete_research_cohort_examples"]
    assert [(item["symbols"], item["estimated_units"]) for item in cohorts] == [
        (30, {"EM_CSD": 210840, "EM_CSS": 53280}),
        (60, {"EM_CSD": 421680, "EM_CSS": 106560}),
    ]
    with ChoiceBudget(control) as budget:
        assert budget.db is not None
        assert budget.db.execute("SELECT * FROM reservations ORDER BY function").fetchall() == before
    saved = list((control / "account").glob("*.json"))
    assert len(saved) == 1 and "must-not-leak" not in saved[0].read_text()


@pytest.mark.parametrize("arguments", [
    ["--planning-symbols", 0], ["--planning-sessions", 0], ["--planning-symbols", 226],
    ["--planning-sessions", 801], ["--planning-snapshot-dates", 0],
    ["--planning-snapshot-dates", 503], ["--planning-report-dates", 21],
])
def test_choice_cli_invalid_quota_planning_scope_never_logs_in(tmp_path, monkeypatch, capsys, arguments):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["quota", *arguments])
    assert code == 2 and "planning scope" in json.loads(captured.err)["error"]
    assert client.calls == []


@pytest.mark.parametrize("response", [
    {"error_code": 0, "indicators": [], "data": []},
    {"error_code": 0, "indicators": ["FUNCENAME", "FUNCENAME"], "data": {"0": ["EM_CSS", "EM_CSS"]}},
    {"error_code": 0, "indicators": ["FUNCENAME"], "data": {"0": []}},
])
def test_choice_cli_malformed_quota_response_fails_without_success_archive(tmp_path, monkeypatch, capsys, response):
    cli, client = _choice_cli_environment(tmp_path, monkeypatch)
    client.request = lambda method, args: response
    code, captured = _run_choice_cli(cli, monkeypatch, capsys, ["quota"])
    assert code == 2 and "strict shape validation" in json.loads(captured.err)["error"]
    account = tmp_path / "data" / "research" / "choice_ingestion_control" / "account"
    assert not account.exists() or not list(account.iterdir())


@pytest.mark.parametrize("operation", ["read", "rejected_login", "write", "sdk_error", "disconnected"])
def test_sdk_worker_serializes_read_results_sanitizes_errors_and_logs_out(monkeypatch, operation):
    from app.services import choice_sdk

    result = SimpleNamespace(ErrorCode=0, Codes=SYMBOLS, Dates=[datetime(2024, 1, 31, tzinfo=UTC)], Data={})
    sdk = SimpleNamespace(start=Mock(return_value=SimpleNamespace(ErrorCode=3 if operation == "rejected_login" else 0)),
                          csd=Mock(return_value=result), stop=Mock())
    if operation == "sdk_error":
        sdk.csd.side_effect = ValueError("sensitive native error")
    requests = iter([("porder" if operation == "write" else "csd", ["sample"]), None])
    pipe = SimpleNamespace(send=Mock(), recv=Mock(side_effect=lambda: next(requests)), close=Mock())
    if operation == "disconnected":
        pipe.recv.side_effect = EOFError
    monkeypatch.setattr(choice_sdk.importlib, "import_module", lambda name: SimpleNamespace(c=sdk))
    choice_sdk._sdk_worker(pipe)
    sdk.start.assert_called_once()
    assert "ForceLogin=0" in sdk.start.call_args.args[0]
    pipe.close.assert_called_once()
    assert sdk.stop.call_count == (0 if operation == "rejected_login" else 1)
    sent = [call.args[0] for call in pipe.send.call_args_list]
    assert "sensitive native error" not in json.dumps(sent)
    if operation == "read":
        assert sent[-1]["dates"] == ["2024-01-31T00:00:00+00:00"]
        sdk.csd.assert_called_once_with("sample")
    elif operation in {"write", "sdk_error"}:
        assert sent[-1] == {"worker_error": "ChoiceError" if operation == "write" else "ValueError"}
    else:
        sdk.csd.assert_not_called()


def test_sdk_worker_broken_pipe_still_closes_and_logs_out(monkeypatch):
    from app.services import choice_sdk

    sdk = SimpleNamespace(start=Mock(return_value=SimpleNamespace(ErrorCode=0)), stop=Mock())
    pipe = SimpleNamespace(send=Mock(side_effect=BrokenPipeError), close=Mock())
    monkeypatch.setattr(choice_sdk.importlib, "import_module", lambda name: SimpleNamespace(c=sdk))
    choice_sdk._sdk_worker(pipe)
    pipe.close.assert_called_once()
    sdk.stop.assert_called_once()


def test_sdk_client_successful_start_rate_limit_read_and_close(monkeypatch):
    from app.services import choice_sdk

    result = {"error_code": 0, "data": {"sample": [1]}}
    parent = SimpleNamespace(poll=Mock(return_value=True), recv=Mock(side_effect=[{"error_code": 0}, result]),
                             send=Mock(), close=Mock())
    child = SimpleNamespace(close=Mock())
    process = SimpleNamespace(pid=123, start=Mock(), is_alive=Mock(return_value=True), close=Mock())
    process.join = Mock(side_effect=lambda timeout: setattr(process.is_alive, "return_value", False))
    context = SimpleNamespace(Pipe=lambda: (parent, child), Process=lambda **kwargs: process)
    monkeypatch.setattr(choice_sdk.mp, "get_context", lambda method: context)
    monkeypatch.setattr(choice_sdk.time, "monotonic", lambda: 100.0)
    sleep = Mock()
    monkeypatch.setattr(choice_sdk.time, "sleep", sleep)
    with ChoiceSDKClient() as client:
        client._last_call = 99.5
        assert client.request("csd", ["sample"]) == result
    sleep.assert_called_once_with(0.5)
    assert parent.send.call_args_list[0].args == (("csd", ["sample"]),)
    parent.close.assert_called_once()
    child.close.assert_called_once()
    process.close.assert_called_once()


@pytest.mark.parametrize("failure,expected", [(EOFError(), "disconnected"), ({"worker_error": "ValueError"}, "worker failed")])
def test_sdk_client_lost_worker_releases_session(monkeypatch, failure, expected):
    client = ChoiceSDKClient(interval=0)
    client._process = SimpleNamespace(is_alive=lambda: True)
    receive = Mock(side_effect=failure) if isinstance(failure, EOFError) else Mock(return_value=failure)
    client._pipe = SimpleNamespace(send=Mock(), poll=lambda timeout: True, recv=receive)
    close = Mock()
    monkeypatch.setattr(client, "close", close)
    with pytest.raises(ChoiceError, match=expected):
        client.request("csd", ["sample"])
    close.assert_called_once()


def test_sdk_force_kills_unresponsive_worker_and_tolerates_closed_pipe():
    client = ChoiceSDKClient()
    pipe = SimpleNamespace(send=Mock(side_effect=OSError), close=Mock())
    process = SimpleNamespace(pid=123, is_alive=lambda: True, join=Mock(), terminate=Mock(), kill=Mock(), close=Mock())
    client._pipe, client._process = pipe, process
    client.close()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    process.close.assert_called_once()
    pipe.close.assert_called_once()
    client.close()  # idempotent after the process and pipe have been detached


def test_sdk_invalid_limits_inactive_session_and_serialization(monkeypatch):
    from app.services import choice_sdk

    with pytest.raises(ValueError, match="timeout or call interval"):
        ChoiceSDKClient(timeout=0)
    with pytest.raises(ChoiceError, match="not active"):
        ChoiceSDKClient().request("csd", [])
    with pytest.raises(ChoiceError, match="error_code=7"):
        ChoiceSDKClient._check({"error_code": 7}, "csd")
    with pytest.raises(TypeError, match="unsupported"):
        choice_sdk._json_default(object())
    monkeypatch.setattr(choice_sdk.importlib.util, "find_spec", lambda name: None)
    assert choice_sdk.sdk_available() is False
