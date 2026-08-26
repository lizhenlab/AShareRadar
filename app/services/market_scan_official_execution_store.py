"""Fail-closed catalog for licensed official execution-session artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
import stat
from threading import RLock

from app.artifacts.io import (
    ArtifactIOError,
    canonical_json_bytes,
    exclusive_atomic_publish,
)
from app.services.market_scan_contracts import MarketScanSettingsProtocol
from app.services.market_scan_official_execution import (
    OFFICIAL_EXECUTION_MAX_SESSION_BYTES,
    OfficialExecutionIntakeError,
    VerifiedOfficialExecutionSession,
    VerifiedOfficialExecutionSourceRegistry,
    load_verified_official_execution_session,
    load_verified_official_execution_source_registry,
)


OFFICIAL_EXECUTION_SESSION_DIRECTORY_RELATIVE_PATH = Path(
    "research/market_scan_official_execution/sessions"
)
_SESSION_FILENAME = re.compile(
    r"official-execution-session-(\d{4}-\d{2}-\d{2})-([0-9a-f]{64})\.json"
)
_PathFingerprint = tuple[str, int, int, int, int, int, int]
_CatalogFingerprint = tuple[
    _PathFingerprint,
    _PathFingerprint | None,
    tuple[_PathFingerprint, ...],
]
_StoreFingerprint = tuple[
    _CatalogFingerprint,
    _PathFingerprint | None,
    tuple[_PathFingerprint, ...],
]


@dataclass(frozen=True)
class OfficialExecutionStoreStatus:
    configured: bool
    status: str
    registry_digest: str | None
    verified_session_count: int
    first_session_date: str | None
    latest_session_date: str | None
    failures: tuple[str, ...] = ()

    @property
    def formal_evidence_available(self) -> bool:
        return self.configured and self.status == "ready" and self.verified_session_count > 0

    def payload(self) -> dict[str, object]:
        return {
            "contract_version": "official-execution-store-status-v1",
            "configured": self.configured,
            "status": self.status,
            "registry_digest": self.registry_digest,
            "verified_session_count": self.verified_session_count,
            "first_session_date": self.first_session_date,
            "latest_session_date": self.latest_session_date,
            "failures": list(self.failures),
            "formal_evidence_available": self.formal_evidence_available,
            "public_vendor_auto_upgrade_forbidden": True,
        }


class MarketScanOfficialExecutionStore:
    """Load only sessions backed by a pinned registry and exact raw bytes."""

    def __init__(
        self,
        *,
        registry_path: str | Path,
        registry_digest: str | None,
        raw_file_root: str | Path,
        session_directory: str | Path,
    ) -> None:
        self.registry_path = Path(registry_path).expanduser().absolute()
        self.registry_digest = registry_digest
        self.raw_file_root = Path(raw_file_root).expanduser().absolute()
        self.session_directory = Path(session_directory).expanduser().absolute()
        self._lock = RLock()
        self._session_cache: tuple[VerifiedOfficialExecutionSession, ...] | None = None
        self._session_cache_fingerprint: _StoreFingerprint | None = None

    @classmethod
    def from_settings(
        cls, settings: MarketScanSettingsProtocol
    ) -> "MarketScanOfficialExecutionStore":
        return cls(
            registry_path=settings.market_scan_official_execution_registry_path,
            registry_digest=settings.market_scan_official_execution_registry_digest,
            raw_file_root=settings.market_scan_official_execution_raw_root,
            session_directory=settings.market_scan_official_execution_session_directory,
        )

    def registry(self) -> VerifiedOfficialExecutionSourceRegistry:
        if self.registry_digest is None:
            raise OfficialExecutionIntakeError(
                "official execution registry digest is not configured out of band"
            )
        return load_verified_official_execution_source_registry(
            self.registry_path,
            expected_registry_digest=self.registry_digest,
        )

    def sessions(self) -> tuple[VerifiedOfficialExecutionSession, ...]:
        with self._lock:
            return self._sessions_locked()

    def _sessions_locked(self) -> tuple[VerifiedOfficialExecutionSession, ...]:
        cached = self._cached_sessions()
        if cached is not None:
            return cached
        catalog_before = _catalog_fingerprint(
            self.registry_path,
            self.session_directory,
        )
        registry = self.registry()
        if not self.session_directory.exists():
            sessions: tuple[VerifiedOfficialExecutionSession, ...] = ()
            self._cache_sessions(sessions, catalog_before)
            return sessions
        if not self.session_directory.is_dir() or self.session_directory.is_symlink():
            raise OfficialExecutionIntakeError("official execution session directory is unsafe")
        loaded: list[VerifiedOfficialExecutionSession] = []
        seen_dates: set[str] = set()
        for path in sorted(self.session_directory.iterdir(), key=lambda item: item.name):
            match = _SESSION_FILENAME.fullmatch(path.name)
            if match is None:
                continue
            session = load_verified_official_execution_session(
                path,
                registry=registry,
                raw_file_root=self.raw_file_root,
            )
            filename_date, filename_digest = match.groups()
            if (
                session.session_date != filename_date
                or session.artifact_digest != filename_digest
            ):
                raise OfficialExecutionIntakeError(
                    "official execution session filename/content identity mismatch"
                )
            if session.session_date in seen_dates:
                raise OfficialExecutionIntakeError(
                    f"official execution session date has conflicting artifacts: {session.session_date}"
                )
            seen_dates.add(session.session_date)
            loaded.append(session)
        catalog_after = _catalog_fingerprint(
            self.registry_path,
            self.session_directory,
        )
        if catalog_before != catalog_after:
            raise OfficialExecutionIntakeError(
                "official execution catalog changed during verification"
            )
        sessions = tuple(loaded)
        self._cache_sessions(sessions, catalog_after)
        return sessions

    def _cached_sessions(self) -> tuple[VerifiedOfficialExecutionSession, ...] | None:
        sessions = self._session_cache
        expected = self._session_cache_fingerprint
        if sessions is None or expected is None:
            return None
        try:
            current = _store_fingerprint(
                self.registry_path,
                self.raw_file_root,
                self.session_directory,
                sessions,
            )
        except (OSError, OfficialExecutionIntakeError):
            return None
        return sessions if current == expected else None

    def _cache_sessions(
        self,
        sessions: tuple[VerifiedOfficialExecutionSession, ...],
        catalog: _CatalogFingerprint,
    ) -> None:
        self._session_cache = sessions
        self._session_cache_fingerprint = _store_fingerprint(
            self.registry_path,
            self.raw_file_root,
            self.session_directory,
            sessions,
            catalog=catalog,
        )

    def session(self, session_date: str) -> VerifiedOfficialExecutionSession | None:
        return next((item for item in self.sessions() if item.session_date == session_date), None)

    def status(self) -> OfficialExecutionStoreStatus:
        if self.registry_digest is None:
            return OfficialExecutionStoreStatus(
                configured=False,
                status="unconfigured_pinned_registry",
                registry_digest=None,
                verified_session_count=0,
                first_session_date=None,
                latest_session_date=None,
                failures=(
                    "licensed_official_source_registry_and_digest_not_configured",
                ),
            )
        try:
            sessions = self.sessions()
        except (OfficialExecutionIntakeError, OSError) as exc:
            return OfficialExecutionStoreStatus(
                configured=True,
                status="verification_failed",
                registry_digest=self.registry_digest,
                verified_session_count=0,
                first_session_date=None,
                latest_session_date=None,
                failures=(_short_error(exc),),
            )
        dates = [item.session_date for item in sessions]
        return OfficialExecutionStoreStatus(
            configured=True,
            status="ready" if sessions else "waiting_sessions",
            registry_digest=self.registry_digest,
            verified_session_count=len(sessions),
            first_session_date=min(dates) if dates else None,
            latest_session_date=max(dates) if dates else None,
        )

    def ingest(self, candidate_path: str | Path) -> Path:
        """Verify candidate plus raw bytes before immutable managed publication."""

        registry = self.registry()
        verified = load_verified_official_execution_session(
            candidate_path,
            registry=registry,
            raw_file_root=self.raw_file_root,
        )
        target = self.session_directory / official_execution_session_filename(verified)
        encoded = canonical_json_bytes(verified.artifact)
        try:
            exclusive_atomic_publish(
                target,
                encoded,
                max_bytes=OFFICIAL_EXECUTION_MAX_SESSION_BYTES,
            )
        except ArtifactIOError as exc:
            raise OfficialExecutionIntakeError(
                "official execution session cannot be published immutably"
            ) from exc
        # Re-open the managed target through the complete trust boundary.  A
        # successful write alone is never returned as authority.
        reloaded = load_verified_official_execution_session(
            target,
            registry=registry,
            raw_file_root=self.raw_file_root,
        )
        if (
            reloaded.artifact_digest != verified.artifact_digest
            or reloaded.raw_file_set_digest != verified.raw_file_set_digest
        ):
            raise OfficialExecutionIntakeError("official execution managed replay mismatch")
        with self._lock:
            self._session_cache = None
            self._session_cache_fingerprint = None
        return target


def official_execution_session_filename(
    session: VerifiedOfficialExecutionSession | Mapping[str, object],
) -> str:
    if isinstance(session, VerifiedOfficialExecutionSession):
        session_date = session.session_date
        artifact_digest = session.artifact_digest
    else:
        session_date = str(session.get("session_date") or "")
        artifact_digest = str(session.get("artifact_digest") or "")
    if (
        not re.fullmatch(r"\d{4}-\d{2}-\d{2}", session_date)
        or not re.fullmatch(r"[0-9a-f]{64}", artifact_digest)
    ):
        raise OfficialExecutionIntakeError("official execution filename identity is invalid")
    return f"official-execution-session-{session_date}-{artifact_digest}.json"


def _store_fingerprint(
    registry_path: Path,
    raw_file_root: Path,
    session_directory: Path,
    sessions: tuple[VerifiedOfficialExecutionSession, ...],
    *,
    catalog: _CatalogFingerprint | None = None,
) -> _StoreFingerprint:
    resolved_catalog = catalog or _catalog_fingerprint(
        registry_path,
        session_directory,
    )
    try:
        raw_root = _path_fingerprint(raw_file_root, directory=True)
    except FileNotFoundError:
        if sessions:
            raise
        raw_root = None
    relative_paths = sorted(
        {
            str(receipt["relative_path"])
            for session in sessions
            for receipt in _mapping_sequence(
                session.artifact.get("receipts"),
                "official session receipts",
            )
        }
    )
    raw_files = tuple(
        _path_fingerprint(raw_file_root / relative_path, directory=False)
        for relative_path in relative_paths
    )
    return resolved_catalog, raw_root, raw_files


def _catalog_fingerprint(
    registry_path: Path,
    session_directory: Path,
) -> _CatalogFingerprint:
    registry = _path_fingerprint(registry_path, directory=False)
    try:
        directory = _path_fingerprint(session_directory, directory=True)
    except FileNotFoundError:
        return registry, None, ()
    paths = sorted(
        path
        for path in session_directory.iterdir()
        if _SESSION_FILENAME.fullmatch(path.name) is not None
    )
    return (
        registry,
        directory,
        tuple(_path_fingerprint(path, directory=False) for path in paths),
    )


def _path_fingerprint(path: Path, *, directory: bool) -> _PathFingerprint:
    try:
        facts = path.lstat()
    except FileNotFoundError:
        raise
    expected = stat.S_ISDIR(facts.st_mode) if directory else stat.S_ISREG(facts.st_mode)
    if not expected or path.is_symlink():
        raise OfficialExecutionIntakeError(
            "official execution cache fingerprint encountered an unsafe path"
        )
    return (
        str(path),
        facts.st_dev,
        facts.st_ino,
        facts.st_mode,
        facts.st_size,
        facts.st_mtime_ns,
        facts.st_ctime_ns,
    )


def _mapping_sequence(value: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise OfficialExecutionIntakeError(f"{label} are invalid")
    return [item for item in value if isinstance(item, Mapping)]


def _short_error(exc: Exception) -> str:
    detail = " ".join(str(exc).split())[:600]
    return f"{type(exc).__name__}: {detail}"


__all__ = [
    "MarketScanOfficialExecutionStore",
    "OFFICIAL_EXECUTION_SESSION_DIRECTORY_RELATIVE_PATH",
    "OfficialExecutionStoreStatus",
    "official_execution_session_filename",
]
