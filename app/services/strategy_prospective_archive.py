"""Bounded, read-only extraction from a verified public-vendor history archive."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
import importlib
from pathlib import Path
import stat
from typing import Any

from app.artifacts.io import canonical_json_bytes, path_has_only_trusted_aliases, sha256_hex
from app.models.fuyao_dumps import FuyaoDumpManifest
from app.services.fuyao_dumps_storage import read_dump_status
from app.services.fuyao_dumps_validation import DumpKind, FuyaoDumpError, dump_date, dump_fields, normalize_row, parquet_modules, require_schema
from app.services.trading_calendar import ASHARE_TIMEZONE


ArchiveFingerprint = tuple[tuple[str, int, int, int, int, int], ...]


@dataclass(frozen=True)
class VerifiedResearchArchive:
    root: Path
    manifest: FuyaoDumpManifest
    fingerprint: ArchiveFingerprint


def read_verified_research_archive(root: Path) -> VerifiedResearchArchive | None:
    """Memoize hash work only while all source identities and timestamps agree."""
    root = root.absolute()
    manifest = read_dump_status(root)
    if manifest is None:
        return None
    fingerprint = research_archive_fingerprint(root, manifest)
    _verify_archive_version(root, manifest.version, fingerprint)
    return VerifiedResearchArchive(root, manifest, fingerprint)


def research_archive_fingerprint(root: Path, manifest: FuyaoDumpManifest) -> ArchiveFingerprint:
    directory = root / "versions" / manifest.version
    paths = [root / "current.json", directory / "manifest.json"]
    paths.extend(directory / name for name in ("daily.parquet", "actions.parquet", "source-daily.parquet", "source-actions.parquet"))
    return tuple(_file_fingerprint(path) for path in paths)


def _file_fingerprint(path: Path) -> tuple[str, int, int, int, int, int]:
    if not path_has_only_trusted_aliases(path):
        raise FuyaoDumpError("研究源文件不接受符号链接")
    value = path.stat()
    if not stat.S_ISREG(value.st_mode):
        raise FuyaoDumpError("研究源文件必须为普通文件")
    return str(path), value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@lru_cache(maxsize=8)
def _verify_archive_version(root: Path, version: str, fingerprint: ArchiveFingerprint) -> None:
    manifest = read_dump_status(root, verify_files=True)
    if manifest is None or manifest.version != version or research_archive_fingerprint(root, manifest) != fingerprint:
        raise FuyaoDumpError("研究源版本在完整校验期间发生变化")


def extract_research_archive(
    archive: VerifiedResearchArchive, pairs: Sequence[tuple[str, str]], *, completed: date,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Predicate-filter normalized row groups without materializing full history."""
    if research_archive_fingerprint(archive.root, archive.manifest) != archive.fingerprint:
        raise FuyaoDumpError("研究源文件在提取前发生变化")
    wanted = set(pairs)
    directory = archive.root / "versions" / archive.manifest.version
    daily = _extract_rows(directory / "daily.parquet", "daily", wanted, completed)
    actions = _extract_rows(directory / "actions.parquet", "actions", wanted, completed)
    if research_archive_fingerprint(archive.root, archive.manifest) != archive.fingerprint:
        raise FuyaoDumpError("研究源文件在提取期间发生变化")
    if len({(row["symbol"], row["session_date"]) for row in daily}) != len(daily):
        raise FuyaoDumpError("研究日线包含重复股票日期")
    return daily, actions


def _extract_rows(path: Path, kind: DumpKind, wanted: set[tuple[str, str]], completed: date) -> list[dict[str, object]]:
    if not wanted:
        return []
    parquet_modules()
    dataset_module = importlib.import_module("pyarrow.dataset")
    dataset = dataset_module.dataset(path, format="parquet")
    require_schema(dataset.schema.names, kind)
    date_field = "date_ms" if kind == "daily" else "ex_date_ms"
    symbols = sorted({symbol for symbol, _day in wanted})
    timestamps = sorted({_date_milliseconds(day) for _symbol, day in wanted})
    predicate = dataset_module.field("thscode").isin(symbols) & dataset_module.field(date_field).isin(timestamps)
    output = []
    for batch in dataset.to_batches(columns=list(dump_fields(kind)), filter=predicate, batch_size=8192):
        output.extend(_matching_rows(batch.to_pylist(), kind, wanted, completed))
        if len(output) > (len(wanted) if kind == "daily" else min(max(len(wanted) * 20, 1000), 200_000)):
            raise FuyaoDumpError("研究提取超过每股票日期有界记录数")
    return sorted(output, key=lambda row: (str(row["symbol"]), str(row["session_date"]), str(row["source_record_digest"])))


def _matching_rows(raw: list[dict[str, Any]], kind: DumpKind, wanted: set[tuple[str, str]], completed: date) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    date_field = "date_ms" if kind == "daily" else "ex_date_ms"
    for row in raw:
        symbol, day = str(row["thscode"]), dump_date(row[date_field]).isoformat()
        if (symbol, day) not in wanted:
            continue
        values = normalize_row(row, kind, today=completed)
        output.append({"symbol": symbol, "session_date": day, "values": values,
                       "source_record_digest": sha256_hex(canonical_json_bytes(values)),
                       "source_file": "daily.parquet" if kind == "daily" else "actions.parquet"})
    return output


def _date_milliseconds(day: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), time.min, ASHARE_TIMEZONE).timestamp() * 1000)
