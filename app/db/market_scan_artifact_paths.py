"""Project-managed artifact path binding."""

import hashlib
import os
from pathlib import Path
import stat


class ManagedArtifactPathError(RuntimeError):
    pass


def market_scan_artifact_lock_path(database_path: str | Path) -> Path:
    database = Path(database_path).expanduser().absolute()
    root = Path("/tmp").resolve() / f"ashare-radar-artifact-locks-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    facts = root.lstat()
    if not stat.S_ISDIR(facts.st_mode) or facts.st_uid != os.getuid() or stat.S_IMODE(facts.st_mode) != 0o700:
        raise ManagedArtifactPathError("artifact 全局租约根目录身份不可信")
    return root / f"{hashlib.sha256(str(database).encode()).hexdigest()}.lock"


def stable_market_scan_artifact_lock_root() -> Path:
    return market_scan_artifact_lock_path("placeholder").parent


def require_project_managed_artifact_database(
    target_path: str | Path,
    database_path: str | Path | None,
    managed_directory: str | Path,
) -> None:
    target = Path(target_path).expanduser().absolute()
    managed = Path(__file__).resolve().parents[2] / "data" / Path(managed_directory)
    if target.parent.resolve(strict=False) != managed.resolve(strict=False):
        return
    if target.parent != managed:
        raise ManagedArtifactPathError("受管 artifact 不能通过路径别名发布")
    production = managed.parents[len(Path(managed_directory).parts) - 1] / "ashare_radar.sqlite3"
    if database_path is None or Path(database_path).expanduser().absolute() != production:
        raise ManagedArtifactPathError("项目受管 artifact 必须绑定正式运行库")


def require_restored_market_scan_artifact_bindings(database_path: str | Path) -> None:
    """Reject DB-only restore while managed research evidence is populated."""

    database = Path(database_path).expanduser().absolute()
    managed = (
        database.parent / "market-scan-probability",
        database.parent / "research/market_scan_probability_source",
        database.parent / "research/market_scan_probability_outcomes",
        database.parent / "research/market_scan_probability_fit",
        database.parent / "research/market_scan_future_range",
        database.parent / "research/individual_probability",
    )
    summary = database.parent / "research/market-scan-future-range-summary.json"
    primary = database.parent / "research/individual_probability"
    fallback = Path(__file__).resolve().parents[2] / "docs/research/artifacts"
    project_data = Path(__file__).resolve().parents[2] / "data"
    try:
        populated = any(_managed_path_populated(path) for path in managed)
        fallback_populated = database.parent == project_data and not _managed_path_populated(primary) and _managed_path_populated(fallback)
    except OSError as exc:
        raise ManagedArtifactPathError("恢复后无法枚举受管 research artifacts") from exc
    if populated or fallback_populated or summary.exists() or summary.is_symlink():
        raise ManagedArtifactPathError("DB-only restore 不能证明现存受管 research artifacts 与恢复快照同源；请成套恢复或先归档 artifacts")


def _managed_path_populated(path: Path) -> bool:
    try:
        facts = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(facts.st_mode):
        raise ManagedArtifactPathError("受管 research artifact 路径不是普通目录")
    return next(path.iterdir(), None) is not None


__all__ = [
    "ManagedArtifactPathError",
    "market_scan_artifact_lock_path",
    "require_project_managed_artifact_database",
    "require_restored_market_scan_artifact_bindings",
    "stable_market_scan_artifact_lock_root",
]
