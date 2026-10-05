"""Isolated vendor requests retain raw facts without inventing execution authority."""

from copy import deepcopy
from datetime import datetime
import io
import json
import sys
from types import SimpleNamespace

import pytest

from app.artifacts.io import canonical_json_bytes
from app.services import public_execution_baostock as source


DAY = "2026-09-18"


def _row(**changes):
    row = dict(zip(source.BAOSTOCK_EXECUTION_FIELDS, [
        DAY, "sh.600000", "10", "11", "9", "10.5", "9.5", "1000000", "10000000", "3", "1.2", "1", "5",
        "10", "1", "2", "3", "0",
    ], strict=True))
    row.update(changes)
    return [row[key] for key in source.BAOSTOCK_EXECUTION_FIELDS]


def _envelope(rows=None):
    return {"provider": "baostock", "endpoint": "query_daily_history_k_AStock", "session_date": DAY,
            "request": {"date": DAY}, "representation": "sdk_fields_rows", "requested_at": "2026-09-18T10:00:00+00:00",
            "observed_at": "2026-09-18T10:00:01+00:00", "sdk_version": "0.9.3", "status": "success", "error_type": None,
            "potential_truncation": False,
            "response": {"fields": list(source.BAOSTOCK_EXECUTION_FIELDS), "rows": rows if rows is not None else [_row()], "error_code": "0"}}


def _worker_payload(rows=None):
    return {key: _envelope(rows)[key] for key in ("response", "error_type", "sdk_version")}


def _command(monkeypatch, script):
    monkeypatch.setattr(source, "_worker_command", lambda _day: [sys.executable, "-B", "-c", script])


def _sdk(monkeypatch, *, rows=None, code="0", login_code="0", query_error=None):
    calls = []
    result = SimpleNamespace(fields=list(source.BAOSTOCK_EXECUTION_FIELDS), data=rows if rows is not None else [_row()], error_code=code)
    def query(**kwargs):
        calls.append(("query", kwargs))
        if query_error:
            raise query_error
        return result
    def login():
        calls.append(("login",))
        return SimpleNamespace(error_code=login_code, error_msg="never expose token-secret")
    fake = SimpleNamespace(login=login, logout=lambda: calls.append(("logout",)), query_daily_history_k_AStock=query)
    monkeypatch.setitem(sys.modules, "baostock", fake)
    monkeypatch.setattr(source.importlib.metadata, "version", lambda _name: "0.9.3")
    return result, calls


def test_trading_facts_retain_exchange_reference_and_explicit_units():
    envelope = _envelope()
    before = deepcopy(envelope)
    row, = source.normalize_baostock_execution_day(envelope)
    assert row == {"symbol": "600000.SH", "session_date": DAY, "trade_state": "trading", "is_st": False,
                   "reference_price": 9.5, "open": 10, "high": 11, "low": 9, "close": 10.5,
                   "volume": 1000000, "amount": 10000000, "adjustment_mode": "unadjusted", "volume_unit": "shares", "amount_unit": "CNY", "missing_fields": []}
    assert envelope == before
    assert not {"official", "previous_close", "entry_tradable", "limit_up_price"}.intersection(row)


def test_suspended_ohlc_is_not_inferred_as_trade_and_reference_stays_independent():
    row, = source.normalize_baostock_execution_day(_envelope([_row(
        code="sz.000001", open="10", high="10", low="10", close="10", preclose="9", volume="0", amount="0", turn="", tradestatus="0", isST="1",
    )]))
    assert row["trade_state"] == "suspended" and row["is_st"] is True
    assert row["open"] == 10 and row["reference_price"] == 9 and row["symbol"] == "000001.SZ"


