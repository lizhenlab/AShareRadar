"""Environment-backed application settings implementation."""

from functools import lru_cache
import math
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.config_shell import load_shell_env as _load_shell_env
from app.config_validation import normalized_llm_base_url as _normalized_llm_base_url
from app.config_validation import normalized_timezone_name as _normalized_timezone_name


LLM_SHELL_ENV_PATH = Path.home() / ".zshrc"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_PATH = PROJECT_ROOT / "data" / "ashare_radar.sqlite3"
CACHE_PATH_ENV_NAME = "ASHARE_RADAR_CACHE_PATH"
LLM_SHELL_ENV_NAMES = {
    "ASHARE_RADAR_LLM_ENABLED",
    "ASHARE_RADAR_LLM_API_KEY",
    "ASHARE_RADAR_LLM_BASE_URL",
    "ASHARE_RADAR_LLM_MODEL",
    "ASHARE_RADAR_LLM_TIMEOUT_SECONDS",
}
LLM_SHELL_SECRET_ENV_NAMES = {"ASHARE_RADAR_LLM_API_KEY"}
DEFAULT_CORS_ALLOW_ORIGINS = ("http://127.0.0.1:8010", "http://localhost:8010")
DEFAULT_ASHARE_RADAR_LLM_ENABLED = True
DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS = 30.0
DEFAULT_LEGACY_AUDIT_TIMEZONE = "Asia/Shanghai"
MIN_QUOTE_HISTORY_RETENTION_ROWS = 120
MIN_RUNTIME_BACKUP_COUNT = 2
DEFAULT_MAX_DATABASE_SIZE_MB = 2048
DEFAULT_MAX_RUNTIME_BACKUPS = MIN_RUNTIME_BACKUP_COUNT
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
FALSE_ENV_VALUES = {"0", "false", "no", "off"}
REGISTERED_PROVIDER_NAMES = ("tencent", "akshare", "tushare", "baostock", "futu", "local", "demo")
KNOWN_PROVIDER_NAMES = frozenset(REGISTERED_PROVIDER_NAMES)


def _env_tuple(name: str, default: tuple[str, ...], *, aliases: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _env_text(name, aliases=aliases)
    if not raw:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


def _env_int_tuple(
    name: str,
    default: tuple[int, ...],
    *,
    minimum: int,
    maximum: int,
) -> tuple[int, ...]:
    raw = _env_text(name)
    if raw is None:
        return default
    parts = tuple(item.strip() for item in raw.split(","))
    if not parts or any(not item for item in parts):
        raise ValueError(f"{name} 必须是逗号分隔的整数列表")
    try:
        values = tuple(int(item) for item in parts)
    except ValueError:
        raise ValueError(f"{name} 必须是逗号分隔的整数列表") from None
    if any(value < minimum or value > maximum for value in values):
        raise ValueError(f"{name} 中的每个值必须在 {minimum} 到 {maximum} 之间")
    return values


def _env_provider_priority(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    return _normalized_provider_priority(
        _env_tuple(name, default),
        setting_name=name,
        reject_unknown=True,
    )


def _normalized_provider_priority(
    value: object,
    *,
    setting_name: str,
    reject_unknown: bool = False,
) -> tuple[str, ...]:
    raw_names = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw_names, (list, tuple)):
        raise ValueError(f"{setting_name} 必须是数据源名称列表")
    names: list[str] = []
    for raw_name in raw_names:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(f"{setting_name} 包含空白或非文本的数据源名称")
        name = raw_name.strip().lower()
        if reject_unknown and name not in KNOWN_PROVIDER_NAMES:
            allowed = ", ".join(sorted(KNOWN_PROVIDER_NAMES))
            raise ValueError(f"{setting_name} 包含未知数据源 {name!r}；可选值：{allowed}")
        if name not in names:
            names.append(name)
    return tuple(names)


def _env_text(name: str, default: str | None = None, *, aliases: tuple[str, ...] = ()) -> str | None:
    raw = _first_env_value(name, aliases)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _env_bool(name: str, default: bool, *, aliases: tuple[str, ...] = ()) -> bool:
    raw = _env_text(name, aliases=aliases)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUE_ENV_VALUES:
        return True
    if value in FALSE_ENV_VALUES:
        return False
    raise ValueError(f"{name} 必须是布尔值，支持 1/0、true/false、yes/no 或 on/off")


def env_bool(name: str, default: bool, *, aliases: tuple[str, ...] = ()) -> bool:
    return _env_bool(name, default, aliases=aliases)


def _env_int(name: str, default: int, *, minimum: int | None = None, aliases: tuple[str, ...] = ()) -> int:
    raw = _env_text(name, aliases=aliases)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是整数") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None, aliases: tuple[str, ...] = ()) -> float:
    raw = _env_text(name, aliases=aliases)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是数字") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} 必须是有限数字")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return value


