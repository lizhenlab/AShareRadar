"""Read-only CLI entrypoint: reuse full artifact validators, return summaries."""

from __future__ import annotations

import hashlib
import os
import sys
from typing import Any

from app.artifacts.io import canonical_json_bytes
from app.services.market_scan_probability_preload_process import (
    PROBABILITY_PRELOAD_SCHEMA,
    WORKER_MAX_INPUT_BYTES,
    WORKER_MAX_OUTPUT_BYTES,
    encode_probability_preload_file,
    read_probability_preload_request,
)
from app.services.market_scan_probability_source import ProbabilitySourceError
from app.services.market_scan_probability_source_research import (
    ProbabilityArchiveFingerprint,
    load_verified_probability_archive_summary,
)


def _verified_summary(kind: str, fingerprint: ProbabilityArchiveFingerprint) -> dict[str, object] | None:
    return load_verified_probability_archive_summary(kind, fingerprint)


def _worker_response(raw: bytes) -> bytes:
    files = read_probability_preload_request(raw)
    results: list[dict[str, object]] = []
    response = {"schema_version": PROBABILITY_PRELOAD_SCHEMA, "request_digest": hashlib.sha256(raw).hexdigest(), "results": results}
    size = len(canonical_json_bytes(response))
    for kind, fingerprint in files:
        result = {**encode_probability_preload_file(kind, fingerprint), "summary": _verified_summary(kind, fingerprint)}
        size += len(canonical_json_bytes(result)) + bool(results)
        if size > WORKER_MAX_OUTPUT_BYTES:
            raise ProbabilitySourceError("上涨概率只读校验进程响应过大")
        results.append(result)
    return canonical_json_bytes(response)


def _read_only_audit(event: str, args: tuple[Any, ...]) -> None:
    if event == "open":
        _path, _mode, flags = args
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise PermissionError("probability preload worker is read-only")
    if event.startswith(("sqlite3.connect", "socket.", "subprocess.")) or event in {
        "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.link", "os.symlink", "os.truncate", "os.chmod", "os.chown", "os.utime", "os.system", "os.posix_spawn",
    }:
        raise PermissionError("probability preload worker is read-only")


def main() -> int:
    sys.addaudithook(_read_only_audit)
    try:
        response = _worker_response(sys.stdin.buffer.read(WORKER_MAX_INPUT_BYTES + 1))
    except Exception:
        # Never return raw exception text, provider configuration, paths or stderr.
        response = canonical_json_bytes({"error": "verification_failed"})
    sys.stdout.buffer.write(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