@pytest.mark.parametrize("field,value", [
    ("date", "2026-09-17"), ("date", "20260918"), ("code", "bj.920001"), ("code", "SH.600000"), ("code", "sh.６０００００"),
    ("adjustflag", "2"), ("adjustflag", ""), ("tradestatus", ""), ("tradestatus", "2"), ("isST", "false"),
    ("open", "NaN"), ("close", "inf"), ("high", "9"), ("low", "12"), ("preclose", "0"),
    ("volume", "-1"), ("amount", "-2"), ("volume", ""), ("open", "1e309"), ("open", " 10"), ("volume", "1_000"),
    ("turn", "NaN"), ("turn", "-1"), ("peTTM", "Infinity"), ("pctChg", "not-a-number"),
])
def test_bad_row_never_turns_into_partial_valid_facts(field, value):
    with pytest.raises(ValueError):
        source.normalize_baostock_execution_day(_envelope([_row(code="sz.000001"), _row(**{field: value})]))


@pytest.mark.parametrize("mutation", ["duplicate", "short", "long", "numeric_type", "unknown_field", "duplicate_field", "wrong_day", "failed", "truncated", "basis"])
def test_strict_response_contract_rejects_ambiguity(mutation):
    envelope = _envelope()
    response = envelope["response"]
    if mutation == "duplicate":
        response["rows"].append(_row())
    elif mutation == "short":
        response["rows"][0].pop()
    elif mutation == "long":
        response["rows"][0].append("extra")
    elif mutation == "numeric_type":
        response["rows"][0][2] = 10.0
    elif mutation == "unknown_field":
        response["fields"][-1] = "unknown"
    elif mutation == "duplicate_field":
        response["fields"][-1] = "date"
    elif mutation == "wrong_day":
        envelope["request"]["date"] = "2026-09-17"
    elif mutation == "failed":
        response["error_code"] = "1001"
    elif mutation == "truncated":
        envelope["potential_truncation"] = True
    else:
        envelope["representation"] = "http_bytes"
    with pytest.raises(ValueError):
        source.normalize_baostock_execution_day(envelope)


@pytest.mark.parametrize("field,value", [("volume", "1"), ("amount", "1"), ("high", "11")])
def test_suspended_fact_requires_flat_ohlc_and_zero_activity(field, value):
    values = {"open": "10", "high": "10", "low": "10", "close": "10", "volume": "0", "amount": "0", "tradestatus": "0", field: value}
    with pytest.raises(ValueError, match="suspended"):
        source.normalize_baostock_execution_day(_envelope([_row(**values)]))


def test_empty_result_stays_empty_and_does_not_assert_suspension():
    assert source.normalize_baostock_execution_day(_envelope([])) == []


def test_sdk_delivered_page_is_read_once_without_next_pagination(monkeypatch):
    result, calls = _sdk(monkeypatch, rows=[_row()] * 2000)
    result.next = lambda: pytest.fail("daily batch must not paginate")
    result.get_data = lambda: pytest.fail("DataFrame helper must not paginate")
    payload = source._sdk_day(DAY)
    assert len(payload["response"]["rows"]) == 2000 and payload["error_type"] is None
    assert calls == [("login",), ("query", {"date": DAY}), ("logout",)]


@pytest.mark.parametrize("count,expected", [(20000, "potential_truncation"), (20001, "response_too_large")])
def test_sdk_row_upper_bound_is_not_claimed_complete(monkeypatch, count, expected):
    _sdk(monkeypatch, rows=[_row()] * count)
    payload = source._sdk_day(DAY)
    assert payload["error_type"] == expected
    assert (payload["response"] is None) == (count > 20000)
    with pytest.raises(ValueError):
        source.normalize_baostock_execution_day(_envelope([_row()] * 20000))


def test_sdk_raw_shape_is_archivable_before_normalization(monkeypatch):
    result, _calls = _sdk(monkeypatch)
    result.fields = ["bad-field"]
    payload = source._sdk_day(DAY)
    assert payload["error_type"] is None and payload["response"]["fields"] == ["bad-field"]
    assert canonical_json_bytes(payload)