def resolve_project_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    return resolved.resolve()


def _env_path(name: str, default: Path, *, aliases: tuple[str, ...] = ()) -> Path:
    raw = _env_text(name, aliases=aliases)
    return resolve_project_path(raw) if raw is not None else default


def _first_env_value(name: str, aliases: tuple[str, ...]) -> str | None:
    for candidate in (name, *aliases):
        raw = os.getenv(candidate)
        if raw is not None:
            return raw
    shell_values = _SHELL_ENV_VALUES if _SHELL_ENV_VALUES is not None else _default_shell_env_values()
    for candidate in (name, *aliases):
        raw = shell_values.get(candidate)
        if raw is not None:
            return raw
    return None


_SHELL_ENV_VALUES: dict[str, str] | None = None


@lru_cache(maxsize=1)
def _default_shell_env_values() -> dict[str, str]:
    return _load_shell_env(LLM_SHELL_ENV_PATH, LLM_SHELL_ENV_NAMES)


class Settings(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, hide_input_in_errors=True, validate_default=True)

    app_name: str = "AShareRadar"
    cors_allow_origins: tuple[str, ...] = Field(
        default_factory=lambda: _env_tuple(
            "ASHARE_RADAR_CORS_ALLOW_ORIGINS",
            DEFAULT_CORS_ALLOW_ORIGINS,
            aliases=("CORS_ALLOW_ORIGINS",),
        )
    )
    data_provider: str = "datahub"
    quote_provider_priority: tuple[str, ...] = Field(
        default_factory=lambda: _env_provider_priority(
            "ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY",
            ("tencent", "futu", "akshare"),
        )
    )
    kline_provider_priority: tuple[str, ...] = Field(
        default_factory=lambda: _env_provider_priority(
            "ASHARE_RADAR_KLINE_PROVIDER_PRIORITY",
            ("tencent", "akshare", "tushare", "baostock"),
        )
    )
    minute_provider_priority: tuple[str, ...] = Field(
        default_factory=lambda: _env_provider_priority(
            "ASHARE_RADAR_MINUTE_PROVIDER_PRIORITY",
            ("futu", "akshare"),
        )
    )
    stock_provider_priority: tuple[str, ...] = Field(
        default_factory=lambda: _env_provider_priority(
            "ASHARE_RADAR_STOCK_PROVIDER_PRIORITY",
            ("akshare", "tushare", "baostock", "local"),
        )
    )
    plate_provider_priority: tuple[str, ...] = Field(
        default_factory=lambda: _env_provider_priority(
            "ASHARE_RADAR_PLATE_PROVIDER_PRIORITY",
            ("akshare", "local"),
        )
    )
    cache_path: Path = Field(default_factory=lambda: _env_path(CACHE_PATH_ENV_NAME, DEFAULT_CACHE_PATH, aliases=("CACHE_PATH",)))
    legacy_audit_timezone: str = Field(
        default_factory=lambda: str(
            _env_text(
                "ASHARE_RADAR_LEGACY_AUDIT_TIMEZONE",
                DEFAULT_LEGACY_AUDIT_TIMEZONE,
            )
        )
    )
    demo_provider_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_DEMO_PROVIDER_ENABLED", False, aliases=("DEMO_PROVIDER_ENABLED",)))
    quote_cache_seconds: int = 8
    kline_cache_seconds: int = 60 * 60 * 6
    minute_kline_cache_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MINUTE_KLINE_CACHE_SECONDS",
            60,
            minimum=1,
            aliases=("MINUTE_KLINE_CACHE_SECONDS",),
        )
    )
    stock_pool_cache_seconds: int = 60 * 60 * 24
    stock_pool_authoritative_min_count: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_STOCK_POOL_AUTHORITATIVE_MIN_COUNT",
            1000,
            minimum=1,
            aliases=("STOCK_POOL_AUTHORITATIVE_MIN_COUNT",),
        )
    )
    plate_rank_cache_seconds: int = 60 * 10
    stock_concept_cache_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_STOCK_CONCEPT_CACHE_SECONDS",
            60 * 60 * 6,
            minimum=1,
            aliases=("STOCK_CONCEPT_CACHE_SECONDS",),
        )
    )
    tushare_token: str | None = Field(repr=False, default_factory=lambda: _env_text("ASHARE_RADAR_TUSHARE_TOKEN", aliases=("TUSHARE_TOKEN",)))
    futu_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_FUTU_ENABLED", False, aliases=("FUTU_ENABLED",)))
    futu_host: str = Field(default_factory=lambda: str(_env_text("ASHARE_RADAR_FUTU_HOST", "127.0.0.1", aliases=("FUTU_HOST",))))
    futu_port: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_FUTU_PORT", 11111, minimum=1, aliases=("FUTU_PORT",)))
    quote_refresh_seconds: int = 3
    request_timeout_seconds: float = 8.0
    provider_call_timeout_seconds: float = 8.0
    stock_pool_provider_timeout_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_STOCK_POOL_PROVIDER_TIMEOUT_SECONDS",
            60.0,
            minimum=1.0,
        ),
        le=300,
    )
    workbench_optional_timeout_seconds: float = 1.5
    provider_failure_cooldown_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_PROVIDER_FAILURE_COOLDOWN_SECONDS",
            90,
            minimum=1,
            aliases=("PROVIDER_FAILURE_COOLDOWN_SECONDS",),
        )
    )
    llm_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_LLM_ENABLED", DEFAULT_ASHARE_RADAR_LLM_ENABLED))
    llm_api_key: str | None = Field(
        default_factory=lambda: _env_text("ASHARE_RADAR_LLM_API_KEY"),
        repr=False,
    )
    llm_base_url: str | None = Field(default_factory=lambda: _env_text("ASHARE_RADAR_LLM_BASE_URL"))
    llm_model: str | None = Field(default_factory=lambda: _env_text("ASHARE_RADAR_LLM_MODEL"))
    llm_timeout_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_LLM_TIMEOUT_SECONDS",
            DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS,
            minimum=0.1,
        )
    )
    scheduler_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_SCHEDULER_ENABLED", True, aliases=("SCHEDULER_ENABLED",)))
    scheduler_quote_interval_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_SCHEDULER_QUOTE_INTERVAL_SECONDS",
            30,
            minimum=1,
            aliases=("SCHEDULER_QUOTE_INTERVAL_SECONDS",),
        )
    )
    scheduler_kline_interval_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_SCHEDULER_KLINE_INTERVAL_SECONDS",
            900,
            minimum=1,
            aliases=("SCHEDULER_KLINE_INTERVAL_SECONDS",),
        )
    )
    scheduler_plate_interval_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_SCHEDULER_PLATE_INTERVAL_SECONDS",
            300,
            minimum=1,
            aliases=("SCHEDULER_PLATE_INTERVAL_SECONDS",),
        )
    )
    scheduler_health_interval_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_SCHEDULER_HEALTH_INTERVAL_SECONDS",
            45,
            minimum=1,
            aliases=("SCHEDULER_HEALTH_INTERVAL_SECONDS",),
        )
    )
    scheduler_kline_symbols_limit: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_SCHEDULER_KLINE_SYMBOLS_LIMIT",
            5,
            minimum=1,
            aliases=("SCHEDULER_KLINE_SYMBOLS_LIMIT",),
        )
    )
    scheduler_shutdown_timeout_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS",
            5.0,
            minimum=0.1,
            aliases=("SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS",),
        )
    )
    market_scan_auto_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_MARKET_SCAN_AUTO_ENABLED", False))
    market_scan_preflight_enabled: bool = Field(
        default_factory=lambda: _env_bool("ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_ENABLED", True)
    )
    market_scan_preflight_timeout_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_TIMEOUT_SECONDS",
            30.0,
            minimum=0.1,
        ),
        le=300,
    )
    market_scan_auto_retry_delays_seconds: tuple[int, ...] = Field(
        default_factory=lambda: _env_int_tuple(
            "ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS",
            (600, 1800, 3600),
            minimum=1,
            maximum=86400,
        ),
        min_length=1,
    )
    market_scan_auto_retry_max_attempts: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS",
            3,
            minimum=0,
        ),
        le=10,
    )
    market_scan_schedule_hour: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_SCHEDULE_HOUR", 16, minimum=0),
        le=23,
    )
    market_scan_schedule_minute: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_SCHEDULE_MINUTE", 30, minimum=0),
        le=59,
    )
    market_scan_batch_size: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_BATCH_SIZE", 50, minimum=1),
        le=500,
    )
    market_scan_concurrency: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_CONCURRENCY", 5, minimum=1),
        le=32,
    )
    market_scan_kline_limit: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_KLINE_LIMIT", 260, minimum=60),
        le=1000,
    )
    market_scan_min_history_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_HISTORY_ROWS", 60, minimum=60),
        le=260,
    )
    market_scan_min_data_quality_score: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE", 50, minimum=0),
        le=100,
    )
    market_scan_min_universe_count: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_UNIVERSE_COUNT", 4000, minimum=1))
    market_scan_min_sh_count: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_SH_COUNT", 1800, minimum=1))
    market_scan_min_sz_count: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_SZ_COUNT", 2500, minimum=1))
    market_scan_min_bj_count: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_BJ_COUNT", 200, minimum=1))
    market_scan_symbol_timeout_seconds: float = Field(
        default_factory=lambda: _env_float("ASHARE_RADAR_MARKET_SCAN_SYMBOL_TIMEOUT_SECONDS", 30.0, minimum=0.1),
        le=300,
    )
    market_scan_quote_batch_timeout_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_MARKET_SCAN_QUOTE_BATCH_TIMEOUT_SECONDS",
            60.0,
            minimum=0.1,
        ),
        le=600,
    )
    market_scan_retry_attempts: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_RETRY_ATTEMPTS", 2, minimum=1),
        le=5,
    )
    market_scan_retry_backoff_seconds: float = Field(
        default_factory=lambda: _env_float("ASHARE_RADAR_MARKET_SCAN_RETRY_BACKOFF_SECONDS", 1.0, minimum=0),
        le=30,
    )
    market_scan_batch_retry_attempts: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_BATCH_RETRY_ATTEMPTS", 3, minimum=1),
        le=5,
    )
    market_scan_provider_wait_budget_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_MARKET_SCAN_PROVIDER_WAIT_BUDGET_SECONDS",
            120.0,
            minimum=0,
        ),
        le=600,
    )
    market_scan_new_stock_days: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_NEW_STOCK_DAYS", 120, minimum=1),
        le=730,
    )
    max_quote_history_rows: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS",
            MIN_QUOTE_HISTORY_RETENTION_ROWS,
            minimum=MIN_QUOTE_HISTORY_RETENTION_ROWS,
            aliases=("MAX_QUOTE_HISTORY_ROWS",),
        ),
        ge=MIN_QUOTE_HISTORY_RETENTION_ROWS,
        le=50000,
    )
    max_daily_kline_rows: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MAX_DAILY_KLINE_ROWS",
            260,
            minimum=60,
            aliases=("MAX_DAILY_KLINE_ROWS",),
        ),
        ge=60,
        le=5000,
    )
    max_minute_kline_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_MINUTE_KLINE_ROWS", 20000, minimum=1, aliases=("MAX_MINUTE_KLINE_ROWS",))
    )
    max_stock_concept_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_STOCK_CONCEPT_ROWS", 20000, minimum=1, aliases=("MAX_STOCK_CONCEPT_ROWS",))
    )
    max_task_run_rows: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MAX_TASK_RUN_ROWS", 2000, minimum=1, aliases=("MAX_TASK_RUN_ROWS",)))
    max_reliability_bucket_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_RELIABILITY_BUCKET_ROWS", 10000, minimum=1)
    )
    max_market_scan_runs: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MAX_MARKET_SCAN_RUNS", 30, minimum=1))
    max_monitor_event_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_MONITOR_EVENT_ROWS", 3000, minimum=1, aliases=("MAX_MONITOR_EVENT_ROWS",))
    )
    max_cache_event_rows: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MAX_CACHE_EVENT_ROWS", 5000, minimum=1, aliases=("MAX_CACHE_EVENT_ROWS",)))
    max_alert_event_rows: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MAX_ALERT_EVENT_ROWS", 5000, minimum=1, aliases=("MAX_ALERT_EVENT_ROWS",)))
    max_advice_history_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_ADVICE_HISTORY_ROWS", 20000, minimum=1, aliases=("MAX_ADVICE_HISTORY_ROWS",))
    )
    max_database_size_mb: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MAX_DATABASE_SIZE_MB",
            DEFAULT_MAX_DATABASE_SIZE_MB,
            minimum=16,
        )
    )
    runtime_maintenance_interval_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_RUNTIME_MAINTENANCE_INTERVAL_SECONDS",
            60 * 60,
            minimum=60,
        ),
        ge=60,
        le=7 * 24 * 60 * 60,
    )
    max_runtime_backups: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MAX_RUNTIME_BACKUPS",
            DEFAULT_MAX_RUNTIME_BACKUPS,
            minimum=MIN_RUNTIME_BACKUP_COUNT,
        ),
        ge=MIN_RUNTIME_BACKUP_COUNT,
        le=100,
    )
    advice_history_dedupe_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_ADVICE_HISTORY_DEDUPE_SECONDS",
            180,
            minimum=0,
            aliases=("ADVICE_HISTORY_DEDUPE_SECONDS",),
        )
    )
    quote_stale_warning_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_QUOTE_STALE_WARNING_SECONDS",
            900,
            minimum=1,
            aliases=("QUOTE_STALE_WARNING_SECONDS",),
        )
    )
    quote_consistency_warning_pct: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_QUOTE_CONSISTENCY_WARNING_PCT",
            1.0,
            minimum=0.0,
            aliases=("QUOTE_CONSISTENCY_WARNING_PCT",),
        )
    )
    seed_symbols: tuple[str, ...] = (
        "600519",
        "000001",
        "300750",
        "601318",
        "000858",
        "002594",
        "600036",
        "600900",
        "000333",
        "002475",
    )

    @field_validator("cache_path")
    @classmethod
    def _resolve_cache_path(cls, value: Path) -> Path:
        return resolve_project_path(value)

    @field_validator("llm_base_url")
    @classmethod
    def _validate_llm_base_url(cls, value: str | None) -> str | None:
        return _normalized_llm_base_url(value)

    @field_validator("legacy_audit_timezone")
    @classmethod
    def _validate_legacy_audit_timezone(cls, value: str) -> str:
        return _normalized_timezone_name(value)

    @field_validator(
        "quote_provider_priority",
        "kline_provider_priority",
        "minute_provider_priority",
        "stock_provider_priority",
        "plate_provider_priority",
        mode="before",
    )
    @classmethod
    def _validate_provider_priority(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        return _normalized_provider_priority(value, setting_name=str(info.field_name))

    @field_validator("market_scan_auto_retry_delays_seconds")
    @classmethod
    def _validate_market_scan_auto_retry_delays(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(delay < 1 or delay > 86400 for delay in value):
            raise ValueError("market_scan_auto_retry_delays_seconds 中的每个值必须在 1 到 86400 之间")
        if any(current >= following for current, following in zip(value, value[1:], strict=False)):
            raise ValueError("market_scan_auto_retry_delays_seconds 必须严格递增")
        return value

    @model_validator(mode="after")
    def _validate_market_scan_limits(self) -> "Settings":
        if self.market_scan_min_history_rows > self.market_scan_kline_limit:
            raise ValueError("market_scan_min_history_rows 不能大于 market_scan_kline_limit")
        if self.max_daily_kline_rows < self.market_scan_kline_limit:
            raise ValueError("max_daily_kline_rows 不能小于 market_scan_kline_limit")
        if self.market_scan_auto_enabled and not self.scheduler_enabled:
            raise ValueError("market_scan_auto_enabled 开启时必须同时开启 scheduler_enabled")
        if self.market_scan_auto_retry_max_attempts > len(self.market_scan_auto_retry_delays_seconds):
            raise ValueError(
                "market_scan_auto_retry_max_attempts 不能大于 "
                "market_scan_auto_retry_delays_seconds 的数量"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
