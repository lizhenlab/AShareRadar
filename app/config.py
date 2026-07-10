from functools import lru_cache
import os
from pathlib import Path
import shlex
from pydantic import BaseModel, Field


LLM_SHELL_ENV_PATH = Path.home() / ".zshrc"
LLM_SHELL_ENV_NAMES = {
    "ASHARE_RADAR_LLM_ENABLED",
    "ASHARE_RADAR_LLM_API_KEY",
    "ASHARE_RADAR_LLM_BASE_URL",
    "ASHARE_RADAR_LLM_MODEL",
    "ASHARE_RADAR_LLM_TIMEOUT_SECONDS",
}
DEFAULT_CORS_ALLOW_ORIGINS = ("http://127.0.0.1:8010", "http://localhost:8010")
DEFAULT_ASHARE_RADAR_LLM_ENABLED = True
DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS = 12.0
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
FALSE_ENV_VALUES = {"0", "false", "no", "off"}


def _env_tuple(name: str, default: tuple[str, ...], *, aliases: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _env_text(name, aliases=aliases)
    if not raw:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


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
    return default


def env_bool(name: str, default: bool, *, aliases: tuple[str, ...] = ()) -> bool:
    return _env_bool(name, default, aliases=aliases)


def _env_int(name: str, default: int, *, minimum: int | None = None, aliases: tuple[str, ...] = ()) -> int:
    raw = _env_text(name, aliases=aliases)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None, aliases: tuple[str, ...] = ()) -> float:
    raw = _env_text(name, aliases=aliases)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _first_env_value(name: str, aliases: tuple[str, ...]) -> str | None:
    for candidate in (name, *aliases):
        raw = os.getenv(candidate)
        if raw is not None:
            return raw
    for candidate in (name, *aliases):
        raw = _SHELL_ENV_VALUES.get(candidate)
        if raw is not None:
            return raw
    return None


def _load_shell_env(path: Path, names: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _shell_env_lines(path):
        values.update(_shell_env_line_values(line, names))
    return values


def _shell_env_lines(path: Path) -> tuple[str, ...]:
    try:
        return tuple(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return ()


def _shell_env_line_values(line: str, names: set[str]) -> dict[str, str]:
    parts = _shell_env_words(line)
    if parts[:1] == ("export",):
        parts = parts[1:]
    values: dict[str, str] = {}
    for part in parts:
        assignment = _shell_env_assignment(part, names)
        if assignment is not None:
            key, value = assignment
            values[key] = value
    return values


def _shell_env_words(line: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(line, comments=True, posix=True))
    except ValueError:
        return ()


def _shell_env_assignment(part: str, names: set[str]) -> tuple[str, str] | None:
    if "=" not in part:
        return None
    key, value = part.split("=", 1)
    stripped = value.strip()
    if key not in names or not stripped:
        return None
    return key, stripped


_SHELL_ENV_VALUES = _load_shell_env(LLM_SHELL_ENV_PATH, LLM_SHELL_ENV_NAMES)


class Settings(BaseModel):
    app_name: str = "AShareRadar"
    cors_allow_origins: tuple[str, ...] = Field(
        default_factory=lambda: _env_tuple(
            "ASHARE_RADAR_CORS_ALLOW_ORIGINS",
            DEFAULT_CORS_ALLOW_ORIGINS,
            aliases=("CORS_ALLOW_ORIGINS",),
        )
    )
    data_provider: str = "datahub"
    quote_provider_priority: tuple[str, ...] = ("tencent", "akshare")
    kline_provider_priority: tuple[str, ...] = ("tencent", "akshare", "baostock")
    minute_provider_priority: tuple[str, ...] = ("futu", "akshare")
    stock_provider_priority: tuple[str, ...] = ("akshare", "tushare", "baostock", "local")
    plate_provider_priority: tuple[str, ...] = ("akshare", "local")
    cache_path: Path = Path("data/ashare_radar.sqlite3")
    demo_provider_enabled: bool = Field(
        default_factory=lambda: _env_bool("ASHARE_RADAR_DEMO_PROVIDER_ENABLED", False, aliases=("DEMO_PROVIDER_ENABLED",))
    )
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
    tushare_token: str | None = Field(
        default_factory=lambda: _env_text("ASHARE_RADAR_TUSHARE_TOKEN", aliases=("TUSHARE_TOKEN",))
    )
    futu_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_FUTU_ENABLED", False, aliases=("FUTU_ENABLED",)))
    futu_host: str = Field(default_factory=lambda: str(_env_text("ASHARE_RADAR_FUTU_HOST", "127.0.0.1", aliases=("FUTU_HOST",))))
    futu_port: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_FUTU_PORT", 11111, minimum=1, aliases=("FUTU_PORT",)))
    quote_refresh_seconds: int = 3
    request_timeout_seconds: float = 8.0
    provider_call_timeout_seconds: float = 8.0
    workbench_optional_timeout_seconds: float = 1.5
    provider_failure_cooldown_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_PROVIDER_FAILURE_COOLDOWN_SECONDS",
            90,
            minimum=1,
            aliases=("PROVIDER_FAILURE_COOLDOWN_SECONDS",),
        )
    )
    llm_enabled: bool = Field(
        default_factory=lambda: _env_bool("ASHARE_RADAR_LLM_ENABLED", DEFAULT_ASHARE_RADAR_LLM_ENABLED)
    )
    llm_api_key: str | None = Field(default_factory=lambda: _env_text("ASHARE_RADAR_LLM_API_KEY"))
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
    max_quote_history_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS", 50000, minimum=1, aliases=("MAX_QUOTE_HISTORY_ROWS",))
    )
    max_minute_kline_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_MINUTE_KLINE_ROWS", 20000, minimum=1, aliases=("MAX_MINUTE_KLINE_ROWS",))
    )
    max_stock_concept_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_STOCK_CONCEPT_ROWS", 20000, minimum=1, aliases=("MAX_STOCK_CONCEPT_ROWS",))
    )
    max_task_run_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_TASK_RUN_ROWS", 2000, minimum=1, aliases=("MAX_TASK_RUN_ROWS",))
    )
    max_monitor_event_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_MONITOR_EVENT_ROWS", 3000, minimum=1, aliases=("MAX_MONITOR_EVENT_ROWS",))
    )
    max_cache_event_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_CACHE_EVENT_ROWS", 5000, minimum=1, aliases=("MAX_CACHE_EVENT_ROWS",))
    )
    max_alert_event_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_ALERT_EVENT_ROWS", 5000, minimum=1, aliases=("MAX_ALERT_EVENT_ROWS",))
    )
    max_advice_history_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_ADVICE_HISTORY_ROWS", 20000, minimum=1, aliases=("MAX_ADVICE_HISTORY_ROWS",))
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
