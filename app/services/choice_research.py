"""Choice research contracts. Retrospective observations never grant PIT authority."""

from __future__ import annotations

from datetime import date, datetime
import math
import re
from typing import Any

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.choice_research_store import Record, SCHEMA_VERSION
from app.services.choice_sdk import ChoiceError


DAILY_FIELDS = ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "AMOUNT", "PRECLOSE", "TURN", "TRADESTATUS", "HIGHLIMIT", "LOWLIMIT", "ISSTSTOCK", "ISXSTSTOCK", "TAFACTOR"]
METADATA_FIELDS = ["NAME", "HISNAME", "LISTDATE", "DELISTDATE", "STATUS", "TRADESTATUS", "SUSPENDREASON", "PRECLOSEEXCH", "LIMITUPPRICE", "LIMITDOWNPRICE"]
DIVIDEND_FIELDS = ["DIVIMPLANNCDATE", "DIVRECORDDATE", "DIVEXDATE", "DIVCASHPSBFTAX", "DIVSTOCKPS", "DIVCAPITALIZATIONPS", "DIVPAYDATE", "DIVBONUSLISTEDDATE"]
EVENT_FIELDS = ["SECUCODE", "DIVIMPLANNCDATE", "DIVEXDATE", "DIVCASHPSBFTAX", "DIVSTOCKPSRATIO", "DIVCAPITPSRATIO"]
REFERENCE_FIELDS = ["PRECLOSEEXCH", "LIMITUPPRICE", "LIMITDOWNPRICE"]
SUSPENSION_FIELDS = ["TRADESTATUS", "SUSPENDREASON", "SUSPENDSDATE", "SUSPENDEDATE"]
OPTIONS = "RECVtimeout=15,Ispandas=0"
SYMBOL = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)")


def iso_date(value: Any) -> str:
    if isinstance(value, str):
        for format_text in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, format_text).date().isoformat()
            except ValueError:
                continue
    raise ChoiceError("invalid Choice date value")


def symbol_text(value: Any) -> str:
    if not isinstance(value, str) or SYMBOL.fullmatch(value) is None:
        raise ChoiceError("invalid/non-A-share Choice symbol")
    return value


def make_plan(start: str, end: str, limit: int, preferred: list[str], event_symbols: list[str]) -> dict[str, Any]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first < date(2022, 1, 1) or first > last or (last - first).days > 1100:
        raise ChoiceError("first-batch history must be within 2022 onward and at most 1100 calendar days")
    if limit < 3 or limit > 225 or limit % 3:
        raise ChoiceError("symbol limit must be a multiple of 3 between 3 and 225")
    for symbol in preferred + event_symbols:
        symbol_text(symbol)
    if len(set(event_symbols)) > 3:
        raise ChoiceError("at most three dividend event samples per dataset")
    return {
        "schema_version": SCHEMA_VERSION, "start_date": start, "end_date": end,
        "symbol_limit": limit, "preferred_symbols": sorted(set(preferred + event_symbols)),
        "event_symbols": sorted(set(event_symbols)),
        "universe_frequency": "month_end_plus_boundaries", "metadata_frequency": "same_as_universe",
        "selection": "sha256_balanced_union_of_monthly_snapshots_plus_explicit_diagnostic_symbols",
        "daily_fields": DAILY_FIELDS, "metadata_fields": METADATA_FIELDS, "dividend_fields": DIVIDEND_FIELDS,
        "daily_adjustment": "none", "volume_unit": "shares", "amount_unit": "CNY",
        "official": False, "formal_equivalent_pit": False, "filter_qualified": False,
        "production_ranking_effect": "none",
        "limitations": [
            "retrospective_download_not_original_provider_vintage",
            "monthly_universe_not_complete_daily_membership",
            "monthly_price_limit_metadata_not_complete_daily_execution_path",
            "dividend_report_snapshots_and_event_samples_not_all_corporate_actions",
            "current_listing_status_and_delist_dates_not_historically_known_features",
            "daily_bars_do_not_prove_open_order_execution",
            "not_a_licensed_source_registry_or_production_model_authorization",
        ],
    }


