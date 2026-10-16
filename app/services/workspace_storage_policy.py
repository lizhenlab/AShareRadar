"""Metadata-only path admission and explicit exclusions for storage previews."""

from __future__ import annotations

from pathlib import Path
import errno
import sys

from app.artifacts.io import path_has_only_trusted_aliases
from app.models.workspace_storage import WorkspaceStorageCategory


EXCLUSION_DESCRIPTIONS = (
    "秘密配置：.env、.env.*、.aws/.ssh、credentials/secrets 名称、API key 名称及 .key/.pem/.p12/.pfx 后缀；仅按名称识别，不读取内容。",
    "日志：logs/log 目录、*.log 及 *.log.*。",
    "运行锁：*.lock、*.lock.* 及 lock/locks 目录。",
    "嵌套备份：根目录以内的 backup/backups/local-backups 目录；显式 backup 类别根本身可纳入。",
    "生产环境：.venv/venv/virtualenv/__pypackages__ 目录；runtime 类别允许纳入普通文件，但符号链接仍不跟随。",
    "工具缓存：__pycache__/.pytest_cache/.mypy_cache/.ruff_cache；runtime 类别保留，以免遗漏封存环境内文件。",
    "显式根的祖先同样应用秘密配置、日志、锁和备份规则；runtime 不豁免这些规则。",
)
_SECRET_NAMES = frozenset({".env", ".aws", ".ssh", "credentials", "secrets", "fuyao-api-key", "api-key", "api_key"})
_SECRET_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
_ENVIRONMENT_NAMES = frozenset({".venv", "venv", "virtualenv", "__pypackages__"})
_BACKUP_NAMES = frozenset({"backup", "backups", "local-backups"})
_CACHE_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def admitted_lexical_path(path: Path) -> Path | None:
    """Never resolve arbitrary aliases; admit only the shared Darwin aliases."""
    try:
        trusted = path_has_only_trusted_aliases(path)
    except RuntimeError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        raise
    if not trusted:
        return None
    if sys.platform == "darwin" and len(path.parts) > 1 and path.parts[1] in {"tmp", "var"}:
        return Path("/private").joinpath(*path.parts[1:])
    return path


def storage_exclusion(path: Path, category: WorkspaceStorageCategory, *, is_root: bool) -> str | None:
    ancestor_reason = _ancestor_exclusion(path) if is_root else None
    if ancestor_reason is not None:
        return ancestor_reason
    filename_reason = _filename_exclusion(path.name.casefold())
    if filename_reason is not None:
        return filename_reason
    if path.name.casefold() in _BACKUP_NAMES and (not is_root or category != "backup"):
        return "nested_backup"
    return _environment_exclusion(path, category)


def _ancestor_exclusion(path: Path) -> str | None:
    for part in path.parent.parts:
        name = part.casefold()
        filename_reason = _filename_exclusion(name)
        if filename_reason is not None:
            return filename_reason
        if name in _BACKUP_NAMES:
            return "nested_backup"
    return None


def _filename_exclusion(name: str) -> str | None:
    if _is_secret(name):
        return "secret_configuration"
    if name in {"log", "logs"} or name.endswith(".log") or ".log." in name:
        return "log"
    if name in {"lock", "locks"} or name.endswith(".lock") or ".lock." in name:
        return "lock"
    return None


def _environment_exclusion(path: Path, category: WorkspaceStorageCategory) -> str | None:
    if category == "runtime":
        return None
    if any(part.casefold() in _ENVIRONMENT_NAMES for part in path.parts):
        return "production_environment"
    if any(part.casefold() in _CACHE_NAMES for part in path.parts):
        return "tool_cache"
    return None


def _is_secret(name: str) -> bool:
    return (name in _SECRET_NAMES or name.startswith((".env.", "credentials.", "secrets."))
            or name.endswith(_SECRET_SUFFIXES) or name.endswith(("-api-key", "_api_key")))


__all__ = ["EXCLUSION_DESCRIPTIONS", "admitted_lexical_path", "storage_exclusion"]
