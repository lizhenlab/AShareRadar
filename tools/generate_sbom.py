#!/usr/bin/env python3
"""Generate byte-reproducible CycloneDX SBOMs from locked dependencies."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
PYTHON_LOCK = ROOT / "requirements-lock.txt"
NPM_LOCK = ROOT / "package-lock.json"


class SbomGenerationError(RuntimeError):
    """Raised when an external SBOM generator cannot produce valid output."""


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stable_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        normalized = [_stable_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return value


def normalize_bom(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove volatile CycloneDX metadata and impose deterministic ordering."""

    normalized = deepcopy(payload)
    normalized.pop("serialNumber", None)
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("timestamp", None)
    return _stable_value(normalized)


def canonical_bom_bytes(payload: dict[str, Any]) -> bytes:
    normalized = normalize_bom(payload)
    return (
        json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _load_bom(raw: str, *, source: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SbomGenerationError(f"{source} returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("bomFormat") != "CycloneDX":
        raise SbomGenerationError(f"{source} did not return a CycloneDX document")
    return payload


def _cyclonedx_executable() -> str:
    alongside_python = Path(sys.executable).with_name("cyclonedx-py")
    if alongside_python.is_file():
        return str(alongside_python)
    executable = shutil.which("cyclonedx-py")
    if executable:
        return executable
    raise SbomGenerationError(
        "cyclonedx-py is unavailable; install requirements-security-lock.txt "
        "or requirements-dev-lock.txt first"
    )


def _redact_error(detail: str) -> str:
    sanitized = detail.replace(str(ROOT), ".")
    sanitized = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", sanitized)
    for name, value in os.environ.items():
        if value and any(token in name.upper() for token in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
            sanitized = sanitized.replace(value, "[REDACTED]")
    return sanitized[-1000:]


def _run(command: Sequence[str], *, stdout: bool = False) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise SbomGenerationError(_redact_error(detail)) from exc
    return completed.stdout if stdout else ""


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    os.replace(temporary, path)


def generate_sboms(output_dir: Path) -> tuple[Path, Path]:
    """Generate normalized Python and npm SBOM files in ``output_dir``."""

    if not PYTHON_LOCK.is_file() or not NPM_LOCK.is_file():
        raise SbomGenerationError("locked dependency manifests are missing")

    with tempfile.TemporaryDirectory(prefix="ashare-radar-sbom-") as temporary:
        python_raw = Path(temporary) / "python.raw.json"
        _run(
            [
                _cyclonedx_executable(),
                "requirements",
                str(PYTHON_LOCK),
                "--output-reproducible",
                "--validate",
                "--spec-version",
                "1.6",
                "--output-format",
                "JSON",
                "--output-file",
                str(python_raw),
            ]
        )
        npm_raw = _run(
            [
                "npm",
                "sbom",
                "--package-lock-only",
                "--sbom-format",
                "cyclonedx",
            ],
            stdout=True,
        )

        python_payload = _load_bom(
            python_raw.read_text(encoding="utf-8"), source="cyclonedx-py"
        )
        npm_payload = _load_bom(npm_raw, source="npm sbom")

    python_output = output_dir / "python.cdx.json"
    npm_output = output_dir / "npm.cdx.json"
    _write_atomic(python_output, canonical_bom_bytes(python_payload))
    _write_atomic(npm_output, canonical_bom_bytes(npm_payload))
    return python_output, npm_output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/sbom"),
        help="directory for python.cdx.json and npm.cdx.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    try:
        outputs = generate_sboms(output_dir)
    except SbomGenerationError as exc:
        print(f"SBOM generation failed: {exc}", file=sys.stderr)
        return 1
    print("Generated " + ", ".join(path.name for path in outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