def snapshot_dates(sessions: list[str]) -> list[str]:
    if not sessions or sessions != sorted(set(sessions)):
        raise ChoiceError("empty, duplicate or unordered Choice trading calendar")
    months = {session[:7]: session for session in sessions}
    return sorted({sessions[0], sessions[-1], *months.values()})


def choose_symbols(universe: set[str], plan: dict[str, Any]) -> list[str]:
    preferred = set(plan["preferred_symbols"])
    if not preferred <= universe:
        raise ChoiceError("preferred symbol absent from every archived universe snapshot")
    per_market = plan["symbol_limit"] // 3
    result: list[str] = []
    for market in ("SH", "SZ", "BJ"):
        candidates = [symbol for symbol in universe if symbol.endswith(f".{market}")]
        if len(candidates) < per_market or sum(symbol in preferred for symbol in candidates) > per_market:
            raise ChoiceError("requested balanced market sample cannot be satisfied")
        candidates.sort(key=lambda symbol: (symbol not in preferred, sha256_hex(symbol)))
        result.extend(candidates[:per_market])
    return sorted(result)


def request(kind: str, method: str, args: list[str], *, as_of: str = "", symbols: list[str] | None = None,
            fields: list[str] | None = None, sessions: list[str] | None = None) -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "kind": kind, "method": method, "args": args,
            "as_of": as_of, "symbols": symbols or [], "fields": fields or [], "sessions": sessions or []}


def normalize(payload: dict[str, Any]) -> list[Record]:
    descriptor, result = payload["request"], payload["result"]
    if result.get("error_code") != 0:
        raise ChoiceError("failed API response cannot be projected")
    normalizers = {"calendar": _calendar, "universe": _universe, "daily": _daily, "metadata": _cross_section,
                   "dividend_snapshot": _cross_section, "dividend_event": _events,
                   "execution_reference": _execution_section, "suspension_detail": _execution_section}
    try:
        records = normalizers[descriptor["kind"]](descriptor, result)
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise ChoiceError("Choice response failed strict shape validation") from exc
    # Finite canonical JSON is also required before a request becomes complete.
    canonical_json_bytes([list(record) for record in records])
    return records


def _calendar(descriptor: dict[str, Any], result: dict[str, Any]) -> list[Record]:
    dates = [iso_date(value) for value in result["dates"]]
    snapshot_dates(dates)
    start, end = descriptor["args"][:2]
    if dates[0] < start or dates[-1] > end:
        raise ChoiceError("Choice calendar outside requested interval")
    return [("calendar", "CNSESH", day, {"session_date": day}) for day in dates]


def _universe(descriptor: dict[str, Any], result: dict[str, Any]) -> list[Record]:
    if (descriptor["method"] != "sector" or descriptor["args"][:2] != ["001071", descriptor["as_of"]]
            or [iso_date(value) for value in result["dates"]] != [descriptor["as_of"]]):
        raise ChoiceError("Choice universe date/sector does not match the requested historical snapshot")
    if result["indicators"] != ["SECUCODE", "SECURITYSHORTNAME"]:
        raise ChoiceError("unexpected Choice universe columns")
    codes, values = result["codes"], result["data"]
    if len(values) != len(codes) * 2 or len(set(codes)) != len(codes):
        raise ChoiceError("incomplete/duplicate Choice universe")
    records: list[Record] = []
    for index, code in enumerate(codes):
        symbol = symbol_text(code)
        if values[index * 2] != symbol:
            raise ChoiceError("universe code identity mismatch")
        records.append(("universe", symbol, descriptor["as_of"], {
            "reported_name_at_download": values[index * 2 + 1], "name_is_point_in_time": False,
        }))
    if {code[-2:] for code in codes} != {"SH", "SZ", "BJ"}:
        raise ChoiceError("Choice full A-share universe is missing a market")
    return records