@pytest.mark.parametrize("kind", ["login", "query", "exception", "uninstalled", "oversize"])
def test_sdk_failures_are_safe_and_cleanup_is_owned(monkeypatch, kind):
    _result, calls = _sdk(monkeypatch, login_code="bad" if kind == "login" else "0", code="1001" if kind == "query" else "0",
                          query_error=RuntimeError("secret provider body") if kind == "exception" else None)
    if kind == "uninstalled":
        def missing(_name):
            raise source.importlib.metadata.PackageNotFoundError("secret path")
        monkeypatch.setattr(source.importlib.metadata, "version", missing)
    if kind == "oversize":
        monkeypatch.setattr(source, "MAX_BAOSTOCK_BYTES", 32)
    payload = source._sdk_day(DAY)
    assert payload["error_type"] == {"login": "login_failed", "query": "query_failed", "exception": "sdk_error",
                                     "uninstalled": "sdk_unavailable", "oversize": "response_too_large"}[kind]
    assert "secret" not in json.dumps(payload)
    assert bool(calls and calls[-1] == ("logout",)) == (kind not in {"login", "uninstalled"})


def test_real_subprocess_round_trip_preserves_raw_rows_and_utc_timing(monkeypatch):
    raw = canonical_json_bytes(_worker_payload())
    _command(monkeypatch, f"import sys; sys.stdout.buffer.write({raw!r})")
    result = source.fetch_baostock_execution_day(DAY, timeout_seconds=2)
    assert result["status"] == "success" and result["response"] == _envelope()["response"]
    assert datetime.fromisoformat(result["observed_at"]) >= datetime.fromisoformat(result["requested_at"])
    assert datetime.fromisoformat(result["observed_at"]).utcoffset().total_seconds() == 0
    assert source.normalize_baostock_execution_day(result)[0]["symbol"] == "600000.SH"


def test_timeout_kills_and_reaps_nonresponsive_worker(monkeypatch):
    _command(monkeypatch, "import time; time.sleep(30)")
    original, started = source.subprocess.Popen, []
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        started.append(process)
        return process
    monkeypatch.setattr(source.subprocess, "Popen", launch)
    result = source.fetch_baostock_execution_day(DAY, timeout_seconds=.1)
    assert result["status"] == "timeout" and result["error_type"] == "timeout" and result["response"] is None
    assert started[0].poll() is not None and started[0].stdout.closed


def test_oversized_worker_output_is_killed_before_unbounded_read(monkeypatch):
    monkeypatch.setattr(source, "MAX_BAOSTOCK_BYTES", 1024)
    _command(monkeypatch, "import os,time; os.write(1,b'x'*2048); time.sleep(30)")
    result = source.fetch_baostock_execution_day(DAY, timeout_seconds=2)
    assert result["status"] == "error" and result["error_type"] == "response_too_large"


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b'{"response":NaN}', b'{"response":null,"error_type":[],"sdk_version":"0.9.3"}',
                                  b'{"response":null,"error_type":"provider-secret","sdk_version":"0.9.3"}'])
def test_malformed_worker_response_never_leaks_raw_text(monkeypatch, raw):
    _command(monkeypatch, f"import sys; sys.stdout.buffer.write({raw!r})")
    result = source.fetch_baostock_execution_day(DAY, timeout_seconds=2)
    assert result["status"] == "error" and result["error_type"] == "worker_protocol_invalid"
    assert "provider-secret" not in json.dumps(result)


def test_nonzero_worker_exit_is_a_safe_failure(monkeypatch):
    _command(monkeypatch, "import sys; print('provider-secret',file=sys.stderr); sys.exit(1)")
    assert source.fetch_baostock_execution_day(DAY, timeout_seconds=2)["error_type"] == "worker_failed"


