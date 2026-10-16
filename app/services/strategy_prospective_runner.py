"""Local deterministic capture loop; control records never alter frozen studies."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime, time
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, cast
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from app.artifacts.io import (
    ArtifactIOError, canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish,
    path_has_only_trusted_aliases, read_regular_file, sha256_hex,
)
from app.services.market_scan_trial_registry_contract import registry_hash, registry_identifier
from app.services.market_scan_trial_registry_lock import trial_registry_execution_lock
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE
from app.services.strategy_prospective_collection import collect_strategy_prospective_inputs
from app.services.strategy_prospective_plan import read_strategy_prospective_state, strategy_prospective_status
from app.services.trading_calendar import ASHARE_TIMEZONE
from app.utils.clock import utc_now
from app.utils.database_identity import database_identity


_MAX_CONTROL_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
LocalRequest = Callable[[str, str, dict[str, object] | None], dict[str, object]]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("local service redirect is not admitted")


def local_service_request(base_url: str) -> LocalRequest:
    """No credentials, proxy, redirects or remote endpoints are used by this loop."""
    parts = urlsplit(base_url)
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port
            or parts.username or parts.password or parts.path not in {"", "/"}
            or parts.query or parts.fragment):
        raise ValueError("runner requires an explicit loopback HTTP service")
    opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(method: str, path: str, payload: dict[str, object] | None) -> dict[str, object]:
        if path not in {"/api/health/ready", "/api/tasks/status", "/api/market-scans"}:
            raise ValueError("unsupported local runner endpoint")
        encoded = canonical_json_bytes(payload) if payload is not None else None
        req = Request(base_url.rstrip("/") + path, data=encoded, method=method,
                      headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=10) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError("local service response is oversized")
            value = decode_json_bytes(body)
            if not isinstance(value, dict):
                raise ValueError("local service response is invalid")
            return value
    return request


def run_strategy_prospective_cycle(
    database: Path, root: Path, plan_id: str, control_root: Path, expected_plan_digest: str,
    *, request: LocalRequest,
) -> dict[str, object]:
    """Capture immediately after publication, at most one persisted POST per day.

    A crashed or ambiguous submission consumes the attempt. It can leave a gap,
    but a restart can never submit again merely because the HTTP reply was lost.
    """
    database, root, control_root = (_absolute_path(path) for path in (database, root, control_root))
    plan_id = registry_identifier(plan_id, "plan_id")
    expected_plan_digest = registry_hash(expected_plan_digest, "expected plan digest")
    if control_root != root.parent / "strategy_prospective_control":
        raise ValueError("runner requires the canonical study control directory")
    directory = control_root / plan_id
    with trial_registry_execution_lock(directory):
        binding = {"schema_version": "strategy-prospective-runner-binding-v1", "plan_id": plan_id,
                   "plan_digest": expected_plan_digest, "root": str(root), "database": str(database)}
        _publish(directory / "binding.json", binding)
        state = strategy_prospective_status(root, plan_id)
        if state["plan_digest"] != expected_plan_digest:
            raise ValueError("runner plan identity changed")
        result: dict[str, object] = {"operation": "runner", "state": state, "action": "idle",
                                    "receipts_added": [], "fault": None}
        if not state["source_code_matches"] or not state["calendar_matches_frozen"]:
            result["action"] = "blocked"
            return _observe(directory, result)
        result = _collect_and_follow(database, root, plan_id, directory, result, request)
        return _observe(directory, result)


def _collect_and_follow(database, root, plan_id, directory, result, request):
    state = result["state"]
    plan, _receipts = read_strategy_prospective_state(root, plan_id)
    now = utc_now().astimezone(ASHARE_TIMEZONE)
    day = state["next_signal_date"]
    if day is None or day > now.date().isoformat():
        return result
    opening = datetime.combine(datetime.fromisoformat(day).date(), time(15, 15), ASHARE_TIMEZONE)
    if now < opening:
        return result
    # The collector owns the exact deadline, first batch and receipt semantics.
    try:
        captured = collect_strategy_prospective_inputs(database, root, plan_id)
        result.update(state=captured["state"], receipts_added=captured["receipts_added"])
    except (ArtifactIOError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        result.update(state=strategy_prospective_status(root, plan_id), action="capture_failed", fault=type(exc).__name__)
        return result
    if captured["receipts_added"]:
        result["action"] = "captured"
        return result
    spec = cast(dict[str, Any], plan["specification"])
    cutoff = datetime.combine(now.date(), time.fromisoformat(spec["cutoff_local"]), ASHARE_TIMEZONE)
    if now > cutoff or day != now.date().isoformat():
        return result
    try:
        result.update(_follow_scan(database, day, cutoff, directory, request))
    except (ArtifactIOError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        # Provider payloads or credential-bearing errors never reach logs.
        result.update(action="service_failed", fault=type(exc).__name__)
    return result


def _follow_scan(database: Path, day: str, cutoff: datetime, directory: Path, request: LocalRequest) -> dict[str, object]:
    attempt_path = directory / f"attempt-{day}.json"
    if attempt_path.exists():
        return _attempt_state(database, day, directory)
    if _active_or_published(database, day):
        return {"action": "waiting_for_scan"}
    health = request("GET", "/api/health/ready", None)
    if health.get("status") != "ready":
        raise ValueError("local service is not ready")
    checks = health.get("checks")
    if not isinstance(checks, dict) or checks.get("database_identity") != database_identity(database):
        raise ValueError("local service database differs from the bound study database")
    tasks = request("GET", "/api/tasks/status", None)
    task_rows = tasks.get("tasks")
    if not isinstance(task_rows, list):
        raise ValueError("local scheduler response is invalid")
    if any(item.get("running") for item in task_rows if isinstance(item, dict)
           and item.get("name") == "full_market_scan"):
        return {"action": "waiting_for_scan"}
    # Recheck after HTTP reads: the user may have started a scan meanwhile.
    if _active_or_published(database, day):
        return {"action": "waiting_for_scan"}
    if utc_now().astimezone(ASHARE_TIMEZONE) >= cutoff:
        return {"action": "waiting_for_deadline"}
    return _submit_scan(day, cutoff, directory, request)


def _attempt_state(database: Path, day: str, directory: Path) -> dict[str, object]:
    attempt = _read_control(directory / f"attempt-{day}.json")
    if attempt.get("trade_date") != day:
        raise ValueError("runner attempt identity mismatch")
    response_path = directory / f"response-{day}.json"
    if not response_path.exists():
        return {"action": "submission_uncertain", "fault": "reserved_without_acknowledgement"}
    outcome = _read_control(response_path)
    fault = outcome.get("fault")
    if "run_id" in outcome and not fault:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("SELECT status FROM market_scan_run WHERE id=?", (outcome["run_id"],)).fetchone()
        if row is not None and row[0] in {"failed", "cancelled", "interrupted"}:
            fault = "scan_" + row[0]
    return {"action": "attempt_already_reserved", "fault": fault}


def _submit_scan(day: str, cutoff: datetime, directory: Path, request: LocalRequest) -> dict[str, object]:
    attempt = {"schema_version": "strategy-prospective-scan-attempt-v1", "trade_date": day,
               "recorded_at": utc_now().isoformat(), "status": "reserved_before_submission"}
    _publish(directory / f"attempt-{day}.json", attempt)
    if utc_now().astimezone(ASHARE_TIMEZONE) >= cutoff:
        _publish(directory / f"response-{day}.json", {
            "schema_version": "strategy-prospective-scan-response-v1", "trade_date": day,
            "status": "cutoff_elapsed_without_submission", "fault": "cutoff_elapsed_without_submission",
        })
        return {"action": "waiting_for_deadline", "fault": "cutoff_elapsed_without_submission"}
    try:
        response = request("POST", "/api/market-scans", {"mode": "official"})
        return _submission_response(day, directory, response)
    except (ArtifactIOError, OSError, ValueError, TypeError) as exc:
        outcome = {"schema_version": "strategy-prospective-scan-response-v1", "trade_date": day,
                   "fault": type(exc).__name__, "status": "submission_failed_or_unknown"}
        _publish(directory / f"response-{day}.json", outcome)
        return {"action": "submission_uncertain", "fault": type(exc).__name__}


def _submission_response(day: str, directory: Path, response: dict[str, object]) -> dict[str, object]:
    run = response.get("run")
    if not isinstance(run, dict) or type(run.get("id")) is not int or not (
        response.get("accepted") is True or response.get("deduplicated") is True
    ):
        raise ValueError("scan submission acknowledgement is invalid")
    correct = run.get("mode") == "official" and run.get("data_date") == day and run.get("scope") == MARKET_SCAN_FULL_MARKET_SCOPE
    outcome = {"schema_version": "strategy-prospective-scan-response-v1", "trade_date": day,
               "run_id": run["id"], "mode": run.get("mode"), "data_date": run.get("data_date"),
               "fault": None if correct else "unexpected_scan_identity",
               "deduplicated": response.get("deduplicated") is True}
    _publish(directory / f"response-{day}.json", outcome)
    if not correct:
        return {"action": "submission_conflict", "fault": "unexpected_scan_identity"}
    return {"action": "scan_submitted", "run_id": run["id"]}


def _active_or_published(database: Path, day: str) -> bool:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        return conn.execute(
            "SELECT 1 FROM market_scan_run WHERE status IN ('queued','running','cancelling') "
            "OR (data_date=? AND mode='official' AND scope=? AND status IN ('success','degraded')) LIMIT 1",
            (day, MARKET_SCAN_FULL_MARKET_SCOPE),
        ).fetchone() is not None


def _absolute_path(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute() or not path_has_only_trusted_aliases(path):
        raise ValueError("runner requires safe absolute paths")
    return path.resolve()


def _publish(path: Path, payload: Mapping[str, object]) -> None:
    value = {**payload, "digest": sha256_hex(canonical_json_bytes(payload))}
    exclusive_atomic_publish(path, canonical_json_bytes(value), max_bytes=_MAX_CONTROL_BYTES)


def _read_control(path: Path) -> dict[str, object]:
    value = decode_json_bytes(read_regular_file(path, max_bytes=_MAX_CONTROL_BYTES))
    if not isinstance(value, dict) or value.get("digest") != sha256_hex(
        canonical_json_bytes({key: item for key, item in value.items() if key != "digest"})
    ):
        raise ValueError("runner control record digest mismatch")
    return value


def _observe(directory: Path, result: dict[str, object]) -> dict[str, object]:
    state = cast(dict[str, Any], result["state"])
    # Running/pending actions and wall-clock timestamps are deliberately absent.
    # An increasing overdue count still notifies while source_drift is unchanged.
    observation = {"status": state["status"], "head_digest": state["head_digest"],
                   "overdue_count": state["overdue_count"], "receipt_counts": state["receipt_counts"],
                   "fault": result["fault"]}
    path = directory / "observation.json"
    previous = _read_control(path) if path.exists() else None
    digest = sha256_hex(canonical_json_bytes(observation))
    changed = previous is None or previous["digest"] != digest
    if changed:
        encoded = canonical_json_bytes({**observation, "digest": digest})
        descriptor, temporary = tempfile.mkstemp(prefix=".observation-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {**result, "meaningful_change": changed}
