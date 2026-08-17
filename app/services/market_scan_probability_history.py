"""Build an isolated, verified qfq history database for probability research."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Literal, Protocol, cast

from pydantic import ValidationError

from app.artifacts.io import (
    ArtifactContentConflictError,
    ArtifactIOError,
    ArtifactPublishConflictError,
    decode_json_bytes,
    exclusive_atomic_publish,
    path_has_only_trusted_aliases,
    read_regular_file,
)
from app.models.local_data import RuntimeBackupManifest, RuntimeBackupVerification
from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline
from app.services.providers import TencentMarketDataProvider
from app.services.runtime_backup import (
    BACKUP_DATABASE_NAME,
    BACKUP_MANIFEST_NAME,
    RuntimeBackupError,
    verify_runtime_backup,
)
from app.services.trading_calendar import previous_trade_date
from app.utils.clock import utc_now
from app.utils.provider_errors import (
    ProviderCoverageMiss,
    ProviderInstrumentDataError,
    ProviderProtocolError,
    ProviderTransportError,
)


PROBABILITY_HISTORY_SCHEMA_VERSION = "market-scan-probability-history-v1"
PROBABILITY_HISTORY_MANIFEST_SCHEMA_VERSION = "market-scan-probability-history-manifest-v2"
PROBABILITY_HISTORY_LEGACY_MANIFEST_SCHEMA_VERSION = "market-scan-probability-history-manifest-v1"
PROBABILITY_HISTORY_COHORT_MODE = "historical_replay_v1"
PROBABILITY_HISTORY_BARS = 360
PROBABILITY_HISTORY_MARKETS = ("SH", "SZ", "BJ")
PROBABILITY_HISTORY_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
_DEFAULT_SYMBOL_LIMIT = 90
_MAXIMUM_SYMBOL_LIMIT = 120
_DATABASE_USER_VERSION = 1
_INTEGRITY_NOTICE = "SHA-256 detects accidental mutation; it is not an authenticity signature."
_TABLE_COLUMNS = (
    "symbol", "adjustment_mode", "date", "open", "close", "high", "low", "volume",
    "as_of", "data_version", "contract_version", "fallback_used", "source", "fetched_at",
)

ProbabilityHistoryManifestAssurance = Literal[
    "attested_v2",
    "legacy_attested_v1",
    "legacy_unattested",
]


class ProbabilityHistoryError(RuntimeError):
    """Raised when an isolated history bundle cannot be safely published or verified."""


class HistoryKlineProvider(Protocol):
    source_name: str

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]: ...


@dataclass(frozen=True)
class ProbabilityHistoryConfig:
    symbol_limit: int = _DEFAULT_SYMBOL_LIMIT
    symbols: tuple[str, ...] = ()
    history_bars: int = PROBABILITY_HISTORY_BARS
    concurrency: int = 2
    max_retries: int = 2
    retry_delay_seconds: float = 0.5
    minimum_symbols_per_market: int = 20
    minimum_symbols_total: int = 60

    def __post_init__(self) -> None:
        if isinstance(self.symbol_limit, bool) or not 1 <= self.symbol_limit <= _MAXIMUM_SYMBOL_LIMIT:
            raise ValueError(f"symbol_limit 必须在 1 到 {_MAXIMUM_SYMBOL_LIMIT} 之间")
        if self.history_bars != PROBABILITY_HISTORY_BARS:
            raise ValueError(f"history_bars 固定为 {PROBABILITY_HISTORY_BARS}")
        if isinstance(self.concurrency, bool) or not 1 <= self.concurrency <= 2:
            raise ValueError("concurrency 必须在 1 到 2 之间")
        if isinstance(self.max_retries, bool) or not 0 <= self.max_retries <= 2:
            raise ValueError("max_retries 必须在 0 到 2 之间")
        if not math.isfinite(self.retry_delay_seconds) or self.retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 必须是非负有限数")
        _validate_minimums(self)
        normalized = tuple(dict.fromkeys(value.strip().upper() for value in self.symbols if value.strip()))
        if len(normalized) > self.symbol_limit:
            raise ValueError("显式 symbols 数量不能超过 symbol_limit")
        object.__setattr__(self, "symbols", normalized)


@dataclass(frozen=True)
class ProbabilityHistoryBuildResult:
    database_path: Path
    manifest_path: Path
    manifest: dict[str, object]


@dataclass(frozen=True)
class LegacyProbabilityHistoryManifestResult:
    manifest: dict[str, object]
    assurance: Literal["legacy_attested_v1", "legacy_unattested"]
    trusted_for_attested_research: Literal[False] = False


@dataclass(frozen=True)
class _SourceUniverse:
    anchor_date: str
    universe_symbols: tuple[str, ...]
    selected_symbols: tuple[str, ...]
    universe_market_counts: dict[str, int]
    selected_market_counts: dict[str, int]
    sampling_strategy: str
    fingerprint: dict[str, int]
    backup: _VerifiedSourceBackup


@dataclass(frozen=True)
class _VerifiedSourceBackup:
    database_path: Path
    manifest_path: Path
    sha256: str
    fingerprint: dict[str, int]
    manifest: dict[str, object]


@dataclass(frozen=True)
class _FetchResult:
    symbol: str
    rows: tuple[Kline, ...] | None
    attempts: int
    error_category: str | None


@dataclass
class _ConcurrencyTracker:
    active: int = 0
    maximum: int = 0


@dataclass(frozen=True)
class _HistoryDirectoryGuard:
    path: Path
    label: str
    device: int
    inode: int


async def backfill_market_scan_probability_history(
    source_database: str | Path,
    target_database: str | Path,
    output_dir: str | Path,
    *,
    config: ProbabilityHistoryConfig | None = None,
    provider: HistoryKlineProvider | None = None,
    generated_at: str | None = None,
    provider_timeout_seconds: float = 15.0,
) -> ProbabilityHistoryBuildResult:
    """Fetch, stage, verify and exclusively publish one isolated research database."""
    config = config or ProbabilityHistoryConfig()
    source, target, output, target_guard, output_guard = _validated_paths(
        source_database,
        target_database,
        output_dir,
    )
    source_backup = _verified_static_source_backup(source)
    universe = _read_source_universe(source, config, source_backup)
    expected_dates = trusted_probability_history_dates(universe.anchor_date, config.history_bars)
    owned_provider = provider is None
    selected_provider = provider or TencentMarketDataProvider(timeout=provider_timeout_seconds)
    timestamp = generated_at or utc_now().isoformat(timespec="seconds")
    try:
        fetched, maximum_concurrency = await _fetch_selected(
            selected_provider, universe.selected_symbols, config,
        )
    finally:
        if owned_provider:
            await cast(TencentMarketDataProvider, selected_provider).aclose()
    accepted, exclusions = _validated_downloads(fetched, expected_dates)
    _require_no_provider_failures(fetched)
    _require_coverage(accepted, config)
    if _verified_static_source_backup(source) != source_backup:
        raise ProbabilityHistoryError("只读源 runtime_data 备份在研究抓取期间发生外部变化；本批拒绝发布")
    return _stage_and_publish(
        source, target, output, target_guard, output_guard, config, universe, accepted, exclusions,
        fetched, maximum_concurrency, expected_dates, timestamp, selected_provider.source_name,
    )


def trusted_probability_history_dates(anchor_date: str, count: int = PROBABILITY_HISTORY_BARS) -> tuple[str, ...]:
    """Return the fixed trusted exchange-session grid ending at ``anchor_date``."""
    if isinstance(count, bool) or count <= 0:
        raise ValueError("count 必须是正整数")
    try:
        current = previous_trade_date(date.fromisoformat(anchor_date))
    except ValueError as exc:
        raise ValueError("anchor_date 必须是 YYYY-MM-DD") from exc
    values: list[date] = []
    for _index in range(count):
        values.append(current)
        current = previous_trade_date(current - timedelta(days=1))
    return tuple(value.isoformat() for value in reversed(values))


def history_manifest_filename(manifest: Mapping[str, object]) -> str:
    """Return the only accepted content-addressed manifest filename."""
    verified = _verified_manifest_identity(manifest)
    integrity = _mapping(verified["integrity"], "integrity")
    return f"market-scan-probability-history-{integrity['integrity_digest']}.manifest.json"


def probability_history_manifest_assurance(
    manifest: Mapping[str, object],
) -> ProbabilityHistoryManifestAssurance:
    """Classify verified manifest provenance without mutating its sealed payload."""
    normalized = _verified_manifest_identity(manifest)
    payload = _mapping(normalized["payload"], "payload")
    return _validate_payload_contract(payload, manifest_schema_version=str(normalized["schema_version"]))


def verify_market_scan_probability_history_manifest(
    manifest: Mapping[str, object],
    *,
    database_path: str | Path | None = None,
    require_attested: bool = True,
) -> dict[str, object]:
    """Deep-verify manifest identity and every persisted SQLite bar/digest."""
    normalized = _verified_manifest_identity(manifest)
    payload = _mapping(normalized["payload"], "payload")
    assurance = _validate_payload_contract(
        payload,
        manifest_schema_version=str(normalized["schema_version"]),
    )
    if require_attested and assurance != "attested_v2":
        raise ProbabilityHistoryError(f"研究历史 manifest 不是当前 attested v2：{assurance}")
    database = _manifest_database_path(payload, database_path)
    expected_dates = trusted_probability_history_dates(
        str(payload["anchor_date"]), int(cast(int, _mapping(payload["config"], "config")["history_bars"])),
    )
    expected_database = _mapping(payload["database"], "database")
    actual_database = _database_facts(database, expected_dates)
    if actual_database != {key: value for key, value in expected_database.items() if key != "path"}:
        raise ProbabilityHistoryError("研究历史 SQLite 内容与 manifest 冲突")
    _validate_quality(payload, actual_database)
    return normalized


def load_market_scan_probability_history_manifest(
    path: str | Path,
    *,
    database_path: str | Path | None = None,
    require_attested: bool = True,
) -> dict[str, object]:
    """Load an immutable manifest and replay its deep SQLite verification."""
    source = Path(path).expanduser().absolute()
    try:
        decoded = decode_json_bytes(
            read_regular_file(source, max_bytes=PROBABILITY_HISTORY_MANIFEST_MAX_BYTES),
        )
    except ArtifactIOError as exc:
        raise ProbabilityHistoryError("研究历史 manifest 无法读取") from exc
    if not isinstance(decoded, Mapping):
        raise ProbabilityHistoryError("研究历史 manifest 顶层必须是 object")
    verified = verify_market_scan_probability_history_manifest(
        decoded,
        database_path=database_path,
        require_attested=require_attested,
    )
    if source.name != history_manifest_filename(verified):
        raise ProbabilityHistoryError("研究历史 manifest 文件名不是内容地址")
    return verified


def load_legacy_market_scan_probability_history_manifest(
    path: str | Path,
    *,
    database_path: str | Path | None = None,
) -> LegacyProbabilityHistoryManifestResult:
    """Read a v1 artifact explicitly while keeping it outside current attested trust."""
    verified = load_market_scan_probability_history_manifest(
        path,
        database_path=database_path,
        require_attested=False,
    )
    assurance = probability_history_manifest_assurance(verified)
    if assurance == "attested_v2":
        raise ProbabilityHistoryError("当前 attested v2 manifest 不得通过 legacy reader 加载")
    return LegacyProbabilityHistoryManifestResult(verified, assurance)


def upgrade_legacy_attested_probability_history_manifest(
    path: str | Path,
    output_dir: str | Path,
    *,
    database_path: str | Path | None = None,
) -> Path:
    """Publish a v2 seal only for a deeply verified transitional attested v1 manifest."""
    legacy = load_legacy_market_scan_probability_history_manifest(path, database_path=database_path)
    if legacy.assurance != "legacy_attested_v1":
        raise ProbabilityHistoryError(f"legacy manifest 缺少可升级的 runtime backup 证明：{legacy.assurance}")
    upgraded = _json_copy(legacy.manifest)
    upgraded["schema_version"] = PROBABILITY_HISTORY_MANIFEST_SCHEMA_VERSION
    upgraded.pop("integrity", None)
    upgraded["integrity"] = {
        "algorithm": "sha256",
        "integrity_digest": _sha256_json(upgraded),
        "notice": _INTEGRITY_NOTICE,
    }
    verified = verify_market_scan_probability_history_manifest(
        upgraded,
        database_path=database_path,
        require_attested=True,
    )
    output = Path(output_dir).expanduser().absolute()
    _prepare_history_directory(output, label="upgrade manifest output")
    target = output / history_manifest_filename(verified)
    encoded = _encoded_manifest(verified)
    _publish_bytes_exclusive(target, encoded)
    return target


def canonical_probability_history_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_minimums(config: ProbabilityHistoryConfig) -> None:
    values = (config.minimum_symbols_per_market, config.minimum_symbols_total)
    if any(isinstance(value, bool) or value <= 0 for value in values):
        raise ValueError("minimum symbol 门槛必须是正整数")
    if config.minimum_symbols_total < len(PROBABILITY_HISTORY_MARKETS) * config.minimum_symbols_per_market:
        raise ValueError("minimum_symbols_total 不能低于三市场门槛之和")
    if config.symbol_limit < config.minimum_symbols_total:
        raise ValueError("symbol_limit 不能低于 minimum_symbols_total")


def _validated_paths(
    source_database: str | Path,
    target_database: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path, Path, _HistoryDirectoryGuard, _HistoryDirectoryGuard]:
    target = Path(target_database).expanduser().absolute()
    output = Path(output_dir).expanduser().absolute()
    try:
        source = Path(source_database).expanduser().resolve()
        target_comparison = target.resolve(strict=False)
        output_comparison = output.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProbabilityHistoryError("source/target/output 路径无法解析") from exc
    if not source.is_file():
        raise ProbabilityHistoryError("只读源 SQLite 不存在或不是文件")
    if target_comparison == source or output_comparison in {source, target_comparison}:
        raise ProbabilityHistoryError("source/target/output 必须彼此隔离")
    if (
        source.parent in target_comparison.parents
        or output_comparison == source.parent
        or source.parent in output_comparison.parents
    ):
        raise ProbabilityHistoryError("target/output 不能写入只读 runtime_data 备份目录")
    if target.exists() or target.is_symlink():
        raise ProbabilityHistoryError("研究历史 target 已存在；拒绝覆盖")
    target_guard = _prepare_history_directory(target.parent, label="target parent")
    output_guard = _prepare_history_directory(output, label="manifest output")
    return source, target, output, target_guard, output_guard


def _prepare_history_directory(path: Path, *, label: str) -> _HistoryDirectoryGuard:
    try:
        if not path_has_only_trusted_aliases(path):
            raise ProbabilityHistoryError(f"{label} 必须是真实目录且不接受符号链接")
        path.mkdir(parents=True, exist_ok=True)
        facts = path.lstat()
    except ProbabilityHistoryError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ProbabilityHistoryError(f"{label} 无法创建或读取") from exc
    if not stat.S_ISDIR(facts.st_mode):
        raise ProbabilityHistoryError(f"{label} 必须是真实目录且不接受符号链接")
    return _HistoryDirectoryGuard(path, label, facts.st_dev, facts.st_ino)


def _validate_history_directory_guard(guard: _HistoryDirectoryGuard) -> None:
    try:
        trusted = path_has_only_trusted_aliases(guard.path)
        facts = guard.path.lstat()
    except (OSError, RuntimeError) as exc:
        raise ProbabilityHistoryError(f"{guard.label} 在研究抓取期间无法读取") from exc
    identity = (facts.st_dev, facts.st_ino)
    if not trusted or not stat.S_ISDIR(facts.st_mode) or identity != (guard.device, guard.inode):
        raise ProbabilityHistoryError(f"{guard.label} 在研究抓取期间发生变化")


def _validate_history_publish_directories(
    target_guard: _HistoryDirectoryGuard,
    output_guard: _HistoryDirectoryGuard,
) -> None:
    _validate_history_directory_guard(target_guard)
    _validate_history_directory_guard(output_guard)


def _verified_static_source_backup(source: Path) -> _VerifiedSourceBackup:
    if source.name != BACKUP_DATABASE_NAME:
        raise ProbabilityHistoryError(
            f"只读源必须是 tools/runtime_data.py backup 生成的 {BACKUP_DATABASE_NAME}",
        )
    _require_no_source_sidecars(source)
    try:
        verification = verify_runtime_backup(source.parent)
    except RuntimeBackupError as exc:
        raise ProbabilityHistoryError(f"只读源 runtime_data 备份验证失败：{exc}") from exc
    _require_matching_backup_database(source, verification)
    _require_no_source_sidecars(source)
    return _VerifiedSourceBackup(
        database_path=source,
        manifest_path=Path(verification.manifest_path).expanduser().resolve(),
        sha256=verification.sha256,
        fingerprint=_file_fingerprint(source),
        manifest=cast(dict[str, object], verification.manifest.model_dump(mode="json")),
    )


def _require_matching_backup_database(
    source: Path,
    verification: RuntimeBackupVerification,
) -> None:
    database = Path(verification.database_path).expanduser().resolve()
    manifest = Path(verification.manifest_path).expanduser().resolve()
    if database != source or manifest != source.parent / BACKUP_MANIFEST_NAME:
        raise ProbabilityHistoryError("相邻 runtime_data manifest 未绑定当前只读源 SQLite")


def _require_no_source_sidecars(source: Path) -> None:
    suffixes = ("-wal", "-shm", "-journal")
    present = [
        suffix
        for suffix in suffixes
        if (candidate := Path(f"{source}{suffix}")).exists() or candidate.is_symlink()
    ]
    if present:
        raise ProbabilityHistoryError(
            "只读源 runtime_data 备份存在 SQLite sidecar，拒绝非静态快照：" + ",".join(present),
        )


def _read_source_universe(
    source: Path,
    config: ProbabilityHistoryConfig,
    backup: _VerifiedSourceBackup,
) -> _SourceUniverse:
    try:
        with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM kline_daily "
                "WHERE adjustment_mode = 'qfq' GROUP BY symbol ORDER BY symbol",
            ).fetchall()
            conn.rollback()
    except sqlite3.Error as exc:
        raise ProbabilityHistoryError("只读源 SQLite 缺少可用 qfq universe") from exc
    catalog = tuple((str(row[0]), str(row[1])) for row in rows if row[1])
    if not catalog:
        raise ProbabilityHistoryError("只读源 SQLite 没有 qfq universe")
    anchor = max(value for _symbol, value in catalog)
    universe = tuple(symbol for symbol, latest in catalog if latest == anchor and _equity_market(symbol))
    selected, strategy = _select_symbols(universe, anchor, config)
    _require_candidate_minimums(selected, universe, config)
    return _SourceUniverse(
        anchor,
        universe,
        selected,
        _market_counts(universe),
        _market_counts(selected),
        strategy,
        backup.fingerprint,
        backup,
    )


def _select_symbols(
    universe: Sequence[str],
    anchor: str,
    config: ProbabilityHistoryConfig,
) -> tuple[tuple[str, ...], str]:
    available = frozenset(universe)
    if config.symbols:
        selected = tuple(symbol for symbol in config.symbols if symbol in available)
        return selected, "explicit_active_qfq_equity_symbols"
    grouped: dict[str, list[str]] = {market: [] for market in PROBABILITY_HISTORY_MARKETS}
    for symbol in universe:
        grouped[cast(str, _equity_market(symbol))].append(symbol)
    for market, symbols in grouped.items():
        symbols.sort(key=lambda value: (_symbol_hash(anchor, market, value), value))
    ordered = _balanced_round_robin(grouped, config.symbol_limit)
    return ordered, "deterministic_sha256_balanced_SH_SZ_BJ_active_qfq_v1"


def _balanced_round_robin(grouped: Mapping[str, Sequence[str]], limit: int) -> tuple[str, ...]:
    values: list[str] = []
    index = 0
    while len(values) < limit:
        added = False
        for market in PROBABILITY_HISTORY_MARKETS:
            candidates = grouped[market]
            if index < len(candidates) and len(values) < limit:
                values.append(candidates[index])
                added = True
        if not added:
            break
        index += 1
    return tuple(values)


def _require_candidate_minimums(
    selected: Sequence[str],
    universe: Sequence[str],
    config: ProbabilityHistoryConfig,
) -> None:
    selected_counts, universe_counts = _market_counts(selected), _market_counts(universe)
    if len(selected) < config.minimum_symbols_total:
        raise ProbabilityHistoryError("当前 qfq universe 无法满足研究样本总量门槛")
    for market in PROBABILITY_HISTORY_MARKETS:
        if selected_counts[market] < config.minimum_symbols_per_market:
            raise ProbabilityHistoryError(
                f"当前 qfq universe 无法满足 {market} 最低样本门槛；可用 {universe_counts[market]} 只",
            )


async def _fetch_selected(
    provider: HistoryKlineProvider,
    symbols: Sequence[str],
    config: ProbabilityHistoryConfig,
) -> tuple[tuple[_FetchResult, ...], int]:
    semaphore = asyncio.Semaphore(config.concurrency)
    tracker = _ConcurrencyTracker()
    tasks = [
        asyncio.create_task(_fetch_symbol(provider, symbol, config, semaphore, tracker))
        for symbol in symbols
    ]
    return tuple(await asyncio.gather(*tasks)), tracker.maximum


async def _fetch_symbol(
    provider: HistoryKlineProvider,
    symbol: str,
    config: ProbabilityHistoryConfig,
    semaphore: asyncio.Semaphore,
    tracker: _ConcurrencyTracker,
) -> _FetchResult:
    for attempt in range(1, config.max_retries + 2):
        try:
            rows = await _provider_call(provider, symbol, config.history_bars, semaphore, tracker)
            return _FetchResult(symbol, tuple(rows), attempt, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            category = _provider_error_category(exc)
            if attempt > config.max_retries:
                return _FetchResult(symbol, None, attempt, category)
            if config.retry_delay_seconds:
                await asyncio.sleep(config.retry_delay_seconds * (2 ** (attempt - 1)))
    raise AssertionError("unreachable retry loop")


async def _provider_call(
    provider: HistoryKlineProvider,
    symbol: str,
    limit: int,
    semaphore: asyncio.Semaphore,
    tracker: _ConcurrencyTracker,
) -> list[Kline]:
    async with semaphore:
        tracker.active += 1
        tracker.maximum = max(tracker.maximum, tracker.active)
        try:
            return await provider.kline(symbol, limit=limit)
        finally:
            tracker.active -= 1


def _provider_error_category(exc: Exception) -> str:
    if isinstance(exc, ProviderCoverageMiss):
        return "provider_coverage_miss"
    if isinstance(exc, ProviderTransportError):
        return "provider_transport_error"
    if isinstance(exc, ProviderInstrumentDataError):
        return "provider_instrument_data_error"
    if isinstance(exc, ProviderProtocolError):
        return "provider_protocol_error"
    if isinstance(exc, TimeoutError):
        return "provider_timeout"
    return "provider_error"


def _validated_downloads(
    fetched: Sequence[_FetchResult],
    expected_dates: Sequence[str],
) -> tuple[dict[str, tuple[Kline, ...]], dict[str, str]]:
    accepted: dict[str, tuple[Kline, ...]] = {}
    exclusions: dict[str, str] = {}
    for result in fetched:
        if result.rows is None:
            continue
        reason = _download_rejection_reason(result.rows, expected_dates)
        if reason is None:
            accepted[result.symbol] = result.rows
        else:
            exclusions[result.symbol] = reason
    return accepted, exclusions


def _download_rejection_reason(rows: Sequence[Kline], expected_dates: Sequence[str]) -> str | None:
    if len(rows) < len(expected_dates):
        return "short_history_or_new_listing"
    if len(rows) != len(expected_dates):
        return "unexpected_bar_count"
    if tuple(row.date for row in rows) != tuple(expected_dates):
        return "fixed_session_coverage_mismatch_or_suspension"
    contracts = {
        (row.adjustment_mode, row.as_of, row.data_version, row.contract_version, row.source, row.fallback_used)
        for row in rows
    }
    if len(contracts) != 1:
        return "series_contract_conflict"
    if _invalid_series_contract(next(iter(contracts)), expected_dates[-1]):
        return "invalid_qfq_series_contract"
    if any(not _valid_bar(row) for row in rows):
        return "invalid_ohlcv"
    return None


def _invalid_series_contract(contract: tuple[object, ...], anchor: str) -> bool:
    adjustment, as_of, version, contract_version, source, fallback = contract
    return bool(
        adjustment != "qfq"
        or as_of != anchor
        or not isinstance(version, str)
        or not version.strip()
        or version == "unknown"
        or contract_version != DAILY_KLINE_CONTRACT_VERSION
        or not isinstance(source, str)
        or not source.strip()
        or fallback is not False
    )


def _valid_bar(row: Kline) -> bool:
    values = tuple(float(value) for value in (row.open, row.close, row.high, row.low, row.volume))
    return bool(
        all(math.isfinite(value) for value in values)
        and row.open > 0
        and row.close > 0
        and row.low > 0
        and row.volume >= 0
        and row.low <= min(row.open, row.close)
        and row.high >= max(row.open, row.close)
    )


def _require_no_provider_failures(fetched: Sequence[_FetchResult]) -> None:
    failures = [result for result in fetched if result.rows is None]
    if not failures:
        return
    categories = Counter(result.error_category or "provider_error" for result in failures)
    summary = ", ".join(f"{key}={categories[key]}" for key in sorted(categories))
    raise ProbabilityHistoryError(f"Tencent 历史抓取存在未恢复失败，整批拒绝发布：{summary}")


def _require_coverage(
    accepted: Mapping[str, Sequence[Kline]],
    config: ProbabilityHistoryConfig,
) -> None:
    counts = _market_counts(tuple(accepted))
    if len(accepted) < config.minimum_symbols_total:
        raise ProbabilityHistoryError("固定360交易日覆盖的可用股票不足总量门槛")
    missing = [
        market for market in PROBABILITY_HISTORY_MARKETS
        if counts[market] < config.minimum_symbols_per_market
    ]
    if missing:
        raise ProbabilityHistoryError("固定360交易日覆盖未满足市场门槛：" + ",".join(missing))


def _stage_and_publish(
    source: Path,
    target: Path,
    output: Path,
    target_guard: _HistoryDirectoryGuard,
    output_guard: _HistoryDirectoryGuard,
    config: ProbabilityHistoryConfig,
    universe: _SourceUniverse,
    accepted: Mapping[str, Sequence[Kline]],
    exclusions: Mapping[str, str],
    fetched: Sequence[_FetchResult],
    maximum_concurrency: int,
    expected_dates: Sequence[str],
    generated_at: str,
    provider_source: str,
) -> ProbabilityHistoryBuildResult:
    _validate_history_publish_directories(target_guard, output_guard)
    stage = _temporary_database_path(target)
    published = False
    body_error: BaseException | None = None
    try:
        _write_staging_database(stage, accepted, generated_at)
        database_facts = _database_facts(stage, expected_dates)
        manifest = _build_manifest(
            source, target, config, universe, exclusions, fetched, maximum_concurrency,
            expected_dates, generated_at, provider_source, database_facts,
        )
        _validate_history_publish_directories(target_guard, output_guard)
        _publish_database_exclusive(stage, target)
        published = True
        _validate_history_publish_directories(target_guard, output_guard)
        verify_market_scan_probability_history_manifest(manifest, database_path=target)
        _validate_history_publish_directories(target_guard, output_guard)
        manifest_path = output / history_manifest_filename(manifest)
        _publish_bytes_exclusive(manifest_path, _encoded_manifest(manifest))
        return ProbabilityHistoryBuildResult(target, manifest_path, manifest)
    except BaseException as exc:
        body_error = exc
        if published:
            try:
                _guarded_history_unlink(target, target_guard)
            except ProbabilityHistoryError as cleanup_error:
                exc.add_note(f"研究历史失败清理未能删除已发布 SQLite：{cleanup_error}")
        raise
    finally:
        try:
            _guarded_history_unlink(stage, target_guard)
        except ProbabilityHistoryError as cleanup_error:
            if body_error is None:
                raise
            body_error.add_note(f"研究历史失败清理未能删除 staging SQLite：{cleanup_error}")


def _write_staging_database(
    stage: Path,
    accepted: Mapping[str, Sequence[Kline]],
    generated_at: str,
) -> None:
    try:
        with sqlite3.connect(stage) as conn:
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute(f"PRAGMA user_version = {_DATABASE_USER_VERSION}")
            conn.execute(_CREATE_KLINE_TABLE_SQL)
            conn.execute("CREATE INDEX idx_kline_symbol_date ON kline_daily(symbol, date)")
            rows = [
                _sqlite_row(symbol, row, generated_at)
                for symbol in sorted(accepted)
                for row in accepted[symbol]
            ]
            conn.executemany(_INSERT_KLINE_SQL, rows)
            conn.commit()
        _fsync_file(stage)
    except sqlite3.Error as exc:
        raise ProbabilityHistoryError("研究历史 staging SQLite 写入失败") from exc


def _sqlite_row(symbol: str, row: Kline, generated_at: str) -> tuple[object, ...]:
    return (
        symbol, row.adjustment_mode, row.date, float(row.open), float(row.close), float(row.high),
        float(row.low), float(row.volume), row.as_of, row.data_version, row.contract_version,
        int(row.fallback_used), row.source, row.fetched_at or generated_at,
    )


def _database_facts(database: Path, expected_dates: Sequence[str]) -> dict[str, object]:
    try:
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            if str(conn.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
                raise ProbabilityHistoryError("研究历史 SQLite quick_check 失败")
            _require_replay_schema(conn)
            rows = conn.execute(
                f"SELECT {','.join(_TABLE_COLUMNS)} FROM kline_daily ORDER BY symbol,date",
            ).fetchall()
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        raise ProbabilityHistoryError("研究历史 SQLite 无法验证") from exc
    symbols = _group_database_rows(rows)
    contracts = {
        symbol: _symbol_database_contract(symbol, values, expected_dates)
        for symbol, values in symbols.items()
    }
    return {
        "sha256": _sha256_file(database), "size_bytes": database.stat().st_size,
        "sqlite_quick_check": "ok", "user_version": user_version,
        "table": "kline_daily", "row_count": len(rows), "symbol_count": len(symbols),
        "market_counts": _market_counts(tuple(symbols)), "bar_start": expected_dates[0],
        "bar_end": expected_dates[-1], "bars_per_symbol": len(expected_dates),
        "expected_dates_digest": _sha256_json(list(expected_dates)),
        "symbol_contracts": contracts,
    }


def _require_replay_schema(conn: sqlite3.Connection) -> None:
    columns = tuple(str(row[1]) for row in conn.execute("PRAGMA table_info(kline_daily)"))
    if columns != _TABLE_COLUMNS:
        raise ProbabilityHistoryError("研究历史 SQLite 与 replay kline_daily schema 不兼容")


def _group_database_rows(rows: Sequence[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["symbol"]), []).append(row)
    if not grouped:
        raise ProbabilityHistoryError("研究历史 SQLite 不含数据")
    return grouped


def _symbol_database_contract(
    symbol: str,
    rows: Sequence[sqlite3.Row],
    expected_dates: Sequence[str],
) -> dict[str, object]:
    if tuple(str(row["date"]) for row in rows) != tuple(expected_dates):
        raise ProbabilityHistoryError(f"研究历史 {symbol} 固定交易日覆盖冲突")
    normalized = [_database_row_values(row) for row in rows]
    _validate_database_contract_rows(symbol, rows, expected_dates[-1])
    first = rows[0]
    return {
        "row_count": len(rows), "bar_start": expected_dates[0], "bar_end": expected_dates[-1],
        "adjustment_mode": first["adjustment_mode"], "as_of": first["as_of"],
        "data_version": first["data_version"], "contract_version": first["contract_version"],
        "source": first["source"], "rows_digest": _sha256_json(normalized),
    }


def _validate_database_contract_rows(
    symbol: str,
    rows: Sequence[sqlite3.Row],
    anchor: str,
) -> None:
    contracts = {
        (
            row["adjustment_mode"], row["as_of"], row["data_version"],
            row["contract_version"], row["source"], bool(row["fallback_used"]),
        )
        for row in rows
    }
    if len(contracts) != 1 or _invalid_series_contract(next(iter(contracts)), anchor):
        raise ProbabilityHistoryError(f"研究历史 {symbol} qfq/version 契约冲突")
    if any(not _valid_database_row(row) for row in rows):
        raise ProbabilityHistoryError(f"研究历史 {symbol} OHLCV 无效")


def _valid_database_row(row: sqlite3.Row) -> bool:
    values = tuple(float(row[name]) for name in ("open", "close", "high", "low", "volume"))
    return bool(
        all(math.isfinite(value) for value in values)
        and values[0] > 0 and values[1] > 0 and values[3] > 0 and values[4] >= 0
        and values[3] <= min(values[0], values[1])
        and values[2] >= max(values[0], values[1])
    )


def _database_row_values(row: sqlite3.Row) -> list[object]:
    return [row[name] for name in _TABLE_COLUMNS]


def _build_manifest(
    source: Path,
    target: Path,
    config: ProbabilityHistoryConfig,
    universe: _SourceUniverse,
    exclusions: Mapping[str, str],
    fetched: Sequence[_FetchResult],
    maximum_concurrency: int,
    expected_dates: Sequence[str],
    generated_at: str,
    provider_source: str,
    database_facts: Mapping[str, object],
) -> dict[str, object]:
    database = {"path": str(target), **database_facts}
    payload = {
        "schema_version": PROBABILITY_HISTORY_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "ready",
        "cohort": _cohort_contract(),
        "anchor_date": universe.anchor_date,
        "config": _config_payload(config),
        "source": _source_payload(source, universe, provider_source),
        "database": database,
        "quality": _quality_payload(universe, exclusions, fetched, maximum_concurrency, database_facts),
        "replay_input": _replay_input(target, expected_dates),
        "limitations": _history_limitations(),
    }
    manifest: dict[str, object] = {
        "schema_version": PROBABILITY_HISTORY_MANIFEST_SCHEMA_VERSION,
        "generated_at": generated_at,
        "payload": payload,
    }
    manifest["integrity"] = {
        "algorithm": "sha256", "integrity_digest": _sha256_json(manifest), "notice": _INTEGRITY_NOTICE,
    }
    return manifest


def _cohort_contract() -> dict[str, object]:
    return {
        "mode": PROBABILITY_HISTORY_COHORT_MODE,
        "scope": "tencent_qfq_360_fixed_session_deterministic_sample",
        "rule_version": "historical-replay-history-source-v1",
        "official": False,
        "live_cohort_compatible": False,
        "production_ranking_effect": "none",
    }


def _config_payload(config: ProbabilityHistoryConfig) -> dict[str, object]:
    return {
        "symbol_limit": config.symbol_limit, "explicit_symbols": list(config.symbols),
        "history_bars": config.history_bars, "concurrency": config.concurrency,
        "max_retries": config.max_retries,
        "minimum_symbols_per_market": config.minimum_symbols_per_market,
        "minimum_symbols_total": config.minimum_symbols_total,
    }


def _source_payload(source: Path, universe: _SourceUniverse, provider_source: str) -> dict[str, object]:
    return {
        "database": source.name, "database_read_only": True, "database_query_only": True,
        "database_snapshot_transaction": True, "database_fingerprint": universe.fingerprint,
        "runtime_backup": {
            "verified_before_and_after_fetch": True,
            "manifest_file": universe.backup.manifest_path.name,
            "verified_sha256": universe.backup.sha256,
            "sqlite_sidecar_policy": "reject_db_wal_shm_journal_v1",
            "manifest": universe.backup.manifest,
        },
        "provider": "TencentMarketDataProvider", "provider_source": provider_source,
        "request_adjustment_mode": "qfq", "request_bars_per_symbol": PROBABILITY_HISTORY_BARS,
        "universe_symbol_count": len(universe.universe_symbols),
        "universe_market_counts": universe.universe_market_counts,
        "selected_symbol_count": len(universe.selected_symbols),
        "selected_market_counts": universe.selected_market_counts,
        "sampling_strategy": universe.sampling_strategy,
    }


def _quality_payload(
    universe: _SourceUniverse,
    exclusions: Mapping[str, str],
    fetched: Sequence[_FetchResult],
    maximum_concurrency: int,
    database_facts: Mapping[str, object],
) -> dict[str, object]:
    accepted = int(cast(int, database_facts["symbol_count"]))
    return {
        "status": "ready", "attempted_symbol_count": len(fetched),
        "accepted_symbol_count": accepted, "excluded_symbol_count": len(exclusions),
        "excluded_reason_counts": dict(sorted(Counter(exclusions.values()).items())),
        "excluded_symbols": dict(sorted(exclusions.items())),
        "attempt_count": sum(result.attempts for result in fetched),
        "retried_symbol_count": sum(result.attempts > 1 for result in fetched),
        "maximum_observed_concurrency": maximum_concurrency,
        "accepted_market_counts": database_facts["market_counts"],
        "selected_market_counts": universe.selected_market_counts,
        "bar_coverage": 1.0, "bars_per_symbol": PROBABILITY_HISTORY_BARS,
        "new_or_short_history_excluded": True,
    }


def _replay_input(target: Path, dates: Sequence[str]) -> dict[str, object]:
    minimum_history_bars, maximum_horizon_plus_entry = 61, 21
    start_index = minimum_history_bars - 1
    end_index = len(dates) - maximum_horizon_plus_entry - 1
    if end_index < start_index:
        raise ProbabilityHistoryError("360根历史无法形成 replay 日期范围")
    return {
        "database": str(target), "start_date": dates[start_index], "end_date": dates[end_index],
        "minimum_history_bars": minimum_history_bars,
        "horizons": [1, 5, 20], "cohort": PROBABILITY_HISTORY_COHORT_MODE,
        "official": False,
    }


def _history_limitations() -> list[str]:
    return [
        "historical_replay_v1_only_not_official_or_live_cohort",
        "current_qfq_universe_sampling_has_survivorship_bias",
        "new_listings_and_short_history_are_excluded",
        "fixed_session_coverage_excludes_suspension_gaps_without_forward_shift",
        "ST_status_amount_turnover_and_listing_membership_history_unavailable",
        "Tencent_public_source_is_research_only_and_not_exchange_licensed_feed",
        "qfq_provider_vintage_may_rebase_after_corporate_actions",
        "no_production_ranking_or_automatic_promotion_effect",
    ]


def _verified_manifest_identity(manifest: Mapping[str, object]) -> dict[str, object]:
    normalized = _json_copy(manifest)
    if set(normalized) != {"schema_version", "generated_at", "payload", "integrity"}:
        raise ProbabilityHistoryError("研究历史 manifest 顶层字段无效")
    if normalized["schema_version"] not in {
        PROBABILITY_HISTORY_MANIFEST_SCHEMA_VERSION,
        PROBABILITY_HISTORY_LEGACY_MANIFEST_SCHEMA_VERSION,
    }:
        raise ProbabilityHistoryError("研究历史 manifest schema_version 不受支持")
    integrity = _mapping(normalized["integrity"], "integrity")
    if set(integrity) != {"algorithm", "integrity_digest", "notice"}:
        raise ProbabilityHistoryError("研究历史 manifest integrity 字段无效")
    unsigned = {key: value for key, value in normalized.items() if key != "integrity"}
    if integrity.get("algorithm") != "sha256" or integrity.get("integrity_digest") != _sha256_json(unsigned):
        raise ProbabilityHistoryError("研究历史 manifest integrity digest 冲突")
    return normalized


def _validate_payload_contract(
    payload: Mapping[str, object],
    *,
    manifest_schema_version: str,
) -> ProbabilityHistoryManifestAssurance:
    required = {
        "schema_version", "generated_at", "status", "cohort", "anchor_date", "config", "source",
        "database", "quality", "replay_input", "limitations",
    }
    if set(payload) != required or payload.get("schema_version") != PROBABILITY_HISTORY_SCHEMA_VERSION:
        raise ProbabilityHistoryError("研究历史 payload schema 无效")
    if payload.get("status") != "ready" or payload.get("cohort") != _cohort_contract():
        raise ProbabilityHistoryError("研究历史 payload cohort/status 冲突")
    config = _mapping(payload["config"], "config")
    if config.get("history_bars") != PROBABILITY_HISTORY_BARS or config.get("concurrency") not in (1, 2):
        raise ProbabilityHistoryError("研究历史 config 冲突")
    replay = _mapping(payload["replay_input"], "replay_input")
    if replay.get("official") is not False or replay.get("cohort") != PROBABILITY_HISTORY_COHORT_MODE:
        raise ProbabilityHistoryError("研究历史 replay_input cohort 冲突")
    source = _mapping(payload["source"], "source")
    if manifest_schema_version == PROBABILITY_HISTORY_MANIFEST_SCHEMA_VERSION:
        _validate_source_contract(source)
        return "attested_v2"
    if manifest_schema_version != PROBABILITY_HISTORY_LEGACY_MANIFEST_SCHEMA_VERSION:
        raise ProbabilityHistoryError("研究历史 manifest schema_version 不受支持")
    if "runtime_backup" in source:
        _validate_source_contract(source)
        return "legacy_attested_v1"
    _validate_legacy_source_contract(source)
    return "legacy_unattested"


def _validate_source_basics(source: Mapping[str, object]) -> None:
    if (
        source.get("database_read_only") is not True
        or source.get("database_query_only") is not True
        or source.get("database_snapshot_transaction") is not True
        or source.get("provider") != "TencentMarketDataProvider"
        or source.get("request_adjustment_mode") != "qfq"
        or source.get("request_bars_per_symbol") != PROBABILITY_HISTORY_BARS
    ):
        raise ProbabilityHistoryError("研究历史 source 只读/provider 契约冲突")


def _validate_legacy_source_contract(source: Mapping[str, object]) -> None:
    required = {
        "database",
        "database_read_only",
        "database_query_only",
        "database_snapshot_transaction",
        "database_fingerprint",
        "provider",
        "provider_source",
        "request_adjustment_mode",
        "request_bars_per_symbol",
        "universe_symbol_count",
        "universe_market_counts",
        "selected_symbol_count",
        "selected_market_counts",
        "sampling_strategy",
    }
    if set(source) != required:
        raise ProbabilityHistoryError("研究历史 legacy v1 source 字段无效")
    _validate_source_basics(source)
    fingerprint = _mapping(source.get("database_fingerprint"), "source.database_fingerprint")
    if (
        set(fingerprint) != {"size_bytes", "mtime_ns"}
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in fingerprint.values())
    ):
        raise ProbabilityHistoryError("研究历史 legacy v1 source fingerprint 无效")


def _validate_source_contract(source: Mapping[str, object]) -> None:
    if set(source) != {
        "database",
        "database_read_only",
        "database_query_only",
        "database_snapshot_transaction",
        "database_fingerprint",
        "runtime_backup",
        "provider",
        "provider_source",
        "request_adjustment_mode",
        "request_bars_per_symbol",
        "universe_symbol_count",
        "universe_market_counts",
        "selected_symbol_count",
        "selected_market_counts",
        "sampling_strategy",
    }:
        raise ProbabilityHistoryError("研究历史 source 字段无效")
    _validate_source_basics(source)
    runtime_backup = _mapping(source.get("runtime_backup"), "source.runtime_backup")
    if set(runtime_backup) != {
        "verified_before_and_after_fetch",
        "manifest_file",
        "verified_sha256",
        "sqlite_sidecar_policy",
        "manifest",
    }:
        raise ProbabilityHistoryError("研究历史 runtime_data 备份证明字段无效")
    if (
        runtime_backup.get("verified_before_and_after_fetch") is not True
        or runtime_backup.get("manifest_file") != BACKUP_MANIFEST_NAME
        or runtime_backup.get("sqlite_sidecar_policy") != "reject_db_wal_shm_journal_v1"
    ):
        raise ProbabilityHistoryError("研究历史 runtime_data 备份证明状态无效")
    raw_manifest = _mapping(runtime_backup.get("manifest"), "source.runtime_backup.manifest")
    try:
        manifest = RuntimeBackupManifest.model_validate(raw_manifest)
    except ValidationError as exc:
        raise ProbabilityHistoryError("研究历史 runtime_data manifest schema 无效") from exc
    digest = runtime_backup.get("verified_sha256")
    fingerprint = _mapping(source.get("database_fingerprint"), "source.database_fingerprint")
    if (
        not _valid_sha256(digest)
        or set(fingerprint) != {"size_bytes", "mtime_ns", "ctime_ns", "inode"}
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in fingerprint.values())
        or manifest.sha256 != digest
        or manifest.database_file != source.get("database")
        or manifest.database_size_bytes != fingerprint.get("size_bytes")
        or manifest.integrity_check != "ok"
    ):
        raise ProbabilityHistoryError("研究历史 runtime_data manifest 绑定冲突")


def _validate_quality(payload: Mapping[str, object], database: Mapping[str, object]) -> None:
    quality = _mapping(payload["quality"], "quality")
    if quality.get("status") != "ready" or quality.get("bar_coverage") != 1.0:
        raise ProbabilityHistoryError("研究历史 quality 状态无效")
    if quality.get("accepted_symbol_count") != database["symbol_count"]:
        raise ProbabilityHistoryError("研究历史 accepted symbol 计数冲突")
    if quality.get("accepted_market_counts") != database["market_counts"]:
        raise ProbabilityHistoryError("研究历史 accepted market 计数冲突")
    config = _mapping(payload["config"], "config")
    counts = _mapping(database["market_counts"], "market_counts")
    if int(cast(int, database["symbol_count"])) < int(cast(int, config["minimum_symbols_total"])):
        raise ProbabilityHistoryError("研究历史不满足总量门槛")
    minimum = int(cast(int, config["minimum_symbols_per_market"]))
    if any(int(cast(int, counts[market])) < minimum for market in PROBABILITY_HISTORY_MARKETS):
        raise ProbabilityHistoryError("研究历史不满足分市场门槛")


def _manifest_database_path(payload: Mapping[str, object], override: str | Path | None) -> Path:
    database = _mapping(payload["database"], "database")
    candidate = Path(override if override is not None else str(database.get("path") or ""))
    path = candidate.expanduser().resolve()
    if not path.is_file():
        raise ProbabilityHistoryError("研究历史 manifest 指向的 SQLite 不存在")
    return path


def _temporary_database_path(target: Path) -> Path:
    try:
        descriptor, raw = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".staging",
        )
    except OSError as exc:
        raise ProbabilityHistoryError("研究历史 staging SQLite 无法创建") from exc
    os.close(descriptor)
    return Path(raw)


def _guarded_history_unlink(path: Path, guard: _HistoryDirectoryGuard) -> None:
    _validate_history_directory_guard(guard)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ProbabilityHistoryError(f"研究历史文件无法安全删除：{path}") from exc


def _publish_database_exclusive(stage: Path, target: Path) -> None:
    try:
        os.link(stage, target)
    except FileExistsError as exc:
        raise ProbabilityHistoryError("研究历史 SQLite 并发发布冲突；拒绝覆盖") from exc
    except OSError as exc:
        raise ProbabilityHistoryError("研究历史 SQLite 原子发布失败") from exc


def _publish_bytes_exclusive(target: Path, encoded: bytes) -> None:
    try:
        exclusive_atomic_publish(
            target,
            encoded,
            max_bytes=PROBABILITY_HISTORY_MANIFEST_MAX_BYTES,
        )
    except ArtifactContentConflictError as exc:
        raise ProbabilityHistoryError("研究历史 manifest 内容地址已存在但字节不同") from exc
    except ArtifactPublishConflictError as exc:
        raise ProbabilityHistoryError("研究历史 manifest 并发发布冲突；拒绝覆盖") from exc
    except ArtifactIOError as exc:
        raise ProbabilityHistoryError("研究历史 manifest 原子发布失败") from exc


def _encoded_manifest(manifest: Mapping[str, object]) -> bytes:
    return (canonical_probability_history_json(manifest) + "\n").encode("utf-8")


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _market_counts(symbols: Sequence[str]) -> dict[str, int]:
    counts = Counter(_equity_market(symbol) for symbol in symbols)
    return {market: counts.get(market, 0) for market in PROBABILITY_HISTORY_MARKETS}


def _equity_market(symbol: str) -> str | None:
    code, separator, market = symbol.upper().partition(".")
    if not separator or not code.isdigit() or len(code) != 6:
        return None
    if market == "SH" and code.startswith(("600", "601", "603", "605", "688", "689")):
        return "SH"
    if market == "SZ" and code.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZ"
    if market == "BJ" and code.startswith(("4", "8", "9")):
        return "BJ"
    return None


def _symbol_hash(anchor: str, market: str, symbol: str) -> str:
    return hashlib.sha256(f"history-v1|{anchor}|{market}|{symbol}".encode()).hexdigest()


def _file_fingerprint(path: Path) -> dict[str, int]:
    facts = path.stat()
    return {
        "size_bytes": facts.st_size,
        "mtime_ns": facts.st_mtime_ns,
        "ctime_ns": facts.st_ctime_ns,
        "inode": facts.st_ino,
    }


def _valid_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_probability_history_json(value).encode()).hexdigest()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ProbabilityHistoryError(f"{label} 必须是 object")
    return dict(value)


def _json_copy(value: Mapping[str, object]) -> dict[str, object]:
    try:
        return cast(dict[str, object], json.loads(canonical_probability_history_json(value)))
    except (TypeError, ValueError) as exc:
        raise ProbabilityHistoryError("研究历史 manifest 包含不可序列化值") from exc


_CREATE_KLINE_TABLE_SQL = """
CREATE TABLE kline_daily (
    symbol TEXT NOT NULL,
    adjustment_mode TEXT NOT NULL CHECK (adjustment_mode IN ('qfq','hfq','none','unknown')),
    date TEXT NOT NULL,
    open REAL NOT NULL,
    close REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    volume REAL NOT NULL,
    as_of TEXT,
    data_version TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    fetched_at TEXT,
    PRIMARY KEY (symbol, adjustment_mode, date)
)
"""
_INSERT_KLINE_SQL = f"INSERT INTO kline_daily ({','.join(_TABLE_COLUMNS)}) VALUES ({','.join('?' for _ in _TABLE_COLUMNS)})"


__all__ = [
    "PROBABILITY_HISTORY_BARS",
    "PROBABILITY_HISTORY_COHORT_MODE",
    "PROBABILITY_HISTORY_LEGACY_MANIFEST_SCHEMA_VERSION",
    "PROBABILITY_HISTORY_MANIFEST_MAX_BYTES",
    "PROBABILITY_HISTORY_MANIFEST_SCHEMA_VERSION",
    "PROBABILITY_HISTORY_SCHEMA_VERSION",
    "HistoryKlineProvider",
    "LegacyProbabilityHistoryManifestResult",
    "ProbabilityHistoryBuildResult",
    "ProbabilityHistoryConfig",
    "ProbabilityHistoryError",
    "ProbabilityHistoryManifestAssurance",
    "backfill_market_scan_probability_history",
    "canonical_probability_history_json",
    "history_manifest_filename",
    "load_legacy_market_scan_probability_history_manifest",
    "load_market_scan_probability_history_manifest",
    "probability_history_manifest_assurance",
    "trusted_probability_history_dates",
    "upgrade_legacy_attested_probability_history_manifest",
    "verify_market_scan_probability_history_manifest",
]
