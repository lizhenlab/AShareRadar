"""Disk-backed normalization and atomic immutable versions for Fuyao dumps."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, path_has_only_trusted_aliases, read_regular_file
from app.models.fuyao_dumps import FuyaoDumpFile, FuyaoDumpManifest
from app.services.fuyao_dumps_validation import BATCH_SIZE, DumpKind, FuyaoDumpError, dump_fields, normalize_row, parquet_modules, require_schema
from app.services.fuyao_sync_control import FuyaoSyncCancelled, FuyaoSyncControl
from app.utils.clock import market_now


def safe_root(path: Path) -> Path:
    if not path_has_only_trusted_aliases(path):
        raise FuyaoDumpError("研究数据目录不接受符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path.resolve()


@contextmanager
def dump_lease(root: Path) -> Iterator[None]:
    descriptor = os.open(root / "sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FuyaoDumpError("已有全市场历史数据同步正在运行") from None
        yield
    finally:
        os.close(descriptor)


def file_digest(path: Path, control: FuyaoSyncControl | None = None, *, progress_offset: int = 0) -> str:
    control = control or FuyaoSyncControl()
    control.checkpoint()
    if not path_has_only_trusted_aliases(path) or not path.is_file():
        raise FuyaoDumpError("研究数据文件不可用或为符号链接")
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            control.checkpoint()
            digest.update(chunk)
            size += len(chunk)
            control.checkpoint(current=progress_offset + size)
    return digest.hexdigest()


def manifest_version(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes({key: value for key, value in payload.items() if key != "version"})).hexdigest()


def read_dump_status(root: Path, *, verify_files: bool = False, control: FuyaoSyncControl | None = None) -> FuyaoDumpManifest | None:
    control = control or FuyaoSyncControl()
    control.checkpoint()
    if not path_has_only_trusted_aliases(root):
        raise FuyaoDumpError("研究数据目录不接受符号链接")
    if not (root / "current.json").exists():
        return None
    try:
        version = _read_current_version(root)
        directory = root / "versions" / version
        manifest = _read_verified_manifest(directory, version)
        if verify_files:
            verify_dump_files(directory, manifest, control)
        return manifest
    except FuyaoSyncCancelled:
        raise
    except Exception:
        raise FuyaoDumpError("研究数据版本校验失败，请检查本地文件") from None


def _read_current_version(root: Path) -> str:
    """Resolve only an exact pointer with a lowercase SHA-256 version name."""
    pointer = decode_json_bytes(read_regular_file(root / "current.json", max_bytes=1024))
    if not isinstance(pointer, dict) or set(pointer) != {"version"}:
        raise ValueError("pointer")
    version = pointer["version"]
    if not isinstance(version, str) or len(version) != 64 or any(char not in "0123456789abcdef" for char in version):
        raise ValueError("version")
    return version


def _read_verified_manifest(directory: Path, version: str) -> FuyaoDumpManifest:
    """Bind the manifest identity, content digest, and canonical file sequence."""
    manifest = FuyaoDumpManifest.model_validate(decode_json_bytes(read_regular_file(directory / "manifest.json", max_bytes=1024 * 1024)))
    if manifest.version != version or manifest_version(manifest.model_dump(mode="json")) != version:
        raise ValueError("digest")
    if [item.name for item in manifest.files] != ["daily.parquet", "actions.parquet"]:
        raise ValueError("files")
    return manifest


def verify_dump_files(directory: Path, manifest: FuyaoDumpManifest, control: FuyaoSyncControl | None = None) -> None:
    control = control or FuyaoSyncControl()
    control.checkpoint(current=0, unit="bytes")
    verified_bytes = 0
    for item in manifest.files:
        path = directory / item.name
        if path.stat().st_size != item.size_bytes or file_digest(path, control, progress_offset=verified_bytes) != item.sha256:
            raise FuyaoDumpError("研究数据文件摘要或长度不匹配")
        verified_bytes += item.size_bytes
    if set(manifest.source_sha256) != {"source-daily.parquet", "source-actions.parquet"}:
        raise FuyaoDumpError("研究数据缺少原始文件摘要")
    for name, expected in manifest.source_sha256.items():
        if file_digest(directory / name, control, progress_offset=verified_bytes) != expected:
            raise FuyaoDumpError("原始下载文件摘要不匹配")
        verified_bytes += (directory / name).stat().st_size


def create_staging_database(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA temp_store=FILE")
    for name in ("daily", "incoming", "actions", "incoming_actions"):
        key = "symbol, day, payload" if "actions" in name else "symbol, day"
        db.execute(f"CREATE TABLE {name}(symbol TEXT, day INTEGER, payload TEXT, conflict INTEGER DEFAULT 0, PRIMARY KEY({key})) WITHOUT ROWID")
    return db


def ingest_parquet(db: sqlite3.Connection, path: Path, kind: DumpKind, control: FuyaoSyncControl | None = None) -> tuple[int, int]:
    control = control or FuyaoSyncControl()
    control.checkpoint(f"validating_{kind}", current=0, unit="rows")
    _, parquet = parquet_modules()
    try:
        source = parquet.ParquetFile(path)
        require_schema(source.schema_arrow.names, kind)
        if source.metadata.num_rows > 30_000_000:
            raise FuyaoDumpError("Parquet 行数超过同步上限")
        incoming = "incoming" if kind == "daily" else "incoming_actions"
        db.execute(f"DELETE FROM {incoming}")
        count = _ingest_batches(db, source, kind, control)
        if db.execute(f"SELECT 1 FROM {incoming} WHERE conflict = 1 LIMIT 1").fetchone():
            raise FuyaoDumpError("同一个源文件存在数值冲突的重复行")
        revisions = db.execute("SELECT COUNT(*) FROM daily old JOIN incoming new USING(symbol, day) WHERE old.payload != new.payload").fetchone()[0] if kind == "daily" else 0
        unique = db.execute(f"SELECT COUNT(*) FROM {incoming}").fetchone()[0]
        db.execute(f"INSERT OR REPLACE INTO {kind} SELECT * FROM {incoming}")
        db.commit()
        control.checkpoint()
        return revisions, count - unique
    except (FuyaoDumpError, FuyaoSyncCancelled):
        raise
    except Exception:
        control.checkpoint()
        raise FuyaoDumpError("Parquet 文件无法解析或写入校验暂存区") from None


def _ingest_batches(db: sqlite3.Connection, source: Any, kind: DumpKind, control: FuyaoSyncControl) -> int:
    date_field = "date_ms" if kind == "daily" else "ex_date_ms"
    count = 0
    today = market_now().date()
    statement = "INSERT INTO incoming(symbol, day, payload) VALUES (?, ?, ?) ON CONFLICT(symbol, day) DO UPDATE SET conflict = incoming.conflict OR incoming.payload != excluded.payload"
    if kind == "actions":
        statement = "INSERT INTO incoming_actions(symbol, day, payload) VALUES (?, ?, ?) ON CONFLICT(symbol, day, payload) DO NOTHING"
    control.checkpoint(total=source.metadata.num_rows)
    for batch in source.iter_batches(batch_size=BATCH_SIZE, columns=list(dump_fields(kind))):
        control.checkpoint()
        rows = [normalize_row(row, kind, today=today) for row in batch.to_pylist()]
        db.executemany(statement, ((row["thscode"], row[date_field], canonical_json_bytes(row).decode("utf-8")) for row in rows))
        count += len(rows)
        control.checkpoint(current=count)
    return count


def seed_verified_daily(db: sqlite3.Connection, path: Path, control: FuyaoSyncControl | None = None) -> None:
    """Reuse only the immutable canonical file whose hash was just verified."""
    control = control or FuyaoSyncControl()
    control.checkpoint("seeding_daily", current=0, unit="rows")
    _, parquet = parquet_modules()
    source = parquet.ParquetFile(path)
    require_schema(source.schema_arrow.names, "daily")
    count = 0
    control.checkpoint(total=source.metadata.num_rows)
    for batch in source.iter_batches(batch_size=BATCH_SIZE, columns=list(dump_fields("daily"))):
        control.checkpoint()
        rows = batch.to_pylist()
        db.executemany("INSERT INTO daily(symbol, day, payload) VALUES (?, ?, ?)",
                       ((row["thscode"], row["date_ms"], canonical_json_bytes(row).decode("utf-8")) for row in rows))
        count += len(rows)
        control.checkpoint(current=count)
    db.commit()


def write_parquet(db: sqlite3.Connection, output: Path, kind: DumpKind, control: FuyaoSyncControl | None = None) -> FuyaoDumpFile:
    control = control or FuyaoSyncControl()
    control.checkpoint(f"writing_{kind}", current=0, unit="rows")
    arrow, parquet = parquet_modules()
    fields = dump_fields(kind)
    schema = arrow.schema([(field, _arrow_type(arrow, field)) for field in fields])
    cursor = db.execute(f"SELECT payload FROM {kind} ORDER BY symbol, day, payload")
    control.checkpoint(total=db.execute(f"SELECT COUNT(*) FROM {kind}").fetchone()[0])
    rows = 0
    with parquet.ParquetWriter(output, schema, compression="zstd") as writer:
        while batch := cursor.fetchmany(BATCH_SIZE):
            control.checkpoint()
            values = [json.loads(row[0]) for row in batch]
            writer.write_table(arrow.Table.from_pylist(values, schema=schema), row_group_size=BATCH_SIZE)
            rows += len(values)
            control.checkpoint(current=rows)
    control.checkpoint(f"hashing_{kind}", current=0, total=output.stat().st_size, unit="bytes")
    return FuyaoDumpFile(name="daily.parquet" if kind == "daily" else "actions.parquet", sha256=file_digest(output, control), size_bytes=output.stat().st_size, rows=rows)


def _arrow_type(arrow: Any, field: str) -> Any:
    if field in ("date_ms", "ex_date_ms"):
        return arrow.int64()
    if field in ("thscode", "currency", "interval", "adjusted", "ticker"):
        return arrow.string()
    return arrow.float64()


def publish_version(root: Path, stage: Path, manifest: FuyaoDumpManifest, control: FuyaoSyncControl | None = None) -> None:
    control = control or FuyaoSyncControl()
    control.checkpoint("publishing")
    versions = safe_root(root / "versions")
    (stage / "manifest.json").write_bytes(canonical_json_bytes(manifest.model_dump(mode="json")))
    for path in stage.iterdir():
        control.checkpoint()
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    target = versions / manifest.version
    if target.exists():
        raise FuyaoDumpError("新版本目录已存在，拒绝覆盖")
    control.checkpoint()
    stage.rename(target)
    _sync_directory(target)
    _sync_directory(versions)
    control.checkpoint()
    _atomic_pointer(root, manifest.version)


def _atomic_pointer(root: Path, version: str) -> None:
    descriptor, raw = tempfile.mkstemp(prefix=".current-", dir=root)
    path = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes({"version": version}))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, root / "current.json")
        _sync_directory(root)
    finally:
        path.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