def _columns(descriptor: dict[str, Any], result: dict[str, Any]) -> None:
    if result["indicators"] != descriptor["fields"]:
        raise ChoiceError("Choice response field order/coverage mismatch")
    if set(result["data"]) != set(descriptor["symbols"]):
        raise ChoiceError("Choice response symbol coverage mismatch")


def _number(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ChoiceError("invalid non-finite/non-numeric Choice value")
    return value


def _yes_no(value: Any) -> bool | None:
    if value is None:
        return None
    if value not in {"是", "否"}:
        raise ChoiceError("unknown Choice yes/no state")
    return value == "是"


def _bar_quality(row: dict[str, Any]) -> str:
    state = row["TRADESTATUS"]
    if state in {"连续停牌", "停牌一天", "暂停上市"}:
        return "suspended"
    if state in {"未上市", "终止上市", "非交易日", "资产重组弃用"}:
        return "not_trading"
    if state not in {"正常交易", "复牌"}:
        return "unknown_or_intraday_restricted_state"
    return _trading_bar_quality(row)


def _trading_bar_quality(row: dict[str, Any]) -> str:
    prices = [row[key] for key in ("OPEN", "HIGH", "LOW", "CLOSE")]
    if any(value is None for value in prices + [row["VOLUME"], row["AMOUNT"]]):
        return "incomplete_trading_bar"
    if min(prices) <= 0 or not row["LOW"] <= min(row["OPEN"], row["CLOSE"]) <= max(row["OPEN"], row["CLOSE"]) <= row["HIGH"]:
        raise ChoiceError("invalid Choice OHLC relationships")
    if row["VOLUME"] < 0 or row["AMOUNT"] < 0:
        raise ChoiceError("negative Choice volume/amount")
    if row["VOLUME"] == 0 or row["AMOUNT"] == 0:
        return "no_trades"
    if len(set(prices)) == 1:
        return _single_price_quality(row)
    return "traded_bar_not_execution_proof"


def _single_price_quality(row: dict[str, Any]) -> str:
    if row["HIGHLIMIT"] is True:
        return "single_price_limit_up"
    if row["LOWLIMIT"] is True:
        return "single_price_limit_down"
    return "single_price_unknown"


def _daily(descriptor: dict[str, Any], result: dict[str, Any]) -> list[Record]:
    _columns(descriptor, result)
    dates = [iso_date(value) for value in result["dates"]]
    if dates != descriptor["sessions"]:
        raise ChoiceError("Choice daily response does not cover the exact requested calendar")
    output: list[Record] = []
    for symbol, columns in result["data"].items():
        if len(columns) != len(DAILY_FIELDS) or any(len(column) != len(dates) for column in columns):
            raise ChoiceError("truncated Choice daily matrix")
        for offset, day in enumerate(dates):
            row = dict(zip(DAILY_FIELDS, [column[offset] for column in columns], strict=True))
            for name in DAILY_FIELDS:
                if name in {"HIGHLIMIT", "LOWLIMIT", "ISSTSTOCK", "ISXSTSTOCK"}:
                    row[name] = _yes_no(row[name])
                elif name != "TRADESTATUS":
                    row[name] = _number(row[name])
            row["quality"] = _bar_quality(row)
            row["adjustment"] = "none"
            row["execution_eligible"] = None
            output.append(("daily", symbol_text(symbol), day, row))
    return output


def _cross_section(descriptor: dict[str, Any], result: dict[str, Any]) -> list[Record]:
    _columns(descriptor, result)
    if descriptor["kind"] == "metadata" and [iso_date(value) for value in result["dates"]] != [descriptor["as_of"]]:
        raise ChoiceError("Choice metadata date does not match the historical snapshot")
    output: list[Record] = []
    for symbol, values in result["data"].items():
        row = dict(zip(descriptor["fields"], values, strict=True))
        for field, value in list(row.items()):
            if field.endswith("DATE") and value is not None:
                row[field] = iso_date(value)
            elif field in {"PRECLOSEEXCH", "LIMITUPPRICE", "LIMITDOWNPRICE", "DIVCASHPSBFTAX", "DIVSTOCKPS", "DIVCAPITALIZATIONPS"}:
                row[field] = _number(value)
        row["original_vintage_available"] = False
        if descriptor["kind"] == "metadata":
            row["STATUS_is_current_not_historical"] = True
        else:
            row["null_is_not_proof_of_no_event"] = True
        output.append((descriptor["kind"], symbol_text(symbol), descriptor["as_of"], row))
    return output


def _events(descriptor: dict[str, Any], result: dict[str, Any]) -> list[Record]:
    if result["indicators"] != EVENT_FIELDS:
        raise ChoiceError("unexpected Choice dividend event columns")
    output: list[Record] = []
    for values in result["data"].values():
        row = dict(zip(EVENT_FIELDS, values, strict=True))
        symbol = symbol_text(row["SECUCODE"])
        if symbol not in descriptor["symbols"]:
            raise ChoiceError("Choice event belongs to an unrequested security")
        for key in ("DIVIMPLANNCDATE", "DIVEXDATE"):
            row[key] = iso_date(row[key]) if row[key] is not None else None
        for key in ("DIVCASHPSBFTAX", "DIVSTOCKPSRATIO", "DIVCAPITPSRATIO"):
            row[key] = _number(row[key])
        row["event_digest"] = sha256_hex(canonical_json_bytes(row))
        row["complete_corporate_action_coverage"] = False
        output.append(("dividend_event", symbol, row["DIVEXDATE"] or descriptor["as_of"], row))
    return output


def _execution_value(field: str, value: Any) -> Any:
    if field in REFERENCE_FIELDS:
        parsed = _number(value)
        if parsed is not None and parsed < 0:
            raise ChoiceError("negative execution reference price")
        return parsed
    if field.endswith("DATE") and value is not None:
        return iso_date(value)
    if value is not None and not isinstance(value, str):
        raise ChoiceError("invalid suspension state/reason")
    return value


def _execution_section(descriptor: dict[str, Any], result: dict[str, Any]) -> list[Record]:
    fields = REFERENCE_FIELDS if descriptor["kind"] == "execution_reference" else SUSPENSION_FIELDS
    if descriptor["fields"] != fields:
        raise ChoiceError("unexpected execution research fields")
    _columns(descriptor, result)
    if [iso_date(value) for value in result["dates"]] != [descriptor["as_of"]]:
        raise ChoiceError("Choice execution reference date mismatch")
    output: list[Record] = []
    for symbol, values in result["data"].items():
        row = {field: _execution_value(field, value) for field, value in zip(fields, values, strict=True)}
        if descriptor["kind"] == "execution_reference":
            upper, lower = row["LIMITUPPRICE"], row["LIMITDOWNPRICE"]
            if upper is not None and lower is not None and upper > 0 and lower > upper:
                raise ChoiceError("reversed execution reference price limits")
            row["null_or_zero_is_not_proof_of_no_limit"] = True
        else:
            row["suspension_end_is_retrospective_not_known_at_start"] = True
        row["execution_eligible"] = None
        row["original_vintage_available"] = False
        output.append((descriptor["kind"], symbol_text(symbol), descriptor["as_of"], row))
    return output


def report_dates(start: str, end: str) -> list[str]:
    result = []
    for year in range(int(start[:4]) - 1, int(end[:4]) + 1):
        for suffix in ("03-31", "06-30", "09-30", "12-31"):
            value = f"{year}-{suffix}"
            if value <= end and value >= f"{int(start[:4]) - 1}-12-31":
                result.append(value)
    return result
