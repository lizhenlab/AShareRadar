"""Immutable public facts and bounded daily collection, independent of execution admission."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from itertools import islice
from pathlib import Path
import re
from typing import Any, cast

from app.artifacts.io import ArtifactIOError, ArtifactNotFoundError, canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, path_has_only_trusted_aliases, read_regular_file, sha256_hex
from app.services.market_scan_trial_registry_contract import registry_timestamp
from app.services.market_scan_trial_registry_lock import trial_registry_execution_lock
from app.services.public_execution_baostock import fetch_baostock_execution_day, normalize_baostock_execution_day
from app.services.public_execution_notices import fetch_exchange_execution_notices, normalize_exchange_execution_notices
from app.services.trading_calendar import ASHARE_TIMEZONE, latest_expected_daily_kline_date, trading_dates_between
from app.utils.clock import utc_now


PUBLIC_EXECUTION_PROVIDERS = ("baostock", "sse_public_notice", "szse_public_notice")
DEFAULT_PUBLIC_EXECUTION_ROOT = Path(__file__).resolve().parents[2] / "data/research/public_execution"
_MAX_BYTES = 24 * 1024 * 1024
_DAILY_ATTEMPTS = 12
_SCHEMA = "public-execution-source-v1"


def _identity(provider: str, session_date: str) -> None:
    day = date.fromisoformat(session_date)
    if day.isoformat() != session_date or provider not in PUBLIC_EXECUTION_PROVIDERS:
        raise ValueError("invalid public execution identity")


def _root(root: Path) -> Path:
    root = root.expanduser().absolute()
    if not path_has_only_trusted_aliases(root) or any(part in {".git", ".codex", ".agents", ".venv"} for part in root.parts):
        raise ValueError("unsafe public execution root")
    return root


def _publish(path: Path, value: object) -> None:
    exclusive_atomic_publish(path, canonical_json_bytes(value), max_bytes=_MAX_BYTES)


def _read(path: Path) -> dict[str, Any]:
    value = decode_json_bytes(read_regular_file(path, max_bytes=_MAX_BYTES))
    if not isinstance(value, dict):
        raise ValueError("invalid public execution artifact")
    return value


def _normalize(envelope: Mapping[str, object]) -> list[dict[str, object]]:
    if envelope.get("provider") == "baostock":
        rows = normalize_baostock_execution_day(envelope)
        if not rows:
            raise ValueError("empty daily vendor batch does not prove market coverage")
        return rows
    return normalize_exchange_execution_notices(envelope)


def _verify_source(source: Mapping[str, Any], provider: str, day: str) -> list[dict[str, object]]:
    if source.get("schema_version") != _SCHEMA or source.get("provider") != provider or source.get("session_date") != day:
        raise ValueError("public execution artifact identity mismatch")
    envelope = source.get("envelope")
    if not isinstance(envelope, dict) or envelope.get("provider") != provider or envelope.get("session_date") != day:
        raise ValueError("public execution response identity mismatch")
    requested = registry_timestamp(envelope.get("requested_at"), "public request time")
    observed = registry_timestamp(envelope.get("observed_at"), "public observation time")
    collected = registry_timestamp(source.get("collected_at"), "public collection time")
    if not requested <= observed <= collected or not _ready(day, requested):
        raise ValueError("invalid public execution observation interval or publication cutoff")
    return _normalize(envelope)


def read_public_execution_source(root: Path, provider: str, session_date: str, *, as_of: datetime) -> dict[str, object]:
    """Replay raw bytes; never treat a cache pointer or normalized row as authority."""
    _identity(provider, session_date)
    root = _root(root)
    cutoff = registry_timestamp(as_of.isoformat(), "public report time")
    try:
        pointer = _read(root / "sources" / provider / f"{session_date}.json")
    except ArtifactNotFoundError:
        return {"status": "missing", "rows": [], "digest": None, "observed_at": None}
    digest = pointer.get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("invalid public execution digest")
    raw = read_regular_file(root / "raw" / f"{digest}.json", max_bytes=_MAX_BYTES)
    if sha256_hex(raw) != digest:
        raise ValueError("public execution content digest mismatch")
    source = decode_json_bytes(raw)
    if not isinstance(source, dict):
        raise ValueError("invalid public execution source")
    rows = _verify_source(source, provider, session_date)
    observed = registry_timestamp(source["envelope"]["observed_at"], "public observation time")
    if observed > cutoff or registry_timestamp(source["collected_at"], "public collection time") > cutoff:
        return {"status": "pending", "rows": [], "digest": digest, "observed_at": observed.isoformat()}
    return {"status": "success", "rows": rows, "digest": digest, "observed_at": observed.isoformat(),
            "official_execution_admitted": False, "timestamp_assurance": "local-only-unverified"}


def _ready(session_date: str, now: datetime) -> bool:
    day = date.fromisoformat(session_date)
    if trading_dates_between(day, day, allow_auto_refresh=False) != (day,):
        raise ValueError("public execution collection requires an exchange session")
    if day > latest_expected_daily_kline_date(now, allow_auto_refresh=False):
        return False
    return now >= datetime.combine(day, time(17, 45), ASHARE_TIMEZONE)


def _reserve(root: Path, provider: str, day: str, now: datetime) -> dict[str, object] | None:
    directory = root / "attempts" / now.astimezone(ASHARE_TIMEZONE).date().isoformat()
    target = directory / f"{provider}-{day}.json"
    if target.exists():
        _read(target)
        return {"status": "attempted_today", "provider": provider, "session_date": day}
    if directory.exists() and len(list(islice(directory.iterdir(), _DAILY_ATTEMPTS))) >= _DAILY_ATTEMPTS:
        return {"status": "daily_budget_exhausted", "provider": provider, "session_date": day}
    _publish(target, {"provider": provider, "session_date": day, "requested_at": now.isoformat()})
    return None


def _fetch(provider: str, day: str) -> dict[str, object]:
    if provider == "baostock":
        return fetch_baostock_execution_day(day)
    return fetch_exchange_execution_notices("SH" if provider == "sse_public_notice" else "SZ", day)


def collect_public_execution_source(root: Path, provider: str, session_date: str) -> dict[str, object]:
    """Reserve before network I/O; successes are reused and failures consume the budget."""
    _identity(provider, session_date)
    root = _root(root)
    now = utc_now()
    cached = read_public_execution_source(root, provider, session_date, as_of=now)
    if cached["status"] != "missing":
        return {"provider": provider, "session_date": session_date, "status": "cached" if cached["status"] == "success" else "pending",
                "digest": cached["digest"], "row_count": len(cast(list[object], cached["rows"]))}
    if not _ready(session_date, now):
        return {"provider": provider, "session_date": session_date, "status": "pending_update"}
    with trial_registry_execution_lock(root):
        cached = read_public_execution_source(root, provider, session_date, as_of=utc_now())
        if cached["status"] != "missing":
            return {"provider": provider, "session_date": session_date, "status": "cached" if cached["status"] == "success" else "pending", "digest": cached["digest"]}
        blocked = _reserve(root, provider, session_date, utc_now())
        if blocked is not None:
            return blocked
        return _collect_reserved(root, provider, session_date)


def _collect_reserved(root: Path, provider: str, day: str) -> dict[str, object]:
    envelope = _fetch(provider, day)
    source = {"schema_version": _SCHEMA, "provider": provider, "session_date": day,
              "collected_at": utc_now().isoformat(), "envelope": envelope}
    raw = canonical_json_bytes(source)
    digest = sha256_hex(raw)
    exclusive_atomic_publish(root / "raw" / f"{digest}.json", raw, max_bytes=_MAX_BYTES)
    result: dict[str, object] = {"provider": provider, "session_date": day, "digest": digest,
                                "status": "invalid_response", "row_count": 0, "official_execution_admitted": False}
    try:
        rows = _verify_source(source, provider, day)
    except (ValueError, TypeError, KeyError, ArtifactIOError):
        result["error_type"] = "source_validation_failed"
    else:
        _publish(root / "sources" / provider / f"{day}.json", {"digest": digest})
        result.update(status="success", row_count=len(rows))
    _publish(root / "results" / f"{digest}.json", result)
    return result


def collect_public_execution_day(root: Path, session_date: str, *, providers: Sequence[str] = PUBLIC_EXECUTION_PROVIDERS) -> dict[str, object]:
    if not providers or len(set(providers)) != len(providers):
        raise ValueError("choose unique public execution providers")
    for provider in providers:
        _identity(provider, session_date)
    return {"session_date": session_date, "sources": [collect_public_execution_source(root, item, session_date) for item in providers],
            "official_execution_admitted": False, "daily_logical_request_budget": _DAILY_ATTEMPTS}


def read_public_execution_sources(
    root: Path, days: Sequence[str], *, as_of: datetime, wanted_pairs: Sequence[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], dict[str, object]]:
    unique = sorted(set(days))
    if len(unique) > 250 or wanted_pairs is None and len(unique) > 1:
        raise ValueError("multi-day public facts reads require a bounded fixed symbol set")
    wanted = _wanted_symbols(wanted_pairs)
    result = {}
    for day in unique:
        for provider in PUBLIC_EXECUTION_PROVIDERS:
            source = read_public_execution_source(root, provider, day, as_of=as_of)
            if wanted_pairs is not None:
                # Validate each complete raw batch before retaining only fixed requirements.
                source["rows"] = [row for row in cast(list[dict[str, object]], source["rows"]) if row.get("symbol") in wanted.get(day, set())]
            result[(provider, day)] = source
    return result


def _wanted_symbols(pairs: Sequence[tuple[str, str]] | None) -> dict[str, set[str]]:
    if pairs is None:
        return {}
    if len(pairs) > 200_000:
        raise ValueError("too many public facts requested pairs")
    wanted: dict[str, set[str]] = {}
    for symbol, day in pairs:
        _identity("baostock", day)
        if re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", symbol) is None:
            raise ValueError("invalid public facts requested symbol")
        wanted.setdefault(day, set()).add(symbol)
    return wanted
