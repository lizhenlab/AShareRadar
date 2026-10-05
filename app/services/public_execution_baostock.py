"""Bounded public-vendor execution facts; never official execution admission."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import importlib.metadata
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
from typing import Any, cast

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, decode_json_bytes
from app.utils.clock import monotonic_now, utc_now


MAX_BAOSTOCK_ROWS = 20_000
MAX_BAOSTOCK_BYTES = 16 * 1024 * 1024
BAOSTOCK_EXECUTION_FIELDS = (
    "date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "adjustflag", "turn",
    "tradestatus", "pctChg", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM", "isST",
)
_ENDPOINT = "query_daily_history_k_AStock"
_ERROR_TYPES = frozenset({"timeout", "process_unavailable", "worker_failed", "worker_protocol_invalid", "sdk_unavailable",
                          "sdk_error", "login_failed", "query_failed", "response_too_large", "potential_truncation"})
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _WorkerFailure(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def fetch_baostock_execution_day(session_date: str, *, timeout_seconds: float = 25) -> dict[str, object]:
    """Query one day anonymously in an owned subprocess, with a hard wall-clock bound."""
    _canonical_date(session_date)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 120:
        raise ValueError("invalid public execution timeout")
    requested = utc_now().isoformat()
    payload = _run_worker(session_date, float(timeout_seconds))
    error = payload["error_type"]
    return {"provider": "baostock", "endpoint": _ENDPOINT, "session_date": session_date,
            "requested_at": requested, "observed_at": utc_now().isoformat(), "representation": "sdk_fields_rows",
            "request": {"date": session_date}, "response": payload["response"], "sdk_version": payload["sdk_version"],
            "status": "timeout" if error == "timeout" else "error" if error else "success", "error_type": error,
            "potential_truncation": error == "potential_truncation"}


def _worker_command(session_date: str) -> list[str]:
    return [sys.executable, "-B", "-m", "app.services.public_execution_baostock", session_date]


def _run_worker(session_date: str, timeout: float) -> dict[str, object]:
    process: subprocess.Popen[bytes] | None = None
    deadline = monotonic_now() + timeout
    try:
        process = subprocess.Popen(_worker_command(session_date), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, cwd=_PROJECT_ROOT, env=_worker_environment())
        raw = _read_worker(process, deadline)
        if process.returncode != 0:
            raise _WorkerFailure("worker_failed")
        return _decode_worker(raw)
    except _WorkerFailure as exc:
        return _error_payload(exc.code)
    except OSError:
        return _error_payload("process_unavailable")
    finally:
        if process is not None:
            _kill_and_reap(process)


def _worker_environment() -> dict[str, str]:
    names = ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "LANG", "LC_ALL")
    return {**{key: os.environ[key] for key in names if key in os.environ},
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def _read_worker(process: subprocess.Popen[bytes], deadline: float) -> bytes:
    stream = process.stdout
    if stream is None:
        raise _WorkerFailure("worker_protocol_invalid")
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
        while selector.get_map() or process.poll() is None:
            remaining = deadline - monotonic_now()
            if remaining <= 0:
                raise _WorkerFailure("timeout")
            for key, _events in selector.select(min(0.05, remaining)):
                chunk = os.read(key.fd, min(65536, MAX_BAOSTOCK_BYTES - len(output) + 1))
                if not chunk:
                    selector.unregister(stream)
                output.extend(chunk)
                if len(output) > MAX_BAOSTOCK_BYTES:
                    raise _WorkerFailure("response_too_large")
    return bytes(output)


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait(timeout=2)
    finally:
        if process.stdout is not None:
            process.stdout.close()


def _decode_worker(raw: bytes) -> dict[str, object]:
    try:
        value = decode_json_bytes(raw)
        canonical_json_bytes(value)
    except (ArtifactIOError, ValueError, TypeError, RecursionError):
        raise _WorkerFailure("worker_protocol_invalid") from None
    if not isinstance(value, dict) or set(value) != {"response", "error_type", "sdk_version"}:
        raise _WorkerFailure("worker_protocol_invalid")
    _validate_worker_metadata(value)
    return cast(dict[str, object], value)


def _validate_worker_metadata(value: Mapping[str, object]) -> None:
    if value["error_type"] is not None and (not isinstance(value["error_type"], str) or value["error_type"] not in _ERROR_TYPES):
        raise _WorkerFailure("worker_protocol_invalid")
    if value["sdk_version"] is not None and (not isinstance(value["sdk_version"], str) or len(value["sdk_version"]) > 64):
        raise _WorkerFailure("worker_protocol_invalid")
    if (value["response"] is not None or value["error_type"] is None) and not isinstance(value["response"], dict):
        raise _WorkerFailure("worker_protocol_invalid")


def _error_payload(code: str, *, sdk_version: str | None = None) -> dict[str, object]:
    return {"response": None, "error_type": code, "sdk_version": sdk_version}


def _sdk_day(session_date: str) -> dict[str, object]:
    version = None
    try:
        version = importlib.metadata.version("baostock")
        import baostock as bs  # type: ignore[import-untyped]
        login = bs.login()
        if getattr(login, "error_code", None) != "0":
            return _error_payload("login_failed", sdk_version=version)
        try:
            result = bs.query_daily_history_k_AStock(date=session_date)
            return _sdk_result(result, version)
        finally:
            bs.logout()
    except (ImportError, importlib.metadata.PackageNotFoundError):
        return _error_payload("sdk_unavailable", sdk_version=version)
    except Exception:
        # The SDK boundary emits only a short category, never provider text.
        return _error_payload("sdk_error", sdk_version=version)


def _sdk_result(result: Any, version: str) -> dict[str, object]:
    # This endpoint is a single page. ResultData.next() can accidentally paginate
    # at exactly 2000 rows in SDK 0.9.3, so read its delivered page directly.
    response = {"fields": result.fields, "rows": result.data, "error_code": result.error_code}
    rows = response["rows"]
    if isinstance(rows, list) and len(rows) > MAX_BAOSTOCK_ROWS:
        return _error_payload("response_too_large", sdk_version=version)
    code = None if result.error_code == "0" else "query_failed"
    if isinstance(rows, list) and len(rows) == MAX_BAOSTOCK_ROWS:
        code = "potential_truncation"
    payload: dict[str, object] = {"response": response, "error_type": code, "sdk_version": version}
    if len(canonical_json_bytes(payload)) > MAX_BAOSTOCK_BYTES:
        return _error_payload("response_too_large", sdk_version=version)
    return payload


def normalize_baostock_execution_day(envelope: Mapping[str, object]) -> list[dict[str, object]]:
    """Parse explicit unadjusted facts; unknown/invalid values fail the whole batch."""
    day, response = _normalization_source(envelope)
    fields, rows = response.get("fields"), response.get("rows")
    if not isinstance(fields, list) or tuple(fields) != BAOSTOCK_EXECUTION_FIELDS:
        raise ValueError("invalid BaoStock execution fields")
    if not isinstance(rows, list) or len(rows) >= MAX_BAOSTOCK_ROWS:
        raise ValueError("invalid or potentially truncated BaoStock execution rows")
    output, seen = [], set()
    for values in rows:
        row = _normalize_row(values, day)
        if row["symbol"] in seen:
            raise ValueError("duplicate BaoStock symbol/session")
        seen.add(row["symbol"])
        output.append(row)
    return output


def _normalization_source(envelope: Mapping[str, object]) -> tuple[str, dict[str, object]]:
    day = _canonical_date(envelope.get("session_date"))
    identity = (envelope.get("provider"), envelope.get("endpoint"), envelope.get("representation"))
    if identity != ("baostock", _ENDPOINT, "sdk_fields_rows") or envelope.get("request") != {"date": day}:
        raise ValueError("BaoStock execution source identity mismatch")
    if envelope.get("status") != "success" or envelope.get("error_type") is not None or envelope.get("potential_truncation") is not False:
        raise ValueError("BaoStock execution response is unavailable or potentially truncated")
    response = envelope.get("response")
    if not isinstance(response, dict) or set(response) != {"fields", "rows", "error_code"} or response["error_code"] != "0":
        raise ValueError("invalid BaoStock execution response")
    if len(canonical_json_bytes(dict(envelope))) > MAX_BAOSTOCK_BYTES:
        raise ValueError("BaoStock execution response exceeds byte limit")
    return day, response


def _normalize_row(values: object, day: str) -> dict[str, object]:
    if not isinstance(values, list) or len(values) != len(BAOSTOCK_EXECUTION_FIELDS) or any(not isinstance(value, str) for value in values):
        raise ValueError("invalid BaoStock execution row shape")
    raw = dict(zip(BAOSTOCK_EXECUTION_FIELDS, values, strict=True))
    if raw["date"] != day or raw["adjustflag"] != "3":
        raise ValueError("BaoStock execution date or unadjusted basis mismatch")
    symbol = _stock_symbol(raw["code"])
    _validate_optional_numbers(raw)
    if raw["tradestatus"] not in {"0", "1"} or raw["isST"] not in {"0", "1"}:
        raise ValueError("unknown BaoStock trade or ST state")
    prices = {key: _number(raw[key], positive=True) for key in ("open", "high", "low", "close")}
    state = "trading" if raw["tradestatus"] == "1" else "suspended"
    volume, amount = (_activity_number(raw[key], state) for key in ("volume", "amount"))
    _validate_market_values(prices, volume, amount, state)
    return {"symbol": symbol, "session_date": day, "trade_state": state, "is_st": raw["isST"] == "1",
            "reference_price": _number(raw["preclose"], positive=True), **prices, "volume": volume, "amount": amount,
            "adjustment_mode": "unadjusted", "volume_unit": "shares", "amount_unit": "CNY",
            "missing_fields": [key for key in ("volume", "amount") if raw[key] == ""]}


def _validate_market_values(prices: Mapping[str, float], volume: float | None, amount: float | None, state: str) -> None:
    low, high = prices["low"], prices["high"]
    if not low <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= high:
        raise ValueError("inconsistent BaoStock OHLC")
    if state == "suspended" and (len(set(prices.values())) != 1 or volume not in {None, 0} or amount not in {None, 0}):
        raise ValueError("inconsistent BaoStock suspended-day values")


def _activity_number(value: str, state: str) -> float | None:
    # Observed daily batches omit suspended-day activity despite documentation
    # describing zero. Retain that missing field; do not manufacture a zero.
    return None if state == "suspended" and value == "" else _number(value, positive=False)


def _number(value: str, *, positive: bool) -> float:
    if re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value) is None:
        raise ValueError("missing or noncanonical BaoStock numeric value")
    try:
        parsed = float(value)
    except ValueError:
        raise ValueError("invalid BaoStock numeric value") from None
    if not math.isfinite(parsed) or parsed < 0 or positive and parsed == 0:
        raise ValueError("invalid BaoStock numeric range")
    return parsed


def _validate_optional_numbers(raw: Mapping[str, str]) -> None:
    for key in ("turn", "pctChg", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"):
        value = raw[key]
        if value == "":
            continue
        try:
            parsed = float(value)
        except ValueError:
            raise ValueError("invalid optional BaoStock numeric value") from None
        if value != value.strip() or not math.isfinite(parsed) or key == "turn" and parsed < 0:
            raise ValueError("invalid optional BaoStock numeric range")


def _stock_symbol(value: str) -> str:
    if re.fullmatch(r"(?:sh|sz)\.[0-9]{6}", value) is None:
        raise ValueError("unsupported or noncanonical BaoStock stock code")
    market, code = value.split(".")
    return f"{code}.{market.upper()}"


def _canonical_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid BaoStock session date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid BaoStock session date") from None
    if parsed.isoformat() != value:
        raise ValueError("noncanonical BaoStock session date")
    return value


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        day = _canonical_date(sys.argv[1])
    except ValueError:
        return 2
    with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
        payload = _sdk_day(day)
    sys.stdout.buffer.write(canonical_json_bytes(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