def test_spawn_failure_does_not_expose_exception(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("provider-secret")
    monkeypatch.setattr(source.subprocess, "Popen", fail)
    result = source.fetch_baostock_execution_day(DAY)
    assert result["error_type"] == "process_unavailable" and "provider-secret" not in json.dumps(result)


@pytest.mark.parametrize("day,timeout", [("20260918", 2), ("2026-02-30", 2), (None, 2), (DAY, 0), (DAY, 121), (DAY, float("nan")), (DAY, True)])
def test_invalid_request_never_starts_provider(monkeypatch, day, timeout):
    monkeypatch.setattr(source.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("must validate before spawn"))
    with pytest.raises(ValueError):
        source.fetch_baostock_execution_day(day, timeout_seconds=timeout)


def test_worker_environment_does_not_forward_account_secrets(monkeypatch):
    monkeypatch.setenv("ASHARE_RADAR_TUSHARE_TOKEN", "secret")
    monkeypatch.setenv("FUYAO_API_KEY", "secret")
    assert not {"ASHARE_RADAR_TUSHARE_TOKEN", "FUYAO_API_KEY"}.intersection(source._worker_environment())
    assert source._worker_command(DAY)[-2:] == ["app.services.public_execution_baostock", DAY]


def test_worker_main_suppresses_sdk_stdout_and_stderr(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["worker", DAY])
    def sdk(_day):
        print("secret SDK log")
        print("secret SDK error", file=sys.stderr)
        return _worker_payload()
    monkeypatch.setattr(source, "_sdk_day", sdk)
    binary = io.BytesIO()
    stream = io.TextIOWrapper(binary)
    monkeypatch.setattr(sys, "stdout", stream)
    assert source.main() == 0
    stream.flush()
    assert json.loads(binary.getvalue()) == _worker_payload()
    assert b"secret" not in binary.getvalue()


def test_cleanup_reaps_already_exited_process_and_tolerates_kill_race():
    class Process:
        stdout = io.BytesIO()
        waited = False
        def poll(self):
            return None
        def kill(self):
            raise ProcessLookupError
        def wait(self, timeout):
            self.waited = True
    process = Process()
    source._kill_and_reap(process)
    assert process.waited and process.stdout.closed


def test_caller_interrupt_still_kills_and_reaps_owned_worker(monkeypatch):
    _command(monkeypatch, "import time; time.sleep(30)")
    original, started = source.subprocess.Popen, []
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        started.append(process)
        return process
    def interrupted(*_args):
        raise KeyboardInterrupt
    monkeypatch.setattr(source.subprocess, "Popen", launch)
    monkeypatch.setattr(source, "_read_worker", interrupted)
    with pytest.raises(KeyboardInterrupt):
        source.fetch_baostock_execution_day(DAY)
    assert started[0].poll() is not None and started[0].stdout.closed


def test_empty_success_worker_protocol_cannot_be_reported_as_success(monkeypatch):
    raw = canonical_json_bytes({"response": None, "sdk_version": "0.9.3", "error_type": None})
    _command(monkeypatch, f"import sys; sys.stdout.buffer.write({raw!r})")
    assert source.fetch_baostock_execution_day(DAY, timeout_seconds=2)["error_type"] == "worker_protocol_invalid"


def test_observed_vendor_suspension_with_empty_activity_preserves_unknowns():
    # The 2026-09-18 daily batch reported this real shape for 12 suspended names.
    suspended = _row(code="sh.600301", open="45.4300", high="45.4300", low="45.4300", close="45.4300",
                     preclose="45.4300", volume="", amount="", turn="", tradestatus="0")
    rows = source.normalize_baostock_execution_day(_envelope([_row(code="sz.000001"), suspended]))
    assert len(rows) == 2 and rows[0]["trade_state"] == "trading"
    assert rows[1]["trade_state"] == "suspended" and rows[1]["volume"] is None and rows[1]["amount"] is None
    assert rows[1]["missing_fields"] == ["volume", "amount"]


def test_suspended_activity_can_preserve_one_missing_field_without_erasing_known_zero():
    row, = source.normalize_baostock_execution_day(_envelope([_row(
        open="10", high="10", low="10", close="10", volume="", amount="0", tradestatus="0",
    )]))
    assert row["volume"] is None and row["amount"] == 0 and row["missing_fields"] == ["volume"]
