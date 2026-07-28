"""Stable configuration façade for application and external callers."""

from app.config_settings import (
    CACHE_PATH_ENV_NAME,
    DEFAULT_ASHARE_RADAR_LLM_ENABLED,
    DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS,
    DEFAULT_CACHE_PATH,
    DEFAULT_CORS_ALLOW_ORIGINS,
    DEFAULT_LEGACY_AUDIT_TIMEZONE,
    DEFAULT_MAX_DATABASE_SIZE_MB,
    DEFAULT_MAX_RUNTIME_BACKUPS,
    FALSE_ENV_VALUES,
    LLM_SHELL_ENV_NAMES,
    LLM_SHELL_ENV_PATH,
    MIN_QUOTE_HISTORY_RETENTION_ROWS,
    MIN_RUNTIME_BACKUP_COUNT,
    PROJECT_ROOT,
    TRUE_ENV_VALUES,
    Settings,
    env_bool,
    get_settings,
    resolve_project_path,
)
from app.config_shell import LLM_SHELL_SECRET_ENV_NAMES, load_shell_env


_load_shell_env = load_shell_env


__all__ = [
    "CACHE_PATH_ENV_NAME",
    "DEFAULT_ASHARE_RADAR_LLM_ENABLED",
    "DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS",
    "DEFAULT_CACHE_PATH",
    "DEFAULT_CORS_ALLOW_ORIGINS",
    "DEFAULT_LEGACY_AUDIT_TIMEZONE",
    "DEFAULT_MAX_DATABASE_SIZE_MB",
    "DEFAULT_MAX_RUNTIME_BACKUPS",
    "FALSE_ENV_VALUES",
    "LLM_SHELL_ENV_NAMES",
    "LLM_SHELL_ENV_PATH",
    "LLM_SHELL_SECRET_ENV_NAMES",
    "MIN_QUOTE_HISTORY_RETENTION_ROWS",
    "MIN_RUNTIME_BACKUP_COUNT",
    "PROJECT_ROOT",
    "TRUE_ENV_VALUES",
    "Settings",
    "env_bool",
    "get_settings",
    "resolve_project_path",
]
