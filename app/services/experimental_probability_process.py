"""Bounded, read-only experiment worker isolated from CPU-heavy warmup threads."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

from pydantic import ValidationError

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, decode_json_bytes
from app.config import PROJECT_ROOT
from app.db.market_scan_integrity import MarketScanSnapshotSealError
from app.repositories.market_scan_experimental import read_experimental_candidates
from app.services.experimental_probability_model import ExperimentalProbabilityUnavailable
from app.services.market_scan_experimental_probability import experimental_results
from app.utils.errors import NotFoundError


WORKER_TIMEOUT_SECONDS = 60
WORKER_MAX_INPUT_BYTES = 4096
WORKER_MAX_OUTPUT_BYTES = 1024 * 1024
_ERROR_TYPES = {
    "snapshot_integrity": MarketScanSnapshotSealError,
    "not_found": NotFoundError,
    "unavailable": ExperimentalProbabilityUnavailable,
    "invalid_input": ValueError,
    "database_unavailable": sqlite3.DatabaseError,
    "internal_validation": RuntimeError,
}


def isolated_experimental_results(database: Path, run_id: int, **filters: Any) -> dict[str, Any]:
    """No model training or provider access; admission remains with the caller."""
    request = canonical_json_bytes({"database": str(database.absolute()), "run_id": run_id, "filters": filters})
    if len(request) > WORKER_MAX_INPUT_BYTES:
        raise ValueError("实验请求过大")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.services.experimental_probability_process"],
            input=request, capture_output=True, check=False, timeout=WORKER_TIMEOUT_SECONDS, cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError("独立实验读取超时或进程不可用，请稍后重试") from exc
    if result.returncode != 0 or len(result.stdout) > WORKER_MAX_OUTPUT_BYTES:
        raise RuntimeError("独立实验进程未返回有效结果")
    return _decode_worker_result(result.stdout)


def _decode_worker_result(raw: bytes) -> dict[str, Any]:
    try:
        result = decode_json_bytes(raw)
    except (ArtifactIOError, ValueError) as exc:
        raise RuntimeError("独立实验进程响应无效") from exc
    if not isinstance(result, dict):
        raise RuntimeError("独立实验进程响应无效")
    if set(result) == {"result"} and isinstance(result["result"], dict):
        return result["result"]
    if set(result) == {"error", "message"} and isinstance(result["message"], str):
        error_type = _ERROR_TYPES.get(result["error"]) if isinstance(result["error"], str) else None
        if error_type is not None:
            raise error_type(result["message"])
    raise RuntimeError("独立实验进程响应无效")


def _read_worker_request(raw: bytes) -> dict[str, Any]:
    value = decode_json_bytes(raw)
    if not isinstance(value, dict) or set(value) != {"database", "run_id", "filters"}:
        raise ValueError("实验进程请求格式无效")
    if not isinstance(value["database"], str) or type(value["run_id"]) is not int or value["run_id"] < 1:
        raise ValueError("实验进程请求身份无效")
    if not isinstance(value["filters"], dict) or not set(value["filters"]) <= {
        "prediction_kind", "minimum", "market", "keyword", "sort", "page", "page_size",
    }:
        raise ValueError("实验进程筛选参数无效")
    database = Path(value["database"])
    run, items = read_experimental_candidates(database, value["run_id"])
    return experimental_results(database, run, items, **value["filters"])


def main() -> int:
    raw = sys.stdin.buffer.read(WORKER_MAX_INPUT_BYTES + 1)
    response: dict[str, Any]
    try:
        if len(raw) > WORKER_MAX_INPUT_BYTES:
            raise ValueError("实验进程请求过大")
        response = {"result": _read_worker_request(raw)}
    except ValidationError:
        response = {"error": "internal_validation", "message": "内部数据格式异常，当前数据暂不可用"}
    except tuple(_ERROR_TYPES.values()) as exc:
        kind = next(key for key, error_type in _ERROR_TYPES.items() if isinstance(exc, error_type))
        response = {"error": kind, "message": str(exc)}
    encoded = canonical_json_bytes(response)
    if len(encoded) > WORKER_MAX_OUTPUT_BYTES:
        return 1
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
