"""Export an immutable none-price audit package without granting PIT admission."""

from __future__ import annotations

import csv
import os
from pathlib import Path
import tempfile

from app.artifacts.io import canonical_json_bytes, path_has_only_trusted_aliases
from app.services.fuyao_dumps_storage import file_digest, read_dump_status, safe_root
from app.services.fuyao_dumps_validation import BATCH_SIZE, DumpKind, FuyaoDumpError, dump_date, parquet_modules


def export_dump_research(root: Path, output: Path) -> Path:
    """Export raw daily/events CSV plus source hashes for independent research.

    These are source inputs, not a substitute for the existing official execution
    intake's source registry, market-state evidence, or frozen PIT score inputs.
    """
    parquet_modules()
    manifest = read_dump_status(root, verify_files=True)
    if manifest is None:
        raise FuyaoDumpError("尚未发布可导出的历史数据版本")
    if not path_has_only_trusted_aliases(output) or output.exists():
        raise FuyaoDumpError("导出目标必须为不存在的独立目录")
    parent = safe_root(output.parent)
    with tempfile.TemporaryDirectory(prefix=".fuyao-export-", dir=parent) as temporary:
        stage = Path(temporary) / "dataset"
        stage.mkdir(mode=0o700)
        source = root / "versions" / manifest.version
        paths = [_export_csv(source / "daily.parquet", stage / "daily-none.csv", "daily"),
                 _export_csv(source / "actions.parquet", stage / "corporate-actions.csv", "actions")]
        payload = {"schema_version": "fuyao-raw-research-export-v1", "source_version": manifest.version,
                   "adjusted": "none", "point_in_time_verified": False, "official_execution_admitted": False,
                   "files": {path.name: file_digest(path) for path in paths}, "source_manifest": manifest.model_dump(mode="json"),
                   "missing_for_official_research": ["historical_universe", "point_in_time_disclosure_versions", "official_daily_execution_state",
                                                      "independently_verified_source_registry", "frozen_score_inputs"]}
        (stage / "manifest.json").write_bytes(canonical_json_bytes(payload))
        for path in stage.iterdir():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        stage.rename(output)
    return output / "manifest.json"


def _export_csv(source: Path, target: Path, kind: DumpKind) -> Path:
    _, parquet = parquet_modules()
    dataset = parquet.ParquetFile(source)
    date_field = "date_ms" if kind == "daily" else "ex_date_ms"
    fields = ["symbol", "date", *dataset.schema_arrow.names, "source", "price_basis"]
    with target.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for batch in dataset.iter_batches(batch_size=BATCH_SIZE):
            for row in batch.to_pylist():
                writer.writerow({**row, "symbol": row["thscode"][:6], "date": dump_date(row[date_field]).isoformat(),
                                 "source": "fuyao", "price_basis": "none"})
    return target
