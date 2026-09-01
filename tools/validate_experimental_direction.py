"""Validate rebuilt Choice D+1/D+2/D+5 history without publishing online models."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, exclusive_atomic_publish, path_has_only_trusted_aliases, sha256_hex  # noqa: E402
from app.services.choice_sdk import ChoiceError  # noqa: E402
from app.services.experimental_direction_validation import (  # noqa: E402
    DIRECTION_VALIDATION_MAX_BYTES, validate_experimental_direction_history,
)
from app.utils.clock import market_now  # noqa: E402


def _load_history(manifest: Path, database: Path) -> Any:
    from app.services.choice_experimental_history import load_choice_experimental_history

    return load_choice_experimental_history(manifest, database)


def _output_directory(directory: Path) -> Path:
    output = directory.expanduser().absolute()
    protected = {".workbuddy-ai", ".git", ".codex", ".agents", ".venv"}
    if protected.intersection(output.parts) or not path_has_only_trusted_aliases(output):
        raise ValueError("validation output directory is unsafe")
    return output


def _require_separate_source(output: Path, history: Any, manifest: Path, database: Path) -> None:
    original = history.provenance.get("source", {}).get("directory")
    sources = [manifest.parent, database.parent]
    if original is not None:
        sources.append(Path(original))
    if any(output.resolve().is_relative_to(source.expanduser().resolve()) for source in sources):
        raise ValueError("validation report must be outside the original and derived Choice history archives")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--choice-history-manifest", type=Path, required=True)
    parser.add_argument("--choice-history-database", "--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        output = _output_directory(args.output_dir)
        history = _load_history(args.choice_history_manifest, args.choice_history_database)
        _require_separate_source(output, history, args.choice_history_manifest, args.choice_history_database)
        report = validate_experimental_direction_history(
            history.series, history.sessions, history.provenance, generated_at=market_now().isoformat(),
        )
        digest = sha256_hex(canonical_json_bytes(report))
        target = output / f"experimental-direction-validation-{digest}.json"
        exclusive_atomic_publish(target, canonical_json_bytes({"payload": report, "sha256": digest}), max_bytes=DIRECTION_VALIDATION_MAX_BYTES)
    except (ArtifactIOError, ChoiceError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": report["status"], "path": str(target), "sha256": digest,
        "evidence_kind": report["evidence_kind"], "formal_filter_qualified": False,
        "production_ranking_effect": "none", "online_models_modified": False,
        "horizons": {key: value["status"] for key, value in report["horizons"].items()},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
