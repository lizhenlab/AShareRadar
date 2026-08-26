"""Bounded, cancellable, read-only artifact verification outside the server GIL."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
from threading import Event
import time
from typing import Any, BinaryIO, cast

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, decode_json_bytes
from app.config import PROJECT_ROOT
from app.services.market_scan_probability_source import ProbabilitySourceError

WORKER_TIMEOUT_SECONDS = 300.0
WORKER_POLL_SECONDS = 0.1
WORKER_MAX_FILES = 2048
WORKER_MAX_INPUT_BYTES = 2 * 1024 * 1024
WORKER_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
PROBABILITY_PRELOAD_SCHEMA = "market-scan-probability-preload-v1"
_SCHEMA = PROBABILITY_PRELOAD_SCHEMA
_FileFingerprint = tuple[Path, int, int, int, int, int, int]
_KINDS = frozenset({"source", "outcome", "fit"})
_SUMMARY_FIELDS = {
    "source": frozenset({
        "artifact_schema_version", "payload_contract_version", "captured_at", "run_id", "quote_date", "as_of", "cohort",
        "score_contract", "record_count", "integrity_digest",
    }),
    "outcome": frozenset({
        "run_id", "generated_at", "as_of_date", "source_integrity_digest", "integrity_digest", "cohort", "horizons",
    }),
    "fit": frozenset({
        "through_run_id", "generated_at", "cohort", "horizons", "fit_status", "fit_replay_verified", "fit_selection_qualification",
        "training_cutoff", "through_source_digest", "through_outcome_digest", "input_pair_digest", "integrity_digest",
    }),
}
_RequestFile = tuple[str, "_FileFingerprint"]


class ProbabilitySourcePreloadCancelled(ProbabilitySourceError):
    """The caller cancelled verification; no new index may be published."""


def check_preload_active(cancel_event: Event | None, deadline: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ProbabilitySourcePreloadCancelled("上涨概率只读预热已取消")
    if time.monotonic() >= deadline:
        raise ProbabilitySourceError("上涨概率只读预热超时，请稍后重试")


def isolated_probability_summaries(
    files: Sequence[_RequestFile],
    *,
    cancel_event: Event | None = None,
    deadline: float | None = None,
) -> dict[_RequestFile, dict[str, object] | None]:
    """Only compact summaries cross the boundary; authority stays in the parent."""
    deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + WORKER_TIMEOUT_SECONDS)
    check_preload_active(cancel_event, deadline)
    if len(files) > WORKER_MAX_FILES:
        raise ProbabilitySourceError("上涨概率只读校验文件数量无效")
    encoded_files = [_encode_file(kind, fingerprint) for kind, fingerprint in files]
    raw = canonical_json_bytes({"schema_version": _SCHEMA, "files": encoded_files})
    _read_worker_request(raw)
    if not files:
        return {}
    response = _run_worker(raw, cancel_event=cancel_event, deadline=deadline)
    check_preload_active(cancel_event, deadline)
    summaries = _decode_worker_response(response, raw, files)
    check_preload_active(cancel_event, deadline)
    return summaries


def _encode_file(kind: str, fingerprint: _FileFingerprint) -> dict[str, object]:
    return {"kind": kind, "fingerprint": [str(fingerprint[0]), *fingerprint[1:]]}


def _worker_command() -> list[str]:
    return [sys.executable, "-m", "app.services.market_scan_probability_preload_worker"]


def _run_worker(raw: bytes, *, cancel_event: Event | None, deadline: float) -> bytes:
    process: subprocess.Popen[bytes] | None = None
    try:
        check_preload_active(cancel_event, deadline)
        process = subprocess.Popen(
            _worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=PROJECT_ROOT, env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        response = _exchange(process, raw, cancel_event=cancel_event, deadline=deadline)
        if process.returncode != 0:
            raise ProbabilitySourceError("上涨概率只读校验进程未返回有效结果")
        return response
    except OSError:
        raise ProbabilitySourceError("上涨概率只读校验进程不可用") from None
    finally:
        if process is not None:
            _stop_and_reap(process)


def _exchange(process: subprocess.Popen[bytes], raw: bytes, *, cancel_event: Event | None, deadline: float) -> bytes:
    output = bytearray()
    offset = 0
    with selectors.DefaultSelector() as selector:
        for stream, events, name in ((process.stdin, selectors.EVENT_WRITE, "input"), (process.stdout, selectors.EVENT_READ, "output")):
            if stream is None:
                raise ProbabilitySourceError("上涨概率只读校验进程管道不可用")
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events, name)
        while selector.get_map() or process.poll() is None:
            check_preload_active(cancel_event, deadline)
            for key, _events in selector.select(min(WORKER_POLL_SECONDS, max(0.0, deadline - time.monotonic()))):
                stream = cast(BinaryIO, key.fileobj)
                if key.data == "input":
                    offset += os.write(key.fd, raw[offset:offset + 65536])
                    if offset == len(raw):
                        selector.unregister(stream)
                        stream.close()
                else:
                    chunk = os.read(key.fd, min(65536, WORKER_MAX_OUTPUT_BYTES - len(output) + 1))
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                    output.extend(chunk)
                    if len(output) > WORKER_MAX_OUTPUT_BYTES:
                        raise ProbabilitySourceError("上涨概率只读校验进程响应过大")
    check_preload_active(cancel_event, deadline)
    return bytes(output)


def _stop_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=2.0)
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _read_worker_request(raw: bytes) -> tuple[_RequestFile, ...]:
    if len(raw) > WORKER_MAX_INPUT_BYTES:
        raise ProbabilitySourceError("上涨概率只读校验请求过大")
    value = _json_object(raw)
    if set(value) != {"schema_version", "files"} or value["schema_version"] != _SCHEMA:
        raise ProbabilitySourceError("上涨概率只读校验请求格式无效")
    files = value["files"]
    if not isinstance(files, list) or len(files) > WORKER_MAX_FILES:
        raise ProbabilitySourceError("上涨概率只读校验文件数量无效")
    parsed = tuple(_decode_file(item) for item in files)
    if len(set(parsed)) != len(parsed):
        raise ProbabilitySourceError("上涨概率只读校验请求含重复文件")
    return parsed


def read_probability_preload_request(raw: bytes) -> tuple[_RequestFile, ...]:
    """The child accepts only this bounded, explicit file-fingerprint protocol."""
    return _read_worker_request(raw)


def encode_probability_preload_file(kind: str, fingerprint: _FileFingerprint) -> dict[str, object]:
    """Encode the same path/stat identity in requests and responses."""
    return _encode_file(kind, fingerprint)


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = decode_json_bytes(raw)
        # Bounded protocol only: also reject exponent overflow and lone UTF-16
        # surrogates, neither of which JSON's parse_constant callback rejects.
        canonical_json_bytes(value)
    except (ArtifactIOError, ValueError, TypeError, RecursionError):
        raise ProbabilitySourceError("上涨概率只读校验 JSON 无效") from None
    if not isinstance(value, dict):
        raise ProbabilitySourceError("上涨概率只读校验 JSON 必须是对象")
    return value


def _decode_file(value: object) -> _RequestFile:
    if not isinstance(value, dict) or set(value) != {"kind", "fingerprint"}:
        raise ProbabilitySourceError("上涨概率只读校验文件身份无效")
    kind, fingerprint = value["kind"], value["fingerprint"]
    if not isinstance(kind, str) or kind not in _KINDS or not isinstance(fingerprint, list) or len(fingerprint) != 7:
        raise ProbabilitySourceError("上涨概率只读校验文件指纹无效")
    path, *facts = fingerprint
    if not isinstance(path, str) or len(path) > 4096 or "\x00" in path or not Path(path).is_absolute() or ".." in Path(path).parts:
        raise ProbabilitySourceError("上涨概率只读校验文件路径无效")
    if any(type(value) is not int or value < 0 for value in facts) or not stat.S_ISREG(facts[2]):
        raise ProbabilitySourceError("上涨概率只读校验文件属性无效")
    result = cast("_FileFingerprint", (Path(path), *facts))
    _file_identity(kind, result)
    return kind, result


def _file_identity(kind: str, fingerprint: _FileFingerprint) -> tuple[int, str, str | None]:
    # This is transport identity validation, not an artifact validator. The
    # isolated loader still verifies its own schema, filename, bytes and replay.
    patterns = {
        "source": r"market-scan-probability-source-run-(\d+)-([0-9a-f]{64})\.json\.gz",
        "outcome": r"market-scan-probability-outcomes-run-(\d+)-through-(\d{4}-\d{2}-\d{2})-([0-9a-f]{64})\.json\.gz",
        "fit": r"market-scan-probability-fit-through-run-(\d+)-([0-9a-f]{64})\.json\.gz",
    }
    match = re.fullmatch(patterns[kind], fingerprint[0].name)
    if match is None or int(match.group(1)) < 1:
        raise ProbabilitySourceError("上涨概率只读校验文件名无效")
    return int(match.group(1)), match.group(3 if kind == "outcome" else 2), match.group(2) if kind == "outcome" else None


def _validate_summary(kind: str, fingerprint: _FileFingerprint, summary: object) -> None:
    if summary is None and kind == "outcome":
        return
    if not isinstance(summary, dict) or set(summary) != _SUMMARY_FIELDS[kind]:
        raise ProbabilitySourceError("上涨概率只读校验摘要格式无效")
    run_id, digest, as_of = _file_identity(kind, fingerprint)
    bound_id = summary.get("through_run_id" if kind == "fit" else "run_id")
    if type(bound_id) is not int or bound_id != run_id or summary["integrity_digest"] != digest:
        raise ProbabilitySourceError("上涨概率只读校验摘要内容绑定无效")
    if kind == "outcome" and summary["as_of_date"] != as_of:
        raise ProbabilitySourceError("上涨概率只读校验摘要日期绑定无效")
    if not isinstance(summary["cohort"], dict) or not {"mode", "scope", "rule_version"} <= summary["cohort"].keys():
        raise ProbabilitySourceError("上涨概率只读校验摘要 cohort 无效")


def _decode_worker_response(raw: bytes, request: bytes, files: Sequence[_RequestFile]) -> dict[_RequestFile, dict[str, object] | None]:
    if len(raw) > WORKER_MAX_OUTPUT_BYTES:
        raise ProbabilitySourceError("上涨概率只读校验进程响应过大")
    value = _json_object(raw)
    if value == {"error": "verification_failed"}:
        raise ProbabilitySourceError("上涨概率只读校验失败，请检查 source/outcome/fit 归档完整性")
    if set(value) != {"schema_version", "request_digest", "results"} or value["schema_version"] != _SCHEMA:
        raise ProbabilitySourceError("上涨概率只读校验进程响应格式无效")
    if value["request_digest"] != hashlib.sha256(request).hexdigest() or not isinstance(value["results"], list) or len(value["results"]) != len(files):
        raise ProbabilitySourceError("上涨概率只读校验进程响应请求绑定无效")
    summaries: dict[_RequestFile, dict[str, object] | None] = {}
    for entry, (kind, fingerprint) in zip(value["results"], files, strict=True):
        if not isinstance(entry, dict) or set(entry) != {"kind", "fingerprint", "summary"}:
            raise ProbabilitySourceError("上涨概率只读校验进程摘要格式无效")
        if {key: entry[key] for key in ("kind", "fingerprint")} != _encode_file(kind, fingerprint):
            raise ProbabilitySourceError("上涨概率只读校验进程摘要文件绑定无效")
        _validate_summary(kind, fingerprint, entry["summary"])
        summaries[(kind, fingerprint)] = entry["summary"]
    return summaries
